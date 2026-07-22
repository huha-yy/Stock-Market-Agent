from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import src.market_radar as market_radar
from src.config import Config
from src.market_radar.capabilities import (
    BoardBar,
    BoardBarSeries,
    BoardFlow,
    BoardFlowSeries,
    CapabilityResult,
    ConstituentMembership,
    ConstituentQuote,
    ConstituentQuoteBatch,
)
from src.storage import DatabaseManager


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
PUBLIC_API = {
    "CandidateSelector",
    "DataQuality",
    "EnrichmentBatch",
    "EnrichmentCandidate",
    "EtfDefinition",
    "FactorBreakdown",
    "LegacyRankingProvider",
    "MarketRadarProvider",
    "MarketRadarEnricher",
    "MarketRadarEnrichmentConfig",
    "MarketRadarReplayEngine",
    "MarketRadarRepository",
    "MarketRadarService",
    "ProviderBatch",
    "ProviderCapabilityAdapter",
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


class OfflineDiscoveryManager:
    def get_sector_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [{"name": "Discovered Industry", "change_pct": 2.5}],
            [],
            [{"provider": "OfflineDiscovery", "result": "ok"}],
            "",
        )

    def get_concept_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [],
            [],
            [{"provider": "OfflineDiscovery", "result": "empty"}],
            "",
        )


class OfflineUniverse:
    def __init__(self) -> None:
        self.seed = market_radar.SectorDefinition(
            sector_id="concept:configured-seed",
            kind="concept",
            name="Configured Seed",
            benchmark_code="000985",
            effective_from=date(2026, 1, 1),
        )

    def load_with_history(self, as_of: date):
        assert as_of == NOW.date()
        return [self.seed], [self.seed]


class OfflineCapabilityProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    @staticmethod
    def _result(capability, data, *, status="ok"):
        return CapabilityResult(
            capability=capability,
            status=status,
            data=data,
            source="OfflineCapability",
            observed_at=NOW,
            data_date=NOW.date(),
            bar_status="finalized",
            freshness_seconds=0,
            trace=({"provider": "OfflineCapability", "result": status},),
        )

    @staticmethod
    def _history(code: str) -> BoardBarSeries:
        start = NOW.date() - timedelta(days=20)
        return BoardBarSeries(
            code=code,
            bars=tuple(
                BoardBar(
                    data_date=start + timedelta(days=index),
                    close=100.0 + index,
                    traded_amount=1000.0,
                )
                for index in range(21)
            ),
        )

    def fetch_board_history(self, sector, as_of):
        self.calls.append(("board_history", sector.sector_id))
        status = "partial" if sector.kind == "concept" else "ok"
        return self._result(
            "board_history",
            self._history(sector.sector_id),
            status=status,
        )

    def fetch_benchmark_history(self, code, as_of):
        self.calls.append(("benchmark_history", code))
        return self._result("benchmark_history", self._history(code))

    def fetch_board_flow(self, sector, as_of):
        self.calls.append(("board_flow", sector.sector_id))
        start = NOW.date() - timedelta(days=19)
        return self._result(
            "board_flow",
            BoardFlowSeries(
                code=sector.sector_id,
                flows=tuple(
                    BoardFlow(
                        data_date=start + timedelta(days=index),
                        net_main_inflow=10.0,
                        traded_amount=1000.0,
                    )
                    for index in range(20)
                ),
            ),
        )

    def fetch_constituents(self, sector, as_of):
        self.calls.append(("constituents", sector.sector_id))
        suffix = "1" if sector.kind == "industry" else "2"
        codes = tuple(f"{suffix}{index:05d}" for index in range(1, 6))
        return self._result(
            "constituents",
            ConstituentMembership(codes=codes, data_date=NOW.date()),
        )

    def fetch_constituent_quotes(self, codes, as_of):
        self.calls.append(("constituent_quotes", tuple(codes)))
        return self._result(
            "constituent_quotes",
            ConstituentQuoteBatch(
                quotes=tuple(
                    ConstituentQuote(
                        code=code,
                        current_price=11.0,
                        previous_close=10.0,
                        traded_amount=100.0,
                        quoted_at=NOW,
                    )
                    for code in codes
                )
            ),
            status="partial",
        )


