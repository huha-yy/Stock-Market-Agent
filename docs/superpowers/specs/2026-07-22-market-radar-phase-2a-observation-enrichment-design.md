# Market Radar Phase 2A Observation Enrichment Design

## 1. Goal

Market Radar Phase 2A enriches the Phase 1 A-share sector observations with deterministic multi-horizon market evidence. It adds reliable market-data capabilities, candidate budgeting, formula-driven observation construction, and immutable constituent-set evidence without adding ETF selection, market-regime classification, position policy, API/Web surfaces, scheduling, alerts, outcome evaluation, Hong Kong support, order execution, or LLM/news scoring.

Phase 2A must remain useful without new paid credentials. Public providers supply the default path; configured Tushare or TickFlow capabilities may enhance coverage but are never required for startup or a successful partial run.

## 2. Confirmed Product Decisions

- Market remains `cn` only.
- Scoring remains deterministic and versioned as `cn-v1`.
- Phase 2A changes observation coverage, not factor weights or lifecycle thresholds.
- The first release covers the observation layer only. ETF selection, market regime, and generic position policy belong to Phase 2B.
- Discovery remains broad and low-cost. Complete enrichment is bounded to 60 deterministic candidates per run.
- One enrichment run has a 180-second response budget.
- Online enrichment supports the current provider-reported trading date only. Historical replay reads persisted observations and never calls current live providers.
- The default A-share benchmark is CSI All Share `000985`. A sector may explicitly override `benchmark_code`; provider fallback may change the source but never the benchmark identity.
- Capital flow is normalized by traded amount, not compared as raw currency amounts.
- Constituent breadth requires at least 80% membership coverage and at least 5 valid quotes.
- Concentration is the top-five constituent traded-amount share under the same coverage gate.
- Intraday evidence is provisional; post-close evidence is finalized.
- `catalyst_score` stays `None` in Phase 2A. With every market-data field present, maximum confidence is `120 / 130 = 0.9231`.
- Constituent sets are immutable, content-addressed, deduplicated, and referenced from observations.
- Missing or failed capabilities lower confidence and remain visible in provenance. They do not silently become neutral evidence.

## 3. Scope Boundary

### In Scope

- deterministic candidate selection;
- board history and benchmark history capabilities;
- board capital-flow capability;
- constituent membership and constituent quote capabilities;
- 1/5/20-day returns and benchmark-relative evidence;
- normalized 1/5/20-day capital flow;
- breadth, liquidity expansion, volatility ratio, MA20 distance, concentration, and price/flow divergence;
- capability-level source trace, freshness, trading date, and provisional/finalized status;
- content-addressed constituent-set persistence;
- atomic persistence of constituent evidence with the existing universe/run/snapshot transaction;
- optional configuration and focused documentation;
- offline deterministic tests and optional network smoke tests.

### Out of Scope

- historical provider backfill;
- reconstructing old observations from current constituents;
- catalyst/news/policy scoring;
- ETF candidate filters or ranking;
- market regime and position ranges;
- lifecycle hysteresis or state-transition alerts;
- scheduler integration;
- API, Web, Desktop, notifications, or reports;
- outcomes and calibration;
- Hong Kong data and A/H links;
- LLM calls or narrative generation.

## 4. Architecture

Phase 2A keeps the Phase 1 inward dependency direction. Network adapters normalize source payloads into capability contracts. Pure selectors and builders consume those contracts. Ranking remains independent of networks and storage.

```text
MarketRadarService
  -> discovery provider: broad current industry/concept rankings
  -> CandidateSelector: deterministic maximum of 60 sectors
  -> CnMarketRadarEnrichmentProvider
       -> board history capability
       -> benchmark history capability
       -> capital-flow capability
       -> constituent membership capability
       -> constituent quote capability
  -> ObservationBuilder: pure formulas and provenance
  -> score_sectors: unchanged cn-v1 ranking
  -> atomic repository write:
       universe + constituent sets/observations + run + sector snapshots
```

### 4.1 Service Orchestration

`MarketRadarService` owns the two-stage flow because it already owns run timing, persistence, and ranking. Its dependencies become:

- `discovery_provider: MarketRadarProvider`;
- optional `enrichment_provider: MarketRadarEnrichmentProvider`;
- `candidate_selector: CandidateSelector`;
- existing universe loader, repository, ranking configuration, and clock.

