from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, event, func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError

from src.config import Config
from src.market_radar.models import (
    EtfDefinition,
    EtfComponentScores,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    PositionSuggestion,
    RadarRunSnapshot,
    RegimeComponents,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)
from src.market_radar.lifecycle import (
    LifecycleContext,
    LifecycleEvaluation,
    LifecycleSignal,
    MarketRadarLifecycleEngine,
)
from src.market_radar.observation_builder import (
    ConstituentEvidence,
    canonical_constituent_set_key,
)
from src.market_radar.repository import ConstituentSetContent, MarketRadarRepository
from src.market_radar.session_policy import RadarRunDecision
from src.storage import (
    DatabaseManager,
    RadarConstituentObservationRecord,
    RadarConstituentSetRecord,
    RadarEtfObservationRecord,
    RadarEtfSelectionRecord,
    RadarPositionPlanRecord,
    RadarRegimeAssessmentRecord,
    RadarRunAttemptRecord,
    RadarRunRecord,
    RadarSectorSnapshotRecord,
    RadarSignalTransitionRecord,
    RadarUniverseRecord,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
TRACKED_METRICS = tuple(SectorObservation.tracked_metric_fields)


@pytest.fixture()
def isolated_db(tmp_path):
    old_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "market_radar.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_path


def _score(
    sector_id: str = "industry:semiconductor",
    *,
    name: str = "Semiconductor",
    score: float = 70.0,
    observed_at: datetime = NOW,
    constituent_set_key: str | None = None,
    evidence_date: date | None = None,
    membership_source: str = "membership-fixture",
) -> SectorScore:
    raw_reference = {"provider": {"name": "fixture"}}
    if constituent_set_key is not None:
        raw_reference = {
            "schema": "market-radar-observation-v2a",
            "data_date": evidence_date or observed_at.date(),
            "capabilities": {
                "membership": {"source": membership_source},
            },
            "constituent_set_key": constituent_set_key,
        }
    observation = SectorObservation(
        sector_id=sector_id,
        name=name,
        kind="industry",
        observed_at=observed_at,
        source="fixture",
        freshness_seconds=0,
        quality="partial",
        return_1d_pct=2.5,
        missing_fields=TRACKED_METRICS[1:],
        raw_reference=raw_reference,
    )
    return SectorScore(
        sector_id=sector_id,
        name=name,
        kind="industry",
        scoring_version="cn-v1",
        gross_score=score + 2.0,
        risk_deduction=2.0,
        score=score,
        confidence=0.8,
        state="improving",
        factors={"trend": {"value": 20.0}},
        risk_reasons=["concentration"],
        missing_fields=observation.missing_fields,
        source="fixture",
        observed_at=observed_at,
        quality="partial",
        observation=observation.model_dump(mode="json"),
    )


def _snapshot(
    *,
    run_key: str = "cn:20260721T060000Z:manual",
    as_of: datetime = NOW,
    sectors: tuple[SectorScore, ...] | None = None,
) -> RadarRunSnapshot:
    return RadarRunSnapshot(
        run_key=run_key,
        market="cn",
        trigger="manual",
        as_of=as_of,
        quality="partial",
        scoring_version="cn-v1",
        sectors=sectors or (_score(observed_at=as_of),),
        provider_trace=[{"source": "fixture", "result": {"status": "ok"}}],
    )


def _phase2b_evidence(
    *,
    current_price: float = 1.2,
    selection_score: float = 80.0,
    regime_score: float = 70.0,
    total_position_max_pct: float = 60.0,
    as_of: datetime = NOW,
) -> tuple[
    tuple[EtfObservation, ...],
    tuple[EtfSelection, ...],
    MarketRegimeAssessment,
    PositionPlan,
]:
    observation = EtfObservation(
        sector_id="industry:semiconductor",
        code="512480",
        name="Semiconductor ETF",
        observed_at=as_of,
        data_date=None,
        bar_status=None,
        source="provider-fixture",
        quality="partial",
        freshness_seconds=30,
        mapping_effective_from=date(2026, 1, 1),
        current_price=current_price,
        missing_fields=tuple(
            field
            for field in EtfObservation.tracked_metric_fields
            if field != "current_price"
        ),
        raw_reference={"provider": "fixture"},
    )
    selection = EtfSelection(
        sector_id=observation.sector_id,
        code=observation.code,
        name=observation.name,
        status="best_supported",
        eligible=True,
        rank=1,
        score=selection_score,
        confidence=0.9,
        components=EtfComponentScores(
            liquidity=90.0,
            trend=80.0,
            tracking_quality=70.0,
            cost=60.0,
            size=50.0,
        ),
        effective_weights={"liquidity": 100.0},
        reason_codes=("supported",),
        observation=observation,
    )
    regime = MarketRegimeAssessment(
        as_of=as_of,
        score=regime_score,
        regime="selective",
        confidence=0.8,
        coverage=0.9,
        components=RegimeComponents(
            benchmark_trend=75.0,
            positive_sector_diffusion=70.0,
            flow_diffusion=65.0,
            liquidity_diffusion=60.0,
            non_risk_sector_share=80.0,
        ),
        cohort_sector_ids=("industry:semiconductor",),
    )
    plan = PositionPlan(
        as_of=as_of,
        regime="selective",
        total_position_min_pct=35.0,
        total_position_max_pct=total_position_max_pct,
        suggestions=(
            PositionSuggestion(
                sector_id=selection.sector_id,
                sector_name="Semiconductor",
                sector_rank=1,
                etf_code=selection.code,
                etf_status="best_supported",
                sector_cap_pct=12.0,
                etf_cap_pct=12.0,
                joint_confidence=0.8,
            ),
        ),
        correlation_coverage=1.0,
        confidence=0.8,
        reason_codes=("selective",),
    )
    return (observation,), (selection,), regime, plan


def _phase2b_snapshot(
    evidence: ConstituentEvidence,
    *,
    selection_score: float = 80.0,
    regime_score: float = 70.0,
    total_position_max_pct: float = 60.0,
) -> tuple[RadarRunSnapshot, tuple[EtfObservation, ...]]:
    observations, selections, regime, plan = _phase2b_evidence(
        selection_score=selection_score,
        regime_score=regime_score,
        total_position_max_pct=total_position_max_pct,
        as_of=evidence.observed_at,
    )
    data = _enriched_snapshot(evidence).model_dump(mode="json")
    data.update(etfs=selections, regime=regime, position_plan=plan)
    return RadarRunSnapshot.model_validate(data), observations


def _scheduled_bundle(
    *,
    run_key: str = "cn:20260721T060000Z:schedule",
    as_of: datetime = NOW,
    context: LifecycleContext | None = None,
    evidence_source: str = "membership-fixture",
) -> dict[str, object]:
    evidence = _constituent_evidence(
        observed_at=as_of,
        source=evidence_source,
    )
    snapshot, observations = _phase2b_snapshot(evidence)
    observation_data = observations[0].model_dump(mode="json")
    observation_data["observed_at"] = as_of
    observations = (EtfObservation.model_validate(observation_data),)
    snapshot_data = snapshot.model_dump(mode="json")
    snapshot_data.update(run_key=run_key, trigger="schedule", as_of=as_of)
    snapshot_data["etfs"][0]["observation"] = observations[0]
    snapshot_data["regime"]["as_of"] = as_of
    snapshot_data["position_plan"]["as_of"] = as_of
    snapshot = RadarRunSnapshot.model_validate(snapshot_data)
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot,
        context or LifecycleContext(),
        run_kind="intraday",
    )
    return {
        "sectors": [_sector_definition()],
        "evidence": [evidence],
        "etf_observations": observations,
        "snapshot": snapshot,
        "evaluation": evaluation,
    }


def _intraday_decision(*, decided_at: datetime = NOW) -> RadarRunDecision:
    return RadarRunDecision(
        kind="intraday_due",
        decided_at=decided_at,
        trading_date=date(2026, 7, 21),
        attempt_key="cn:intraday:2026-07-21:morning:1400",
        session_segment="morning",
        slot_start=decided_at,
    )


def _constituent_evidence(
    *,
    sector_id: str = "industry:semiconductor",
    source: str = "membership-fixture",
    data_date: date = NOW.date(),
    observed_at: datetime = NOW,
    codes: tuple[str, ...] = ("000001", "300750", "600519"),
) -> ConstituentEvidence:
    return ConstituentEvidence(
        market="cn",
        sector_id=sector_id,
        source=source,
        data_date=data_date,
        observed_at=observed_at,
        codes=codes,
        set_key=canonical_constituent_set_key(
            "cn",
            sector_id,
            source,
            codes,
        ),
    )


