from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import Callable, Literal

from src.market_radar.models import (
    FactorBreakdown,
    SectorObservation,
    SectorScore,
    SectorState,
)


_FACTOR_WEIGHTS = {
    "trend_momentum": 25.0,
    "relative_strength": 20.0,
    "capital_flow": 20.0,
    "breadth": 15.0,
    "liquidity_expansion": 10.0,
    "catalyst": 10.0,
}


@dataclass(frozen=True)
class RankingConfig:
    scoring_version: Literal["cn-v1"] = "cn-v1"
    min_confidence: float = 0.4
    leading_confidence: float = 0.7
    stale_after_seconds: int = 2700

    def __post_init__(self) -> None:
        if self.scoring_version != "cn-v1":
            raise ValueError("scoring_version must be 'cn-v1'")
        for field_name in ("min_confidence", "leading_confidence"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
            ):
                raise ValueError(f"{field_name} must be a finite number")
        if not 0 <= self.min_confidence <= self.leading_confidence <= 1:
            raise ValueError(
                "confidence thresholds must satisfy "
                "0 <= min_confidence <= leading_confidence <= 1"
            )
        if (
            isinstance(self.stale_after_seconds, bool)
            or not isinstance(self.stale_after_seconds, int)
            or self.stale_after_seconds < 0
        ):
            raise ValueError("stale_after_seconds must be a non-negative integer")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear(value: float | None, low: float, high: float, points: float) -> float:
    if value is None:
        return 0.0
    if high == low:
        return points
    return round(_clamp((value - low) / (high - low), 0.0, 1.0) * points, 4)


