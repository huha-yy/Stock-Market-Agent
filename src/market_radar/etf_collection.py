from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from statistics import fmean, pstdev
import time
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from data_provider.base import sanitize_persisted_text
from src.market_radar.capabilities import CapabilityResult, EtfCapabilityData
from src.market_radar.enrichment import RunScopedCapabilityCircuit, _BoundedScheduler
from src.market_radar.models import (
    EtfDefinition,
    EtfObservation,
    SectorDefinition,
    SectorScore,
)


_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_ERROR_LIMIT = 256
_TRACE_LIMIT = 1200


@dataclass(frozen=True)
class EtfCollectionConfig:
    candidate_limit: int = 30
    total_budget_seconds: int = 90
    max_concurrency: int = 6

    def __post_init__(self) -> None:
        if self.candidate_limit != 30:
            raise ValueError("ETF candidate limit is fixed at 30")
        if self.total_budget_seconds != 90:
            raise ValueError("ETF collection budget is fixed at 90 seconds")
        if self.max_concurrency != 6:
            raise ValueError("ETF collection concurrency is fixed at 6")


@dataclass(frozen=True)
class EtfCollectionBatch:
    observations: tuple[EtfObservation, ...]
    trace: tuple[Mapping[str, Any], ...]
    as_of: datetime


def _is_effective(etf: EtfDefinition, market_date) -> bool:
    return etf.effective_from <= market_date and (
        etf.effective_to is None or market_date <= etf.effective_to
    )


def _candidate_selection(
    universe: Sequence[SectorDefinition],
    sectors: Sequence[SectorScore],
    as_of: datetime,
    *,
    limit: int,
) -> tuple[
    tuple[tuple[SectorScore, EtfDefinition], ...],
    tuple[Mapping[str, Any], ...],
]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    market_date = as_of.astimezone(_CN_TIMEZONE).date()
    definitions: dict[str, SectorDefinition] = {}
    for sector in universe:
        if sector.effective_from > market_date or (
            sector.effective_to is not None and sector.effective_to < market_date
        ):
            continue
        existing = definitions.get(sector.sector_id)
        if existing is None or sector.effective_from > existing.effective_from:
            definitions[sector.sector_id] = sector

    selected: list[tuple[SectorScore, EtfDefinition]] = []
    trace: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for sector_rank, score in enumerate(sectors, start=1):
        definition = definitions.get(score.sector_id)
        if definition is None:
            trace.append(
                MappingProxyType(
                    {
                        "sector_id": score.sector_id,
                        "result": "sector_not_in_effective_universe",
                    }
                )
            )
            continue
        ordered = sorted(
            enumerate(definition.etfs), key=lambda pair: (pair[0], pair[1].code)
        )
        for curated_order, etf in ordered:
            base_trace = {
                "sector_id": score.sector_id,
                "code": etf.code,
                "sector_rank": sector_rank,
                "curated_order": curated_order,
            }
            if not _is_effective(etf, market_date):
                trace.append(
                    MappingProxyType({**base_trace, "result": "inactive_mapping"})
                )
                continue
            if etf.code in seen:
                trace.append(MappingProxyType({**base_trace, "result": "duplicate"}))
                continue
            seen.add(etf.code)
            if len(selected) >= limit:
                trace.append(
                    MappingProxyType({**base_trace, "result": "limit_exceeded"})
                )
                continue
            selected.append((score, etf))
    return tuple(selected), tuple(trace[:_TRACE_LIMIT])


def _select_effective_candidates(
    universe: Sequence[SectorDefinition],
    sectors: Sequence[SectorScore],
    as_of: datetime,
    *,
    limit: int = 30,
) -> tuple[tuple[SectorScore, EtfDefinition], ...]:
    return _candidate_selection(universe, sectors, as_of, limit=limit)[0]


