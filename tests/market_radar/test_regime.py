from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import fmean

import pytest

from src.market_radar.models import SectorObservation, SectorScore
from src.market_radar.policy_config import RegimeConfig
from src.market_radar.regime import _regime, assess_market_regime


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
DATA_DATE = date(2026, 7, 22)


def observation(
    sector_id: str,
    *,
    benchmark_return: float | None = 2.0,
    raw_reference: dict[str, object] | None = None,
    **values: object,
) -> SectorObservation:
    payload: dict[str, object] = {
        "sector_id": sector_id,
        "kind": "industry",
        "name": sector_id,
        "observed_at": NOW,
        "source": "fixture",
        "freshness_seconds": 30,
        "quality": "complete",
        "return_1d_pct": 1.0,
        "return_5d_pct": 3.0,
        "return_20d_pct": 8.0,
        "benchmark_return_20d_pct": benchmark_return,
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
        "raw_reference": raw_reference
        or {
            "schema": "market-radar-observation-v2a",
            "benchmark_code": "000985",
            "data_date": DATA_DATE.isoformat(),
        },
    }
    payload.update(values)
    payload["missing_fields"] = tuple(
        field
        for field in SectorObservation.tracked_metric_fields
        if payload.get(field) is None
    )
    return SectorObservation(**payload)


def score(
    sector_id: str,
    *,
    confidence: float = 0.8,
    state: str = "leading",
    risk_reasons: tuple[str, ...] = (),
    **observation_values: object,
) -> SectorScore:
    source = observation(sector_id, **observation_values)
    return SectorScore(
        sector_id=source.sector_id,
        name=source.name,
        kind=source.kind,
        scoring_version="cn-v1",
        gross_score=80.0,
        risk_deduction=0.0,
        score=80.0,
        confidence=confidence,
        state=state,  # type: ignore[arg-type]
        factors={
            "trend_momentum": 20.0,
            "relative_strength": 16.0,
            "capital_flow": 16.0,
            "breadth": 12.0,
            "liquidity_expansion": 8.0,
            "catalyst": 8.0,
        },
        risk_reasons=risk_reasons,
        missing_fields=source.missing_fields,
        source=source.source,
        observed_at=source.observed_at,
        quality=source.quality,
        observation=source.model_dump(mode="json"),
    )


def scores(count: int, *, prefix: str = "sector", **kwargs: object) -> list[SectorScore]:
    return [
        score(f"industry:{prefix}-{index:02d}", **kwargs)
        for index in range(count)
    ]


def test_fewer_than_minimum_cohort_is_insufficient_data() -> None:
    assessment = assess_market_regime(scores(4), RegimeConfig(), NOW)

    assert assessment.regime == "insufficient_data"
    assert assessment.score is None
    assert "cohort_below_minimum" in assessment.reasons


def test_coverage_below_minimum_is_insufficient_data() -> None:
    eligible = scores(5_999)
    excluded = scores(4_001, prefix="excluded", state="insufficient_data")

    assessment = assess_market_regime([*eligible, *excluded], RegimeConfig(), NOW)

    assert assessment.regime == "insufficient_data"
    assert assessment.score is None
    assert assessment.coverage == 0.5999
    assert "coverage_below_minimum" in assessment.reasons


def test_exact_minimum_coverage_proceeds() -> None:
    eligible = scores(6)
    excluded = scores(4, prefix="excluded", state="insufficient_data")

    assessment = assess_market_regime(
        [*eligible, *excluded], RegimeConfig(minimum_coverage=0.6), NOW
    )

    assert assessment.regime == "risk_on"
    assert assessment.score == 92.5
    assert assessment.coverage == 0.6
    assert len(assessment.cohort_sector_ids) == 6


def test_raw_coverage_below_minimum_does_not_pass_after_rounding() -> None:
    eligible = scores(10_000)
    excluded = scores(6_667, prefix="excluded", state="insufficient_data")

    assessment = assess_market_regime([*eligible, *excluded], RegimeConfig(), NOW)

    assert 10_000 / 16_667 < 0.6
    assert assessment.coverage == 0.6
    assert assessment.regime == "insufficient_data"
    assert assessment.score is None
    assert "coverage_below_minimum" in assessment.reasons


def test_exact_minimum_cohort_count_proceeds() -> None:
    assessment = assess_market_regime(scores(5), RegimeConfig(), NOW)

    assert assessment.regime == "risk_on"
    assert assessment.score == 92.5
    assert len(assessment.cohort_sector_ids) == 5


