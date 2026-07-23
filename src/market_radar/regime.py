from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import fmean
from src.market_radar.models import (
    MarketRegime,
    MarketRegimeAssessment,
    RegimeComponents,
    SectorObservation,
    SectorScore,
)
from src.market_radar.policy_config import RegimeConfig


_COHORT_METRICS = (
    "return_20d_pct",
    "capital_flow_5d",
    "turnover_ratio_20d",
)


@dataclass(frozen=True)
class _CohortSector:
    score: SectorScore
    observation: SectorObservation


@dataclass(frozen=True)
class _BenchmarkEvidence:
    code: str
    data_date: date
    return_20d_pct: float


def _benchmark_score(benchmark_return: float) -> float:
    if benchmark_return >= 5.0:
        return 100.0
    if benchmark_return >= 2.0:
        return 75.0
    if benchmark_return >= 0.0:
        return 50.0
    if benchmark_return > -2.0:
        return 25.0
    return 0.0


def _share(
    cohort: Sequence[_CohortSector],
    predicate: Callable[[_CohortSector], bool],
) -> float:
    return round(sum(predicate(item) for item in cohort) / len(cohort) * 100.0, 4)


def _cohort_reason(score: SectorScore, observation: SectorObservation) -> tuple[str, ...]:
    reasons = [
        field_name
        for field_name in _COHORT_METRICS
        if getattr(observation, field_name) is None
    ]
    if score.state == "insufficient_data":
        reasons.append("sector_state_insufficient_data")
    if score.quality == "stale" or "critical_price_stale" in score.risk_reasons:
        reasons.append("critical_price_stale")
    return tuple(sorted(set(reasons)))


def _parse_benchmark(
    item: _CohortSector,
) -> _BenchmarkEvidence | None:
    value = item.observation.benchmark_return_20d_pct
    raw_reference = item.observation.raw_reference
    code = raw_reference.get("benchmark_code")
    data_date = raw_reference.get("data_date")
    if value is None or not isinstance(code, str) or not code or data_date is None:
        return None
    try:
        terminal_date = date.fromisoformat(str(data_date))
    except ValueError:
        return None
    return _BenchmarkEvidence(
        code=code,
        data_date=terminal_date,
        return_20d_pct=float(value),
    )


def _canonical_benchmark(
    cohort: Sequence[_CohortSector], config: RegimeConfig
) -> tuple[float | None, bool]:
    evidence = [_parse_benchmark(item) for item in cohort]
    if any(item is None for item in evidence):
        return None, True

    canonical = evidence[0]
    assert canonical is not None
    if canonical.code != config.default_benchmark_code:
        raise ValueError("conflicting benchmark evidence")
    if any(
        item is None
        or item.code != canonical.code
        or item.data_date != canonical.data_date
        or item.return_20d_pct != canonical.return_20d_pct
        for item in evidence[1:]
    ):
        raise ValueError("conflicting benchmark evidence")
    return canonical.return_20d_pct, False


def _regime(score: float, config: RegimeConfig) -> MarketRegime:
    if score >= config.risk_on_minimum:
        return "risk_on"
    if score >= config.selective_minimum:
        return "selective"
    if score >= config.defensive_minimum:
        return "defensive"
    return "risk_off"


def _assessment(
    *,
    config: RegimeConfig,
    as_of: datetime,
    coverage: float,
    cohort: Sequence[_CohortSector],
    excluded: Mapping[str, tuple[str, ...]],
    missing_fields: tuple[str, ...],
    reasons: tuple[str, ...],
    components: RegimeComponents | None = None,
    score: float | None = None,
    regime: MarketRegime = "insufficient_data",
) -> MarketRegimeAssessment:
    confidence = round(
        coverage * fmean(item.score.confidence for item in cohort), 4
    ) if cohort else 0.0
    return MarketRegimeAssessment(
        regime_version=config.regime_version,
        as_of=as_of,
        score=score,
        regime=regime,
        confidence=confidence,
        coverage=coverage,
        components=components,
        cohort_sector_ids=tuple(item.score.sector_id for item in cohort),
        excluded_sector_reasons=dict(sorted(excluded.items())),
        missing_fields=missing_fields,
        reasons=reasons,
    )


def assess_market_regime(
    sectors: Sequence[SectorScore],
    config: RegimeConfig,
    as_of: datetime,
) -> MarketRegimeAssessment:
    sector_ids = [item.sector_id for item in sectors]
    if len(sector_ids) != len(set(sector_ids)):
        raise ValueError("duplicate sector_id inputs")

    cohort: list[_CohortSector] = []
    excluded: dict[str, tuple[str, ...]] = {}
    missing_fields: set[str] = set()
    for score in sectors:
        observation = SectorObservation.model_validate(score.observation)
        if observation.sector_id != score.sector_id:
            raise ValueError("sector score identity does not match observation")
        reasons = _cohort_reason(score, observation)
        if reasons:
            excluded[score.sector_id] = reasons
            missing_fields.update(
                reason for reason in reasons if reason in _COHORT_METRICS
            )
            continue
        cohort.append(_CohortSector(score=score, observation=observation))

    coverage = round(len(cohort) / len(sectors), 4) if sectors else 0.0
    reasons: list[str] = []
    if len(cohort) < config.minimum_sector_count:
        reasons.append("cohort_below_minimum")
    if coverage < config.minimum_coverage:
        reasons.append("coverage_below_minimum")

    benchmark_return: float | None = None
    if cohort:
        benchmark_return, benchmark_missing = _canonical_benchmark(cohort, config)
        if benchmark_missing:
            reasons.append("benchmark_missing")
            missing_fields.add("benchmark_return_20d_pct")
    else:
        reasons.append("benchmark_missing")
        missing_fields.add("benchmark_return_20d_pct")

    if reasons:
        return _assessment(
            config=config,
            as_of=as_of,
            coverage=coverage,
            cohort=cohort,
            excluded=excluded,
            missing_fields=tuple(sorted(missing_fields)),
            reasons=tuple(sorted(set(reasons))),
        )

    assert benchmark_return is not None
    components = RegimeComponents(
        benchmark_trend=_benchmark_score(benchmark_return),
        positive_sector_diffusion=_share(
            cohort, lambda item: item.observation.return_20d_pct > 0
        ),
        flow_diffusion=_share(
            cohort, lambda item: item.observation.capital_flow_5d > 0
        ),
        liquidity_diffusion=_share(
            cohort, lambda item: item.observation.turnover_ratio_20d >= 1.0
        ),
        non_risk_sector_share=_share(
            cohort, lambda item: item.score.state not in {"weakening", "avoid"}
        ),
    )
    score = round(
        sum(
            getattr(components, component_name) * weight / 100.0
            for component_name, weight in config.weights.items()
        ),
        4,
    )
    return _assessment(
        config=config,
        as_of=as_of,
        coverage=coverage,
        cohort=cohort,
        excluded=excluded,
        missing_fields=tuple(sorted(missing_fields)),
        reasons=(),
        components=components,
        score=score,
        regime=_regime(score, config),
    )
