from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from threading import Lock
import time
from types import MappingProxyType
from typing import Any, TypeVar

from data_provider.base import normalize_stock_code, sanitize_persisted_text
from src.market_radar.candidates import EnrichmentCandidate
from src.market_radar.capabilities import (
    BoardBarSeries,
    BoardFlowSeries,
    CapabilityResult,
    ConstituentMembership,
    ConstituentQuoteBatch,
    MarketRadarEnrichmentConfig,
)
from src.market_radar.models import SectorObservation
from src.market_radar.observation_builder import (
    CnSectorObservationBuilder,
    ConstituentEvidence,
)


_FAILED_RESULTS = frozenset({"failed", "empty", "invalid"})
_RESET_RESULTS = frozenset({"ok", "partial", "stale"})
_TRACE_LIMIT = 1200
_TRACE_TEXT_LIMIT = 128
_ERROR_LIMIT = 256


@dataclass(frozen=True)
class EnrichmentBatch:
    observations: tuple[SectorObservation, ...]
    constituent_evidence: tuple[ConstituentEvidence, ...]
    trace: tuple[Mapping[str, Any], ...]
    as_of: datetime | None = None


@dataclass(frozen=True)
class _CandidateCapabilities:
    board_history: CapabilityResult[BoardBarSeries]
    board_flow: CapabilityResult[BoardFlowSeries]
    membership: CapabilityResult[ConstituentMembership]


class RunScopedCapabilityCircuit:
    """Thread-safe consecutive-failure circuit scoped to one enrichment run."""

    def __init__(self, failure_threshold: int = 3) -> None:
        if isinstance(failure_threshold, bool) or failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.failure_threshold = failure_threshold
        self._failures: dict[tuple[str, str], int] = {}
        self._open: set[tuple[str, str]] = set()
        self._lock = Lock()

    def should_attempt(self, capability: str, source: str) -> bool:
        key = (str(capability), str(source))
        with self._lock:
            return key not in self._open

    def record_attempt(self, capability: str, source: str, result: str) -> None:
        key = (str(capability), str(source))
        with self._lock:
            if result in _RESET_RESULTS:
                self._failures.pop(key, None)
                self._open.discard(key)
                return
            if result not in _FAILED_RESULTS:
                return
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.failure_threshold:
                self._open.add(key)


T = TypeVar("T")


