from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.exc import IntegrityError

from data_provider.base import normalize_stock_code
from src.market_radar.models import (
    EtfDefinition,
    EtfObservation,
    EtfSelection,
    MarketRegimeAssessment,
    PositionPlan,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)
from src.market_radar.lifecycle import (
    LifecycleContext,
    LifecycleEvaluation,
    LifecycleSignal,
    LifecycleTransition,
)
from src.market_radar.observation_builder import (
    ConstituentEvidence,
    canonical_constituent_set_key,
)
from src.market_radar.session_policy import RadarRunDecision
from src.storage import (
    DatabaseManager,
    RadarConstituentObservationRecord,
    RadarConstituentSetRecord,
    RadarEtfObservationRecord,
    RadarEtfSelectionRecord,
    RadarLifecycleHeadRecord,
    RadarPositionPlanRecord,
    RadarRegimeAssessmentRecord,
    RadarRunAttemptRecord,
    RadarRunRecord,
    RadarSectorSnapshotRecord,
    RadarSignalInstanceRecord,
    RadarSignalTransitionRecord,
    RadarUniverseRecord,
    to_utc_naive_datetime,
    utc_naive_now,
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


_SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ConstituentSetContent:
    set_key: str
    market: str
    sector_id: str
    source: str
    codes: tuple[str, ...]
    constituent_count: int
    created_at: datetime


@dataclass(frozen=True)
class AttemptReservation:
    attempt_key: str
    acquired: bool
    status: str
    run_id: int | None = None
    owner_token: str | None = None
    reason_code: str | None = None
    failure_category: str | None = None


class MarketRadarRepository:
    def __init__(self, db: DatabaseManager | None = None) -> None:
        self.db = db or DatabaseManager.get_instance()

    def _run_idempotent_write(self, operation_name: str, operation: Any) -> Any:
        try:
            return self.db._run_write_transaction(operation_name, operation)
        except IntegrityError:
            return self.db._run_write_transaction(
                f"{operation_name}[integrity-retry]",
                operation,
            )

    def _load_attempt_for_update(
        self,
        session: Any,
        attempt_key: str,
    ) -> RadarRunAttemptRecord | None:
        if self.db._is_sqlite_engine:
            return session.get(RadarRunAttemptRecord, attempt_key)
        return session.execute(
            select(RadarRunAttemptRecord)
            .where(RadarRunAttemptRecord.attempt_key == attempt_key)
            .with_for_update()
        ).scalar_one_or_none()

    def reserve_scheduled_attempt(
        self,
        decision: RadarRunDecision,
        *,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> AttemptReservation:
        if not decision.attempt_key:
            raise ValueError("scheduled decision requires an attempt_key")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        lease_expires = current + timedelta(seconds=lease_seconds)
        current_naive = to_utc_naive_datetime(current)
        owner_token = uuid.uuid4().hex

        def write(session: Any) -> AttemptReservation:
            row = self._load_attempt_for_update(session, decision.attempt_key)
            if row is None:
                row = RadarRunAttemptRecord(
                    attempt_key=decision.attempt_key,
                    market=decision.market,
                    trigger_type=decision.kind,
                    trading_date=decision.trading_date,
                    decided_at=to_utc_naive_datetime(decision.decided_at),
                    lease_expires_at=to_utc_naive_datetime(lease_expires),
                    owner_token=owner_token,
                    status="started",
                )
                session.add(row)
                session.flush()
                return AttemptReservation(
                    row.attempt_key,
                    True,
                    "started",
                    None,
                    owner_token,
                )

            expected_identity = (
                decision.market,
                decision.kind,
                decision.trading_date,
            )
            stored_identity = (
                row.market,
                row.trigger_type,
                row.trading_date,
            )
            if stored_identity != expected_identity:
                raise ValueError(
                    "scheduled attempt identity conflict for "
                    f"{decision.attempt_key}"
                )

            expired = (
                row.status == "started"
                and row.lease_expires_at is not None
                and row.lease_expires_at <= current_naive
            )
            if expired:
                row.decided_at = to_utc_naive_datetime(decision.decided_at)
                row.lease_expires_at = to_utc_naive_datetime(lease_expires)
                row.owner_token = owner_token
                row.updated_at = current_naive
                return AttemptReservation(
                    row.attempt_key,
                    True,
                    "started",
                    row.run_id,
                    owner_token,
                )
            return AttemptReservation(
                row.attempt_key,
                False,
                row.status,
                row.run_id,
                None,
                row.reason_code,
                row.failure_category,
            )

        return self._run_idempotent_write(
            f"reserve_radar_attempt[{decision.attempt_key}]",
            write,
        )

    def finish_scheduled_attempt(
        self,
        attempt_key: str,
        *,
        owner_token: str,
        status: Literal["skipped", "failed"],
        reason_code: str | None = None,
        failure_category: str | None = None,
        failure_summary: str | None = None,
    ) -> None:
        if status == "succeeded":
            raise ValueError(
                "scheduled attempt success requires atomic scheduled persistence"
            )
        if status not in {"skipped", "failed"}:
            raise ValueError(f"invalid scheduled attempt terminal status: {status}")
        if not owner_token:
            raise ValueError("scheduled attempt owner_token is required")
        values = {
            "status": status,
            "run_id": None,
            "reason_code": reason_code,
            "failure_category": failure_category,
            "failure_summary": failure_summary[:512] if failure_summary else None,
        }

        def write(session: Any) -> None:
            row = self._load_attempt_for_update(session, attempt_key)
            if row is None:
                raise ValueError(f"unknown scheduled attempt: {attempt_key}")
            if row.owner_token != owner_token:
                raise ValueError(
                    f"scheduled attempt ownership lost: {attempt_key}"
                )
            actual = {key: getattr(row, key) for key in values}
            if row.status != "started":
                if actual == values:
                    return
                raise ValueError(
                    f"scheduled attempt is already terminal: {attempt_key}"
                )
            for key, value in values.items():
                setattr(row, key, value)
            row.lease_expires_at = None
            row.updated_at = utc_naive_now()

        self._run_idempotent_write(
            f"finish_radar_attempt[{attempt_key}]",
            write,
        )

    def sync_universe(self, sectors: list[SectorDefinition]) -> None:
        self._validate_universe(sectors)
        self._run_idempotent_write(
            "sync_market_radar_universe",
            lambda session: self._sync_universe_in_session(session, sectors),
        )

    @staticmethod
    def _validate_universe(sectors: list[SectorDefinition]) -> None:
        if any(sector.market != "cn" for sector in sectors):
            raise ValueError("Market Radar supports market=cn only")

    @staticmethod
    def _sync_universe_in_session(
        session: Any,
        sectors: list[SectorDefinition],
    ) -> None:
        for sector in sectors:
            row = session.execute(
                select(RadarUniverseRecord).where(
                    and_(
                        RadarUniverseRecord.sector_id == sector.sector_id,
                        RadarUniverseRecord.effective_from == sector.effective_from,
                    )
                )
            ).scalar_one_or_none()
            fields = {
                "market": sector.market,
                "kind": sector.kind,
                "name": sector.name,
                "aliases_json": _dump(sector.aliases),
                "benchmark_code": sector.benchmark_code,
                "etfs_json": _dump(
                    [item.model_dump(mode="json") for item in sector.etfs]
                ),
                "effective_to": sector.effective_to,
            }
            if row is None:
                session.add(
                    RadarUniverseRecord(
                        sector_id=sector.sector_id,
                        effective_from=sector.effective_from,
                        **fields,
                    )
                )
            else:
                for field, value in fields.items():
                    setattr(row, field, value)

    def list_universe(self, as_of: date) -> list[SectorDefinition]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(RadarUniverseRecord)
                .where(
                    and_(
                        RadarUniverseRecord.market == "cn",
                        RadarUniverseRecord.effective_from <= as_of,
                        or_(
                            RadarUniverseRecord.effective_to.is_(None),
                            RadarUniverseRecord.effective_to >= as_of,
                        ),
                    )
                )
                .order_by(
                    RadarUniverseRecord.kind,
                    RadarUniverseRecord.sector_id,
                )
            ).scalars().all()
            return [
                SectorDefinition(
                    sector_id=row.sector_id,
                    market=row.market,
                    kind=row.kind,
                    name=row.name,
                    aliases=json.loads(row.aliases_json or "[]"),
                    benchmark_code=row.benchmark_code,
                    etfs=[
                        EtfDefinition.model_validate(item)
                        for item in json.loads(row.etfs_json or "[]")
                        if date.fromisoformat(str(item["effective_from"])) <= as_of
                        and (
                            not item.get("effective_to")
                            or date.fromisoformat(str(item["effective_to"])) >= as_of
                        )
                    ],
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                )
                for row in rows
            ]

    def save_run(self, snapshot: RadarRunSnapshot) -> int:
        self._validate_snapshot_traceability(snapshot)
        return self._run_idempotent_write(
            f"save_market_radar_run[{snapshot.run_key}]",
            lambda session: self._save_run_in_session(session, snapshot),
        )

    def save_run_with_universe(
        self,
        sectors: list[SectorDefinition],
        snapshot: RadarRunSnapshot,
    ) -> int:
        self._validate_universe(sectors)
        self._validate_snapshot_traceability(snapshot)

        def write(session: Any) -> int:
            self._sync_universe_in_session(session, sectors)
            return self._save_run_in_session(session, snapshot)

        return self._run_idempotent_write(
            f"save_market_radar_run_with_universe[{snapshot.run_key}]",
            write,
        )

    def save_enriched_run(
        self,
        sectors: list[SectorDefinition],
        evidence: Sequence[ConstituentEvidence],
        etf_observations: Sequence[EtfObservation] | RadarRunSnapshot = (),
        snapshot: RadarRunSnapshot | None = None,
    ) -> int:
        if isinstance(etf_observations, RadarRunSnapshot):
            if snapshot is not None:
                raise TypeError("snapshot was provided twice")
            snapshot = etf_observations
            validated_etf_observations: tuple[EtfObservation, ...] = ()
        else:
            validated_etf_observations = tuple(etf_observations)
        if snapshot is None:
            raise TypeError("snapshot is required")
        self._validate_universe(sectors)
        self._validate_snapshot_traceability(snapshot)
        self._validate_etf_observations(snapshot, validated_etf_observations)
        validated_evidence = self._validate_enriched_traceability(
            snapshot,
            evidence,
        )

        def write(session: Any) -> int:
            existing_id = session.execute(
                select(RadarRunRecord.id).where(
                    RadarRunRecord.run_key == snapshot.run_key
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                self._assert_constituent_evidence_compatible_in_session(
                    session,
                    validated_evidence,
                )
                self._validate_effective_constituent_evidence_in_session(
                    session,
                    validated_evidence,
                    snapshot,
                )
                self._assert_run_semantically_equal_in_session(
                    session,
                    int(existing_id),
                    snapshot,
                    validated_etf_observations,
                )
                return int(existing_id)
            self._sync_universe_in_session(session, sectors)
            self._save_constituent_evidence_in_session(
                session,
                validated_evidence,
            )
            self._validate_effective_constituent_evidence_in_session(
                session,
                validated_evidence,
                snapshot,
            )
            return self._save_run_in_session(
                session,
                snapshot,
                validated_etf_observations,
            )

        return self._run_idempotent_write(
            f"save_market_radar_enriched_run[{snapshot.run_key}]",
            write,
        )

    def load_lifecycle_context(self) -> LifecycleContext:
        with self.db.get_session() as session:
            head = session.get(RadarLifecycleHeadRecord, "cn")
            head_run_key: str | None = None
            head_effective_at: datetime | None = None
            if head is not None:
                head_run = session.get(RadarRunRecord, head.last_run_id)
                if head_run is None or head_run.market != "cn":
                    raise ValueError("Market Radar lifecycle head is corrupt")
                head_run_key = head_run.run_key
                head_effective_at = _aware(head.last_effective_at)
            rows = session.execute(
                select(RadarSignalInstanceRecord).order_by(
                    RadarSignalInstanceRecord.sector_id,
                    RadarSignalInstanceRecord.instance_number,
                )
            ).scalars().all()
            latest: dict[str, int] = {}
            open_signals: list[LifecycleSignal] = []
            for row in rows:
                latest[row.sector_id] = max(
                    latest.get(row.sector_id, 0),
                    row.instance_number,
                )
                if row.closed_at is None:
                    open_signals.append(
                        LifecycleSignal.model_validate_json(row.signal_json)
                    )
            return LifecycleContext(
                open_signals=tuple(open_signals),
                latest_instance_by_sector=latest,
                head_run_key=head_run_key,
                head_effective_at=head_effective_at,
            )

    def save_scheduled_enriched_run(
        self,
        sectors: list[SectorDefinition],
        evidence: Sequence[ConstituentEvidence],
        etf_observations: Sequence[EtfObservation],
        snapshot: RadarRunSnapshot,
        evaluation: LifecycleEvaluation,
        *,
        attempt_key: str,
        attempt_owner_token: str,
    ) -> int:
        if snapshot.trigger != "schedule" or evaluation.run_key != snapshot.run_key:
            raise ValueError("scheduled snapshot and lifecycle evaluation must match")
        self._validate_universe(sectors)
        self._validate_snapshot_traceability(snapshot)
        validated_observations = tuple(etf_observations)
        self._validate_etf_observations(snapshot, validated_observations)
        validated_evidence = self._validate_enriched_traceability(
            snapshot,
            evidence,
        )

        def write(session: Any) -> int:
            attempt = self._load_attempt_for_update(session, attempt_key)
            if attempt is None:
                raise ValueError(f"unknown scheduled attempt: {attempt_key}")
            if attempt.owner_token != attempt_owner_token:
                raise ValueError(
                    f"scheduled attempt ownership lost: {attempt_key}"
                )
            if attempt.status not in {"started", "succeeded"}:
                raise ValueError(
                    f"scheduled attempt is already terminal: {attempt_key}"
                )
            existing_run = session.execute(
                select(RadarRunRecord).where(
                    RadarRunRecord.run_key == snapshot.run_key
                )
            ).scalar_one_or_none()
            should_advance_head = (
                existing_run is None
                or existing_run.lifecycle_evaluation_json is None
            )
            lifecycle_head = None
            if should_advance_head:
                lifecycle_head = self._validate_lifecycle_head_in_session(
                    session,
                    snapshot,
                    evaluation,
                )
            if existing_run is not None:
                existing_id = int(existing_run.id)
                self._assert_constituent_evidence_compatible_in_session(
                    session,
                    validated_evidence,
                )
                self._validate_effective_constituent_evidence_in_session(
                    session,
                    validated_evidence,
                    snapshot,
                )
                self._assert_run_semantically_equal_in_session(
                    session,
                    existing_id,
                    snapshot,
                    validated_observations,
                )
                self._save_lifecycle_in_session(
                    session,
                    existing_id,
                    evaluation,
                )
                if should_advance_head:
                    self._advance_lifecycle_head_in_session(
                        session,
                        lifecycle_head,
                        snapshot,
                        existing_id,
                    )
                self._succeed_scheduled_attempt_in_session(
                    attempt,
                    existing_id,
                )
                return existing_id

            if attempt.status != "started":
                raise ValueError(
                    f"scheduled attempt run binding conflict: {attempt_key}"
                )

            self._sync_universe_in_session(session, sectors)
            self._save_constituent_evidence_in_session(
                session,
                validated_evidence,
            )
            self._validate_effective_constituent_evidence_in_session(
                session,
                validated_evidence,
                snapshot,
            )
            run_id = self._save_run_in_session(
                session,
                snapshot,
                validated_observations,
            )
            self._save_lifecycle_in_session(session, run_id, evaluation)
            self._advance_lifecycle_head_in_session(
                session,
                lifecycle_head,
                snapshot,
                run_id,
            )
            self._succeed_scheduled_attempt_in_session(attempt, run_id)
            return run_id

        return self._run_idempotent_write(
            f"save_scheduled_market_radar_run[{snapshot.run_key}]",
            write,
        )

    @staticmethod
    def _succeed_scheduled_attempt_in_session(
        attempt: RadarRunAttemptRecord,
        run_id: int,
    ) -> None:
        if attempt.status == "succeeded":
            if attempt.run_id == run_id:
                return
            raise ValueError(
                f"scheduled attempt run binding conflict: {attempt.attempt_key}"
            )
        if attempt.status != "started":
            raise ValueError(
                f"scheduled attempt is already terminal: {attempt.attempt_key}"
            )
        attempt.status = "succeeded"
        attempt.run_id = run_id
        attempt.reason_code = None
        attempt.failure_category = None
        attempt.failure_summary = None
        attempt.lease_expires_at = None
        attempt.updated_at = utc_naive_now()

    def _load_lifecycle_head_for_update(
        self,
        session: Any,
        market: str,
    ) -> RadarLifecycleHeadRecord | None:
        if self.db._is_sqlite_engine:
            return session.get(RadarLifecycleHeadRecord, market)
        return session.execute(
            select(RadarLifecycleHeadRecord)
            .where(RadarLifecycleHeadRecord.market == market)
            .with_for_update()
        ).scalar_one_or_none()

    def _validate_lifecycle_head_in_session(
        self,
        session: Any,
        snapshot: RadarRunSnapshot,
        evaluation: LifecycleEvaluation,
    ) -> RadarLifecycleHeadRecord | None:
        head = self._load_lifecycle_head_for_update(session, snapshot.market)
        expected = (
            evaluation.expected_head_run_key,
            (
                _aware(evaluation.expected_head_effective_at)
                if evaluation.expected_head_effective_at is not None
                else None
            ),
        )
        if head is None:
            if expected != (None, None):
                raise ValueError("Market Radar lifecycle head predecessor conflict")
            return None

        if head.lifecycle_version != "cn-lifecycle-v1":
            raise ValueError("Market Radar lifecycle head version conflict")
        head_run = session.get(RadarRunRecord, head.last_run_id)
        if head_run is None or head_run.market != snapshot.market:
            raise ValueError("Market Radar lifecycle head is corrupt")
        actual_effective_at = _aware(head.last_effective_at)
        actual = (head_run.run_key, actual_effective_at)
        if expected != actual:
            raise ValueError("Market Radar lifecycle head predecessor conflict")
        if snapshot.as_of.astimezone(timezone.utc) <= actual_effective_at:
            raise ValueError(
                "scheduled lifecycle run must be strictly newer than its head"
            )
        return head

    @staticmethod
    def _advance_lifecycle_head_in_session(
        session: Any,
        head: RadarLifecycleHeadRecord | None,
        snapshot: RadarRunSnapshot,
        run_id: int,
    ) -> None:
        effective_at = to_utc_naive_datetime(snapshot.as_of)
        if head is None:
            session.add(
                RadarLifecycleHeadRecord(
                    market=snapshot.market,
                    last_run_id=run_id,
                    last_effective_at=effective_at,
                    lifecycle_version="cn-lifecycle-v1",
                )
            )
            return
        head.last_run_id = run_id
        head.last_effective_at = effective_at
        head.updated_at = utc_naive_now()

    def _save_lifecycle_in_session(
        self,
        session: Any,
        run_id: int,
        evaluation: LifecycleEvaluation,
    ) -> None:
        signal_keys = [signal.signal_key for signal in evaluation.signals]
        transition_keys = [
            transition.transition_key for transition in evaluation.transitions
        ]
        if len(signal_keys) != len(set(signal_keys)):
            raise ValueError("duplicate lifecycle signal key")
        if len(transition_keys) != len(set(transition_keys)):
            raise ValueError("duplicate lifecycle transition key")

        current_run = session.get(RadarRunRecord, run_id)
        if current_run is None or current_run.run_key != evaluation.run_key:
            raise ValueError("lifecycle evaluation run does not match persisted run")

        evaluation_json = _dump(evaluation.model_dump(mode="json"))
        if current_run.lifecycle_evaluation_json is not None:
            stored_evaluation = LifecycleEvaluation.model_validate_json(
                current_run.lifecycle_evaluation_json
            )
            if stored_evaluation != evaluation:
                raise ValueError(
                    f"lifecycle evaluation semantic conflict for {evaluation.run_key}"
                )
            return

        locked_signal_rows: dict[str, RadarSignalInstanceRecord] = {}
        if signal_keys and not self.db._is_sqlite_engine:
            locked_signal_rows = {
                row.signal_key: row
                for row in session.execute(
                    select(RadarSignalInstanceRecord)
                    .where(
                        RadarSignalInstanceRecord.signal_key.in_(
                            sorted(signal_keys)
                        )
                    )
                    .order_by(RadarSignalInstanceRecord.signal_key)
                    .with_for_update()
                ).scalars()
            }

        stored_signal_keys = set(
            session.execute(
                select(RadarSignalInstanceRecord.signal_key).where(
                    RadarSignalInstanceRecord.last_run_id == run_id
                )
            ).scalars()
        )
        stored_transition_keys = set(
            session.execute(
                select(RadarSignalTransitionRecord.transition_key).where(
                    RadarSignalTransitionRecord.effective_run_id == run_id
                )
            ).scalars()
        )
        if stored_signal_keys or stored_transition_keys:
            if (
                stored_signal_keys != set(signal_keys)
                or stored_transition_keys != set(transition_keys)
            ):
                raise ValueError(
                    f"lifecycle evaluation semantic conflict for {evaluation.run_key}"
                )

        required_run_keys = {evaluation.run_key}
        for signal in evaluation.signals:
            if signal.current_run_key != evaluation.run_key:
                raise ValueError("lifecycle signal run does not match evaluation")
            required_run_keys.add(signal.first_run_key)
            required_run_keys.add(signal.current_run_key)
            if signal.previous_run_key is not None:
                required_run_keys.add(signal.previous_run_key)
        for transition in evaluation.transitions:
            if transition.effective_run_key != evaluation.run_key:
                raise ValueError("lifecycle transition run does not match evaluation")
            required_run_keys.add(transition.effective_run_key)

        run_ids = {
            run_key: int(stored_run_id)
            for run_key, stored_run_id in session.execute(
                select(RadarRunRecord.run_key, RadarRunRecord.id).where(
                    RadarRunRecord.run_key.in_(required_run_keys)
                )
            ).all()
        }
        missing_run_keys = required_run_keys - set(run_ids)
        if missing_run_keys:
            missing = ", ".join(sorted(missing_run_keys))
            raise ValueError(f"lifecycle run reference is not persisted: {missing}")

        for signal in evaluation.signals:
            first_run_id = run_ids[signal.first_run_key]
            last_run_id = run_ids[signal.current_run_key]
            signal_json = _dump(signal.model_dump(mode="json"))
            row = (
                session.get(RadarSignalInstanceRecord, signal.signal_key)
                if self.db._is_sqlite_engine
                else locked_signal_rows.get(signal.signal_key)
            )
            identity = (
                signal.market,
                signal.sector_id,
                signal.instance_number,
                signal.lifecycle_version,
                first_run_id,
            )
            if row is None:
                row = RadarSignalInstanceRecord(
                    signal_key=signal.signal_key,
                    market=signal.market,
                    sector_id=signal.sector_id,
                    instance_number=signal.instance_number,
                    state=signal.state,
                    lifecycle_version=signal.lifecycle_version,
                    first_run_id=first_run_id,
                    last_run_id=last_run_id,
                    signal_json=signal_json,
                    closed_at=(
                        to_utc_naive_datetime(signal.closed_at)
                        if signal.closed_at is not None
                        else None
                    ),
                )
                session.add(row)
                session.flush()
                continue

            stored_identity = (
                row.market,
                row.sector_id,
                row.instance_number,
                row.lifecycle_version,
                row.first_run_id,
            )
            if stored_identity != identity:
                raise ValueError(
                    f"lifecycle signal semantic conflict for {signal.signal_key}"
                )
            stored_signal = LifecycleSignal.model_validate_json(row.signal_json)
            if stored_signal.current_run_key == signal.current_run_key:
                expected_closed_at = (
                    to_utc_naive_datetime(signal.closed_at)
                    if signal.closed_at is not None
                    else None
                )
                if (
                    stored_signal != signal
                    or row.state != signal.state
                    or row.last_run_id != last_run_id
                    or row.closed_at != expected_closed_at
                ):
                    raise ValueError(
                        f"lifecycle signal semantic conflict for {signal.signal_key}"
                    )
                continue
            if (
                row.closed_at is not None
                or signal.previous_run_key != stored_signal.current_run_key
            ):
                raise ValueError(
                    f"lifecycle signal semantic conflict for {signal.signal_key}"
                )
            row.state = signal.state
            row.last_run_id = last_run_id
            row.signal_json = signal_json
            row.closed_at = (
                to_utc_naive_datetime(signal.closed_at)
                if signal.closed_at is not None
                else None
            )
            row.updated_at = utc_naive_now()

        session.flush()

        evaluation_signal_keys = set(signal_keys)
        for transition in evaluation.transitions:
            if transition.signal_key not in evaluation_signal_keys:
                raise ValueError(
                    "lifecycle transition must reference an evaluated signal"
                )
            transition_json = _dump(transition.model_dump(mode="json"))
            effective_run_id = run_ids[transition.effective_run_key]
            row = session.get(
                RadarSignalTransitionRecord,
                transition.transition_key,
            )
            if row is None:
                session.add(
                    RadarSignalTransitionRecord(
                        transition_key=transition.transition_key,
                        signal_key=transition.signal_key,
                        effective_run_id=effective_run_id,
                        previous_state=transition.previous_state,
                        new_state=transition.new_state,
                        transition_json=transition_json,
                    )
                )
                continue
            stored_transition = LifecycleTransition.model_validate_json(
                row.transition_json
            )
            if (
                stored_transition != transition
                or row.signal_key != transition.signal_key
                or row.effective_run_id != effective_run_id
                or row.previous_state != transition.previous_state
                or row.new_state != transition.new_state
            ):
                raise ValueError(
                    "lifecycle transition semantic conflict for "
                    f"{transition.transition_key}"
                )
        current_run.lifecycle_evaluation_json = evaluation_json

    @classmethod
    def _validate_enriched_traceability(
        cls,
        snapshot: RadarRunSnapshot,
        evidence: Sequence[ConstituentEvidence],
    ) -> tuple[ConstituentEvidence, ...]:
        validated = tuple(evidence)
        by_key: dict[str, ConstituentEvidence] = {}
        identities: set[tuple[str, str, date, str]] = set()
        for item in validated:
            cls._validate_constituent_evidence(item)
            if item.set_key in by_key:
                raise ValueError(f"duplicate constituent evidence key: {item.set_key}")
            identity = (item.market, item.sector_id, item.data_date, item.source)
            if identity in identities:
                raise ValueError("duplicate constituent evidence identity")
            by_key[item.set_key] = item
            identities.add(identity)

        referenced: set[str] = set()
        for sector in snapshot.sectors:
            observation = SectorObservation.model_validate(sector.observation)
            raw_reference = observation.raw_reference
            set_key = raw_reference.get("constituent_set_key")
            if set_key is None:
                continue
            if not isinstance(set_key, str) or not set_key:
                raise ValueError(
                    f"invalid constituent_set_key for {sector.sector_id}"
                )
            item = by_key.get(set_key)
            if item is None:
                raise ValueError(
                    f"constituent_set_key does not resolve for {sector.sector_id}"
                )
            if item.market != snapshot.market or observation.market != snapshot.market:
                raise ValueError(
                    f"constituent evidence market mismatch for {sector.sector_id}"
                )
            if item.sector_id != sector.sector_id:
                raise ValueError(
                    f"constituent evidence sector mismatch for {sector.sector_id}"
                )
            reference_date = cls._reference_data_date(raw_reference, sector.sector_id)
            if reference_date != item.data_date:
                raise ValueError(
                    f"constituent evidence data_date mismatch for {sector.sector_id}"
                )
            reference_source = cls._reference_membership_source(
                raw_reference,
                sector.sector_id,
            )
            if reference_source != item.source:
                raise ValueError(
                    f"constituent evidence source mismatch for {sector.sector_id}"
                )
            cls._validate_point_in_time_evidence(item, snapshot, observation)
            referenced.add(set_key)

        orphaned = set(by_key) - referenced
        if orphaned:
            raise ValueError(
                "orphan constituent evidence is not referenced by the run: "
                + ", ".join(sorted(orphaned))
            )
        return validated

    @staticmethod
    def _validate_constituent_evidence(item: ConstituentEvidence) -> None:
        if not isinstance(item, ConstituentEvidence):
            raise ValueError("constituent evidence must be ConstituentEvidence")
        if type(item.data_date) is not date:
            raise ValueError("constituent evidence data_date must be a date")
        if (
            not isinstance(item.observed_at, datetime)
            or item.observed_at.tzinfo is None
            or item.observed_at.utcoffset() is None
        ):
            raise ValueError("constituent evidence observed_at must be timezone-aware")
        MarketRadarRepository._validate_constituent_set_identity(
            market=item.market,
            sector_id=item.sector_id,
            source=item.source,
            codes=item.codes,
            set_key=item.set_key,
        )

    @staticmethod
    def _validate_constituent_set_identity(
        *,
        market: str,
        sector_id: str,
        source: str,
        codes: tuple[str, ...],
        set_key: str,
    ) -> None:
        if market != "cn":
            raise ValueError("constituent evidence market must be cn")
        if not isinstance(sector_id, str) or not sector_id.strip():
            raise ValueError("constituent evidence sector_id is required")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("constituent evidence source is required")
        if not isinstance(codes, tuple) or not codes:
            raise ValueError("constituent evidence codes must be a non-empty tuple")
        if any(not isinstance(code, str) for code in codes):
            raise ValueError("constituent evidence codes must be strings")
        try:
            expected_key = canonical_constituent_set_key(
                market,
                sector_id,
                source,
                codes,
            )
            canonical_codes = tuple(
                sorted(normalize_stock_code(code) for code in codes)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"constituent evidence codes are not canonical: {exc}") from exc
        if codes != canonical_codes:
            raise ValueError(
                "constituent evidence codes must be sorted canonical codes"
            )
        if set_key != expected_key:
            raise ValueError("constituent evidence set_key does not match content")

    @staticmethod
    def _reference_data_date(
        raw_reference: Mapping[str, Any],
        sector_id: str,
    ) -> date:
        value = raw_reference.get("data_date")
        if type(value) is date:
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise ValueError(
            f"constituent evidence data_date mismatch for {sector_id}"
        )

    @staticmethod
    def _reference_membership_source(
        raw_reference: Mapping[str, Any],
        sector_id: str,
    ) -> str:
        capabilities = raw_reference.get("capabilities")
        membership = (
            capabilities.get("membership")
            if isinstance(capabilities, Mapping)
            else None
        )
        source = membership.get("source") if isinstance(membership, Mapping) else None
        if not isinstance(source, str) or not source:
            raise ValueError(
                f"constituent evidence source is required for {sector_id}"
            )
        return source

    @staticmethod
    def _validate_point_in_time_evidence(
        evidence: ConstituentEvidence,
        snapshot: RadarRunSnapshot,
        observation: SectorObservation,
    ) -> None:
        evidence_observed_at = _aware(evidence.observed_at)
        if evidence_observed_at > _aware(snapshot.as_of):
            raise ValueError(
                f"constituent evidence observed_at is after snapshot as_of for "
                f"{observation.sector_id}"
            )
        if evidence_observed_at > _aware(observation.observed_at):
            raise ValueError(
                "constituent evidence observed_at is after sector observation "
                f"for {observation.sector_id}"
            )
        snapshot_data_date = _aware(snapshot.as_of).astimezone(
            _SHANGHAI_TIMEZONE
        ).date()
        if evidence.data_date > snapshot_data_date:
            raise ValueError(
                f"constituent evidence data_date is after snapshot date for "
                f"{observation.sector_id}"
            )

    @classmethod
    def _save_constituent_evidence_in_session(
        cls,
        session: Any,
        evidence: Sequence[ConstituentEvidence],
    ) -> None:
        for item in evidence:
            codes_json = _dump(list(item.codes))
            existing_set, observation = cls._existing_constituent_rows(
                session,
                item,
            )
            cls._assert_existing_constituent_compatible(
                item,
                codes_json,
                existing_set,
                observation,
            )
            if existing_set is None:
                session.add(
                    RadarConstituentSetRecord(
                        set_key=item.set_key,
                        market=item.market,
                        sector_id=item.sector_id,
                        source=item.source,
                        codes_json=codes_json,
                        constituent_count=len(item.codes),
                    )
                )
                session.flush()
            if observation is None:
                session.add(
                    RadarConstituentObservationRecord(
                        market=item.market,
                        sector_id=item.sector_id,
                        data_date=item.data_date,
                        observed_at=to_utc_naive_datetime(item.observed_at),
                        source=item.source,
                        set_key=item.set_key,
                    )
                )
                session.flush()

    @classmethod
    def _assert_constituent_evidence_compatible_in_session(
        cls,
        session: Any,
        evidence: Sequence[ConstituentEvidence],
    ) -> None:
        for item in evidence:
            existing_set, observation = cls._existing_constituent_rows(
                session,
                item,
            )
            cls._assert_existing_constituent_compatible(
                item,
                _dump(list(item.codes)),
                existing_set,
                observation,
            )

    @classmethod
    def _validate_effective_constituent_evidence_in_session(
        cls,
        session: Any,
        evidence: Sequence[ConstituentEvidence],
        snapshot: RadarRunSnapshot,
    ) -> None:
        observations_by_key: dict[str, SectorObservation] = {}
        for sector in snapshot.sectors:
            observation = SectorObservation.model_validate(sector.observation)
            set_key = observation.raw_reference.get("constituent_set_key")
            if isinstance(set_key, str) and set_key:
                observations_by_key[set_key] = observation

        for item in evidence:
            observation = observations_by_key.get(item.set_key)
            if observation is None:
                raise ValueError(
                    f"effective constituent evidence is not referenced for "
                    f"{item.sector_id}"
                )
            set_row = session.get(RadarConstituentSetRecord, item.set_key)
            observation_row = session.execute(
                select(RadarConstituentObservationRecord).where(
                    and_(
                        RadarConstituentObservationRecord.market == item.market,
                        RadarConstituentObservationRecord.sector_id
                        == item.sector_id,
                        RadarConstituentObservationRecord.data_date
                        == item.data_date,
                        RadarConstituentObservationRecord.source == item.source,
                        RadarConstituentObservationRecord.set_key == item.set_key,
                    )
                )
            ).scalar_one_or_none()
            if set_row is None or observation_row is None:
                raise ValueError(
                    f"missing or mismatched effective constituent evidence for "
                    f"{item.sector_id}"
                )
            effective = cls._evidence_from_rows(set_row, observation_row)
            cls._validate_point_in_time_evidence(
                effective,
                snapshot,
                observation,
            )

    @staticmethod
    def _existing_constituent_rows(
        session: Any,
        item: ConstituentEvidence,
    ) -> tuple[
        RadarConstituentSetRecord | None,
        RadarConstituentObservationRecord | None,
    ]:
        existing_set = session.get(RadarConstituentSetRecord, item.set_key)
        observation = session.execute(
            select(RadarConstituentObservationRecord).where(
                and_(
                    RadarConstituentObservationRecord.market == item.market,
                    RadarConstituentObservationRecord.sector_id == item.sector_id,
                    RadarConstituentObservationRecord.data_date == item.data_date,
                    RadarConstituentObservationRecord.source == item.source,
                )
            )
        ).scalar_one_or_none()
        return existing_set, observation

    @staticmethod
    def _assert_existing_constituent_compatible(
        item: ConstituentEvidence,
        codes_json: str,
        existing_set: RadarConstituentSetRecord | None,
        observation: RadarConstituentObservationRecord | None,
    ) -> None:
        if existing_set is not None:
            actual = (
                existing_set.market,
                existing_set.sector_id,
                existing_set.source,
                existing_set.codes_json,
                existing_set.constituent_count,
            )
            expected = (
                item.market,
                item.sector_id,
                item.source,
                codes_json,
                len(item.codes),
            )
            if actual != expected:
                raise ValueError(
                    f"immutable constituent set mismatch for {item.set_key}"
                )
        if observation is not None and observation.set_key != item.set_key:
            raise ValueError(
                "conflicting constituent membership for "
                f"{item.market}/{item.sector_id}/{item.data_date}/{item.source}"
            )

    @classmethod
    def _save_run_in_session(
        cls,
        session: Any,
        snapshot: RadarRunSnapshot,
        etf_observations: Sequence[EtfObservation] = (),
    ) -> int:
        existing_id = session.execute(
            select(RadarRunRecord.id).where(
                RadarRunRecord.run_key == snapshot.run_key
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            return int(existing_id)

        snapshot_data = snapshot.model_dump(mode="json")
        run = RadarRunRecord(
            run_key=snapshot.run_key,
            market=snapshot.market,
            trigger=snapshot.trigger,
            as_of=to_utc_naive_datetime(snapshot.as_of),
            quality=snapshot.quality,
            scoring_version=snapshot.scoring_version,
            provider_trace_json=_dump(snapshot_data["provider_trace"]),
        )
        session.add(run)
        session.flush()
        for position, sector in enumerate(snapshot.sectors):
            sector_data = sector.model_dump(mode="json")
            session.add(
                RadarSectorSnapshotRecord(
                    run_id=run.id,
                    position=position,
                    sector_id=sector.sector_id,
                    name=sector.name,
                    kind=sector.kind,
                    score=sector.score,
                    gross_score=sector.gross_score,
                    risk_deduction=sector.risk_deduction,
                    confidence=sector.confidence,
                    state=sector.state,
                    scoring_version=sector.scoring_version,
                    quality=sector.quality,
                    source=sector.source,
                    observed_at=to_utc_naive_datetime(sector.observed_at),
                    factors_json=_dump(sector_data["factors"]),
                    risk_reasons_json=_dump(sector_data["risk_reasons"]),
                    missing_fields_json=_dump(sector_data["missing_fields"]),
                    observation_json=_dump(sector_data["observation"]),
                )
            )
        for position, observation in enumerate(etf_observations):
            session.add(
                RadarEtfObservationRecord(
                    run_id=run.id,
                    sector_id=observation.sector_id,
                    code=observation.code,
                    position=position,
                    observation_json=_dump(
                        observation.model_dump(mode="json")
                    ),
                )
            )
        for position, selection in enumerate(snapshot.etfs):
            session.add(
                RadarEtfSelectionRecord(
                    run_id=run.id,
                    sector_id=selection.sector_id,
                    code=selection.code,
                    position=position,
                    selection_json=_dump(selection.model_dump(mode="json")),
                )
            )
        if snapshot.regime is not None:
            session.add(
                RadarRegimeAssessmentRecord(
                    run_id=run.id,
                    assessment_json=_dump(
                        snapshot.regime.model_dump(mode="json")
                    ),
                )
            )
        if snapshot.position_plan is not None:
            session.add(
                RadarPositionPlanRecord(
                    run_id=run.id,
                    plan_json=_dump(
                        snapshot.position_plan.model_dump(mode="json")
                    ),
                )
            )
        return int(run.id)

    @classmethod
    def _assert_run_semantically_equal_in_session(
        cls,
        session: Any,
        run_id: int,
        snapshot: RadarRunSnapshot,
        etf_observations: tuple[EtfObservation, ...],
    ) -> None:
        run = session.get(RadarRunRecord, run_id)
        if run is None:
            raise ValueError(f"stored Market Radar run not found: {run_id}")
        stored_snapshot = cls._snapshot_from_run_in_session(session, run)
        stored_observations, _, _, _ = cls._phase2b_evidence_from_run_in_session(
            session,
            run_id,
        )
        if stored_snapshot != snapshot or stored_observations != etf_observations:
            raise ValueError(
                f"Market Radar run semantic conflict for run_key={snapshot.run_key}"
            )

    @staticmethod
    def _validate_snapshot_traceability(snapshot: RadarRunSnapshot) -> None:
        for sector in snapshot.sectors:
            if not sector.observation:
                raise ValueError(
                    f"SectorScore observation is required for {sector.sector_id}"
                )
            try:
                observation = SectorObservation.model_validate(sector.observation)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid SectorScore observation for {sector.sector_id}: {exc}"
                ) from exc

            comparisons = {
                "sector_id": (sector.sector_id, observation.sector_id),
                "source": (sector.source, observation.source),
                "observed_at": (
                    _aware(sector.observed_at),
                    _aware(observation.observed_at),
                ),
                "quality": (sector.quality, observation.quality),
                "missing_fields": (
                    sorted(sector.missing_fields),
                    sorted(observation.missing_fields),
                ),
            }
            for field_name, (score_value, observation_value) in comparisons.items():
                if score_value != observation_value:
                    raise ValueError(
                        f"SectorScore observation {field_name} mismatch for "
                        f"{sector.sector_id}"
                    )

    @staticmethod
    def _validate_etf_observations(
        snapshot: RadarRunSnapshot,
        observations: tuple[EtfObservation, ...],
    ) -> None:
        identities: set[tuple[str, str]] = set()
        for observation in observations:
            if not isinstance(observation, EtfObservation):
                raise ValueError("ETF observations must be EtfObservation models")
            identity = (observation.sector_id, observation.code)
            if identity in identities:
                raise ValueError("duplicate ETF observation identity")
            identities.add(identity)
            if observation.market != snapshot.market:
                raise ValueError("ETF observation market must match run market")
            if _aware(observation.observed_at) != _aware(snapshot.as_of):
                raise ValueError("ETF observation timestamp must match run as_of")

    def get_latest_run(
        self,
        market: str,
        before: datetime | None = None,
    ) -> RadarRunSnapshot | None:
        filters = [RadarRunRecord.market == market]
        if before is not None:
            if (
                not isinstance(before, datetime)
                or before.tzinfo is None
                or before.utcoffset() is None
            ):
                raise ValueError("before must be timezone-aware")
            filters.append(
                RadarRunRecord.as_of
                < to_utc_naive_datetime(before.astimezone(timezone.utc))
            )
        with self.db.get_session() as session:
            run = session.execute(
                select(RadarRunRecord)
                .where(*filters)
                .order_by(desc(RadarRunRecord.as_of), desc(RadarRunRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            return self._snapshot_from_run_in_session(session, run)

    def get_run_by_key(self, run_key: str) -> RadarRunSnapshot | None:
        with self.db.get_session() as session:
            run = session.execute(
                select(RadarRunRecord).where(RadarRunRecord.run_key == run_key)
            ).scalar_one_or_none()
            if run is None:
                return None
            return self._snapshot_from_run_in_session(session, run)

    def get_run_id_by_key(self, run_key: str) -> int:
        with self.db.get_session() as session:
            run_id = session.execute(
                select(RadarRunRecord.id).where(RadarRunRecord.run_key == run_key)
            ).scalar_one_or_none()
            if run_id is None:
                raise ValueError(f"stored Market Radar run not found: {run_key}")
            return int(run_id)

    def get_run(self, run_id: int) -> RadarRunSnapshot | None:
        with self.db.get_session() as session:
            run = session.execute(
                select(RadarRunRecord).where(RadarRunRecord.id == run_id)
            ).scalar_one_or_none()
            if run is None:
                return None
            return self._snapshot_from_run_in_session(session, run)

    def load_phase2b_evidence(
        self,
        run_id: int,
    ) -> tuple[
        tuple[EtfObservation, ...],
        tuple[EtfSelection, ...],
        MarketRegimeAssessment | None,
        PositionPlan | None,
    ]:
        with self.db.get_session() as session:
            if session.get(RadarRunRecord, run_id) is None:
                raise ValueError(f"stored Market Radar run not found: {run_id}")
            return self._phase2b_evidence_from_run_in_session(session, run_id)

    def get_constituent_set(
        self,
        set_key: str,
    ) -> ConstituentSetContent | None:
        with self.db.get_session() as session:
            set_row = session.get(RadarConstituentSetRecord, set_key)
            if set_row is None:
                return None
            return self._constituent_set_content_from_row(set_row)

    def list_constituent_evidence_for_set(
        self,
        set_key: str,
    ) -> tuple[ConstituentEvidence, ...]:
        with self.db.get_session() as session:
            set_row = session.get(RadarConstituentSetRecord, set_key)
            if set_row is None:
                return ()
            observation_rows = session.execute(
                select(RadarConstituentObservationRecord)
                .where(RadarConstituentObservationRecord.set_key == set_key)
                .order_by(
                    RadarConstituentObservationRecord.data_date,
                    RadarConstituentObservationRecord.observed_at,
                    RadarConstituentObservationRecord.id,
                )
            ).scalars().all()
            return tuple(
                self._evidence_from_rows(set_row, observation_row)
                for observation_row in observation_rows
            )

    def get_constituent_evidence(
        self,
        market: str,
        sector_id: str,
        data_date: date,
        source: str,
    ) -> ConstituentEvidence | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(
                    RadarConstituentSetRecord,
                    RadarConstituentObservationRecord,
                )
                .join(
                    RadarConstituentObservationRecord,
                    RadarConstituentObservationRecord.set_key
                    == RadarConstituentSetRecord.set_key,
                )
                .where(
                    and_(
                        RadarConstituentObservationRecord.market == market,
                        RadarConstituentObservationRecord.sector_id == sector_id,
                        RadarConstituentObservationRecord.data_date == data_date,
                        RadarConstituentObservationRecord.source == source,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return self._evidence_from_rows(row[0], row[1])

    def resolve_run_constituent_evidence(
        self,
        run_id: int,
    ) -> tuple[ConstituentEvidence, ...]:
        with self.db.get_session() as session:
            run = session.get(RadarRunRecord, run_id)
            if run is None:
                raise ValueError(f"stored Market Radar run not found: {run_id}")
            snapshot = self._snapshot_from_run_in_session(session, run)
            return self._resolve_snapshot_evidence_in_session(session, snapshot)

    def resolve_snapshot_constituent_evidence(
        self,
        snapshot: RadarRunSnapshot,
    ) -> tuple[ConstituentEvidence, ...]:
        self._validate_snapshot_traceability(snapshot)
        with self.db.get_session() as session:
            return self._resolve_snapshot_evidence_in_session(session, snapshot)

    @classmethod
    def _resolve_snapshot_evidence_in_session(
        cls,
        session: Any,
        snapshot: RadarRunSnapshot,
    ) -> tuple[ConstituentEvidence, ...]:
        resolved: list[ConstituentEvidence] = []
        for sector in snapshot.sectors:
            observation = SectorObservation.model_validate(sector.observation)
            set_key = observation.raw_reference.get("constituent_set_key")
            if set_key is None:
                continue
            if not isinstance(set_key, str) or not set_key:
                raise ValueError(
                    f"invalid constituent_set_key for {sector.sector_id}"
                )
            set_row = session.get(RadarConstituentSetRecord, set_key)
            if set_row is None:
                raise ValueError(
                    f"missing referenced constituent set for {sector.sector_id}: "
                    f"{set_key}"
                )
            if set_row.market != snapshot.market:
                raise ValueError(
                    f"constituent evidence market mismatch for {sector.sector_id}"
                )
            if set_row.sector_id != sector.sector_id:
                raise ValueError(
                    f"constituent evidence sector mismatch for {sector.sector_id}"
                )
            reference_source = cls._reference_membership_source(
                observation.raw_reference,
                sector.sector_id,
            )
            if reference_source != set_row.source:
                raise ValueError(
                    f"constituent evidence source mismatch for {sector.sector_id}"
                )
            reference_date = cls._reference_data_date(
                observation.raw_reference,
                sector.sector_id,
            )
            observation_row = session.execute(
                select(RadarConstituentObservationRecord).where(
                    and_(
                        RadarConstituentObservationRecord.market
                        == snapshot.market,
                        RadarConstituentObservationRecord.sector_id
                        == sector.sector_id,
                        RadarConstituentObservationRecord.data_date
                        == reference_date,
                        RadarConstituentObservationRecord.source == set_row.source,
                        RadarConstituentObservationRecord.set_key == set_key,
                    )
                )
            ).scalar_one_or_none()
            if observation_row is None:
                raise ValueError(
                    f"missing referenced constituent observation for "
                    f"{sector.sector_id}: {set_key}"
                )
            evidence = cls._evidence_from_rows(set_row, observation_row)
            cls._validate_point_in_time_evidence(evidence, snapshot, observation)
            resolved.append(evidence)
        return tuple(resolved)

    @classmethod
    def _evidence_from_rows(
        cls,
        set_row: RadarConstituentSetRecord,
        observation_row: RadarConstituentObservationRecord,
    ) -> ConstituentEvidence:
        content = cls._constituent_set_content_from_row(set_row)
        if observation_row.set_key != content.set_key:
            raise ValueError(
                f"constituent observation set mismatch for {content.set_key}"
            )
        if (
            observation_row.market != content.market
            or observation_row.sector_id != content.sector_id
            or observation_row.source != content.source
        ):
            raise ValueError(
                f"constituent observation identity mismatch for {content.set_key}"
            )
        evidence = ConstituentEvidence(
            market=content.market,
            sector_id=content.sector_id,
            source=content.source,
            data_date=observation_row.data_date,
            observed_at=_aware(observation_row.observed_at),
            codes=content.codes,
            set_key=content.set_key,
        )
        cls._validate_constituent_evidence(evidence)
        return evidence

    @classmethod
    def _constituent_set_content_from_row(
        cls,
        set_row: RadarConstituentSetRecord,
    ) -> ConstituentSetContent:
        try:
            decoded_codes = json.loads(set_row.codes_json)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid constituent set JSON for {set_row.set_key}"
            ) from exc
        if not isinstance(decoded_codes, list):
            raise ValueError(
                f"invalid constituent set JSON for {set_row.set_key}"
            )
        if set_row.constituent_count != len(decoded_codes):
            raise ValueError(
                f"constituent count mismatch for {set_row.set_key}"
            )
        codes = tuple(decoded_codes)
        cls._validate_constituent_set_identity(
            market=set_row.market,
            sector_id=set_row.sector_id,
            source=set_row.source,
            codes=codes,
            set_key=set_row.set_key,
        )
        if not isinstance(set_row.created_at, datetime):
            raise ValueError(
                f"constituent set created_at is invalid for {set_row.set_key}"
            )
        return ConstituentSetContent(
            set_key=set_row.set_key,
            market=set_row.market,
            sector_id=set_row.sector_id,
            source=set_row.source,
            codes=codes,
            constituent_count=set_row.constituent_count,
            created_at=_aware(set_row.created_at),
        )

    @classmethod
    def _snapshot_from_run_in_session(
        cls,
        session: Any,
        run: RadarRunRecord,
    ) -> RadarRunSnapshot:
        sectors = cls._list_sector_snapshots_in_session(session, int(run.id))
        _, selections, regime, position_plan = (
            cls._phase2b_evidence_from_run_in_session(session, int(run.id))
        )
        return RadarRunSnapshot(
            run_key=run.run_key,
            market=run.market,
            trigger=run.trigger,
            as_of=_aware(run.as_of),
            quality=run.quality,
            scoring_version=run.scoring_version,
            sectors=sectors,
            provider_trace=json.loads(run.provider_trace_json or "[]"),
            etfs=selections,
            regime=regime,
            position_plan=position_plan,
        )

    @staticmethod
    def _phase2b_evidence_from_run_in_session(
        session: Any,
        run_id: int,
    ) -> tuple[
        tuple[EtfObservation, ...],
        tuple[EtfSelection, ...],
        MarketRegimeAssessment | None,
        PositionPlan | None,
    ]:
        observation_rows = session.execute(
            select(RadarEtfObservationRecord)
            .where(RadarEtfObservationRecord.run_id == run_id)
            .order_by(
                RadarEtfObservationRecord.position,
                RadarEtfObservationRecord.id,
            )
        ).scalars().all()
        selection_rows = session.execute(
            select(RadarEtfSelectionRecord)
            .where(RadarEtfSelectionRecord.run_id == run_id)
            .order_by(
                RadarEtfSelectionRecord.position,
                RadarEtfSelectionRecord.id,
            )
        ).scalars().all()
        regime_row = session.execute(
            select(RadarRegimeAssessmentRecord).where(
                RadarRegimeAssessmentRecord.run_id == run_id
            )
        ).scalar_one_or_none()
        plan_row = session.execute(
            select(RadarPositionPlanRecord).where(
                RadarPositionPlanRecord.run_id == run_id
            )
        ).scalar_one_or_none()

        observations = tuple(
            EtfObservation.model_validate(json.loads(row.observation_json))
            for row in observation_rows
        )
        selections = tuple(
            EtfSelection.model_validate(json.loads(row.selection_json))
            for row in selection_rows
        )
        regime = (
            MarketRegimeAssessment.model_validate(
                json.loads(regime_row.assessment_json)
            )
            if regime_row is not None
            else None
        )
        position_plan = (
            PositionPlan.model_validate(json.loads(plan_row.plan_json))
            if plan_row is not None
            else None
        )
        return observations, selections, regime, position_plan

    def list_sector_snapshots(self, run_id: int) -> list[SectorScore]:
        with self.db.get_session() as session:
            return self._list_sector_snapshots_in_session(session, run_id)

    @staticmethod
    def _list_sector_snapshots_in_session(
        session: Any,
        run_id: int,
    ) -> list[SectorScore]:
        rows = session.execute(
            select(RadarSectorSnapshotRecord)
            .where(RadarSectorSnapshotRecord.run_id == run_id)
            .order_by(
                RadarSectorSnapshotRecord.position.is_(None),
                RadarSectorSnapshotRecord.position,
                RadarSectorSnapshotRecord.id,
            )
        ).scalars().all()
        return [
            SectorScore(
                sector_id=row.sector_id,
                name=row.name,
                kind=row.kind,
                scoring_version=row.scoring_version,
                gross_score=row.gross_score,
                risk_deduction=row.risk_deduction,
                score=row.score,
                confidence=row.confidence,
                state=row.state,
                factors=json.loads(row.factors_json or "{}"),
                risk_reasons=json.loads(row.risk_reasons_json or "[]"),
                missing_fields=json.loads(row.missing_fields_json or "[]"),
                source=row.source,
                observed_at=_aware(row.observed_at),
                quality=row.quality,
                observation=json.loads(row.observation_json or "{}"),
            )
            for row in rows
        ]
