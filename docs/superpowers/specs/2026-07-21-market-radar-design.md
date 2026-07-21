# Market Radar Design

**Status:** Approved design

**Date:** 2026-07-21

**Target repository:** `huha-yy/Stock-Market-Agent`

**Upstream:** `ZhuLinsen/daily_stock_analysis`

## 1. Purpose

Build an independent Market Radar subsystem that monitors A-share and Hong Kong market sectors, capital-flow changes, and related ETFs. The subsystem produces deterministic sector rankings, ETF candidates, market-regime assessments, alerts, and model position ranges.

The initial user workflow is market monitoring rather than portfolio management. The system does not read the user's actual holdings, provide individualized suitability analysis, place orders, connect to a broker for execution, or use leverage.

## 2. Confirmed Product Decisions

- Final market scope: A shares and Hong Kong equities, delivered in phases.
- Phase 1 market: A shares, including industry/theme sectors and exchange-traded ETFs.
- Second market rollout: Hong Kong sectors and ETFs, southbound-flow context, and A/H linkage. This is delivered after the A-share foundation and evaluation phases.
- Intraday cadence: scan every 30 minutes while either supported market is open.
- End-of-day cadence: finalize daily data and generate a complete review after each market closes.
- Strategy horizon: 2 to 8 weeks.
- Primary efficacy metric: whether a recommended sector is absolutely higher after 20 trading days.
- Secondary metrics: benchmark-relative return, maximum adverse excursion, maximum drawdown, turnover, alert frequency, and coverage.
- Optimization priority: sector-direction hit rate. A 5% drawdown is a soft preference, not a hard constraint.
- Data budget: free sources for broad coverage plus one optional low-cost token-backed source for critical capabilities. Uncovered capabilities continue through free fallbacks with lower confidence.
- User experience: the Web home is a monitoring cockpit, not a research-first heatmap.
- LLM role: explain structured results only. An LLM cannot change scores, state transitions, risk limits, confidence, or position caps.

## 3. Goals and Non-Goals

### Goals

1. Maintain a normalized sector, index, and ETF universe for each supported market.
2. Collect time-stamped sector, capital-flow, breadth, liquidity, and ETF evidence.
3. Rank sectors with deterministic, reproducible scoring rules.
4. Select suitable ETF candidates within a sector and explain exclusions.
5. Produce a market regime and conservative model position range.
6. Detect meaningful state changes without generating repetitive alerts.
7. Persist immutable snapshots and evaluate each eligible signal after 20 trading days.
8. Reuse the existing scheduler, provider framework, storage conventions, API, notification channels, authentication, and React Web application.

### Non-Goals

- Automated order execution or broker integration.
- Personalized investment advice based on income, liabilities, tax status, or actual holdings.
- High-frequency or tick-level trading.
- Predicting exact prices or guaranteed returns.
- Treating news sentiment or LLM output as an independent trading signal.
- Combining incompatible provider definitions into a fabricated capital-flow total.

## 4. Architecture

Market Radar is a bounded domain alongside the existing single-stock and market-review pipelines. It does not add sector-monitoring responsibilities to the existing `StockAnalysisPipeline`.

### 4.1 `UniverseService`

Responsibilities:

- Maintain canonical sector identifiers and display names.
- Normalize aliases and provider-specific sector identifiers.
- Store sector-to-index, sector-to-constituent, and sector-to-ETF mappings.
- Track effective dates so historical replay uses the universe that existed at the time.
- Keep A-share and Hong Kong taxonomies separate while supporting explicit A/H relationships.

Dependencies: provider capability adapters and the Market Radar repositories.

### 4.2 `MarketRadarCollector`

Responsibilities:

- Collect sector prices, returns, turnover, capital flow, breadth, ETF facts, and benchmark data.
- Stamp each observation with provider, observed time, freshness, and quality.
- Normalize values without erasing provider provenance.
- Produce partial snapshots when optional capabilities fail.

Dependencies: existing `DataFetcherManager` patterns, trading calendars, provider adapters, and timeout/circuit-breaker infrastructure.

### 4.3 `SectorRankingEngine`

Responsibilities:

- Compute deterministic factor scores and risk deductions.
- Produce total score, rank, lifecycle state, confidence, and evidence.
- Use pure functions shared by online runs and historical replay.
- Apply state hysteresis so transient 30-minute noise does not repeatedly reverse a signal.

This component has no database, network, notification, or LLM dependency.

### 4.4 `EtfSelector`

Responsibilities:

- Apply hard eligibility filters before ranking ETFs.
- Compare eligible ETFs by liquidity, size, cost, tracking quality, and trend.
- Distinguish `best_supported`, `candidate`, and `insufficient_data` outcomes.
- Explain why each selected or excluded ETF received its result.

ETF thresholds are configuration values with documented defaults. Missing size, fee, spread, or tracking data lowers confidence and may prevent a `best_supported` result.

### 4.5 `PositionPolicy`

Responsibilities:

- Convert market regime, sector states, correlation groups, and confidence into model position ranges.
- Enforce total, sector, ETF, and correlated-exposure caps.
- Generate entry staging and explicit invalidation conditions.

This is a generic model policy. It does not consume account size or actual positions.

### 4.6 `RadarOrchestrator`

Responsibilities:

- Run scheduled and manually triggered scans.
- Persist run and snapshot records atomically.
- Compare current and previous states.
- Create and expire signals.
- Deduplicate and route alerts.
- Finalize daily reports and mature 20-day outcomes.

## 5. Data Contract and Quality

Every provider observation uses the following conceptual contract:

```text
value / source / observed_at / freshness / quality / raw_reference
```

Snapshot quality values:

- `COMPLETE`: all required capabilities and configured critical optional capabilities are fresh.
- `PARTIAL`: scoring can continue, but at least one optional capability is unavailable.
- `STALE`: a required value exists but exceeds its configured freshness limit.
- `UNAVAILABLE`: required evidence is absent and no new score can be produced.

Provider rules:

1. Provider-specific meanings such as "main capital flow" remain source-scoped.
2. Absolute flow amounts from incompatible providers are never added together.
3. Cross-sector comparisons use within-provider percentiles and normalized changes where possible.
4. A provider switch sets `source_changed`; continuity-dependent factors are withheld or down-weighted until comparable observations accumulate.
5. Missing data lowers confidence rather than automatically lowering sector strength.
6. Stale critical price data prevents a signal upgrade.
7. LLM or news failure cannot block deterministic ranking.
8. All downgrade reasons are visible in API responses, reports, and diagnostics.

A-share and Hong Kong observations use separate calendars, sessions, currencies, benchmarks, and freshness rules. One market being closed never implies synchronous movement in the other market.

## 6. Sector Scoring

The positive factor score totals 100 points:

| Factor | Weight | Purpose |
| --- | ---: | --- |
| Multi-horizon trend and momentum | 25 | Identify persistent 2-to-8-week trends |
| Relative strength versus market benchmark | 20 | Prefer sectors stronger than their own market |
| Capital-flow persistence over 1/5/20 days | 20 | Reward sustained rather than one-off flow |
| Constituent breadth and diffusion | 15 | Distinguish broad participation from isolated leaders |
| Turnover and liquidity expansion | 10 | Confirm that participation supports the move |
| Policy, industry, and news catalyst | 10 | Low-authority supporting evidence only |

Risk deductions total at most 30 points and cover:

- volatility shock;
- acceleration or distance-from-trend overheating;
- price/flow or price/breadth divergence;
- crowding and concentration;
- confirmed lifecycle deterioration.

The final score is clamped to `[0, 100]`. Data quality is represented separately as confidence in `[0, 1]`.

Lifecycle states:

| State | Default score rule | Additional rule |
| --- | --- | --- |
| `LEADING` | `>= 75` | confidence meets the upgrade threshold |
| `IMPROVING` | `60-74` | no critical stale data |
| `NEUTRAL` | `40-59` | none |
| `WEAKENING` | `25-39` | none |
| `AVOID` | `< 25` | none |
| `INSUFFICIENT_DATA` | not scoreable | critical evidence unavailable |

An upgrade requires either two consecutive qualifying intraday scans or an end-of-day confirmation. A risk downgrade may occur immediately. Exact numeric thresholds are centralized configuration and covered by tests; they are not duplicated in prompts or UI code.

## 7. ETF Selection

ETF selection occurs only after a sector is eligible for candidate generation.

Hard filters evaluate:

- active/listed status and minimum history;
- minimum tradable liquidity;
- acceptable stale quote and spread status;
- usable sector/index mapping;
- absence of known suspension or data-integrity failure.

Eligible ETFs are ranked by:

- assets or another supported size proxy;
- average turnover and traded value;
- bid/ask spread when available;
- total fee burden when available;
- tracking difference/error when available;
- 20/60-day trend and liquidity stability;
- deviation from the mapped sector index.

The API returns both selected candidates and rejected alternatives with reason codes. If critical comparison fields are missing, the result is `candidate`, not `best_supported`.

## 8. Market Regime and Position Policy

