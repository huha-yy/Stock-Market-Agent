from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    field_validator,
    model_serializer,
    model_validator,
)


MarketRadarMarket = Literal["cn"]
SectorKind = Literal["industry", "concept"]
DataQuality = Literal["complete", "partial", "stale", "unavailable"]
SectorState = Literal[
    "leading",
    "improving",
    "neutral",
    "weakening",
    "avoid",
    "insufficient_data",
]
EtfSelectionStatus = Literal[
    "best_supported", "candidate", "rejected", "insufficient_data"
]
MarketRegime = Literal[
    "risk_on", "selective", "defensive", "risk_off", "insufficient_data"
]


def aggregate_run_quality(values: Iterable[DataQuality]) -> DataQuality:
    qualities = tuple(values)
    if not qualities or all(value == "unavailable" for value in qualities):
        return "unavailable"
    if any(value == "stale" for value in qualities):
        return "stale"
    if any(value in {"partial", "unavailable"} for value in qualities):
        return "partial"
    return "complete"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw_for_serialization(value: Any, mode: str) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode=mode)
    if isinstance(value, Mapping):
        return {
            key: _thaw_for_serialization(item, mode) for key, item in value.items()
        }
    if isinstance(value, (tuple, frozenset)):
        return [_thaw_for_serialization(item, mode) for item in value]
    if mode == "json" and isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def freeze_public_containers(self) -> "FrozenModel":
        for field_name in type(self).model_fields:
            object.__setattr__(self, field_name, _freeze(getattr(self, field_name)))
        return self

    @model_serializer(mode="plain")
    def serialize_public_containers(self, info: SerializationInfo) -> dict[str, Any]:
        return {
            field_name: _thaw_for_serialization(getattr(self, field_name), info.mode)
            for field_name in type(self).model_fields
        }


class EtfDefinition(FrozenModel):
    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    sector_id: str = Field(min_length=3)
    benchmark_code: str | None = None
    effective_from: date
    effective_to: date | None = None


class SectorDefinition(FrozenModel):
    sector_id: str = Field(min_length=3)
    market: MarketRadarMarket = "cn"
    kind: SectorKind
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = Field(default_factory=tuple)
    benchmark_code: str | None = None
    etfs: tuple[EtfDefinition, ...] = Field(default_factory=tuple)
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def validate_effective_range(self) -> "SectorDefinition":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if any(etf.sector_id != self.sector_id for etf in self.etfs):
            raise ValueError("ETF sector_id must match parent sector_id")
        return self


class SectorObservation(FrozenModel):
    tracked_metric_fields: ClassVar[tuple[str, ...]] = (
        "return_1d_pct",
        "return_5d_pct",
        "return_20d_pct",
        "benchmark_return_20d_pct",
        "capital_flow_1d",
        "capital_flow_5d",
        "capital_flow_20d",
        "turnover_ratio_20d",
        "up_count",
        "down_count",
        "flat_count",
        "volatility_ratio_20d",
        "distance_ma20_pct",
        "price_flow_divergence",
        "concentration_ratio",
        "catalyst_score",
    )

    sector_id: str = Field(min_length=3)
    market: MarketRadarMarket = "cn"
    kind: SectorKind
    name: str = Field(min_length=1)
    observed_at: datetime
    source: str = Field(min_length=1)
    freshness_seconds: int = Field(ge=0)
    quality: DataQuality
    return_1d_pct: float | None = None
    return_5d_pct: float | None = None
    return_20d_pct: float | None = None
    benchmark_return_20d_pct: float | None = None
    capital_flow_1d: float | None = None
    capital_flow_5d: float | None = None
    capital_flow_20d: float | None = None
    turnover_ratio_20d: float | None = Field(default=None, ge=0)
    up_count: int | None = Field(default=None, ge=0)
    down_count: int | None = Field(default=None, ge=0)
    flat_count: int | None = Field(default=None, ge=0)
    volatility_ratio_20d: float | None = Field(default=None, ge=0)
    distance_ma20_pct: float | None = None
    price_flow_divergence: bool | None = None
    concentration_ratio: float | None = Field(default=None, ge=0, le=1)
    catalyst_score: float | None = Field(default=None, ge=0, le=1)
    missing_fields: tuple[str, ...]
    raw_reference: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_missing_field_provenance(self) -> "SectorObservation":
        declared_fields = list(self.missing_fields)
        declared_set = set(declared_fields)
        if len(declared_fields) != len(declared_set):
            raise ValueError("missing_fields must not contain duplicates")

        tracked_fields = set(self.tracked_metric_fields)
        unknown_fields = declared_set - tracked_fields
        if unknown_fields:
            raise ValueError("missing_fields contains unknown metric names")

        absent_fields = {
            field_name
            for field_name in self.tracked_metric_fields
            if getattr(self, field_name) is None
        }
        if declared_set != absent_fields:
            raise ValueError(
                "missing_fields must exactly list tracked metrics whose values are None"
            )
        return self


