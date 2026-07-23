from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import sqrt
from statistics import correlation, fmean

import pytest

from src.market_radar.models import (
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    SectorScore,
)
from src.market_radar.policy_config import PositionPolicyConfig
from src.market_radar.position_policy import build_position_plan


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
DATES = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(60))


def _normalise(values: tuple[float, ...]) -> tuple[float, ...]:
    mean = fmean(values)
    centred = tuple(value - mean for value in values)
    magnitude = sqrt(sum(value * value for value in centred))
    return tuple(value / magnitude for value in centred)


def _orthonormal_vectors() -> tuple[tuple[float, ...], ...]:
    raw = (
        tuple(float(index) for index in range(60)),
        tuple(1.0 if index % 2 else -1.0 for index in range(60)),
        tuple(float((index - 30) ** 2) for index in range(60)),
    )
    basis: list[tuple[float, ...]] = []
    for values in raw:
        residual = list(_normalise(values))
        for vector in basis:
            projection = sum(left * right for left, right in zip(residual, vector))
            residual = [left - projection * right for left, right in zip(residual, vector)]
        basis.append(_normalise(tuple(residual)))
    return tuple(basis)


VECTOR_A, VECTOR_B, VECTOR_C = _orthonormal_vectors()


def _mix(
    first: tuple[float, ...], second: tuple[float, ...], coefficient: float
) -> tuple[float, ...]:
    secondary = sqrt(1 - coefficient**2)
    return tuple(
        coefficient * left + secondary * right
        for left, right in zip(first, second)
    )


def sector(
    sector_id: str,
    *,
    score: float = 80.0,
    confidence: float = 0.9,
    state: str = "leading",
    quality: str = "complete",
    risk_reasons: tuple[str, ...] = (),
) -> SectorScore:
    return SectorScore(
        sector_id=sector_id,
        name=sector_id,
        kind="industry",
        scoring_version="cn-v1",
        gross_score=score,
        risk_deduction=0.0,
        score=score,
        confidence=confidence,
        state=state,  # type: ignore[arg-type]
        factors={
            "trend_momentum": 20.0,
            "relative_strength": 16.0,
            "capital_flow": 16.0,
            "breadth": 12.0,
            "liquidity_expansion": 8.0,
            "catalyst": 8.0,
        },
        risk_reasons=risk_reasons,
        missing_fields=(),
        source="fixture",
        observed_at=NOW,
        quality=quality,  # type: ignore[arg-type]
    )


def selection(
    sector_id: str,
    code: str,
    *,
    status: str = "best_supported",
    rank: int | None = 1,
    score: float | None = 80.0,
    confidence: float = 0.9,
    returns: tuple[float, ...] = VECTOR_A,
    dates: tuple[date, ...] = DATES,
) -> EtfSelection:
    observation = EtfObservation(
        sector_id=sector_id,
        code=code,
        name=f"ETF {code}",
        observed_at=NOW,
        data_date=date(2026, 7, 23),
        bar_status="finalized",
        source="fixture",
        quality="complete",
        freshness_seconds=30,
        mapping_effective_from=date(2026, 1, 1),
        active=True,
        finalized_session_count=60,
        suspended=False,
        current_price=1.2,
        current_traded_amount=20_000_000.0,
        average_traded_amount_20d=15_000_000.0,
        spread_bps=10.0,
        premium_discount_pct=0.1,
        return_20d_pct=3.0,
        return_60d_pct=8.0,
        daily_return_dates_60=dates,
        daily_returns_60=returns,
        tracking_error_pct=0.2,
        tracking_difference_pct=-0.1,
        annual_fee_pct=0.5,
        size_cny=1_000_000_000.0,
        liquidity_stability=0.9,
        missing_fields=(),
    )
    return EtfSelection(
        sector_id=sector_id,
        code=code,
        name=observation.name,
        status=status,  # type: ignore[arg-type]
        eligible=status in {"best_supported", "candidate"},
        rank=rank,
        score=score,
        confidence=confidence,
        components=EtfComponentScores(),
        observation=observation,
    )


def regime(*, kind: str = "risk_on", confidence: float = 0.9) -> MarketRegimeAssessment:
    return MarketRegimeAssessment(
        as_of=NOW,
        regime=kind,  # type: ignore[arg-type]
        confidence=confidence,
        coverage=0.9,
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("risk_on", (60.0, 80.0)),
        ("selective", (35.0, 60.0)),
        ("defensive", (10.0, 35.0)),
        ("risk_off", (0.0, 15.0)),
        ("insufficient_data", (0.0, 10.0)),
    ],
)
def test_range_matches_each_regime(kind: str, expected: tuple[float, float]) -> None:
    plan = build_position_plan((), (), regime(kind=kind), PositionPolicyConfig())

    assert (plan.total_position_min_pct, plan.total_position_max_pct) == expected
    assert plan.suggestions == ()
    assert plan.reason_codes == ("no_supported_sector_suggestions",)


