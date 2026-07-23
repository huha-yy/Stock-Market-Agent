# Market Radar Phase 2C Read API Design

**Status:** Approved design; written specification awaiting review

**Date:** 2026-07-23

**Base:** Market Radar Phase 2B at `5d5721fb`

## 1. Purpose

Phase 2C exposes the latest persisted A-share Market Radar result through a small, read-only FastAPI contract. It establishes the backend boundary required by the later Web monitoring cockpit without changing collection, scoring, ETF selection, regime assessment, or position policy.

## 2. Scope

### Included

- authenticated `GET /api/v1/market-radar/latest`;
- authenticated `GET /api/v1/market-radar/sectors`;
- authenticated `GET /api/v1/market-radar/sectors/{sector_id}`;
- explicit Pydantic response schemas and stable API error responses;
- focused endpoint tests, OpenAPI coverage, user documentation, and changelog entry.

### Excluded

- running or scheduling Market Radar scans;
- Web and Desktop user interfaces;
- historical-run pagination, signals, alerts, notifications, and reports;
- lifecycle hysteresis, outcomes, calibration, and Hong Kong support;
- changes to persisted Phase 1, 2A, or 2B contracts.

## 3. Architecture And Ownership

The new endpoint module lives in `api/v1/endpoints/market_radar.py`, its response models live in `api/v1/schemas/market_radar.py`, and the existing v1 router mounts it at `/market-radar`. The router uses the existing `AdminSessionCookie` security dependency so the global optional-admin-auth middleware and generated OpenAPI remain consistent with protected API modules.

All three endpoints instantiate `MarketRadarRepository` and read `get_latest_run("cn")`. The repository remains the only database boundary and reconstructs the immutable `RadarRunSnapshot`. The API performs only presentation-oriented selection and aggregation from that snapshot. It must not call providers, invoke `MarketRadarService.run`, persist data, recompute scores, or introduce a parallel storage query.

## 4. HTTP Contracts

All timestamps use ISO 8601 with an explicit offset. Existing domain names, units, enum values, ordering, reason codes, and confidence semantics are preserved. Response schemas are additive wrappers around the Phase 2B snapshot and use typed nested Market Radar domain models rather than untyped dictionaries.

### 4.1 `GET /latest`

Returns one `MarketRadarLatestResponse`:

- `available`: whether a persisted A-share run exists;
- `run`: the complete latest `RadarRunSnapshot` when available, otherwise `null`.

No stored run is a normal bootstrap state and returns HTTP 200 with `available=false` and `run=null`. This lets a cockpit distinguish "not run yet" from API or storage failure without treating an empty installation as an error.

### 4.2 `GET /sectors`

Returns one `MarketRadarSectorListResponse`:

- `available`;
- `run_key` and `as_of`, nullable only when unavailable;
- `items`, containing the latest run's sectors in their persisted rank order;
- `total`, equal to the item count.

Each item contains `rank` (one-based persisted order) and the complete typed `SectorScore`. No query filters or pagination are added in this slice because the Phase 2B universe is bounded and the cockpit needs the full ranking.

When no run exists, the endpoint returns HTTP 200 with `available=false`, null run metadata, `items=[]`, and `total=0`.

### 4.3 `GET /sectors/{sector_id}`

Returns one `MarketRadarSectorDetailResponse` assembled from a single latest snapshot:

- `run_key`, `as_of`, and one-based `rank`;
- the complete typed `SectorScore`;
- every ETF selection whose `sector_id` matches, in persisted order;
- the matching position suggestion when present, otherwise `null`;
- the run-level regime assessment and position plan, both nullable for legacy snapshots.

If no run exists, it returns HTTP 404 with `error="market_radar_run_not_found"`. If a run exists but the exact canonical `sector_id` does not, it returns HTTP 404 with `error="market_radar_sector_not_found"`. The path is an identifier, not an alias or display-name search.

## 5. Failure Handling

Repository and deserialization failures are logged with stack context and mapped to HTTP 500 with the stable public payload `error="market_radar_read_failed"` and a non-sensitive message. Internal exception text, database paths, SQL, provider traces outside the normal typed response, and credentials are not copied into the error body.

FastAPI validation remains responsible for malformed paths. Authentication failures continue to be handled by the existing middleware. The endpoints do not catch `HTTPException` as a generic storage failure.

## 6. Compatibility And Security

The change is additive under `/api/v1`; no existing endpoint, schema, database table, configuration, CLI behavior, or snapshot byte representation changes. Legacy sector-only snapshots remain readable: ETF items are empty and regime and position plan are null. The API remains A-share-only and does not accept a market parameter that would imply unsupported coverage.

Every route declares the existing admin session cookie security scheme and documents HTTP 401. Authentication-disabled local deployments retain the repository's existing open-access behavior through the global middleware.

## 7. Testing And Verification

Focused API tests use an isolated database or a patched repository boundary and cover:

1. `/latest` with a complete Phase 2B snapshot;
2. all list fields, rank ordering, and empty bootstrap state;
3. sector detail aggregation for ETF selections and a position suggestion;
4. legacy nullable Phase 2B fields;
5. missing run and unknown sector 404 error codes;
6. repository failure redaction and stable 500 payload;
7. route registration, OpenAPI response models, and admin-cookie security metadata;
8. authentication enforcement through the existing application middleware.

Verification runs the focused API tests first, then Python compilation for changed modules, followed by `./scripts/ci_gate.sh` when time permits before the midnight cutoff. Documentation updates describe the three routes in `docs/market-radar.md`; the English index is updated only if its current Market Radar entry needs a phase-status correction. `docs/CHANGELOG.md` receives one flat `[Unreleased]` entry.

## 8. Rollback

Rollback removes the new endpoint and schema modules, their v1 router registration, focused tests, and documentation entries. Because the feature performs no writes and changes no persisted schema or existing API contract, rollback requires no data migration.