class FactorBreakdown(FrozenModel):
    trend_momentum: float = Field(ge=0, le=25)
    relative_strength: float = Field(ge=0, le=20)
    capital_flow: float = Field(ge=0, le=20)
    breadth: float = Field(ge=0, le=15)
    liquidity_expansion: float = Field(ge=0, le=10)
    catalyst: float = Field(ge=0, le=10)


class SectorScore(FrozenModel):
    sector_id: str
    name: str
    kind: SectorKind
    scoring_version: Literal["cn-v1"]
    gross_score: float = Field(ge=0, le=100)
    risk_deduction: float = Field(ge=0, le=30)
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    state: SectorState
    factors: FactorBreakdown | Mapping[str, Any]
    risk_reasons: tuple[str, ...]
    missing_fields: tuple[str, ...]
    source: str
    observed_at: datetime
    quality: DataQuality
    observation: Mapping[str, Any] = Field(default_factory=dict)


class EtfObservation(FrozenModel):
    tracked_metric_fields: ClassVar[tuple[str, ...]] = (
        "data_date",
        "bar_status",
        "active",
        "finalized_session_count",
        "suspended",
        "current_price",
        "current_traded_amount",
        "average_traded_amount_20d",
        "spread_bps",
        "premium_discount_pct",
        "return_20d_pct",
        "return_60d_pct",
        "daily_return_dates_60",
        "daily_returns_60",
        "tracking_error_pct",
        "tracking_difference_pct",
        "annual_fee_pct",
        "size_cny",
        "liquidity_stability",
    )

    market: MarketRadarMarket = "cn"
    sector_id: str
    code: str = Field(pattern=r"^\d{6}$")
    name: str
    observed_at: datetime
    data_date: date | None
    bar_status: Literal["provisional", "finalized"] | None
    source: str
    quality: DataQuality
    freshness_seconds: int = Field(ge=0)
    mapping_effective_from: date
    mapping_effective_to: date | None = None
    benchmark_code: str | None = None
    active: bool | None = None
    finalized_session_count: int | None = Field(default=None, ge=0)
    suspended: bool | None = None
    current_price: float | None = Field(default=None, allow_inf_nan=False)
    current_traded_amount: float | None = Field(default=None, allow_inf_nan=False)
    average_traded_amount_20d: float | None = Field(default=None, allow_inf_nan=False)
    spread_bps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    premium_discount_pct: float | None = Field(default=None, allow_inf_nan=False)
    return_20d_pct: float | None = Field(default=None, allow_inf_nan=False)
    return_60d_pct: float | None = Field(default=None, allow_inf_nan=False)
    daily_return_dates_60: tuple[date, ...] = ()
    daily_returns_60: tuple[float, ...] = ()
    tracking_error_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tracking_difference_pct: float | None = Field(default=None, allow_inf_nan=False)
    annual_fee_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    size_cny: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    liquidity_stability: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    missing_fields: tuple[str, ...]
    raw_reference: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @field_validator("daily_returns_60")
    @classmethod
    def require_finite_daily_returns(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not float(item) == float(item) or abs(float(item)) == float("inf") for item in value):
            raise ValueError("daily_returns_60 must contain finite values")
        return value

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> "EtfObservation":
        if (
            self.mapping_effective_to is not None
            and self.mapping_effective_to < self.mapping_effective_from
        ):
            raise ValueError("mapping_effective_to must not precede mapping_effective_from")

        declared_fields = list(self.missing_fields)
        declared_set = set(declared_fields)
        if len(declared_fields) != len(declared_set):
            raise ValueError("missing_fields must not contain duplicates")
        unknown_fields = declared_set - set(self.tracked_metric_fields)
        if unknown_fields:
            raise ValueError("missing_fields contains unknown metric names")
        absent_fields = {
            field_name
            for field_name in self.tracked_metric_fields
            if not getattr(self, field_name)
            if field_name in {"daily_return_dates_60", "daily_returns_60"}
        }
        absent_fields.update(
            field_name
            for field_name in self.tracked_metric_fields
            if field_name not in {"daily_return_dates_60", "daily_returns_60"}
            and getattr(self, field_name) is None
        )
        if declared_set != absent_fields:
            raise ValueError(
                "missing_fields must exactly list tracked metrics whose values are missing"
            )

        dates_present = bool(self.daily_return_dates_60)
        returns_present = bool(self.daily_returns_60)
        if dates_present != returns_present:
            raise ValueError("daily return dates and returns must be present together")
        if dates_present:
            if len(self.daily_return_dates_60) != 60 or len(self.daily_returns_60) != 60:
                raise ValueError("daily return series must contain exactly 60 values")
            if any(
                previous >= current
                for previous, current in zip(
                    self.daily_return_dates_60, self.daily_return_dates_60[1:]
                )
            ):
                raise ValueError("daily return dates must be strictly increasing")
        return self