def test_candidates_are_filtered_ordered_and_prefer_best_supported() -> None:
    alpha = sector("industry:alpha", score=90.0, confidence=0.75)
    delta = sector("industry:delta", score=85.0, confidence=0.8, state="improving")
    beta = sector("industry:beta", score=80.0, confidence=0.9)
    rejected = sector("industry:rejected", score=99.0)
    neutral = sector("industry:neutral", score=98.0, state="neutral")
    low_confidence = sector("industry:low", score=97.0, confidence=0.5999)
    selections = (
        selection(alpha.sector_id, "100002", status="candidate", rank=1, score=99.0),
        selection(alpha.sector_id, "100001", status="best_supported", rank=2, score=1.0),
        selection(delta.sector_id, "100003", status="candidate", rank=1),
        selection(beta.sector_id, "100005", status="candidate", rank=2, score=99.0),
        selection(beta.sector_id, "100004", status="candidate", rank=1, score=1.0),
        selection(rejected.sector_id, "100006", status="rejected", rank=None, score=None),
        selection(neutral.sector_id, "100007"),
        selection(low_confidence.sector_id, "100008"),
    )

    plan = build_position_plan(
        (beta, low_confidence, rejected, delta, neutral, alpha),
        selections,
        regime(),
        PositionPolicyConfig(),
    )

    assert [(item.sector_id, item.etf_code, item.etf_status) for item in plan.suggestions] == [
        ("industry:alpha", "100001", "best_supported"),
        ("industry:delta", "100003", "candidate"),
        ("industry:beta", "100004", "candidate"),
    ]
    assert [item.sector_rank for item in plan.suggestions] == [1, 2, 3]
    assert all(item.minimum_pct is None for item in plan.suggestions)


def test_caps_use_decimal_floor_and_drop_zero_cap_suggestions() -> None:
    supported = sector("industry:supported", confidence=0.61)
    zero = sector("industry:zero", score=81.0)

    plan = build_position_plan(
        (supported, zero),
        (
            selection(supported.sector_id, "100009", confidence=0.99),
            selection(zero.sector_id, "100010", confidence=0.0),
        ),
        regime(confidence=0.99),
        PositionPolicyConfig(),
    )

    assert len(plan.suggestions) == 1
    assert plan.suggestions[0].sector_cap_pct == 9.1
    assert plan.suggestions[0].etf_cap_pct == 9.1
    assert plan.reason_codes == ()


def _with_unvalidated_returns(
    item: EtfSelection,
    *,
    returns: tuple[float, ...],
    dates: tuple[date, ...] = DATES,
) -> EtfSelection:
    observation = item.observation.model_copy(
        update={"daily_return_dates_60": dates, "daily_returns_60": returns}
    )
    return item.model_copy(update={"observation": observation})


@pytest.mark.parametrize(
    "other",
    [
        tuple(VECTOR_B[:59]),
        VECTOR_B,
        tuple(float("nan") if index == 4 else value for index, value in enumerate(VECTOR_B)),
        tuple(0.01 for _ in range(60)),
    ],
    ids=("fewer_than_sixty", "misaligned_dates", "non_finite", "zero_variance"),
)
def test_invalid_correlation_evidence_is_unknown(other: tuple[float, ...], request: pytest.FixtureRequest) -> None:
    first = sector("industry:first")
    second = sector("industry:second", score=79.0)
    second_dates = (
        tuple(value + timedelta(days=1) for value in DATES)
        if request.node.callspec.id == "misaligned_dates"
        else DATES
    )
    selections = (
        selection(first.sector_id, "200001", returns=VECTOR_A),
        _with_unvalidated_returns(
            selection(second.sector_id, "200002", returns=VECTOR_B),
            returns=other,
            dates=second_dates,
        ),
    )

    plan = build_position_plan((first, second), selections, regime(), PositionPolicyConfig())

    assert plan.correlation_groups == ()
    assert plan.correlation_coverage == 0.0
    assert plan.confidence == 0.0
    assert plan.reason_codes == ("correlation_coverage_incomplete",)