The service obtains an optional prior snapshot only when a repository is already available for a persistent run. A non-persistent CLI run must not initialize or read a database. Callers may explicitly pass a previous snapshot to a non-persistent run when they already own one.

The service ranks the merged observations exactly once. An enrichment failure never causes a second independent ranking path.

### 4.2 Discovery Provider

`LegacyRankingProvider` remains the broad discovery provider. It continues to return the current daily ranking evidence and discovered sector definitions. It does not fetch history, flow, membership, or constituent quotes.

### 4.3 Candidate Selector

`CandidateSelector.select(...) -> tuple[EnrichmentCandidate, ...]` is a pure function. Inputs are:

- active configured universe;
- current discovery observations;
- optional previous persisted snapshot;
- candidate limit, fixed at 60 by default.

Candidates are deduplicated by canonical `sector_id`. Selection priority is:

1. every active YAML-configured seed sector;
2. previous `leading` sectors, then previous `improving` sectors, ordered by prior rank and `sector_id`;
3. current ranking extremes, filled round-robin from industry leaders, industry laggards, concept leaders, and concept laggards.

Within each queue, leaders sort by descending `return_1d_pct`, laggards by ascending value, then `sector_id`. Missing daily return sorts last. The selector records all applicable reason codes, such as `configured_seed`, `previous_leading`, and `current_concept_laggard`. When more than 60 higher-priority candidates exist, stable ordering determines the cutoff.

The selection result depends only on supplied inputs. The `persist` flag cannot otherwise change selection semantics.

### 4.4 Capability Adapters

Each capability uses an immutable generic result contract:

```python
CapabilityStatus = Literal["ok", "partial", "stale", "unavailable"]
BarStatus = Literal["provisional", "finalized"]

class CapabilityResult(FrozenModel, Generic[T]):
    capability: str
    status: CapabilityStatus
    data: T | None
    source: str
    observed_at: datetime
    data_date: date | None
    bar_status: BarStatus | None
    freshness_seconds: int
    trace: tuple[Mapping[str, Any], ...]
    error: str | None
```

The concrete normalized payloads are:

- `BoardBarSeries`: ordered trading-date bars with close and traded amount;
- `BoardFlowSeries`: ordered trading-date net-main-flow and traded amount;
- `ConstituentMembership`: canonical stock codes and source observation date;
- `ConstituentQuoteBatch`: code, current/close price, previous close, traded amount, and quote timestamp;
- benchmark history uses `BoardBarSeries` with benchmark code recorded separately.

Adapters own provider-specific field aliases, numeric parsing, non-finite rejection, code normalization, request timeout behavior, and trace entries. The orchestrator never reads a provider-native column name.

### 4.5 Provider Manager Integration

The existing `BaseFetcher` gains optional capability methods for sector history, sector flow, and sector constituents. `DataFetcherManager` exposes metadata-preserving fallback methods for those capabilities, following the existing ranking-chain pattern. Existing quote and daily-history manager APIs are reused where their contracts fit.

The initial free path uses AkShare board-level endpoints. EFinance may provide industry membership or quote fallback where its existing capability is valid. Configured Tushare or TickFlow implementations participate only when they can return the same normalized capability without changing field semantics.

A source returning an empty, malformed, wrong-date, or non-finite payload is a failed attempt and does not block the next source. Source fallback is capability-local: history failure does not discard valid flow or constituent evidence.

## 5. Timing And Point-In-Time Rules

All input instants are timezone-aware. Market dates are interpreted in `Asia/Shanghai`.

- The provider-returned `data_date` is authoritative. Local wall-clock date is never used to invent a trading date.
- Provisional evidence may use a current quote with prior finalized daily bars.
- Finalized evidence uses a completed daily bar for the same market date.
- Fields combined into one horizon must share the same terminal `data_date`.
- Sector and benchmark returns use aligned trading dates. Missing benchmark dates make `benchmark_return_20d_pct` unavailable; another index is not substituted.
- Capital-flow windows and price windows must terminate on the same `data_date`.
- Constituent quotes must belong to the observation `data_date`. A prior-day constituent quote cannot be counted as flat.
- Online enrichment rejects a requested historical `as_of` when providers expose a later current trading date. It returns explicit unavailable traces instead of attaching current data to the past.
- Replay never invokes a live capability adapter.

`bar_status` is recorded per capability because price, flow, and constituents may finalize at different times. An observation is provisional if any field it publishes depends on provisional evidence.

