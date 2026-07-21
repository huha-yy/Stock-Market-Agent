from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from data_provider import DataFetcherManager
from src.market_radar.models import SectorDefinition, SectorObservation
from src.market_radar.universe import canonical_sector_id


class ProviderBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: list[SectorObservation]
    trace: list[dict[str, Any]]
    discovered_sectors: list[SectorDefinition] = Field(default_factory=list)


class MarketRadarProvider(Protocol):
    def fetch(
        self,
        market: str,
        as_of: datetime,
        universe: list[SectorDefinition],
    ) -> ProviderBatch:
        pass


class LegacyRankingProvider:
    def __init__(self, manager: DataFetcherManager, limit: int = 1000) -> None:
        self.manager = manager
        self.limit = max(1, int(limit))

    def fetch(
        self,
        market: str,
        as_of: datetime,
        universe: list[SectorDefinition],
    ) -> ProviderBatch:
        if market != "cn":
            raise ValueError("Market Radar Phase 1 supports market=cn only")

        alias_map: dict[tuple[str, str], SectorDefinition] = {}
        for sector in universe:
            for name in [sector.name, *sector.aliases]:
                alias_map[(sector.kind, self._name_key(name))] = sector

        observations: list[SectorObservation] = []
        trace: list[dict[str, Any]] = []
        discovered_by_id: dict[str, SectorDefinition] = {}
        datasets = (
            ("industry", self.manager.get_sector_rankings_with_meta),
            ("concept", self.manager.get_concept_rankings_with_meta),
        )
        for kind, fetch in datasets:
            top, bottom, source_chain, last_error = fetch(self.limit)
            rows = self._dedupe_rows([*top, *bottom])
            trace.extend({"dataset": kind, **entry} for entry in source_chain)
            successful_source = next(
                (
                    entry["provider"]
                    for entry in reversed(source_chain)
                    if entry.get("result") == "ok"
                ),
                f"{kind}_rankings_unavailable",
            )
            if not rows and last_error:
                trace.append(
                    {
                        "dataset": kind,
                        "provider": "manager",
                        "result": "failed",
                        "error": last_error,
                    }
                )
            for row in rows:
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                definition = alias_map.get((kind, self._name_key(name)))
                sector_id = (
                    definition.sector_id
                    if definition
                    else canonical_sector_id(kind, name)
                )
                if definition is None:
                    discovered_by_id.setdefault(
                        sector_id,
                        SectorDefinition(
                            sector_id=sector_id,
                            kind=kind,
                            name=name,
                            effective_from=as_of.date(),
                        ),
                    )
                observations.append(
                    SectorObservation(
                        sector_id=sector_id,
                        kind=kind,
                        name=definition.name if definition else name,
                        observed_at=as_of,
                        source=successful_source,
                        freshness_seconds=0,
                        quality="partial",
                        return_1d_pct=float(row["change_pct"]),
                        missing_fields=tuple(
                            field
                            for field in SectorObservation.tracked_metric_fields
                            if field != "return_1d_pct"
                        ),
                        raw_reference=dict(row),
                    )
                )
        return ProviderBatch(
            observations=observations,
            trace=trace,
            discovered_sectors=sorted(
                discovered_by_id.values(),
                key=lambda item: (item.kind, item.sector_id),
            ),
        )

    @staticmethod
    def _name_key(name: str) -> str:
        return "".join(str(name or "").strip().lower().split())

    @classmethod
    def _dedupe_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = cls._name_key(str(row.get("name") or ""))
            if key:
                by_name[key] = dict(row)
        return list(by_name.values())
