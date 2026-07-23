from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, field_validator, model_validator

from src.market_radar.models import FrozenModel


ETF_COMPONENT_WEIGHTS = {
    "liquidity": 35.0,
    "trend": 25.0,
    "tracking_quality": 20.0,
    "cost": 10.0,
    "size": 10.0,
}
REGIME_WEIGHTS = {
    "benchmark_trend": 30.0,
    "positive_sector_diffusion": 25.0,
    "flow_diffusion": 20.0,
    "liquidity_diffusion": 10.0,
    "non_risk_sector_share": 15.0,
}
POSITION_TOTAL_RANGES = {
    "risk_on": (60.0, 80.0),
    "selective": (35.0, 60.0),
    "defensive": (10.0, 35.0),
    "risk_off": (0.0, 15.0),
    "insufficient_data": (0.0, 10.0),
}


def _validate_weights(
    value: Mapping[str, float], expected_keys: set[str], field_name: str
) -> Mapping[str, float]:
    if set(value) != expected_keys:
        raise ValueError(f"{field_name} must contain the approved keys")
    if any(not 0 <= item <= 100 for item in value.values()):
        raise ValueError(f"{field_name} must contain finite percentages in [0, 100]")
    if sum(value.values()) != 100:
        raise ValueError(f"{field_name} must sum to 100")
    return value


class EtfPolicyConfig(FrozenModel):
    policy_version: Literal["cn-etf-v1"] = "cn-etf-v1"
    candidate_limit: int = Field(default=30, ge=1, le=30)
    total_budget_seconds: int = Field(default=90, ge=10, le=90)
    max_concurrency: int = Field(default=6, ge=1, le=6)
    minimum_finalized_sessions: int = Field(default=60, ge=1)
    minimum_average_amount_cny: float = Field(default=10_000_000.0, gt=0, allow_inf_nan=False)
    stale_after_seconds: int = Field(default=2700, ge=0)
    maximum_spread_bps: float = Field(default=50.0, ge=0, allow_inf_nan=False)
    maximum_abs_premium_discount_pct: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    component_weights: Mapping[str, float] = ETF_COMPONENT_WEIGHTS

    @field_validator("component_weights")
    @classmethod
    def validate_component_weights(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return _validate_weights(value, set(ETF_COMPONENT_WEIGHTS), "component_weights")


class RegimeConfig(FrozenModel):
    regime_version: Literal["cn-regime-v1"] = "cn-regime-v1"
    default_benchmark_code: str = "000985"
    minimum_sector_count: int = Field(default=5, ge=1)
    minimum_coverage: float = Field(default=0.60, ge=0, le=1, allow_inf_nan=False)
    weights: Mapping[str, float] = REGIME_WEIGHTS
    risk_on_minimum: float = Field(default=75.0, ge=0, le=100, allow_inf_nan=False)
    selective_minimum: float = Field(default=55.0, ge=0, le=100, allow_inf_nan=False)
    defensive_minimum: float = Field(default=35.0, ge=0, le=100, allow_inf_nan=False)

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return _validate_weights(value, set(REGIME_WEIGHTS), "weights")

    @model_validator(mode="after")
    def validate_thresholds(self) -> "RegimeConfig":
        if not self.risk_on_minimum > self.selective_minimum > self.defensive_minimum:
            raise ValueError("regime thresholds must be strictly descending")
        return self


class PositionPolicyConfig(FrozenModel):
    policy_version: Literal["cn-position-v1"] = "cn-position-v1"
    total_ranges: Mapping[str, tuple[float, float]] = POSITION_TOTAL_RANGES
    minimum_sector_confidence: float = Field(default=0.60, ge=0, le=1, allow_inf_nan=False)
    maximum_suggested_sectors: int = Field(default=3, ge=1, le=3)
    maximum_sector_pct: float = Field(default=15.0, ge=0, le=15, allow_inf_nan=False)
    maximum_etf_pct: float = Field(default=15.0, ge=0, le=15, allow_inf_nan=False)
    correlation_threshold: float = Field(default=0.80, ge=0, le=1, allow_inf_nan=False)
    maximum_correlated_pct: float = Field(default=25.0, ge=0, le=25, allow_inf_nan=False)
    correlation_sessions: int = Field(default=60, ge=60, le=60)

    @field_validator("total_ranges")
    @classmethod
    def validate_total_ranges(
        cls, value: Mapping[str, tuple[float, float]]
    ) -> Mapping[str, tuple[float, float]]:
        if set(value) != set(POSITION_TOTAL_RANGES):
            raise ValueError("total_ranges must contain every market regime")
        for minimum, maximum in value.values():
            if not 0 <= minimum <= maximum <= 100:
                raise ValueError("total_ranges must contain ordered percentages in [0, 100]")
        return value

    @model_validator(mode="after")
    def validate_caps(self) -> "PositionPolicyConfig":
        if self.maximum_etf_pct > self.maximum_sector_pct:
            raise ValueError("maximum_etf_pct must not exceed maximum_sector_pct")
        return self
