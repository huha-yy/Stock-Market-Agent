from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone

import pytest

from src.market_radar.candidates import EnrichmentCandidate
from src.market_radar.capabilities import (
    BoardBar,
    BoardBarSeries,
    BoardFlow,
    BoardFlowSeries,
    CapabilityResult,
    ConstituentMembership,
    ConstituentQuote,
    ConstituentQuoteBatch,
    MarketRadarEnrichmentConfig,
)
from src.market_radar.enrichment import (
    MarketRadarEnricher,
    RunScopedCapabilityCircuit,
)
from src.market_radar.models import SectorDefinition, SectorObservation
from src.market_radar.observation_builder import ObservationBuildResult


AS_OF = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _sector(index: int, *, benchmark_code: str | None = None) -> SectorDefinition:
    return SectorDefinition(
        sector_id=f"industry:sector-{index}",
        kind="industry",
        name=f"Sector {index}",
        benchmark_code=benchmark_code,
        effective_from=date(2026, 1, 1),
    )


def _base(sector: SectorDefinition, **metrics: object) -> SectorObservation:
    values = {field: None for field in SectorObservation.tracked_metric_fields}
    values.update(metrics)
    missing = tuple(field for field, value in values.items() if value is None)
    return SectorObservation(
        sector_id=sector.sector_id,
        market=sector.market,
        kind=sector.kind,
        name=sector.name,
        observed_at=AS_OF,
        source="phase1",
        freshness_seconds=0,
        quality="partial",
        missing_fields=missing,
        raw_reference={},
        **values,
    )


def _candidate(
    index: int,
    *,
    codes: tuple[str, ...] = ("000001",),
    observation: SectorObservation | None | object = ...,
    benchmark_code: str | None = None,
) -> EnrichmentCandidate:
    sector = _sector(index, benchmark_code=benchmark_code)
    base = _base(sector) if observation is ... else observation
    candidate = EnrichmentCandidate(
        sector=sector,
        observation=base,  # type: ignore[arg-type]
        reasons=("configured_seed",),
    )
    object.__setattr__(candidate, "_test_codes", codes)
    return candidate


def _result(
    capability: str,
    data: object | None,
    *,
    status: str = "ok",
    source: str = "fixture",
    data_date: date | None = date(2026, 7, 22),
    trace: tuple[dict[str, object], ...] = (),
    error: str | None = None,
    observed_at: datetime = AS_OF,
) -> CapabilityResult:
    return CapabilityResult(
        capability=capability,
        status=status,
        data=data,
        source=source,
        observed_at=observed_at,
        data_date=data_date,
        bar_status=None if data_date is None else "finalized",
        freshness_seconds=0,
        trace=trace,
        error=error,
    )


def _history(code: str) -> CapabilityResult:
    start = date(2026, 7, 2)
    return _result(
        "board_history",
        BoardBarSeries(
            code=code,
            bars=tuple(
                BoardBar(
                    data_date=start + timedelta(days=index),
                    close=100.0 + index,
                    traded_amount=1000.0 + index,
                )
                for index in range(21)
            ),
        ),
    )


def _flow(code: str) -> CapabilityResult:
    start = date(2026, 7, 3)
    return _result(
        "board_flow",
        BoardFlowSeries(
            code=code,
            flows=tuple(
                BoardFlow(
                    data_date=start + timedelta(days=index),
                    net_main_inflow=10.0,
                    traded_amount=1000.0,
                )
                for index in range(20)
            ),
        ),
    )


