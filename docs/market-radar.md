# Market Radar

Market Radar Phase 2B produces a deterministic, current A-share industry and concept snapshot. It preserves Phase 2A sector discovery, evidence, and `cn-v1` ranking, then adds bounded ETF collection, deterministic ETF selection, a market-regime assessment, and a generic cap-only position policy. It records provider provenance and does not call an LLM.

## Run

Normal execution enables the Phase 2A enrichment and Phase 2B policy pipeline and prints JSON to standard output:

```bash
python scripts/run_market_radar.py --market cn
```

Persistence, atomic file output, and the Phase 1-compatible discovery diagnostic are explicit options:

```bash
python scripts/run_market_radar.py --market cn --persist
python scripts/run_market_radar.py --market cn --output reports/market-radar.json
python scripts/run_market_radar.py --market cn --discovery-only
```

`--discovery-only` skips the enrichment stage; it is an operator diagnostic, not a silent fallback. Without `--persist`, the run does not initialize or read SQLite: there is no previous-snapshot carry-forward and no database write. `--output` writes the complete JSON snapshot atomically whether or not persistence is enabled.

## Configuration

Phase 2A works without new configuration. The following optional integer settings control only enrichment orchestration:

| Setting | Default | Valid range | Meaning |
| --- | ---: | ---: | --- |
| `MARKET_RADAR_ENRICHMENT_LIMIT=60` | 60 | 1-200 | Maximum candidates selected for enrichment |
| `MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS=180` | 180 seconds | 10-900 | Monotonic deadline for the whole enrichment stage |
| `MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY=6` | 6 | 1-16 | Maximum concurrent enrichment work |

The formula thresholds remain code-owned and versioned; these settings are not exposed in Web settings because Market Radar has no Web administration surface. Phase 2B adds no environment variables. ETF collection is fixed at 30 unique ETFs, a 90-second monotonic budget, and maximum concurrency of 6.

AkShare currently implements board history, benchmark history, industry flow, and current industry/concept membership. Its current membership endpoints do not supply an authoritative observation date. Those codes may remain visible as partial provenance, but undated membership is excluded from dated breadth, concentration, constituent-set keys, and persisted constituent evidence; its date is never inferred from board history or constituent quotes. Concept flow has no equivalent capability and is explicitly `unavailable`.

Constituent realtime quotes use the existing `DataFetcherManager` fallback chain. The default A-share path accepts AkShare Tencent and Sina quotes when the provider payload supplies an authoritative Asia/Shanghai timestamp, previous close, current price, and traded amount. It never substitutes the local fetch time for a missing provider timestamp. A malformed or missing timestamp or previous close leaves that quote unusable and continues fallback; EFinance/Eastmoney may therefore remain invalid for this capability when those authoritative fields are absent. Tushare and TickFlow do not currently override the optional normalized board-capability methods. Future implementations may participate only when they satisfy the same normalized field, date, and provenance contracts. An empty, malformed, non-finite, or wrong-date result advances that individual capability to its next implemented provider; a history failure does not discard valid flow or provider-dated constituent evidence.

## Two-Stage Flow

1. Broad discovery loads current industry and concept rankings and the active curated seeds.
2. Deterministic selection enriches at most the configured candidate limit: active configured seeds first, then prior persisted `leading` and `improving` sectors, then current industry/concept leaders and laggards in stable round-robin order.

The second stage uses bounded concurrency and a monotonic total deadline. It stops submitting new work after the deadline, cancels work that has not started, and marks unfinished capabilities unavailable. One provider or sector failure remains visible as partial evidence and does not prevent ranking or persistence of other observations.

Concept sectors follow the same selection and normalized evidence contract as industry sectors. When a provider cannot map a concept to history, flow, membership, or quotes, the missing capability remains explicitly `unavailable` or the observation remains `partial`; it is never converted to a neutral zero.

## Phase 2B JSON Contract

Phase 2B appends three fields without changing existing sector JSON bytes: `etfs` is the ordered tuple of ETF alternatives, `regime` is the versioned market assessment, and `position_plan` is the generic cap-only policy. Discovery-only and legacy Phase 1/2A snapshots retain `etfs=[]`, `regime=null`, and `position_plan=null`.

