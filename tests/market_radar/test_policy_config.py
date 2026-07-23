import pytest
from pydantic import ValidationError

from src.market_radar.policy_config import (
    EtfPolicyConfig,
    PositionPolicyConfig,
    RegimeConfig,
)


def test_policy_config_defaults_are_approved_and_immutable() -> None:
    etf = EtfPolicyConfig()
    regime = RegimeConfig()
    position = PositionPolicyConfig()

    assert etf.policy_version == "cn-etf-v1"
    assert etf.candidate_limit == 30
    assert etf.total_budget_seconds == 90
    assert etf.max_concurrency == 6
    assert etf.minimum_finalized_sessions == 60
    assert etf.minimum_average_amount_cny == 10_000_000.0
    assert etf.stale_after_seconds == 2700
    assert etf.maximum_spread_bps == 50.0
    assert etf.maximum_abs_premium_discount_pct == 2.0
    assert dict(etf.component_weights) == {
        "liquidity": 35.0,
        "trend": 25.0,
        "tracking_quality": 20.0,
        "cost": 10.0,
        "size": 10.0,
    }
    assert regime.regime_version == "cn-regime-v1"
    assert regime.default_benchmark_code == "000985"
    assert regime.minimum_sector_count == 5
    assert regime.minimum_coverage == 0.60
    assert dict(regime.weights) == {
        "benchmark_trend": 30.0,
        "positive_sector_diffusion": 25.0,
        "flow_diffusion": 20.0,
        "liquidity_diffusion": 10.0,
        "non_risk_sector_share": 15.0,
    }
    assert (regime.risk_on_minimum, regime.selective_minimum, regime.defensive_minimum) == (
        75.0,
        55.0,
        35.0,
    )
    assert dict(position.total_ranges) == {
        "risk_on": (60.0, 80.0),
        "selective": (35.0, 60.0),
        "defensive": (10.0, 35.0),
        "risk_off": (0.0, 15.0),
        "insufficient_data": (0.0, 10.0),
    }
    assert position.minimum_sector_confidence == 0.60
    assert position.maximum_suggested_sectors == 3
    assert position.maximum_sector_pct == 15.0
    assert position.maximum_etf_pct == 15.0
    assert position.correlation_threshold == 0.80
    assert position.maximum_correlated_pct == 25.0
    assert position.correlation_sessions == 60
    with pytest.raises(TypeError):
        etf.component_weights["liquidity"] = 0.0


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (
            EtfPolicyConfig,
            {
                "component_weights": {
                    "liquidity": 35.0,
                    "trend": 25.0,
                    "tracking_quality": 20.0,
                    "cost": 10.0,
                    "size": 9.0,
                }
            },
        ),
        (
            RegimeConfig,
            {
                "weights": {
                    "benchmark_trend": 30.0,
                    "positive_sector_diffusion": 25.0,
                    "flow_diffusion": 20.0,
                    "liquidity_diffusion": 10.0,
                    "non_risk_sector_share": 14.0,
                }
            },
        ),
        (RegimeConfig, {"risk_on_minimum": 55.0}),
        (EtfPolicyConfig, {"total_budget_seconds": 91}),
        (PositionPolicyConfig, {"total_ranges": {"risk_on": (81.0, 80.0)}}),
        (PositionPolicyConfig, {"correlation_threshold": 1.1}),
    ],
)
def test_policy_config_rejects_invalid_contract_values(factory: type, kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        factory(**kwargs)
