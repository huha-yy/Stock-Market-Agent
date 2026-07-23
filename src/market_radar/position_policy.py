from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from itertools import combinations
from math import isfinite
from statistics import StatisticsError, correlation, pvariance

from src.market_radar.models import (
    CorrelationGroup,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    SectorScore,
)
from src.market_radar.policy_config import PositionPolicyConfig


_SUPPORTED_ETF_STATUSES = {"best_supported", "candidate"}
_INVESTABLE_SECTOR_STATES = {"leading", "improving"}
_INVALIDATION_CODE_ORDER = (
    "sector_state_deteriorated",
    "sector_confidence_below_threshold",
    "etf_became_ineligible",
    "critical_evidence_stale",
    "market_regime_deteriorated",
    "correlation_cap_reached",
)


@dataclass(frozen=True)
class _Candidate:
    sector: SectorScore
    selection: EtfSelection
    joint_confidence: float
    sector_cap_pct: float


def _selection_sort_key(selection: EtfSelection) -> tuple[float, float, float, str]:
    return (
        float(selection.rank) if selection.rank is not None else float("inf"),
        -float(selection.score or 0.0),
        -selection.confidence,
        selection.code,
    )


def _select_etf(selections: Sequence[EtfSelection]) -> EtfSelection | None:
    supported = [item for item in selections if item.status in _SUPPORTED_ETF_STATUSES]
    if not supported:
        return None
    best_supported = [item for item in supported if item.status == "best_supported"]
    return min(best_supported or supported, key=_selection_sort_key)


def _floor_cap(maximum_pct: float, joint_confidence: float) -> float:
    amount = Decimal(str(maximum_pct)) * Decimal(str(joint_confidence))
    return float((amount * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR) / Decimal("10"))


def _base_invalidation_codes(
    sector: SectorScore,
    selection: EtfSelection,
    regime: MarketRegimeAssessment,
    config: PositionPolicyConfig,
) -> tuple[str, ...]:
    applicable: set[str] = set()
    if sector.state not in _INVESTABLE_SECTOR_STATES:
        applicable.add("sector_state_deteriorated")
    if sector.confidence < config.minimum_sector_confidence:
        applicable.add("sector_confidence_below_threshold")
    if selection.status not in _SUPPORTED_ETF_STATUSES or not selection.eligible:
        applicable.add("etf_became_ineligible")
    if sector.quality == "stale" or "critical_price_stale" in sector.risk_reasons:
        applicable.add("critical_evidence_stale")
    if regime.regime in {"defensive", "risk_off", "insufficient_data"}:
        applicable.add("market_regime_deteriorated")
    return tuple(code for code in _INVALIDATION_CODE_ORDER if code in applicable)


def _validated_returns(
    selection: EtfSelection, config: PositionPolicyConfig
) -> tuple[tuple[date, ...], tuple[float, ...]] | None:
    observation = selection.observation
    dates = observation.daily_return_dates_60
    returns = observation.daily_returns_60
    if not isinstance(dates, tuple) or not isinstance(returns, tuple):
        return None
    if observation.finalized_session_count is None:
        return None
    if observation.finalized_session_count < config.correlation_sessions:
        return None
    if len(dates) != config.correlation_sessions or len(returns) != config.correlation_sessions:
        return None
    if any(not isinstance(value, date) for value in dates):
        return None
    if any(previous >= current for previous, current in zip(dates, dates[1:])):
        return None
    try:
        numeric_returns = tuple(float(value) for value in returns)
    except (TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in numeric_returns):
        return None
    if pvariance(numeric_returns) == 0.0:
        return None
    return dates, numeric_returns


def _known_correlation(
    left: EtfSelection, right: EtfSelection, config: PositionPolicyConfig
) -> float | None:
    left_evidence = _validated_returns(left, config)
    right_evidence = _validated_returns(right, config)
    if left_evidence is None or right_evidence is None:
        return None
    left_dates, left_returns = left_evidence
    right_dates, right_returns = right_evidence
    if left_dates != right_dates:
        return None
    try:
        value = correlation(left_returns, right_returns)
    except (StatisticsError, ValueError):
        return None
    return value if isfinite(value) else None


def _correlation_components(
    candidates: Sequence[_Candidate], config: PositionPolicyConfig
) -> tuple[tuple[tuple[str, ...], ...], float]:
    known_pair_count = 0
    neighbours = {item.selection.code: set() for item in candidates}
    by_code = {item.selection.code: item for item in candidates}
    for left_code, right_code in combinations(sorted(by_code), 2):
        value = _known_correlation(
            by_code[left_code].selection, by_code[right_code].selection, config
        )
        if value is None:
            continue
        known_pair_count += 1
        if value >= config.correlation_threshold:
            neighbours[left_code].add(right_code)
            neighbours[right_code].add(left_code)

    components: list[tuple[str, ...]] = []
    visited: set[str] = set()
    for root in sorted(neighbours):
        if root in visited or not neighbours[root]:
            continue
        component: set[str] = set()
        stack = [root]
        while stack:
            code = stack.pop()
            if code in visited:
                continue
            visited.add(code)
            component.add(code)
            stack.extend(sorted(neighbours[code] - visited, reverse=True))
        if len(component) >= 2:
            components.append(tuple(sorted(component)))

    total_pair_count = len(candidates) * (len(candidates) - 1) // 2
    coverage = known_pair_count / total_pair_count if total_pair_count else 1.0
    return tuple(components), coverage


