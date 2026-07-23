from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from src.market_radar.models import (
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    RadarRunSnapshot,
    SectorScore,
)


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)


def _sector(sector_id: str, name: str, score: float) -> SectorScore:
    return SectorScore(
        sector_id=sector_id,
        name=name,
        kind="industry",
        scoring_version="cn-v1",
        gross_score=score,
        risk_deduction=0,
        score=score,
        confidence=0.8,
        state="leading" if score >= 75 else "improving",
        factors={"trend_momentum": 20.0},
        risk_reasons=(),
        missing_fields=("catalyst_score",),
        source="fixture",
        observed_at=NOW,
        quality="partial",
        observation={"sector_id": sector_id},
    )


def _snapshot(*, phase2b: bool = True) -> RadarRunSnapshot:
    sectors = (
        _sector("industry:semiconductor", "Semiconductor", 82),
        _sector("industry:bank", "Bank", 68),
    )
    if not phase2b:
        return RadarRunSnapshot(
            run_key="cn:20260723T070000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=sectors,
            provider_trace=(),
        )

    observation = EtfObservation(
        sector_id=sectors[0].sector_id,
        code="512480",
        name="Semiconductor ETF",
        observed_at=NOW,
        data_date=None,
        bar_status=None,
        source="fixture",
        quality="partial",
        freshness_seconds=30,
        mapping_effective_from=date(2026, 1, 1),
        current_price=1.2,
        missing_fields=tuple(
            field
            for field in EtfObservation.tracked_metric_fields
            if field != "current_price"
        ),
    )
    selection = EtfSelection(
        sector_id=observation.sector_id,
        code=observation.code,
        name=observation.name,
        status="candidate",
        eligible=True,
        rank=1,
        score=80,
        confidence=0.75,
        components=EtfComponentScores(liquidity=85, trend=75),
        effective_weights={"liquidity": 58.3333, "trend": 41.6667},
        reason_codes=("optional_evidence_missing",),
        observation=observation,
    )
    regime = MarketRegimeAssessment(
        as_of=NOW,
        score=65,
        regime="selective",
        confidence=0.8,
        coverage=0.9,
        cohort_sector_ids=tuple(sector.sector_id for sector in sectors),
    )
    suggestion = PositionSuggestion(
        sector_id=sectors[0].sector_id,
        sector_name=sectors[0].name,
        sector_rank=1,
        etf_code=selection.code,
        etf_status="candidate",
        sector_cap_pct=10,
        etf_cap_pct=10,
        joint_confidence=0.75,
    )
    plan = PositionPlan(
        as_of=NOW,
        regime="selective",
        total_position_min_pct=35,
        total_position_max_pct=60,
        suggestions=(suggestion,),
        correlation_coverage=1,
        confidence=0.75,
    )
    return RadarRunSnapshot(
        run_key="cn:20260723T070000Z:manual",
        market="cn",
        trigger="manual",
        as_of=NOW,
        quality="partial",
        scoring_version="cn-v1",
        sectors=sectors,
        provider_trace=({"source": "fixture", "status": "ok"},),
        etfs=(selection,),
        regime=regime,
        position_plan=plan,
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("api.middlewares.auth.is_auth_enabled", lambda: False)
    return TestClient(create_app(static_dir=tmp_path / "empty-static"))


@pytest.fixture()
def repository():
    with patch("api.v1.endpoints.market_radar.MarketRadarRepository") as mocked:
        yield mocked


def test_latest_returns_complete_snapshot(client: TestClient, repository) -> None:
    snapshot = _snapshot()
    repository.return_value.get_latest_run.return_value = snapshot

    response = client.get("/api/v1/market-radar/latest")

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["run"]["run_key"] == snapshot.run_key
    assert response.json()["run"]["position_plan"]["regime"] == "selective"
    repository.return_value.get_latest_run.assert_called_once_with("cn")


def test_sector_list_preserves_persisted_rank_order(
    client: TestClient, repository
) -> None:
    snapshot = _snapshot()
    repository.return_value.get_latest_run.return_value = snapshot

    payload = client.get("/api/v1/market-radar/sectors").json()

    assert payload["available"] is True
    assert payload["run_key"] == snapshot.run_key
    assert payload["as_of"] == NOW.isoformat().replace("+00:00", "Z")
    assert payload["total"] == 2
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert [item["sector"]["sector_id"] for item in payload["items"]] == [
        sector.sector_id for sector in snapshot.sectors
    ]


def test_sector_detail_aggregates_only_matching_evidence(
    client: TestClient, repository
) -> None:
    snapshot = _snapshot()
    repository.return_value.get_latest_run.return_value = snapshot

    response = client.get(
        f"/api/v1/market-radar/sectors/{snapshot.sectors[0].sector_id}"
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["rank"] == 1
    assert payload["sector"]["sector_id"] == snapshot.sectors[0].sector_id
    assert [item["code"] for item in payload["etfs"]] == ["512480"]
    assert payload["position_suggestion"]["sector_id"] == snapshot.sectors[0].sector_id
    assert payload["regime"]["regime"] == "selective"
    assert payload["position_plan"]["policy_version"] == "cn-position-v1"


def test_empty_bootstrap_returns_available_false(client: TestClient, repository) -> None:
    repository.return_value.get_latest_run.return_value = None

    assert client.get("/api/v1/market-radar/latest").json() == {
        "available": False,
        "run": None,
    }
    assert client.get("/api/v1/market-radar/sectors").json() == {
        "available": False,
        "run_key": None,
        "as_of": None,
        "items": [],
        "total": 0,
    }


def test_sector_detail_distinguishes_missing_run_and_sector(
    client: TestClient, repository
) -> None:
    repository.return_value.get_latest_run.return_value = None
    missing_run = client.get("/api/v1/market-radar/sectors/industry:missing")
    assert missing_run.status_code == 404
    assert missing_run.json()["error"] == "market_radar_run_not_found"

    repository.return_value.get_latest_run.return_value = _snapshot()
    missing_sector = client.get("/api/v1/market-radar/sectors/industry:missing")
    assert missing_sector.status_code == 404
    assert missing_sector.json()["error"] == "market_radar_sector_not_found"


def test_legacy_snapshot_keeps_phase2b_fields_nullable(
    client: TestClient, repository
) -> None:
    snapshot = _snapshot(phase2b=False)
    repository.return_value.get_latest_run.return_value = snapshot

    payload = client.get(
        f"/api/v1/market-radar/sectors/{snapshot.sectors[0].sector_id}"
    ).json()

    assert payload["etfs"] == []
    assert payload["position_suggestion"] is None
    assert payload["regime"] is None
    assert payload["position_plan"] is None


def test_storage_errors_are_redacted(client: TestClient, repository) -> None:
    repository.return_value.get_latest_run.side_effect = RuntimeError(
        "secret database path"
    )

    response = client.get("/api/v1/market-radar/latest")

    assert response.status_code == 500
    assert response.json() == {
        "error": "market_radar_read_failed",
        "message": "Unable to read Market Radar data",
    }
    assert "secret database path" not in response.text


def test_routes_require_session_when_admin_auth_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.middlewares.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("api.middlewares.auth.verify_session", lambda _cookie: False)
    protected_client = TestClient(create_app(static_dir=tmp_path / "empty-static"))

    response = protected_client.get("/api/v1/market-radar/latest")

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_openapi_declares_market_radar_models_and_cookie_security(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/market-radar/latest"]["get"]

    assert operation["operationId"] == "getLatestMarketRadar"
    assert {"AdminSessionCookie": []} in operation["security"]
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/MarketRadarLatestResponse"
    )