def _sector_definition(
    *,
    sector_id: str = "industry:semiconductor",
    name: str = "Semiconductor",
) -> SectorDefinition:
    return SectorDefinition(
        sector_id=sector_id,
        kind="industry",
        name=name,
        effective_from=date(2026, 1, 1),
    )


def _enriched_snapshot(
    evidence: ConstituentEvidence,
    *,
    run_key: str = "cn:20260721T060000Z:manual",
) -> RadarRunSnapshot:
    return _snapshot(
        run_key=run_key,
        as_of=evidence.observed_at,
        sectors=(
            _score(
                evidence.sector_id,
                observed_at=evidence.observed_at,
                constituent_set_key=evidence.set_key,
                evidence_date=evidence.data_date,
                membership_source=evidence.source,
            ),
        ),
    )


def _run_concurrently_after_first_select(db, table_name, operation):
    query_count = 0
    query_count_lock = threading.Lock()
    start = threading.Barrier(2)

    def delay_first_select(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal query_count
        if f"FROM {table_name}" not in statement:
            return
        with query_count_lock:
            query_count += 1
            is_first = query_count == 1
        if is_first:
            time.sleep(0.25)

    def run_operation():
        start.wait()
        return operation()

    event.listen(db._engine, "after_cursor_execute", delay_first_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_operation) for _ in range(2)]
            return [future.result(timeout=10) for future in futures]
    finally:
        event.remove(db._engine, "after_cursor_execute", delay_first_select)


def _capture_non_sqlite_statements(db, operation):
    statements = []

    def capture_statement(
        _connection,
        clauseelement,
        _multiparams,
        _params,
        _execution_options,
    ) -> None:
        statements.append(clauseelement)

    event.listen(db._engine, "before_execute", capture_statement)
    db._is_sqlite_engine = False
    try:
        return operation(), statements
    finally:
        db._is_sqlite_engine = True
        event.remove(db._engine, "before_execute", capture_statement)


def test_tables_are_created(isolated_db) -> None:
    inspector = inspect(isolated_db._engine)
    names = set(inspector.get_table_names())

    assert {
        "radar_universe",
        "radar_constituent_sets",
        "radar_constituent_observations",
        "radar_runs",
        "radar_sector_snapshots",
        "radar_run_attempts",
        "radar_signal_instances",
        "radar_signal_transitions",
    } <= names
    snapshot_columns = {
        column["name"]: column
        for column in inspector.get_columns("radar_sector_snapshots")
    }
    assert snapshot_columns["position"]["nullable"] is True

    universe_indexes = {
        item["name"] for item in inspector.get_indexes("radar_universe")
    }
    run_index_details = {
        item["name"]: item for item in inspector.get_indexes("radar_runs")
    }
    run_indexes = set(run_index_details)
    snapshot_indexes = {
        item["name"] for item in inspector.get_indexes("radar_sector_snapshots")
    }
    assert "idx_radar_universe_market_kind" in universe_indexes
    assert {
        "ix_radar_runs_run_key",
        "ix_radar_runs_market",
        "ix_radar_runs_as_of",
    } <= run_indexes
    assert bool(run_index_details["ix_radar_runs_run_key"]["unique"])
    assert {
        "idx_radar_sector_history",
        "idx_radar_sector_run_position",
        "ix_radar_sector_snapshots_run_id",
    } <= snapshot_indexes

    universe_uniques = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("radar_universe")
    }
    snapshot_uniques = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("radar_sector_snapshots")
    }
    assert universe_uniques["uix_radar_universe_effective"] == (
        "sector_id",
        "effective_from",
    )
    assert snapshot_uniques["uix_radar_run_sector"] == ("run_id", "sector_id")

    foreign_keys = inspector.get_foreign_keys("radar_sector_snapshots")
    assert any(
        tuple(item["constrained_columns"]) == ("run_id",)
        and item["referred_table"] == "radar_runs"
        and tuple(item["referred_columns"]) == ("id",)
        and item["options"].get("ondelete") == "CASCADE"
        for item in foreign_keys
    )

    constituent_set_pk = inspector.get_pk_constraint("radar_constituent_sets")
    assert tuple(constituent_set_pk["constrained_columns"]) == ("set_key",)
    constituent_set_indexes = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_indexes("radar_constituent_sets")
    }
    assert constituent_set_indexes["idx_radar_constituent_set_identity"] == (
        "market",
        "sector_id",
        "source",
    )

    observation_uniques = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints(
            "radar_constituent_observations"
        )
    }
    assert observation_uniques["uix_radar_constituent_observation_identity"] == (
        "market",
        "sector_id",
        "data_date",
        "source",
    )
    observation_indexes = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_indexes("radar_constituent_observations")
    }
    assert observation_indexes[
        "idx_radar_constituent_observation_history"
    ] == ("market", "sector_id", "data_date")
    assert observation_indexes["idx_radar_constituent_observation_set_key"] == (
        "set_key",
    )
    constituent_foreign_keys = inspector.get_foreign_keys(
        "radar_constituent_observations"
    )
    assert any(
        tuple(item["constrained_columns"]) == ("set_key",)
        and item["referred_table"] == "radar_constituent_sets"
        and tuple(item["referred_columns"]) == ("set_key",)
        for item in constituent_foreign_keys
    )

    expected_indexes = {
        "radar_run_attempts": {
            "ix_radar_run_attempts_market": ("market",),
            "ix_radar_run_attempts_trading_date": ("trading_date",),
        },
        "radar_signal_instances": {
            "ix_radar_signal_instances_market": ("market",),
            "ix_radar_signal_instances_sector_id": ("sector_id",),
        },
        "radar_signal_transitions": {
            "ix_radar_signal_transitions_signal_key": ("signal_key",),
            "ix_radar_signal_transitions_effective_run_id": ("effective_run_id",),
        },
    }
    for table_name, expected in expected_indexes.items():
        actual = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_indexes(table_name)
        }
        assert expected.items() <= actual.items()


def test_phase2b_tables_are_additive_and_cascade_with_run(isolated_db) -> None:
    inspector = inspect(isolated_db._engine)
    table_shapes = {
        "radar_etf_observations": {
            "run_id",
            "sector_id",
            "code",
            "position",
            "observation_json",
        },
        "radar_etf_selections": {
            "run_id",
            "sector_id",
            "code",
            "position",
            "selection_json",
        },
        "radar_regime_assessments": {"run_id", "assessment_json"},
        "radar_position_plans": {"run_id", "plan_json"},
    }
    assert set(table_shapes) <= set(inspector.get_table_names())

    for table_name, expected_columns in table_shapes.items():
        columns = {
            item["name"]: item for item in inspector.get_columns(table_name)
        }
        assert expected_columns <= set(columns)
        foreign_keys = inspector.get_foreign_keys(table_name)
        assert any(
            tuple(item["constrained_columns"]) == ("run_id",)
            and item["referred_table"] == "radar_runs"
            and tuple(item["referred_columns"]) == ("id",)
            and item["options"].get("ondelete") == "CASCADE"
            for item in foreign_keys
        )

    for table_name in ("radar_etf_observations", "radar_etf_selections"):
        uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(table_name)
        }
        assert ("run_id", "sector_id", "code") in uniques
        columns = {
            item["name"]: item for item in inspector.get_columns(table_name)
        }
        assert columns["position"]["nullable"] is False

    for table_name in ("radar_regime_assessments", "radar_position_plans"):
        uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(table_name)
        }
        assert ("run_id",) in uniques


def test_phase2b_evidence_round_trips_through_models_and_snapshot(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot, observations = _phase2b_snapshot(evidence)

    run_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        etf_observations=observations,
        snapshot=snapshot,
    )

    loaded = repo.load_phase2b_evidence(run_id)
    assert loaded == (
        observations,
        snapshot.etfs,
        snapshot.regime,
        snapshot.position_plan,
    )
    assert all(isinstance(item, EtfObservation) for item in loaded[0])
    assert all(isinstance(item, EtfSelection) for item in loaded[1])
    assert isinstance(loaded[2], MarketRegimeAssessment)
    assert isinstance(loaded[3], PositionPlan)
    assert repo.get_run(run_id) == snapshot

    with isolated_db.session_scope() as session:
        selection_row = session.scalar(
            select(RadarEtfSelectionRecord).where(
                RadarEtfSelectionRecord.run_id == run_id
            )
        )
        assert selection_row is not None
        assert selection_row.position == 0


