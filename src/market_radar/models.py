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


class RadarRunSnapshot(FrozenModel):
    run_key: str
    market: MarketRadarMarket
    trigger: Literal["manual", "replay"]
    as_of: datetime
    quality: DataQuality
    scoring_version: Literal["cn-v1"]
    sectors: tuple[SectorScore, ...]
    provider_trace: tuple[Mapping[str, Any], ...]

    @model_validator(mode="after")
    def require_unique_sectors(self) -> "RadarRunSnapshot":
        sector_ids = [sector.sector_id for sector in self.sectors]
        if len(sector_ids) != len(set(sector_ids)):
            raise ValueError("duplicate sector_id in run snapshot")
        return self
