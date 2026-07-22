from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from math import log
import re
from statistics import fmean, stdev
from typing import Any
from zoneinfo import ZoneInfo

from data_provider.base import normalize_stock_code, sanitize_persisted_text
from src.market_radar.capabilities import (
    BoardBar,
    BoardBarSeries,
    BoardFlowSeries,
    CapabilityResult,
    ConstituentMembership,
    ConstituentQuoteBatch,
    MarketRadarEnrichmentConfig,
)
from src.market_radar.models import SectorObservation


_SCHEMA = "market-radar-observation-v2a"
_SOURCE = "market_radar_enrichment_v2a"
_RETURN_FIELDS = (
    "return_1d_pct",
    "return_5d_pct",
    "return_20d_pct",
    "benchmark_return_20d_pct",
)
_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_CN_CODE_PATTERN = re.compile(r"^\d{6}$")
_TRACE_FIELDS = (
    "provider",
    "result",
    "duration_ms",
    "code",
    "selected",
    "error",
)
_TRACE_LIMIT = 20
_TEXT_LIMIT = 128
_ERROR_LIMIT = 256
_INVALID_CODE_LIMIT = 50
_CANDIDATE_REASON_LIMIT = 20


@dataclass(frozen=True)
class ConstituentEvidence:
    market: str
    sector_id: str
    source: str
    data_date: date
    observed_at: datetime
    codes: tuple[str, ...]
    set_key: str


@dataclass(frozen=True)
class ObservationBuildResult:
    observation: SectorObservation
    constituent_evidence: ConstituentEvidence | None


@dataclass(frozen=True)
class _Metrics:
    values: Mapping[str, Any]
    forced_missing: frozenset[str]
    field_sources: Mapping[str, str]
    field_capabilities: Mapping[str, tuple[CapabilityResult, ...]]
    coverage: Mapping[str, Any]
    membership_usable: bool
    membership_data_date: date | None
    membership_provenance: str | None
    constituent_codes: tuple[str, ...]
    terminal_date: date | None


def _canonical_cn_codes(codes: Sequence[str]) -> tuple[str, ...]:
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_code in codes:
        code = normalize_stock_code(raw_code)
        if _CN_CODE_PATTERN.fullmatch(code) is None:
            raise ValueError(f"constituent code is not a canonical CN code: {raw_code}")
        if code in seen:
            raise ValueError(f"duplicate canonical constituent code: {code}")
        seen.add(code)
        canonical.append(code)
    return tuple(sorted(canonical))