class _Provider:
    def __init__(self, codes_by_sector: dict[str, tuple[str, ...]]) -> None:
        self.codes_by_sector = codes_by_sector
        self.benchmark_calls: list[tuple[str, datetime]] = []
        self.quote_calls: list[tuple[str, ...]] = []
        self.history_failures: set[str] = set()

    def fetch_board_history(self, sector, as_of):
        if sector.sector_id in self.history_failures:
            raise RuntimeError("secret upstream board failure")
        return _history(sector.sector_id)

    def fetch_benchmark_history(self, code, as_of):
        self.benchmark_calls.append((code, as_of))
        result = _history(code)
        return result.model_copy(update={"capability": "benchmark_history"})

    def fetch_board_flow(self, sector, as_of):
        return _flow(sector.sector_id)

    def fetch_constituents(self, sector, as_of):
        codes = self.codes_by_sector[sector.sector_id]
        return _result(
            "constituents",
            ConstituentMembership(codes=codes, data_date=as_of.date()),
        )

    def fetch_constituent_quotes(self, codes, as_of):
        self.quote_calls.append(codes)
        return _result(
            "constituent_quotes",
            ConstituentQuoteBatch(
                quotes=tuple(
                    ConstituentQuote(
                        code=code,
                        current_price=11.0,
                        previous_close=10.0,
                        traded_amount=100.0,
                        quoted_at=as_of,
                    )
                    for code in codes
                )
            ),
            status="partial",
            source="authoritative-quotes",
            trace=({"provider": "quotes-a", "result": "ok"},),
        )


def _enricher(provider, **overrides) -> MarketRadarEnricher:
    config = MarketRadarEnrichmentConfig(
        candidate_limit=overrides.pop("candidate_limit", 60),
        total_budget_seconds=10,
        max_concurrency=overrides.pop("max_concurrency", 6),
        constituent_min_count=1,
        constituent_coverage_ratio=0.5,
    )
    return MarketRadarEnricher(provider=provider, config=config, **overrides)


def test_enricher_fetches_benchmark_once_and_deduplicates_quote_codes() -> None:
    candidates = (
        _candidate(1, codes=("600519", "000001")),
        _candidate(2, codes=("600000", "000001")),
    )
    provider = _Provider(
        {
            candidate.sector.sector_id: candidate._test_codes  # type: ignore[attr-defined]
            for candidate in candidates
        }
    )

    batch = _enricher(provider).enrich(candidates, AS_OF)

    assert provider.benchmark_calls == [("000985", AS_OF)]
    assert provider.quote_calls == [("000001", "600000", "600519")]
    assert tuple(item.sector_id for item in batch.observations) == tuple(
        item.sector.sector_id for item in candidates
    )
    assert len(batch.constituent_evidence) == 2
    for observation in batch.observations:
        quotes = observation.raw_reference["capabilities"]["quotes"]
        assert quotes["status"] == "partial"
        assert quotes["source"] == "authoritative-quotes"
        assert quotes["trace"] == ({"provider": "quotes-a", "result": "ok"},)


def test_enricher_advances_observation_anchor_to_quote_acquisition_time() -> None:
    candidate = _candidate(1)
    acquired_at = AS_OF + timedelta(seconds=60)
    quoted_at = AS_OF + timedelta(seconds=30)

    class AdvancingQuoteProvider(_Provider):
        def fetch_constituent_quotes(self, codes, as_of):
            return _result(
                "constituent_quotes",
                ConstituentQuoteBatch(
                    quotes=(
                        ConstituentQuote(
                            code=codes[0],
                            current_price=11.0,
                            previous_close=10.0,
                            traded_amount=100.0,
                            quoted_at=quoted_at,
                        ),
                    )
                ),
                observed_at=acquired_at,
            )

    batch = _enricher(
        AdvancingQuoteProvider({candidate.sector.sector_id: ("000001",)})
    ).enrich((candidate,), AS_OF)

    assert batch.as_of == acquired_at
    assert batch.observations[0].observed_at == acquired_at
    assert batch.observations[0].up_count == 1
    assert batch.observations[0].benchmark_return_20d_pct is not None
    assert batch.observations[0].capital_flow_5d is not None
    assert batch.observations[0].price_flow_divergence is False


def test_enricher_honors_limit_without_deduplicating_selected_candidates() -> None:
    repeated = _candidate(1)
    candidates = (repeated, repeated, _candidate(2))
    provider = _Provider({repeated.sector.sector_id: ("000001",)})

    batch = _enricher(provider, candidate_limit=2).enrich(candidates, AS_OF)

    assert len(batch.observations) == 2
    assert [item.sector_id for item in batch.observations] == [
        repeated.sector.sector_id,
        repeated.sector.sector_id,
    ]