Market regimes use benchmark trend, breadth, volatility, liquidity, and flow diffusion:

| Regime | Model total-position range |
| --- | ---: |
| `RISK_ON` | `60%-80%` |
| `SELECTIVE` | `35%-60%` |
| `DEFENSIVE` | `10%-35%` |
| `RISK_OFF` | `0%-15%` |

Default portfolio guardrails:

- at most three suggested sectors at once;
- at most 15% per sector;
- at most 15% per ETF;
- at most 25% across highly correlated sectors;
- confidence-adjusted caps for partial data;
- no full-investment, leverage, or margin recommendation;
- staged observation/entry suggestions rather than a single all-in action.

Suggestion lifecycle:

```text
WATCHING -> CANDIDATE -> CONFIRMED -> ACTIVE -> DOWNGRADED -> EXITED
```

Every suggestion includes market regime, total range, sector cap, ETF candidate, evidence, confidence, invalidation rules, and validity period. The 5% drawdown preference is reported and monitored but does not override the user's confirmed direction-hit-rate priority.

## 9. Runtime Data Flow

```text
Pre-market:
  refresh effective universe and mappings

Every 30 minutes during an open market session:
  collect incremental observations
  -> assess data quality
  -> compute sector factors and ranks
  -> select ETF candidates
  -> compute regime and model positions
  -> persist immutable snapshot
  -> compare state transitions
  -> emit deduplicated alerts when necessary

After each market close:
  finalize official daily bars
  -> recompute end-of-day snapshot
  -> generate daily review
  -> create eligible 20-trading-day signals

After signal maturity:
  load the 20th subsequent trading-day close
  -> compute absolute and benchmark-relative outcomes
  -> persist evaluation and aggregate metrics
```

The online and replay paths call the same scoring and policy functions. Replay freezes universe membership and observations by effective time to prevent look-ahead bias and survivorship leakage where source history permits.

## 10. Persistence

New repositories own the following entities:

- `radar_universe`: effective-dated sectors, indices, constituents, and ETF mappings.
- `radar_runs`: scan identity, market, trigger, timing, and overall quality.
- `sector_snapshots`: raw factors, normalized factors, deductions, score, rank, state, confidence, and provenance.
- `etf_candidate_snapshots`: eligibility, comparison fields, rank, status, and reason codes.
- `radar_signals`: lifecycle transitions, recommendation time, validity, and invalidation rules.
- `radar_outcomes`: 20-day absolute result, benchmark result, drawdown/adverse excursion, and hit status.
- `radar_alert_events`: alert transition, deduplication key, channel attempts, and delivery result.

Writes for one run are transactional. A failed run never replaces the last successful snapshot.

## 11. API

The API is additive under `/api/v1/market-radar`:

```text
GET  /latest
GET  /sectors
GET  /sectors/{sector_id}
GET  /signals
GET  /performance
POST /runs
```

`POST /runs` requires existing administrator authentication. Read endpoints follow the repository's established authentication policy. Responses expose `as_of`, `quality`, `confidence`, `source_changed`, and downgrade reasons. A stale cached response is explicitly marked and never represented as current.

## 12. Web Experience

The chosen first-screen layout is the monitoring cockpit.

Information order:

1. current market regime, data completeness, and total model position range;
2. A-share/Hong Kong sector ranking table;
3. selected sector and ETF candidates;
4. capital-flow changes and factor trends;
5. position plan and invalidation conditions;
6. recent alerts and historical hit-rate summary.

The sector detail view contains 1/5/20/60-day rank history, flow persistence, constituent breadth, ETF comparison, factor evidence, and provenance. Long LLM-generated commentary is not placed above structured evidence.

If refresh fails, the UI retains the last successful snapshot and prominently displays its timestamp and quality. Stable dimensions are used for the ranking table, status panels, charts, and loading/error states to prevent layout shifts.

## 13. Alerts and Reports

Intraday alerts are emitted only for:

- a confirmed lifecycle transition;
- a material rank jump or drop;
- a sustained capital-flow reversal;
- market-regime deterioration;
- critical data-source or freshness failure.

Alerts use cooldown and deduplication keys based on market, sector, transition, and effective window. End-of-day reports reuse existing notification routing. One failed channel cannot fail the radar run.

## 14. Error Handling

- Provider timeout: retry within budget, then fallback or mark capability unavailable.
- Repeated provider failure: open a circuit breaker and expose diagnostics.
- Partial capabilities: continue scoreable factors and reduce confidence.
- Critical stale price: prohibit upgrades and preserve the previous confirmed state as stale.
- Database failure: roll back the whole run transaction and do not alert on unpersisted state.
- Notification failure: record each attempt and continue other channels.
- LLM failure: publish the structured report without narrative expansion.
- Unsupported market session: skip the scan with a recorded reason rather than producing empty rankings.

