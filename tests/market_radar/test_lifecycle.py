from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.market_radar.lifecycle import (
    LifecycleContext,
    LifecycleSignal,
    MarketRadarLifecycleEngine,
)
from src.market_radar.models import (
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    RadarRunSnapshot,
    SectorObservation,
    SectorScore,
)


NOW = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)


def _sector_score(
    sector_id: str,
    *,
    bar_status: str = "provisional",
    confidence: float = 0.8,
    state: str = "leading",
    quality: str = "complete",
) -> SectorScore:
    observation = SectorObservation(
        sector_id=sector_id,
        name=sector_id.rsplit(":", 1)[-1].title(),
        kind="industry",
        observed_at=NOW,
        source="test",
        freshness_seconds=0,
        quality=quality,
        missing_fields=SectorObservation.tracked_metric_fields,
        raw_reference={"bar_status": bar_status},
    )
    return SectorScore(
        sector_id=sector_id,
        name=observation.name,
        kind="industry",
        scoring_version="cn-v1",
        gross_score=80.0,
        risk_deduction=5.0,
        score=75.0,
        confidence=confidence,
        state=state,
        factors={},
        risk_reasons=(),
        missing_fields=(),
        source="test",
        observed_at=NOW,
        quality=quality,
        observation=observation.model_dump(mode="json"),
    )


def _selection(sector_id: str, etf_code: str) -> EtfSelection:
    observation = EtfObservation(
        sector_id=sector_id,
        code=etf_code,
        name=f"{sector_id} ETF",
        observed_at=NOW,
        data_date=None,
        bar_status=None,
        source="test",
        quality="unavailable",
        freshness_seconds=0,
        mapping_effective_from=date(2026, 1, 1),
        missing_fields=EtfObservation.tracked_metric_fields,
    )
    return EtfSelection(
        sector_id=sector_id,
        code=etf_code,
        name=observation.name,
        status="candidate",
        eligible=True,
        rank=1,
        confidence=0.8,
        components=EtfComponentScores(),
        observation=observation,
    )


@pytest.fixture
def snapshot_factory():
    def factory(
        *,
        qualifying: bool,
        bar_status: str = "provisional",
        sector_ids: tuple[str, ...] = ("industry:semiconductor",),
        run_key: str = "cn:20260721T070000Z:manual",
        invalidation_codes: tuple[str, ...] = (),
        watch: bool = True,
    ) -> RadarRunSnapshot:
        sectors = tuple(
            _sector_score(
                sector_id,
                bar_status=bar_status,
                state="leading" if watch else "neutral",
            )
            for sector_id in sector_ids
        )
        if not qualifying:
            return RadarRunSnapshot(
                run_key=run_key,
                market="cn",
                trigger="manual",
                as_of=NOW,
                quality="complete",
                scoring_version="cn-v1",
                sectors=sectors,
                provider_trace=(),
            )

        selections = tuple(
            _selection(sector_id, f"{510000 + index:06d}")
            for index, sector_id in enumerate(sector_ids)
        )
        suggestions = tuple(
            PositionSuggestion(
                sector_id=sector.sector_id,
                sector_name=sector.name,
                sector_rank=index,
                etf_code=selection.code,
                etf_status="candidate",
                sector_cap_pct=10.0,
                etf_cap_pct=10.0,
                joint_confidence=0.8,
                invalidation_codes=invalidation_codes,
            )
            for index, (sector, selection) in enumerate(
                zip(sectors, selections), start=1
            )
        )
        regime = MarketRegimeAssessment(
            as_of=NOW,
            regime="selective",
            confidence=0.8,
            coverage=0.8,
        )
        return RadarRunSnapshot(
            run_key=run_key,
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="complete",
            scoring_version="cn-v1",
            sectors=sectors,
            provider_trace=(),
            etfs=selections,
            regime=regime,
            position_plan=PositionPlan(
                as_of=NOW,
                regime=regime.regime,
                total_position_min_pct=20.0,
                total_position_max_pct=30.0,
                suggestions=suggestions,
                correlation_coverage=1.0,
                confidence=0.8,
            ),
        )

    return factory


