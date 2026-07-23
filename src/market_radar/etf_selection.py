from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import fmean

from src.market_radar.models import (
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
)
from src.market_radar.policy_config import EtfPolicyConfig


HARD_FILTER_REASON_CODES = (
    "inactive_mapping",
    "not_active",
    "insufficient_history",
    "invalid_price",
    "invalid_amount",
    "low_liquidity",
    "stale_quote",
    "suspended",
    "data_integrity_failure",
    "spread_too_wide",
    "premium_discount_too_large",
)

MISSING_REQUIRED_EVIDENCE_REASON_CODES = (
    "missing_data_date",
    "missing_active",
    "missing_finalized_session_count",
    "missing_current_price",
    "missing_current_traded_amount",
    "missing_average_traded_amount_20d",
)

_SAFETY_FIELDS = ("spread_bps", "premium_discount_pct", "suspended")


@dataclass(frozen=True)
class _Classified:
    observation: EtfObservation
    hard_reasons: tuple[str, ...]
    missing_reasons: tuple[str, ...]

    @property
    def passes_hard_filters(self) -> bool:
        return not self.hard_reasons and not self.missing_reasons


def _require_unique_observations(observations: Sequence[EtfObservation]) -> None:
    codes = [item.code for item in observations]
    duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
    if duplicate_codes:
        raise ValueError("duplicate ETF codes: " + ", ".join(duplicate_codes))


def _is_effective(item: EtfObservation) -> bool:
    if item.data_date is None:
        return False
    return item.mapping_effective_from <= item.data_date and (
        item.mapping_effective_to is None
        or item.data_date <= item.mapping_effective_to
    )


def _has_data_integrity_failure(item: EtfObservation) -> bool:
    reference = item.raw_reference
    expected_code = reference.get("normalized_code")
    expected_date = reference.get("normalized_data_date")
    return (
        (expected_code is not None and expected_code != item.code)
        or (expected_date is not None and expected_date != item.data_date)
    )


def _valid_fresh_auction(item: EtfObservation, config: EtfPolicyConfig) -> bool:
    return (
        item.raw_reference.get("valid_fresh_auction") is True
        and item.freshness_seconds <= config.stale_after_seconds
    )


def _eligibility(item: EtfObservation, config: EtfPolicyConfig) -> _Classified:
    hard: list[str] = []
    missing: list[str] = []

    if item.data_date is None:
        missing.append("missing_data_date")
    elif not _is_effective(item):
        hard.append("inactive_mapping")

    if item.active is None:
        missing.append("missing_active")
    elif not item.active:
        hard.append("not_active")

    if item.finalized_session_count is None:
        missing.append("missing_finalized_session_count")
    elif item.finalized_session_count < config.minimum_finalized_sessions:
        hard.append("insufficient_history")

    if item.current_price is None:
        missing.append("missing_current_price")
    elif item.current_price <= 0:
        hard.append("invalid_price")

    if item.current_traded_amount is None:
        missing.append("missing_current_traded_amount")
    elif item.current_traded_amount < 0 or (
        item.current_traded_amount == 0 and not _valid_fresh_auction(item, config)
    ):
        hard.append("invalid_amount")

    if item.average_traded_amount_20d is None:
        missing.append("missing_average_traded_amount_20d")
    else:
        if item.average_traded_amount_20d < 0:
            hard.append("invalid_amount")
        elif item.average_traded_amount_20d < config.minimum_average_amount_cny:
            hard.append("low_liquidity")

    if item.freshness_seconds > config.stale_after_seconds:
        hard.append("stale_quote")
    if item.suspended is True:
        hard.append("suspended")
    if _has_data_integrity_failure(item):
        hard.append("data_integrity_failure")
    if item.spread_bps is not None and item.spread_bps > config.maximum_spread_bps:
        hard.append("spread_too_wide")
    if (
        item.premium_discount_pct is not None
        and abs(item.premium_discount_pct) > config.maximum_abs_premium_discount_pct
    ):
        hard.append("premium_discount_too_large")

    ordered_hard = tuple(reason for reason in HARD_FILTER_REASON_CODES if reason in hard)
    ordered_missing = tuple(
        reason for reason in MISSING_REQUIRED_EVIDENCE_REASON_CODES if reason in missing
    )
    return _Classified(item, ordered_hard, ordered_missing)


def _mean_available(values: Sequence[float | None]) -> float | None:
    available = [float(value) for value in values if value is not None]
    return fmean(available) if available else None


def _component_values(item: EtfObservation) -> dict[str, object | None]:
    trend = _mean_available((item.return_20d_pct, item.return_60d_pct))
    tracking = (
        item.tracking_error_pct
        if item.tracking_error_pct is not None
        else (
            abs(item.tracking_difference_pct)
            if item.tracking_difference_pct is not None
            else None
        )
    )
    return {
        "liquidity": (
            (item.average_traded_amount_20d, item.liquidity_stability)
            if item.average_traded_amount_20d is not None
            else None
        ),
        "trend": trend,
        "tracking_quality": tracking,
        "cost": item.annual_fee_pct,
        "size": item.size_cny,
    }