## 15. Testing

### Unit tests

- factor normalization and weighted score;
- risk deductions and clamping;
- confidence under each quality combination;
- state hysteresis and immediate risk downgrade;
- ETF hard filters, ranking, and reason codes;
- market-regime and position-cap rules;
- 20-trading-day maturity using market calendars.

### Provider contract tests

- field schema, source identity, timestamps, currency, and units;
- fallback behavior, timeout, stale data, and source switching;
- A-share/Hong Kong session and holiday boundaries.

### Integration tests

- complete scan through persistence and API;
- transactional rollback;
- scheduler re-entry and duplicate-run protection;
- notification deduplication and channel isolation;
- outcome maturation and aggregate performance.

### Frontend tests

- cockpit states for complete, partial, stale, unavailable, loading, and error data;
- ranking/detail navigation and API compatibility;
- responsive layout and stable dimensions;
- visible provenance and snapshot timestamp.

### Historical validation

- at least three years where the provider history supports the required factors;
- point-in-time replay with no future values;
- primary 20-day absolute-up hit rate;
- secondary benchmark-relative return, drawdown, adverse excursion, turnover, frequency, and coverage;
- comparison against simple baselines so a broad bull market does not masquerade as model skill.

## 16. Acceptance Criteria

1. At least 95% of scans that should run produce a persisted usable snapshot during the shadow period; every skip and failure records an explicit reason and is reported separately from the success rate.
2. Identical inputs and configuration produce identical ranks, states, and positions.
3. Every factor and recommendation is traceable to source observations and calculation version.
4. Stale or unavailable critical data cannot create an upgraded signal.
5. No LLM output can mutate structured decisions.
6. Historical replay shows no detected look-ahead path in tests or review.
7. Performance reporting includes enough samples and baseline comparisons; no profitability claim is made solely from a headline hit rate.
8. The subsystem first runs in shadow mode and records suggestions without execution.

## 17. Delivery Phases

### Phase 1: A-share radar foundation

- canonical A-share sector and ETF universe;
- provider capability contracts and snapshots;
- deterministic scoring, confidence, and persistence;
- offline historical replay foundation.

### Phase 2: A-share cockpit and policy

- market regimes and position policy;
- lifecycle transitions and alerts;
- Market Radar APIs and monitoring cockpit;
- end-of-day structured report.

### Phase 3: Evaluation and calibration

- signal maturity and outcome aggregation;
- historical replay UI/API;
- parameter calibration with versioned configurations;
- shadow-mode acceptance report.

### Phase 4: Hong Kong expansion

- Hong Kong industry and ETF universe;
- Hong Kong calendars, sessions, currency, and benchmarks;
- southbound-flow evidence where supported;
- explicit A/H sector relationships without forced data merging.

### Phase 5: Narrative enrichment

- low-authority catalyst evidence;
- LLM-generated explanations constrained to structured facts;
- richer daily narrative and diagnostics.

Each phase must remain deployable without the later phases. Hong Kong work starts only after the A-share contracts and replay path are stable.

## 18. Repository and Upstream Strategy

- `origin` remains `https://github.com/huha-yy/Stock-Market-Agent.git`.
- `upstream` remains `https://github.com/ZhuLinsen/daily_stock_analysis.git`.
- The repository preserves upstream commit history.
- Product work is developed on feature branches and merged into the user's `main` after verification.
- Upstream updates are fetched explicitly and integrated without rewriting user-owned feature history.
- Commits and pushes require explicit user confirmation, as required by `AGENTS.md`.

## 19. Risks and Mitigations

- **Capital-flow definitions differ:** preserve provenance and compare normalized ranks within one source.
- **Free providers are unstable:** use capability-level fallbacks, time budgets, circuit breakers, and visible confidence.
- **Sector taxonomies drift:** use canonical effective-dated mappings and mapping diagnostics.
- **ETF metadata is incomplete:** downgrade from `best_supported` to `candidate`; never fabricate fee or tracking quality.
- **Absolute hit rate is regime-sensitive:** retain the confirmed primary metric but always publish baseline and benchmark-relative context.
- **Overfitting:** begin with fixed interpretable weights, version every configuration, and validate out of sample before calibration is accepted.
- **Alert fatigue:** use state hysteresis, transition-only alerts, cooldown, and deduplication.
- **Scope growth:** deliver A-share deterministic foundations before Hong Kong and LLM enrichment.
