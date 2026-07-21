from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import run_market_radar
from src.config import Config


MARKET_RADAR_ENV = (
    "MARKET_RADAR_PROVIDER_LIMIT",
    "MARKET_RADAR_STALE_AFTER_SECONDS",
    "MARKET_RADAR_SCORING_VERSION",
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


def test_config_reads_market_radar_defaults(monkeypatch) -> None:
    config = _load_config(monkeypatch)

    assert config.market_radar_provider_limit == 1000
    assert config.market_radar_stale_after_seconds == 2700
    assert config.market_radar_scoring_version == "cn-v1"


def test_config_reads_market_radar_environment(monkeypatch) -> None:
    config = _load_config(
        monkeypatch,
        {
            "MARKET_RADAR_PROVIDER_LIMIT": "250",
            "MARKET_RADAR_STALE_AFTER_SECONDS": "600",
            "MARKET_RADAR_SCORING_VERSION": " cn-v1 ",
        },
    )

    assert config.market_radar_provider_limit == 250
    assert config.market_radar_stale_after_seconds == 600
    assert config.market_radar_scoring_version == "cn-v1"


def test_config_clamps_and_falls_back_for_invalid_market_radar_values(
    monkeypatch,
) -> None:
    config = _load_config(
        monkeypatch,
        {
            "MARKET_RADAR_PROVIDER_LIMIT": "9",
            "MARKET_RADAR_STALE_AFTER_SECONDS": "not-an-int",
            "MARKET_RADAR_SCORING_VERSION": "   ",
        },
    )

    assert config.market_radar_provider_limit == 10
    assert config.market_radar_stale_after_seconds == 2700
    assert config.market_radar_scoring_version == "cn-v1"


def test_build_service_composes_phase_one_dependencies(monkeypatch) -> None:
    manager = object()
    universe_loader = object()
    provider = object()
    repository = object()
    ranking_config = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        run_market_radar,
        "get_config",
        lambda: SimpleNamespace(
            market_radar_provider_limit=321,
            market_radar_stale_after_seconds=654,
            market_radar_scoring_version="cn-v1",
        ),
    )
    monkeypatch.setattr(run_market_radar, "DataFetcherManager", lambda: manager)

    def build_universe_loader(path):
        captured["universe_path"] = path
        return universe_loader

    monkeypatch.setattr(run_market_radar, "UniverseLoader", build_universe_loader)

    def build_provider(actual_manager, *, limit):
        captured["manager"] = actual_manager
        captured["limit"] = limit
        return provider

    def build_ranking_config(**kwargs):
        captured["ranking_kwargs"] = kwargs
        return ranking_config

    def build_service(**kwargs):
        captured["service_kwargs"] = kwargs
        return "service"

    monkeypatch.setattr(run_market_radar, "LegacyRankingProvider", build_provider)
    monkeypatch.setattr(run_market_radar, "MarketRadarRepository", lambda: repository)
    monkeypatch.setattr(run_market_radar, "RankingConfig", build_ranking_config)
    monkeypatch.setattr(run_market_radar, "MarketRadarService", build_service)

    result = run_market_radar.build_service()

    assert result == "service"
    assert captured["universe_path"] == (
        run_market_radar.ROOT / "src/data/market_radar/a_share_etfs.yaml"
    )
    assert captured["manager"] is manager
    assert captured["limit"] == 321
    assert captured["ranking_kwargs"] == {
        "scoring_version": "cn-v1",
        "stale_after_seconds": 654,
    }
    assert captured["service_kwargs"] == {
        "universe_loader": universe_loader,
        "provider": provider,
        "repository": repository,
        "ranking_config": ranking_config,
    }


def test_cli_writes_structured_json_without_persistence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "nested" / "radar.json"

    class FakeService:
        def run(self, **kwargs):
            assert kwargs == {"market": "cn", "persist": False}
            return _snapshot()

    monkeypatch.setattr(run_market_radar, "build_service", lambda: FakeService())

    exit_code = run_market_radar.main(
        ["--market", "cn", "--output", str(output)]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "market": "cn",
        "quality": "partial",
        "sectors": [],
    }


def test_cli_persists_and_writes_structured_json_to_stdout(
    monkeypatch,
    capsys,
) -> None:
    class FakeService:
        def run(self, **kwargs):
            assert kwargs == {"market": "cn", "persist": True}
            return _snapshot()

    monkeypatch.setattr(run_market_radar, "build_service", lambda: FakeService())

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
        lambda: pytest.fail("unsupported market must not build dependencies"),
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

    monkeypatch.setattr(run_market_radar, "build_service", lambda: FailingService())

    exit_code = run_market_radar.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.strip() == "Market Radar failed: provider unavailable"
    assert captured.out == ""
    assert not output.exists()


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