def _percentile_scores(
    rows: Sequence[_Classified],
    value_getter: Callable[[_Classified], object | None],
    *,
    lower_is_better: bool = False,
) -> dict[str, float]:
    values = [
        (row.observation.code, value)
        for row in rows
        if (value := value_getter(row)) is not None
    ]
    if not values:
        return {}

    def sort_value(value: object) -> object:
        if isinstance(value, tuple):
            amount, stability = value
            return (amount, stability is not None, stability)
        return value

    ordered = sorted(
        values,
        key=lambda pair: (sort_value(pair[1]), pair[0]),
        reverse=not lower_is_better,
    )
    if len(ordered) == 1:
        return {ordered[0][0]: 100.0}

    positions: dict[object, list[int]] = {}
    for position, (_, value) in enumerate(ordered):
        positions.setdefault(value, []).append(position)
    return {
        code: round(
            100.0 * (len(ordered) - 1 - fmean(positions[value])) / (len(ordered) - 1),
            4,
        )
        for code, value in ordered
    }


def _confidence(item: EtfObservation, values: dict[str, object], config: EtfPolicyConfig) -> float:
    available_weight = sum(
        config.component_weights[name]
        for name, value in values.items()
        if value is not None
    )
    safety_checks = sum(getattr(item, field) is not None for field in _SAFETY_FIELDS)
    quality_multiplier = {
        "complete": 1.0,
        "partial": 0.85,
        "stale": 0.0,
        "unavailable": 0.0,
    }[item.quality]
    return round(
        min(
            1.0,
            available_weight / 100.0
            * (0.8 + 0.2 * safety_checks / len(_SAFETY_FIELDS))
            * quality_multiplier,
        ),
        4,
    )


def _effective_weights(
    values: dict[str, object], config: EtfPolicyConfig
) -> dict[str, float]:
    available_weight = sum(
        config.component_weights[name]
        for name, value in values.items()
        if value is not None
    )
    if not available_weight:
        return {}
    return {
        name: round(config.component_weights[name] / available_weight * 100.0, 4)
        for name, value in values.items()
        if value is not None
    }


def _unranked_selection(row: _Classified, *, ranking_reason: str | None = None) -> EtfSelection:
    reasons = row.hard_reasons + row.missing_reasons
    if ranking_reason is not None:
        reasons += (ranking_reason,)
    status = "insufficient_data" if row.missing_reasons or ranking_reason else "rejected"
    return EtfSelection(
        sector_id=row.observation.sector_id,
        code=row.observation.code,
        name=row.observation.name,
        status=status,
        eligible=False,
        rank=None,
        score=None,
        confidence=0.0,
        components=EtfComponentScores(),
        effective_weights={},
        reason_codes=reasons,
        observation=row.observation,
    )


def _rank_sector(rows: Sequence[_Classified], config: EtfPolicyConfig) -> list[EtfSelection]:
    scoreable: list[tuple[_Classified, dict[str, object]]] = []
    deferred: dict[str, EtfSelection] = {}
    for row in rows:
        if not row.passes_hard_filters:
            deferred[row.observation.code] = _unranked_selection(row)
            continue
        values = _component_values(row.observation)
        if values["liquidity"] is None or values["trend"] is None:
            deferred[row.observation.code] = _unranked_selection(
                row, ranking_reason="missing_required_ranking_evidence"
            )
            continue
        scoreable.append((row, values))

    percentile_maps = {
        name: _percentile_scores(
            [row for row, _ in scoreable],
            lambda row, component=name: _component_values(row.observation)[component],
            lower_is_better=name in {"tracking_quality", "cost"},
        )
        for name in config.component_weights
    }
    ranked: list[tuple[EtfSelection, float]] = []
    for row, values in scoreable:
        component_scores = {
            name: (
                percentile_maps[name][row.observation.code]
                if value is not None
                else None
            )
            for name, value in values.items()
        }
        weights = _effective_weights(values, config)
        score = round(
            sum(component_scores[name] * weights[name] / 100.0 for name in weights), 4
        )
        confidence = _confidence(row.observation, values, config)
        selection = EtfSelection(
            sector_id=row.observation.sector_id,
            code=row.observation.code,
            name=row.observation.name,
            status="candidate",
            eligible=True,
            rank=None,
            score=score,
            confidence=confidence,
            components=EtfComponentScores(**component_scores),
            effective_weights=weights,
            reason_codes=(),
            observation=row.observation,
        )
        ranked.append((selection, row.observation.average_traded_amount_20d or 0.0))

    ranked.sort(
        key=lambda pair: (
            -float(pair[0].score or 0.0),
            -pair[0].confidence,
            -pair[1],
            pair[0].code,
        )
    )
    results: list[EtfSelection] = []
    for index, (selection, _) in enumerate(ranked, start=1):
        is_complete_winner = (
            index == 1
            and len(selection.effective_weights) == len(config.component_weights)
            and all(getattr(selection.observation, field) is not None for field in _SAFETY_FIELDS)
            and selection.observation.quality == "complete"
        )
        results.append(
            selection.model_copy(
                update={
                    "rank": index,
                    "status": "best_supported" if is_complete_winner else "candidate",
                }
            )
        )
    results.extend(deferred[row.observation.code] for row in rows if row.observation.code in deferred)
    return results


def _rank_by_sector(
    classified: Sequence[_Classified], config: EtfPolicyConfig
) -> tuple[EtfSelection, ...]:
    sectors: dict[str, list[_Classified]] = {}
    for row in classified:
        sectors.setdefault(row.observation.sector_id, []).append(row)
    return tuple(
        selection
        for rows in sectors.values()
        for selection in _rank_sector(rows, config)
    )


def select_etfs(
    observations: Sequence[EtfObservation],
    config: EtfPolicyConfig,
) -> tuple[EtfSelection, ...]:
    _require_unique_observations(observations)
    classified = [_eligibility(item, config) for item in observations]
    return _rank_by_sector(classified, config)