def test_phase2b_rows_cascade_when_run_is_deleted(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot, observations = _phase2b_snapshot(evidence)
    run_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        etf_observations=observations,
        snapshot=snapshot,
    )

    with isolated_db.session_scope() as session:
        session.execute(delete(RadarRunRecord).where(RadarRunRecord.id == run_id))

    with isolated_db.get_session() as session:
        for record_type in (
            RadarSectorSnapshotRecord,
            RadarEtfObservationRecord,
            RadarEtfSelectionRecord,
            RadarRegimeAssessmentRecord,
            RadarPositionPlanRecord,
        ):
            assert session.scalar(select(func.count()).select_from(record_type)) == 0


def test_phase2b_reads_legacy_run_without_policy_rows(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    legacy = _snapshot()

    run_id = repo.save_run(legacy)

    assert repo.load_phase2b_evidence(run_id) == ((), (), None, None)
    assert repo.get_run(run_id) == legacy


@pytest.mark.parametrize(
    "table_name",
    [
        "radar_etf_observations",
        "radar_etf_selections",
        "radar_regime_assessments",
        "radar_position_plans",
    ],
)
def test_phase2b_insert_failure_rolls_back_the_complete_enriched_run(
    isolated_db,
    table_name,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot, observations = _phase2b_snapshot(evidence)
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER reject_phase2b_insert
            BEFORE INSERT ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, 'rejected phase2b insert');
            END
            """
        )

    with pytest.raises(IntegrityError, match="rejected phase2b insert"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            etf_observations=observations,
            snapshot=snapshot,
        )

    record_types = (
        RadarUniverseRecord,
        RadarConstituentSetRecord,
        RadarConstituentObservationRecord,
        RadarRunRecord,
        RadarSectorSnapshotRecord,
        RadarEtfObservationRecord,
        RadarEtfSelectionRecord,
        RadarRegimeAssessmentRecord,
        RadarPositionPlanRecord,
    )
    with isolated_db.get_session() as session:
        for record_type in record_types:
            assert session.scalar(select(func.count()).select_from(record_type)) == 0


def test_phase2b_identical_retry_reuses_run_id(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot, observations = _phase2b_snapshot(evidence)

    first_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        etf_observations=observations,
        snapshot=snapshot,
    )
    second_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        etf_observations=observations,
        snapshot=snapshot,
    )

    assert first_id == second_id


@pytest.mark.parametrize("changed_entity", ["observation", "selection", "regime", "plan"])
def test_phase2b_retry_rejects_changed_semantics(
    isolated_db,
    changed_entity,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot, observations = _phase2b_snapshot(evidence)
    repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        etf_observations=observations,
        snapshot=snapshot,
    )

    changed_snapshot = snapshot
    changed_observations = observations
    if changed_entity == "observation":
        observation_data = observations[0].model_dump(mode="json")
        observation_data["current_price"] = 1.3
        changed_observations = (EtfObservation.model_validate(observation_data),)
    elif changed_entity == "selection":
        changed_snapshot, _ = _phase2b_snapshot(evidence, selection_score=81.0)
    elif changed_entity == "regime":
        changed_snapshot, _ = _phase2b_snapshot(evidence, regime_score=71.0)
    else:
        changed_snapshot, _ = _phase2b_snapshot(
            evidence,
            total_position_max_pct=61.0,
        )

    with pytest.raises(ValueError, match="semantic conflict"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            etf_observations=changed_observations,
            snapshot=changed_snapshot,
        )


def test_reserve_attempt_reuses_terminal_identity(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    run_id = repo.save_run(_snapshot())
    decision = _intraday_decision()

    first = repo.reserve_scheduled_attempt(decision, lease_seconds=900, now=NOW)
    repo.finish_scheduled_attempt(
        first.attempt_key,
        status="succeeded",
        run_id=run_id,
    )
    second = repo.reserve_scheduled_attempt(
        decision,
        lease_seconds=900,
        now=NOW + timedelta(hours=1),
    )

    assert second.acquired is False
    assert second.status == "succeeded"
    assert second.run_id == run_id


def test_started_attempt_can_only_be_reclaimed_after_lease(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    decision = _intraday_decision()

    first = repo.reserve_scheduled_attempt(decision, lease_seconds=900, now=NOW)
    before_expiry = repo.reserve_scheduled_attempt(
        decision,
        lease_seconds=900,
        now=NOW + timedelta(seconds=899),
    )
    at_expiry = repo.reserve_scheduled_attempt(
        decision,
        lease_seconds=900,
        now=NOW + timedelta(seconds=900),
    )

    assert first.acquired is True
    assert before_expiry.acquired is False
    assert at_expiry.acquired is True


def test_non_sqlite_expired_attempt_reclaim_locks_the_row(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    decision = _intraday_decision()
    repo.reserve_scheduled_attempt(decision, lease_seconds=900, now=NOW)
    reservation, statements = _capture_non_sqlite_statements(
        isolated_db,
        lambda: repo.reserve_scheduled_attempt(
            decision,
            lease_seconds=900,
            now=NOW + timedelta(seconds=900),
        ),
    )

    attempt_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if "radar_run_attempts" in str(statement)
    ]
    assert reservation.acquired is True
    assert any("FOR UPDATE" in statement for statement in attempt_sql)


def test_non_sqlite_finish_attempt_locks_the_row(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    reservation = repo.reserve_scheduled_attempt(
        _intraday_decision(),
        lease_seconds=900,
        now=NOW,
    )

    _, statements = _capture_non_sqlite_statements(
        isolated_db,
        lambda: repo.finish_scheduled_attempt(
            reservation.attempt_key,
            status="failed",
            failure_category="provider_error",
        ),
    )

    attempt_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if "radar_run_attempts" in str(statement)
    ]
    assert any("FOR UPDATE" in statement for statement in attempt_sql)


def test_concurrent_attempt_reservations_have_one_database_winner(
    isolated_db,
) -> None:
    decision = _intraday_decision()

    reservations = _run_concurrently_after_first_select(
        isolated_db,
        "radar_run_attempts",
        lambda: MarketRadarRepository(isolated_db).reserve_scheduled_attempt(
            decision,
            lease_seconds=900,
            now=NOW,
        ),
    )

    assert sorted(item.acquired for item in reservations) == [False, True]
    assert {item.attempt_key for item in reservations} == {decision.attempt_key}


def test_finish_attempt_is_semantically_idempotent_and_bounds_summary(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    reservation = repo.reserve_scheduled_attempt(
        _intraday_decision(),
        lease_seconds=900,
        now=NOW,
    )
    long_summary = "x" * 700

    repo.finish_scheduled_attempt(
        reservation.attempt_key,
        status="failed",
        failure_category="provider_error",
        failure_summary=long_summary,
    )
    repo.finish_scheduled_attempt(
        reservation.attempt_key,
        status="failed",
        failure_category="provider_error",
        failure_summary=long_summary,
    )

    with isolated_db.get_session() as session:
        stored_summary = session.execute(
            select(text("failure_summary")).select_from(
                text("radar_run_attempts")
            )
        ).scalar_one()
    assert stored_summary == "x" * 512

    with pytest.raises(ValueError, match="already terminal"):
        repo.finish_scheduled_attempt(
            reservation.attempt_key,
            status="skipped",
            reason_code="duplicate_slot",
        )


@pytest.mark.parametrize(
    ("column_name", "corrupted_value", "reserve_at", "terminalize"),
    [
        ("market", "us", NOW + timedelta(seconds=1), True),
        ("trigger_type", "eod_due", NOW + timedelta(seconds=900), False),
        (
            "trading_date",
            date(2026, 7, 22),
            NOW + timedelta(seconds=1),
            False,
        ),
    ],
)
def test_attempt_key_rejects_conflicting_decision_identity(
    isolated_db,
    column_name,
    corrupted_value,
    reserve_at,
    terminalize,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    decision = _intraday_decision()
    reservation = repo.reserve_scheduled_attempt(
        decision,
        lease_seconds=900,
        now=NOW,
    )
    if terminalize:
        repo.finish_scheduled_attempt(
            reservation.attempt_key,
            status="skipped",
            reason_code="calendar_unavailable",
        )
    with isolated_db.session_scope() as session:
        row = session.get(RadarRunAttemptRecord, decision.attempt_key)
        setattr(row, column_name, corrupted_value)

    with pytest.raises(ValueError, match="identity conflict"):
        repo.reserve_scheduled_attempt(
            decision,
            lease_seconds=900,
            now=reserve_at,
        )


def test_finish_attempt_rejects_invalid_terminal_status(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    reservation = repo.reserve_scheduled_attempt(
        _intraday_decision(),
        lease_seconds=900,
        now=NOW,
    )

    with pytest.raises(ValueError, match="terminal status"):
        repo.finish_scheduled_attempt(
            reservation.attempt_key,
            status="cancelled",
        )

    duplicate = repo.reserve_scheduled_attempt(
        _intraday_decision(),
        lease_seconds=900,
        now=NOW + timedelta(seconds=1),
    )
    assert duplicate.status == "started"


def test_scheduled_lifecycle_context_updates_current_row_and_appends_transitions(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    first_bundle = _scheduled_bundle()
    first_id = repo.save_scheduled_enriched_run(**first_bundle)
    first_context = repo.load_lifecycle_context()

    second_bundle = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=first_context,
    )
    second_id = repo.save_scheduled_enriched_run(**second_bundle)
    current = repo.load_lifecycle_context()

    assert first_id != second_id
    assert len(current.open_signals) == 1
    assert current.open_signals[0].state == "confirmed"
    assert current.open_signals[0].current_run_key == second_bundle["snapshot"].run_key
    assert current.latest_instance_by_sector == {"industry:semiconductor": 1}
    with isolated_db.get_session() as session:
        assert session.execute(
            select(func.count()).select_from(text("radar_signal_instances"))
        ).scalar_one() == 1
        assert session.execute(
            select(func.count()).select_from(text("radar_signal_transitions"))
        ).scalar_one() == 2


def test_identical_historical_retry_is_accepted_after_signal_advances(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    first_bundle = _scheduled_bundle()
    first_id = repo.save_scheduled_enriched_run(**first_bundle)
    second_bundle = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=repo.load_lifecycle_context(),
    )
    repo.save_scheduled_enriched_run(**second_bundle)
    advanced_context = repo.load_lifecycle_context()

    assert repo.save_scheduled_enriched_run(**first_bundle) == first_id
    assert repo.load_lifecycle_context() == advanced_context


def test_historical_retry_rejects_empty_or_conflicting_evaluation(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    first_bundle = _scheduled_bundle()
    repo.save_scheduled_enriched_run(**first_bundle)
    second_bundle = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=repo.load_lifecycle_context(),
    )
    repo.save_scheduled_enriched_run(**second_bundle)
    original = first_bundle["evaluation"]
    changed_signal = original.signals[0].model_copy(update={"confidence": 0.7})
    retries = (
        LifecycleEvaluation(run_key=original.run_key, signals=(), transitions=()),
        LifecycleEvaluation(
            run_key=original.run_key,
            signals=(changed_signal,),
            transitions=original.transitions,
        ),
    )

    for evaluation in retries:
        with pytest.raises(ValueError, match="semantic conflict"):
            repo.save_scheduled_enriched_run(
                **{**first_bundle, "evaluation": evaluation}
            )


def test_non_sqlite_lifecycle_update_locks_current_signal_rows(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    repo.save_scheduled_enriched_run(**_scheduled_bundle())
    second_bundle = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=repo.load_lifecycle_context(),
    )
    _, statements = _capture_non_sqlite_statements(
        isolated_db,
        lambda: repo.save_scheduled_enriched_run(**second_bundle),
    )

    signal_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if "radar_signal_instances" in str(statement)
    ]
    assert any("FOR UPDATE" in statement for statement in signal_sql)


def test_stale_lifecycle_branch_rolls_back_before_extra_transition(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    repo.save_scheduled_enriched_run(**_scheduled_bundle())
    stale_context = repo.load_lifecycle_context()
    winner = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=stale_context,
    )
    loser = _scheduled_bundle(
        run_key="cn:20260721T063100Z:schedule",
        as_of=NOW + timedelta(minutes=31),
        context=stale_context,
    )
    repo.save_scheduled_enriched_run(**winner)

    with pytest.raises(ValueError, match="semantic conflict"):
        repo.save_scheduled_enriched_run(**loser)

    assert repo.get_run_by_key(loser["snapshot"].run_key) is None
    with isolated_db.get_session() as session:
        transition_count = session.scalar(
            select(func.count()).select_from(RadarSignalTransitionRecord)
        )
    assert transition_count == 2


def test_scheduled_retry_rejects_changed_signal_semantics(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    bundle = _scheduled_bundle()
    run_id = repo.save_scheduled_enriched_run(**bundle)
    evaluation = bundle["evaluation"]
    changed_signal_data = evaluation.signals[0].model_dump(mode="json")
    changed_signal_data["confidence"] = 0.7
    changed_evaluation = LifecycleEvaluation(
        run_key=evaluation.run_key,
        signals=(LifecycleSignal.model_validate(changed_signal_data),),
        transitions=evaluation.transitions,
    )

    with pytest.raises(ValueError, match="semantic conflict"):
        repo.save_scheduled_enriched_run(
            **{**bundle, "evaluation": changed_evaluation}
        )

    assert repo.get_run_by_key(bundle["snapshot"].run_key) is not None
    assert repo.save_scheduled_enriched_run(**bundle) == run_id


def test_scheduled_retry_accepts_timezone_equivalent_evaluation(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    bundle = _scheduled_bundle()
    run_id = repo.save_scheduled_enriched_run(**bundle)
    evaluation = bundle["evaluation"]
    shanghai = ZoneInfo("Asia/Shanghai")
    equivalent = LifecycleEvaluation(
        run_key=evaluation.run_key,
        signals=tuple(
            signal.model_copy(
                update={"effective_at": signal.effective_at.astimezone(shanghai)}
            )
            for signal in evaluation.signals
        ),
        transitions=tuple(
            transition.model_copy(
                update={
                    "effective_at": transition.effective_at.astimezone(shanghai)
                }
            )
            for transition in evaluation.transitions
        ),
    )

    assert equivalent == evaluation
    assert repo.save_scheduled_enriched_run(
        **{**bundle, "evaluation": equivalent}
    ) == run_id


def test_scheduled_retry_rejects_omitted_transition(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    bundle = _scheduled_bundle()
    repo.save_scheduled_enriched_run(**bundle)
    evaluation = bundle["evaluation"]
    incomplete = LifecycleEvaluation(
        run_key=evaluation.run_key,
        signals=evaluation.signals,
        transitions=(),
    )

    with pytest.raises(ValueError, match="semantic conflict"):
        repo.save_scheduled_enriched_run(
            **{**bundle, "evaluation": incomplete}
        )


def test_real_transition_failure_rolls_back_scheduled_lifecycle_transaction(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    repo.save_scheduled_enriched_run(**_scheduled_bundle())
    baseline_context = repo.load_lifecycle_context()
    bundle = _scheduled_bundle(
        run_key="cn:20260721T063000Z:schedule",
        as_of=NOW + timedelta(minutes=30),
        context=baseline_context,
        evidence_source="membership-rollback",
    )
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_lifecycle_transition
            BEFORE INSERT ON radar_signal_transitions
            BEGIN
                SELECT RAISE(ABORT, 'rejected lifecycle transition');
            END
            """
        )
    executed_statements = []

    def capture_sql(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        executed_statements.append(statement.lower())

    event.listen(isolated_db._engine, "before_cursor_execute", capture_sql)
    try:
        with pytest.raises(IntegrityError, match="rejected lifecycle transition"):
            repo.save_scheduled_enriched_run(**bundle)
    finally:
        event.remove(isolated_db._engine, "before_cursor_execute", capture_sql)

    assert repo.get_run_by_key(bundle["snapshot"].run_key) is None
    assert repo.load_lifecycle_context() == baseline_context
    signal_update_index = next(
        index
        for index, statement in enumerate(executed_statements)
        if statement.startswith("update radar_signal_instances")
    )
    transition_insert_index = next(
        index
        for index, statement in enumerate(executed_statements)
        if statement.startswith("insert into radar_signal_transitions")
    )
    assert signal_update_index < transition_insert_index
    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count()).select_from(RadarRunRecord)) == 1
        assert session.scalar(
            select(func.count()).select_from(RadarEtfObservationRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(RadarConstituentSetRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(RadarConstituentObservationRecord)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(RadarSignalTransitionRecord)
        ) == 1


def test_existing_snapshot_table_gains_nullable_position_and_index(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy_radar.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE radar_sector_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL
            )
            """
        )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        db = DatabaseManager.get_instance()
        inspector = inspect(db._engine)

        columns = {
            item["name"]: item
            for item in inspector.get_columns("radar_sector_snapshots")
        }
        indexes = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_indexes("radar_sector_snapshots")
        }
        assert columns["position"]["nullable"] is True
        assert indexes["idx_radar_sector_run_position"] == ("run_id", "position")
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_existing_lifecycle_tables_gain_indexes_without_rewriting_rows(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy_lifecycle.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE radar_run_attempts (
                attempt_key TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                trading_date DATE NOT NULL,
                legacy_note TEXT
            );
            CREATE TABLE radar_signal_instances (
                signal_key TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                sector_id TEXT NOT NULL
            );
            CREATE TABLE radar_signal_transitions (
                transition_key TEXT PRIMARY KEY,
                signal_key TEXT NOT NULL,
                effective_run_id INTEGER NOT NULL
            );
            CREATE TABLE radar_runs (
                id INTEGER PRIMARY KEY,
                run_key TEXT NOT NULL,
                legacy_note TEXT
            );
            INSERT INTO radar_run_attempts
                (attempt_key, market, trading_date, legacy_note)
            VALUES
                ('legacy-attempt', 'cn', '2026-07-21', 'keep-me');
            INSERT INTO radar_runs (id, run_key, legacy_note)
            VALUES (7, 'legacy-run', 'keep-run');
            """
        )
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        db = DatabaseManager.get_instance()
        inspector = inspect(db._engine)
        expected_indexes = {
            "radar_run_attempts": {
                "ix_radar_run_attempts_market",
                "ix_radar_run_attempts_trading_date",
            },
            "radar_signal_instances": {
                "ix_radar_signal_instances_market",
                "ix_radar_signal_instances_sector_id",
            },
            "radar_signal_transitions": {
                "ix_radar_signal_transitions_signal_key",
                "ix_radar_signal_transitions_effective_run_id",
            },
        }
        for table_name, expected in expected_indexes.items():
            actual = {
                item["name"] for item in inspector.get_indexes(table_name)
            }
            assert expected <= actual

        with db._engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT attempt_key, legacy_note FROM radar_run_attempts"
            ).one()
        assert row == ("legacy-attempt", "keep-me")
        assert "legacy_note" in {
            item["name"]
            for item in inspector.get_columns("radar_run_attempts")
        }
        run_columns = {
            item["name"]: item for item in inspector.get_columns("radar_runs")
        }
        assert run_columns["lifecycle_evaluation_json"]["nullable"] is True
        with db._engine.connect() as connection:
            legacy_run = connection.exec_driver_sql(
                "SELECT id, legacy_note, lifecycle_evaluation_json "
                "FROM radar_runs"
            ).one()
        assert legacy_run == (7, "keep-run", None)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


