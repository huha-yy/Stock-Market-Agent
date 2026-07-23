"""Read-only Market Radar endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Security
from fastapi.security import APIKeyCookie

from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.market_radar import (
    MarketRadarLatestResponse,
    MarketRadarSectorDetailResponse,
    MarketRadarSectorListItem,
    MarketRadarSectorListResponse,
)
from src.auth import COOKIE_NAME
from src.market_radar.models import RadarRunSnapshot
from src.market_radar.repository import MarketRadarRepository


logger = logging.getLogger(__name__)

admin_session_cookie = APIKeyCookie(
    name=COOKIE_NAME,
    scheme_name="AdminSessionCookie",
    auto_error=False,
)
router = APIRouter(dependencies=[Security(admin_session_cookie)])

AUTH_RESPONSE = {
    401: {
        "model": ErrorResponse,
        "description": "Admin session is required when authentication is enabled",
    },
}


def _latest_snapshot() -> RadarRunSnapshot | None:
    try:
        return MarketRadarRepository().get_latest_run("cn")
    except Exception as exc:
        logger.error("Read Market Radar snapshot failed: %s", exc, exc_info=True)
        raise api_error(
            500,
            "market_radar_read_failed",
            "Unable to read Market Radar data",
        ) from exc


@router.get(
    "/latest",
    response_model=MarketRadarLatestResponse,
    responses={**AUTH_RESPONSE, 500: {"model": ErrorResponse}},
    operation_id="getLatestMarketRadar",
    summary="Get the latest Market Radar snapshot",
)
def get_latest() -> MarketRadarLatestResponse:
    snapshot = _latest_snapshot()
    return MarketRadarLatestResponse(
        available=snapshot is not None,
        run=snapshot,
    )


@router.get(
    "/sectors",
    response_model=MarketRadarSectorListResponse,
    responses={**AUTH_RESPONSE, 500: {"model": ErrorResponse}},
    operation_id="listLatestMarketRadarSectors",
    summary="List sectors from the latest Market Radar snapshot",
)
def list_sectors() -> MarketRadarSectorListResponse:
    snapshot = _latest_snapshot()
    if snapshot is None:
        return MarketRadarSectorListResponse(
            available=False,
            items=[],
            total=0,
        )
    items = [
        MarketRadarSectorListItem(rank=rank, sector=sector)
        for rank, sector in enumerate(snapshot.sectors, start=1)
    ]
    return MarketRadarSectorListResponse(
        available=True,
        run_key=snapshot.run_key,
        as_of=snapshot.as_of,
        items=items,
        total=len(items),
    )


@router.get(
    "/sectors/{sector_id}",
    response_model=MarketRadarSectorDetailResponse,
    responses={
        **AUTH_RESPONSE,
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    operation_id="getLatestMarketRadarSector",
    summary="Get one sector from the latest Market Radar snapshot",
)
def get_sector(sector_id: str) -> MarketRadarSectorDetailResponse:
    snapshot = _latest_snapshot()
    if snapshot is None:
        raise api_error(
            404,
            "market_radar_run_not_found",
            "No Market Radar run is available",
        )

    match = next(
        (
            (rank, sector)
            for rank, sector in enumerate(snapshot.sectors, start=1)
            if sector.sector_id == sector_id
        ),
        None,
    )
    if match is None:
        raise api_error(
            404,
            "market_radar_sector_not_found",
            f"Market Radar sector not found: {sector_id}",
        )

    rank, sector = match
    position_suggestion = None
    if snapshot.position_plan is not None:
        position_suggestion = next(
            (
                suggestion
                for suggestion in snapshot.position_plan.suggestions
                if suggestion.sector_id == sector_id
            ),
            None,
        )
    return MarketRadarSectorDetailResponse(
        run_key=snapshot.run_key,
        as_of=snapshot.as_of,
        rank=rank,
        sector=sector,
        etfs=[item for item in snapshot.etfs if item.sector_id == sector_id],
        position_suggestion=position_suggestion,
        regime=snapshot.regime,
        position_plan=snapshot.position_plan,
    )
