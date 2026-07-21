# Market Radar Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a runnable A-share Market Radar foundation with canonical sector/ETF contracts, provider provenance, deterministic scoring, immutable persistence, and point-in-time replay.

**Architecture:** Add a bounded `src.market_radar` package beside the existing single-stock pipeline. Pure Pydantic contracts and ranking functions remain independent of network and storage; provider adapters, repositories, and orchestration depend inward on those contracts. Phase 1 exposes a manual CLI and persisted snapshots but does not add Web/API, alerts, scheduled scans, Hong Kong support, position policy, outcomes, or LLM narration.

**Tech Stack:** Python 3.10+, Pydantic v2, SQLAlchemy 2, PyYAML, pytest, existing `DataFetcherManager`, existing SQLite `DatabaseManager`.

## Global Constraints

- Preserve `origin=https://github.com/huha-yy/Stock-Market-Agent.git` and `upstream=https://github.com/ZhuLinsen/daily_stock_analysis.git`.
- Phase 1 supports market `cn` only; Hong Kong belongs to a later plan.
- Scoring is deterministic and versioned as `cn-v1`; no LLM call may participate.
- Every observation carries source, observed time, freshness, quality, and missing-field provenance.
- Missing fields lower confidence; they do not silently become zero-strength evidence.
- A critical stale price observation cannot produce an upgraded state.
- Online and replay paths call the same ranking function.
- No broker integration, order execution, leverage, actual holdings, personalized suitability, Web/API, alerts, or scheduler changes are in scope.
- New configuration must be optional and documented in `.env.example`.
- User-visible CLI behavior requires updates to `docs/market-radar.md`, `docs/INDEX.md`, and `docs/CHANGELOG.md`.
- Before every commit step, obtain explicit user confirmation unless the user has explicitly authorized all commits for this plan.
- Commit messages are English and contain no `Co-Authored-By` trailer.

---

## File Structure

### New production files

- `src/market_radar/__init__.py`: public Phase 1 exports only.
- `src/market_radar/models.py`: canonical immutable contracts and enums.
- `src/market_radar/universe.py`: YAML-backed effective-dated A-share sector/ETF universe.
- `src/market_radar/providers.py`: provider protocol and legacy ranking adapter.
- `src/market_radar/ranking.py`: pure normalization, scoring, confidence, and lifecycle classification.
- `src/market_radar/repository.py`: transactional universe/run/snapshot persistence.
- `src/market_radar/service.py`: one-run orchestration with injected clock/provider/repository.
- `src/market_radar/replay.py`: chronological replay and future-observation guard.
- `src/data/market_radar/a_share_etfs.yaml`: curated initial A-share ETF mappings.
- `scripts/run_market_radar.py`: manual `cn` snapshot CLI.
- `docs/market-radar.md`: Phase 1 behavior, limitations, provenance, and commands.

### Modified production files

- `src/storage.py`: three Phase 1 SQLAlchemy records and indexes.
- `src/config.py`: optional Phase 1 configuration fields and environment parsing.
- `.env.example`: documented radar settings.
- `docs/INDEX.md`: Market Radar documentation link.
- `docs/CHANGELOG.md`: one flat `[Unreleased]` feature entry.

### New tests

- `tests/market_radar/test_models.py`
- `tests/market_radar/test_universe.py`
- `tests/market_radar/test_providers.py`
- `tests/market_radar/test_ranking.py`
- `tests/market_radar/test_repository.py`
- `tests/market_radar/test_service.py`
- `tests/market_radar/test_replay.py`
- `tests/test_run_market_radar.py`

---

### Task 1: Canonical Market Radar Contracts

**Files:**
- Create: `src/market_radar/__init__.py`
- Create: `src/market_radar/models.py`
- Create: `tests/market_radar/test_models.py`

**Interfaces:**
- Produces: `SectorDefinition`, `EtfDefinition`, `SectorObservation`, `FactorBreakdown`, `SectorScore`, `RadarRunSnapshot`, and the literal types consumed by every later task.
- Consumes: Pydantic v2 and Python standard library only.

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/market_radar/test_models.py
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def test_sector_definition_rejects_non_cn_market() -> None:
    with pytest.raises(ValidationError):
        SectorDefinition(
            sector_id="industry:semiconductor",
            market="hk",
            kind="industry",
            name="半导体",
            effective_from=date(2026, 1, 1),
        )


def test_observation_keeps_missing_fields_and_provenance() -> None:
    observation = SectorObservation(
        sector_id="industry:semiconductor",
        name="半导体",
        kind="industry",
        observed_at=NOW,
        source="akshare_industry",
        freshness_seconds=12,
        quality="partial",
        return_1d_pct=2.5,
        missing_fields=["return_20d_pct", "capital_flow_5d"],
    )

    assert observation.market == "cn"
    assert observation.return_20d_pct is None
    assert observation.missing_fields == ["return_20d_pct", "capital_flow_5d"]


def test_run_snapshot_requires_unique_sector_ids() -> None:
    score = SectorScore(
        sector_id="industry:semiconductor",
        name="半导体",
        kind="industry",
        scoring_version="cn-v1",
        gross_score=70.0,
        risk_deduction=0.0,
        score=70.0,
        confidence=0.65,
        state="improving",
        factors={},
        risk_reasons=[],
        missing_fields=[],
        source="akshare_industry",
        observed_at=NOW,
        quality="partial",
    )

    with pytest.raises(ValidationError, match="duplicate sector_id"):
        RadarRunSnapshot(
            run_key="cn:20260721T060000Z:manual",
            market="cn",
            trigger="manual",
            as_of=NOW,
            quality="partial",
            scoring_version="cn-v1",
            sectors=[score, score],
            provider_trace=[],
        )


def test_etf_definition_validates_six_digit_code() -> None:
    with pytest.raises(ValidationError):
        EtfDefinition(
            code="ETF512480",
            name="半导体ETF",
            sector_id="industry:semiconductor",
            effective_from=date(2026, 1, 1),
        )