class CnEtfObservationBuilder:
    @staticmethod
    def _missing_fields(values: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(
            field
            for field in EtfObservation.tracked_metric_fields
            if (
                not values[field]
                if field in {"daily_return_dates_60", "daily_returns_60"}
                else values[field] is None
            )
        )

    @staticmethod
    def _capability_reference(result: CapabilityResult) -> Mapping[str, Any]:
        return {
            "status": result.status,
            "source": result.source,
            "data_date": result.data_date,
            "bar_status": result.bar_status,
            "freshness_seconds": result.freshness_seconds,
            "trace": result.trace,
            "error": result.error,
        }

    def build(
        self,
        sector: SectorScore,
        etf: EtfDefinition,
        result: CapabilityResult[EtfCapabilityData],
        *,
        observed_at: datetime,
    ) -> EtfObservation:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        data = result.data
        values: dict[str, Any] = {
            "data_date": result.data_date,
            "bar_status": result.bar_status,
            "active": None,
            "finalized_session_count": None,
            "suspended": None,
            "current_price": None,
            "current_traded_amount": None,
            "average_traded_amount_20d": None,
            "spread_bps": None,
            "premium_discount_pct": None,
            "return_20d_pct": None,
            "return_60d_pct": None,
            "daily_return_dates_60": (),
            "daily_returns_60": (),
            "tracking_error_pct": None,
            "tracking_difference_pct": None,
            "annual_fee_pct": None,
            "size_cny": None,
            "liquidity_stability": None,
        }
        if data is not None:
            finalized = (
                data.bars[:-1]
                if result.bar_status == "provisional"
                else data.bars
            )
            values.update(
                {
                    "active": data.active,
                    "finalized_session_count": len(finalized),
                    "suspended": data.suspended,
                    "current_price": data.current_price,
                    "current_traded_amount": data.current_traded_amount,
                    "tracking_error_pct": data.tracking_error_pct,
                    "tracking_difference_pct": data.tracking_difference_pct,
                    "annual_fee_pct": data.annual_fee_pct,
                }
            )
            if len(finalized) >= 20:
                amounts = tuple(bar.traded_amount for bar in finalized[-20:])
                average_amount = fmean(amounts)
                values["average_traded_amount_20d"] = average_amount
                if average_amount > 0:
                    values["liquidity_stability"] = average_amount / (
                        average_amount + pstdev(amounts)
                    )
            if len(finalized) >= 21:
                values["return_20d_pct"] = (
                    finalized[-1].close / finalized[-21].close - 1
                ) * 100
            if len(finalized) >= 61:
                window = finalized[-61:]
                values["return_60d_pct"] = (
                    window[-1].close / window[0].close - 1
                ) * 100
                values["daily_return_dates_60"] = tuple(
                    bar.data_date for bar in window[1:]
                )
                values["daily_returns_60"] = tuple(
                    window[index].close / window[index - 1].close - 1
                    for index in range(1, len(window))
                )
            if data.bid_price is not None and data.ask_price is not None:
                midpoint = (data.bid_price + data.ask_price) / 2
                values["spread_bps"] = (
                    (data.ask_price - data.bid_price) / midpoint * 10_000
                )
            if data.current_price is not None and data.nav is not None:
                values["premium_discount_pct"] = (
                    data.current_price / data.nav - 1
                ) * 100
            values["size_cny"] = data.net_assets_cny
            if (
                values["size_cny"] is None
                and data.shares is not None
                and data.current_price is not None
            ):
                values["size_cny"] = data.shares * data.current_price

        quality = {
            "ok": "complete",
            "partial": "partial",
            "stale": "stale",
            "unavailable": "unavailable",
        }[result.status]
        reference = {
            "normalized_code": data.code if data is not None else None,
            "normalized_data_date": result.data_date,
            "sector_confidence": sector.confidence,
            "sector_quality": sector.quality,
            "capability": self._capability_reference(result),
        }
        return EtfObservation(
            sector_id=etf.sector_id,
            code=etf.code,
            name=etf.name,
            observed_at=observed_at,
            source=result.source,
            quality=quality,
            freshness_seconds=result.freshness_seconds,
            mapping_effective_from=etf.effective_from,
            mapping_effective_to=etf.effective_to,
            benchmark_code=etf.benchmark_code,
            **values,
            missing_fields=self._missing_fields(values),
            raw_reference=reference,
        )


class MarketRadarEtfCollector:
    def __init__(
        self,
        *,
        provider: Any,
        config: EtfCollectionConfig | None = None,
        builder: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        executor_factory: Callable[[int], Any] = ThreadPoolExecutor,
    ) -> None:
        self.provider = provider
        self.config = config or EtfCollectionConfig()
        self.builder = builder or CnEtfObservationBuilder()
        self.monotonic = monotonic
        self.executor_factory = executor_factory

    def _scheduler(self, deadline: float) -> _BoundedScheduler:
        return _BoundedScheduler(
            executor_factory=self.executor_factory,
            max_active=self.config.max_concurrency,
            deadline=deadline,
            monotonic=self.monotonic,
        )

    @staticmethod
    def _unavailable(as_of: datetime, error: str) -> CapabilityResult:
        return CapabilityResult(
            capability="etf_snapshot",
            status="unavailable",
            data=None,
            source="market_radar_etf_collection",
            observed_at=as_of,
            data_date=None,
            bar_status=None,
            freshness_seconds=0,
            trace=(),
            error=sanitize_persisted_text(error, _ERROR_LIMIT),
        )

    @staticmethod
    def _supported_kwargs(method: Callable[..., Any], values: Mapping[str, Any]):
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return {}
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return {
            name: value
            for name, value in values.items()
            if accepts_kwargs or name in names
        }

    def _fetch(
        self,
        candidate: tuple[SectorScore, EtfDefinition],
        as_of: datetime,
        deadline: float,
        circuit: RunScopedCapabilityCircuit,
    ) -> CapabilityResult[EtfCapabilityData]:
        if self.monotonic() >= deadline:
            return self._unavailable(as_of, "deadline_exceeded")
        method = getattr(self.provider, "fetch_etf", None)
        if not callable(method):
            return self._unavailable(as_of, "provider_invalid_result_type")
        kwargs = self._supported_kwargs(
            method,
            {
                "attempt_policy": circuit,
                "deadline_monotonic": deadline,
                "monotonic": self.monotonic,
            },
        )
        try:
            result = method(candidate[1], as_of, **kwargs)
        except Exception as exc:
            return self._unavailable(
                as_of, f"provider_exception:{type(exc).__name__}"
            )
        if not isinstance(result, CapabilityResult) or (
            result.data is not None and not isinstance(result.data, EtfCapabilityData)
        ):
            return self._unavailable(as_of, "provider_invalid_result_type")
        return result

    def collect(
        self,
        universe: Sequence[SectorDefinition],
        sectors: Sequence[SectorScore],
        as_of: datetime,
    ) -> EtfCollectionBatch:
        candidates, selection_trace = _candidate_selection(
            universe,
            sectors,
            as_of,
            limit=self.config.candidate_limit,
        )
        if not candidates:
            return EtfCollectionBatch((), selection_trace, as_of)
        deadline = self.monotonic() + self.config.total_budget_seconds
        circuit = RunScopedCapabilityCircuit()
        results, unfinished = self._scheduler(deadline).run(
            candidates,
            lambda candidate: self._fetch(candidate, as_of, deadline, circuit),
        )
        normalized = {
            index: results.get(
                index, self._unavailable(as_of, "deadline_exceeded")
            )
            for index in range(len(candidates))
        }
        anchor = max(
            (as_of, *(item.observed_at for item in normalized.values())),
            key=lambda value: value.astimezone(timezone.utc),
        ).astimezone(timezone.utc)
        observations = tuple(
            self.builder.build(score, etf, normalized[index], observed_at=anchor)
            for index, (score, etf) in enumerate(candidates)
        )
        trace: list[Mapping[str, Any]] = list(selection_trace)
        trace.extend(
            MappingProxyType(
                {
                    "sector_id": candidates[index][0].sector_id,
                    "code": candidates[index][1].code,
                    "capability": "etf_snapshot",
                    "result": normalized[index].status,
                    "source": normalized[index].source,
                }
            )
            for index in range(len(candidates))
        )
        trace.extend(
            MappingProxyType(
                {
                    "sector_id": candidates[index][0].sector_id,
                    "code": candidates[index][1].code,
                    "capability": "etf_snapshot",
                    "result": "deadline_exceeded",
                }
            )
            for index in unfinished
        )
        return EtfCollectionBatch(
            observations=observations,
            trace=tuple(trace[:_TRACE_LIMIT]),
            as_of=anchor,
        )