def canonical_constituent_set_key(
    market: str,
    sector_id: str,
    source: str,
    codes: Sequence[str],
) -> str:
    canonical_codes = _canonical_cn_codes(codes)
    payload = json.dumps(
        [market, sector_id, source, canonical_codes],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _valid_history(
    result: CapabilityResult[BoardBarSeries], expected_code: str
) -> BoardBarSeries | None:
    if result.status == "unavailable" or result.data is None or result.data_date is None:
        return None
    if result.data.code != expected_code:
        return None
    if not result.data.bars or result.data.bars[-1].data_date != result.data_date:
        return None
    if result.data.bars[-1].close <= 0:
        return None
    return result.data


def _return_for_window(bars: Sequence[BoardBar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    current = bars[-1].close
    prior = bars[-window - 1].close
    if current <= 0 or prior <= 0:
        return None
    return (current / prior - 1) * 100


def _flow_for_window(series: BoardFlowSeries, window: int) -> float | None:
    if len(series.flows) < window:
        return None
    rows = series.flows[-window:]
    denominator = sum(row.traded_amount for row in rows)
    if denominator <= 0:
        return None
    return sum(row.net_main_inflow for row in rows) / denominator * 100


def _liquidity_ratio(bars: Sequence[BoardBar]) -> float | None:
    if len(bars) < 21:
        return None
    prior_amounts = [row.traded_amount for row in bars[-21:-1]]
    denominator = fmean(prior_amounts)
    if denominator <= 0:
        return None
    return bars[-1].traded_amount / denominator


def _ma20_distance(
    bars: Sequence[BoardBar], bar_status: str | None
) -> float | None:
    required = 21 if bar_status == "provisional" else 20
    if len(bars) < required:
        return None
    closes = (
        [row.close for row in bars[-21:-1]]
        if bar_status == "provisional"
        else [row.close for row in bars[-20:]]
    )
    if len(closes) != 20 or any(value <= 0 for value in closes):
        return None
    average = fmean(closes)
    return (bars[-1].close / average - 1) * 100


def _aligned_terminal_bars(
    board: BoardBarSeries,
    benchmark: BoardBarSeries,
) -> tuple[tuple[BoardBar, ...], tuple[BoardBar, ...]] | None:
    if len(board.bars) < 21:
        return None
    board_rows = tuple(board.bars[-21:])
    benchmark_by_date = {row.data_date: row for row in benchmark.bars}
    if any(row.data_date not in benchmark_by_date for row in board_rows):
        return None
    benchmark_rows = tuple(benchmark_by_date[row.data_date] for row in board_rows)
    if tuple(row.data_date for row in board_rows) != tuple(
        row.data_date for row in benchmark_rows
    ):
        return None
    return board_rows, benchmark_rows


def _volatility_ratio(
    board_rows: Sequence[BoardBar], benchmark_rows: Sequence[BoardBar]
) -> float | None:
    board_closes = [row.close for row in board_rows]
    benchmark_closes = [row.close for row in benchmark_rows]
    if (
        len(board_closes) != 21
        or len(benchmark_closes) != 21
        or any(value <= 0 for value in board_closes + benchmark_closes)
    ):
        return None
    board_returns = [
        log(current / previous)
        for previous, current in zip(board_closes, board_closes[1:])
    ]
    benchmark_returns = [
        log(current / previous)
        for previous, current in zip(benchmark_closes, benchmark_closes[1:])
    ]
    benchmark_deviation = stdev(benchmark_returns)
    if benchmark_deviation <= 0:
        return None
    return stdev(board_returns) / benchmark_deviation


def _bounded_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = str(value or "")
    lowered = text.casefold()
    if "traceback" in lowered or "most recent call last" in lowered:
        return "redacted_exception"
    return sanitize_persisted_text(text, limit=limit)


def _trace_summary(trace: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    summaries: list[dict[str, Any]] = []
    for entry in trace[:_TRACE_LIMIT]:
        if not isinstance(entry, Mapping):
            continue
        summary: dict[str, Any] = {}
        for key in _TRACE_FIELDS:
            if key not in entry:
                continue
            value = entry[key]
            if key == "selected" and isinstance(value, bool):
                summary[key] = value
            elif key == "duration_ms" and isinstance(value, (int, float)):
                summary[key] = value
            elif isinstance(value, (str, int, float, bool)):
                summary[key] = _bounded_text(
                    value,
                    _ERROR_LIMIT if key == "error" else _TEXT_LIMIT,
                )
        summaries.append(summary)
    return tuple(summaries)


def _capability_summary(result: CapabilityResult) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": result.status,
        "source": _bounded_text(result.source),
        "observed_at": result.observed_at,
        "data_date": result.data_date,
        "bar_status": result.bar_status,
        "freshness_seconds": result.freshness_seconds,
        "trace": _trace_summary(result.trace),
    }
    if result.error:
        summary["error"] = _bounded_text(result.error, _ERROR_LIMIT)
    return summary


def _source_label(source: str) -> str:
    return _bounded_text(source)


def _combined_source(*sources: str) -> str:
    return _bounded_text("+".join(_source_label(source) for source in sources))


def _base_source(source: str) -> str:
    return _bounded_text(f"base:{_source_label(source)}")


class CnSectorObservationBuilder:
    def __init__(self, config: MarketRadarEnrichmentConfig | None = None) -> None:
        self.config = config or MarketRadarEnrichmentConfig()

    def build(
        self,
        *,
        base: SectorObservation,
        candidate_reasons: tuple[str, ...],
        benchmark_code: str,
        board_history: CapabilityResult[BoardBarSeries],
        benchmark_history: CapabilityResult[BoardBarSeries],
        board_flow: CapabilityResult[BoardFlowSeries],
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        observed_at: datetime,
    ) -> ObservationBuildResult:
        metrics = self._compute_metrics(
            base=base,
            benchmark_code=benchmark_code,
            board_history=board_history,
            benchmark_history=benchmark_history,
            board_flow=board_flow,
            membership=membership,
            quotes=quotes,
            observed_at=observed_at,
        )
        evidence = self._constituent_evidence(
            base=base,
            membership=membership,
            metrics=metrics,
        )
        observation = self._assemble_observation(
            base=base,
            metrics=metrics,
            candidate_reasons=candidate_reasons,
            benchmark_code=benchmark_code,
            board_history=board_history,
            benchmark_history=benchmark_history,
            board_flow=board_flow,
            membership=membership,
            quotes=quotes,
            observed_at=observed_at,
            evidence=evidence,
        )
        return ObservationBuildResult(
            observation=observation,
            constituent_evidence=evidence,
        )

    def _compute_metrics(
        self,
        *,
        base: SectorObservation,
        benchmark_code: str,
        board_history: CapabilityResult[BoardBarSeries],
        benchmark_history: CapabilityResult[BoardBarSeries],
        board_flow: CapabilityResult[BoardFlowSeries],
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        observed_at: datetime,
    ) -> _Metrics:
        values: dict[str, Any] = {}
        forced_missing: set[str] = set()
        sources: dict[str, str] = {}
        dependencies: dict[str, tuple[CapabilityResult, ...]] = {}
        board = _valid_history(board_history, base.sector_id)
        terminal_date = board_history.data_date if board is not None else None

        if board is not None:
            for window in (1, 5, 20):
                field = f"return_{window}d_pct"
                value = _return_for_window(board.bars, window)
                self._publish(
                    values,
                    sources,
                    dependencies,
                    field,
                    value,
                    _source_label(board_history.source),
                    (board_history,),
                )
            self._publish(
                values,
                sources,
                dependencies,
                "turnover_ratio_20d",
                _liquidity_ratio(board.bars),
                _source_label(board_history.source),
                (board_history,),
            )
            self._publish(
                values,
                sources,
                dependencies,
                "distance_ma20_pct",
                _ma20_distance(board.bars, board_history.bar_status),
                _source_label(board_history.source),
                (board_history,),
            )

        benchmark = _valid_history(benchmark_history, benchmark_code)
        aligned = None
        if (
            board is not None
            and benchmark is not None
            and terminal_date == benchmark_history.data_date
            and board_history.observed_at == observed_at
            and benchmark_history.observed_at == observed_at
            and "return_20d_pct" in values
        ):
            aligned = _aligned_terminal_bars(board, benchmark)
        if aligned is not None:
            board_rows, benchmark_rows = aligned
            benchmark_return = _return_for_window(benchmark_rows, 20)
            self._publish(
                values,
                sources,
                dependencies,
                "benchmark_return_20d_pct",
                benchmark_return,
                _combined_source(board_history.source, benchmark_history.source),
                (board_history, benchmark_history),
            )
            self._publish(
                values,
                sources,
                dependencies,
                "volatility_ratio_20d",
                _volatility_ratio(board_rows, benchmark_rows),
                _combined_source(board_history.source, benchmark_history.source),
                (board_history, benchmark_history),
            )
        if (
            "return_20d_pct" in values
            and "benchmark_return_20d_pct" not in values
        ):
            forced_missing.add("benchmark_return_20d_pct")

        flow = board_flow.data
        flow_usable = (
            terminal_date is not None
            and board_flow.status != "unavailable"
            and flow is not None
            and board_flow.data_date == terminal_date
            and flow.flows[-1].data_date == terminal_date
            and flow.code == base.sector_id
        )
        if flow_usable:
            for window in (1, 5, 20):
                field = f"capital_flow_{window}d"
                self._publish(
                    values,
                    sources,
                    dependencies,
                    field,
                    _flow_for_window(flow, window),
                    _source_label(board_flow.source),
                    (board_flow,),
                )

        coverage, membership_usable, membership_date, provenance, constituent_codes = (
            self._constituent_metrics(
                terminal_date=terminal_date,
                membership=membership,
                quotes=quotes,
                observed_at=observed_at,
                values=values,
                sources=sources,
                dependencies=dependencies,
            )
        )

        compatible_divergence = (
            "return_5d_pct" in values
            and "capital_flow_5d" in values
            and board_history.observed_at == observed_at
            and board_flow.observed_at == observed_at
            and board_history.data_date == terminal_date
            and board_flow.data_date == terminal_date
        )
        if compatible_divergence:
            final_return_5d = values["return_5d_pct"]
            final_flow_5d = values["capital_flow_5d"]
            divergent = (
                final_return_5d * final_flow_5d < 0
                and abs(final_return_5d)
                >= self.config.price_divergence_threshold_pct
                and abs(final_flow_5d) >= self.config.flow_divergence_threshold_pct
            )
            values["price_flow_divergence"] = divergent
            sources["price_flow_divergence"] = _combined_source(
                sources["return_5d_pct"], sources["capital_flow_5d"]
            )
            combined_dependencies = (
                dependencies.get("return_5d_pct", ())
                + dependencies.get("capital_flow_5d", ())
            )
            dependencies["price_flow_divergence"] = tuple(
                capability
                for index, capability in enumerate(combined_dependencies)
                if all(
                    capability is not previous
                    for previous in combined_dependencies[:index]
                )
            )
        elif "return_5d_pct" in values or "capital_flow_5d" in values:
            forced_missing.add("price_flow_divergence")

        return _Metrics(
            values=values,
            forced_missing=frozenset(forced_missing),
            field_sources=sources,
            field_capabilities=dependencies,
            coverage=coverage,
            membership_usable=membership_usable,
            membership_data_date=membership_date,
            membership_provenance=provenance,
            constituent_codes=constituent_codes,
            terminal_date=terminal_date,
        )

    @staticmethod
    def _publish(
        values: dict[str, Any],
        sources: dict[str, str],
        dependencies: dict[str, tuple[CapabilityResult, ...]],
        field: str,
        value: Any,
        source: str,
        capabilities: tuple[CapabilityResult, ...],
    ) -> None:
        if value is None:
            return
        values[field] = value
        sources[field] = source
        dependencies[field] = capabilities

    def _constituent_metrics(
        self,
        *,
        terminal_date: date | None,
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        observed_at: datetime,
        values: dict[str, Any],
        sources: dict[str, str],
        dependencies: dict[str, tuple[CapabilityResult, ...]],
    ) -> tuple[
        dict[str, Any],
        bool,
        date | None,
        str | None,
        tuple[str, ...],
    ]:
        membership_data = membership.data
        quote_data = quotes.data
        try:
            membership_codes = (
                _canonical_cn_codes(membership_data.codes)
                if membership_data is not None
                else ()
            )
        except ValueError:
            membership_codes = ()
            membership_data = None
        coverage = self._coverage(membership_codes, ())
        if (
            terminal_date is None
            or membership.status == "unavailable"
            or membership_data is None
        ):
            return coverage, False, None, None, membership_codes

        membership_date = membership_data.data_date
        provenance = None
        if membership_date is None:
            if (
                membership.status != "partial"
                or membership.observed_at != observed_at
                or quotes.status == "unavailable"
                or quote_data is None
                or quotes.observed_at != observed_at
                or quotes.data_date != terminal_date
            ):
                return coverage, False, None, None, membership_codes
            evidence_date = quotes.data_date
            provenance = "partial_unversioned_current_membership"
        elif membership_date == terminal_date and membership.data_date == terminal_date:
            evidence_date = membership_date
        else:
            return coverage, False, None, None, membership_codes

        if (
            quotes.status == "unavailable"
            or quote_data is None
            or quotes.data_date != terminal_date
        ):
            return coverage, True, evidence_date, provenance, membership_codes

        membership_set = set(membership_codes)
        valid_by_code: dict[str, Any] = {}
        duplicate_codes: set[str] = set()
        for quote in quote_data.quotes:
            try:
                code = _canonical_cn_codes((quote.code,))[0]
            except ValueError:
                continue
            if code not in membership_set:
                continue
            if (
                quote.quoted_at > observed_at
                or quote.quoted_at.astimezone(_CN_TIMEZONE).date() != terminal_date
            ):
                continue
            if code in valid_by_code:
                duplicate_codes.add(code)
            else:
                valid_by_code[code] = quote
        valid_quotes = [
            quote
            for code, quote in valid_by_code.items()
            if code not in duplicate_codes
        ]
        valid_codes = tuple(
            sorted(code for code in valid_by_code if code not in duplicate_codes)
        )
        valid = len(valid_quotes)
        total = len(membership_codes)
        ratio = valid / total if total else 0.0
        coverage = self._coverage(membership_codes, valid_codes)
        gate = (
            total >= self.config.constituent_min_count
            and valid >= self.config.constituent_min_count
            and ratio >= self.config.constituent_coverage_ratio
        )
        if gate:
            up = sum(quote.current_price > quote.previous_close for quote in valid_quotes)
            down = sum(quote.current_price < quote.previous_close for quote in valid_quotes)
            flat = valid - up - down
            for field, value in (
                ("up_count", up),
                ("down_count", down),
                ("flat_count", flat),
            ):
                self._publish(
                    values,
                    sources,
                    dependencies,
                    field,
                    value,
                    _combined_source(membership.source, quotes.source),
                    (membership, quotes),
                )
            total_amount = sum(quote.traded_amount for quote in valid_quotes)
            concentration = (
                sum(
                    sorted(
                        (quote.traded_amount for quote in valid_quotes), reverse=True
                    )[:5]
                )
                / total_amount
                if total_amount > 0
                else None
            )
            self._publish(
                values,
                sources,
                dependencies,
                "concentration_ratio",
                concentration,
                _combined_source(membership.source, quotes.source),
                (membership, quotes),
            )
        return coverage, True, evidence_date, provenance, membership_codes

    @staticmethod
    def _coverage(
        membership_codes: Sequence[str], valid_codes: Sequence[str]
    ) -> dict[str, Any]:
        valid_set = set(valid_codes)
        total = len(membership_codes)
        valid = len(valid_set)
        return {
            "total": total,
            "valid": valid,
            "ratio": round(valid / total, 4) if total else 0.0,
            "invalid_codes": [
                code
                for code in membership_codes
                if code not in valid_set
            ][:_INVALID_CODE_LIMIT],
        }

    @staticmethod
    def _constituent_evidence(
        *,
        base: SectorObservation,
        membership: CapabilityResult[ConstituentMembership],
        metrics: _Metrics,
    ) -> ConstituentEvidence | None:
        if (
            metrics.terminal_date is None
            or not metrics.membership_usable
            or metrics.membership_data_date is None
            or membership.data is None
        ):
            return None
        codes = metrics.constituent_codes
        source = _source_label(membership.source)
        return ConstituentEvidence(
            market=base.market,
            sector_id=base.sector_id,
            source=source,
            data_date=metrics.membership_data_date,
            observed_at=membership.observed_at,
            codes=codes,
            set_key=canonical_constituent_set_key(
                base.market,
                base.sector_id,
                source,
                codes,
            ),
        )

    def _assemble_observation(
        self,
        *,
        base: SectorObservation,
        metrics: _Metrics,
        candidate_reasons: tuple[str, ...],
        benchmark_code: str,
        board_history: CapabilityResult[BoardBarSeries],
        benchmark_history: CapabilityResult[BoardBarSeries],
        board_flow: CapabilityResult[BoardFlowSeries],
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        observed_at: datetime,
        evidence: ConstituentEvidence | None,
    ) -> SectorObservation:
        payload = base.model_dump()
        for field in metrics.forced_missing:
            payload[field] = None
        payload.update(metrics.values)
        payload.update(
            observed_at=observed_at,
            source=_SOURCE,
            quality=self._quality(board_history, base.sector_id),
            freshness_seconds=self._freshness(base, payload, metrics),
        )
        payload["missing_fields"] = tuple(
            field
            for field in SectorObservation.tracked_metric_fields
            if payload.get(field) is None
        )
        membership_summary = _capability_summary(membership)
        membership_summary["membership_data_date"] = (
            membership.data.data_date if membership.data is not None else None
        )
        if metrics.membership_provenance is not None:
            membership_summary["provenance"] = metrics.membership_provenance
        field_sources = {
            field: metrics.field_sources.get(field, _base_source(base.source))
            for field in SectorObservation.tracked_metric_fields
            if payload.get(field) is not None
        }
        payload["raw_reference"] = {
            "schema": _SCHEMA,
            "candidate_reasons": tuple(
                _bounded_text(reason)
                for reason in candidate_reasons[:_CANDIDATE_REASON_LIMIT]
            ),
            "benchmark_code": _bounded_text(benchmark_code),
            "data_date": metrics.terminal_date,
            "bar_status": self._bar_status(metrics),
            "field_sources": field_sources,
            "base": {
                "source": _bounded_text(base.source),
                "observed_at": base.observed_at,
                "freshness_seconds": base.freshness_seconds,
            },
            "capabilities": {
                "board_history": _capability_summary(board_history),
                "benchmark_history": _capability_summary(benchmark_history),
                "board_flow": _capability_summary(board_flow),
                "membership": membership_summary,
                "quotes": _capability_summary(quotes),
            },
            "constituent_set_key": evidence.set_key if evidence is not None else None,
            "constituent_coverage": dict(metrics.coverage),
        }
        return SectorObservation(**payload)

    @staticmethod
    def _quality(
        board_history: CapabilityResult[BoardBarSeries], sector_id: str
    ) -> str:
        if _valid_history(board_history, sector_id) is None:
            return "unavailable"
        if board_history.status == "stale":
            return "stale"
        return "partial"

    @staticmethod
    def _freshness(
        base: SectorObservation,
        payload: Mapping[str, Any],
        metrics: _Metrics,
    ) -> int:
        ages: list[int] = []
        for field in _RETURN_FIELDS:
            if payload.get(field) is None:
                continue
            capabilities = metrics.field_capabilities.get(field)
            if capabilities:
                ages.extend(item.freshness_seconds for item in capabilities)
            else:
                ages.append(base.freshness_seconds)
        return max(ages, default=0)

    @staticmethod
    def _bar_status(metrics: _Metrics) -> str | None:
        statuses = {
            capability.bar_status
            for dependencies in metrics.field_capabilities.values()
            for capability in dependencies
            if capability.bar_status is not None
        }
        if "provisional" in statuses:
            return "provisional"
        if "finalized" in statuses:
            return "finalized"
        return None