@pytest.mark.parametrize(
    ("column_sql", "expected_alter_count"),
    [
        ("", 1),
        (", lifecycle_evaluation_json TEXT", 0),
    ],
)
def test_non_sqlite_lifecycle_schema_adds_only_missing_evaluation_column(
    column_sql,
    expected_alter_count,
) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE radar_runs (id INTEGER PRIMARY KEY"
            f"{column_sql})"
        )
    manager = object.__new__(DatabaseManager)
    manager._engine = engine
    manager._is_sqlite_engine = False
    statements = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        manager._ensure_market_radar_lifecycle_schema()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    columns = {item["name"] for item in inspect(engine).get_columns("radar_runs")}
    alter_statements = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("ALTER TABLE")
    ]
    assert "lifecycle_evaluation_json" in columns
    assert len(alter_statements) == expected_alter_count
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("DROP ", "DELETE ", "TRUNCATE ", "RENAME ")
    )
    engine.dispose()


@pytest.mark.parametrize(
    ("sqlstate", "message", "should_raise"),
    [
        ("42701", "column lifecycle_evaluation_json already exists", False),
        ("XX000", "database unavailable", True),
    ],
)
def test_non_sqlite_lifecycle_schema_handles_only_duplicate_column_races(
    monkeypatch,
    sqlstate,
    message,
    should_raise,
) -> None:
    class DriverError(Exception):
        def __init__(self) -> None:
            super().__init__(message)
            self.sqlstate = sqlstate

    error = OperationalError("ALTER TABLE", {}, DriverError())
    statements = []

    class Connection:
        def exec_driver_sql(self, statement):
            statements.append(statement)
            raise error

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, _exc_type, _exc_value, _traceback):
            return False

    class Engine:
        dialect = postgresql.dialect()

        @staticmethod
        def begin():
            return Transaction()

    class Inspector:
        @staticmethod
        def has_table(_table_name):
            return True

        @staticmethod
        def get_columns(_table_name):
            return [{"name": "id"}]

    manager = object.__new__(DatabaseManager)
    manager._engine = Engine()
    manager._is_sqlite_engine = False
    monkeypatch.setattr("src.storage.inspect", lambda _engine: Inspector())

    if should_raise:
        with pytest.raises(OperationalError, match=message):
            manager._ensure_market_radar_lifecycle_schema()
    else:
        manager._ensure_market_radar_lifecycle_schema()

    assert statements == [
        "ALTER TABLE radar_runs ADD COLUMN lifecycle_evaluation_json TEXT"
    ]


