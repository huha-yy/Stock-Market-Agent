from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from src.market_radar.models import EtfDefinition, SectorDefinition, SectorKind


def canonical_sector_id(kind: SectorKind, name: str) -> str:
    normalized = re.sub(r"\s+", " ", str(name or "").strip()).lower()
    normalized = normalized.replace(" ", "-")
    if not normalized:
        raise ValueError("sector name is required")
    return f"{kind}:{normalized}"


class UniverseLoader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, as_of: date) -> list[SectorDefinition]:
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if payload.get("version") != 1:
            raise ValueError("unsupported Market Radar universe version")

        sectors: list[SectorDefinition] = []
        seen_etfs: set[str] = set()
        for raw in payload.get("sectors", []):
            kind = raw["kind"]
            name = str(raw["name"]).strip()
            sector_id = canonical_sector_id(kind, name)
            effective_from = date.fromisoformat(str(raw["effective_from"]))
            effective_to = (
                date.fromisoformat(str(raw["effective_to"]))
                if raw.get("effective_to")
                else None
            )
            if effective_from > as_of or (
                effective_to is not None and effective_to < as_of
            ):
                continue

            etfs: list[EtfDefinition] = []
            for item in raw.get("etfs", []):
                code = str(item["code"])
                etf_effective_from = date.fromisoformat(str(item["effective_from"]))
                etf_effective_to = (
                    date.fromisoformat(str(item["effective_to"]))
                    if item.get("effective_to")
                    else None
                )
                if etf_effective_from > as_of or (
                    etf_effective_to is not None and etf_effective_to < as_of
                ):
                    continue
                if code in seen_etfs:
                    raise ValueError(f"duplicate ETF code in universe: {code}")
                seen_etfs.add(code)
                etfs.append(
                    EtfDefinition(
                        code=code,
                        name=str(item["name"]).strip(),
                        sector_id=sector_id,
                        benchmark_code=item.get("benchmark_code"),
                        effective_from=etf_effective_from,
                        effective_to=etf_effective_to,
                    )
                )
            sectors.append(
                SectorDefinition(
                    sector_id=sector_id,
                    kind=kind,
                    name=name,
                    aliases=[str(value).strip() for value in raw.get("aliases", [])],
                    benchmark_code=raw.get("benchmark_code"),
                    etfs=etfs,
                    effective_from=effective_from,
                    effective_to=effective_to,
                )
            )
        return sorted(sectors, key=lambda sector: (sector.kind, sector.sector_id))
