from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, or_, select

from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorScore,
)
from src.storage import (
    DatabaseManager,
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


class MarketRadarRepository:
    def __init__(self, db: DatabaseManager | None = None) -> None:
        self.db = db or DatabaseManager.get_instance()

    def sync_universe(self, sectors: list[SectorDefinition]) -> None:
        if any(sector.market != "cn" for sector in sectors):
            raise ValueError("Market Radar Phase 1 supports market=cn only")

        def write(session: Any) -> None:
            for sector in sectors:
                row = session.execute(
                    select(RadarUniverseRecord).where(
                        and_(
                            RadarUniverseRecord.sector_id == sector.sector_id,
                            RadarUniverseRecord.effective_from
                            == sector.effective_from,
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

        self.db._run_write_transaction("sync_market_radar_universe", write)

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
                    ],
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                )
                for row in rows
            ]

    def save_run(self, snapshot: RadarRunSnapshot) -> int:
        def write(session: Any) -> int:
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
            for sector in snapshot.sectors:
                sector_data = sector.model_dump(mode="json")
                session.add(
                    RadarSectorSnapshotRecord(
                        run_id=run.id,
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

        return self.db._run_write_transaction(
            f"save_market_radar_run[{snapshot.run_key}]",
            write,
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
            sectors = self._list_sector_snapshots_in_session(session, int(run.id))
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
                desc(RadarSectorSnapshotRecord.score),
                RadarSectorSnapshotRecord.sector_id,
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