class _BoundedScheduler:
    def __init__(
        self,
        *,
        executor_factory: Callable[[int], Any],
        max_active: int,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.executor_factory = executor_factory
        self.max_active = max_active
        self.deadline = deadline
        self.monotonic = monotonic

    def run(
        self,
        items: Sequence[T],
        operation: Callable[[T], Any],
    ) -> tuple[dict[int, Any], tuple[int, ...]]:
        if not items:
            return {}, ()
        executor = self.executor_factory(self.max_active)
        completed: dict[int, Any] = {}
        pending: dict[Future, int] = {}
        next_index = 0
        try:
            while next_index < len(items) or pending:
                while (
                    next_index < len(items)
                    and len(pending) < self.max_active
                    and self.monotonic() < self.deadline
                ):
                    future = executor.submit(operation, items[next_index])
                    pending[future] = next_index
                    next_index += 1

                if not pending:
                    break
                remaining = self.deadline - self.monotonic()
                if remaining <= 0:
                    break
                done, _ = wait(
                    tuple(pending),
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    break
                for future in done:
                    index = pending.pop(future)
                    completed[index] = future.result()
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        unfinished = tuple(
            index for index in range(len(items)) if index not in completed
        )
        return completed, unfinished


class MarketRadarEnricher:
    def __init__(
        self,
        *,
        provider: Any,
        config: MarketRadarEnrichmentConfig | None = None,
        builder: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        executor_factory: Callable[[int], Any] = ThreadPoolExecutor,
    ) -> None:
        self.provider = provider
        self.config = config or MarketRadarEnrichmentConfig()
        self.builder = builder or CnSectorObservationBuilder(self.config)
        self.monotonic = monotonic
        self.executor_factory = executor_factory

    def enrich(
        self,
        candidates: Sequence[EnrichmentCandidate],
        as_of: datetime,
    ) -> EnrichmentBatch:
        self._require_aware(as_of)
        selected = tuple(candidates[: self.config.candidate_limit])
        if not selected:
            return EnrichmentBatch((), (), (), as_of)

        deadline = self.monotonic() + self.config.total_budget_seconds
        circuit = RunScopedCapabilityCircuit()
        scheduler = self._scheduler(deadline)

        benchmark_codes = tuple(
            dict.fromkeys(
                candidate.sector.benchmark_code
                or self.config.default_benchmark_code
                for candidate in selected
            )
        )
        benchmark_values, benchmark_unfinished = scheduler.run(
            benchmark_codes,
            lambda code: self._deadline_provider_call(
                "benchmark_history",
                as_of,
                deadline,
                self.provider.fetch_benchmark_history,
                code,
                as_of,
                attempt_policy=circuit,
            ),
        )
        benchmarks = {
            code: benchmark_values.get(
                index,
                self._unavailable(
                    "benchmark_history", as_of, "deadline_exceeded"
                ),
            )
            for index, code in enumerate(benchmark_codes)
        }

        candidate_values: dict[int, _CandidateCapabilities] = {}
        candidate_unfinished = tuple(range(len(selected)))
        if self.monotonic() < deadline:
            candidate_values, candidate_unfinished = self._scheduler(deadline).run(
                selected,
                lambda candidate: self._fetch_candidate(
                    candidate, as_of, deadline, circuit
                ),
            )

        candidate_capabilities = {
            index: candidate_values.get(
                index, self._deadline_candidate_capabilities(as_of)
            )
            for index in range(len(selected))
        }
        union_codes = self._membership_union(candidate_capabilities.values())
        quote_result = self._fetch_quote_union(
            union_codes, as_of, deadline, circuit
        )
        observation_anchor = max(
            (
                as_of,
                quote_result.observed_at,
                *(result.observed_at for result in benchmarks.values()),
                *(
                    result.observed_at
                    for capabilities in candidate_capabilities.values()
                    for result in (
                        capabilities.board_history,
                        capabilities.board_flow,
                        capabilities.membership,
                    )
                ),
            ),
            key=lambda value: value.astimezone(timezone.utc),
        ).astimezone(timezone.utc)

        observations: list[SectorObservation] = []
        evidence: list[ConstituentEvidence] = []
        trace: list[Mapping[str, Any]] = []
        for index in candidate_unfinished:
            trace.append(
                self._trace_entry(
                    sector_id=selected[index].sector.sector_id,
                    capability="candidate_enrichment",
                    result="deadline_exceeded",
                )
            )
        for index in benchmark_unfinished:
            trace.append(
                self._trace_entry(
                    capability="benchmark_history",
                    code=benchmark_codes[index],
                    result="deadline_exceeded",
                )
            )

        for index, candidate in enumerate(selected):
            capabilities = candidate_capabilities[index]
            benchmark_code = (
                candidate.sector.benchmark_code
                or self.config.default_benchmark_code
            )
            sector_quotes = self._slice_quotes(
                capabilities.membership, quote_result, observation_anchor
            )
            result = self.builder.build(
                base=candidate.observation
                or self._missing_base(candidate, observation_anchor),
                candidate_reasons=candidate.reasons,
                benchmark_code=benchmark_code,
                board_history=capabilities.board_history,
                benchmark_history=benchmarks[benchmark_code],
                board_flow=capabilities.board_flow,
                membership=capabilities.membership,
                quotes=sector_quotes,
                observed_at=observation_anchor,
            )
            observations.append(result.observation)
            if result.constituent_evidence is not None:
                evidence.append(result.constituent_evidence)
            trace.extend(
                self._capability_trace(candidate, capability)
                for capability in (
                    capabilities.board_history,
                    benchmarks[benchmark_code],
                    capabilities.board_flow,
                    capabilities.membership,
                    sector_quotes,
                )
            )

        return EnrichmentBatch(
            observations=tuple(observations),
            constituent_evidence=tuple(evidence),
            trace=tuple(trace[:_TRACE_LIMIT]),
            as_of=observation_anchor,
        )

    def _scheduler(self, deadline: float) -> _BoundedScheduler:
        return _BoundedScheduler(
            executor_factory=self.executor_factory,
            max_active=self.config.max_concurrency,
            deadline=deadline,
            monotonic=self.monotonic,
        )

    def _fetch_candidate(
        self,
        candidate: EnrichmentCandidate,
        as_of: datetime,
        deadline: float,
        circuit: RunScopedCapabilityCircuit,
    ) -> _CandidateCapabilities:
        return _CandidateCapabilities(
            board_history=self._deadline_provider_call(
                "board_history",
                as_of,
                deadline,
                self.provider.fetch_board_history,
                candidate.sector,
                as_of,
                attempt_policy=circuit,
            ),
            board_flow=self._deadline_provider_call(
                "board_flow",
                as_of,
                deadline,
                self.provider.fetch_board_flow,
                candidate.sector,
                as_of,
                attempt_policy=circuit,
            ),
            membership=self._deadline_provider_call(
                "constituents",
                as_of,
                deadline,
                self.provider.fetch_constituents,
                candidate.sector,
                as_of,
                attempt_policy=circuit,
            ),
        )

    def _deadline_provider_call(
        self,
        capability: str,
        as_of: datetime,
        deadline: float,
        method: Callable[..., Any],
        *args: Any,
        attempt_policy: RunScopedCapabilityCircuit,
    ) -> CapabilityResult:
        if self.monotonic() >= deadline:
            return self._unavailable(capability, as_of, "deadline_exceeded")
        return self._safe_provider_call(
            capability,
            as_of,
            method,
            *args,
            attempt_policy=attempt_policy,
            deadline_monotonic=deadline,
            monotonic=self.monotonic,
        )

    def _fetch_quote_union(
        self,
        codes: tuple[str, ...],
        as_of: datetime,
        deadline: float,
        circuit: RunScopedCapabilityCircuit,
    ) -> CapabilityResult[ConstituentQuoteBatch]:
        if not codes:
            return self._unavailable(
                "constituent_quotes", as_of, "no_usable_memberships"
            )
        if self.monotonic() >= deadline:
            return self._unavailable(
                "constituent_quotes", as_of, "deadline_exceeded"
            )
        completed, _ = self._scheduler(deadline).run(
            (codes,),
            lambda requested: self._deadline_provider_call(
                "constituent_quotes",
                as_of,
                deadline,
                self.provider.fetch_constituent_quotes,
                requested,
                as_of,
                attempt_policy=circuit,
            ),
        )
        return completed.get(
            0,
            self._unavailable(
                "constituent_quotes", as_of, "deadline_exceeded"
            ),
        )

    @staticmethod
    def _supported_provider_kwargs(
        method: Callable[..., Any], values: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return {}
        parameter_names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return {
            name: value
            for name, value in values.items()
            if accepts_kwargs or name in parameter_names
        }

    @classmethod
    def _safe_provider_call(
        cls,
        capability: str,
        as_of: datetime,
        method: Callable[..., Any],
        *args: Any,
        attempt_policy: RunScopedCapabilityCircuit,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult:
        kwargs = cls._supported_provider_kwargs(
            method,
            {
                "attempt_policy": attempt_policy,
                "deadline_monotonic": deadline_monotonic,
                "monotonic": monotonic,
            },
        )
        try:
            result = method(*args, **kwargs)
        except Exception as exc:
            return cls._unavailable(
                capability,
                as_of,
                f"provider_exception:{type(exc).__name__}",
            )
        if not isinstance(result, CapabilityResult):
            return cls._unavailable(
                capability, as_of, "provider_invalid_result_type"
            )
        return result

    @staticmethod
    def _unavailable(
        capability: str,
        as_of: datetime,
        error: str,
    ) -> CapabilityResult:
        return CapabilityResult(
            capability=capability,
            status="unavailable",
            data=None,
            source="market_radar_enrichment",
            observed_at=as_of,
            data_date=None,
            bar_status=None,
            freshness_seconds=0,
            trace=(),
            error=sanitize_persisted_text(error, _ERROR_LIMIT),
        )

    @classmethod
    def _deadline_candidate_capabilities(
        cls, as_of: datetime
    ) -> _CandidateCapabilities:
        return _CandidateCapabilities(
            board_history=cls._unavailable(
                "board_history", as_of, "deadline_exceeded"
            ),
            board_flow=cls._unavailable(
                "board_flow", as_of, "deadline_exceeded"
            ),
            membership=cls._unavailable(
                "constituents", as_of, "deadline_exceeded"
            ),
        )

    @staticmethod
    def _membership_union(
        values: Iterable[_CandidateCapabilities],
    ) -> tuple[str, ...]:
        codes: set[str] = set()
        for capabilities in values:
            result = capabilities.membership
            if result.status == "unavailable" or result.data is None:
                continue
            for raw_code in result.data.codes:
                code = normalize_stock_code(raw_code)
                if len(code) == 6 and code.isdigit():
                    codes.add(code)
        return tuple(sorted(codes))

    @classmethod
    def _slice_quotes(
        cls,
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        as_of: datetime,
    ) -> CapabilityResult[ConstituentQuoteBatch]:
        if membership.status == "unavailable" or membership.data is None:
            return cls._unavailable(
                "constituent_quotes", as_of, "membership_unavailable"
            )
        if quotes.status == "unavailable" or quotes.data is None:
            return quotes
        requested: set[str] = set()
        for raw_code in membership.data.codes:
            code = normalize_stock_code(raw_code)
            if len(code) == 6 and code.isdigit():
                requested.add(code)
        sliced = tuple(
            quote for quote in quotes.data.quotes if quote.code in requested
        )
        if not sliced:
            return cls._unavailable(
                "constituent_quotes", as_of, "no_quotes_for_membership"
            )
        return quotes.model_copy(
            update={"data": ConstituentQuoteBatch(quotes=sliced)}
        )

    @staticmethod
    def _missing_base(
        candidate: EnrichmentCandidate, as_of: datetime
    ) -> SectorObservation:
        return SectorObservation(
            sector_id=candidate.sector.sector_id,
            market=candidate.sector.market,
            kind=candidate.sector.kind,
            name=candidate.sector.name,
            observed_at=as_of,
            source="configured_seed_no_discovery",
            freshness_seconds=0,
            quality="unavailable",
            missing_fields=SectorObservation.tracked_metric_fields,
            raw_reference={},
        )

    @staticmethod
    def _require_aware(as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

    @staticmethod
    def _trace_entry(**values: Any) -> Mapping[str, Any]:
        allowed = {
            "sector_id",
            "capability",
            "provider",
            "result",
            "source",
            "code",
            "selected",
            "duration_ms",
            "error",
        }
        cleaned: dict[str, Any] = {}
        for key, value in values.items():
            if key not in allowed or value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                limit = _ERROR_LIMIT if key == "error" else _TRACE_TEXT_LIMIT
                cleaned[key] = (
                    sanitize_persisted_text(value, limit)
                    if isinstance(value, str)
                    else value
                )
        return MappingProxyType(cleaned)

    @classmethod
    def _capability_trace(
        cls,
        candidate: EnrichmentCandidate,
        result: CapabilityResult,
    ) -> Mapping[str, Any]:
        outcome = (
            "deadline_exceeded"
            if result.error == "deadline_exceeded"
            else result.status
        )
        return cls._trace_entry(
            sector_id=candidate.sector.sector_id,
            capability=result.capability,
            result=outcome,
            source=result.source,
            error=result.error,
        )