def test_save_run_is_idempotent_and_preserves_first_snapshot(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    original = _snapshot()
    conflicting_retry = _snapshot(
        sectors=(_score(name="Changed name", score=55.0),),
    )

    first_id = repo.save_run(original)
    second_id = repo.save_run(original)
    conflicting_id = repo.save_run(conflicting_retry)

    assert first_id == second_id == conflicting_id
    latest = repo.get_latest_run("cn")
    assert latest == original
    assert repo.list_sector_snapshots(first_id) == list(original.sectors)


def test_get_run_reconstructs_the_exact_snapshot_by_id(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot()

    run_id = repo.save_run(snapshot)

    assert repo.get_run(run_id) == snapshot
    assert repo.get_run(run_id + 1) is None


def test_save_run_retries_one_integrity_race_then_succeeds(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot()
    original_transaction = isolated_db._run_write_transaction
    attempts = 0
    winner_id = None

    def fail_once(operation_name, operation):
        nonlocal attempts, winner_id
        attempts += 1
        if attempts == 1:
            winner_id = original_transaction("concurrent-winner", operation)
            raise IntegrityError("INSERT", {}, RuntimeError("concurrent insert"))
        return original_transaction(operation_name, operation)

    monkeypatch.setattr(isolated_db, "_run_write_transaction", fail_once)

    run_id = repo.save_run(snapshot)

    assert attempts == 2
    assert run_id == winner_id
    assert repo.list_sector_snapshots(run_id) == list(snapshot.sectors)


def test_save_run_propagates_repeated_integrity_failure(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    attempts = 0

    def always_fail(_operation_name, _operation):
        nonlocal attempts
        attempts += 1
        raise IntegrityError("INSERT", {}, RuntimeError("persistent failure"))

    monkeypatch.setattr(isolated_db, "_run_write_transaction", always_fail)

    with pytest.raises(IntegrityError, match="persistent failure"):
        repo.save_run(_snapshot())

    assert attempts == 2


def test_concurrent_save_run_retries_return_the_same_snapshot_id(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot()

    run_ids = _run_concurrently_after_first_select(
        isolated_db,
        "radar_runs",
        lambda: repo.save_run(snapshot),
    )

    assert run_ids[0] == run_ids[1]
    assert repo.get_latest_run("cn") == snapshot


def test_sync_universe_round_trips_without_deleting_history(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    etf = EtfDefinition(
        code="512480",
        name="Semiconductor ETF",
        sector_id="industry:semiconductor",
        benchmark_code="931865",
        effective_from=date(2026, 1, 1),
    )
    first = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor",
        aliases=["Chips"],
        benchmark_code="931865",
        etfs=[etf],
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )
    second = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor Manufacturing",
        aliases=["Semiconductor"],
        effective_from=date(2027, 1, 1),
    )

    repo.sync_universe([first])
    repo.sync_universe([second])

    assert repo.list_universe(date(2026, 7, 21)) == [first]
    assert repo.list_universe(date(2027, 7, 21)) == [second]


def test_sync_universe_updates_same_version_without_adding_history(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    original = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor",
        effective_from=date(2026, 1, 1),
    )
    updated = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor Manufacturing",
        aliases=["Chips"],
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )

    repo.sync_universe([original])
    repo.sync_universe([updated])

    assert repo.list_universe(date(2026, 7, 21)) == [updated]
    with isolated_db.get_session() as session:
        count = session.scalar(select(func.count(RadarUniverseRecord.id)))
    assert count == 1


def test_sync_universe_retries_integrity_race_and_updates_winner(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    original_transaction = isolated_db._run_write_transaction
    attempts = 0
    effective_from = date(2026, 1, 1)
    updated = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor Manufacturing",
        aliases=["Chips"],
        effective_from=effective_from,
    )

    def fail_once(operation_name, operation):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            original_transaction(
                "concurrent-universe-winner",
                lambda session: session.add(
                    RadarUniverseRecord(
                        sector_id=updated.sector_id,
                        kind=updated.kind,
                        name="Semiconductor",
                        effective_from=effective_from,
                    )
                ),
            )
            raise IntegrityError("INSERT", {}, RuntimeError("concurrent insert"))
        return original_transaction(operation_name, operation)

    monkeypatch.setattr(isolated_db, "_run_write_transaction", fail_once)

    repo.sync_universe([updated])

    assert attempts == 2
    assert repo.list_universe(date(2026, 7, 21)) == [updated]
    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarUniverseRecord.id))) == 1


