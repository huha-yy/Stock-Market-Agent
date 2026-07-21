from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from src.market_radar.models import (
    RadarRunSnapshot,
    SectorDefinition,
    aggregate_run_quality,
)
from src.market_radar.providers import MarketRadarProvider
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.universe import UniverseLoader


_CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _merge_discovered_sectors(
    configured_history: list[SectorDefinition],
    discovered_sectors: list[SectorDefinition],
) -> list[SectorDefinition]:
    configured_by_id: dict[str, list[SectorDefinition]] = {}
    for configured in configured_history:
        configured_by_id.setdefault(configured.sector_id, []).append(configured)

    merged: list[SectorDefinition] = []
    for discovered in discovered_sectors:
        intervals = configured_by_id.get(discovered.sector_id, [])
        if any(
            configured.effective_from <= discovered.effective_from
            and (
                configured.effective_to is None
                or configured.effective_to >= discovered.effective_from
            )
            for configured in intervals
        ):
            continue

        future_starts = [
            configured.effective_from
            for configured in intervals
            if configured.effective_from > discovered.effective_from
        ]
        if not future_starts:
            merged.append(discovered)
            continue

        boundary = min(future_starts) - timedelta(days=1)
        if (
            discovered.effective_to is not None
            and discovered.effective_to <= boundary
        ):
            merged.append(discovered)
            continue

        payload = discovered.model_dump(mode="python")
        payload["effective_to"] = boundary
        merged.append(SectorDefinition.model_validate(payload))
    return merged


class MarketRadarService:
    def __init__(
        self,
        *,
        universe_loader: UniverseLoader,
        provider: MarketRadarProvider,
        repository: MarketRadarRepository | None,
        ranking_config: RankingConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.universe_loader = universe_loader
        self.provider = provider
        self.repository = repository
        self.ranking_config = ranking_config
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        market: str = "cn",
        as_of: datetime | None = None,
        trigger: Literal["manual", "replay"] = "manual",
        persist: bool = True,
    ) -> RadarRunSnapshot:
        if market != "cn":
            raise ValueError("Market Radar Phase 1 supports market=cn only")
        repository = self.repository
        if persist and repository is None:
            raise ValueError("repository is required when persist=True")
        requested_as_of = as_of if as_of is not None else self.clock()
        if requested_as_of.tzinfo is None or requested_as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        effective_as_of = requested_as_of.astimezone(timezone.utc)

        market_date = effective_as_of.astimezone(_CN_MARKET_TIMEZONE).date()
        universe, configured_history = self.universe_loader.load_with_history(
            market_date
        )
        batch = self.provider.fetch(market, effective_as_of, universe)
        sectors = score_sectors(batch.observations, self.ranking_config)
        snapshot = RadarRunSnapshot(
            run_key=(
                f"{market}:"
                f"{effective_as_of.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}:"
                f"{trigger}"
            ),
            market="cn",
            trigger=trigger,
            as_of=effective_as_of,
            quality=aggregate_run_quality(
                item.quality for item in batch.observations
            ),
            scoring_version=self.ranking_config.scoring_version,
            sectors=sectors,
            provider_trace=batch.trace,
        )
        if persist:
            combined_universe = [
                *configured_history,
                *_merge_discovered_sectors(
                    configured_history,
                    batch.discovered_sectors,
                ),
            ]
            run_id = repository.save_run_with_universe(
                sorted(
                    combined_universe,
                    key=lambda item: (
                        item.kind,
                        item.sector_id,
                        item.effective_from,
                    ),
                ),
                snapshot,
            )
            stored_snapshot = repository.get_run(run_id)
            if stored_snapshot is None:
                raise RuntimeError(f"Persisted Market Radar run {run_id} was not found")
            return stored_snapshot
        return snapshot
