from datetime import datetime, timezone
from math import inf, nan

import pytest

from src.market_radar.models import SectorObservation
from src.market_radar.ranking import (
    _COVERAGE_WEIGHTS,
    RankingConfig,
    score_sectors,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def observation(name: str, sector_id: str, **values: object) -> SectorObservation:
    payload: dict[str, object] = {
        "sector_id": sector_id,
        "kind": "industry",
        "name": name,
        "observed_at": NOW,
        "source": "fixture",
        "freshness_seconds": 30,
        "quality": "complete",
        "return_1d_pct": 1.0,
        "return_5d_pct": 3.0,
        "return_20d_pct": 8.0,
        "benchmark_return_20d_pct": 2.0,
        "capital_flow_1d": 3.0,
        "capital_flow_5d": 8.0,
        "capital_flow_20d": 15.0,
        "turnover_ratio_20d": 1.2,
        "up_count": 8,
        "down_count": 2,
        "flat_count": 0,
        "volatility_ratio_20d": 1.0,
        "distance_ma20_pct": 4.0,
        "price_flow_divergence": False,
        "concentration_ratio": 0.25,
        "catalyst_score": 0.5,
    }
    payload.update(values)
    payload["missing_fields"] = tuple(
        field
        for field in SectorObservation.tracked_metric_fields
        if payload.get(field) is None
    )
    return SectorObservation(**payload)


def test_scores_are_deterministic_and_sorted() -> None:
    weak = observation(
        "Weak",
        "industry:weak",
        return_5d_pct=-3.0,
        return_20d_pct=-8.0,
        capital_flow_1d=-2.0,
        capital_flow_5d=-5.0,
        capital_flow_20d=-10.0,
        up_count=2,
        down_count=8,
    )
    strong = observation("Strong", "industry:strong")

    first = score_sectors([weak, strong], RankingConfig())
    second = score_sectors([weak, strong], RankingConfig())

    assert first == second
    assert [item.sector_id for item in first] == [
        "industry:strong",
        "industry:weak",
    ]
    assert first[0].score > first[1].score


def test_missing_data_lowers_confidence_without_becoming_zero_strength() -> None:
    partial = observation(
        "Partial",
        "industry:partial",
        quality="partial",
        return_5d_pct=None,
        return_20d_pct=None,
        benchmark_return_20d_pct=None,
        capital_flow_1d=None,
        capital_flow_5d=None,
        capital_flow_20d=None,
        up_count=None,
        down_count=None,
        flat_count=None,
    )

    result = score_sectors([partial], RankingConfig())[0]

    assert result.factors.trend_momentum > 0
    assert result.gross_score == 54.4444
    assert result.confidence == 0.4487
    assert result.state == "neutral"


def test_stale_quality_is_insufficient_even_while_fresh() -> None:
    stale = observation(
        "Stale",
        "industry:stale",
        quality="stale",
        freshness_seconds=30,
    )

    result = score_sectors(
        [stale], RankingConfig(stale_after_seconds=2700)
    )[0]

    assert result.state == "insufficient_data"
    assert "critical_price_stale" in result.risk_reasons


def test_stale_age_is_insufficient_even_with_complete_quality() -> None:
    stale = observation(
        "Stale",
        "industry:stale",
        quality="complete",
        freshness_seconds=4000,
    )

    result = score_sectors(
        [stale], RankingConfig(stale_after_seconds=2700)
    )[0]

    assert result.state == "insufficient_data"
    assert "critical_price_stale" in result.risk_reasons


def test_exact_stale_threshold_remains_eligible() -> None:
    current = observation(
        "Current",
        "industry:current",
        quality="complete",
        freshness_seconds=2700,
    )

    result = score_sectors(
        [current], RankingConfig(stale_after_seconds=2700)
    )[0]

    assert result.state != "insufficient_data"
    assert "critical_price_stale" not in result.risk_reasons


def test_risk_deductions_are_capped_at_thirty() -> None:
    risky = observation(
        "Risky",
        "industry:risky",
        volatility_ratio_20d=3.0,
        distance_ma20_pct=25.0,
        price_flow_divergence=True,
        concentration_ratio=0.95,
    )

    result = score_sectors([risky], RankingConfig())[0]

    assert result.risk_deduction == 30.0
    assert 0 <= result.score <= 100


def test_flow_score_uses_within_source_percentiles_not_absolute_units() -> None:
    base = [
        observation(
            "A",
            "industry:a",
            capital_flow_1d=1.0,
            capital_flow_5d=2.0,
            capital_flow_20d=3.0,
        ),
        observation(
            "B",
            "industry:b",
            capital_flow_1d=2.0,
            capital_flow_5d=4.0,
            capital_flow_20d=6.0,
        ),
    ]
    scaled = [
        item.model_copy(
            update={
                "capital_flow_1d": item.capital_flow_1d * 10_000,
                "capital_flow_5d": item.capital_flow_5d * 10_000,
                "capital_flow_20d": item.capital_flow_20d * 10_000,
            }
        )
        for item in base
    ]

    base_scores = {
        item.sector_id: item.factors.capital_flow
        for item in score_sectors(base, RankingConfig())
    }
    scaled_scores = {
        item.sector_id: item.factors.capital_flow
        for item in score_sectors(scaled, RankingConfig())
    }

    assert base_scores == scaled_scores


def test_zero_is_rankable_evidence_while_missing_is_not() -> None:
    unavailable_metrics = {
        field: None for field in SectorObservation.tracked_metric_fields
    }
    missing = observation(
        "Missing",
        "industry:missing",
        quality="unavailable",
        **unavailable_metrics,
    )
    zero = observation(
        "Zero",
        "industry:zero",
        quality="partial",
        **{**unavailable_metrics, "return_1d_pct": 0.0},
    )

    by_id = {
        item.sector_id: item
        for item in score_sectors([missing, zero], RankingConfig())
    }

    assert by_id["industry:zero"].factors.trend_momentum == 12.5
    assert by_id["industry:missing"].factors.trend_momentum == 0.0
    assert by_id["industry:zero"].confidence == 0.0641
    assert by_id["industry:missing"].confidence == 0.0


def test_leading_threshold_is_inclusive_at_seventy_five() -> None:
    leader = observation(
        "Leader",
        "industry:leader",
        turnover_ratio_20d=0.5,
        up_count=2,
        down_count=1,
        catalyst_score=0.0,
    )
    laggard = observation(
        "Laggard",
        "industry:laggard",
        return_1d_pct=-10.0,
        return_5d_pct=-10.0,
        return_20d_pct=-10.0,
        benchmark_return_20d_pct=10.0,
        capital_flow_1d=-10.0,
        capital_flow_5d=-10.0,
        capital_flow_20d=-10.0,
        turnover_ratio_20d=0.5,
        up_count=0,
        down_count=1,
        catalyst_score=0.0,
    )

    result = score_sectors([leader, laggard], RankingConfig())[0]

    assert result.score == 75.0
    assert result.confidence == 1.0
    assert result.state == "leading"


def test_score_is_clamped_at_zero() -> None:
    unavailable_metrics = {
        field: None for field in SectorObservation.tracked_metric_fields
    }
    risky = observation(
        "Risk Floor",
        "industry:risk-floor",
        quality="partial",
        **{
            **unavailable_metrics,
            "volatility_ratio_20d": 3.0,
            "distance_ma20_pct": 25.0,
            "price_flow_divergence": True,
            "concentration_ratio": 0.95,
        },
    )

    result = score_sectors([risky], RankingConfig())[0]

    assert result.gross_score == 0.0
    assert result.risk_deduction == 30.0
    assert result.score == 0.0


def test_observation_evidence_uses_json_serialization() -> None:
    source = observation("Evidence", "industry:evidence")

    result = score_sectors([source], RankingConfig())[0]
    serialized_evidence = result.model_dump(mode="json")["observation"]

    assert serialized_evidence == source.model_dump(mode="json")
    assert serialized_evidence["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    ("missing_field", "expected_confidence"),
    [
        ("return_1d_pct", 0.9359),
        ("return_5d_pct", 0.9359),
        ("return_20d_pct", 0.859),
        ("benchmark_return_20d_pct", 0.9231),
        ("capital_flow_1d", 0.9487),
        ("capital_flow_5d", 0.9487),
        ("capital_flow_20d", 0.9487),
        ("turnover_ratio_20d", 0.9231),
        ("up_count", 0.9615),
        ("down_count", 0.9615),
        ("flat_count", 0.9615),
        ("volatility_ratio_20d", 0.9231),
        ("distance_ma20_pct", 0.9385),
        ("price_flow_divergence", 0.9538),
        ("concentration_ratio", 0.9538),
        ("catalyst_score", 0.9231),
    ],
)
def test_each_tracked_field_independently_lowers_confidence(
    missing_field: str,
    expected_confidence: float,
) -> None:
    partial = observation(
        "Granular",
        "industry:granular",
        **{missing_field: None},
    )

    result = score_sectors([partial], RankingConfig())[0]

    assert result.confidence == expected_confidence


def test_confidence_coverage_has_130_points_of_authority() -> None:
    assert _COVERAGE_WEIGHTS["price_flow_divergence"] == 6.0
    assert sum(_COVERAGE_WEIGHTS.values()) == pytest.approx(130.0)


def test_phase2a_full_market_evidence_without_catalyst_has_approved_confidence() -> None:
    enriched = observation(
        "Enriched",
        "industry:enriched",
        quality="partial",
        catalyst_score=None,
    )

    result = score_sectors([enriched], RankingConfig())[0]

    assert result.confidence == 0.9231


def test_phase1_sparse_partial_confidence_is_coverage_based() -> None:
    sparse = observation(
        "Sparse",
        "industry:sparse",
        quality="partial",
        return_5d_pct=None,
        return_20d_pct=None,
        benchmark_return_20d_pct=None,
        capital_flow_1d=None,
        capital_flow_5d=None,
        capital_flow_20d=None,
        up_count=None,
        down_count=None,
        flat_count=None,
    )

    expected_coverage = sum(
        weight
        for field, weight in _COVERAGE_WEIGHTS.items()
        if getattr(sparse, field) is not None
    ) / sum(_COVERAGE_WEIGHTS.values())

    result = score_sectors([sparse], RankingConfig())[0]

    assert result.confidence == round(expected_coverage, 4)


def test_relative_inputs_contribute_independent_cumulative_coverage() -> None:
    sector_missing = observation(
        "Sector Missing",
        "industry:sector-missing",
        return_20d_pct=None,
    )
    benchmark_missing = observation(
        "Benchmark Missing",
        "industry:benchmark-missing",
        benchmark_return_20d_pct=None,
    )
    both_missing = observation(
        "Both Missing",
        "industry:both-missing",
        return_20d_pct=None,
        benchmark_return_20d_pct=None,
    )

    by_id = {
        item.sector_id: item
        for item in score_sectors(
            [sector_missing, benchmark_missing, both_missing],
            RankingConfig(),
        )
    }

    assert by_id["industry:sector-missing"].confidence == 0.859
    assert by_id["industry:benchmark-missing"].confidence == 0.9231
    assert by_id["industry:both-missing"].confidence == 0.7821


def test_multiple_missing_fields_reduce_confidence_additively() -> None:
    partial = observation(
        "Multiple Missing",
        "industry:multiple-missing",
        return_5d_pct=None,
        flat_count=None,
        volatility_ratio_20d=None,
    )

    result = score_sectors([partial], RankingConfig())[0]

    assert result.confidence == 0.8205


def test_incomplete_breadth_is_excluded_from_strength_denominator() -> None:
    partial = observation(
        "Breadth Missing",
        "industry:breadth-missing",
        flat_count=None,
    )

    result = score_sectors([partial], RankingConfig())[0]

    assert result.factors.breadth == 0.0
    assert result.gross_score == 52.3529


def test_no_available_factor_has_zero_strength_and_confidence() -> None:
    unavailable_metrics = {
        field: None for field in SectorObservation.tracked_metric_fields
    }
    unavailable = observation(
        "Unavailable",
        "industry:unavailable",
        quality="complete",
        **unavailable_metrics,
    )

    result = score_sectors([unavailable], RankingConfig())[0]

    assert result.gross_score == 0.0
    assert result.confidence == 0.0
    assert result.state == "insufficient_data"


def test_duplicate_sector_ids_are_rejected_across_sources() -> None:
    first = observation("Duplicate", "industry:duplicate", source="source-a")
    second = observation("Duplicate", "industry:duplicate", source="source-b")

    with pytest.raises(ValueError, match="duplicate sector_id"):
        score_sectors([first, second], RankingConfig())


@pytest.mark.parametrize(
    "config_values",
    [
        {"scoring_version": "cn-v2"},
        {"min_confidence": -0.1},
        {"min_confidence": True},
        {"min_confidence": nan},
        {"leading_confidence": 1.1},
        {"leading_confidence": inf},
        {"min_confidence": 0.8, "leading_confidence": 0.7},
        {"stale_after_seconds": -1},
        {"stale_after_seconds": 1.5},
        {"stale_after_seconds": True},
    ],
)
def test_ranking_config_rejects_invalid_values_on_construction(
    config_values: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RankingConfig(**config_values)


def test_percentiles_are_isolated_by_source() -> None:
    observations = [
        observation(
            "A Low",
            "industry:a-low",
            source="source-a",
            capital_flow_1d=1.0,
            capital_flow_5d=1.0,
            capital_flow_20d=1.0,
        ),
        observation(
            "A High",
            "industry:a-high",
            source="source-a",
            capital_flow_1d=2.0,
            capital_flow_5d=2.0,
            capital_flow_20d=2.0,
        ),
        observation(
            "B Low",
            "industry:b-low",
            source="source-b",
            capital_flow_1d=1_000.0,
            capital_flow_5d=1_000.0,
            capital_flow_20d=1_000.0,
        ),
        observation(
            "B High",
            "industry:b-high",
            source="source-b",
            capital_flow_1d=2_000.0,
            capital_flow_5d=2_000.0,
            capital_flow_20d=2_000.0,
        ),
    ]

    by_id = {
        item.sector_id: item
        for item in score_sectors(observations, RankingConfig())
    }

    assert by_id["industry:a-low"].factors.capital_flow == 0.0
    assert by_id["industry:b-low"].factors.capital_flow == 0.0
    assert by_id["industry:a-high"].factors.capital_flow == 20.0
    assert by_id["industry:b-high"].factors.capital_flow == 20.0


def test_tied_values_receive_the_average_percentile_rank() -> None:
    observations = [
        observation(
            "Tie A",
            "industry:tie-a",
            capital_flow_1d=1.0,
            capital_flow_5d=1.0,
            capital_flow_20d=1.0,
        ),
        observation(
            "Tie B",
            "industry:tie-b",
            capital_flow_1d=1.0,
            capital_flow_5d=1.0,
            capital_flow_20d=1.0,
        ),
        observation(
            "High",
            "industry:high",
            capital_flow_1d=3.0,
            capital_flow_5d=3.0,
            capital_flow_20d=3.0,
        ),
    ]

    by_id = {
        item.sector_id: item
        for item in score_sectors(observations, RankingConfig())
    }

    assert by_id["industry:tie-a"].factors.capital_flow == 5.0
    assert by_id["industry:tie-b"].factors.capital_flow == 5.0
    assert by_id["industry:high"].factors.capital_flow == 20.0
