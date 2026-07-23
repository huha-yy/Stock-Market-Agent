from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from src.market_radar.models import (
    EtfObservation,
    RadarRunSnapshot,
    SectorObservation,
    aggregate_run_quality,
)
from src.market_radar.etf_selection import select_etfs
from src.market_radar.policy_config import (
    EtfPolicyConfig,
    PositionPolicyConfig,
    RegimeConfig,
)
from src.market_radar.position_policy import build_position_plan
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.regime import assess_market_regime
from src.storage import RadarRunRecord

if TYPE_CHECKING:
    from src.market_radar.repository import MarketRadarRepository


class ReplayFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    observations: tuple[SectorObservation, ...]
    etf_observations: tuple[EtfObservation, ...] = ()


class MarketRadarReplayEngine:
    def __init__(
        self,
        ranking_config: RankingConfig,
        etf_policy_config: EtfPolicyConfig | None = None,
        regime_config: RegimeConfig | None = None,
        position_policy_config: PositionPolicyConfig | None = None,
    ) -> None:
        self.ranking_config = ranking_config
        self.etf_policy_config = etf_policy_config or EtfPolicyConfig()
        self.regime_config = regime_config or RegimeConfig()
        self.position_policy_config = (
            position_policy_config or PositionPolicyConfig()
        )

    def _apply_phase2b(
        self,
        snapshot: RadarRunSnapshot,
        etf_observations: tuple[EtfObservation, ...],
    ) -> RadarRunSnapshot:
        anchor = snapshot.as_of.astimezone(timezone.utc)
        if any(
            item.observed_at.astimezone(timezone.utc) > anchor
            for item in etf_observations
        ):
            raise ValueError("future ETF observation is not allowed in replay")
        etfs = select_etfs(etf_observations, self.etf_policy_config)
        regime = assess_market_regime(
            snapshot.sectors,
            self.regime_config,
            snapshot.as_of,
        )
        position_plan = build_position_plan(
            snapshot.sectors,
            etfs,
            regime,
            self.position_policy_config,
        )
        payload = snapshot.model_dump(mode="json")
        payload.update(etfs=etfs, regime=regime, position_plan=position_plan)
        return RadarRunSnapshot.model_validate(payload)

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
            snapshot = RadarRunSnapshot(
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
            if frame.etf_observations:
                snapshot = self._apply_phase2b(
                    snapshot,
                    frame.etf_observations,
                )
            snapshots.append(snapshot)
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
        replayed = self.replay(
            [ReplayFrame(as_of=stored.as_of, observations=observations)]
        )[0]
        with repository.db.get_session() as session:
            run_id = session.execute(
                select(RadarRunRecord.id).where(
                    RadarRunRecord.run_key == run_key
                )
            ).scalar_one()
        (
            etf_observations,
            stored_etfs,
            stored_regime,
            stored_position_plan,
        ) = repository.load_phase2b_evidence(int(run_id))
        has_phase2b = bool(etf_observations or stored_etfs) or (
            stored_regime is not None or stored_position_plan is not None
        )
        if not has_phase2b:
            return replayed
        recomputed = self._apply_phase2b(replayed, etf_observations)
        if (
            recomputed.etfs != stored_etfs
            or recomputed.regime != stored_regime
            or recomputed.position_plan != stored_position_plan
        ):
            raise ValueError("stored Phase 2B output semantic mismatch")
        return recomputed