def test_correlation_threshold_is_inclusive_and_below_threshold_is_not_grouped() -> None:
    assert correlation(VECTOR_A, _mix(VECTOR_A, VECTOR_B, 0.8)) >= 0.8
    assert correlation(VECTOR_A, _mix(VECTOR_A, VECTOR_B, 0.799)) < 0.8
    first = sector("industry:first")
    second = sector("industry:second", score=79.0)

    at_threshold = build_position_plan(
        (first, second),
        (
            selection(first.sector_id, "200003", returns=VECTOR_A),
            selection(second.sector_id, "200004", returns=_mix(VECTOR_A, VECTOR_B, 0.8)),
        ),
        regime(),
        PositionPolicyConfig(),
    )
    below_threshold = build_position_plan(
        (first, second),
        (
            selection(first.sector_id, "200003", returns=VECTOR_A),
            selection(second.sector_id, "200004", returns=_mix(VECTOR_A, VECTOR_B, 0.799)),
        ),
        regime(),
        PositionPolicyConfig(),
    )

    assert at_threshold.correlation_groups[0].etf_codes == ("200003", "200004")
    assert below_threshold.correlation_groups == ()
    assert at_threshold.correlation_coverage == below_threshold.correlation_coverage == 1.0


def test_transitive_group_reduces_lowest_ranked_caps_first() -> None:
    first = sector("industry:first", score=90.0, confidence=1.0)
    second = sector("industry:second", score=80.0, confidence=1.0)
    third = sector("industry:third", score=70.0, confidence=1.0)
    second_returns = _mix(VECTOR_A, VECTOR_B, 0.9)
    third_returns = _mix(second_returns, VECTOR_C, 0.85)

    plan = build_position_plan(
        (first, second, third),
        (
            selection(first.sector_id, "300003", status="candidate", rank=1, confidence=1.0),
            selection(second.sector_id, "300002", status="candidate", rank=2, confidence=1.0, returns=second_returns),
            selection(third.sector_id, "300001", status="candidate", rank=3, confidence=1.0, returns=third_returns),
        ),
        regime(confidence=1.0),
        PositionPolicyConfig(),
    )

    assert plan.correlation_groups[0].etf_codes == ("300001", "300002", "300003")
    assert [item.etf_cap_pct for item in plan.suggestions] == [15.0, 10.0, 0.0]
    assert plan.suggestions[1].invalidation_codes == ("correlation_cap_reached",)
    assert plan.suggestions[2].invalidation_codes == ("correlation_cap_reached",)
    assert sum(item.etf_cap_pct for item in plan.suggestions) == 25.0


def test_correlation_reduction_uses_code_as_rank_tie_breaker() -> None:
    first = sector("industry:first", score=90.0, confidence=1.0)
    second = sector("industry:second", score=80.0, confidence=1.0)

    plan = build_position_plan(
        (first, second),
        (
            selection(first.sector_id, "400002", status="candidate", rank=1, confidence=1.0),
            selection(second.sector_id, "400001", status="candidate", rank=1, confidence=1.0, returns=_mix(VECTOR_A, VECTOR_B, 0.9)),
        ),
        regime(confidence=1.0),
        PositionPolicyConfig(),
    )

    caps_by_code = {item.etf_code: item.etf_cap_pct for item in plan.suggestions}
    assert caps_by_code == {"400002": 15.0, "400001": 10.0}


def test_one_suggestion_has_full_correlation_coverage() -> None:
    item = sector("industry:only")

    plan = build_position_plan(
        (item,),
        (selection(item.sector_id, "500001"),),
        regime(confidence=0.8),
        PositionPolicyConfig(),
    )

    assert plan.correlation_coverage == 1.0
    assert plan.confidence == 0.8
    assert plan.correlation_groups == ()


def test_missing_pair_penalises_plan_confidence() -> None:
    first = sector("industry:first")
    second = sector("industry:second", score=80.0)
    third = sector("industry:third", score=70.0)
    missing = _with_unvalidated_returns(
        selection(third.sector_id, "600003", returns=VECTOR_C), returns=()
    )

    plan = build_position_plan(
        (first, second, third),
        (
            selection(first.sector_id, "600001", returns=VECTOR_A),
            selection(second.sector_id, "600002", returns=_mix(VECTOR_A, VECTOR_B, 0.5)),
            missing,
        ),
        regime(),
        PositionPolicyConfig(),
    )

    assert plan.correlation_coverage == pytest.approx(1 / 3)
    assert plan.confidence == 0.3
    assert plan.reason_codes == ("correlation_coverage_incomplete",)