## 6. Field Semantics

Percent values are stored as percentage points: `2.5` means `2.5%`.

### 6.1 Returns

Let `P0` be the current valid price: the provisional current quote intraday or the finalized close after market close. Let `Pn` be the finalized close `n` trading sessions before the terminal data date.

```text
return_n_pct = (P0 / Pn - 1) * 100
```

`return_1d_pct`, `return_5d_pct`, and `return_20d_pct` require 1, 5, and 20 prior finalized sessions respectively. Missing sessions do not produce shortened-window substitutes.

`benchmark_return_20d_pct` uses the identical formula and aligned dates for the configured benchmark, default `000985`.

### 6.2 Capital Flow

For each requested window:

```text
capital_flow_nd = sum(net_main_inflow) / sum(traded_amount) * 100
```

The denominator must be finite and greater than zero. A source that supplies net inflow without corresponding traded amount cannot publish the normalized field. Raw currency amount, denominator, currency `CNY`, source-native label, and dates remain in capability provenance.

### 6.3 Breadth

Membership is the constituent set observed for the same sector and market date. Each valid quote is classified by current price versus previous close:

- greater: `up_count`;
- less: `down_count`;
- equal: `flat_count`.

The three fields publish together only when:

- at least 5 constituents have valid same-date quotes; and
- valid quotes cover at least 80% of the normalized constituent set.

If the gate fails, all three fields are `None`. The builder records total constituents, valid quotes, invalid codes, and coverage ratio.

### 6.4 Liquidity Expansion

`turnover_ratio_20d` has one canonical meaning despite its legacy name:

```text
current board traded amount / mean traded amount of the prior 20 finalized sessions
```

It is unavailable when fewer than 20 prior amounts exist or any required denominator is invalid. A provider-specific turnover-rate ratio is not silently substituted.

### 6.5 Volatility And Trend Distance

`volatility_ratio_20d` is the sample standard deviation of 20 aligned sector daily log returns divided by the sample standard deviation of the same 20 benchmark log returns. It is unavailable unless both sides have 20 finite aligned returns and benchmark volatility is positive.

`distance_ma20_pct` is:

```text
(P0 / arithmetic_mean(latest 20 finalized closes) - 1) * 100
```

Intraday calculations use the latest 20 prior finalized closes. A finalized observation includes the current finalized close in the latest-20 window.

### 6.6 Concentration

Under the same 80%-and-5-quote breadth gate:

```text
concentration_ratio = sum(top 5 constituent traded amounts) / sum(all valid constituent traded amounts)
```

The field is unavailable when total valid amount is not positive.

### 6.7 Price/Flow Divergence

`price_flow_divergence` is available only when both `return_5d_pct` and `capital_flow_5d` are available.

It is `True` when their signs oppose and both noise gates are met:

- `abs(return_5d_pct) >= 1.0`;
- `abs(capital_flow_5d) >= 0.1`.

Otherwise it is explicit `False`. These two defaults live in one enrichment configuration object and are covered at, below, and above each boundary. They are not new environment variables.

### 6.8 Catalyst And Confidence

`catalyst_score` remains `None` and must appear in `missing_fields`. Phase 2A does not synthesize zero. All other market-data fields present produce confidence `120 / 130`, rounded by the existing scorer to `0.9231`.

## 7. Observation Assembly And Provenance

`CnSectorObservationBuilder` is pure and receives the base discovery observation plus capability results. It returns one immutable `SectorObservation`.

Rules:

- enriched fields replace a discovery field only when they are valid and point-in-time compatible;
- a failed enrichment never replaces a present base field with `None`;
- missing fields are recomputed exhaustively from final values;
- `source` identifies the composite observation implementation, not a misleading single upstream provider;
- `observed_at` is the run instant, while capability timestamps remain in provenance;
- `freshness_seconds` is the maximum age among the price capabilities used by published return fields;
- a stale critical current price makes observation quality `stale`;
- no usable current price makes the observation `unavailable`;
- otherwise, because catalyst remains missing, Phase 2A enriched observations remain `partial`.

`raw_reference` contains a versioned structured payload:

```json
{
  "schema": "market-radar-observation-v2a",
  "candidate_reasons": ["configured_seed"],
  "benchmark_code": "000985",
  "data_date": "2026-07-22",
  "bar_status": "provisional",
  "field_sources": {"return_20d_pct": "akshare_board_history"},
  "capabilities": {"board_history": {"status": "ok", "trace": []}},
  "constituent_set_key": "sha256:...",
  "constituent_coverage": {"total": 42, "valid": 39, "ratio": 0.9286}
}
```

Secrets, raw HTTP headers, cookies, and full exception traces are never persisted.

## 8. Budget, Concurrency, And Failure Handling

`MarketRadarEnrichmentConfig` centralizes:

- `candidate_limit=60`;
- `total_budget_seconds=180`;
- `max_concurrency=6`;
- `constituent_min_count=5`;
- `constituent_coverage_ratio=0.80`;
- `price_divergence_threshold_pct=1.0`;
- `flow_divergence_threshold_pct=0.1`;
- `default_benchmark_code="000985"`.

Only candidate limit, total budget, and maximum concurrency are operational settings exposed through optional environment variables:

- `MARKET_RADAR_ENRICHMENT_LIMIT=60`;
- `MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS=180`;
- `MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY=6`.

They remain hidden from Web settings because Phase 2A has no Web administration surface. Formula thresholds stay code-owned and versioned.

The orchestrator uses bounded concurrency and a monotonic deadline. It stops submitting new work when the deadline expires, cancels work that has not started, and returns unavailable results for unfinished capabilities. Each adapter must also use the repository's existing bounded HTTP/library call mechanisms; the orchestration deadline is not a substitute for source-level timeout handling.

Within one run:

- benchmark history is fetched once per benchmark/data-date pair;
- identical constituent quote requests are deduplicated by canonical code;
- a capability/source pair opens a run-scoped circuit after three consecutive failed attempts, and remaining candidates record `circuit_open` before the next fallback source is considered;
- provider errors are summarized with bounded messages;
- one sector or capability failure cannot fail the full run;
- ranking and persistence still execute for partial observations.

An unexpected programming or validation error in the pure builder is not converted into broad fallback. It fails the run before persistence, preserving the existing transaction and correctness boundary.

## 9. Constituent Evidence Persistence

Phase 2A adds two additive storage entities.

### 9.1 `radar_constituent_sets`

- `set_key`: SHA-256 over market, sector ID, source, and sorted canonical codes;
- `market`;
- `sector_id`;
- `source`;
- `codes_json`;
- `constituent_count`;
- `created_at`.

The canonical code list is sorted and duplicate-free. `set_key` is the primary identity. The record is immutable; identical content reuses the same row.

### 9.2 `radar_constituent_observations`

- generated row ID;
- `market`;
- `sector_id`;
- `data_date`;
- `observed_at`;
- `source`;
- `set_key` foreign-key reference;
- unique constraint on market, sector, data date, and source.

This table states when a source observed a set without pretending the source supplied historical membership. A newly observed set is not backdated. Re-observing the same date/source with different content is a contract error rather than an overwrite.

The sector snapshot references `constituent_set_key` in its versioned observation evidence. Replay uses the persisted sector observation and may resolve the referenced set for audit; it never queries current membership.

The repository adds one atomic write method that persists configured universe history, new constituent sets/observations, the radar run, and sector snapshots in a single transaction. Any conflict or validation failure rolls back the whole run. Existing Phase 1 records require no destructive migration.

## 10. Configuration And CLI Behavior

The three operational environment variables are optional and validated with bounded integer parsing:

- limit: minimum 1, maximum 200;
- budget: minimum 10, maximum 900 seconds;
- concurrency: minimum 1, maximum 16.

Defaults work without new configuration. `.env.example`, `docs/market-radar.md`, `docs/INDEX.md` when necessary, and the flat `[Unreleased]` changelog are updated.

The existing manual CLI remains the entry point. It uses enrichment by default when the enrichment provider is available. A new `--discovery-only` switch provides an explicit diagnostic path equivalent to Phase 1 coverage. It is not a silent fallback mode: normal enrichment failure already produces partial observations and visible traces.

Non-persistent CLI execution remains database-side-effect free. Persistent execution may use the latest stored snapshot for candidate carry-forward and writes constituent evidence atomically.

## 11. Compatibility