class EtfComponentScores(FrozenModel):
    liquidity: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    trend: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    tracking_quality: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    cost: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    size: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class EtfSelection(FrozenModel):
    sector_id: str
    code: str
    name: str
    status: EtfSelectionStatus
    eligible: bool
    rank: int | None = Field(default=None, ge=1)
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    components: EtfComponentScores
    effective_weights: Mapping[str, float] = Field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    observation: EtfObservation

    @field_validator("effective_weights")
    @classmethod
    def require_finite_weights(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if any(not 0 <= weight <= 100 for weight in value.values()):
            raise ValueError("effective_weights must be finite percentages in [0, 100]")
        return value

    @model_validator(mode="after")
    def validate_observation_identity(self) -> "EtfSelection":
        if self.sector_id != self.observation.sector_id or self.code != self.observation.code:
            raise ValueError("selection identity must match observation identity")
        return self


class RegimeComponents(FrozenModel):
    benchmark_trend: float = Field(ge=0, le=100, allow_inf_nan=False)
    positive_sector_diffusion: float = Field(ge=0, le=100, allow_inf_nan=False)
    flow_diffusion: float = Field(ge=0, le=100, allow_inf_nan=False)
    liquidity_diffusion: float = Field(ge=0, le=100, allow_inf_nan=False)
    non_risk_sector_share: float = Field(ge=0, le=100, allow_inf_nan=False)


class MarketRegimeAssessment(FrozenModel):
    regime_version: Literal["cn-regime-v1"] = "cn-regime-v1"
    as_of: datetime
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    regime: MarketRegime
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    components: RegimeComponents | None = None
    cohort_sector_ids: tuple[str, ...] = ()
    excluded_sector_reasons: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value


class PositionSuggestion(FrozenModel):
    sector_id: str
    sector_name: str
    sector_rank: int = Field(ge=1)
    etf_code: str
    etf_status: Literal["best_supported", "candidate"]
    minimum_pct: None = None
    sector_cap_pct: float = Field(ge=0, le=15, allow_inf_nan=False)
    etf_cap_pct: float = Field(ge=0, le=15, allow_inf_nan=False)
    joint_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    invalidation_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_caps(self) -> "PositionSuggestion":
        if self.etf_cap_pct > self.sector_cap_pct:
            raise ValueError("etf_cap_pct must not exceed sector_cap_pct")
        return self


class CorrelationGroup(FrozenModel):
    etf_codes: tuple[str, ...] = Field(min_length=2)
    maximum_total_pct: float = Field(default=25, ge=0, le=25, allow_inf_nan=False)

    @field_validator("etf_codes")
    @classmethod
    def require_unique_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("etf_codes must not contain duplicates")
        return value


class PositionPlan(FrozenModel):
    policy_version: Literal["cn-position-v1"] = "cn-position-v1"
    as_of: datetime
    regime: MarketRegime
    total_position_min_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    total_position_max_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    suggestions: tuple[PositionSuggestion, ...] = Field(default=(), max_length=3)
    correlation_groups: tuple[CorrelationGroup, ...] = ()
    correlation_coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason_codes: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_position_range(self) -> "PositionPlan":
        if self.total_position_min_pct > self.total_position_max_pct:
            raise ValueError("total_position_min_pct must not exceed total_position_max_pct")
        return self


class RadarRunSnapshot(FrozenModel):
    run_key: str
    market: MarketRadarMarket
    trigger: Literal["manual", "schedule", "replay"]
    as_of: datetime
    quality: DataQuality
    scoring_version: Literal["cn-v1"]
    sectors: tuple[SectorScore, ...]
    provider_trace: tuple[Mapping[str, Any], ...]
    etfs: tuple[EtfSelection, ...] = ()
    regime: MarketRegimeAssessment | None = None
    position_plan: PositionPlan | None = None

    @model_validator(mode="after")
    def require_unique_sectors(self) -> "RadarRunSnapshot":
        sector_ids = [sector.sector_id for sector in self.sectors]
        if len(sector_ids) != len(set(sector_ids)):
            raise ValueError("duplicate sector_id in run snapshot")
        etf_ids = [(selection.sector_id, selection.code) for selection in self.etfs]
        if len(etf_ids) != len(set(etf_ids)):
            raise ValueError("duplicate ETF identity in run snapshot")
        etf_codes = [selection.code for selection in self.etfs]
        if len(etf_codes) != len(set(etf_codes)):
            raise ValueError("duplicate ETF code in run snapshot")
        if any(selection.observation.observed_at != self.as_of for selection in self.etfs):
            raise ValueError("ETF observation timestamps must match run as_of")
        best_supported_sectors = [
            selection.sector_id
            for selection in self.etfs
            if selection.status == "best_supported"
        ]
        if len(best_supported_sectors) != len(set(best_supported_sectors)):
            raise ValueError("at most one best_supported ETF is allowed per sector")
        if self.regime is not None and self.regime.as_of != self.as_of:
            raise ValueError("regime timestamp must match run as_of")
        if self.position_plan is not None:
            if self.position_plan.as_of != self.as_of:
                raise ValueError("position plan timestamp must match run as_of")
            if self.regime is None:
                raise ValueError("position plan requires a regime assessment")
            if self.position_plan.regime != self.regime.regime:
                raise ValueError("position plan regime must match regime assessment")
            selections_by_identity = {
                (selection.sector_id, selection.code): selection
                for selection in self.etfs
            }
            suggested_codes = set()
            for suggestion in self.position_plan.suggestions:
                identity = (suggestion.sector_id, suggestion.etf_code)
                selection = selections_by_identity.get(identity)
                if selection is None:
                    raise ValueError(
                        "position suggestion ETF identity must match a run selection"
                    )
                if selection.status != suggestion.etf_status:
                    raise ValueError(
                        "position suggestion ETF status must match the run selection"
                    )
                suggested_codes.add(suggestion.etf_code)
            if any(
                code not in suggested_codes
                for group in self.position_plan.correlation_groups
                for code in group.etf_codes
            ):
                raise ValueError(
                    "correlation group ETF codes must match position suggestions"
                )
        return self
