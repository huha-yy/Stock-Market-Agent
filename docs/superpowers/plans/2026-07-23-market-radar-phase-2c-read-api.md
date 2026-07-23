# Market Radar Phase 2C Read API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the latest persisted A-share Market Radar snapshot through three authenticated, read-only FastAPI endpoints.

**Architecture:** A focused API schema module wraps the existing immutable Phase 2B domain models. A focused endpoint module reads `MarketRadarRepository.get_latest_run("cn")` once per request and performs presentation-only aggregation; the v1 router supplies the `/market-radar` prefix.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, pytest, FastAPI `TestClient`

## Global Constraints

- Do not call providers, run scans, recompute scores, or write storage from these endpoints.
- Preserve persisted ordering, domain enum values, reason codes, percentages, confidence values, and timezone-aware timestamps.
- No new dependency, configuration item, environment variable, database migration, Web change, or Desktop change.
- Use stable public error codes and never expose internal exception text.
- Keep the API A-share-only with the fixed repository lookup `get_latest_run("cn")`.
- Stop starting new steps at `2026-07-24 00:00 Asia/Shanghai`.

---

### Task 1: Lock And Implement The Read API Contract

**Files:**
- Create: `tests/test_market_radar_api.py`
- Create: `api/v1/schemas/market_radar.py`
- Create: `api/v1/endpoints/market_radar.py`
- Modify: `api/v1/router.py`

**Interfaces:**
- Consumes: `MarketRadarRepository.get_latest_run(market: str) -> RadarRunSnapshot | None`
- Produces: `GET /api/v1/market-radar/latest`, `GET /api/v1/market-radar/sectors`, and `GET /api/v1/market-radar/sectors/{sector_id}`
- Produces schemas: `MarketRadarLatestResponse`, `MarketRadarSectorListItem`, `MarketRadarSectorListResponse`, and `MarketRadarSectorDetailResponse`

- [ ] **Step 1: Write failing endpoint tests**

Create helpers that build one complete `RadarRunSnapshot` from the existing domain models, patch `api.v1.endpoints.market_radar.MarketRadarRepository`, and assert these exact behaviors:

```python
def test_latest_returns_complete_snapshot(client, snapshot, repository):
    repository.return_value.get_latest_run.return_value = snapshot
    response = client.get("/api/v1/market-radar/latest")
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["run"]["run_key"] == snapshot.run_key

def test_sector_list_preserves_rank_order(client, snapshot, repository):
    repository.return_value.get_latest_run.return_value = snapshot
    payload = client.get("/api/v1/market-radar/sectors").json()
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert [item["sector"]["sector_id"] for item in payload["items"]] == [
        sector.sector_id for sector in snapshot.sectors
    ]

def test_sector_detail_aggregates_same_snapshot(client, snapshot, repository):
    repository.return_value.get_latest_run.return_value = snapshot
    payload = client.get(
        f"/api/v1/market-radar/sectors/{snapshot.sectors[0].sector_id}"
    ).json()
    assert payload["rank"] == 1
    assert all(item["sector_id"] == snapshot.sectors[0].sector_id for item in payload["etfs"])
    assert payload["position_suggestion"]["sector_id"] == snapshot.sectors[0].sector_id

def test_empty_bootstrap_and_not_found_contracts(client, repository):
    repository.return_value.get_latest_run.return_value = None
    assert client.get("/api/v1/market-radar/latest").json() == {
        "available": False, "run": None
    }
    assert client.get("/api/v1/market-radar/sectors").json()["items"] == []
    response = client.get("/api/v1/market-radar/sectors/cn-missing")
    assert response.status_code == 404
    assert response.json()["error"] == "market_radar_run_not_found"

def test_storage_errors_are_redacted(client, repository):
    repository.return_value.get_latest_run.side_effect = RuntimeError("secret path")
    response = client.get("/api/v1/market-radar/latest")
    assert response.status_code == 500
    assert response.json() == {
        "error": "market_radar_read_failed",
        "message": "Unable to read Market Radar data",
    }
    assert "secret path" not in response.text
```

Also assert unknown-sector 404, legacy null Phase 2B fields, auth-enabled 401, and OpenAPI `AdminSessionCookie` security metadata.

- [ ] **Step 2: Run tests and confirm the red state**

Run: `python -m pytest tests/test_market_radar_api.py -q`

Expected: collection or request failures because the schema, endpoint, and router registration do not exist.

- [ ] **Step 3: Add typed response schemas**

Implement the schema module with these exact public fields:

