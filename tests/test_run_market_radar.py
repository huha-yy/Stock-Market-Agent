from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import run_market_radar
from src.config import Config
from src.market_radar import factory as factory_module
from src.market_radar.service import MarketRadarService


MARKET_RADAR_ENV = (
    "MARKET_RADAR_PROVIDER_LIMIT",
    "MARKET_RADAR_STALE_AFTER_SECONDS",
    "MARKET_RADAR_SCORING_VERSION",
    "MARKET_RADAR_ENRICHMENT_LIMIT",
    "MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS",
    "MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY",
)


def _load_config(monkeypatch, values: dict[str, str] | None = None) -> Config:
    monkeypatch.setattr("src.config.setup_env", lambda: None)
    for key in MARKET_RADAR_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in (values or {}).items():
        monkeypatch.setenv(key, value)
    return Config._load_from_env()


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        model_dump_json=lambda indent: json.dumps(
            {"market": "cn", "quality": "partial", "sectors": []},
            ensure_ascii=False,
            indent=indent,
        )
    )


def _runtime_config(**overrides: object) -> SimpleNamespace:
    values = {
        "market_radar_provider_limit": 321,
        "market_radar_stale_after_seconds": 654,
        "market_radar_scoring_version": "cn-v1",
        "market_radar_enrichment_limit": 60,
        "market_radar_enrichment_budget_seconds": 180,
        "market_radar_enrichment_max_concurrency": 6,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_config_reads_market_radar_defaults(monkeypatch) -> None:
    config = _load_config(monkeypatch)

    assert config.market_radar_provider_limit == 1000
    assert config.market_radar_stale_after_seconds == 2700
    assert config.market_radar_scoring_version == "cn-v1"
    assert config.market_radar_enrichment_limit == 60
    assert config.market_radar_enrichment_budget_seconds == 180
    assert config.market_radar_enrichment_max_concurrency == 6


def test_config_reads_market_radar_environment(monkeypatch) -> None:
    config = _load_config(
        monkeypatch,
        {
            "MARKET_RADAR_PROVIDER_LIMIT": "250",
            "MARKET_RADAR_STALE_AFTER_SECONDS": "600",
            "MARKET_RADAR_SCORING_VERSION": " cn-v1 ",
            "MARKET_RADAR_ENRICHMENT_LIMIT": "75",
            "MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS": "240",
            "MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY": "4",
        },
    )

    assert config.market_radar_provider_limit == 250
    assert config.market_radar_stale_after_seconds == 600
    assert config.market_radar_scoring_version == "cn-v1"
    assert config.market_radar_enrichment_limit == 75
    assert config.market_radar_enrichment_budget_seconds == 240
    assert config.market_radar_enrichment_max_concurrency == 4


def test_config_clamps_and_falls_back_for_invalid_market_radar_values(
    monkeypatch,
) -> None:
    config = _load_config(
        monkeypatch,
        {
            "MARKET_RADAR_PROVIDER_LIMIT": "9",
            "MARKET_RADAR_STALE_AFTER_SECONDS": "not-an-int",
            "MARKET_RADAR_SCORING_VERSION": "   ",
            "MARKET_RADAR_ENRICHMENT_LIMIT": "0",
            "MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS": "901",
            "MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY": "invalid",
        },
    )

    assert config.market_radar_provider_limit == 10
    assert config.market_radar_stale_after_seconds == 2700
    assert config.market_radar_scoring_version == "cn-v1"
    assert config.market_radar_enrichment_limit == 1
    assert config.market_radar_enrichment_budget_seconds == 900
    assert config.market_radar_enrichment_max_concurrency == 6


def test_build_service_composes_enrichment_with_one_shared_manager(monkeypatch) -> None:
    manager = object()
    universe_loader = object()
    provider = object()
    adapter = object()
    selector = object()
    enricher = object()
    repository = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        factory_module,
        "get_config",
        lambda: _runtime_config(
            market_radar_enrichment_limit=75,
            market_radar_enrichment_budget_seconds=240,
            market_radar_enrichment_max_concurrency=4,
        ),
    )
    manager_calls = 0

    def build_manager():
        nonlocal manager_calls
        manager_calls += 1
        return manager

    monkeypatch.setattr(factory_module, "DataFetcherManager", build_manager)

    def build_universe_loader(path):
        captured["universe_path"] = path
        return universe_loader

    monkeypatch.setattr(factory_module, "UniverseLoader", build_universe_loader)

    def build_provider(actual_manager, *, limit):
        captured["manager"] = actual_manager
        captured["limit"] = limit
        return provider

    monkeypatch.setattr(factory_module, "LegacyRankingProvider", build_provider)
    monkeypatch.setattr(
        factory_module,
        "ProviderCapabilityAdapter",
        lambda actual_manager: (
            captured.setdefault("adapter_manager", actual_manager),
            adapter,
        )[1],
    )
    monkeypatch.setattr(factory_module, "CandidateSelector", lambda: selector)

    def build_enricher(*, provider, config):
        captured["adapter"] = provider
        captured["enrichment_config"] = config
        return enricher

    monkeypatch.setattr(factory_module, "MarketRadarEnricher", build_enricher)
    monkeypatch.setattr(factory_module, "MarketRadarRepository", lambda: repository)

    result = run_market_radar.build_service(persist=True)

    assert isinstance(result, MarketRadarService)
    assert manager_calls == 1
    assert captured["universe_path"] == (
        factory_module.ROOT / "src/data/market_radar/a_share_etfs.yaml"
    )
    assert captured["manager"] is manager
    assert captured["adapter_manager"] is manager
    assert captured["adapter"] is adapter
    assert captured["limit"] == 321
    assert result.universe_loader is universe_loader
    assert result.provider is provider
    assert result.repository is repository
    assert result.enricher is enricher
    assert result.candidate_selector is selector
    assert result.enrichment_config == captured["enrichment_config"]
    assert result.enrichment_config.candidate_limit == 75
    assert result.enrichment_config.total_budget_seconds == 240
    assert result.enrichment_config.max_concurrency == 4
    assert result.ranking_config.scoring_version == "cn-v1"
    assert result.ranking_config.stale_after_seconds == 654


