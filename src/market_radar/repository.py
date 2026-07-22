from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.exc import IntegrityError

from data_provider.base import normalize_stock_code
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
from src.storage import (
    DatabaseManager,
    RadarConstituentObservationRecord,
    RadarConstituentSetRecord,
    RadarRunRecord,
    RadarSectorSnapshotRecord,
    RadarUniverseRecord,
    to_utc_naive_datetime,
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

    def sync_universe(self, sectors: list[SectorDefinition]) -> None:
        self._validate_universe(sectors)
        self._run_idempotent_write(
            "sync_market_radar_universe",
            lambda session: self._sync_universe_in_session(session, sectors),
        )

    @staticmethod
    def _validate_universe(sectors: list[SectorDefinition]) -> None:
        if any(sector.market != "cn" for sector in sectors):
            raise ValueError("Market Radar Phase 1 supports market=cn only")

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
        snapshot: RadarRunSnapshot,
    ) -> int:
        self._validate_universe(sectors)
        self._validate_snapshot_traceability(snapshot)
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
                return int(existing_id)
            self._sync_universe_in_session(session, sectors)
            self._save_constituent_evidence_in_session(
                session,
                validated_evidence,
            )
            return self._save_run_in_session(session, snapshot)

        return self._run_idempotent_write(
            f"save_market_radar_enriched_run[{snapshot.run_key}]",
            write,
        )

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

    @staticmethod
    def _save_run_in_session(
        session: Any,
        snapshot: RadarRunSnapshot,
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
        return int(run.id)

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

    def get_latest_run(self, market: str) -> RadarRunSnapshot | None:
        with self.db.get_session() as session:
            run = session.execute(
                select(RadarRunRecord)
                .where(RadarRunRecord.market == market)
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

    def get_run(self, run_id: int) -> RadarRunSnapshot | None:
        with self.db.get_session() as session:
            run = session.execute(
                select(RadarRunRecord).where(RadarRunRecord.id == run_id)
            ).scalar_one_or_none()
            if run is None:
                return None
            return self._snapshot_from_run_in_session(session, run)

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
        return RadarRunSnapshot(
            run_key=run.run_key,
            market=run.market,
            trigger=run.trigger,
            as_of=_aware(run.as_of),
            quality=run.quality,
            scoring_version=run.scoring_version,
            sectors=sectors,
            provider_trace=json.loads(run.provider_trace_json or "[]"),
        )

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
