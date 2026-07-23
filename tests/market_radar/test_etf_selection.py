from datetime import date, datetime, timezone

import pytest

from src.market_radar.etf_selection import (
    HARD_FILTER_REASON_CODES,
    _component_values,
    select_etfs,
)
from src.market_radar.models import EtfObservation
from src.market_radar.policy_config import EtfPolicyConfig


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def observation(
    *,
    code: str = "512480",
    sector_id: str = "industry:semiconductor",
    **overrides: object,
) -> EtfObservation:
    dates = tuple(date(2026, 4, day) for day in range(1, 31)) + tuple(
        date(2026, 5, day) for day in range(1, 31)
    )
    values: dict[str, object] = {
        "sector_id": sector_id,
        "code": code,
        "name": f"ETF {code}",
        "observed_at": NOW,
        "data_date": date(2026, 7, 21),
        "bar_status": "finalized",
        "source": "fixture",
        "quality": "complete",
        "freshness_seconds": 30,
        "mapping_effective_from": date(2026, 1, 1),
        "active": True,
        "finalized_session_count": 60,
        "suspended": False,
        "current_price": 1.2,
        "current_traded_amount": 20_000_000.0,
        "average_traded_amount_20d": 15_000_000.0,
        "spread_bps": 10.0,
        "premium_discount_pct": 0.1,
        "return_20d_pct": 3.0,
        "return_60d_pct": 8.0,
        "daily_return_dates_60": dates,
        "daily_returns_60": tuple(0.01 for _ in dates),
        "tracking_error_pct": 0.2,
        "tracking_difference_pct": -0.1,
        "annual_fee_pct": 0.5,
        "size_cny": 1_000_000_000.0,
        "liquidity_stability": 0.9,
    }
    values.update(overrides)
    values["missing_fields"] = tuple(
        field
        for field in EtfObservation.tracked_metric_fields
        if values.get(field) is None
        or (
            field in {"daily_return_dates_60", "daily_returns_60"}
            and not values.get(field)
        )
    )
    return EtfObservation(**values)


def test_hard_filter_reasons_preserve_specification_order() -> None:
    item = observation(
        data_date=date(2025, 12, 31),
        active=False,
        finalized_session_count=59,
        current_price=0.0,
        current_traded_amount=-1.0,
        average_traded_amount_20d=9_999_999.0,
        freshness_seconds=2701,
        suspended=True,
        spread_bps=50.1,
        premium_discount_pct=-2.1,
    )

    result = select_etfs([item], EtfPolicyConfig())[0]

    assert HARD_FILTER_REASON_CODES == (
        "inactive_mapping",
        "not_active",
        "insufficient_history",
        "invalid_price",
        "invalid_amount",
        "low_liquidity",
        "stale_quote",
        "suspended",
        "data_integrity_failure",
        "spread_too_wide",
        "premium_discount_too_large",
    )
    assert result.status == "rejected"
    assert result.reason_codes == (
        "inactive_mapping",
        "not_active",
        "insufficient_history",
        "invalid_price",
        "invalid_amount",
        "low_liquidity",
        "stale_quote",
        "suspended",
        "spread_too_wide",
        "premium_discount_too_large",
    )
    assert result.score is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("finalized_session_count", 60),
        ("average_traded_amount_20d", 10_000_000.0),
        ("spread_bps", 50.0),
        ("premium_discount_pct", -2.0),
        ("freshness_seconds", 2700),
    ],
)
def test_filter_boundaries_are_inclusive(field: str, value: object) -> None:
    result = select_etfs([observation(**{field: value})], EtfPolicyConfig())[0]

    assert result.eligible is True
    assert result.status == "best_supported"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("finalized_session_count", 59, "insufficient_history"),
        ("average_traded_amount_20d", 9_999_999.99, "low_liquidity"),
        ("spread_bps", 50.01, "spread_too_wide"),
        ("premium_discount_pct", 2.01, "premium_discount_too_large"),
    ],
)
def test_filter_values_just_outside_boundaries_are_rejected(
    field: str, value: object, reason: str
) -> None:
    result = select_etfs([observation(**{field: value})], EtfPolicyConfig())[0]

    assert result.status == "rejected"
    assert result.reason_codes == (reason,)


@pytest.mark.parametrize(
    ("field", "missing_reason"),
    [
        ("data_date", "missing_data_date"),
        ("active", "missing_active"),
        ("finalized_session_count", "missing_finalized_session_count"),
        ("current_price", "missing_current_price"),
        ("current_traded_amount", "missing_current_traded_amount"),
        ("average_traded_amount_20d", "missing_average_traded_amount_20d"),
    ],
)
def test_missing_required_filter_evidence_is_insufficient_not_rejected(
    field: str, missing_reason: str
) -> None:
    result = select_etfs([observation(**{field: None})], EtfPolicyConfig())[0]

    assert result.status == "insufficient_data"
    assert result.eligible is False
    assert result.reason_codes == (missing_reason,)
    assert result.score is None


