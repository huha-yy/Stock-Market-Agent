from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def test_sector_definition_rejects_non_cn_market() -> None:
    with pytest.raises(ValidationError):
        SectorDefinition(
            sector_id="industry:semiconductor",
            market="hk",
            kind="industry",
            name="Semiconductor",
            effective_from=date(2026, 1, 1),
        )


def test_observation_keeps_missing_fields_and_provenance() -> None:
    observation = SectorObservation(
        sector_id="industry:semiconductor",
        name="Semiconductor",
        kind="industry",
        observed_at=NOW,
        source="akshare_industry",
        freshness_seconds=12,
        quality="partial",
        return_1d_pct=2.5,
        missing_fields=["return_20d_pct", "capital_flow_5d"],
    )

    assert observation.market == "cn"
    assert observation.return_20d_pct is None
    assert observation.missing_fields == ["return_20d_pct", "capital_flow_5d"]


def test_run_snapshot_requires_unique_sector_ids() -> None:
    score = SectorScore(
        sector_id="industry:semiconductor",
        name="Semiconductor",
        kind="industry",
        scoring_version="cn-v1",
        gross_score=70.0,
        risk_deduction=0.0,
        score=70.0,
        confidence=0.65,
        state="improving",
        factors={},
        risk_reasons=[],
        missing_fields=[],
        source="akshare_industry",
        observed_at=NOW,
        quality="partial",
    )

    with pytest.raises(ValidationError, match="duplicate sector_id"):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=[score, score],
            provider_trace=[],
        )


def test_etf_definition_validates_six_digit_code() -> None:
    with pytest.raises(ValidationError):
        EtfDefinition(
            code="ETF512480",
            name="Semiconductor ETF",
            sector_id="industry:semiconductor",
            effective_from=date(2026, 1, 1),
        )


def test_score_rejects_non_cn_v1_scoring_version() -> None:
    with pytest.raises(ValidationError):
        SectorScore(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            scoring_version="cn-v2",
            gross_score=70.0,
            risk_deduction=0.0,
            score=70.0,
            confidence=0.65,
            state="improving",
            factors={},
            risk_reasons=[],
            missing_fields=[],
            source="akshare_industry",
            observed_at=NOW,
            quality="partial",
        )


def test_observation_requires_explicit_missing_field_provenance() -> None:
    with pytest.raises(ValidationError, match="missing_fields"):
        SectorObservation(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            observed_at=NOW,
            source="akshare_industry",
            freshness_seconds=12,
            quality="partial",
        )


def test_contract_containers_are_deeply_immutable_and_serializable() -> None:
    etf = EtfDefinition(
        code="512480",
        name="Semiconductor ETF",
        sector_id="industry:semiconductor",
        effective_from=date(2026, 1, 1),
    )
    definition = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor",
        aliases=["Chips"],
        etfs=[etf],
        effective_from=date(2026, 1, 1),
    )
    observation = SectorObservation(
        sector_id="industry:semiconductor",
        name="Semiconductor",
        kind="industry",
        observed_at=NOW,
        source="akshare_industry",
        freshness_seconds=12,
        quality="partial",
        missing_fields=["return_20d_pct"],
        raw_reference={"providers": [{"name": "akshare"}]},
    )
    score = SectorScore(
        sector_id="industry:semiconductor",
        name="Semiconductor",
        kind="industry",
        scoring_version="cn-v1",
        gross_score=70.0,
        risk_deduction=0.0,
        score=70.0,
        confidence=0.65,
        state="improving",
        factors={"trend": {"value": 20.0}},
        risk_reasons=["concentration"],
        missing_fields=["capital_flow_5d"],
        source="akshare_industry",
        observed_at=NOW,
        quality="partial",
        observation={"details": ["partial"]},
    )
    snapshot = RadarRunSnapshot(
        run_key="cn:20260721T060000Z:manual",
        market="cn",
        trigger="manual",
        as_of=NOW,
        quality="partial",
        scoring_version="cn-v1",
        sectors=[score],
        provider_trace=[{"provider": {"name": "akshare"}}],
    )

    with pytest.raises(TypeError):
        definition.aliases.append("Semis")
    with pytest.raises(TypeError):
        definition.etfs.append(etf)
    with pytest.raises(TypeError):
        observation.missing_fields.append("capital_flow_5d")
    with pytest.raises(TypeError):
        observation.raw_reference["providers"][0]["name"] = "other"
    with pytest.raises(TypeError):
        score.risk_reasons.append("volatility")
    with pytest.raises(TypeError):
        score.factors["trend"]["value"] = 10.0
    with pytest.raises(TypeError):
        score.observation["details"].append("stale")
    with pytest.raises(TypeError):
        snapshot.sectors.append(score)
    with pytest.raises(TypeError):
        snapshot.provider_trace[0]["provider"]["name"] = "other"

    assert snapshot.model_dump()["provider_trace"] == [
        {"provider": {"name": "akshare"}}
    ]


def test_run_snapshot_rejects_non_cn_v1_scoring_version() -> None:
    with pytest.raises(ValidationError):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v2",
            sectors=[],
            provider_trace=[],
        )
