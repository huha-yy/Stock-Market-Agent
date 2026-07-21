from datetime import date
from pathlib import Path

import pytest

from src.market_radar.universe import UniverseLoader, canonical_sector_id


def test_canonical_sector_id_normalizes_aliases() -> None:
    assert canonical_sector_id("industry", " 半导体设备 ") == "industry:半导体设备"
    assert canonical_sector_id("concept", "AI  算力") == "concept:ai-算力"


def test_loader_filters_effective_dates_and_builds_etfs(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(
        """
version: 1
sectors:
  - kind: industry
    name: 半导体
    aliases: [芯片]
    effective_from: 2025-01-01
    etfs:
      - code: "512480"
        name: 半导体ETF
        effective_from: 2025-01-01
      - code: "512499"
        name: 已失效ETF
        effective_from: 2020-01-01
        effective_to: 2024-12-31
  - kind: industry
    name: 已失效板块
    effective_from: 2020-01-01
    effective_to: 2024-12-31
    etfs: []
""".strip(),
        encoding="utf-8",
    )

    sectors = UniverseLoader(path).load(date(2026, 7, 21))

    assert [sector.name for sector in sectors] == ["半导体"]
    assert sectors[0].sector_id == "industry:半导体"
    assert [item.code for item in sectors[0].etfs] == ["512480"]


def test_loader_rejects_duplicate_active_etf_code(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(
        """
version: 1
sectors:
  - kind: industry
    name: 半导体
    effective_from: 2026-01-01
    etfs:
      - code: "512480"
        name: 半导体ETF
        effective_from: 2026-01-01
  - kind: concept
    name: 芯片
    effective_from: 2026-01-01
    etfs:
      - code: "512480"
        name: 芯片ETF
        effective_from: 2026-01-01
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate ETF code"):
        UniverseLoader(path).load(date(2026, 7, 21))


def test_repository_seed_contains_no_duplicate_etf_code() -> None:
    path = Path("src/data/market_radar/a_share_etfs.yaml")
    sectors = UniverseLoader(path).load(date(2026, 7, 21))
    codes = [etf.code for sector in sectors for etf in sector.etfs]
    assert len(codes) == len(set(codes))