@pytest.mark.parametrize("field", ["spread_bps", "premium_discount_pct", "suspended"])
def test_optional_safety_gaps_remain_eligible_but_prevent_best_supported(
    field: str,
) -> None:
    result = select_etfs([observation(**{field: None})], EtfPolicyConfig())[0]

    assert result.eligible is True
    assert result.status == "candidate"
    assert result.reason_codes == ()
    assert result.confidence == 0.9333


def test_missing_liquidity_stability_remains_missing_not_zero() -> None:
    values = _component_values(observation(liquidity_stability=None))

    assert values["liquidity"] == (15_000_000.0, None)


def test_one_eligible_etf_scores_one_hundred() -> None:
    result = select_etfs([observation()], EtfPolicyConfig())[0]

    assert result.score == 100.0
    assert result.rank == 1
    assert result.status == "best_supported"


def test_percentiles_use_mean_ordinals_and_reverse_tracking_and_cost_metrics() -> None:
    tied_a = observation(
        code="512481",
        average_traded_amount_20d=10_000_000.0,
        return_20d_pct=1.0,
        return_60d_pct=1.0,
        tracking_error_pct=0.6,
        annual_fee_pct=0.8,
        size_cny=100_000_000.0,
    )
    tied_b = observation(
        code="512482",
        average_traded_amount_20d=10_000_000.0,
        return_20d_pct=1.0,
        return_60d_pct=1.0,
        tracking_error_pct=0.6,
        annual_fee_pct=0.8,
        size_cny=100_000_000.0,
    )
    best = observation(
        code="512483",
        average_traded_amount_20d=30_000_000.0,
        return_20d_pct=5.0,
        return_60d_pct=5.0,
        tracking_error_pct=0.1,
        annual_fee_pct=0.2,
        size_cny=900_000_000.0,
    )

    results = {item.code: item for item in select_etfs([tied_a, tied_b, best], EtfPolicyConfig())}

    assert results["512481"].components.liquidity == 25.0
    assert results["512482"].components.tracking_quality == 25.0
    assert results["512483"].components.cost == 100.0
    assert results["512483"].score == 100.0


def test_tracking_error_is_preferred_to_tracking_difference_when_both_exist() -> None:
    lower_error = observation(
        code="512481", tracking_error_pct=0.1, tracking_difference_pct=5.0
    )
    higher_error = observation(
        code="512482", tracking_error_pct=0.5, tracking_difference_pct=0.0
    )

    results = {item.code: item for item in select_etfs([lower_error, higher_error], EtfPolicyConfig())}

    assert results["512481"].components.tracking_quality == 100.0
    assert results["512482"].components.tracking_quality == 0.0


def test_missing_optional_components_renormalize_score_and_lower_confidence() -> None:
    complete = observation(code="512481")
    missing_fee = observation(code="512482", annual_fee_pct=None)

    results = {item.code: item for item in select_etfs([complete, missing_fee], EtfPolicyConfig())}

    assert results["512482"].components.cost is None
    assert results["512482"].effective_weights == {
        "liquidity": 38.8889,
        "trend": 27.7778,
        "tracking_quality": 22.2222,
        "size": 11.1111,
    }
    assert results["512482"].confidence == 0.9
    assert results["512482"].confidence < results["512481"].confidence


@pytest.mark.parametrize(
    "overrides",
    [
        {"average_traded_amount_20d": None},
        {"return_20d_pct": None, "return_60d_pct": None},
    ],
)
def test_missing_required_ranking_component_is_insufficient(
    overrides: dict[str, object],
) -> None:
    result = select_etfs([observation(**overrides)], EtfPolicyConfig())[0]

    assert result.status == "insufficient_data"
    assert result.score is None


def test_final_ranking_tie_breaker_is_stable_and_only_one_sector_winner() -> None:
    first = observation(code="512482")
    second = observation(code="512481")
    other_sector = observation(code="512483", sector_id="concept:technology")

    results = select_etfs([first, second, other_sector], EtfPolicyConfig())

    assert [item.code for item in results] == ["512481", "512482", "512483"]
    assert [item.status for item in results] == [
        "best_supported",
        "candidate",
        "best_supported",
    ]
    assert [item.rank for item in results] == [1, 2, 1]