- Existing `SectorObservation`, `SectorScore`, and `RadarRunSnapshot` serialized fields remain compatible.
- Phase 2A adds structured keys under `raw_reference`; consumers must continue treating that field as extensible evidence.
- `cn-v1` factor weights and state thresholds do not change.
- Confidence values rise as evidence becomes available but remain below 1.0 while catalyst is absent.
- Existing Phase 1 database rows remain readable; new tables are additive.
- The discovery-only path preserves Phase 1 behavior for diagnostics.
- No API/Web/Desktop consumer changes are required.

## 12. Testing Strategy

### Pure Unit Tests

- candidate priority, deduplication, round-robin fairness, and exact 60-item cutoff;
- return windows and insufficient-history behavior;
- aligned benchmark returns and date mismatch rejection;
- capital-flow normalization and zero denominator;
- 80%/5-quote breadth boundary;
- top-five concentration;
- liquidity and MA20 windows for provisional/finalized bars;
- 20-point aligned volatility ratio;
- divergence threshold boundaries and explicit `False`;
- non-finite and malformed capability payload rejection;
- exhaustive missing-field provenance and maximum `0.9231` confidence.

### Provider And Manager Tests

- provider-native aliases normalize into capability contracts;
- source fallback preserves ordered trace;
- wrong-date and empty payloads continue fallback;
- source isolation prevents mixed flow semantics;
- optional token-backed providers are skipped cleanly when unconfigured;
- run-scoped circuit breaker opens after three failures;
- benchmark and quote requests deduplicate within a run;
- total deadline stops new submissions and records unavailable results.

### Repository Tests

- canonical code hashing is order-independent and rejects duplicates after normalization;
- identical sets deduplicate;
- observations reference the expected set;
- same date/source conflicting membership fails;
- atomic rollback covers constituent, universe, run, and sector rows;
- legacy Phase 1 snapshots remain readable;
- replay resolves persisted evidence without provider calls.

### Integration Tests

An offline end-to-end fixture covers:

```text
discovery rows
  -> deterministic candidate selection
  -> mixed ok/partial capability results
  -> enriched observation formulas
  -> cn-v1 ranking
  -> atomic SQLite persistence
  -> readback and replay with zero network calls
```

The CLI test verifies both normal enrichment and `--discovery-only`, including non-persistent database isolation.

Optional `network` tests validate current AkShare/EFinance shapes and are non-blocking observation checks. They never replace deterministic fixtures.

## 13. Verification Gates

Each implementation task follows red-green-refactor and receives an independent review. Final verification includes:

- all Market Radar and CLI tests;
- configuration registry/environment tests;
- storage and repository regressions;
- changed-file `py_compile`;
- `git diff --check`;
- repository `./scripts/ci_gate.sh` in a supported shell or current-head GitHub CI;
- boundary scan confirming no API/Web/scheduler/alerts/LLM/order scope leakage;
- one optional live-provider smoke with source, date, duration, coverage, and missing fields recorded without committing transient evidence.

## 14. Risks And Mitigations

- **Public source instability:** capability-local fallback, bounded attempts, circuit breakers, partial results, and visible trace.
- **Provider semantic drift:** normalize at adapters, reject unknown columns and wrong units, and maintain network shape smoke tests.
- **Request explosion:** two-stage selection, 60-candidate cap, run-level deduplication, bulk endpoints first, and concurrency/budget limits.
- **Look-ahead leakage:** provider data dates are authoritative; online historical requests do not use current data; replay never calls providers.
- **Constituent survivorship leakage:** membership begins on the source observation date and is content-addressed; no retroactive fill.
- **Large persistence growth:** constituent lists are deduplicated by content hash and referenced from observations.
- **Mixed intraday/final evidence:** every capability records `data_date` and `bar_status`; incompatible dates do not combine.
- **False precision:** insufficient windows and low membership coverage remain missing instead of using shortened or partial substitutes.

## 15. Rollback

Revert the Phase 2A commits. New tables and optional configuration are additive and may remain unused. Phase 1 discovery-only runs continue to work against existing snapshots. No existing configuration or historical Phase 1 row requires destructive rollback.

## 16. Deferred Work

After Phase 2A stabilizes, separate specs cover:

1. Phase 2B ETF selection, market regime, and generic position policy;
2. Market Radar API and monitoring cockpit;
3. 30-minute scheduling, lifecycle hysteresis, alerts, and end-of-day reports;
4. 20-trading-day outcomes and calibration;
5. Hong Kong expansion;
6. deterministic catalyst evidence and constrained LLM narrative explanation.
