from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import log
from statistics import stdev

import pytest

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
from src.market_radar.models import FrozenModel, SectorObservation
from src.market_radar.observation_builder import (
    CnSectorObservationBuilder,
    ConstituentEvidence,
    ObservationBuildResult,
    canonical_constituent_set_key,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
START = date(2026, 7, 2)


def _dates(count: int) -> list[date]:
    return [START + timedelta(days=index) for index in range(count)]


def _base(**updates: object) -> SectorObservation:
    payload: dict[str, object] = {
        "sector_id": "industry:semiconductor",
        "market": "cn",
        "kind": "industry",
        "name": "Semiconductor",
        "observed_at": NOW,
        "source": "phase1",
        "freshness_seconds": 11,
        "quality": "partial",
        **{field: None for field in SectorObservation.tracked_metric_fields},
        "raw_reference": {"phase": 1},
    }
    payload.update(updates)
    payload["missing_fields"] = tuple(
        field
        for field in SectorObservation.tracked_metric_fields
        if payload.get(field) is None
    )
    return SectorObservation(**payload)


def _capability(
    capability: str,
    data: FrozenModel | None,
    *,
    source: str,
    data_date: date | None,
    status: str = "ok",
    bar_status: str | None = "finalized",
    freshness: int = 3,
    observed_at: datetime = NOW,
    trace: tuple[dict[str, object], ...] = (),
    error: str | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        capability=capability,
        status=status,
        data=data,
        source=source,
        observed_at=observed_at,
        data_date=data_date,
        bar_status=bar_status,
        freshness_seconds=freshness,
        trace=trace,
        error=error,
    )


def _history(
    closes: list[float],
    *,
    code: str = "industry:semiconductor",
    amounts: list[float] | None = None,
    dates: list[date] | None = None,
    status: str = "ok",
    bar_status: str = "finalized",
    freshness: int = 3,
) -> CapabilityResult[BoardBarSeries]:
    row_dates = dates or _dates(len(closes))
    row_amounts = amounts or [100.0] * len(closes)
    terminal = row_dates[-1]
    return _capability(
        "benchmark_history" if code == "000985" else "board_history",
        BoardBarSeries(
            code=code,
            bars=[
                BoardBar(data_date=row_date, close=close, traded_amount=amount)
                for row_date, close, amount in zip(row_dates, closes, row_amounts)
            ],
        ),
        source="benchmark-fixture" if code == "000985" else "board-fixture",
        data_date=terminal,
        status=status,
        bar_status=bar_status,
        freshness=freshness,
    )


def _flows(
    values: list[float],
    *,
    amounts: list[float] | None = None,
    dates: list[date] | None = None,
    bar_status: str = "finalized",
) -> CapabilityResult[BoardFlowSeries]:
    row_dates = dates or _dates(21)[-len(values) :]
    row_amounts = amounts or [100.0] * len(values)
    return _capability(
        "board_flow",
        BoardFlowSeries(
            code="industry:semiconductor",
            flows=[
                BoardFlow(
                    data_date=row_date,
                    net_main_inflow=value,
                    traded_amount=amount,
                )
                for row_date, value, amount in zip(row_dates, values, row_amounts)
            ],
        ),
        source="flow-fixture",
        data_date=row_dates[-1],
        bar_status=bar_status,
    )


def _membership(
    codes: tuple[str, ...] = (),
    *,
    data_date: date | None = None,
    status: str = "ok",
    observed_at: datetime = NOW,
) -> CapabilityResult[ConstituentMembership]:
    codes = codes or tuple(f"{index:06d}" for index in range(1, 11))
    return _capability(
        "constituents",
        ConstituentMembership(codes=codes, data_date=data_date),
        source="membership-fixture",
        data_date=data_date,
        status=status,
        bar_status=None if data_date is None else "finalized",
        observed_at=observed_at,
        error="unversioned_current_membership" if data_date is None else None,
    )


def _quotes(
    codes: tuple[str, ...] = (),
    *,
    data_date: date,
    amounts: tuple[float, ...] = (),
    moves: tuple[int, ...] = (),
    status: str = "ok",
) -> CapabilityResult[ConstituentQuoteBatch]:
    codes = codes or tuple(f"{index:06d}" for index in range(1, 11))
    amounts = amounts or tuple(float(index) for index in range(1, len(codes) + 1))
    moves = moves or tuple((1, -1, 0)[index % 3] for index in range(len(codes)))
    quoted_at = datetime.combine(data_date, datetime.min.time(), timezone.utc)
    return _capability(
        "constituent_quotes",
        ConstituentQuoteBatch(
            quotes=[
                ConstituentQuote(
                    code=code,
                    current_price=10.0 + move,
                    previous_close=10.0,
                    traded_amount=amount,
                    quoted_at=quoted_at,
                )
                for code, amount, move in zip(codes, amounts, moves)
            ]
        ),
        source="quote-fixture",
        data_date=data_date,
        status=status,
        bar_status="provisional",
    )


def _unavailable(capability: str) -> CapabilityResult:
    return _capability(
        capability,
        None,
        source="none",
        data_date=None,
        status="unavailable",
        bar_status=None,
        freshness=0,
        error="not available",
    )


def _build(
    *,
    base: SectorObservation | None = None,
    board_history: CapabilityResult[BoardBarSeries] | None = None,
    benchmark_history: CapabilityResult[BoardBarSeries] | None = None,
    board_flow: CapabilityResult[BoardFlowSeries] | None = None,
    membership: CapabilityResult[ConstituentMembership] | None = None,
    quotes: CapabilityResult[ConstituentQuoteBatch] | None = None,
    config: MarketRadarEnrichmentConfig | None = None,
) -> ObservationBuildResult:
    history = board_history or _history([100.0] * 21)
    terminal = history.data_date or _dates(21)[-1]
    return CnSectorObservationBuilder(config).build(
        base=base or _base(),
        candidate_reasons=("configured_seed", "previous_leader"),
        benchmark_code="000985",
        board_history=history,
        benchmark_history=benchmark_history or _history([100.0] * 21, code="000985"),
        board_flow=board_flow or _flows([1.0] * 20),
        membership=membership or _membership(data_date=terminal),
        quotes=quotes or _quotes(data_date=terminal),
        observed_at=NOW,
    )


@pytest.mark.parametrize(("window", "expected"), [(1, 2.0), (5, 10.0), (20, 25.0)])
def test_returns_require_exact_prior_finalized_sessions(
    window: int, expected: float
) -> None:
    closes = [100.0] * 21 + [100.0 * (1 + expected / 100)]

    result = _build(board_history=_history(closes))

    assert getattr(result.observation, f"return_{window}d_pct") == pytest.approx(
        expected
    )


@pytest.mark.parametrize("window", [1, 5, 20])
def test_returns_do_not_shorten_insufficient_windows(window: int) -> None:
    result = _build(board_history=_history([100.0] * window))

    assert getattr(result.observation, f"return_{window}d_pct") is None


def test_flow_windows_include_terminal_and_normalize_by_summed_amount() -> None:
    result = _build(
        board_flow=_flows([1.0] * 20, amounts=[10.0] * 20),
    )

    assert result.observation.capital_flow_1d == pytest.approx(10.0)
    assert result.observation.capital_flow_5d == pytest.approx(10.0)
    assert result.observation.capital_flow_20d == pytest.approx(10.0)

    zero_denominator = _build(board_flow=_flows([1.0] * 20, amounts=[0.0] * 20))
    assert zero_denominator.observation.capital_flow_20d is None


def test_flow_requires_exact_rows_and_matching_terminal_date() -> None:
    short = _build(board_flow=_flows([1.0] * 19))
    wrong_date = _build(
        board_flow=_flows([1.0] * 20, dates=_dates(20)[:-1] + [date(2026, 8, 1)])
    )

    assert short.observation.capital_flow_20d is None
    assert wrong_date.observation.capital_flow_1d is None
    assert wrong_date.observation.capital_flow_5d is None
    assert wrong_date.observation.capital_flow_20d is None


def test_provisional_and_finalized_ma20_use_approved_windows() -> None:
    closes = [float(value) for value in range(1, 22)]
    provisional = _build(
        board_history=_history(closes, bar_status="provisional")
    ).observation
    finalized = _build(
        board_history=_history(closes, bar_status="finalized")
    ).observation

    assert provisional.distance_ma20_pct == pytest.approx(
        (21.0 / (sum(range(1, 21)) / 20) - 1) * 100
    )
    assert finalized.distance_ma20_pct == pytest.approx(
        (21.0 / (sum(range(2, 22)) / 20) - 1) * 100
    )


@pytest.mark.parametrize("bar_status", ["provisional", "finalized"])
def test_liquidity_uses_twenty_prior_finalized_amounts(bar_status: str) -> None:
    result = _build(
        board_history=_history(
            [100.0] * 21,
            amounts=[10.0] * 20 + [30.0],
            bar_status=bar_status,
        )
    )

    assert result.observation.turnover_ratio_20d == pytest.approx(3.0)


def test_benchmark_return_and_volatility_use_exactly_aligned_dates() -> None:
    row_dates = _dates(21)
    sector_closes = [100.0 * (1.01**index) for index in range(21)]
    benchmark_closes = [100.0 * (1.005**index) for index in range(21)]

    observation = _build(
        board_history=_history(sector_closes, dates=row_dates),
        benchmark_history=_history(
            benchmark_closes, code="000985", dates=row_dates, freshness=17
        ),
    ).observation

    sector_returns = [
        log(current / previous)
        for previous, current in zip(sector_closes, sector_closes[1:])
    ]
    benchmark_returns = [
        log(current / previous)
        for previous, current in zip(benchmark_closes, benchmark_closes[1:])
    ]
    assert observation.benchmark_return_20d_pct == pytest.approx(
        (benchmark_closes[-1] / benchmark_closes[0] - 1) * 100
    )
    assert observation.volatility_ratio_20d == pytest.approx(
        stdev(sector_returns) / stdev(benchmark_returns)
    )
    assert observation.freshness_seconds == 17

    shifted_dates = row_dates[:-1] + [row_dates[-1] + timedelta(days=1)]
    shifted = _build(
        board_history=_history(sector_closes, dates=row_dates),
        benchmark_history=_history(
            benchmark_closes, code="000985", dates=shifted_dates
        ),
    ).observation
    assert shifted.benchmark_return_20d_pct is None
    assert shifted.volatility_ratio_20d is None


def test_zero_benchmark_volatility_is_unavailable() -> None:
    sector_closes = [100.0 + index * index for index in range(21)]
    observation = _build(
        board_history=_history(sector_closes),
        benchmark_history=_history([100.0] * 21, code="000985"),
    ).observation

    assert observation.volatility_ratio_20d is None


def test_breadth_and_concentration_publish_at_exact_coverage_boundary() -> None:
    codes = tuple(f"{index:06d}" for index in range(1, 11))
    valid_codes = codes[:8]
    result = _build(
        membership=_membership(codes, data_date=_dates(21)[-1]),
        quotes=_quotes(
            valid_codes,
            data_date=_dates(21)[-1],
            amounts=tuple(float(index) for index in range(1, 9)),
            moves=(1, 1, 1, 1, -1, -1, -1, 0),
            status="partial",
        ),
    )

    observation = result.observation
    assert (observation.up_count, observation.down_count, observation.flat_count) == (
        4,
        3,
        1,
    )
    assert observation.concentration_ratio == pytest.approx(
        sum(range(4, 9)) / sum(range(1, 9))
    )
    assert observation.raw_reference["constituent_coverage"] == {
        "total": 10,
        "valid": 8,
        "ratio": 0.8,
    }


def test_breadth_ignores_extra_quotes_and_enforces_date_membership_and_minimum() -> None:
    terminal = _dates(21)[-1]
    codes = tuple(f"{index:06d}" for index in range(1, 7))
    extras = codes + ("999999",)
    wrong_quote_date = _build(
        membership=_membership(codes, data_date=terminal),
        quotes=_quotes(extras, data_date=terminal - timedelta(days=1)),
    ).observation
    wrong_membership_date = _build(
        membership=_membership(codes, data_date=terminal - timedelta(days=1)),
        quotes=_quotes(extras, data_date=terminal),
    ).observation
    too_few = _build(
        membership=_membership(codes[:4], data_date=terminal),
        quotes=_quotes(extras, data_date=terminal),
    ).observation

    for observation in (wrong_quote_date, wrong_membership_date, too_few):
        assert observation.up_count is None
        assert observation.down_count is None
        assert observation.flat_count is None
        assert observation.concentration_ratio is None


def test_unversioned_membership_requires_same_online_run_and_quote_terminal_date() -> None:
    terminal = _dates(21)[-1]
    codes = tuple(f"{index:06d}" for index in range(1, 7))
    current = _build(
        membership=_membership(codes, data_date=None, status="partial"),
        quotes=_quotes(codes, data_date=terminal),
    )

    assert isinstance(current.constituent_evidence, ConstituentEvidence)
    assert current.constituent_evidence.data_date == terminal
    assert current.observation.raw_reference["capabilities"]["membership"][
        "membership_data_date"
    ] is None
    assert current.observation.raw_reference["capabilities"]["membership"][
        "provenance"
    ] == "partial_unversioned_current_membership"

    different_run = _build(
        membership=_membership(
            codes,
            data_date=None,
            status="partial",
            observed_at=NOW - timedelta(seconds=1),
        ),
        quotes=_quotes(codes, data_date=terminal),
    )
    assert different_run.constituent_evidence is None
    assert different_run.observation.up_count is None


def test_constituent_evidence_is_canonical_and_content_addressed() -> None:
    terminal = _dates(21)[-1]
    codes = ("600519", "000001", "300750")
    result = _build(
        membership=_membership(codes, data_date=terminal),
        quotes=_quotes(codes, data_date=terminal),
    )

    evidence = result.constituent_evidence
    assert evidence == ConstituentEvidence(
        market="cn",
        sector_id="industry:semiconductor",
        source="membership-fixture",
        data_date=terminal,
        observed_at=NOW,
        codes=("000001", "300750", "600519"),
        set_key=canonical_constituent_set_key(
            "cn",
            "industry:semiconductor",
            "membership-fixture",
            codes,
        ),
    )
    assert evidence.set_key.startswith("sha256:")
    assert result.observation.raw_reference["constituent_set_key"] == evidence.set_key


def test_versioned_membership_evidence_survives_unavailable_quotes() -> None:
    terminal = _dates(21)[-1]
    result = _build(
        membership=_membership(data_date=terminal),
        quotes=_unavailable("constituent_quotes"),
    )

    assert result.constituent_evidence is not None
    assert result.constituent_evidence.data_date == terminal
    assert result.observation.raw_reference["constituent_coverage"] == {
        "total": 10,
        "valid": 0,
        "ratio": 0.0,
    }


def test_divergence_is_false_at_noise_boundary_without_opposite_signs() -> None:
    same_sign = _build(
        board_history=_history([100.0] * 16 + [100.0] + [101.0] * 5),
        board_flow=_flows(
            [0.0] * 15 + [0.1] * 5,
            amounts=[100.0] * 20,
            dates=_dates(22)[-20:],
        ),
    ).observation
    opposite_sign = _build(
        board_history=_history([100.0] * 16 + [100.0] + [101.0] * 5),
        board_flow=_flows(
            [0.0] * 15 + [-0.1] * 5,
            amounts=[100.0] * 20,
            dates=_dates(22)[-20:],
        ),
    ).observation

    assert same_sign.return_5d_pct == pytest.approx(1.0)
    assert same_sign.capital_flow_5d == pytest.approx(0.1)
    assert same_sign.price_flow_divergence is False
    assert opposite_sign.capital_flow_5d == pytest.approx(-0.1)
    assert opposite_sign.price_flow_divergence is True


def test_failed_enrichment_preserves_base_values_and_recomputes_missing_fields() -> None:
    base = _base(return_1d_pct=7.5, capital_flow_20d=2.5, catalyst_score=0.4)
    result = _build(
        base=base,
        board_history=_unavailable("board_history"),
        benchmark_history=_unavailable("benchmark_history"),
        board_flow=_unavailable("board_flow"),
        membership=_unavailable("constituents"),
        quotes=_unavailable("constituent_quotes"),
    )

    observation = result.observation
    assert observation.return_1d_pct == 7.5
    assert observation.capital_flow_20d == 2.5
    assert observation.catalyst_score == 0.4
    assert observation.quality == "unavailable"
    assert set(observation.missing_fields) == {
        field
        for field in SectorObservation.tracked_metric_fields
        if getattr(observation, field) is None
    }


def test_current_price_quality_and_freshness_follow_used_price_capabilities() -> None:
    stale = _build(
        board_history=_history([100.0] * 21, status="stale", freshness=5000)
    ).observation
    sparse = _build(
        base=_base(return_1d_pct=4.0, freshness_seconds=29),
        board_history=_history([100.0], freshness=7),
        benchmark_history=_unavailable("benchmark_history"),
    ).observation

    assert stale.quality == "stale"
    assert stale.freshness_seconds == 5000
    assert sparse.quality == "partial"
    assert sparse.return_1d_pct == 4.0
    assert sparse.freshness_seconds == 29


def test_raw_reference_is_structured_v2a_and_drops_sensitive_trace_content() -> None:
    board = _history([100.0] * 21, bar_status="provisional")
    board = board.model_copy(
        update={
            "trace": (
                {
                    "provider": "fixture",
                    "result": "ok",
                    "headers": {"Authorization": "Bearer secret"},
                    "error": "Traceback (most recent call last):\nsecret stack",
                },
            )
        }
    )
    observation = _build(board_history=board).observation
    raw = observation.model_dump(mode="json")["raw_reference"]

    assert raw["schema"] == "market-radar-observation-v2a"
    assert raw["candidate_reasons"] == ["configured_seed", "previous_leader"]
    assert raw["benchmark_code"] == "000985"
    assert raw["data_date"] == _dates(21)[-1].isoformat()
    assert raw["bar_status"] == "provisional"
    assert raw["field_sources"]["return_20d_pct"] == "board-fixture"
    assert set(raw["capabilities"]) == {
        "board_history",
        "benchmark_history",
        "board_flow",
        "membership",
        "quotes",
    }
    serialized = str(raw)
    assert "Authorization" not in serialized
    assert "Bearer secret" not in serialized
    assert "Traceback" not in serialized
    assert "secret stack" not in serialized
