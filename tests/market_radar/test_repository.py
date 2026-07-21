from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)
from src.market_radar.repository import MarketRadarRepository
from src.storage import (
    DatabaseManager,
    RadarRunRecord,
    RadarSectorSnapshotRecord,
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
) -> SectorScore:
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
        raw_reference={"provider": {"name": "fixture"}},
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


def test_tables_are_created(isolated_db) -> None:
    inspector = inspect(isolated_db._engine)
    names = set(inspector.get_table_names())

    assert {"radar_universe", "radar_runs", "radar_sector_snapshots"} <= names
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