def test_build_service_default_mode_does_not_initialize_database(
    monkeypatch,
) -> None:
    from src.storage import DatabaseManager

    monkeypatch.setattr(
        factory_module,
        "get_config",
        _runtime_config,
    )
    monkeypatch.setattr(factory_module, "DataFetcherManager", lambda: object())

    def unexpected_database(*_args, **_kwargs):
        raise AssertionError("non-persistent CLI must not initialize the database")

    monkeypatch.setattr(
        factory_module,
        "MarketRadarRepository",
        unexpected_database,
    )
    monkeypatch.setattr(DatabaseManager, "get_instance", unexpected_database)

    service = run_market_radar.build_service(persist=False)

    assert isinstance(service, MarketRadarService)
    assert service.repository is None


def test_reusable_factory_reuses_injected_repository(monkeypatch) -> None:
    factory = importlib.import_module("src.market_radar.factory")

    class FalseyRepository:
        def __bool__(self) -> bool:
            return False

    repository = FalseyRepository()
    monkeypatch.setattr(factory, "get_config", _runtime_config)
    monkeypatch.setattr(factory, "DataFetcherManager", lambda: object())

    def unexpected_repository():
        raise AssertionError("an injected repository must be reused")

    monkeypatch.setattr(factory, "MarketRadarRepository", unexpected_repository)

    service = factory.build_market_radar_service(
        persist=True,
        repository=repository,
    )

    assert service.repository is repository
    assert service.enricher is not None
    assert service.etf_collector is not None
    assert service.lifecycle_engine is not None


def test_cli_build_service_is_factory_compatibility_alias() -> None:
    from src.market_radar.factory import build_market_radar_service

    assert run_market_radar.build_service is build_market_radar_service


def test_build_service_discovery_only_skips_enrichment_construction(
    monkeypatch,
) -> None:
    manager = object()
    monkeypatch.setattr(factory_module, "get_config", _runtime_config)
    monkeypatch.setattr(factory_module, "DataFetcherManager", lambda: manager)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("discovery-only mode must not construct enrichment")

    monkeypatch.setattr(factory_module, "ProviderCapabilityAdapter", unexpected)
    monkeypatch.setattr(factory_module, "CandidateSelector", unexpected)
    monkeypatch.setattr(factory_module, "MarketRadarEnricher", unexpected)

    service = run_market_radar.build_service(
        persist=False,
        discovery_only=True,
    )

    assert service.provider.manager is manager
    assert service.enricher is None
    assert service.candidate_selector is None
    assert service.repository is None


