from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone

import pytest

from src.market_radar.capabilities import (
    CapabilityResult,
    EtfBar,
    EtfCapabilityData,
)
from src.market_radar.etf_collection import (
    CnEtfObservationBuilder,
    EtfCollectionConfig,
    MarketRadarEtfCollector,
    _select_effective_candidates,
)
from src.market_radar.models import (
    EtfDefinition,
    FactorBreakdown,
    SectorDefinition,
    SectorScore,
)


AS_OF = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _etf(code: str, sector_id: str, *, start: date = date(2026, 1, 1)):
    return EtfDefinition(
        code=code,
        name=f"ETF {code}",
        sector_id=sector_id,
        benchmark_code="000300",
        effective_from=start,
    )


def _sector(index: int, codes: tuple[str, ...]) -> SectorDefinition:
    sector_id = f"industry:sector-{index}"
    return SectorDefinition(
        sector_id=sector_id,
        kind="industry",
        name=f"Sector {index}",
        etfs=tuple(_etf(code, sector_id) for code in codes),
        effective_from=date(2026, 1, 1),
    )


def _score(sector: SectorDefinition, score: float) -> SectorScore:
    return SectorScore(
        sector_id=sector.sector_id,
        name=sector.name,
        kind=sector.kind,
        scoring_version="cn-v1",
        gross_score=score,
        risk_deduction=0,
        score=score,
        confidence=0.75,
        state="leading",
        factors=FactorBreakdown(
            trend_momentum=10,
            relative_strength=10,
            capital_flow=10,
            breadth=10,
            liquidity_expansion=5,
            catalyst=5,
        ),
        risk_reasons=(),
        missing_fields=(),
        source="fixture-sector",
        observed_at=AS_OF,
        quality="partial",
        observation={},
    )


def _capability(
    etf: EtfDefinition,
    *,
    observed_at: datetime = AS_OF,
    status: str = "ok",
) -> CapabilityResult[EtfCapabilityData]:
    start = date(2026, 5, 23)
    bars = tuple(
        EtfBar(
            data_date=start + timedelta(days=index),
            close=100.0 + index,
            traded_amount=1_000_000.0 + index * 100_000.0,
        )
        for index in range(61)
    )
    data = EtfCapabilityData(
        code=etf.code,
        bars=bars,
        quoted_at=AS_OF - timedelta(minutes=1),
        current_price=161.0,
        current_traded_amount=9_000_000.0,
        active=True,
        suspended=False,
        bid_price=160.0,
        ask_price=162.0,
        nav=160.0,
        tracking_error_pct=0.2,
        tracking_difference_pct=-0.1,
        annual_fee_pct=0.6,
        net_assets_cny=5_000_000_000.0,
        shares=30_000_000.0,
    )
    return CapabilityResult(
        capability="etf_snapshot",
        status=status,
        data=data,
        source="fixture-etf",
        observed_at=observed_at,
        data_date=bars[-1].data_date,
        bar_status="finalized",
        freshness_seconds=60,
        trace=({"provider": "fixture-etf", "result": "ok"},),
        error=None,
    )


def test_etf_builder_uses_exact_session_formulas_and_point_in_time_facts() -> None:
    sector = _sector(1, ("510300",))
    score = _score(sector, 90)
    etf = sector.etfs[0]
    result = _capability(etf)

    observation = CnEtfObservationBuilder().build(
        score, etf, result, observed_at=AS_OF
    )

    amounts = [bar.traded_amount for bar in result.data.bars[-20:]]
    closes = [bar.close for bar in result.data.bars]
    assert observation.mapping_effective_from == etf.effective_from
    assert observation.mapping_effective_to is None
    assert observation.finalized_session_count == 61
    assert observation.average_traded_amount_20d == pytest.approx(sum(amounts) / 20)
    assert observation.return_20d_pct == pytest.approx((closes[-1] / closes[-21] - 1) * 100)
    assert observation.return_60d_pct == pytest.approx((closes[-1] / closes[-61] - 1) * 100)
    assert observation.daily_return_dates_60 == tuple(
        bar.data_date for bar in result.data.bars[-60:]
    )
    assert observation.daily_returns_60 == pytest.approx(
        tuple(
            result.data.bars[index].close / result.data.bars[index - 1].close - 1
            for index in range(1, len(result.data.bars))
        )
    )
    assert observation.spread_bps == pytest.approx((2.0 / 161.0) * 10_000)
    assert observation.premium_discount_pct == pytest.approx((161.0 / 160.0 - 1) * 100)
    assert observation.size_cny == 5_000_000_000.0
    assert observation.quality == "complete"
    assert observation.raw_reference["sector_confidence"] == 0.75
    assert observation.raw_reference["capability"]["source"] == "fixture-etf"