def _offline_enriched_service(isolated_db):
    config = market_radar.MarketRadarEnrichmentConfig(
        candidate_limit=2,
        total_budget_seconds=10,
        max_concurrency=1,
    )
    capability_provider = OfflineCapabilityProvider()
    repository = market_radar.MarketRadarRepository(isolated_db)
    service = market_radar.MarketRadarService(
        universe_loader=OfflineUniverse(),
        provider=market_radar.LegacyRankingProvider(
            OfflineDiscoveryManager(),
            limit=1000,
        ),
        repository=repository,
        ranking_config=market_radar.RankingConfig(),
        enricher=market_radar.MarketRadarEnricher(
            provider=capability_provider,
            config=config,
        ),
        candidate_selector=market_radar.CandidateSelector(),
        enrichment_config=config,
        clock=lambda: NOW,
    )
    return service, repository, capability_provider


@pytest.fixture()
def isolated_db(tmp_path):
    old_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "phase_one_integration.db")
    try:
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_path


def test_package_and_model_imports_do_not_load_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys

        import src.market_radar as market_radar
        from src.market_radar import DataQuality, SectorScore

        assert DataQuality is market_radar.DataQuality
        assert SectorScore is market_radar.SectorScore
        forbidden = (
            "data_provider",
            "pandas",
            "src.storage",
            "src.market_radar.providers",
            "src.market_radar.candidates",
            "src.market_radar.capabilities",
            "src.market_radar.capability_provider",
            "src.market_radar.enrichment",
            "src.market_radar.ranking",
            "src.market_radar.replay",
            "src.market_radar.repository",
            "src.market_radar.service",
            "src.market_radar.universe",
        )
        loaded = tuple(sys.modules)
        assert not {
            name
            for name in loaded
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in forbidden
            )
        }
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_lazy_public_symbol_uses_fixed_mapping_and_caches_result() -> None:
    probe = textwrap.dedent(
        """
        import sys

        import src.market_radar as market_radar

        assert "src.market_radar.providers" not in sys.modules
        provider_class = market_radar.LegacyRankingProvider
        assert "src.market_radar.providers" in sys.modules
        assert market_radar.__dict__["LegacyRankingProvider"] is provider_class
        assert market_radar.LegacyRankingProvider is provider_class

        try:
            market_radar.not_a_public_symbol
        except AttributeError:
            pass
        else:
            raise AssertionError("unknown package attributes must be rejected")
        assert "src.market_radar.not_a_public_symbol" not in sys.modules
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_isolated_db_restores_state_when_database_initialization_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", "original.db")
    resets: list[str] = []
    monkeypatch.setattr(
        Config,
        "reset_instance",
        lambda: resets.append("config"),
    )
    monkeypatch.setattr(
        DatabaseManager,
        "reset_instance",
        lambda: resets.append("database"),
    )

    def fail_initialization():
        raise RuntimeError("database initialization failed")

    monkeypatch.setattr(DatabaseManager, "get_instance", fail_initialization)
    fixture = isolated_db.__wrapped__(tmp_path)

    with pytest.raises(RuntimeError, match="database initialization failed"):
        next(fixture)

    assert os.environ["DATABASE_PATH"] == "original.db"
    assert resets == ["config", "database", "database", "config"]


def test_public_api_is_explicit_and_complete() -> None:
    assert set(market_radar.__all__) == PUBLIC_API
    assert all(hasattr(market_radar, name) for name in PUBLIC_API)


def test_market_radar_docs_cover_phase_2a_operational_contract() -> None:
    text = (ROOT / "docs/market-radar.md").read_text(encoding="utf-8")
    for token in (
        "MARKET_RADAR_ENRICHMENT_LIMIT=60",
        "MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS=180",
        "MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY=6",
        "--discovery-only",
        "exact same UTC instant",
        "including another instant on the same Asia/Shanghai date",
        "persisted snapshot replay",
        "zero live provider calls",
        "does not initialize or read SQLite",
        "000985",
        "undated membership is excluded from dated breadth, concentration",
        "never inferred from board history or constituent quotes",
        "Tencent and Sina",
        "never substitutes the local fetch time",
    ):
        assert token in text

    for token in (
        "AkShare currently implements board history, benchmark history, "
        "industry flow, and current industry/concept membership",
        "Concept flow has no equivalent capability and is explicitly "
        "`unavailable`",
        "Constituent realtime quotes use the existing `DataFetcherManager` "
        "fallback chain",
        "Tushare and TickFlow do not currently override the optional "
        "normalized board-capability methods",
    ):
        assert token in text
    assert "EFinance may provide industry membership" not in text
    assert "Tushare or TickFlow providers participate" not in text


def test_english_index_market_radar_entry_tracks_phase_2a() -> None:
    text = (ROOT / "docs/INDEX_EN.md").read_text(encoding="utf-8")
    entry = next(line for line in text.splitlines() if "[Market Radar]" in line)

    assert "Phase 2A" in entry
    assert "Phase 1" not in entry


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


def test_offline_enrichment_persists_evidence_and_replays_without_live_calls(
    isolated_db,
) -> None:
    service, repository, capability_provider = _offline_enriched_service(isolated_db)

    snapshot = service.run(market="cn", persist=True)

    assert snapshot == repository.get_latest_run("cn")
    assert {item.sector_id for item in snapshot.sectors} == {
        "concept:configured-seed",
        "industry:discovered-industry",
    }
    configured = next(
        item
        for item in snapshot.sectors
        if item.sector_id == "concept:configured-seed"
    )
    observation = market_radar.SectorObservation.model_validate(
        configured.observation
    )
    assert observation.return_20d_pct == pytest.approx(20.0)
    assert observation.benchmark_return_20d_pct == pytest.approx(20.0)
    assert observation.capital_flow_20d == 1.0
    assert (observation.up_count, observation.down_count, observation.flat_count) == (
        5,
        0,
        0,
    )
    assert observation.concentration_ratio == 1.0
    assert observation.raw_reference["candidate_reasons"] == (
        "configured_seed",
    )
    assert len(repository.resolve_snapshot_constituent_evidence(snapshot)) == 2
    assert snapshot.provider_trace[0]["provider"] == "OfflineDiscovery"
    assert all(
        item["stage"] == "enrichment"
        for item in snapshot.provider_trace[2:]
    )
    assert any(
        item.get("result") == "partial"
        for item in snapshot.provider_trace[2:]
    )

    calls_before_replay = tuple(capability_provider.calls)
    replay = market_radar.MarketRadarReplayEngine(
        market_radar.RankingConfig()
    ).replay_persisted_run(repository, snapshot.run_key)

    assert replay.sectors == snapshot.sectors
    assert tuple(capability_provider.calls) == calls_before_replay


def test_offline_nonpersistent_enrichment_never_initializes_database(
    monkeypatch,
) -> None:
    config = market_radar.MarketRadarEnrichmentConfig(
        candidate_limit=2,
        total_budget_seconds=10,
        max_concurrency=1,
    )
    capability_provider = OfflineCapabilityProvider()
    monkeypatch.setattr(
        DatabaseManager,
        "get_instance",
        lambda: pytest.fail("nonpersistent enrichment must not initialize SQLite"),
    )
    service = market_radar.MarketRadarService(
        universe_loader=OfflineUniverse(),
        provider=market_radar.LegacyRankingProvider(
            OfflineDiscoveryManager(),
            limit=1000,
        ),
        repository=None,
        ranking_config=market_radar.RankingConfig(),
        enricher=market_radar.MarketRadarEnricher(
            provider=capability_provider,
            config=config,
        ),
        candidate_selector=market_radar.CandidateSelector(),
        enrichment_config=config,
        clock=lambda: NOW,
    )

    snapshot = service.run(market="cn", persist=False)

    assert len(snapshot.sectors) == 2
    assert capability_provider.calls


def test_offline_enriched_service_rolls_back_every_table_on_atomic_failure(
    isolated_db,
) -> None:
    service, repository, _ = _offline_enriched_service(isolated_db)
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_offline_enriched_evidence
            BEFORE INSERT ON radar_constituent_observations
            WHEN NEW.sector_id = 'concept:configured-seed'
            BEGIN
                SELECT RAISE(ABORT, 'rejected offline enriched evidence');
            END
            """
        )

    with pytest.raises(IntegrityError, match="rejected offline enriched evidence"):
        service.run(market="cn", persist=True)

    assert repository.get_latest_run("cn") is None
    assert repository.list_universe(NOW.date()) == []
    with isolated_db._engine.connect() as connection:
        for table in (
            "radar_constituent_sets",
            "radar_constituent_observations",
            "radar_runs",
            "radar_sector_snapshots",
            "radar_universe",
        ):
            count = connection.exec_driver_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).scalar_one()
            assert count == 0, table