```python
class MarketRadarLatestResponse(BaseModel):
    available: bool
    run: RadarRunSnapshot | None = None

class MarketRadarSectorListItem(BaseModel):
    rank: int = Field(ge=1)
    sector: SectorScore

class MarketRadarSectorListResponse(BaseModel):
    available: bool
    run_key: str | None = None
    as_of: datetime | None = None
    items: list[MarketRadarSectorListItem] = Field(default_factory=list)
    total: int = Field(ge=0)

class MarketRadarSectorDetailResponse(BaseModel):
    run_key: str
    as_of: datetime
    rank: int = Field(ge=1)
    sector: SectorScore
    etfs: list[EtfSelection] = Field(default_factory=list)
    position_suggestion: PositionSuggestion | None = None
    regime: MarketRegimeAssessment | None = None
    position_plan: PositionPlan | None = None
```

- [ ] **Step 4: Add presentation-only endpoints and router registration**

Use `APIRouter(dependencies=[Security(admin_session_cookie)])`. Centralize the repository read so every handler gets the same stable failure mapping:

```python
def _latest_snapshot() -> RadarRunSnapshot | None:
    try:
        return MarketRadarRepository().get_latest_run("cn")
    except Exception as exc:
        logger.error("Read Market Radar snapshot failed: %s", exc, exc_info=True)
        raise api_error(
            500,
            "market_radar_read_failed",
            "Unable to read Market Radar data",
        ) from exc
```

Build list ranks with `enumerate(snapshot.sectors, start=1)`. Build detail ETF items and the position suggestion only by matching the canonical `sector_id`. Raise `api_error(404, ...)` before generic exception handling. Register the module in `api/v1/router.py` with prefix `/market-radar` and tag `MarketRadar`.

- [ ] **Step 5: Run focused tests and compile changed modules**

Run:

```bash
python -m pytest tests/test_market_radar_api.py -q
python -m py_compile api/v1/schemas/market_radar.py api/v1/endpoints/market_radar.py api/v1/router.py tests/test_market_radar_api.py
```

Expected: all tests pass and compilation exits 0.

- [ ] **Step 6: Commit the API slice**

```bash
git add api/v1/schemas/market_radar.py api/v1/endpoints/market_radar.py api/v1/router.py tests/test_market_radar_api.py
git commit -m "feat: add Market Radar read API"
```

### Task 2: Document And Verify The Public Slice

**Files:**
- Modify: `docs/market-radar.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `tests/market_radar/test_integration.py` only if its existing documentation contract requires a Phase 2C assertion

**Interfaces:**
- Consumes: the three Task 1 endpoints and their bootstrap/error semantics
- Produces: user-facing API usage and release history consistent with the implemented contract

- [ ] **Step 1: Add a failing documentation contract assertion if required**

When the existing integration suite already enforces Market Radar docs, extend its assertion to require all three literal route paths:

```python
for route in (
    "/api/v1/market-radar/latest",
    "/api/v1/market-radar/sectors",
    "/api/v1/market-radar/sectors/{sector_id}",
):
    assert route in text
```

Run: `python -m pytest tests/market_radar/test_integration.py -q`

Expected: the new assertion fails until the docs are updated. If the existing suite has no documentation contract suitable for extension, keep documentation verification manual and do not add a parallel test file.

- [ ] **Step 2: Update the focused documentation and flat changelog**

Add a `Read-only API` section to `docs/market-radar.md` that lists authentication, all three routes, empty bootstrap semantics, exact 404 cases, A-share-only scope, and the fact that reads never run a scan. Append exactly one flat `[Unreleased]` line:

```text
- [新功能] Market Radar 新增最新快照、板块排名和板块详情只读 API，为 Web 监控驾驶舱提供稳定数据契约。
```

Do not expand `README.md`; the behavior belongs in the Market Radar topic document.

- [ ] **Step 3: Run focused regression and repository gate**

Run:

```bash
python -m pytest tests/test_market_radar_api.py tests/market_radar/test_integration.py -q
./scripts/ci_gate.sh
```

Expected: focused tests pass and the backend gate exits 0. If the gate cannot finish before midnight, record the last completed command and do not claim the gate passed.

- [ ] **Step 4: Review diff and commit documentation**

Run: `git diff --check && git status --short`

Expected: no whitespace errors and only planned files are modified.

```bash
git add docs/market-radar.md docs/CHANGELOG.md tests/market_radar/test_integration.py
git commit -m "docs: document Market Radar read API"
```

- [ ] **Step 5: Final branch verification and remote handoff**

Run focused tests once more if any code changed after Task 1. Then push `codex/market-radar-phase-2c-api` and create one ready PR with scope, verification, compatibility, risk, and rollback sections. No UI screenshot is required because this slice changes no report rendering or Web UI.