def test_concurrent_universe_sync_is_idempotent(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    sector = SectorDefinition(
        sector_id="industry:semiconductor",
        kind="industry",
        name="Semiconductor",
        effective_from=date(2026, 1, 1),
    )

    results = _run_concurrently_after_first_select(
        isolated_db,
        "radar_universe",
        lambda: repo.sync_universe([sector]),
    )

    assert results == [None, None]
    assert repo.list_universe(date(2026, 7, 21)) == [sector]


def test_failed_run_rolls_back_all_rows_and_keeps_latest_success(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    successful = _snapshot()
    successful_id = repo.save_run(successful)
    later = NOW + timedelta(hours=1)
    rejected_sector_id = "industry:reject-insert"
    failed = _snapshot(
        run_key="cn:20260721T070000Z:manual",
        as_of=later,
        sectors=(
            _score("industry:first-insert", name="First", observed_at=later),
            _score(rejected_sector_id, name="Rejected", observed_at=later),
        ),
    )
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_radar_sector_insert
            BEFORE INSERT ON radar_sector_snapshots
            WHEN NEW.sector_id = 'industry:reject-insert'
            BEGIN
                SELECT RAISE(ABORT, 'rejected test sector');
            END
            """
        )

    with pytest.raises(IntegrityError, match="rejected test sector"):
        repo.save_run(failed)

    assert repo.get_latest_run("cn") == successful
    with isolated_db.get_session() as session:
        run_count = session.scalar(select(func.count(RadarRunRecord.id)))
        sector_count = session.scalar(select(func.count(RadarSectorSnapshotRecord.id)))
    assert run_count == 1
    assert sector_count == 1
    assert repo.list_sector_snapshots(successful_id) == list(successful.sectors)


def test_save_run_round_trips_sector_order_exactly(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot(
        sectors=(
            _score("industry:z-low", name="Z Low", score=40.0),
            _score("industry:high", name="High", score=90.0),
            _score("industry:a-low", name="A Low", score=40.0),
        )
    )

    run_id = repo.save_run(snapshot)

    assert repo.list_sector_snapshots(run_id) == list(snapshot.sectors)
    assert repo.get_latest_run("cn") == snapshot


def test_legacy_null_positions_fall_back_to_insertion_order(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot(
        sectors=(
            _score("industry:z-low", name="Z Low", score=40.0),
            _score("industry:high", name="High", score=90.0),
            _score("industry:a-low", name="A Low", score=40.0),
        )
    )
    run_id = repo.save_run(snapshot)
    with isolated_db.session_scope() as session:
        rows = session.execute(
            select(RadarSectorSnapshotRecord).where(
                RadarSectorSnapshotRecord.run_id == run_id
            )
        ).scalars().all()
        for row in rows:
            row.position = None

    assert repo.list_sector_snapshots(run_id) == list(snapshot.sectors)


def test_save_run_rejects_empty_observation_before_writing(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    invalid_score = _score().model_copy(update={"observation": {}})

    with pytest.raises(ValueError, match="observation"):
        repo.save_run(_snapshot(sectors=(invalid_score,)))

    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarRunRecord.id))) == 0


@pytest.mark.parametrize(
    ("field_name", "mismatched_value"),
    [
        ("sector_id", "industry:other"),
        ("source", "other-provider"),
        ("observed_at", "2026-07-21T07:00:00Z"),
        ("quality", "stale"),
        ("missing_fields", ["return_5d_pct"]),
    ],
)
def test_save_run_rejects_mismatched_observation_before_writing(
    isolated_db,
    field_name,
    mismatched_value,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    score_data = _score().model_dump(mode="json")
    if field_name == "missing_fields":
        score_data[field_name] = mismatched_value
    else:
        score_data["observation"][field_name] = mismatched_value
    invalid_score = SectorScore.model_validate(score_data)

    with pytest.raises(ValueError, match=field_name):
        repo.save_run(_snapshot(sectors=(invalid_score,)))

    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarRunRecord.id))) == 0


def test_enriched_run_reuses_content_and_reads_immutable_evidence(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    first = _constituent_evidence()
    later = replace(
        first,
        data_date=first.data_date + timedelta(days=1),
        observed_at=first.observed_at + timedelta(days=1),
    )
    first_snapshot = _enriched_snapshot(first)
    later_snapshot = _enriched_snapshot(
        later,
        run_key="cn:20260722T060000Z:manual",
    )

    first_id = repo.save_enriched_run(
        [_sector_definition()], [first], first_snapshot
    )
    first_content = repo.get_constituent_set(first.set_key)
    later_id = repo.save_enriched_run(
        [_sector_definition()], [later], later_snapshot
    )
    later_content = repo.get_constituent_set(first.set_key)

    with isolated_db.get_session() as session:
        assert session.scalar(
            select(func.count(RadarConstituentSetRecord.set_key))
        ) == 1
        assert session.scalar(
            select(func.count(RadarConstituentObservationRecord.id))
        ) == 2
        stored_observed_at = session.scalar(
            select(RadarConstituentObservationRecord.observed_at).where(
                RadarConstituentObservationRecord.data_date == first.data_date
            )
        )
        assert stored_observed_at == first.observed_at.replace(tzinfo=None)
        assert stored_observed_at.tzinfo is None
    assert repo.get_run_by_key(first_snapshot.run_key) == first_snapshot
    assert repo.get_run_by_key("missing") is None
    assert first_content == later_content
    assert isinstance(first_content, ConstituentSetContent)
    assert first_content.set_key == first.set_key
    assert first_content.market == first.market
    assert first_content.sector_id == first.sector_id
    assert first_content.source == first.source
    assert first_content.codes == first.codes
    assert first_content.constituent_count == len(first.codes)
    assert first_content.created_at.tzinfo is not None
    assert not hasattr(first_content, "data_date")
    assert not hasattr(first_content, "observed_at")
    with pytest.raises(FrozenInstanceError):
        first_content.codes = ()
    assert repo.get_constituent_set("sha256:missing") is None
    assert repo.get_constituent_evidence(
        first.market,
        first.sector_id,
        first.data_date,
        first.source,
    ) == first
    assert repo.get_constituent_evidence(
        later.market,
        later.sector_id,
        later.data_date,
        later.source,
    ) == later
    assert repo.list_constituent_evidence_for_set(first.set_key) == (first, later)
    assert repo.resolve_run_constituent_evidence(first_id) == (first,)
    assert repo.resolve_snapshot_constituent_evidence(later_snapshot) == (later,)
    assert later_id != first_id


def test_constituent_set_lookup_does_not_require_an_observation(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    with isolated_db.session_scope() as session:
        session.add(
            RadarConstituentSetRecord(
                set_key=evidence.set_key,
                market=evidence.market,
                sector_id=evidence.sector_id,
                source=evidence.source,
                codes_json=json.dumps(list(evidence.codes), sort_keys=True),
                constituent_count=len(evidence.codes),
            )
        )

    content = repo.get_constituent_set(evidence.set_key)

    assert content is not None
    assert content.codes == evidence.codes
    assert content.constituent_count == len(evidence.codes)
    assert repo.list_constituent_evidence_for_set(evidence.set_key) == ()


def test_same_identity_and_set_is_idempotent_and_preserves_first_observed_at(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    first = _constituent_evidence()
    retry = replace(first, observed_at=first.observed_at + timedelta(minutes=5))

    repo.save_enriched_run(
        [_sector_definition()], [first], _enriched_snapshot(first)
    )
    repo.save_enriched_run(
        [_sector_definition()],
        [retry],
        _enriched_snapshot(retry, run_key="cn:20260721T060500Z:manual"),
    )

    with isolated_db.get_session() as session:
        assert session.scalar(
            select(func.count(RadarConstituentSetRecord.set_key))
        ) == 1
        assert session.scalar(
            select(func.count(RadarConstituentObservationRecord.id))
        ) == 1
    assert repo.get_constituent_evidence(
        first.market,
        first.sector_id,
        first.data_date,
        first.source,
    ) == first


def test_effective_first_observation_blocks_reverse_time_run_and_allows_later_run(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    first_observed = NOW + timedelta(minutes=2)
    first = _constituent_evidence(observed_at=first_observed)
    reverse_time = replace(first, observed_at=NOW)
    later = replace(first, observed_at=NOW + timedelta(minutes=3))
    repo.save_enriched_run(
        [_sector_definition(name="Original")],
        [first],
        _enriched_snapshot(first, run_key="cn:20260721T060200Z:manual"),
    )

    with pytest.raises(ValueError, match="observed_at.*after snapshot"):
        repo.save_enriched_run(
            [_sector_definition(name="Rejected")],
            [reverse_time],
            _enriched_snapshot(
                reverse_time,
                run_key="cn:20260721T060000Z:manual",
            ),
        )

    assert repo.get_run_by_key("cn:20260721T060000Z:manual") is None
    assert repo.list_universe(NOW.date()) == [
        _sector_definition(name="Original")
    ]

    later_snapshot = _enriched_snapshot(
        later,
        run_key="cn:20260721T060300Z:manual",
    )
    later_id = repo.save_enriched_run(
        [_sector_definition(name="Later")],
        [later],
        later_snapshot,
    )

    assert repo.get_run(later_id) == later_snapshot
    assert repo.get_constituent_evidence(
        first.market,
        first.sector_id,
        first.data_date,
        first.source,
    ) == first


def test_existing_run_key_validates_effective_first_observation(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    first = _constituent_evidence(observed_at=NOW + timedelta(minutes=2))
    reverse_time = replace(first, observed_at=NOW)
    repo.save_enriched_run(
        [_sector_definition()],
        [first],
        _enriched_snapshot(first, run_key="cn:20260721T060200Z:manual"),
    )
    existing_snapshot = _enriched_snapshot(
        reverse_time,
        run_key="cn:20260721T060000Z:manual",
    )
    existing_id = repo.save_run(existing_snapshot)

    with pytest.raises(ValueError, match="observed_at.*after snapshot"):
        repo.save_enriched_run(
            [_sector_definition()],
            [reverse_time],
            existing_snapshot,
        )

    assert repo.get_run(existing_id) == existing_snapshot


def test_integrity_retry_validates_effective_winner_observed_at(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    caller = _constituent_evidence(observed_at=NOW)
    winner = replace(caller, observed_at=NOW + timedelta(minutes=1))
    snapshot = _enriched_snapshot(caller)
    original_transaction = isolated_db._run_write_transaction
    attempts = 0

    def fail_once(operation_name, operation):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            original_transaction(
                "concurrent-later-constituent-winner",
                lambda session: repo._save_constituent_evidence_in_session(
                    session, [winner]
                ),
            )
            raise IntegrityError("INSERT", {}, RuntimeError("concurrent insert"))
        return original_transaction(operation_name, operation)

    monkeypatch.setattr(isolated_db, "_run_write_transaction", fail_once)

    with pytest.raises(ValueError, match="observed_at.*after snapshot"):
        repo.save_enriched_run(
            [_sector_definition()],
            [caller],
            snapshot,
        )

    assert attempts == 2
    assert repo.get_run_by_key(snapshot.run_key) is None
    assert repo.list_universe(NOW.date()) == []


def test_existing_run_key_rejects_unpersisted_retry_evidence(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    original = _constituent_evidence()
    changed = _constituent_evidence(
        data_date=original.data_date + timedelta(days=1),
        observed_at=original.observed_at + timedelta(days=1),
        codes=("000001", "600519"),
    )
    original_snapshot = _enriched_snapshot(original)
    changed_snapshot = _enriched_snapshot(changed)
    original_id = repo.save_enriched_run(
        [_sector_definition(name="Original")],
        [original],
        original_snapshot,
    )

    with pytest.raises(
        ValueError,
        match="missing or mismatched effective constituent evidence",
    ):
        repo.save_enriched_run(
            [_sector_definition(name="Changed")],
            [changed],
            changed_snapshot,
        )

    assert repo.get_run(original_id) == original_snapshot
    assert repo.get_constituent_set(changed.set_key) is None
    assert repo.list_universe(NOW.date()) == [
        _sector_definition(name="Original")
    ]


def test_existing_run_key_still_surfaces_membership_identity_conflict(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    original = _constituent_evidence()
    conflicting = _constituent_evidence(codes=("000001", "600519"))
    repo.save_enriched_run(
        [_sector_definition()],
        [original],
        _enriched_snapshot(original),
    )

    with pytest.raises(ValueError, match="conflicting constituent membership"):
        repo.save_enriched_run(
            [_sector_definition()],
            [conflicting],
            _enriched_snapshot(conflicting),
        )


def test_conflicting_membership_rolls_back_universe_evidence_and_run(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    original = _constituent_evidence()
    conflicting = _constituent_evidence(codes=("000001", "600519"))
    original_snapshot = _enriched_snapshot(original)
    rejected_snapshot = _enriched_snapshot(
        conflicting,
        run_key="cn:20260721T070000Z:manual",
    )
    repo.save_enriched_run(
        [_sector_definition(name="Original")],
        [original],
        original_snapshot,
    )

    with pytest.raises(ValueError, match="conflicting constituent membership"):
        repo.save_enriched_run(
            [_sector_definition(name="Changed")],
            [conflicting],
            rejected_snapshot,
        )

    assert repo.get_run_by_key(rejected_snapshot.run_key) is None
    assert repo.get_constituent_set(conflicting.set_key) is None
    assert repo.list_universe(NOW.date()) == [
        _sector_definition(name="Original")
    ]
    assert repo.get_run_by_key(original_snapshot.run_key) == original_snapshot


def test_snapshot_insert_failure_rolls_back_whole_enriched_write(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    rejected_sector_id = "industry:reject-enriched-insert"
    evidence = _constituent_evidence(sector_id=rejected_sector_id)
    snapshot = _enriched_snapshot(evidence)
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_enriched_sector_insert
            BEFORE INSERT ON radar_sector_snapshots
            WHEN NEW.sector_id = 'industry:reject-enriched-insert'
            BEGIN
                SELECT RAISE(ABORT, 'rejected enriched sector');
            END
            """
        )

    with pytest.raises(IntegrityError, match="rejected enriched sector"):
        repo.save_enriched_run(
            [_sector_definition(sector_id=rejected_sector_id)],
            [evidence],
            snapshot,
        )

    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarUniverseRecord.id))) == 0
        assert session.scalar(
            select(func.count(RadarConstituentSetRecord.set_key))
        ) == 0
        assert session.scalar(
            select(func.count(RadarConstituentObservationRecord.id))
        ) == 0
        assert session.scalar(select(func.count(RadarRunRecord.id))) == 0
        assert session.scalar(
            select(func.count(RadarSectorSnapshotRecord.id))
        ) == 0


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        (
            replace(_constituent_evidence(), set_key="sha256:" + "0" * 64),
            "set_key",
        ),
        (
            replace(
                _constituent_evidence(),
                codes=("600519", "000001", "300750"),
            ),
            "sorted canonical",
        ),
        (
            replace(
                _constituent_evidence(),
                codes=("SH600519", "600519"),
            ),
            "canonical",
        ),
        (
            replace(
                _constituent_evidence(),
                observed_at=NOW.replace(tzinfo=None),
            ),
            "timezone-aware",
        ),
    ],
)
def test_enriched_run_revalidates_evidence_before_any_write(
    isolated_db,
    invalid,
    message,
) -> None:
    repo = MarketRadarRepository(isolated_db)

    with pytest.raises(ValueError, match=message):
        repo.save_enriched_run(
            [_sector_definition()],
            [invalid],
            _enriched_snapshot(invalid),
        )

    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarUniverseRecord.id))) == 0
        assert session.scalar(select(func.count(RadarRunRecord.id))) == 0