ETF evidence is collected only for mappings effective on the authoritative China market date. Sector score order, curated mapping order, and ETF code provide deterministic priority. Provider failure is isolated to that ETF; deadline expiry remains visible in trace output and never changes `snapshot.sectors`.

### ETF Eligibility, Ranking, And Confidence

`cn-etf-v1` evaluates no more than 30 ETFs. Each alternative has one of four statuses: `best_supported`, `candidate`, `rejected`, or `insufficient_data`. Hard-filter reason codes are ordered as follows:

`inactive_mapping`, `not_active`, `insufficient_history`, `invalid_price`, `invalid_amount`, `low_liquidity`, `stale_quote`, `suspended`, `data_integrity_failure`, `spread_too_wide`, and `premium_discount_too_large`.

Required-evidence gaps use `missing_data_date`, `missing_active`, `missing_finalized_session_count`, `missing_current_price`, `missing_current_traded_amount`, and `missing_average_traded_amount_20d`. Liquidity and trend gaps that prevent ranking add `missing_required_ranking_evidence`. A rejected ETF keeps every applicable hard reason; missing required evidence produces `insufficient_data`, never negative market evidence.

The fixed gates are 60 finalized sessions, prior-20-session average traded amount of at least CNY 10,000,000, quote freshness no more than 2,700 seconds, spread no more than 50 bps when available, and absolute premium/discount no more than 2.0% when available. Zero current amount is valid only for an explicitly identified fresh auction/session state.

Eligible alternatives are ranked within their sector using liquidity 35%, trend 25%, tracking quality 20%, cost 10%, and size 10%. Missing optional components stay absent and available weights are renormalized only for score comparability. Confidence is:

```text
ranking coverage * (0.8 + 0.2 * available safety checks / 3) * quality multiplier
quality multiplier = complete: 1.0, partial: 0.85, stale/unavailable: 0.0
```

Only a first-ranked ETF with all five components plus spread, premium/discount, and suspension evidence can be `best_supported`; other scoreable alternatives remain `candidate`.

### Market Regime

`cn-regime-v1` uses the existing sector scores and canonical benchmark `000985`. A sector enters the cohort only when 20-session return, 5-session capital flow, 20-session turnover ratio, usable state, and non-stale critical price evidence are present. Fewer than 5 cohort sectors, coverage below 60%, or missing benchmark evidence produces `insufficient_data` with no numeric score.

The five components are benchmark trend 30%, positive-sector diffusion 25%, flow diffusion 20%, liquidity diffusion 10%, and non-risk sector share 15%. Benchmark trend maps returns `>=5%`, `>=2%`, `>=0%`, `>-2%`, and `<=-2%` to 100, 75, 55, 35, and 0. Inclusive regime thresholds are `risk_on >=75`, `selective >=55`, `defensive >=35`, and `risk_off <35`. Confidence equals cohort coverage times mean cohort sector confidence. Excluded sectors, missing fields, and reasons such as `cohort_below_minimum`, `coverage_below_minimum`, and `benchmark_missing` remain explicit.

### Generic Position Policy

`cn-position-v1` emits model ranges, not personalized advice or executable allocation instructions:

| Regime | Total-position range |
| --- | ---: |
| `risk_on` | 60%-80% |
| `selective` | 35%-60% |
| `defensive` | 10%-35% |
| `risk_off` | 0%-15% |
| `insufficient_data` | 0%-10% |

At most three `leading` or `improving` sectors with confidence at least 0.60 and a supported ETF are suggested. For each suggestion, joint confidence is the minimum of sector, ETF, and regime confidence; the sector cap is `floor_to_0.1(15 * joint_confidence)` and the ETF cap cannot exceed 15% or its sector cap. There is no minimum allocation target.

Correlation uses exactly 60 aligned finalized daily ETF returns. Correlation `>=0.80` creates deterministic transitive groups whose combined ETF cap is at most 25%. Unknown pairs do not imply independence: coverage is `known_pair_count / total_pair_count`, multiplies plan confidence, and adds `correlation_coverage_incomplete`. No retained suggestion adds `no_supported_sector_suggestions`. Suggestion invalidation codes are `sector_state_deteriorated`, `sector_confidence_below_threshold`, `etf_became_ineligible`, `critical_evidence_stale`, `market_regime_deteriorated`, and `correlation_cap_reached`.

## Current Snapshot And Replay

