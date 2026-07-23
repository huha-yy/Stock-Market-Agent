"""Read-only Market Radar API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.market_radar.models import (
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    RadarRunSnapshot,
    SectorScore,
)


class MarketRadarLatestResponse(BaseModel):
    available: bool
    run: RadarRunSnapshot | None = None


class MarketRadarSectorListItem(BaseModel):
    rank: int = Field(ge=1)
    sector: SectorScore


class MarketRadarSectorListResponse(BaseModel):
    available: bool
    run_key: str | None = None
    as_of: datetime | None = None
    items: list[MarketRadarSectorListItem] = Field(default_factory=list)
    total: int = Field(ge=0)


class MarketRadarSectorDetailResponse(BaseModel):
    run_key: str
    as_of: datetime
    rank: int = Field(ge=1)
    sector: SectorScore
    etfs: list[EtfSelection] = Field(default_factory=list)
    position_suggestion: PositionSuggestion | None = None
    regime: MarketRegimeAssessment | None = None
    position_plan: PositionPlan | None = None
