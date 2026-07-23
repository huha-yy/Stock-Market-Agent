from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, Literal, Mapping, TypeVar

from pydantic import Field, field_validator, model_validator

from src.market_radar.models import FrozenModel


CapabilityStatus = Literal["ok", "partial", "stale", "unavailable"]
BarStatus = Literal["provisional", "finalized"]


def _require_timezone(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class BoardBar(FrozenModel):
    data_date: date
    close: float = Field(allow_inf_nan=False)
    traded_amount: float = Field(ge=0, allow_inf_nan=False)


class BoardBarSeries(FrozenModel):
    code: str = Field(min_length=1)
    bars: tuple[BoardBar, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_strictly_increasing_dates(self) -> "BoardBarSeries":
        dates = tuple(bar.data_date for bar in self.bars)
        if any(previous >= current for previous, current in zip(dates, dates[1:])):
            raise ValueError("bars must have strictly increasing data_date values")
        return self


class BoardFlow(FrozenModel):
    data_date: date
    net_main_inflow: float = Field(allow_inf_nan=False)
    traded_amount: float = Field(ge=0, allow_inf_nan=False)


class BoardFlowSeries(FrozenModel):
    code: str = Field(min_length=1)
    flows: tuple[BoardFlow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_strictly_increasing_dates(self) -> "BoardFlowSeries":
        dates = tuple(flow.data_date for flow in self.flows)
        if any(previous >= current for previous, current in zip(dates, dates[1:])):
            raise ValueError("flows must have strictly increasing data_date values")
        return self


class ConstituentMembership(FrozenModel):
    codes: tuple[str, ...] = Field(min_length=1)
    data_date: date | None

    @field_validator("codes")
    @classmethod
    def require_unique_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not code.strip() for code in value):
            raise ValueError("codes must not contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("codes must not contain duplicates")
        return value


class ConstituentQuote(FrozenModel):
    code: str = Field(min_length=1)
    current_price: float = Field(ge=0, allow_inf_nan=False)
    previous_close: float = Field(gt=0, allow_inf_nan=False)
    traded_amount: float = Field(ge=0, allow_inf_nan=False)
    quoted_at: datetime

    @field_validator("quoted_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "quoted_at")


class ConstituentQuoteBatch(FrozenModel):
    quotes: tuple[ConstituentQuote, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_codes(self) -> "ConstituentQuoteBatch":
        codes = tuple(quote.code for quote in self.quotes)
        if len(codes) != len(set(codes)):
            raise ValueError("quotes must not contain duplicate codes")
        return self


class EtfBar(FrozenModel):
    data_date: date
    close: float = Field(gt=0, allow_inf_nan=False)
    traded_amount: float = Field(ge=0, allow_inf_nan=False)


class EtfCapabilityData(FrozenModel):
    code: str = Field(pattern=r"^\d{6}$")
    bars: tuple[EtfBar, ...] = Field(min_length=1)
    quoted_at: datetime | None = None
    current_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    current_traded_amount: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    active: bool | None = None
    suspended: bool | None = None
    bid_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ask_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    nav: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    tracking_error_pct: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    tracking_difference_pct: float | None = Field(default=None, allow_inf_nan=False)
    annual_fee_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    net_assets_cny: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    shares: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("quoted_at")
    @classmethod
    def require_quote_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone(value, "quoted_at")

    @model_validator(mode="after")
    def validate_etf_evidence(self) -> "EtfCapabilityData":
        dates = tuple(bar.data_date for bar in self.bars)
        if any(previous >= current for previous, current in zip(dates, dates[1:])):
            raise ValueError("bars must have strictly increasing data_date values")
        quote_fields = (
            self.current_price,
            self.current_traded_amount,
            self.active,
            self.suspended,
            self.bid_price,
            self.ask_price,
            self.nav,
        )
        if any(value is not None for value in quote_fields) and self.quoted_at is None:
            raise ValueError("current quote facts require authoritative quoted_at")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise ValueError("bid_price must not exceed ask_price")
        return self


T = TypeVar("T", bound=FrozenModel)


class CapabilityResult(FrozenModel, Generic[T]):
    capability: str = Field(min_length=1)
    status: CapabilityStatus
    data: T | None
    source: str = Field(min_length=1)
    observed_at: datetime
    data_date: date | None
    bar_status: BarStatus | None
    freshness_seconds: int = Field(ge=0)
    trace: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "observed_at")


class MarketRadarEnrichmentConfig(FrozenModel):
    candidate_limit: int = Field(default=60, ge=1, le=200)
    total_budget_seconds: int = Field(default=180, ge=10, le=900)
    max_concurrency: int = Field(default=6, ge=1, le=16)
    constituent_min_count: int = 5
    constituent_coverage_ratio: float = 0.80
    price_divergence_threshold_pct: float = 1.0
    flow_divergence_threshold_pct: float = 0.1
    default_benchmark_code: str = "000985"

    @classmethod
    def from_runtime(
        cls, limit: int, budget_seconds: int, max_concurrency: int
    ) -> "MarketRadarEnrichmentConfig":
        return cls(
            candidate_limit=limit,
            total_budget_seconds=budget_seconds,
            max_concurrency=max_concurrency,
        )
