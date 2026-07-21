from datetime import datetime, timezone

from src.market_radar.models import SectorObservation
from src.market_radar.ranking import RankingConfig, score_sectors


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
    assert 0 < result.confidence < 0.4
    assert result.state == "insufficient_data"


def test_stale_critical_price_is_insufficient() -> None:
    stale = observation(
        "Stale",
        "industry:stale",
        quality="stale",
        freshness_seconds=4000,
    )

    result = score_sectors(
        [stale], RankingConfig(stale_after_seconds=2700)
    )[0]

    assert result.state == "insufficient_data"
    assert "critical_price_stale" in result.risk_reasons


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
    assert by_id["industry:zero"].confidence == 0.2
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
            "concentration_ratio": 0.95,
        },
        price_flow_divergence=True,
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