def _mean_available(values: list[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return fmean(available) if available else None


def _percentile_map(
    observations: list[SectorObservation],
    value_getter: Callable[[SectorObservation], float | None],
) -> dict[str, float]:
    result: dict[str, float] = {}
    by_source: dict[str, list[tuple[str, float]]] = {}
    for item in observations:
        value = value_getter(item)
        if value is not None:
            by_source.setdefault(item.source, []).append(
                (item.sector_id, float(value))
            )
    for rows in by_source.values():
        ordered = sorted(rows, key=lambda pair: (pair[1], pair[0]))
        if len(ordered) == 1:
            result[ordered[0][0]] = 0.5
            continue
        positions_by_value: dict[float, list[int]] = {}
        for position, (_, value) in enumerate(ordered):
            positions_by_value.setdefault(value, []).append(position)
        for sector_id, value in ordered:
            average_position = fmean(positions_by_value[value])
            result[sector_id] = average_position / (len(ordered) - 1)
    return result


def _factor_scores(
    item: SectorObservation,
    *,
    availability: dict[str, bool],
    trend_percentiles: dict[str, float],
    relative_percentiles: dict[str, float],
    flow_percentiles: dict[str, float],
) -> FactorBreakdown:
    total = (
        (item.up_count or 0) + (item.down_count or 0) + (item.flat_count or 0)
        if availability["breadth"]
        else 0
    )
    breadth = (
        round((item.up_count or 0) / total * 15.0, 4) if total > 0 else 0.0
    )
    return FactorBreakdown(
        trend_momentum=round(
            trend_percentiles.get(item.sector_id, 0.0) * 25.0, 4
        ),
        relative_strength=round(
            relative_percentiles.get(item.sector_id, 0.0) * 20.0, 4
        ),
        capital_flow=round(
            flow_percentiles.get(item.sector_id, 0.0) * 20.0, 4
        ),
        breadth=breadth,
        liquidity_expansion=_linear(item.turnover_ratio_20d, 0.5, 1.5, 10.0),
        catalyst=round((item.catalyst_score or 0.0) * 10.0, 4),
    )


def _factor_availability(item: SectorObservation) -> dict[str, bool]:
    return {
        "trend_momentum": any(
            value is not None
            for value in [
                item.return_1d_pct,
                item.return_5d_pct,
                item.return_20d_pct,
            ]
        ),
        "relative_strength": (
            item.return_20d_pct is not None
            and item.benchmark_return_20d_pct is not None
        ),
        "capital_flow": any(
            value is not None
            for value in [
                item.capital_flow_1d,
                item.capital_flow_5d,
                item.capital_flow_20d,
            ]
        ),
        "breadth": all(
            value is not None
            for value in [item.up_count, item.down_count, item.flat_count]
        ),
        "liquidity_expansion": item.turnover_ratio_20d is not None,
        "catalyst": item.catalyst_score is not None,
    }


def _risk_deduction(item: SectorObservation) -> tuple[float, list[str]]:
    deduction = 0.0
    reasons: list[str] = []
    if item.volatility_ratio_20d is not None and item.volatility_ratio_20d > 1.0:
        deduction += _linear(item.volatility_ratio_20d, 1.0, 2.0, 10.0)
        reasons.append("volatility_shock")
    if item.distance_ma20_pct is not None and item.distance_ma20_pct > 8.0:
        deduction += _linear(item.distance_ma20_pct, 8.0, 20.0, 8.0)
        reasons.append("trend_overheating")
    if item.price_flow_divergence:
        deduction += 6.0
        reasons.append("price_flow_divergence")
    if item.concentration_ratio is not None and item.concentration_ratio > 0.6:
        deduction += _linear(item.concentration_ratio, 0.6, 0.9, 6.0)
        reasons.append("crowding_concentration")
    return round(min(30.0, deduction), 4), reasons


def _confidence(item: SectorObservation) -> float:
    return_horizons = [
        item.return_1d_pct,
        item.return_5d_pct,
        item.return_20d_pct,
    ]
    flow_horizons = [
        item.capital_flow_1d,
        item.capital_flow_5d,
        item.capital_flow_20d,
    ]
    breadth_counts = [item.up_count, item.down_count, item.flat_count]
    weighted_presence = (
        25.0
        * sum(value is not None for value in return_horizons)
        / len(return_horizons)
        + (
            20.0
            if item.return_20d_pct is not None
            and item.benchmark_return_20d_pct is not None
            else 0.0
        )
        + 20.0
        * sum(value is not None for value in flow_horizons)
        / len(flow_horizons)
        + 15.0
        * sum(value is not None for value in breadth_counts)
        / len(breadth_counts)
        + (10.0 if item.turnover_ratio_20d is not None else 0.0)
        + (10.0 if item.catalyst_score is not None else 0.0)
    )
    quality_multiplier = {
        "complete": 1.0,
        "partial": 0.8,
        "stale": 0.4,
        "unavailable": 0.0,
    }[item.quality]
    return round(weighted_presence / 100.0 * quality_multiplier, 4)


def _state(
    score: float,
    confidence: float,
    config: RankingConfig,
    stale: bool,
) -> SectorState:
    if stale or confidence < config.min_confidence:
        return "insufficient_data"
    if score >= 75.0 and confidence >= config.leading_confidence:
        return "leading"
    if score >= 60.0:
        return "improving"
    if score >= 40.0:
        return "neutral"
    if score >= 25.0:
        return "weakening"
    return "avoid"


def score_sectors(
    observations: list[SectorObservation],
    config: RankingConfig,
) -> list[SectorScore]:
    sector_ids = [item.sector_id for item in observations]
    duplicate_sector_ids = sorted(
        sector_id
        for sector_id in set(sector_ids)
        if sector_ids.count(sector_id) > 1
    )
    if duplicate_sector_ids:
        raise ValueError(
            "duplicate sector_id inputs: " + ", ".join(duplicate_sector_ids)
        )

    trend_percentiles = _percentile_map(
        observations,
        lambda item: _mean_available(
            [item.return_5d_pct, item.return_20d_pct, item.return_1d_pct]
        ),
    )
    relative_percentiles = _percentile_map(
        observations,
        lambda item: (
            item.return_20d_pct - item.benchmark_return_20d_pct
            if item.return_20d_pct is not None
            and item.benchmark_return_20d_pct is not None
            else None
        ),
    )
    flow_percentiles = _percentile_map(
        observations,
        lambda item: _mean_available(
            [
                item.capital_flow_1d,
                item.capital_flow_5d,
                item.capital_flow_20d,
            ]
        ),
    )
    results: list[SectorScore] = []
    for item in observations:
        availability = _factor_availability(item)
        factors = _factor_scores(
            item,
            availability=availability,
            trend_percentiles=trend_percentiles,
            relative_percentiles=relative_percentiles,
            flow_percentiles=flow_percentiles,
        )
        available_weight = sum(
            weight
            for factor_name, weight in _FACTOR_WEIGHTS.items()
            if availability[factor_name]
        )
        earned_points = sum(
            getattr(factors, factor_name)
            for factor_name in _FACTOR_WEIGHTS
            if availability[factor_name]
        )
        gross = (
            round(_clamp(earned_points / available_weight * 100.0, 0.0, 100.0), 4)
            if available_weight > 0
            else 0.0
        )
        deduction, reasons = _risk_deduction(item)
        score = round(_clamp(gross - deduction, 0.0, 100.0), 4)
        confidence = _confidence(item)
        stale = (
            item.quality == "stale"
            or item.freshness_seconds > config.stale_after_seconds
        )
        if stale:
            reasons.append("critical_price_stale")
        results.append(
            SectorScore(
                sector_id=item.sector_id,
                name=item.name,
                kind=item.kind,
                scoring_version=config.scoring_version,
                gross_score=gross,
                risk_deduction=deduction,
                score=score,
                confidence=confidence,
                state=_state(score, confidence, config, stale),
                factors=factors,
                risk_reasons=sorted(set(reasons)),
                missing_fields=sorted(set(item.missing_fields)),
                source=item.source,
                observed_at=item.observed_at,
                quality=item.quality,
                observation=item.model_dump(mode="json"),
            )
        )
    return sorted(results, key=lambda item: (-item.score, item.sector_id))
