from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal

from src.market_radar.models import DataQuality, RadarRunSnapshot
from src.market_radar.providers import MarketRadarProvider
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.universe import UniverseLoader


class MarketRadarService:
    def __init__(
        self,
        *,
        universe_loader: UniverseLoader,
        provider: MarketRadarProvider,
        repository: MarketRadarRepository,
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
        effective_as_of = as_of or self.clock()
        if effective_as_of.tzinfo is None or effective_as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

        universe = self.universe_loader.load(effective_as_of.date())
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
            quality=self._run_quality([item.quality for item in batch.observations]),
            scoring_version=self.ranking_config.scoring_version,
            sectors=sectors,
            provider_trace=batch.trace,
        )
        if persist:
            combined_universe = {
                item.sector_id: item for item in batch.discovered_sectors
            }
            combined_universe.update({item.sector_id: item for item in universe})
            self.repository.sync_universe(
                sorted(
                    combined_universe.values(),
                    key=lambda item: (item.kind, item.sector_id),
                )
            )
            self.repository.save_run(snapshot)
        return snapshot

    @staticmethod
    def _run_quality(values: list[DataQuality]) -> DataQuality:
        if not values or all(value == "unavailable" for value in values):
            return "unavailable"
        if any(value == "stale" for value in values):
            return "stale"
        if any(value in {"partial", "unavailable"} for value in values):
            return "partial"
        return "complete"
