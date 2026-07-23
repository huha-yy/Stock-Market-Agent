import warnings
from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from src.market_radar.models import (
    CorrelationGroup,
    EtfDefinition,
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    RadarRunSnapshot,
    RegimeComponents,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
TRACKED_METRICS = [
    "return_1d_pct",
    "return_5d_pct",
    "return_20d_pct",
    "benchmark_return_20d_pct",
    "capital_flow_1d",
    "capital_flow_5d",
    "capital_flow_20d",
    "turnover_ratio_20d",
    "up_count",
    "down_count",
    "flat_count",
    "volatility_ratio_20d",
    "distance_ma20_pct",
    "price_flow_divergence",
    "concentration_ratio",
    "catalyst_score",
]
MISSING_EXCEPT_RETURN_1D = TRACKED_METRICS[1:]


ETF_TRACKED_METRICS = (
    "data_date",
    "bar_status",
    "active",
    "finalized_session_count",
    "suspended",
    "current_price",
    "current_traded_amount",
    "average_traded_amount_20d",
    "spread_bps",
    "premium_discount_pct",
    "return_20d_pct",
    "return_60d_pct",
    "daily_return_dates_60",
    "daily_returns_60",
    "tracking_error_pct",
    "tracking_difference_pct",
    "annual_fee_pct",
    "size_cny",
    "liquidity_stability",
)


def complete_etf_observation(
    *,
    code: str = "512480",
    sector_id: str = "industry:semiconductor",
    **overrides: object,
) -> EtfObservation:
    dates = tuple(date(2026, 4, day) for day in range(1, 31)) + tuple(
        date(2026, 5, day) for day in range(1, 31)
    )
    values: dict[str, object] = {
        "sector_id": sector_id,
        "code": code,
        "name": "Semiconductor ETF",
        "observed_at": NOW,
        "data_date": date(2026, 7, 21),
        "bar_status": "finalized",
        "source": "provider",
        "quality": "complete",
        "freshness_seconds": 30,
        "mapping_effective_from": date(2026, 1, 1),
        "active": True,
        "finalized_session_count": 60,
        "suspended": False,
        "current_price": 1.2,
        "current_traded_amount": 20_000_000.0,
        "average_traded_amount_20d": 15_000_000.0,
        "spread_bps": 10.0,
        "premium_discount_pct": 0.1,
        "return_20d_pct": 3.0,
        "return_60d_pct": 8.0,
        "daily_return_dates_60": dates,
        "daily_returns_60": tuple(0.01 for _ in dates),
        "tracking_error_pct": 0.2,
        "tracking_difference_pct": -0.1,
        "annual_fee_pct": 0.5,
        "size_cny": 1_000_000_000.0,
        "liquidity_stability": 0.9,
        "missing_fields": (),
    }
    values.update(overrides)
    return EtfObservation(**values)


def complete_etf_selection(
    *, code: str = "512480", sector_id: str = "industry:semiconductor"
) -> EtfSelection:
    return EtfSelection(
        sector_id=sector_id,
        code=code,
        name="Semiconductor ETF",
        status="best_supported",
        eligible=True,
        rank=1,
        score=80.0,
        confidence=0.9,
        components=EtfComponentScores(
            liquidity=90.0,
            trend=80.0,
            tracking_quality=70.0,
            cost=60.0,
            size=50.0,
        ),
        effective_weights={
            "liquidity": 35.0,
            "trend": 25.0,
            "tracking_quality": 20.0,
            "cost": 10.0,
            "size": 10.0,
        },
        observation=complete_etf_observation(code=code, sector_id=sector_id),
    )


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
        missing_fields=MISSING_EXCEPT_RETURN_1D,
    )

    assert observation.market == "cn"
    assert observation.return_20d_pct is None
    assert observation.price_flow_divergence is None
    assert tuple(observation.missing_fields) == tuple(MISSING_EXCEPT_RETURN_1D)


