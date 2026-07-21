from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.market_radar as market_radar
from src.config import Config
from src.storage import DatabaseManager


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
PUBLIC_API = {
    "DataQuality",
    "EtfDefinition",
    "FactorBreakdown",
    "LegacyRankingProvider",
    "MarketRadarProvider",
    "MarketRadarReplayEngine",
    "MarketRadarRepository",
    "MarketRadarService",
    "ProviderBatch",
    "RadarRunSnapshot",
    "RankingConfig",
    "ReplayFrame",
    "SectorDefinition",
    "SectorObservation",
    "SectorScore",
    "SectorState",
    "UniverseLoader",
    "score_sectors",
}


class OfflineManager:
    def get_sector_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [{"name": "半导体", "change_pct": 2.5}],
            [],
            [{"provider": "OfflineFixture", "result": "ok", "duration_ms": 0}],
            "",
        )

    def get_concept_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [],
            [],
            [{"provider": "OfflineFixture", "result": "empty", "duration_ms": 0}],
            "",
        )


@pytest.fixture()
def isolated_db(tmp_path):
    old_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "phase_one_integration.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_path


def test_phase_one_public_api_is_explicit_and_complete() -> None:
    assert set(market_radar.__all__) == PUBLIC_API
    assert all(hasattr(market_radar, name) for name in PUBLIC_API)


def test_offline_manual_run_persists_and_replays_same_scoring_path(
    isolated_db,
) -> None:
    config = market_radar.RankingConfig()
    repository = market_radar.MarketRadarRepository(isolated_db)
    service = market_radar.MarketRadarService(
        universe_loader=market_radar.UniverseLoader(
            ROOT / "src/data/market_radar/a_share_etfs.yaml"
        ),
        provider=market_radar.LegacyRankingProvider(OfflineManager(), limit=1000),
        repository=repository,
        ranking_config=config,
        clock=lambda: NOW,
    )

    snapshot = service.run(market="cn", persist=True)

    assert snapshot == repository.get_latest_run("cn")
    assert snapshot.run_key == "cn:20260721T060000Z:manual"
    assert snapshot.trigger == "manual"
    assert snapshot.provider_trace[0]["provider"] == "OfflineFixture"
    assert snapshot.sectors[0].source == "OfflineFixture"
    assert snapshot.sectors[0].observation["raw_reference"] == {
        "name": "半导体",
        "change_pct": 2.5,
    }
    assert any(
        etf.code == "512480"
        for sector in repository.list_universe(NOW.date())
        for etf in sector.etfs
    )

    observation = market_radar.SectorObservation.model_validate(
        snapshot.sectors[0].observation
    )
    replay = market_radar.MarketRadarReplayEngine(config).replay(
        [market_radar.ReplayFrame(as_of=NOW, observations=[observation])]
    )[0]

    assert replay.trigger == "replay"
    assert replay.sectors == snapshot.sectors