All instants are timezone-aware and market dates use `Asia/Shanghai`. A live run first captures a start anchor for request validation, universe selection, discovery, and previous-snapshot lookup. Omitting `as_of` uses this captured start; when a caller supplies `as_of`, it must represent the exact same UTC instant as that start anchor. Caller-selected historical or future instants are rejected, including another instant on the same Asia/Shanghai date. Realtime quote acquisition may advance the final observation anchor to the latest accepted evidence acquisition time. The persisted snapshot and enriched observations use that final anchor, which is never earlier than any accepted evidence timestamp. The live service also rejects `trigger="replay"`.

Replay is a separate persisted snapshot replay path. It reads stored sector and ETF observations, reuses the same deterministic sector, ETF, regime, and position functions, and makes zero live provider calls and zero current-universe reads. Referenced constituent evidence remains resolvable for audit. Recomputed Phase 2B outputs must semantically equal stored outputs; a mismatch or future observation is a corruption error. Legacy runs follow the unchanged sector-only path. There is no historical provider backfill, and current membership is never used to reconstruct an old observation.

The provider-returned `data_date` is authoritative. Price, benchmark, flow, membership, and constituent quote evidence combined into a field must be point-in-time compatible. Intraday evidence is marked `provisional`; completed same-date evidence is `finalized`.

## Metric Contract

Percent fields use percentage points: `2.5` means `2.5%`. Windows count trading sessions, not calendar days. Missing history never produces a shortened-window substitute.

### Returns And Benchmark

For 1, 5, and 20 sessions:

```text
return_n_pct = (current valid price / finalized close n sessions earlier - 1) * 100
```

The current valid price is the provisional current quote intraday or the finalized close after market close. `return_1d_pct`, `return_5d_pct`, and `return_20d_pct` require 1, 5, and 20 prior finalized sessions respectively.

`benchmark_return_20d_pct` uses the same formula on aligned trading dates. The exact default benchmark is `000985`; if its aligned history is unavailable, the benchmark field stays missing. Market Radar does not substitute another index and does not use the sector itself as an identity fallback.

### Capital Flow

For each 1, 5, and 20-session window:

```text
capital_flow_nd = sum(net_main_inflow) / sum(traded_amount) * 100
```

The flow and price windows must end on the same `data_date`. The denominator must be finite and positive. A raw inflow amount without a matching traded-amount denominator cannot publish a normalized flow value.

### Breadth And Concentration

Each valid same-date constituent quote is classified by current price versus previous close as `up_count`, `down_count`, or `flat_count`. Membership must carry its own provider date and exactly match the terminal board session; matching quotes cannot date an undated membership. All three publish together only when there are at least 5 valid quotes and those quotes cover at least 80% of the normalized constituent set. Otherwise all three stay missing; an absent quote is not counted as flat.

Under the same breadth gate:

```text
concentration_ratio = sum(top 5 valid constituent traded amounts) / sum(all valid constituent traded amounts)
```

Concentration stays missing when total valid traded amount is not positive.

### Liquidity, Volatility, And MA20

`turnover_ratio_20d` keeps its legacy field name but has one canonical liquidity meaning:

```text
current board traded amount / mean traded amount of the prior 20 finalized sessions
```

A provider-specific turnover-rate ratio is not substituted.

`volatility_ratio_20d` is the sample standard deviation of 20 aligned sector daily log returns divided by the sample standard deviation of the same 20 benchmark log returns. Both sides require 20 finite aligned returns and benchmark volatility must be positive.

`distance_ma20_pct` is:

```text
(current valid price / arithmetic mean of the latest 20 finalized closes - 1) * 100
```

Intraday calculation uses the prior 20 finalized closes; a finalized observation includes the current finalized close in the latest-20 window.

### Price/Flow Divergence

`price_flow_divergence` is available only when both `return_5d_pct` and `capital_flow_5d` are available from compatible evidence. It is `true` when their signs oppose and both `abs(return_5d_pct) >= 1.0` and `abs(capital_flow_5d) >= 0.1`. When both inputs exist but the condition is not met, it is explicitly `false`; when either input is missing, divergence also stays missing.

## Quality And Provenance

Every capability records status (`ok`, `partial`, `stale`, or `unavailable`), source trace, observation time, provider `data_date`, freshness, and provisional/finalized status. Published fields retain field-level source references; constituent coverage and missing fields remain in the observation evidence.