def test_explicit_false_divergence_is_observed_evidence() -> None:
    observation = SectorObservation(
        sector_id="industry:semiconductor",
        name="Semiconductor",
        kind="industry",
        observed_at=NOW,
        source="akshare_industry",
        freshness_seconds=12,
        quality="partial",
        price_flow_divergence=False,
        missing_fields=[
            field
            for field in TRACKED_METRICS
            if field != "price_flow_divergence"
        ],
    )

    assert observation.price_flow_divergence is False
    assert "price_flow_divergence" not in observation.missing_fields


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
        missing_fields=TRACKED_METRICS,
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

    with pytest.raises((AttributeError, TypeError)):
        definition.aliases.append("Semis")
    with pytest.raises((AttributeError, TypeError)):
        definition.etfs.append(etf)
    with pytest.raises((AttributeError, TypeError)):
        observation.missing_fields.append("capital_flow_5d")
    with pytest.raises(TypeError):
        observation.raw_reference["providers"][0]["name"] = "other"
    with pytest.raises((AttributeError, TypeError)):
        score.risk_reasons.append("volatility")
    with pytest.raises(TypeError):
        score.factors["trend"]["value"] = 10.0
    with pytest.raises((AttributeError, TypeError)):
        score.observation["details"].append("stale")
    with pytest.raises((AttributeError, TypeError)):
        snapshot.sectors.append(score)
    with pytest.raises(TypeError):
        snapshot.provider_trace[0]["provider"]["name"] = "other"

    with warnings.catch_warnings(record=True) as captured_warnings:
        python_dump = snapshot.model_dump()
        json_dump = snapshot.model_dump(mode="json")
    assert python_dump["provider_trace"] == [{"provider": {"name": "akshare"}}]
    assert json_dump["provider_trace"] == [
        {"provider": {"name": "akshare"}}
    ]
    assert not captured_warnings


def test_contract_containers_reject_builtin_mutation_descriptors() -> None:
    definition = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor",
        aliases=["Chips"],
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
        missing_fields=TRACKED_METRICS,
        raw_reference={"provider": "akshare"},
    )

    with pytest.raises(TypeError):
        list.append(definition.aliases, "Semis")
    with pytest.raises(TypeError):
        dict.__setitem__(observation.raw_reference, "x", 1)
    with pytest.raises(AttributeError):
        observation.raw_reference._values = {}
    with pytest.raises(AttributeError):
        object.__setattr__(observation.raw_reference, "_values", MappingProxyType({}))


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


def test_observation_rejects_empty_provenance_when_metrics_are_missing() -> None:
    with pytest.raises(ValidationError, match="missing_fields"):
        SectorObservation(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            observed_at=NOW,
            source="akshare_industry",
            freshness_seconds=12,
            quality="partial",
            missing_fields=[],
        )


def test_observation_rejects_incomplete_missing_field_provenance() -> None:
    with pytest.raises(ValidationError, match="missing_fields"):
        SectorObservation(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            observed_at=NOW,
            source="akshare_industry",
            freshness_seconds=12,
            quality="partial",
            missing_fields=TRACKED_METRICS[:-1],
        )


def test_observation_rejects_unknown_missing_field_provenance() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        SectorObservation(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            observed_at=NOW,
            source="akshare_industry",
            freshness_seconds=12,
            quality="partial",
            missing_fields=["non_metric"],
        )


def test_observation_rejects_duplicate_missing_field_provenance() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        SectorObservation(
            sector_id="industry:semiconductor",
            name="Semiconductor",
            kind="industry",
            observed_at=NOW,
            source="akshare_industry",
            freshness_seconds=12,
            quality="partial",
            missing_fields=TRACKED_METRICS + ["return_1d_pct"],
        )