@pytest.fixture
def context_factory():
    def factory(
        *,
        previous_state: str | None,
        latest_instance: int = 0,
        sector_id: str = "industry:semiconductor",
        intraday_qualifying_streak: int = 1,
    ) -> LifecycleContext:
        if previous_state is None:
            return LifecycleContext(
                latest_instance_by_sector=(
                    {sector_id: latest_instance} if latest_instance else {}
                )
            )
        previous_at = NOW - timedelta(hours=1)
        exited = previous_state == "exited"
        signal = LifecycleSignal(
            signal_key=f"cn:{sector_id}:{max(latest_instance, 1)}",
            sector_id=sector_id,
            instance_number=max(latest_instance, 1),
            state=previous_state,
            first_run_key="cn:20260721T060000Z:manual",
            current_run_key="cn:20260721T060000Z:manual",
            effective_at=previous_at,
            intraday_qualifying_streak=intraday_qualifying_streak,
            confidence=0.7,
            etf_code="510999",
            closed_at=previous_at if exited else None,
            terminal_reason="lifecycle_exited" if exited else None,
        )
        return LifecycleContext(
            open_signals=(signal,),
            latest_instance_by_sector={sector_id: max(latest_instance, 1)},
        )

    return factory


@pytest.mark.parametrize(
    ("previous", "qualifying", "run_kind", "bar_status", "expected"),
    [
        (None, True, "intraday", "provisional", "candidate"),
        (None, True, "eod", "finalized", "confirmed"),
        ("candidate", True, "intraday", "provisional", "confirmed"),
        ("candidate", True, "eod", "finalized", "confirmed"),
        ("candidate", True, "eod", "provisional", "candidate"),
        ("confirmed", True, "intraday", "provisional", "active"),
        ("active", False, "intraday", "provisional", "downgraded"),
        ("downgraded", True, "intraday", "provisional", "exited"),
    ],
)
def test_lifecycle_transition_table(
    previous,
    qualifying,
    run_kind,
    bar_status,
    expected,
    snapshot_factory,
    context_factory,
) -> None:
    snapshot = snapshot_factory(qualifying=qualifying, bar_status=bar_status)
    context = context_factory(previous_state=previous)

    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot, context, run_kind=run_kind
    )

    assert evaluation.signals[0].state == expected


def test_failed_attempt_is_not_an_engine_input() -> None:
    assert (
        "attempt"
        not in inspect.signature(MarketRadarLifecycleEngine.evaluate).parameters
    )


def test_reentry_after_exit_increments_instance(
    snapshot_factory, context_factory
) -> None:
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=True),
        context_factory(previous_state="exited", latest_instance=3),
        run_kind="intraday",
    )

    signal = evaluation.signals[0]
    assert signal.state == "candidate"
    assert signal.instance_number == 4
    assert signal.signal_key == "cn:industry:semiconductor:4"


def test_preconfirmation_loss_closes_without_exited_transition(
    snapshot_factory, context_factory
) -> None:
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=False),
        context_factory(previous_state="candidate"),
        run_kind="intraday",
    )

    signal = evaluation.signals[0]
    assert signal.state == "candidate"
    assert signal.closed_at == NOW
    assert signal.terminal_reason == "preconfirmation_no_longer_qualifies"
    assert signal.reason_codes == ("preconfirmation_no_longer_qualifies",)
    assert evaluation.transitions == ()


def test_watching_loss_closes_without_changing_state_or_emitting_transition(
    snapshot_factory,
    context_factory,
) -> None:
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=False, watch=False),
        context_factory(previous_state="watching"),
        run_kind="intraday",
    )

    signal = evaluation.signals[0]
    assert signal.state == "watching"
    assert signal.closed_at == NOW
    assert signal.terminal_reason == "preconfirmation_no_longer_qualifies"
    assert signal.reason_codes == ("preconfirmation_no_longer_qualifies",)
    assert evaluation.transitions == ()


def test_finalized_eod_uses_production_observation_payload(
    snapshot_factory,
) -> None:
    snapshot = snapshot_factory(qualifying=True, bar_status="finalized")
    observation = snapshot.sectors[0].observation

    assert "bar_status" not in observation
    assert observation["raw_reference"]["bar_status"] == "finalized"

    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot,
        LifecycleContext(),
        run_kind="eod",
    )

    assert evaluation.signals[0].state == "confirmed"


