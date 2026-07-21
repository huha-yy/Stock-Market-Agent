from __future__ import annotations

import os
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
        missing_fields=["capital_flow_5d"],
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
    names = set(inspect(isolated_db._engine).get_table_names())

    assert {"radar_universe", "radar_runs", "radar_sector_snapshots"} <= names


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