def test_phase_2b_contracts_are_complete_and_legacy_snapshots_still_construct() -> None:
    selection = complete_etf_selection()
    regime = MarketRegimeAssessment(
        as_of=NOW,
        score=70.0,
        regime="selective",
        confidence=0.8,
        coverage=0.9,
        components=RegimeComponents(
            benchmark_trend=75.0,
            positive_sector_diffusion=70.0,
            flow_diffusion=65.0,
            liquidity_diffusion=60.0,
            non_risk_sector_share=80.0,
        ),
        cohort_sector_ids=("industry:semiconductor",),
    )
    suggestion = PositionSuggestion(
        sector_id="industry:semiconductor",
        sector_name="Semiconductor",
        sector_rank=1,
        etf_code="512480",
        etf_status="best_supported",
        sector_cap_pct=12.0,
        etf_cap_pct=12.0,
        joint_confidence=0.8,
    )
    plan = PositionPlan(
        as_of=NOW,
        regime="selective",
        total_position_min_pct=35.0,
        total_position_max_pct=60.0,
        suggestions=(suggestion,),
        correlation_coverage=1.0,
        confidence=0.8,
    )
    snapshot = RadarRunSnapshot(
        run_key="cn:20260721T060000Z:manual",
        market="cn",
        trigger="manual",
        as_of=NOW,
        quality="partial",
        scoring_version="cn-v1",
        sectors=(),
        provider_trace=(),
        etfs=(selection,),
        regime=regime,
        position_plan=plan,
    )
    legacy = RadarRunSnapshot(
        run_key="cn:20260723T070000Z:manual",
        market="cn",
        trigger="manual",
        as_of=NOW,
        quality="partial",
        scoring_version="cn-v1",
        sectors=(),
        provider_trace=(),
    )

    assert snapshot.etfs == (selection,)
    assert legacy.etfs == ()
    assert legacy.regime is None
    assert legacy.position_plan is None


def test_phase_2b_contracts_reject_invalid_evidence_and_plan_relationships() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        complete_etf_observation(observed_at=datetime(2026, 7, 21, 6, 0))

    with pytest.raises(ValidationError):
        complete_etf_observation(current_price=float("nan"))

    with pytest.raises(ValidationError, match="strictly increasing"):
        complete_etf_observation(
            daily_return_dates_60=(date(2026, 1, 1),) * 60,
            daily_returns_60=(0.01,) * 60,
        )

    selection = complete_etf_selection()
    with pytest.raises(ValidationError, match="duplicate ETF"):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=(),
            provider_trace=(),
            etfs=(selection, selection),
        )

    with pytest.raises(ValidationError):
        PositionSuggestion(
            sector_id="industry:semiconductor",
            sector_name="Semiconductor",
            sector_rank=1,
            etf_code="512480",
            etf_status="rejected",
            sector_cap_pct=10.0,
            etf_cap_pct=10.0,
            joint_confidence=0.8,
        )

    with pytest.raises(ValidationError, match="must not exceed"):
        PositionPlan(
            as_of=NOW,
            regime="risk_on",
            total_position_min_pct=80.0,
            total_position_max_pct=60.0,
            correlation_coverage=1.0,
            confidence=0.8,
        )


def test_run_snapshot_rejects_duplicate_etf_codes_across_sectors() -> None:
    semiconductor = complete_etf_selection()
    technology = complete_etf_selection(sector_id="concept:technology")

    with pytest.raises(ValidationError, match="duplicate ETF code"):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=(),
            provider_trace=(),
            etfs=(semiconductor, technology),
        )


def test_run_snapshot_requires_position_plan_to_reference_its_etf_selection() -> None:
    selection = complete_etf_selection()
    regime = MarketRegimeAssessment(
        as_of=NOW,
        regime="selective",
        confidence=0.8,
        coverage=0.8,
    )
    plan = PositionPlan(
        as_of=NOW,
        regime="selective",
        total_position_min_pct=35.0,
        total_position_max_pct=60.0,
        suggestions=(
            PositionSuggestion(
                sector_id="industry:semiconductor",
                sector_name="Semiconductor",
                sector_rank=1,
                etf_code="512481",
                etf_status="candidate",
                sector_cap_pct=12.0,
                etf_cap_pct=12.0,
                joint_confidence=0.8,
            ),
        ),
        correlation_groups=(CorrelationGroup(etf_codes=("512480", "512481")),),
        correlation_coverage=1.0,
        confidence=0.8,
    )

    with pytest.raises(ValidationError, match="position suggestion ETF identity"):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=(),
            provider_trace=(),
            etfs=(selection,),
            regime=regime,
            position_plan=plan,
        )