@pytest.mark.parametrize(
    ("previous_state", "invalidation_codes", "expected_reasons"),
    [
        (
            "confirmed",
            ("critical_evidence_stale",),
            ("critical_evidence_stale",),
        ),
        (
            "active",
            ("market_regime_deteriorated", "critical_evidence_stale"),
            ("critical_evidence_stale", "market_regime_deteriorated"),
        ),
    ],
)
def test_current_invalidation_downgrades_confirmed_or_active_signal(
    previous_state,
    invalidation_codes,
    expected_reasons,
    snapshot_factory,
    context_factory,
) -> None:
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(
            qualifying=True,
            invalidation_codes=invalidation_codes,
        ),
        context_factory(previous_state=previous_state),
        run_kind="intraday",
    )

    assert evaluation.signals[0].state == "downgraded"
    assert evaluation.signals[0].reason_codes == expected_reasons
    assert evaluation.transitions[0].reason_codes == expected_reasons


def test_provisional_eod_does_not_advance_intraday_confirmation_streak(
    snapshot_factory,
) -> None:
    engine = MarketRadarLifecycleEngine()
    eod = engine.evaluate(
        snapshot_factory(
            qualifying=True,
            bar_status="provisional",
            run_key="cn:20260721T070000Z:schedule",
        ),
        LifecycleContext(),
        run_kind="eod",
    )
    first_intraday = engine.evaluate(
        snapshot_factory(
            qualifying=True,
            run_key="cn:20260722T020000Z:schedule",
        ),
        LifecycleContext(
            open_signals=eod.signals,
            latest_instance_by_sector={"industry:semiconductor": 1},
        ),
        run_kind="intraday",
    )
    second_intraday = engine.evaluate(
        snapshot_factory(
            qualifying=True,
            run_key="cn:20260722T030000Z:schedule",
        ),
        LifecycleContext(
            open_signals=first_intraday.signals,
            latest_instance_by_sector={"industry:semiconductor": 1},
        ),
        run_kind="intraday",
    )

    assert (eod.signals[0].state, eod.signals[0].intraday_qualifying_streak) == (
        "candidate",
        0,
    )
    assert (
        first_intraday.signals[0].state,
        first_intraday.signals[0].intraday_qualifying_streak,
    ) == ("candidate", 1)
    assert (
        second_intraday.signals[0].state,
        second_intraday.signals[0].intraday_qualifying_streak,
    ) == ("confirmed", 2)


def test_nonqualifying_eligible_run_closes_and_resets_candidate_streak(
    snapshot_factory,
) -> None:
    engine = MarketRadarLifecycleEngine()
    candidate = engine.evaluate(
        snapshot_factory(qualifying=True),
        LifecycleContext(),
        run_kind="intraday",
    )

    closed = engine.evaluate(
        snapshot_factory(
            qualifying=False,
            run_key="cn:20260721T080000Z:schedule",
        ),
        LifecycleContext(
            open_signals=candidate.signals,
            latest_instance_by_sector={"industry:semiconductor": 1},
        ),
        run_kind="intraday",
    )

    assert closed.signals[0].state == "candidate"
    assert closed.signals[0].intraday_qualifying_streak == 0
    assert closed.transitions == ()


def test_downgraded_exits_only_on_next_successful_evaluation(
    snapshot_factory, context_factory
) -> None:
    first = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=False),
        context_factory(previous_state="active"),
        run_kind="intraday",
    )
    assert first.signals[0].state == "downgraded"
    assert first.signals[0].closed_at is None

    second = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=True),
        LifecycleContext(
            open_signals=first.signals,
            latest_instance_by_sector={"industry:semiconductor": 1},
        ),
        run_kind="intraday",
    )

    assert second.signals[0].state == "exited"
    assert second.signals[0].closed_at == NOW
    assert second.signals[0].terminal_reason == "lifecycle_exited"
    assert second.transitions[0].reason_codes == ("downgrade_confirmed",)


def test_repeated_evaluation_is_deterministic(snapshot_factory) -> None:
    snapshot = snapshot_factory(qualifying=True, bar_status="finalized")
    context = LifecycleContext()
    engine = MarketRadarLifecycleEngine()

    first = engine.evaluate(snapshot, context, run_kind="eod")
    second = engine.evaluate(snapshot, context, run_kind="eod")

    assert first == second
    assert first.transitions[0].transition_key == second.transitions[0].transition_key
    assert first.signals[0].reason_codes == ("qualification_confirmed",)


def test_signal_order_preserves_snapshot_sector_order(snapshot_factory) -> None:
    sector_ids = ("industry:zeta", "industry:alpha")

    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=True, sector_ids=sector_ids),
        LifecycleContext(),
        run_kind="intraday",
    )

    assert tuple(signal.sector_id for signal in evaluation.signals) == sector_ids
    assert evaluation.transitions == tuple(
        sorted(
            evaluation.transitions,
            key=lambda item: (item.signal_key, item.transition_key),
        )
    )


