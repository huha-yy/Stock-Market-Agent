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
from sqlalchemy import delete, event, func, inspect, select
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)
from src.market_radar.observation_builder import (
    ConstituentEvidence,
    canonical_constituent_set_key,
)
from src.market_radar.repository import ConstituentSetContent, MarketRadarRepository
from src.storage import (
    DatabaseManager,
    RadarConstituentObservationRecord,
    RadarConstituentSetRecord,
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


def test_tables_are_created(isolated_db) -> None:
    inspector = inspect(isolated_db._engine)
    names = set(inspector.get_table_names())

    assert {
        "radar_universe",
        "radar_constituent_sets",
        "radar_constituent_observations",
        "radar_runs",
        "radar_sector_snapshots",
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
    with isolated_db.session_scope() as session:
        session.execute(
            delete(RadarConstituentSetRecord).where(
                RadarConstituentSetRecord.set_key == evidence.set_key
            )
        )

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
