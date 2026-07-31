from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from src.market_radar.models import (
    FrozenModel,
    PositionSuggestion,
    RadarRunSnapshot,
    SectorScore,
)


LifecycleState = Literal[
    "watching", "candidate", "confirmed", "active", "downgraded", "exited"
]
RunKind = Literal["intraday", "eod"]
_PRECONFIRMATION_REASON = "preconfirmation_no_longer_qualifies"
_EXIT_REASON = "lifecycle_exited"


class LifecycleSignal(FrozenModel):
    signal_key: str
    market: Literal["cn"] = "cn"
    sector_id: str
    instance_number: int = Field(ge=1)
    state: LifecycleState
    lifecycle_version: Literal["cn-lifecycle-v1"] = "cn-lifecycle-v1"
    first_run_key: str
    previous_run_key: str | None = None
    current_run_key: str
    effective_at: datetime
    intraday_qualifying_streak: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    etf_code: str | None = None
    reason_codes: tuple[str, ...] = ()
    closed_at: datetime | None = None
    terminal_reason: str | None = None

    @model_validator(mode="after")
    def validate_identity_and_terminal_state(self) -> "LifecycleSignal":
        expected_key = f"{self.market}:{self.sector_id}:{self.instance_number}"
        if self.signal_key != expected_key:
            raise ValueError(f"signal_key must equal {expected_key}")

        if self.state == "exited":
            if self.closed_at is None or not self.terminal_reason:
                raise ValueError(
                    "exited signal requires closed_at and terminal_reason"
                )
            if self.terminal_reason == _EXIT_REASON:
                return self
        elif self.closed_at is None and self.terminal_reason is None:
            return self
        elif (
            self.state in {"watching", "candidate"}
            and self.closed_at is not None
            and self.terminal_reason == _PRECONFIRMATION_REASON
        ):
            return self

        raise ValueError("invalid terminal fields for lifecycle state")


class LifecycleTransition(FrozenModel):
    transition_key: str
    signal_key: str
    previous_state: LifecycleState | None
    new_state: LifecycleState
    effective_run_key: str
    effective_at: datetime
    lifecycle_version: Literal["cn-lifecycle-v1"] = "cn-lifecycle-v1"
    reason_codes: tuple[str, ...] = ()


class LifecycleContext(FrozenModel):
    open_signals: tuple[LifecycleSignal, ...] = ()
    latest_instance_by_sector: Mapping[str, int] = Field(default_factory=dict)
    head_run_key: str | None = None
    head_effective_at: datetime | None = None

    @model_validator(mode="after")
    def validate_signal_instances(self) -> "LifecycleContext":
        if (self.head_run_key is None) != (self.head_effective_at is None):
            raise ValueError(
                "lifecycle head run key and effective time must be provided together"
            )
        if (
            self.head_effective_at is not None
            and (
                self.head_effective_at.tzinfo is None
                or self.head_effective_at.utcoffset() is None
            )
        ):
            raise ValueError("lifecycle head effective time must be timezone-aware")
        sector_ids = [signal.sector_id for signal in self.open_signals]
        if len(sector_ids) != len(set(sector_ids)):
            raise ValueError("duplicate open signal sector_id")
        if any(value < 1 for value in self.latest_instance_by_sector.values()):
            raise ValueError("latest instance values must be positive")
        for signal in self.open_signals:
            latest = self.latest_instance_by_sector.get(signal.sector_id)
            if latest != signal.instance_number:
                raise ValueError("latest instance must match signal instance")
        return self


class LifecycleEvaluation(FrozenModel):
    run_key: str
    signals: tuple[LifecycleSignal, ...]
    transitions: tuple[LifecycleTransition, ...]
    expected_head_run_key: str | None = None
    expected_head_effective_at: datetime | None = None

    @model_validator(mode="after")
    def validate_expected_head(self) -> "LifecycleEvaluation":
        if (self.expected_head_run_key is None) != (
            self.expected_head_effective_at is None
        ):
            raise ValueError(
                "expected lifecycle head run key and effective time must be provided together"
            )
        if (
            self.expected_head_effective_at is not None
            and (
                self.expected_head_effective_at.tzinfo is None
                or self.expected_head_effective_at.utcoffset() is None
            )
        ):
            raise ValueError("expected lifecycle head time must be timezone-aware")
        return self


class MarketRadarLifecycleEngine:
    def evaluate(
        self,
        snapshot: RadarRunSnapshot,
        context: LifecycleContext,
        *,
        run_kind: RunKind,
    ) -> LifecycleEvaluation:
        previous = {item.sector_id: item for item in context.open_signals}
        sectors = {item.sector_id: item for item in snapshot.sectors}
        suggestions = {
            item.sector_id: item
            for item in (
                snapshot.position_plan.suggestions if snapshot.position_plan else ()
            )
        }
        sector_ids = tuple(dict.fromkeys((*sectors, *previous)))
        signals: list[LifecycleSignal] = []
        transitions: list[LifecycleTransition] = []

        for sector_id in sector_ids:
            sector = sectors.get(sector_id)
            old = previous.get(sector_id)
            suggestion = suggestions.get(sector_id)
            invalidation_codes = (
                suggestion.invalidation_codes if suggestion is not None else ()
            )
            watch = bool(
                sector
                and sector.state in {"leading", "improving"}
                and sector.confidence >= 0.60
                and sector.quality not in {"stale", "unavailable"}
            )
            qualifying = watch and suggestion is not None
            risk_down = bool(
                old
                and old.state in {"confirmed", "active"}
                and (not qualifying or bool(invalidation_codes))
            )
            raw_reference = (
                sector.observation.get("raw_reference") if sector else None
            )
            finalized = bool(
                isinstance(raw_reference, Mapping)
                and raw_reference.get("bar_status") == "finalized"
            )
            new_state, preconfirmation_close = _next_state(
                old,
                watch=watch,
                candidate=qualifying,
                risk_down=risk_down,
                eod_confirmed=run_kind == "eod" and finalized,
                streak_confirmed=run_kind == "intraday",
            )
            if new_state is None:
                continue

            signal = _build_signal(
                snapshot,
                sector_id,
                sector,
                suggestion,
                old,
                new_state,
                context.latest_instance_by_sector,
                qualifying=qualifying,
                run_kind=run_kind,
                invalidation_codes=invalidation_codes,
                preconfirmation_close=preconfirmation_close,
            )
            signals.append(signal)
            if (
                not preconfirmation_close
                and (old is None or old.state != signal.state)
            ):
                transitions.append(_build_transition(signal, old))

        return LifecycleEvaluation(
            run_key=snapshot.run_key,
            signals=tuple(signals),
            transitions=tuple(
                sorted(
                    transitions,
                    key=lambda item: (item.signal_key, item.transition_key),
                )
            ),
            expected_head_run_key=context.head_run_key,
            expected_head_effective_at=context.head_effective_at,
        )


