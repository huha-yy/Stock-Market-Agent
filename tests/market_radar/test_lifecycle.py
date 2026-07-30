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
    return SectorScore(
        sector_id=sector_id,
        name=sector_id.rsplit(":", 1)[-1].title(),
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
        observation={"bar_status": bar_status},
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
    ) -> RadarRunSnapshot:
        sectors = tuple(
            _sector_score(sector_id, bar_status=bar_status)
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
    ) -> LifecycleContext:
        if previous_state is None:
            return LifecycleContext(
                latest_instance_by_sector={sector_id: latest_instance}
            )
        previous_at = NOW - timedelta(hours=1)
        signal = LifecycleSignal(
            signal_key=f"cn:{sector_id}:{max(latest_instance, 1)}",
            sector_id=sector_id,
            instance_number=max(latest_instance, 1),
            state=previous_state,
            first_run_key="cn:20260721T060000Z:manual",
            current_run_key="cn:20260721T060000Z:manual",
            effective_at=previous_at,
            qualifying_streak=1,
            confidence=0.7,
            etf_code="510999",
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