def _reduce_correlation_caps(
    suggestions: Sequence[PositionSuggestion],
    candidates: Sequence[_Candidate],
    components: Sequence[tuple[str, ...]],
    config: PositionPolicyConfig,
) -> tuple[PositionSuggestion, ...]:
    candidate_by_code = {item.selection.code: item for item in candidates}
    caps = {
        item.etf_code: Decimal(str(item.etf_cap_pct))
        for item in suggestions
    }
    reduced_codes: set[str] = set()
    maximum = Decimal(str(config.maximum_correlated_pct))
    for component in components:
        excess = sum((caps[code] for code in component), Decimal("0")) - maximum
        if excess <= 0:
            continue
        ordered = sorted(
            component,
            key=lambda code: (
                -(
                    candidate_by_code[code].selection.rank
                    if candidate_by_code[code].selection.rank is not None
                    else 10**9
                ),
                code,
            ),
        )
        for code in ordered:
            if excess <= 0:
                break
            reduction = min(caps[code], excess)
            if reduction:
                caps[code] -= reduction
                excess -= reduction
                reduced_codes.add(code)

    output: list[PositionSuggestion] = []
    for suggestion in suggestions:
        invalidation_codes = set(suggestion.invalidation_codes)
        if suggestion.etf_code in reduced_codes:
            invalidation_codes.add("correlation_cap_reached")
        output.append(
            suggestion.model_copy(
                update={
                    "etf_cap_pct": float(caps[suggestion.etf_code]),
                    "invalidation_codes": tuple(
                        code
                        for code in _INVALIDATION_CODE_ORDER
                        if code in invalidation_codes
                    ),
                }
            )
        )
    return tuple(output)


def _require_unique_inputs(
    sectors: Sequence[SectorScore], selections: Sequence[EtfSelection]
) -> None:
    sector_ids = [item.sector_id for item in sectors]
    if len(sector_ids) != len(set(sector_ids)):
        raise ValueError("duplicate sector_id inputs")
    selection_codes = [item.code for item in selections]
    if len(selection_codes) != len(set(selection_codes)):
        raise ValueError("duplicate ETF code inputs")
    selection_ids = [(item.sector_id, item.code) for item in selections]
    if len(selection_ids) != len(set(selection_ids)):
        raise ValueError("duplicate ETF identity inputs")


def build_position_plan(
    sectors: Sequence[SectorScore],
    selections: Sequence[EtfSelection],
    regime: MarketRegimeAssessment,
    config: PositionPolicyConfig,
) -> PositionPlan:
    """Build a generic cap-only position policy from immutable Market Radar evidence."""
    _require_unique_inputs(sectors, selections)
    selections_by_sector: dict[str, list[EtfSelection]] = {}
    for selection in selections:
        selections_by_sector.setdefault(selection.sector_id, []).append(selection)

    candidates: list[_Candidate] = []
    ordered_sectors = sorted(
        sectors,
        key=lambda item: (-item.score, -item.confidence, item.sector_id),
    )
    for sector in ordered_sectors:
        if (
            sector.state not in _INVESTABLE_SECTOR_STATES
            or sector.confidence < config.minimum_sector_confidence
        ):
            continue
        selection = _select_etf(selections_by_sector.get(sector.sector_id, ()))
        if selection is None:
            continue
        joint_confidence = min(sector.confidence, selection.confidence, regime.confidence)
        sector_cap_pct = _floor_cap(config.maximum_sector_pct, joint_confidence)
        if sector_cap_pct <= 0:
            continue
        candidates.append(
            _Candidate(
                sector=sector,
                selection=selection,
                joint_confidence=joint_confidence,
                sector_cap_pct=sector_cap_pct,
            )
        )
        if len(candidates) == config.maximum_suggested_sectors:
            break

    suggestions = tuple(
        PositionSuggestion(
            sector_id=item.sector.sector_id,
            sector_name=item.sector.name,
            sector_rank=index,
            etf_code=item.selection.code,
            etf_status=item.selection.status,
            sector_cap_pct=item.sector_cap_pct,
            etf_cap_pct=min(config.maximum_etf_pct, item.sector_cap_pct),
            joint_confidence=item.joint_confidence,
            invalidation_codes=_base_invalidation_codes(
                item.sector, item.selection, regime, config
            ),
        )
        for index, item in enumerate(candidates, start=1)
    )
    components, correlation_coverage = _correlation_components(candidates, config)
    suggestions = _reduce_correlation_caps(suggestions, candidates, components, config)
    groups = tuple(
        CorrelationGroup(
            etf_codes=component,
            maximum_total_pct=config.maximum_correlated_pct,
        )
        for component in components
    )

    base_confidence = (
        min([regime.confidence, *(item.joint_confidence for item in candidates)])
        if candidates
        else regime.confidence
    )
    reason_codes: list[str] = []
    if not candidates:
        reason_codes.append("no_supported_sector_suggestions")
    if correlation_coverage < 1:
        reason_codes.append("correlation_coverage_incomplete")
    minimum, maximum = config.total_ranges[regime.regime]
    return PositionPlan(
        policy_version=config.policy_version,
        as_of=regime.as_of,
        regime=regime.regime,
        total_position_min_pct=minimum,
        total_position_max_pct=maximum,
        suggestions=suggestions,
        correlation_groups=groups,
        correlation_coverage=correlation_coverage,
        confidence=round(base_confidence * correlation_coverage, 4),
        reason_codes=tuple(reason_codes),
    )