def test_candidate_selection_is_rank_curated_order_deduplicated_and_exactly_30() -> None:
    first = _sector(1, tuple(f"51{index:04d}" for index in range(20)))
    second_codes = (first.etfs[0].code,) + tuple(
        f"56{index:04d}" for index in range(20)
    )
    second = _sector(2, second_codes)
    future = _sector(3, ("588888",)).model_copy(
        update={
            "etfs": (
                _etf("588888", "industry:sector-3", start=date(2026, 7, 23)),
            )
        }
    )

    selected = _select_effective_candidates(
        (first, second, future),
        (_score(second, 99), _score(first, 90), _score(future, 80)),
        AS_OF,
        limit=30,
    )

    assert len(selected) == 30
    assert [etf.code for _, etf in selected[:21]] == list(second_codes)
    assert [etf.code for _, etf in selected[21:]] == [
        item.code for item in first.etfs[1:10]
    ]
    assert len({etf.code for _, etf in selected}) == 30
    assert "588888" not in {etf.code for _, etf in selected}


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


def test_collector_isolates_provider_failure_and_uses_final_observation_anchor() -> None:
    sector = _sector(1, ("510300", "510500"))
    later = AS_OF + timedelta(seconds=30)

    class Provider:
        def fetch_etf(self, etf, as_of, **kwargs):
            if etf.code == "510300":
                raise RuntimeError("secret upstream failure")
            return _capability(etf, observed_at=later)

    batch = MarketRadarEtfCollector(
        provider=Provider(), executor_factory=_ImmediateExecutor
    ).collect((sector,), (_score(sector, 90),), AS_OF)

    assert batch.as_of == later
    assert len(batch.observations) == 2
    assert all(item.observed_at == later for item in batch.observations)
    assert batch.observations[0].quality == "unavailable"
    assert batch.observations[1].quality == "complete"
    assert "secret" not in str(batch.trace)


class _NeverCompletingExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.futures: list[Future] = []
        self.shutdown_calls = []

    def submit(self, fn, *args):
        future = Future()
        self.futures.append(future)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdown_calls.append((wait, cancel_futures))
        if cancel_futures:
            for future in self.futures:
                future.cancel()


class _DeadlineClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls <= 2 else 90.0


def test_collector_bounds_active_work_cancels_deadline_and_build_errors_fail() -> None:
    sector = _sector(1, tuple(f"51{index:04d}" for index in range(12)))
    executors = []

    def executor_factory(max_workers):
        executor = _NeverCompletingExecutor(max_workers)
        executors.append(executor)
        return executor

    batch = MarketRadarEtfCollector(
        provider=object(),
        monotonic=_DeadlineClock(),
        executor_factory=executor_factory,
    ).collect((sector,), (_score(sector, 90),), AS_OF)

    assert executors[0].max_workers == 6
    assert len(executors[0].futures) <= 6
    assert executors[0].shutdown_calls == [(False, True)]
    assert len(batch.observations) == 12
    assert all(item.quality == "unavailable" for item in batch.observations)
    assert all(item.raw_reference["capability"]["error"] == "deadline_exceeded" for item in batch.observations)

    class ExplodingBuilder:
        def build(self, *args, **kwargs):
            raise TypeError("builder contract broken")

    one = _sector(2, ("560001",))
    with pytest.raises(TypeError, match="builder contract broken"):
        MarketRadarEtfCollector(
            provider=type(
                "Provider",
                (),
                {"fetch_etf": lambda self, etf, as_of, **kwargs: _capability(etf)},
            )(),
            builder=ExplodingBuilder(),
            executor_factory=_ImmediateExecutor,
        ).collect((one,), (_score(one, 90),), AS_OF)


def test_collection_config_uses_code_owned_bounds() -> None:
    assert EtfCollectionConfig() == EtfCollectionConfig(
        candidate_limit=30,
        total_budget_seconds=90,
        max_concurrency=6,
    )