def _next_state(
    old: LifecycleSignal | None,
    *,
    watch: bool,
    candidate: bool,
    risk_down: bool,
    eod_confirmed: bool,
    streak_confirmed: bool = True,
) -> tuple[LifecycleState | None, bool]:
    if old is None or old.state == "exited":
        if candidate:
            return ("confirmed" if eod_confirmed else "candidate"), False
        if watch:
            return "watching", False
        return None, False
    if old.state == "downgraded":
        return "exited", False
    if old.state in {"confirmed", "active"} and risk_down:
        return "downgraded", False
    if old.state == "confirmed":
        return "active", False
    if old.state == "active":
        return "active", False
    if old.state == "candidate":
        if candidate and (
            eod_confirmed
            or (
                streak_confirmed
                and old.intraday_qualifying_streak >= 1
            )
        ):
            return "confirmed", False
        return ("candidate", False) if candidate else ("candidate", True)
    if old.state == "watching":
        if candidate:
            return ("confirmed" if eod_confirmed else "candidate"), False
        return ("watching", False) if watch else ("watching", True)
    raise ValueError(f"unsupported lifecycle state: {old.state}")


def _stable_key(prefix: str, payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{prefix}:{hashlib.sha256(rendered.encode('utf-8')).hexdigest()}"


def _build_signal(
    snapshot: RadarRunSnapshot,
    sector_id: str,
    sector: SectorScore | None,
    suggestion: PositionSuggestion | None,
    old: LifecycleSignal | None,
    new_state: LifecycleState,
    latest_instances: Mapping[str, int],
    *,
    qualifying: bool,
    run_kind: RunKind,
    invalidation_codes: tuple[str, ...],
    preconfirmation_close: bool,
) -> LifecycleSignal:
    new_instance = old is None or old.state == "exited"
    latest_instance = max(
        int(latest_instances.get(sector_id, 0)),
        old.instance_number if old is not None else 0,
    )
    instance_number = latest_instance + 1 if new_instance else old.instance_number
    signal_key = f"cn:{sector_id}:{instance_number}"
    if preconfirmation_close or new_state in {"downgraded", "exited"}:
        intraday_streak = 0
    elif not qualifying:
        intraday_streak = 0
    elif run_kind == "intraday":
        intraday_streak = (
            old.intraday_qualifying_streak + 1
            if old is not None and not new_instance
            else 1
        )
    elif old is not None and not new_instance:
        intraday_streak = old.intraday_qualifying_streak
    else:
        intraday_streak = 0

    reason_codes: set[str] = set()
    if preconfirmation_close:
        reason_codes.add(_PRECONFIRMATION_REASON)
    elif new_state == "downgraded":
        reason_codes.update(
            invalidation_codes
            or ("position_suggestion_no_longer_qualifies",)
        )
    elif new_state == "exited":
        reason_codes.add("downgrade_confirmed")
    elif new_state == "confirmed":
        reason_codes.add("qualification_confirmed")
    reasons = tuple(sorted(reason_codes))

    return LifecycleSignal(
        signal_key=signal_key,
        sector_id=sector_id,
        instance_number=instance_number,
        state=new_state,
        first_run_key=snapshot.run_key if new_instance else old.first_run_key,
        previous_run_key=old.current_run_key if old else None,
        current_run_key=snapshot.run_key,
        effective_at=snapshot.as_of,
        intraday_qualifying_streak=intraday_streak,
        confidence=(
            suggestion.joint_confidence
            if suggestion
            else sector.confidence
            if sector
            else old.confidence
        ),
        etf_code=(
            suggestion.etf_code
            if suggestion
            else old.etf_code
            if old
            else None
        ),
        reason_codes=reasons,
        closed_at=(
            snapshot.as_of
            if preconfirmation_close or new_state == "exited"
            else None
        ),
        terminal_reason=(
            _PRECONFIRMATION_REASON
            if preconfirmation_close
            else _EXIT_REASON
            if new_state == "exited"
            else None
        ),
    )


def _build_transition(
    signal: LifecycleSignal, old: LifecycleSignal | None
) -> LifecycleTransition:
    payload = {
        "signal_key": signal.signal_key,
        "run_key": signal.current_run_key,
        "previous": old.state if old else None,
        "new": signal.state,
    }
    return LifecycleTransition(
        transition_key=_stable_key("radar-transition", payload),
        signal_key=signal.signal_key,
        previous_state=old.state if old else None,
        new_state=signal.state,
        effective_run_key=signal.current_run_key,
        effective_at=signal.effective_at,
        reason_codes=signal.reason_codes,
    )