def test_missing_canonical_benchmark_is_insufficient_data() -> None:
    assessment = assess_market_regime(
        scores(5, benchmark_return=None), RegimeConfig(), NOW
    )

    assert assessment.regime == "insufficient_data"
    assert assessment.score is None
    assert "benchmark_missing" in assessment.reasons
    assert "benchmark_return_20d_pct" in assessment.missing_fields


def test_stale_critical_price_is_excluded_from_cohort() -> None:
    assessment = assess_market_regime(
        [*scores(4), score("industry:stale", risk_reasons=("critical_price_stale",))],
        RegimeConfig(),
        NOW,
    )

    assert assessment.regime == "insufficient_data"
    assert assessment.score is None
    assert assessment.excluded_sector_reasons["industry:stale"] == (
        "critical_price_stale",
    )


def test_score_and_persisted_observation_must_have_the_same_sector_id() -> None:
    mismatched = score("industry:score").model_copy(
        update={"observation": observation("industry:observation").model_dump(mode="json")}
    )

    with pytest.raises(ValueError, match="identity"):
        assess_market_regime([mismatched], RegimeConfig(), NOW)


@pytest.mark.parametrize(
    "raw_reference",
    [
        {
            "schema": "market-radar-observation-v2a",
            "benchmark_code": "000985",
            "data_date": "2026-07-21",
        },
        {
            "schema": "market-radar-observation-v2a",
            "benchmark_code": "000985",
            "data_date": DATA_DATE.isoformat(),
        },
    ],
)
def test_conflicting_canonical_benchmark_evidence_is_rejected(
    raw_reference: dict[str, object],
) -> None:
    first = score("industry:first")
    second = score("industry:second", raw_reference=raw_reference)
    remainder = scores(3)

    if raw_reference["data_date"] == DATA_DATE.isoformat():
        second = score(
            "industry:second",
            benchmark_return=3.0,
            raw_reference=raw_reference,
        )

    with pytest.raises(ValueError, match="conflicting benchmark evidence"):
        assess_market_regime([first, second, *remainder], RegimeConfig(), NOW)


@pytest.mark.parametrize(
    ("benchmark_return", "expected"),
    [
        (5.0, 100.0),
        (2.0, 75.0),
        (0.0, 55.0),
        (-0.0001, 35.0),
        (-2.0, 0.0),
    ],
)
def test_benchmark_trend_uses_approved_piecewise_boundaries(
    benchmark_return: float, expected: float
) -> None:
    assessment = assess_market_regime(
        scores(5, benchmark_return=benchmark_return), RegimeConfig(), NOW
    )

    assert assessment.components is not None
    assert assessment.components.benchmark_trend == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (75.0, "risk_on"),
        (55.0, "selective"),
        (35.0, "defensive"),
        (34.9999, "risk_off"),
    ],
)
def test_regime_thresholds_are_inclusive(
    value: float, expected: str
) -> None:
    assert _regime(value, RegimeConfig()) == expected


def test_diffusions_use_the_same_eligible_cohort_as_the_denominator() -> None:
    cohort = [
        score("industry:one", return_20d_pct=1.0, capital_flow_5d=1.0, turnover_ratio_20d=1.0),
        score("industry:two", return_20d_pct=0.0, capital_flow_5d=1.0, turnover_ratio_20d=0.9),
        score("industry:three", return_20d_pct=-1.0, capital_flow_5d=-1.0, turnover_ratio_20d=1.1),
        score("industry:four", return_20d_pct=1.0, capital_flow_5d=1.0, turnover_ratio_20d=0.5),
        score("industry:five", return_20d_pct=0.0, capital_flow_5d=-1.0, turnover_ratio_20d=1.0),
    ]
    cohort[3] = cohort[3].model_copy(update={"state": "weakening"})
    cohort[4] = cohort[4].model_copy(update={"state": "avoid"})

    assessment = assess_market_regime(cohort, RegimeConfig(), NOW)

    assert assessment.components is not None
    assert assessment.components.positive_sector_diffusion == 40.0
    assert assessment.components.flow_diffusion == 60.0
    assert assessment.components.liquidity_diffusion == 60.0
    assert assessment.components.non_risk_sector_share == 60.0


def test_confidence_uses_coverage_times_mean_cohort_confidence_at_four_decimals() -> None:
    cohort = [
        score(f"industry:cohort-{index}", confidence=confidence)
        for index, confidence in enumerate((0.8123, 0.7345, 0.6789, 0.9567, 0.8456, 0.7234))
    ]
    excluded = scores(4, prefix="excluded", state="insufficient_data")

    assessment = assess_market_regime(
        [*cohort, *excluded], RegimeConfig(minimum_coverage=0.6), NOW
    )

    expected_confidence = round(
        assessment.coverage * fmean(item.confidence for item in cohort), 4
    )
    assert assessment.confidence == expected_confidence