def test_benchmark_overrides_and_sector_quote_slices_keep_exact_identity() -> None:
    candidates = (
        _candidate(1, codes=("600519", "000001"), benchmark_code="000300"),
        _candidate(2, codes=("600000", "000001"), benchmark_code="000905"),
    )
    provider = _Provider(
        {
            candidate.sector.sector_id: candidate._test_codes  # type: ignore[attr-defined]
            for candidate in candidates
        }
    )

    class CapturingBuilder:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def build(self, **kwargs):
            self.calls.append(kwargs)
            return ObservationBuildResult(
                observation=kwargs["base"], constituent_evidence=None
            )

    builder = CapturingBuilder()

    _enricher(provider, builder=builder).enrich(candidates, AS_OF)

    assert provider.benchmark_calls == [("000300", AS_OF), ("000905", AS_OF)]
    assert [call["benchmark_code"] for call in builder.calls] == [
        "000300",
        "000905",
    ]
    assert [
        tuple(quote.code for quote in call["quotes"].data.quotes)  # type: ignore[union-attr]
        for call in builder.calls
    ] == [("000001", "600519"), ("000001", "600000")]
    assert all(call["quotes"].status == "partial" for call in builder.calls)
    assert all(
        call["quotes"].source == "authoritative-quotes"
        for call in builder.calls
    )


def test_missing_candidate_observation_uses_new_all_missing_base() -> None:
    candidate = _candidate(1, observation=None)
    provider = _Provider({candidate.sector.sector_id: ("000001",)})
    provider.history_failures.add(candidate.sector.sector_id)

    observation = _enricher(provider).enrich((candidate,), AS_OF).observations[0]

    assert observation.observed_at == AS_OF
    assert observation.raw_reference["base"]["source"] == (
        "configured_seed_no_discovery"
    )
    assert observation.return_1d_pct is None


def test_one_capability_failure_is_bounded_and_other_inputs_still_rank() -> None:
    sector = _sector(1)
    candidate = EnrichmentCandidate(
        sector=sector,
        observation=_base(sector, return_1d_pct=5.0),
        reasons=("configured_seed",),
    )
    provider = _Provider({sector.sector_id: ("000001",)})
    provider.history_failures.add(sector.sector_id)

    batch = _enricher(provider).enrich((candidate,), AS_OF)

    assert batch.observations[0].return_1d_pct == 5.0
    history_trace = [
        item
        for item in batch.trace
        if item.get("capability") == "board_history"
    ]
    assert history_trace[0]["result"] == "unavailable"
    assert "secret" not in str(history_trace[0])


class _NeverCompletingExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.futures: list[Future] = []
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self.max_active = 0

    def submit(self, fn, *args):
        future = Future()
        self.futures.append(future)
        self.max_active = max(self.max_active, len(self.futures))
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdown_calls.append((wait, cancel_futures))
        if cancel_futures:
            for future in self.futures:
                future.cancel()


class _DeadlineClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 0.0 if self.calls <= 2 else 10.0


class _ImmediateExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        return None


class _QuoteDeadlineClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 0.0 if self.calls <= 10 else 10.0


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _ExpireOnSubmitExecutor(_ImmediateExecutor):
    def __init__(self, max_workers: int, clock: _ManualClock) -> None:
        super().__init__(max_workers)
        self.clock = clock

    def submit(self, fn, *args):
        self.clock.now = 10.0
        return super().submit(fn, *args)


def test_deadline_stops_submission_and_marks_unfinished_capabilities_unavailable() -> None:
    candidates = tuple(_candidate(index) for index in range(12))
    provider = _Provider(
        {candidate.sector.sector_id: ("000001",) for candidate in candidates}
    )
    executors: list[_NeverCompletingExecutor] = []

    def executor_factory(max_workers: int):
        executor = _NeverCompletingExecutor(max_workers)
        executors.append(executor)
        return executor

    batch = _enricher(
        provider,
        max_concurrency=6,
        monotonic=_DeadlineClock(),
        executor_factory=executor_factory,
    ).enrich(candidates, AS_OF)

    assert executors[0].max_active <= 6
    assert len(executors[0].futures) < len(candidates) + 1
    assert executors[0].shutdown_calls == [(False, True)]
    assert len(batch.observations) == len(candidates)
    deadline_trace = [
        item for item in batch.trace if item.get("result") == "deadline_exceeded"
    ]
    assert len(deadline_trace) >= len(candidates)
    assert provider.quote_calls == []