def test_lifecycle_contracts_are_immutable(snapshot_factory) -> None:
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=True),
        LifecycleContext(latest_instance_by_sector={}),
        run_kind="intraday",
    )

    with pytest.raises(ValidationError, match="frozen"):
        evaluation.signals[0].state = "active"
    with pytest.raises(TypeError):
        LifecycleContext(
            latest_instance_by_sector={"industry:semiconductor": 1}
        ).latest_instance_by_sector["industry:semiconductor"] = 2


def _signal_for_validation(**overrides) -> LifecycleSignal:
    payload = {
        "signal_key": "cn:industry:semiconductor:1",
        "sector_id": "industry:semiconductor",
        "instance_number": 1,
        "state": "candidate",
        "first_run_key": "cn:20260721T060000Z:manual",
        "current_run_key": "cn:20260721T060000Z:manual",
        "effective_at": NOW,
        "intraday_qualifying_streak": 1,
        "confidence": 0.8,
    }
    payload.update(overrides)
    return LifecycleSignal(**payload)


@pytest.mark.parametrize(
    ("closed_at", "terminal_reason"),
    [
        (None, None),
        (NOW, None),
        (None, "lifecycle_exited"),
        (NOW, ""),
    ],
)
def test_exited_signal_requires_complete_terminal_fields(
    closed_at,
    terminal_reason,
) -> None:
    with pytest.raises(
        ValidationError,
        match="exited signal requires closed_at and terminal_reason",
    ):
        _signal_for_validation(
            state="exited",
            closed_at=closed_at,
            terminal_reason=terminal_reason,
        )


@pytest.mark.parametrize("state", ["watching", "candidate"])
def test_preconfirmation_states_allow_exact_closed_terminal_combination(
    state,
) -> None:
    signal = _signal_for_validation(
        state=state,
        closed_at=NOW,
        terminal_reason="preconfirmation_no_longer_qualifies",
    )

    assert signal.state == state
    assert signal.closed_at == NOW


@pytest.mark.parametrize(
    ("state", "closed_at", "terminal_reason"),
    [
        ("candidate", NOW, None),
        ("watching", None, "preconfirmation_no_longer_qualifies"),
        ("candidate", NOW, "lifecycle_exited"),
        ("watching", NOW, "unknown_reason"),
        ("confirmed", NOW, "preconfirmation_no_longer_qualifies"),
        ("active", NOW, "preconfirmation_no_longer_qualifies"),
        ("downgraded", NOW, "preconfirmation_no_longer_qualifies"),
        ("exited", NOW, "preconfirmation_no_longer_qualifies"),
    ],
)
def test_signal_rejects_invalid_terminal_reason_state_combinations(
    state,
    closed_at,
    terminal_reason,
) -> None:
    with pytest.raises(
        ValidationError,
        match="invalid terminal fields for lifecycle state",
    ):
        _signal_for_validation(
            state=state,
            closed_at=closed_at,
            terminal_reason=terminal_reason,
        )


def test_signal_key_must_match_canonical_identity() -> None:
    with pytest.raises(ValidationError, match="signal_key must equal"):
        _signal_for_validation(signal_key="cn:industry:banking:1")


def test_lifecycle_context_rejects_duplicate_sector_signals() -> None:
    with pytest.raises(ValidationError, match="duplicate open signal sector_id"):
        LifecycleContext(
            open_signals=(
                _signal_for_validation(),
                _signal_for_validation(
                    signal_key="cn:industry:semiconductor:2",
                    instance_number=2,
                ),
            ),
            latest_instance_by_sector={"industry:semiconductor": 2},
        )


def test_lifecycle_context_rejects_inconsistent_latest_instance() -> None:
    with pytest.raises(
        ValidationError,
        match="latest instance must match signal instance",
    ):
        LifecycleContext(
            open_signals=(_signal_for_validation(),),
            latest_instance_by_sector={"industry:semiconductor": 2},
        )


def test_schedule_is_a_supported_snapshot_trigger() -> None:
    snapshot = RadarRunSnapshot(
        run_key="cn:20260721T070000Z:schedule",
        market="cn",
        trigger="schedule",
        as_of=NOW,
        quality="complete",
        scoring_version="cn-v1",
        sectors=(),
        provider_trace=(),
    )

    assert snapshot.trigger == "schedule"