```

- [ ] **Step 2: Run the contract tests and confirm the import failure**

Run: `python -m pytest tests/market_radar/test_models.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'src.market_radar'`.

- [ ] **Step 3: Implement the immutable contracts**

```python
# src/market_radar/models.py
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MarketRadarMarket = Literal["cn"]
SectorKind = Literal["industry", "concept"]
DataQuality = Literal["complete", "partial", "stale", "unavailable"]
SectorState = Literal[
    "leading",
    "improving",
    "neutral",
    "weakening",
    "avoid",
    "insufficient_data",
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EtfDefinition(FrozenModel):
    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    sector_id: str = Field(min_length=3)
    benchmark_code: str | None = None
    effective_from: date
    effective_to: date | None = None


class SectorDefinition(FrozenModel):
    sector_id: str = Field(min_length=3)
    market: MarketRadarMarket = "cn"
    kind: SectorKind
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    benchmark_code: str | None = None
    etfs: list[EtfDefinition] = Field(default_factory=list)
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def validate_effective_range(self) -> "SectorDefinition":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if any(etf.sector_id != self.sector_id for etf in self.etfs):
            raise ValueError("ETF sector_id must match parent sector_id")
        return self


class SectorObservation(FrozenModel):
    sector_id: str = Field(min_length=3)
    market: MarketRadarMarket = "cn"
    kind: SectorKind
    name: str = Field(min_length=1)
    observed_at: datetime
    source: str = Field(min_length=1)
    freshness_seconds: int = Field(ge=0)
    quality: DataQuality
    return_1d_pct: float | None = None
    return_5d_pct: float | None = None
    return_20d_pct: float | None = None
    benchmark_return_20d_pct: float | None = None
    capital_flow_1d: float | None = None
    capital_flow_5d: float | None = None
    capital_flow_20d: float | None = None
    turnover_ratio_20d: float | None = Field(default=None, ge=0)
    up_count: int | None = Field(default=None, ge=0)
    down_count: int | None = Field(default=None, ge=0)
    flat_count: int | None = Field(default=None, ge=0)
    volatility_ratio_20d: float | None = Field(default=None, ge=0)
    distance_ma20_pct: float | None = None
    price_flow_divergence: bool = False
    concentration_ratio: float | None = Field(default=None, ge=0, le=1)
    catalyst_score: float | None = Field(default=None, ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list)
    raw_reference: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class FactorBreakdown(FrozenModel):
    trend_momentum: float = Field(ge=0, le=25)
    relative_strength: float = Field(ge=0, le=20)
    capital_flow: float = Field(ge=0, le=20)
    breadth: float = Field(ge=0, le=15)
    liquidity_expansion: float = Field(ge=0, le=10)
    catalyst: float = Field(ge=0, le=10)


class SectorScore(FrozenModel):
    sector_id: str
    name: str
    kind: SectorKind
    scoring_version: str
    gross_score: float = Field(ge=0, le=100)
    risk_deduction: float = Field(ge=0, le=30)
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    state: SectorState
    factors: FactorBreakdown | dict[str, Any]
    risk_reasons: list[str]
    missing_fields: list[str]
    source: str
    observed_at: datetime
    quality: DataQuality
    observation: dict[str, Any] = Field(default_factory=dict)


class RadarRunSnapshot(FrozenModel):
    run_key: str
    market: MarketRadarMarket
    trigger: Literal["manual", "replay"]
    as_of: datetime
    quality: DataQuality
    scoring_version: str
    sectors: list[SectorScore]
    provider_trace: list[dict[str, Any]]

    @model_validator(mode="after")
    def require_unique_sectors(self) -> "RadarRunSnapshot":
        sector_ids = [sector.sector_id for sector in self.sectors]
        if len(sector_ids) != len(set(sector_ids)):
            raise ValueError("duplicate sector_id in run snapshot")
        return self
```

```python
# src/market_radar/__init__.py
from src.market_radar.models import (
    DataQuality,
    EtfDefinition,
    FactorBreakdown,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
    SectorState,
)

__all__ = [
    "DataQuality",
    "EtfDefinition",
    "FactorBreakdown",
    "RadarRunSnapshot",
    "SectorDefinition",
    "SectorObservation",
    "SectorScore",
    "SectorState",
]
```

- [ ] **Step 4: Run the contract tests**

Run: `python -m pytest tests/market_radar/test_models.py -q`

Expected: `4 passed`.

- [ ] **Step 5: Run syntax validation**

Run: `python -m py_compile src/market_radar/__init__.py src/market_radar/models.py`

Expected: exit code `0` and no output.

- [ ] **Step 6: Commit the contract task after approval**

```bash
git add src/market_radar/__init__.py src/market_radar/models.py tests/market_radar/test_models.py
git commit -m "feat: add Market Radar domain contracts"
```

---

### Task 2: Effective-Dated Sector and ETF Universe

**Files:**
- Create: `src/data/market_radar/a_share_etfs.yaml`
- Create: `src/market_radar/universe.py`
- Create: `tests/market_radar/test_universe.py`

**Interfaces:**
- Consumes: `SectorDefinition` and `EtfDefinition` from Task 1.
- Produces: `UniverseLoader.load(as_of: date) -> list[SectorDefinition]` and `canonical_sector_id(kind: SectorKind, name: str) -> str`.

- [ ] **Step 1: Write failing universe tests**

```python
# tests/market_radar/test_universe.py
from datetime import date
from pathlib import Path

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


def test_repository_seed_contains_no_duplicate_etf_code() -> None:
    path = Path("src/data/market_radar/a_share_etfs.yaml")
    sectors = UniverseLoader(path).load(date(2026, 7, 21))
    codes = [etf.code for sector in sectors for etf in sector.etfs]
    assert len(codes) == len(set(codes))
```

- [ ] **Step 2: Run the universe tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_universe.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.market_radar.universe'`.

- [ ] **Step 3: Implement canonicalization and YAML loading**

```python
# src/market_radar/universe.py
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
            if effective_from > as_of or (effective_to is not None and effective_to < as_of):
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
```

- [ ] **Step 4: Add the reviewed initial ETF mapping seed**

```yaml
# src/data/market_radar/a_share_etfs.yaml
version: 1
sectors:
  - kind: industry
    name: 半导体
    aliases: [芯片, 半导体设备]
    effective_from: 2026-01-01
    etfs:
      - code: "512480"
        name: 半导体ETF
        effective_from: 2026-01-01
      - code: "159995"
        name: 芯片ETF
        effective_from: 2026-01-01
  - kind: industry
    name: 证券
    aliases: [券商]
    effective_from: 2026-01-01
    etfs:
      - code: "512880"
        name: 证券ETF
        effective_from: 2026-01-01
      - code: "512000"
        name: 券商ETF
        effective_from: 2026-01-01
  - kind: industry
    name: 银行
    aliases: []
    effective_from: 2026-01-01
    etfs:
      - code: "512800"
        name: 银行ETF
        effective_from: 2026-01-01
  - kind: industry
    name: 医药生物
    aliases: [医药]
    effective_from: 2026-01-01
    etfs:
      - code: "512010"
        name: 医药ETF
        effective_from: 2026-01-01
      - code: "159929"
        name: 医药ETF
        effective_from: 2026-01-01
  - kind: concept
    name: 新能源车
    aliases: [新能车]
    effective_from: 2026-01-01
    etfs:
      - code: "515030"
        name: 新能源车ETF
        effective_from: 2026-01-01
      - code: "515700"
        name: 新能车ETF
        effective_from: 2026-01-01
  - kind: industry
    name: 国防军工
    aliases: [军工]
    effective_from: 2026-01-01
    etfs:
      - code: "512660"
        name: 军工ETF
        effective_from: 2026-01-01
  - kind: industry
    name: 有色金属
    aliases: [有色]
    effective_from: 2026-01-01
    etfs:
      - code: "512400"
        name: 有色金属ETF
        effective_from: 2026-01-01
  - kind: concept
    name: 光伏
    aliases: [光伏设备]
    effective_from: 2026-01-01
    etfs:
      - code: "515790"
        name: 光伏ETF
        effective_from: 2026-01-01
```

Before merging this seed, verify every code/name pair against the Shanghai Stock Exchange or Shenzhen Stock Exchange fund-product search. When an exchange page omits the short name, use the fund manager's current product page or prospectus. Record the source URL and verification date for every code in the PR description. This review is data verification, not a code change.

- [ ] **Step 5: Run universe tests and syntax validation**

Run: `python -m pytest tests/market_radar/test_universe.py -q`

Expected: `3 passed`.

Run: `python -m py_compile src/market_radar/universe.py`

Expected: exit code `0`.

- [ ] **Step 6: Commit the universe task after approval**

```bash
git add src/data/market_radar/a_share_etfs.yaml src/market_radar/universe.py tests/market_radar/test_universe.py
git commit -m "feat: add A-share radar universe"
```

---

### Task 3: Provider Contract and Legacy Ranking Adapter

**Files:**
- Modify: `data_provider/base.py:3587-3720`
- Create: `src/market_radar/providers.py`
- Create: `tests/market_radar/test_providers.py`

**Interfaces:**
- Consumes: `SectorDefinition`, `SectorObservation`, `canonical_sector_id`, and `DataFetcherManager.get_sector_rankings/get_concept_rankings`.
- Produces: `MarketRadarProvider.fetch(market: str, as_of: datetime, universe: list[SectorDefinition]) -> ProviderBatch` and `LegacyRankingProvider`.

- [ ] **Step 1: Write failing adapter tests**

```python
# tests/market_radar/test_providers.py
from datetime import datetime, timezone

from src.market_radar.models import SectorDefinition
from src.market_radar.providers import LegacyRankingProvider


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


class FakeManager:
    def get_sector_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [{"name": "半导体", "change_pct": 2.5}],
            [{"name": "煤炭", "change_pct": -1.2}],
            [{"provider": "AkshareFetcher", "result": "ok", "duration_ms": 12}],
            "",
        )

    def get_concept_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [{"name": "AI算力", "change_pct": 3.1}],
            [],
            [{"provider": "AkshareFetcher", "result": "ok", "duration_ms": 9}],
            "",
        )


def test_legacy_adapter_preserves_partial_quality_and_missing_fields() -> None:
    universe = [
        SectorDefinition(
            sector_id="industry:半导体",
            kind="industry",
            name="半导体",
            aliases=["芯片"],
            effective_from=NOW.date(),
        )
    ]
    batch = LegacyRankingProvider(FakeManager(), limit=1000).fetch("cn", NOW, universe)

    semiconductor = next(item for item in batch.observations if item.name == "半导体")
    assert semiconductor.return_1d_pct == 2.5
    assert semiconductor.quality == "partial"
    assert semiconductor.source == "AkshareFetcher"
    assert "return_20d_pct" in semiconductor.missing_fields
    assert batch.trace[0]["dataset"] == "industry"
    assert batch.trace[0]["provider"] == "AkshareFetcher"
    assert [item.sector_id for item in batch.discovered_sectors] == ["concept:ai算力", "industry:煤炭"]


def test_legacy_adapter_rejects_unsupported_market() -> None:
    provider = LegacyRankingProvider(FakeManager(), limit=1000)
    try:
        provider.fetch("hk", NOW, [])
    except ValueError as exc:
        assert str(exc) == "Market Radar Phase 1 supports market=cn only"
    else:
        raise AssertionError("expected unsupported-market error")


def test_manager_metadata_methods_preserve_fallback_chain() -> None:
    from data_provider.base import DataFetcherManager

    class EmptyFetcher:
        name = "EmptyFetcher"
        priority = 0

        def get_sector_rankings(self, n):
            return None

        def get_concept_rankings(self, n):
            return None

    class WorkingFetcher:
        name = "WorkingFetcher"
        priority = 1

        def get_sector_rankings(self, n):
            return ([{"name": "银行", "change_pct": 1.0}], [])

        def get_concept_rankings(self, n):
            return ([{"name": "中特估", "change_pct": 1.5}], [])

    manager = DataFetcherManager(fetchers=[EmptyFetcher(), WorkingFetcher()])
    _, _, sector_trace, _ = manager.get_sector_rankings_with_meta(100)
    _, _, concept_trace, _ = manager.get_concept_rankings_with_meta(100)

    assert [item["provider"] for item in sector_trace] == ["EmptyFetcher", "WorkingFetcher"]
    assert [item["result"] for item in sector_trace] == ["empty", "ok"]
    assert [item["provider"] for item in concept_trace] == ["EmptyFetcher", "WorkingFetcher"]
```

- [ ] **Step 2: Run the provider tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_providers.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.market_radar.providers'`.

- [ ] **Step 3: Expose provider metadata without changing legacy callers**

Add this public wrapper immediately after `_get_sector_rankings_with_meta` in `DataFetcherManager`:

```python
def get_sector_rankings_with_meta(
    self,
    n: int = 5,
) -> Tuple[List[Dict], List[Dict], List[Dict[str, Any]], str]:
    """Return sector rankings with the complete ordered provider trace."""
    return self._get_sector_rankings_with_meta(n)
```

Add this method immediately before the existing `get_concept_rankings`. It deliberately performs an uncached fetch because the existing shared cache does not retain source provenance; the existing `get_concept_rankings` behavior remains unchanged for all current callers.

```python
def get_concept_rankings_with_meta(
    self,
    n: int = 5,
) -> Tuple[List[Dict], List[Dict], List[Dict[str, Any]], str]:
    """Return concept rankings with ordered provider trace for audit-sensitive consumers."""
    try:
        normalized_n = int(n)
    except (TypeError, ValueError):
        normalized_n = 5
    if normalized_n <= 0:
        normalized_n = 5

    source_chain: List[Dict[str, Any]] = []
    last_error = ""
    for fetcher in self._get_fetchers_snapshot():
        start = time.time()
        try:
            data = fetcher.get_concept_rankings(normalized_n)
            duration_ms = int((time.time() - start) * 1000)
            if data and (data[0] or data[1]):
                source_chain.append(
                    {
                        "provider": fetcher.name,
                        "result": "ok",
                        "duration_ms": duration_ms,
                    }
                )
                return data[0] or [], data[1] or [], source_chain, ""
            last_error = f"{fetcher.name}返回空结果"
            source_chain.append(
                {
                    "provider": fetcher.name,
                    "result": "empty",
                    "duration_ms": duration_ms,
                    "error": last_error,
                }
            )
        except Exception as exc:
            error_type, error_reason = summarize_exception(exc)
            last_error = f"{fetcher.name} ({error_type}) {error_reason}"
            source_chain.append(
                {
                    "provider": fetcher.name,
                    "result": "failed",
                    "duration_ms": int((time.time() - start) * 1000),
                    "error": error_reason,
                }
            )
    return [], [], source_chain, last_error
```

- [ ] **Step 4: Implement the provider boundary**

```python
# src/market_radar/providers.py
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
                (entry["provider"] for entry in reversed(source_chain) if entry.get("result") == "ok"),
                f"{kind}_rankings_unavailable",
            )
            if not rows and last_error:
                trace.append({"dataset": kind, "provider": "manager", "result": "failed", "error": last_error})
            for row in rows:
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                definition = alias_map.get((kind, self._name_key(name)))
                sector_id = definition.sector_id if definition else canonical_sector_id(kind, name)
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
                        missing_fields=[
                            "return_5d_pct",
                            "return_20d_pct",
                            "benchmark_return_20d_pct",
                            "capital_flow_1d",
                            "capital_flow_5d",
                            "capital_flow_20d",
                            "turnover_ratio_20d",
                            "breadth",
                        ],
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
```

- [ ] **Step 5: Run provider tests and syntax validation**

Run: `python -m pytest tests/market_radar/test_providers.py -q`

Expected: `3 passed`.

Run: `python -m py_compile data_provider/base.py src/market_radar/providers.py`

Expected: exit code `0`.

- [ ] **Step 6: Commit the provider task after approval**

```bash
git add data_provider/base.py src/market_radar/providers.py tests/market_radar/test_providers.py
git commit -m "feat: add Market Radar provider boundary"
```

---

### Task 4: Deterministic Ranking Engine

**Files:**
- Create: `src/market_radar/ranking.py`
- Create: `tests/market_radar/test_ranking.py`

**Interfaces:**
- Consumes: `list[SectorObservation]` and `RankingConfig`.
- Produces: `score_sectors(observations, config) -> list[SectorScore]`, sorted by descending score and then sector ID.

- [ ] **Step 1: Write failing ranking tests**

```python
# tests/market_radar/test_ranking.py
from datetime import datetime, timezone

from src.market_radar.models import SectorObservation
from src.market_radar.ranking import RankingConfig, score_sectors


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def observation(name: str, sector_id: str, **values) -> SectorObservation:
    payload = {
        "sector_id": sector_id,
        "kind": "industry",
        "name": name,
        "observed_at": NOW,
        "source": "fixture",
        "freshness_seconds": 30,
        "quality": "complete",
        "return_1d_pct": 1.0,
        "return_5d_pct": 3.0,
        "return_20d_pct": 8.0,
        "benchmark_return_20d_pct": 2.0,
        "capital_flow_1d": 3.0,
        "capital_flow_5d": 8.0,
        "capital_flow_20d": 15.0,
        "turnover_ratio_20d": 1.2,
        "up_count": 8,
        "down_count": 2,
        "flat_count": 0,
        "volatility_ratio_20d": 1.0,
        "distance_ma20_pct": 4.0,
        "concentration_ratio": 0.25,
        "catalyst_score": 0.5,
    }
    payload.update(values)
    return SectorObservation(**payload)


def test_scores_are_deterministic_and_sorted() -> None:
    weak = observation(
        "弱板块",
        "industry:weak",
        return_5d_pct=-3.0,
        return_20d_pct=-8.0,
        capital_flow_1d=-2.0,
        capital_flow_5d=-5.0,
        capital_flow_20d=-10.0,
        up_count=2,
        down_count=8,
    )
    strong = observation("强板块", "industry:strong")

    first = score_sectors([weak, strong], RankingConfig())
    second = score_sectors([weak, strong], RankingConfig())

    assert first == second
    assert [item.sector_id for item in first] == ["industry:strong", "industry:weak"]
    assert first[0].score > first[1].score


def test_missing_data_lowers_confidence_without_becoming_zero_strength() -> None:
    partial = observation(
        "部分数据",
        "industry:partial",
        quality="partial",
        return_5d_pct=None,
        return_20d_pct=None,
        benchmark_return_20d_pct=None,
        capital_flow_1d=None,
        capital_flow_5d=None,
        capital_flow_20d=None,
        up_count=None,
        down_count=None,
        flat_count=None,
        missing_fields=["return_20d_pct", "capital_flow_5d", "breadth"],
    )

    result = score_sectors([partial], RankingConfig())[0]

    assert result.factors.trend_momentum > 0
    assert 0 < result.confidence < 0.4
    assert result.state == "insufficient_data"


def test_stale_critical_price_is_insufficient() -> None:
    stale = observation(
        "过期板块",
        "industry:stale",
        quality="stale",
        freshness_seconds=4000,
    )

    result = score_sectors([stale], RankingConfig(stale_after_seconds=2700))[0]

    assert result.state == "insufficient_data"
    assert "critical_price_stale" in result.risk_reasons


def test_risk_deductions_are_capped_at_thirty() -> None:
    risky = observation(
        "拥挤板块",
        "industry:risky",
        volatility_ratio_20d=3.0,
        distance_ma20_pct=25.0,
        price_flow_divergence=True,
        concentration_ratio=0.95,
    )

    result = score_sectors([risky], RankingConfig())[0]

    assert result.risk_deduction == 30.0
    assert 0 <= result.score <= 100


def test_flow_score_uses_within_source_percentiles_not_absolute_units() -> None:
    base = [
        observation("甲", "industry:a", capital_flow_1d=1.0, capital_flow_5d=2.0, capital_flow_20d=3.0),
        observation("乙", "industry:b", capital_flow_1d=2.0, capital_flow_5d=4.0, capital_flow_20d=6.0),
    ]
    scaled = [
        item.model_copy(
            update={
                "capital_flow_1d": item.capital_flow_1d * 10_000,
                "capital_flow_5d": item.capital_flow_5d * 10_000,
                "capital_flow_20d": item.capital_flow_20d * 10_000,
            }
        )
        for item in base
    ]

    base_scores = {item.sector_id: item.factors.capital_flow for item in score_sectors(base, RankingConfig())}
    scaled_scores = {item.sector_id: item.factors.capital_flow for item in score_sectors(scaled, RankingConfig())}

    assert base_scores == scaled_scores
```

- [ ] **Step 2: Run ranking tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_ranking.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.market_radar.ranking'`.

- [ ] **Step 3: Implement deterministic factor scoring**

```python
# src/market_radar/ranking.py
from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Callable

from src.market_radar.models import FactorBreakdown, SectorObservation, SectorScore, SectorState


@dataclass(frozen=True)
class RankingConfig:
    scoring_version: str = "cn-v1"
    min_confidence: float = 0.4
    leading_confidence: float = 0.7
    stale_after_seconds: int = 2700


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear(value: float | None, low: float, high: float, points: float) -> float:
    if value is None:
        return 0.0
    if high == low:
        return points
    return round(_clamp((value - low) / (high - low), 0.0, 1.0) * points, 4)


def _mean_available(values: list[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return fmean(available) if available else None


def _percentile_map(
    observations: list[SectorObservation],
    value_getter: Callable[[SectorObservation], float | None],
) -> dict[str, float]:
    result: dict[str, float] = {}
    by_source: dict[str, list[tuple[str, float]]] = {}
    for item in observations:
        value = value_getter(item)
        if value is not None:
            by_source.setdefault(item.source, []).append((item.sector_id, float(value)))
    for rows in by_source.values():
        ordered = sorted(rows, key=lambda pair: (pair[1], pair[0]))
        if len(ordered) == 1:
            result[ordered[0][0]] = 0.5
            continue
        positions_by_value: dict[float, list[int]] = {}
        for position, (_, value) in enumerate(ordered):
            positions_by_value.setdefault(value, []).append(position)
        for sector_id, value in ordered:
            average_position = fmean(positions_by_value[value])
            result[sector_id] = average_position / (len(ordered) - 1)
    return result


def _factor_scores(
    item: SectorObservation,
    *,
    trend_percentiles: dict[str, float],
    relative_percentiles: dict[str, float],
    flow_percentiles: dict[str, float],
) -> FactorBreakdown:
    total = (
        (item.up_count or 0) + (item.down_count or 0) + (item.flat_count or 0)
        if item.up_count is not None and item.down_count is not None
        else 0
    )
    breadth = round((item.up_count or 0) / total * 15.0, 4) if total > 0 else 0.0
    return FactorBreakdown(
        trend_momentum=round(trend_percentiles.get(item.sector_id, 0.0) * 25.0, 4),
        relative_strength=round(relative_percentiles.get(item.sector_id, 0.0) * 20.0, 4),
        capital_flow=round(flow_percentiles.get(item.sector_id, 0.0) * 20.0, 4),
        breadth=breadth,
        liquidity_expansion=_linear(item.turnover_ratio_20d, 0.5, 1.5, 10.0),
        catalyst=round((item.catalyst_score or 0.0) * 10.0, 4),
    )


def _risk_deduction(item: SectorObservation) -> tuple[float, list[str]]:
    deduction = 0.0
    reasons: list[str] = []
    if item.volatility_ratio_20d is not None and item.volatility_ratio_20d > 1.0:
        deduction += _linear(item.volatility_ratio_20d, 1.0, 2.0, 10.0)
        reasons.append("volatility_shock")
    if item.distance_ma20_pct is not None and item.distance_ma20_pct > 8.0:
        deduction += _linear(item.distance_ma20_pct, 8.0, 20.0, 8.0)
        reasons.append("trend_overheating")
    if item.price_flow_divergence:
        deduction += 6.0
        reasons.append("price_flow_divergence")
    if item.concentration_ratio is not None and item.concentration_ratio > 0.6:
        deduction += _linear(item.concentration_ratio, 0.6, 0.9, 6.0)
        reasons.append("crowding_concentration")
    return round(min(30.0, deduction), 4), reasons


def _confidence(item: SectorObservation) -> float:
    weighted_presence = (
        (25.0 if item.return_5d_pct is not None or item.return_20d_pct is not None or item.return_1d_pct is not None else 0.0)
        + (20.0 if item.return_20d_pct is not None and item.benchmark_return_20d_pct is not None else 0.0)
        + (20.0 if any(value is not None for value in [item.capital_flow_1d, item.capital_flow_5d, item.capital_flow_20d]) else 0.0)
        + (15.0 if item.up_count is not None and item.down_count is not None else 0.0)
        + (10.0 if item.turnover_ratio_20d is not None else 0.0)
        + (10.0 if item.catalyst_score is not None else 0.0)
    )
    quality_multiplier = {"complete": 1.0, "partial": 0.8, "stale": 0.4, "unavailable": 0.0}[item.quality]
    return round(weighted_presence / 100.0 * quality_multiplier, 4)


def _state(score: float, confidence: float, config: RankingConfig, stale: bool) -> SectorState:
    if stale or confidence < config.min_confidence:
        return "insufficient_data"
    if score >= 75.0 and confidence >= config.leading_confidence:
        return "leading"
    if score >= 60.0:
        return "improving"
    if score >= 40.0:
        return "neutral"
    if score >= 25.0:
        return "weakening"
    return "avoid"


def score_sectors(
    observations: list[SectorObservation],
    config: RankingConfig,
) -> list[SectorScore]:
    trend_percentiles = _percentile_map(
        observations,
        lambda item: _mean_available([item.return_5d_pct, item.return_20d_pct, item.return_1d_pct]),
    )
    relative_percentiles = _percentile_map(
        observations,
        lambda item: (
            item.return_20d_pct - item.benchmark_return_20d_pct
            if item.return_20d_pct is not None and item.benchmark_return_20d_pct is not None
            else None
        ),
    )
    flow_percentiles = _percentile_map(
        observations,
        lambda item: _mean_available(
            [item.capital_flow_1d, item.capital_flow_5d, item.capital_flow_20d]
        ),
    )
    results: list[SectorScore] = []
    for item in observations:
        factors = _factor_scores(
            item,
            trend_percentiles=trend_percentiles,
            relative_percentiles=relative_percentiles,
            flow_percentiles=flow_percentiles,
        )
        gross = round(sum(factors.model_dump().values()), 4)
        deduction, reasons = _risk_deduction(item)
        score = round(_clamp(gross - deduction, 0.0, 100.0), 4)
        confidence = _confidence(item)
        stale = item.quality == "stale" or item.freshness_seconds > config.stale_after_seconds
        if stale:
            reasons.append("critical_price_stale")
        results.append(
            SectorScore(
                sector_id=item.sector_id,
                name=item.name,
                kind=item.kind,
                scoring_version=config.scoring_version,
                gross_score=gross,
                risk_deduction=deduction,
                score=score,
                confidence=confidence,
                state=_state(score, confidence, config, stale),
                factors=factors,
                risk_reasons=sorted(set(reasons)),
                missing_fields=sorted(set(item.missing_fields)),
                source=item.source,
                observed_at=item.observed_at,
                quality=item.quality,
                observation=item.model_dump(mode="json"),
            )
        )
    return sorted(results, key=lambda item: (-item.score, item.sector_id))
```

- [ ] **Step 4: Run ranking tests and syntax validation**

Run: `python -m pytest tests/market_radar/test_ranking.py -q`

Expected: `5 passed`.

Run: `python -m py_compile src/market_radar/ranking.py`

Expected: exit code `0`.

- [ ] **Step 5: Commit the ranking task after approval**

```bash
git add src/market_radar/ranking.py tests/market_radar/test_ranking.py
git commit -m "feat: add deterministic sector ranking"
```

---

### Task 5: Immutable Persistence and Transactional Repository

**Files:**
- Modify: `src/storage.py:1119` to add records before `DatabaseManager`
- Create: `src/market_radar/repository.py`
- Create: `tests/market_radar/test_repository.py`

**Interfaces:**
- Consumes: `SectorDefinition`, `SectorObservation`, and `RadarRunSnapshot`.
- Produces: `MarketRadarRepository.sync_universe`, `save_run`, `get_latest_run`, and `list_sector_snapshots`.

- [ ] **Step 1: Write failing repository tests**

```python
# tests/market_radar/test_repository.py
import os
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect

from src.config import Config
from src.market_radar.models import RadarRunSnapshot, SectorDefinition, SectorScore
from src.market_radar.repository import MarketRadarRepository
from src.storage import DatabaseManager


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


def test_tables_are_created(isolated_db) -> None:
    names = set(inspect(isolated_db._engine).get_table_names())
    assert {"radar_universe", "radar_runs", "radar_sector_snapshots"} <= names


def test_save_run_is_idempotent_and_atomic(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    now = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
    score = SectorScore(
        sector_id="industry:半导体",
        name="半导体",
        kind="industry",
        scoring_version="cn-v1",
        gross_score=72.0,
        risk_deduction=2.0,
        score=70.0,
        confidence=0.8,
        state="improving",
        factors={},
        risk_reasons=[],
        missing_fields=[],
        source="fixture",
        observed_at=now,
        quality="complete",
    )
    snapshot = RadarRunSnapshot(
        run_key="cn:20260721T060000Z:manual",
        market="cn",
        trigger="manual",
        as_of=now,
        quality="complete",
        scoring_version="cn-v1",
        sectors=[score],
        provider_trace=[{"source": "fixture", "result": "ok"}],
    )

    first_id = repo.save_run(snapshot)
    second_id = repo.save_run(snapshot)

    assert first_id == second_id
    latest = repo.get_latest_run("cn")
    assert latest is not None
    assert latest.run_key == snapshot.run_key
    assert [item.sector_id for item in latest.sectors] == ["industry:半导体"]


def test_sync_universe_round_trips_without_deleting_history(isolated_db) -> None:
    repo = MarketRadarRepository(isolated_db)
    first = SectorDefinition(
        sector_id="industry:半导体",
        kind="industry",
        name="半导体",
        aliases=["芯片"],
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )
    second = SectorDefinition(
        sector_id="industry:半导体",
        kind="industry",
        name="半导体产业",
        aliases=["半导体"],
        effective_from=date(2027, 1, 1),
    )

    repo.sync_universe([first])
    repo.sync_universe([second])

    assert repo.list_universe(date(2026, 7, 21)) == [first]
    assert repo.list_universe(date(2027, 7, 21)) == [second]
```

- [ ] **Step 2: Run repository tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_repository.py -q`

Expected: FAIL because the repository and SQLAlchemy records do not exist.

- [ ] **Step 3: Add the SQLAlchemy records and indexes**

Add these classes immediately before `DatabaseManager` in `src/storage.py`, using the file's existing imports for `Column`, `DateTime`, `Float`, `ForeignKey`, `Index`, `Integer`, `String`, `Text`, and `UniqueConstraint`:

```python
class RadarUniverseRecord(Base):
    __tablename__ = "radar_universe"
    __table_args__ = (
        UniqueConstraint("sector_id", "effective_from", name="uix_radar_universe_effective"),
        Index("idx_radar_universe_market_kind", "market", "kind"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_id = Column(String(160), nullable=False)
    market = Column(String(16), nullable=False, default="cn")
    kind = Column(String(32), nullable=False)
    name = Column(String(160), nullable=False)
    aliases_json = Column(Text, nullable=False, default="[]")
    benchmark_code = Column(String(64), nullable=True)
    etfs_json = Column(Text, nullable=False, default="[]")
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)
    updated_at = Column(DateTime, default=utc_naive_now, onupdate=utc_naive_now, nullable=False)


class RadarRunRecord(Base):
    __tablename__ = "radar_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_key = Column(String(160), nullable=False, unique=True, index=True)
    market = Column(String(16), nullable=False, index=True)
    trigger = Column(String(32), nullable=False)
    as_of = Column(DateTime, nullable=False, index=True)
    quality = Column(String(32), nullable=False)
    scoring_version = Column(String(32), nullable=False)
    provider_trace_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)


class RadarSectorSnapshotRecord(Base):
    __tablename__ = "radar_sector_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "sector_id", name="uix_radar_run_sector"),
        Index("idx_radar_sector_history", "sector_id", "observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("radar_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sector_id = Column(String(160), nullable=False)
    name = Column(String(160), nullable=False)
    kind = Column(String(32), nullable=False)
    score = Column(Float, nullable=False)
    gross_score = Column(Float, nullable=False)
    risk_deduction = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    state = Column(String(32), nullable=False)
    scoring_version = Column(String(32), nullable=False)
    quality = Column(String(32), nullable=False)
    source = Column(String(128), nullable=False)
    observed_at = Column(DateTime, nullable=False)
    factors_json = Column(Text, nullable=False, default="{}")
    risk_reasons_json = Column(Text, nullable=False, default="[]")
    missing_fields_json = Column(Text, nullable=False, default="[]")
    observation_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)
```

- [ ] **Step 4: Implement the transactional repository**

Create the complete repository:

```python
# src/market_radar/repository.py
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
        with self.db.session_scope() as session:
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
                    "etfs_json": _dump([item.model_dump(mode="json") for item in sector.etfs]),
                    "effective_to": sector.effective_to,
                }
                if row is None:
                    session.add(RadarUniverseRecord(
                        sector_id=sector.sector_id,
                        effective_from=sector.effective_from,
                        **fields,
                    ))
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
                .order_by(RadarUniverseRecord.kind, RadarUniverseRecord.sector_id)
            ).scalars().all()
            return [
                SectorDefinition(
                    sector_id=row.sector_id,
                    market=row.market,
                    kind=row.kind,
                    name=row.name,
                    aliases=json.loads(row.aliases_json or "[]"),
                    benchmark_code=row.benchmark_code,
                    etfs=[EtfDefinition.model_validate(item) for item in json.loads(row.etfs_json or "[]")],
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                )
                for row in rows
            ]

    def save_run(self, snapshot: RadarRunSnapshot) -> int:
        with self.db.session_scope() as session:
            existing_id = session.execute(
                select(RadarRunRecord.id).where(RadarRunRecord.run_key == snapshot.run_key)
            ).scalar_one_or_none()
            if existing_id is not None:
                return int(existing_id)
            run = RadarRunRecord(
                run_key=snapshot.run_key,
                market=snapshot.market,
                trigger=snapshot.trigger,
                as_of=to_utc_naive_datetime(snapshot.as_of),
                quality=snapshot.quality,
                scoring_version=snapshot.scoring_version,
                provider_trace_json=_dump(snapshot.provider_trace),
            )
            session.add(run)
            session.flush()
            for sector in snapshot.sectors:
                factors = (
                    sector.factors.model_dump(mode="json")
                    if hasattr(sector.factors, "model_dump")
                    else dict(sector.factors)
                )
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
                        factors_json=_dump(factors),
                        risk_reasons_json=_dump(sector.risk_reasons),
                        missing_fields_json=_dump(sector.missing_fields),
                        observation_json=_dump(sector.observation),
                    )
                )
            return int(run.id)

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
    def _list_sector_snapshots_in_session(session: Any, run_id: int) -> list[SectorScore]:
        rows = session.execute(
            select(RadarSectorSnapshotRecord)
            .where(RadarSectorSnapshotRecord.run_id == run_id)
            .order_by(desc(RadarSectorSnapshotRecord.score), RadarSectorSnapshotRecord.sector_id)
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
```

- [ ] **Step 5: Run repository tests**

Run: `python -m pytest tests/market_radar/test_repository.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Run storage regression tests**

Run: `python -m pytest tests/test_storage.py tests/test_decision_signal_repo.py -q`

Expected: all selected tests PASS.

- [ ] **Step 7: Commit persistence after approval**

```bash
git add src/storage.py src/market_radar/repository.py tests/market_radar/test_repository.py
git commit -m "feat: persist Market Radar snapshots"
```

---

### Task 6: One-Run Orchestration

**Files:**
- Create: `src/market_radar/service.py`
- Create: `tests/market_radar/test_service.py`

**Interfaces:**
- Consumes: `UniverseLoader`, `MarketRadarProvider`, `score_sectors`, and `MarketRadarRepository`.
- Produces: `MarketRadarService.run(market="cn", as_of=None, trigger="manual", persist=True) -> RadarRunSnapshot`.

- [ ] **Step 1: Write failing service tests**

```python
# tests/market_radar/test_service.py
from datetime import date, datetime, timezone

from src.market_radar.models import SectorDefinition, SectorObservation
from src.market_radar.providers import ProviderBatch
from src.market_radar.ranking import RankingConfig
from src.market_radar.service import MarketRadarService


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


class FakeUniverse:
    def load(self, as_of: date):
        return [
            SectorDefinition(
                sector_id="industry:半导体",
                kind="industry",
                name="半导体",
                effective_from=date(2026, 1, 1),
            )
        ]


class FakeProvider:
    def fetch(self, market, as_of, universe):
        return ProviderBatch(
            observations=[
                SectorObservation(
                    sector_id="industry:半导体",
                    kind="industry",
                    name="半导体",
                    observed_at=as_of,
                    source="fixture",
                    freshness_seconds=0,
                    quality="partial",
                    return_1d_pct=2.0,
                    missing_fields=["return_20d_pct"],
                )
            ],
            trace=[{"source": "fixture", "result": "ok"}],
        )


class FakeRepository:
    def __init__(self):
        self.universe = None
        self.snapshot = None

    def sync_universe(self, sectors):
        self.universe = sectors

    def save_run(self, snapshot):
        self.snapshot = snapshot
        return 7


def test_run_builds_stable_key_scores_and_persists() -> None:
    repo = FakeRepository()
    service = MarketRadarService(
        universe_loader=FakeUniverse(),
        provider=FakeProvider(),
        repository=repo,
        ranking_config=RankingConfig(),
        clock=lambda: NOW,
    )

    snapshot = service.run()

    assert snapshot.run_key == "cn:20260721T060000Z:manual"
    assert snapshot.quality == "partial"
    assert repo.snapshot == snapshot
    assert repo.universe[0].name == "半导体"


def test_run_without_persistence_does_not_write() -> None:
    repo = FakeRepository()
    service = MarketRadarService(
        universe_loader=FakeUniverse(),
        provider=FakeProvider(),
        repository=repo,
        ranking_config=RankingConfig(),
        clock=lambda: NOW,
    )

    service.run(persist=False)

    assert repo.snapshot is None
```

- [ ] **Step 2: Run service tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_service.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.market_radar.service'`.

- [ ] **Step 3: Implement one-run orchestration**

```python
# src/market_radar/service.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal

from src.market_radar.models import DataQuality, RadarRunSnapshot
from src.market_radar.providers import MarketRadarProvider
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.universe import UniverseLoader


class MarketRadarService:
    def __init__(
        self,
        *,
        universe_loader: UniverseLoader,
        provider: MarketRadarProvider,
        repository: MarketRadarRepository,
        ranking_config: RankingConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.universe_loader = universe_loader
        self.provider = provider
        self.repository = repository
        self.ranking_config = ranking_config
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        market: str = "cn",
        as_of: datetime | None = None,
        trigger: Literal["manual", "replay"] = "manual",
        persist: bool = True,
    ) -> RadarRunSnapshot:
        if market != "cn":
            raise ValueError("Market Radar Phase 1 supports market=cn only")
        effective_as_of = as_of or self.clock()
        if effective_as_of.tzinfo is None or effective_as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        universe = self.universe_loader.load(effective_as_of.date())
        batch = self.provider.fetch(market, effective_as_of, universe)
        sectors = score_sectors(batch.observations, self.ranking_config)
        quality = self._run_quality([item.quality for item in batch.observations])
        snapshot = RadarRunSnapshot(
            run_key=f"{market}:{effective_as_of.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}:{trigger}",
            market="cn",
            trigger=trigger,
            as_of=effective_as_of,
            quality=quality,
            scoring_version=self.ranking_config.scoring_version,
            sectors=sectors,
            provider_trace=batch.trace,
        )
        if persist:
            combined_universe = {item.sector_id: item for item in batch.discovered_sectors}
            combined_universe.update({item.sector_id: item for item in universe})
            self.repository.sync_universe(
                sorted(combined_universe.values(), key=lambda item: (item.kind, item.sector_id))
            )
            self.repository.save_run(snapshot)
        return snapshot

    @staticmethod
    def _run_quality(values: list[DataQuality]) -> DataQuality:
        if not values or all(value == "unavailable" for value in values):
            return "unavailable"
        if any(value == "stale" for value in values):
            return "stale"
        if any(value in {"partial", "unavailable"} for value in values):
            return "partial"
        return "complete"
```

- [ ] **Step 4: Run service tests and relevant unit tests**

Run: `python -m pytest tests/market_radar/test_service.py tests/market_radar/test_ranking.py -q`

Expected: `7 passed`.

- [ ] **Step 5: Commit orchestration after approval**

```bash
git add src/market_radar/service.py tests/market_radar/test_service.py
git commit -m "feat: orchestrate Market Radar runs"
```

---

### Task 7: Point-in-Time Replay Foundation

**Files:**
- Create: `src/market_radar/replay.py`
- Create: `tests/market_radar/test_replay.py`

**Interfaces:**
- Consumes: chronological `(as_of, observations)` frames and `RankingConfig`.
- Produces: `MarketRadarReplayEngine.replay(frames) -> list[RadarRunSnapshot]`.

- [ ] **Step 1: Write failing replay tests**

```python
# tests/market_radar/test_replay.py
from datetime import datetime, timedelta, timezone

import pytest

from src.market_radar.models import SectorObservation
from src.market_radar.ranking import RankingConfig
from src.market_radar.replay import MarketRadarReplayEngine, ReplayFrame


START = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)


def obs(observed_at: datetime, return_1d: float) -> SectorObservation:
    return SectorObservation(
        sector_id="industry:半导体",
        kind="industry",
        name="半导体",
        observed_at=observed_at,
        source="fixture",
        freshness_seconds=0,
        quality="partial",
        return_1d_pct=return_1d,
        missing_fields=["return_20d_pct"],
    )


def test_replay_is_chronological_and_uses_replay_trigger() -> None:
    frames = [
        ReplayFrame(as_of=START, observations=[obs(START, 1.0)]),
        ReplayFrame(as_of=START + timedelta(days=1), observations=[obs(START + timedelta(days=1), 2.0)]),
    ]

    snapshots = MarketRadarReplayEngine(RankingConfig()).replay(frames)

    assert [item.as_of for item in snapshots] == [frame.as_of for frame in frames]
    assert all(item.trigger == "replay" for item in snapshots)


def test_replay_rejects_future_observation() -> None:
    frame = ReplayFrame(
        as_of=START,
        observations=[obs(START + timedelta(minutes=1), 1.0)],
    )

    with pytest.raises(ValueError, match="future observation"):
        MarketRadarReplayEngine(RankingConfig()).replay([frame])


def test_replay_rejects_out_of_order_frames() -> None:
    frames = [
        ReplayFrame(as_of=START + timedelta(days=1), observations=[]),
        ReplayFrame(as_of=START, observations=[]),
    ]

    with pytest.raises(ValueError, match="chronological order"):
        MarketRadarReplayEngine(RankingConfig()).replay(frames)
```

- [ ] **Step 2: Run replay tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_replay.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.market_radar.replay'`.

- [ ] **Step 3: Implement replay guards and snapshots**

```python
# src/market_radar/replay.py
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from src.market_radar.models import RadarRunSnapshot, SectorObservation
from src.market_radar.ranking import RankingConfig, score_sectors


class ReplayFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    as_of: datetime
    observations: list[SectorObservation]


class MarketRadarReplayEngine:
    def __init__(self, ranking_config: RankingConfig) -> None:
        self.ranking_config = ranking_config

    def replay(self, frames: list[ReplayFrame]) -> list[RadarRunSnapshot]:
        snapshots: list[RadarRunSnapshot] = []
        previous: datetime | None = None
        for frame in frames:
            if frame.as_of.tzinfo is None or frame.as_of.utcoffset() is None:
                raise ValueError("replay as_of must be timezone-aware")
            if previous is not None and frame.as_of < previous:
                raise ValueError("replay frames must be in chronological order")
            for item in frame.observations:
                if item.observed_at > frame.as_of:
                    raise ValueError("future observation is not allowed in replay")
            sectors = score_sectors(frame.observations, self.ranking_config)
            quality = "partial" if frame.observations else "unavailable"
            if frame.observations and all(item.quality == "complete" for item in frame.observations):
                quality = "complete"
            snapshots.append(
                RadarRunSnapshot(
                    run_key=f"cn:{frame.as_of.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}:replay",
                    market="cn",
                    trigger="replay",
                    as_of=frame.as_of,
                    quality=quality,
                    scoring_version=self.ranking_config.scoring_version,
                    sectors=sectors,
                    provider_trace=[{"source": "replay_frame", "result": "ok"}],
                )
            )
            previous = frame.as_of
        return snapshots
```

- [ ] **Step 4: Run replay and ranking tests**

Run: `python -m pytest tests/market_radar/test_replay.py tests/market_radar/test_ranking.py -q`

Expected: `8 passed`.

- [ ] **Step 5: Commit replay foundation after approval**

```bash
git add src/market_radar/replay.py tests/market_radar/test_replay.py
git commit -m "feat: add point-in-time radar replay"
```

---

### Task 8: Configuration, Manual CLI, and Documentation

**Files:**
- Modify: `src/config.py:1031` and environment loading near `src/config.py:1982`
- Modify: `.env.example:785`
- Create: `scripts/run_market_radar.py`
- Create: `tests/test_run_market_radar.py`
- Create: `docs/market-radar.md`
- Modify: `docs/INDEX.md`
- Modify: `docs/CHANGELOG.md:9`

**Interfaces:**
- Consumes: all Phase 1 services.
- Produces: `python scripts/run_market_radar.py --market cn [--persist] [--output PATH]`.

- [ ] **Step 1: Write failing configuration and CLI tests**

```python
# tests/test_run_market_radar.py
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import run_market_radar
from src.config import Config


def test_config_reads_market_radar_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_PROVIDER_LIMIT", raising=False)
    monkeypatch.delenv("MARKET_RADAR_STALE_AFTER_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_RADAR_SCORING_VERSION", raising=False)
    config = Config._load_from_env()
    assert config.market_radar_provider_limit == 1000
    assert config.market_radar_stale_after_seconds == 2700
    assert config.market_radar_scoring_version == "cn-v1"


def test_cli_writes_structured_json_without_persistence(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "radar.json"
    snapshot = SimpleNamespace(
        model_dump_json=lambda indent: json.dumps(
            {"market": "cn", "quality": "partial", "sectors": []},
            ensure_ascii=False,
            indent=indent,
        )
    )

    class FakeService:
        def run(self, **kwargs):
            assert kwargs == {"market": "cn", "persist": False}
            return snapshot

    monkeypatch.setattr(run_market_radar, "build_service", lambda: FakeService())

    exit_code = run_market_radar.main(["--market", "cn", "--output", str(output)])

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["market"] == "cn"


def test_cli_rejects_non_cn_market() -> None:
    assert run_market_radar.main(["--market", "hk"]) == 2
```

- [ ] **Step 2: Run CLI tests and confirm failure**

Run: `python -m pytest tests/test_run_market_radar.py -q`

Expected: FAIL because the config fields and script do not exist.

- [ ] **Step 3: Add optional configuration fields**

Add these dataclass fields next to the existing market-review scheduling fields in `Config`:

```python
market_radar_provider_limit: int = 1000
market_radar_stale_after_seconds: int = 2700
market_radar_scoring_version: str = "cn-v1"
```

Add these environment mappings to `Config._load_from_env()`:

```python
market_radar_provider_limit=parse_env_int(
    os.getenv("MARKET_RADAR_PROVIDER_LIMIT"),
    1000,
    field_name="MARKET_RADAR_PROVIDER_LIMIT",
    minimum=10,
    maximum=5000,
),
market_radar_stale_after_seconds=parse_env_int(
    os.getenv("MARKET_RADAR_STALE_AFTER_SECONDS"),
    2700,
    field_name="MARKET_RADAR_STALE_AFTER_SECONDS",
    minimum=60,
    maximum=86400,
),
market_radar_scoring_version=(
    os.getenv("MARKET_RADAR_SCORING_VERSION", "cn-v1").strip() or "cn-v1"
),
```

- [ ] **Step 4: Implement the manual CLI**

```python
# scripts/run_market_radar.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_provider import DataFetcherManager
from src.config import get_config
from src.market_radar.providers import LegacyRankingProvider
from src.market_radar.ranking import RankingConfig
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.service import MarketRadarService
from src.market_radar.universe import UniverseLoader


def build_service() -> MarketRadarService:
    config = get_config()
    return MarketRadarService(
        universe_loader=UniverseLoader(ROOT / "src/data/market_radar/a_share_etfs.yaml"),
        provider=LegacyRankingProvider(
            DataFetcherManager(),
            limit=config.market_radar_provider_limit,
        ),
        repository=MarketRadarRepository(),
        ranking_config=RankingConfig(
            scoring_version=config.market_radar_scoring_version,
            stale_after_seconds=config.market_radar_stale_after_seconds,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one A-share Market Radar snapshot")
    parser.add_argument("--market", default="cn")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.market != "cn":
        print("Market Radar Phase 1 supports --market cn only", file=sys.stderr)
        return 2
    try:
        snapshot = build_service().run(market="cn", persist=args.persist)
        rendered = snapshot.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return 0
    except Exception as exc:
        print(f"Market Radar failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Document environment variables**

Add this block after market-review settings in `.env.example`:

```dotenv
# Market Radar Phase 1: manual A-share snapshots only; enable/interval settings arrive with scheduler integration.
MARKET_RADAR_PROVIDER_LIMIT=1000
MARKET_RADAR_STALE_AFTER_SECONDS=2700
MARKET_RADAR_SCORING_VERSION=cn-v1
```

- [ ] **Step 6: Write focused Phase 1 documentation**

Create `docs/market-radar.md` with these sections and exact claims:

````markdown
# Market Radar

Market Radar Phase 1 provides a manual A-share sector snapshot foundation. It is deterministic, records provider provenance, and does not call an LLM.

## Run

```bash
python scripts/run_market_radar.py --market cn
python scripts/run_market_radar.py --market cn --persist
python scripts/run_market_radar.py --market cn --output reports/market-radar.json
```

## Current Boundary

- A shares only
- manual runs only
- industry/concept ranking fallback through existing providers
- immutable run and sector snapshots when `--persist` is used
- partial confidence when only daily ranking fields are available
- no Web page, API, scheduling, alerts, position policy, outcome evaluation, Hong Kong data, order execution, or personalized advice

## Data Quality

Each snapshot exposes source, observation time, freshness, quality, confidence, missing fields, and scoring version. Missing fields reduce confidence. Stale critical price evidence cannot produce an upgraded state.

## Universe

The curated ETF seed is stored at `src/data/market_radar/a_share_etfs.yaml`. Code/name pairs must be checked against an exchange or fund-manager source before release.
````

Add this exact row immediately after the `实时告警中心` row in the `使用专题` table of `docs/INDEX.md`:

```markdown
| [Market Radar](market-radar.md) | A 股板块/ETF 雷达第一阶段契约、手动运行、数据质量、持久化和当前能力边界 |
```

Append one flat line under `[Unreleased]` in `docs/CHANGELOG.md`:

```markdown
- [新功能] 新增 A 股 Market Radar 第一阶段基础能力，包括板块/ETF 契约、可追溯数据质量、确定性评分、快照持久化、离线回放基础与手动 CLI；暂不包含 Web、API、调度、告警、仓位策略和港股。
```

- [ ] **Step 7: Run CLI, config, and documentation-facing tests**

Run: `python -m pytest tests/test_run_market_radar.py tests/test_config_env_compat.py tests/test_config_validate_structured.py -q`

Expected: all selected tests PASS.

Run: `python -m py_compile scripts/run_market_radar.py src/config.py`

Expected: exit code `0`.

- [ ] **Step 8: Run an offline CLI smoke with providers mocked by the test suite**

Run: `python -m pytest tests/test_run_market_radar.py::test_cli_writes_structured_json_without_persistence -q`

Expected: `1 passed`.

- [ ] **Step 9: Commit CLI and docs after approval**

```bash
git add .env.example src/config.py scripts/run_market_radar.py tests/test_run_market_radar.py docs/market-radar.md docs/INDEX.md docs/CHANGELOG.md
git commit -m "feat: expose Market Radar Phase 1 CLI"
```

---

### Task 9: Phase 1 Integration Verification

**Files:**
- No planned file changes. A failure returns execution to the task that introduced it; do not accumulate miscellaneous verification patches.

**Interfaces:**
- Consumes: complete Phase 1 implementation.
- Produces: reviewable verification evidence and a clean worktree.

- [ ] **Step 1: Run all Market Radar tests**

Run: `python -m pytest tests/market_radar tests/test_run_market_radar.py -q`

Expected: all tests PASS.

- [ ] **Step 2: Run storage, configuration, and provider regressions**

Run: `python -m pytest tests/test_storage.py tests/test_decision_signal_repo.py tests/test_config_env_compat.py tests/test_config_validate_structured.py tests/test_tickflow_manager_routing.py -q`

Expected: all selected tests PASS.

- [ ] **Step 3: Run repository backend gate**

Run on Git Bash/WSL: `./scripts/ci_gate.sh`

Expected: exit code `0`. If the Windows environment cannot execute the shell gate, run `python -m pytest -m "not network"` and record the unexecuted shell gate as a verification gap.

- [ ] **Step 4: Run static checks on changed Python files**

Run:

```bash
python -m py_compile src/market_radar/__init__.py src/market_radar/models.py src/market_radar/universe.py src/market_radar/providers.py src/market_radar/ranking.py src/market_radar/repository.py src/market_radar/service.py src/market_radar/replay.py scripts/run_market_radar.py
```

Expected: exit code `0` and no output.

Run: `git diff --check`

Expected: exit code `0` and no output.

- [ ] **Step 5: Review Phase 1 boundaries**

Run:

```bash
git diff --name-only upstream/main..HEAD
rg -n "LLM|place_order|submit_order|APIRouter|schedule\.every|MARKET_RADAR" src/market_radar scripts/run_market_radar.py
```

Expected:

- changed files match the file structure in this plan;
- no LLM, broker-order, FastAPI route, or scheduler integration exists in `src/market_radar`;
- `MARKET_RADAR_*` appears only in configuration, CLI construction, tests, and docs.

- [ ] **Step 6: Produce handoff evidence**

Record in the final handoff:

- implementation summary;
- exact test commands and pass counts;
- any unexecuted network/live-provider checks;
- ETF mapping verification source and date;
- risks: legacy provider coverage is partial and cannot yet produce high-confidence multi-horizon signals;
- rollback: revert the Phase 1 commits; the new tables are additive and can remain unused.

---

## Deferred Plans

The approved design requires separate implementation plans after Phase 1 is accepted:

1. A-share full-observation enrichment for multi-horizon returns, benchmark strength, capital flow, breadth, and liquidity; then ETF selection, market regime, and generic position policy.
2. Market Radar API, monitoring cockpit, and manual-run administration.
3. Thirty-minute scheduler, lifecycle hysteresis, alert deduplication, and end-of-day reports.
4. Twenty-trading-day signal outcomes, baseline comparison, and calibration workflow.
5. Hong Kong universe, ETFs, calendars, southbound-flow evidence, and A/H mappings.
6. Constrained LLM narrative enrichment after deterministic outputs are stable.