def test_naive_as_of_is_rejected_before_provider_calls() -> None:
    candidate = _candidate(1)
    provider = _Provider({candidate.sector.sector_id: ("000001",)})

    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        _enricher(provider).enrich((candidate,), AS_OF.replace(tzinfo=None))

    assert provider.benchmark_calls == []


def test_deadline_before_quote_batch_emits_explicit_unavailable_quotes() -> None:
    candidate = _candidate(1)
    provider = _Provider({candidate.sector.sector_id: ("000001",)})

    batch = _enricher(
        provider,
        monotonic=_QuoteDeadlineClock(),
        executor_factory=_ImmediateExecutor,
    ).enrich((candidate,), AS_OF)

    quotes = batch.observations[0].raw_reference["capabilities"]["quotes"]
    assert provider.quote_calls == []
    assert quotes["status"] == "unavailable"
    assert quotes["error"] == "deadline_exceeded"


def test_candidate_does_not_start_later_capabilities_after_deadline() -> None:
    candidate = _candidate(1)
    clock = _ManualClock()

    class ExpiringProvider(_Provider):
        def __init__(self) -> None:
            super().__init__({candidate.sector.sector_id: ("000001",)})
            self.flow_calls = 0
            self.membership_calls = 0

        def fetch_board_history(self, sector, as_of):
            clock.now = 10.0
            return _history(sector.sector_id)

        def fetch_board_flow(self, sector, as_of):
            self.flow_calls += 1
            return super().fetch_board_flow(sector, as_of)

        def fetch_constituents(self, sector, as_of):
            self.membership_calls += 1
            return super().fetch_constituents(sector, as_of)

    provider = ExpiringProvider()

    batch = _enricher(
        provider,
        monotonic=clock,
        executor_factory=_ImmediateExecutor,
    ).enrich((candidate,), AS_OF)

    assert provider.flow_calls == 0
    assert provider.membership_calls == 0
    assert any(
        item.get("result") == "deadline_exceeded" for item in batch.trace
    )


def test_benchmark_worker_rechecks_deadline_when_execution_starts() -> None:
    candidate = _candidate(1)
    provider = _Provider({candidate.sector.sector_id: ("000001",)})
    clock = _ManualClock()

    batch = _enricher(
        provider,
        monotonic=clock,
        executor_factory=lambda workers: _ExpireOnSubmitExecutor(workers, clock),
    ).enrich((candidate,), AS_OF)

    assert provider.benchmark_calls == []
    benchmark = batch.observations[0].raw_reference["capabilities"][
        "benchmark_history"
    ]
    assert benchmark["status"] == "unavailable"
    assert benchmark["error"] == "deadline_exceeded"


def test_quote_worker_rechecks_deadline_when_execution_starts() -> None:
    provider = _Provider({})
    clock = _ManualClock()
    enricher = _enricher(
        provider,
        monotonic=clock,
        executor_factory=lambda workers: _ExpireOnSubmitExecutor(workers, clock),
    )

    result = enricher._fetch_quote_union(
        ("000001",),
        AS_OF,
        10.0,
        RunScopedCapabilityCircuit(),
    )

    assert provider.quote_calls == []
    assert result.status == "unavailable"
    assert result.error == "deadline_exceeded"


def test_run_circuit_is_capability_source_local_and_success_resets_count() -> None:
    circuit = RunScopedCapabilityCircuit(failure_threshold=3)
    for _ in range(2):
        circuit.record_attempt("history", "A", "failed")
    circuit.record_attempt("history", "A", "ok")
    for _ in range(2):
        circuit.record_attempt("history", "A", "invalid")

    assert circuit.should_attempt("history", "A") is True
    circuit.record_attempt("history", "A", "empty")
    assert circuit.should_attempt("history", "A") is False
    assert circuit.should_attempt("flow", "A") is True
    assert circuit.should_attempt("history", "B") is True


def test_builder_programming_error_aborts_run() -> None:
    candidate = _candidate(1)
    provider = _Provider({candidate.sector.sector_id: ("000001",)})

    class ExplodingBuilder:
        def build(self, **kwargs):
            raise TypeError("builder contract broken")

    with pytest.raises(TypeError, match="builder contract broken"):
        _enricher(provider, builder=ExplodingBuilder()).enrich((candidate,), AS_OF)