def test_enriched_run_rejects_evidence_observed_after_snapshot(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence(observed_at=NOW + timedelta(minutes=1))
    snapshot = _snapshot(
        as_of=NOW,
        sectors=(
            _score(
                observed_at=evidence.observed_at,
                constituent_set_key=evidence.set_key,
                evidence_date=evidence.data_date,
                membership_source=evidence.source,
            ),
        ),
    )

    with pytest.raises(ValueError, match="observed_at.*after snapshot"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            snapshot,
        )

    with isolated_db.get_session() as session:
        assert session.scalar(select(func.count(RadarRunRecord.id))) == 0


def test_enriched_run_rejects_evidence_observed_after_sector_observation(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence(observed_at=NOW + timedelta(minutes=1))
    snapshot = _snapshot(
        as_of=NOW + timedelta(minutes=2),
        sectors=(
            _score(
                observed_at=NOW,
                constituent_set_key=evidence.set_key,
                evidence_date=evidence.data_date,
                membership_source=evidence.source,
            ),
        ),
    )

    with pytest.raises(ValueError, match="observed_at.*after sector observation"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            snapshot,
        )


def test_enriched_run_rejects_evidence_date_after_shanghai_snapshot_date(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    future_date = NOW.astimezone(ZoneInfo("Asia/Shanghai")).date() + timedelta(
        days=1
    )
    evidence = _constituent_evidence(data_date=future_date)
    snapshot = _snapshot(
        as_of=NOW,
        sectors=(
            _score(
                constituent_set_key=evidence.set_key,
                evidence_date=evidence.data_date,
                membership_source=evidence.source,
            ),
        ),
    )

    with pytest.raises(ValueError, match="data_date.*after snapshot"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            snapshot,
        )


def test_enriched_run_rejects_missing_and_orphan_evidence(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()

    with pytest.raises(ValueError, match="does not resolve"):
        repo.save_enriched_run(
            [_sector_definition()],
            [],
            _enriched_snapshot(evidence),
        )
    with pytest.raises(ValueError, match="orphan constituent evidence"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            _snapshot(),
        )


def test_enriched_run_rejects_keyed_evidence_without_membership_source(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    score_data = _enriched_snapshot(evidence).sectors[0].model_dump(mode="json")
    del score_data["observation"]["raw_reference"]["capabilities"]["membership"][
        "source"
    ]
    snapshot = _snapshot(sectors=(SectorScore.model_validate(score_data),))

    with pytest.raises(ValueError, match="source is required"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            snapshot,
        )


@pytest.mark.parametrize("mismatch", ["sector", "source"])
def test_enriched_run_rejects_cross_identity_evidence(
    isolated_db,
    mismatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence(
        sector_id=(
            "industry:other" if mismatch == "sector" else "industry:semiconductor"
        )
    )
    snapshot = _enriched_snapshot(evidence)
    if mismatch == "sector":
        snapshot = _snapshot(
            sectors=(
                _score(
                    constituent_set_key=evidence.set_key,
                    evidence_date=evidence.data_date,
                    membership_source=evidence.source,
                ),
            )
        )
    else:
        snapshot = _snapshot(
            sectors=(
                _score(
                    constituent_set_key=evidence.set_key,
                    evidence_date=evidence.data_date,
                    membership_source="different-source",
                ),
            )
        )

    with pytest.raises(ValueError, match=f"{mismatch}.*mismatch"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            snapshot,
        )


def test_existing_set_key_with_different_content_is_a_domain_error(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    with isolated_db.session_scope() as session:
        session.add(
            RadarConstituentSetRecord(
                set_key=evidence.set_key,
                market=evidence.market,
                sector_id=evidence.sector_id,
                source=evidence.source,
                codes_json='["000002"]',
                constituent_count=1,
            )
        )

    with pytest.raises(ValueError, match="immutable constituent set mismatch"):
        repo.save_enriched_run(
            [_sector_definition()],
            [evidence],
            _enriched_snapshot(evidence),
        )


def test_integrity_retry_recovers_identical_concurrent_evidence(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    snapshot = _enriched_snapshot(evidence)
    original_transaction = isolated_db._run_write_transaction
    attempts = 0

    def fail_once(operation_name, operation):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            original_transaction(
                "concurrent-constituent-winner",
                lambda session: repo._save_constituent_evidence_in_session(
                    session, [evidence]
                ),
            )
            raise IntegrityError("INSERT", {}, RuntimeError("concurrent insert"))
        return original_transaction(operation_name, operation)

    monkeypatch.setattr(isolated_db, "_run_write_transaction", fail_once)

    run_id = repo.save_enriched_run(
        [_sector_definition()], [evidence], snapshot
    )

    assert attempts == 2
    assert repo.get_run(run_id) == snapshot


def test_get_latest_run_before_uses_aware_utc_strict_upper_bound(
    isolated_db,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    earlier_as_of = NOW - timedelta(hours=1)
    later_as_of = NOW + timedelta(hours=1)
    earlier = _snapshot(
        run_key="cn:20260721T050000Z:manual",
        as_of=earlier_as_of,
        sectors=(_score(observed_at=earlier_as_of),),
    )
    current = _snapshot()
    later = _snapshot(
        run_key="cn:20260721T070000Z:manual",
        as_of=later_as_of,
        sectors=(_score(observed_at=later_as_of),),
    )
    for snapshot in (earlier, current, later):
        repo.save_run(snapshot)

    assert repo.get_latest_run("cn") == later
    assert repo.get_latest_run(
        "cn",
        before=NOW.astimezone(ZoneInfo("Asia/Shanghai")),
    ) == earlier
    assert repo.get_latest_run("cn", before=earlier_as_of) is None


@pytest.mark.parametrize(
    "before",
    [datetime(2026, 7, 21, 6, 0), "2026-07-21T06:00:00Z"],
)
def test_get_latest_run_rejects_invalid_before_without_opening_database(
    isolated_db,
    monkeypatch,
    before,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    monkeypatch.setattr(
        isolated_db,
        "get_session",
        lambda: pytest.fail("invalid before must not open a database session"),
    )

    with pytest.raises(ValueError, match="before must be timezone-aware"):
        repo.get_latest_run("cn", before=before)


def test_integrity_retry_surfaces_different_concurrent_membership(
    isolated_db,
    monkeypatch,
) -> None:
    repo = MarketRadarRepository(isolated_db)
    winner = _constituent_evidence()
    conflicting = _constituent_evidence(codes=("000001", "600519"))
    original_transaction = isolated_db._run_write_transaction
    attempts = 0

    def fail_once(operation_name, operation):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            original_transaction(
                "concurrent-constituent-winner",
                lambda session: repo._save_constituent_evidence_in_session(
                    session, [winner]
                ),
            )
            raise IntegrityError("INSERT", {}, RuntimeError("concurrent insert"))
        return original_transaction(operation_name, operation)

    monkeypatch.setattr(isolated_db, "_run_write_transaction", fail_once)

    with pytest.raises(ValueError, match="conflicting constituent membership"):
        repo.save_enriched_run(
            [_sector_definition()],
            [conflicting],
            _enriched_snapshot(conflicting),
        )

    assert attempts == 2
    assert repo.get_run_by_key("cn:20260721T060000Z:manual") is None


def test_legacy_snapshot_resolves_no_evidence(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    snapshot = _snapshot()
    run_id = repo.save_run(snapshot)

    assert repo.get_run_by_key(snapshot.run_key) == snapshot
    assert repo.resolve_run_constituent_evidence(run_id) == ()
    assert repo.resolve_snapshot_constituent_evidence(snapshot) == ()


def test_resolving_a_run_rejects_a_missing_referenced_set(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    run_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        _enriched_snapshot(evidence),
    )
    connection = isolated_db._engine.raw_connection()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM radar_constituent_sets WHERE set_key = ?",
            (evidence.set_key,),
        )
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.close()

    with pytest.raises(ValueError, match="missing referenced constituent set"):
        repo.resolve_run_constituent_evidence(run_id)


def test_resolving_a_run_rejects_corrupted_future_evidence(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    evidence = _constituent_evidence()
    run_id = repo.save_enriched_run(
        [_sector_definition()],
        [evidence],
        _enriched_snapshot(evidence),
    )
    with isolated_db.session_scope() as session:
        row = session.execute(
            select(RadarConstituentObservationRecord).where(
                RadarConstituentObservationRecord.set_key == evidence.set_key
            )
        ).scalar_one()
        row.observed_at = (evidence.observed_at + timedelta(minutes=1)).replace(
            tzinfo=None
        )

    with pytest.raises(ValueError, match="observed_at.*after snapshot"):
        repo.resolve_run_constituent_evidence(run_id)