A stale critical board price makes the enriched observation `stale`; no usable board history makes it `unavailable`; otherwise Phase 2A enrichment remains `partial` because catalyst evidence is not implemented. `catalyst_score` is `None` and appears in `missing_fields`. With every market-data field present, the maximum confidence is `120 / 130 = 0.9231`. Missing capabilities reduce confidence and remain explicit; they never become neutral zero evidence.

## Persistence And Audit

With `--persist`, only provider-dated constituent membership that exactly matches the terminal board session is stored as immutable, content-addressed evidence. Its key is SHA-256 over market, sector ID, source, and the sorted canonical constituent codes. Identical content reuses the same row; conflicting content for the same sector, source, and observation date is rejected rather than overwritten or backdated. Undated current membership remains observation-only and cannot produce a constituent-set reference.

The sector snapshot references its constituent-set key. One atomic transaction writes the effective-dated universe, constituent sets and observations, radar run, sector snapshots, ETF observations and selections, the regime assessment, and the position plan; any validation or storage conflict rolls back the whole run. Same-key retries must be semantically identical, while changed ETF or policy evidence is rejected rather than overwritten. Repository reads reconstruct all JSON through immutable Pydantic models. Existing Phase 1/2A records remain readable with empty Phase 2B fields.

The curated ETF seed remains at `src/data/market_radar/a_share_etfs.yaml`. Persistence retains its effective-dated sector and ETF history, while an online run sends providers only mappings active on the current China market date.

## Read-only API (Phase 2C)

Phase 2C exposes the latest persisted A-share snapshot under the authenticated FastAPI v1 surface:

- `GET /api/v1/market-radar/latest` returns the complete latest snapshot. A new installation with no persisted run returns HTTP 200 with `available=false` and `run=null`.
- `GET /api/v1/market-radar/sectors` returns every sector in persisted rank order. With no run it returns HTTP 200 with an empty list and `available=false`.
- `GET /api/v1/market-radar/sectors/{sector_id}` returns one canonical sector, its ETF alternatives, matching position suggestion, regime, and position plan from the same snapshot. It returns `market_radar_run_not_found` when no run exists and `market_radar_sector_not_found` when the identifier is absent.

These routes always read market `cn` through `MarketRadarRepository`; they do not accept a market override, call a provider, start a scan, recompute policy, or write data. When `ADMIN_AUTH_ENABLED=true`, the existing administrator session cookie is required. Legacy persisted snapshots remain readable with an empty ETF list and null regime, position suggestion, and position plan.

## Web Monitoring Cockpit (Phase 2C)

The authenticated Web shell exposes the read-only cockpit at `/market-radar`. The page preserves the API ranking and policy outputs without recomputing score, regime, ETF eligibility, confidence, or position limits in the browser. It supports the existing Chinese and English UI languages and the existing light/dark themes.

Information is presented in this order:

1. market regime, run quality and coverage, suggested total-position range, and snapshot metadata;
2. the complete persisted sector ranking in API order;
3. the selected sector's factor scores, risk reasons, and missing evidence;
4. ETF alternatives, sector and ETF caps, joint confidence, and invalidation codes.

The page selects the first persisted rank initially and loads another sector detail only when the user selects it. Refresh requests the latest snapshot and ranking together. A run-key mismatch is treated as a summary error rather than combining two snapshots. A detail error stays local so the overview and ranking remain usable, and stale detail responses cannot replace a newer selection. With no persisted run, the page shows an empty bootstrap state; legacy snapshots show unavailable regime and policy values instead of zero.

This cockpit is A-share-only and read-only. It has no run, scheduling, alert, notification, report, configuration, or mutation controls. Creating a fresh snapshot remains an operator workflow through `scripts/run_market_radar.py --market cn --persist`.

## Phase 2C Out Of Scope

The current exclusions are:

- historical provider backfill;
- reconstructing old observations from current constituents;
- catalyst/news/policy scoring;
- lifecycle hysteresis, signals, state-transition alerts, scheduling, or notifications;
- write APIs, Desktop, or report rendering;
- outcomes and calibration;
- Hong Kong data and A/H links;
- LLM calls or narrative generation;
- order execution, broker integration, account holdings, suitability, leverage, or margin.