def test_invalid_scoring_version_fails_before_side_effectful_construction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        factory_module,
        "get_config",
        lambda: _runtime_config(market_radar_scoring_version="cn-v2"),
    )
    calls: list[str] = []

    def unexpected(name):
        def construct(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be constructed")

        return construct

    monkeypatch.setattr(
        factory_module,
        "DataFetcherManager",
        unexpected("manager"),
    )
    monkeypatch.setattr(
        factory_module,
        "UniverseLoader",
        unexpected("universe"),
    )
    monkeypatch.setattr(
        factory_module,
        "MarketRadarRepository",
        unexpected("repository"),
    )

    with pytest.raises(ValueError, match="scoring_version"):
        run_market_radar.build_service(persist=True)

    assert calls == []


def test_invalid_enrichment_config_fails_before_side_effectful_construction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        factory_module,
        "get_config",
        lambda: _runtime_config(market_radar_enrichment_limit=0),
    )
    calls: list[str] = []

    def unexpected(name):
        def construct(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be constructed")

        return construct

    monkeypatch.setattr(factory_module, "DataFetcherManager", unexpected("manager"))
    monkeypatch.setattr(factory_module, "UniverseLoader", unexpected("universe"))
    monkeypatch.setattr(
        factory_module,
        "MarketRadarRepository",
        unexpected("repository"),
    )

    with pytest.raises(ValueError, match="candidate_limit"):
        run_market_radar.build_service(persist=True)

    assert calls == []


def test_cli_writes_structured_json_without_persistence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "nested" / "radar.json"

    class FakeService:
        def run(self, **kwargs):
            assert kwargs == {
                "market": "cn",
                "persist": False,
                "discovery_only": False,
            }
            return _snapshot()

    monkeypatch.setattr(
        run_market_radar,
        "build_service",
        lambda *, persist, discovery_only: FakeService(),
    )

    exit_code = run_market_radar.main(
        ["--market", "cn", "--output", str(output)]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "market": "cn",
        "quality": "partial",
        "sectors": [],
    }
    assert list(output.parent.iterdir()) == [output]


def test_cli_persists_and_writes_structured_json_to_stdout(
    monkeypatch,
    capsys,
) -> None:
    class FakeService:
        def run(self, **kwargs):
            assert kwargs == {
                "market": "cn",
                "persist": True,
                "discovery_only": False,
            }
            return _snapshot()

    monkeypatch.setattr(
        run_market_radar,
        "build_service",
        lambda *, persist, discovery_only: FakeService(),
    )

    exit_code = run_market_radar.main(["--market", "cn", "--persist"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["market"] == "cn"
    assert captured.err == ""


def test_cli_rejects_non_cn_market_before_building_service(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        run_market_radar,
        "build_service",
        lambda *, persist, discovery_only: pytest.fail(
            "unsupported market must not build dependencies"
        ),
    )

    exit_code = run_market_radar.main(["--market", "hk"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "supports --market cn only" in captured.err
    assert captured.out == ""


def test_cli_reports_runtime_failure_without_json_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "radar.json"

    class FailingService:
        def run(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        run_market_radar,
        "build_service",
        lambda *, persist, discovery_only: FailingService(),
    )

    exit_code = run_market_radar.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.strip() == "Market Radar failed: provider unavailable"
    assert captured.out == ""
    assert not output.exists()


def test_cli_failed_atomic_replace_preserves_existing_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "radar.json"
    original = b'{"status":"existing"}\n'
    output.write_bytes(original)

    class FakeService:
        def run(self, **_kwargs):
            return _snapshot()

    monkeypatch.setattr(
        run_market_radar,
        "build_service",
        lambda *, persist, discovery_only: FakeService(),
    )

    def fail_replace(_self, _target):
        raise OSError("replace denied")

    monkeypatch.setattr(Path, "replace", fail_replace)

    exit_code = run_market_radar.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "replace denied" in captured.err
    assert output.read_bytes() == original
    assert list(tmp_path.iterdir()) == [output]


def test_discovery_only_cli_bypasses_enrichment_explicitly(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        def run(self, **kwargs):
            captured["run"] = kwargs
            return _snapshot()

    def build_service(*, persist, discovery_only):
        captured["build"] = {
            "persist": persist,
            "discovery_only": discovery_only,
        }
        return FakeService()

    monkeypatch.setattr(run_market_radar, "build_service", build_service)

    exit_code = run_market_radar.main(["--discovery-only"])

    assert exit_code == 0
    assert captured == {
        "build": {"persist": False, "discovery_only": True},
        "run": {
            "market": "cn",
            "persist": False,
            "discovery_only": True,
        },
    }
    assert json.loads(capsys.readouterr().out)["market"] == "cn"


def test_import_does_not_construct_provider_or_repository(monkeypatch) -> None:
    import data_provider
    from src.market_radar import repository as repository_module

    def unexpected_construction(*_args, **_kwargs):
        raise AssertionError("import must not construct runtime dependencies")

    monkeypatch.setattr(data_provider, "DataFetcherManager", unexpected_construction)
    monkeypatch.setattr(
        repository_module,
        "MarketRadarRepository",
        unexpected_construction,
    )
    sys.modules.pop("scripts.run_market_radar", None)
    try:
        imported = importlib.import_module("scripts.run_market_radar")
    finally:
        sys.modules["scripts.run_market_radar"] = run_market_radar
        setattr(sys.modules["scripts"], "run_market_radar", run_market_radar)

    assert callable(imported.build_service)
