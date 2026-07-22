from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from src.market_radar.models import (
    RadarRunSnapshot,
    SectorObservation,
    aggregate_run_quality,
)
from src.market_radar.ranking import RankingConfig, score_sectors

if TYPE_CHECKING:
    from src.market_radar.repository import MarketRadarRepository


class ReplayFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    observations: tuple[SectorObservation, ...]


class MarketRadarReplayEngine:
    def __init__(self, ranking_config: RankingConfig) -> None:
        self.ranking_config = ranking_config

    def replay(self, frames: list[ReplayFrame]) -> list[RadarRunSnapshot]:
        snapshots: list[RadarRunSnapshot] = []
        previous: datetime | None = None
        for frame in frames:
            if frame.as_of.tzinfo is None or frame.as_of.utcoffset() is None:
                raise ValueError("replay as_of must be timezone-aware")
            frame_as_of_utc = frame.as_of.astimezone(timezone.utc)
            if previous is not None and frame_as_of_utc < previous:
                raise ValueError("replay frames must be in chronological order")
            for item in frame.observations:
                observed_at_utc = item.observed_at.astimezone(timezone.utc)
                if observed_at_utc > frame_as_of_utc:
                    raise ValueError("future observation is not allowed in replay")

            sectors = score_sectors(list(frame.observations), self.ranking_config)
            quality = aggregate_run_quality(
                item.quality for item in frame.observations
            )
            snapshots.append(
                RadarRunSnapshot(
                    run_key=(
                        "cn:"
                        f"{frame_as_of_utc:%Y%m%dT%H%M%SZ}:"
                        "replay"
                    ),
                    market="cn",
                    trigger="replay",
                    as_of=frame.as_of,
                    quality=quality,
                    scoring_version=self.ranking_config.scoring_version,
                    sectors=sectors,
                    provider_trace=[{"source": "replay_frame", "result": "ok"}],
                )
            )
            previous = frame_as_of_utc
        return snapshots

    def replay_persisted_run(
        self,
        repository: MarketRadarRepository,
        run_key: str,
    ) -> RadarRunSnapshot:
        stored = repository.get_run_by_key(run_key)
        if stored is None:
            raise ValueError(f"stored Market Radar run not found: {run_key}")
        repository.resolve_snapshot_constituent_evidence(stored)
        observations = tuple(
            SectorObservation.model_validate(sector.observation)
            for sector in stored.sectors
        )
        return self.replay(
            [ReplayFrame(as_of=stored.as_of, observations=observations)]
        )[0]
