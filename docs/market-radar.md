# Market Radar

Market Radar Phase 2A produces a deterministic, current A-share industry and concept snapshot. It first discovers the broad market, then enriches a bounded candidate set with multi-period price, capital-flow, benchmark, constituent breadth, and concentration evidence. It records provider provenance and does not call an LLM.

## Run

Normal execution enables Phase 2A enrichment and prints JSON to standard output:

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

The formula thresholds remain code-owned and versioned; these settings are not exposed in Web settings because Phase 2A has no Web administration surface.

The default free path uses AkShare board-level capabilities. EFinance may provide industry membership or quote fallback where its data satisfies the same contract. Configured Tushare or TickFlow providers participate only when they can produce the same normalized semantics. An empty, malformed, non-finite, or wrong-date result advances that individual capability to its next provider; a history failure does not discard valid flow or constituent evidence.

## Two-Stage Flow

1. Broad discovery loads current industry and concept rankings and the active curated seeds.
2. Deterministic selection enriches at most the configured candidate limit: active configured seeds first, then prior persisted `leading` and `improving` sectors, then current industry/concept leaders and laggards in stable round-robin order.

The second stage uses bounded concurrency and a monotonic total deadline. It stops submitting new work after the deadline, cancels work that has not started, and marks unfinished capabilities unavailable. One provider or sector failure remains visible as partial evidence and does not prevent ranking or persistence of other observations.

Concept sectors follow the same selection and normalized evidence contract as industry sectors. When a provider cannot map a concept to history, flow, membership, or quotes, the missing capability remains explicitly `unavailable` or the observation remains `partial`; it is never converted to a neutral zero.

## Current Snapshot And Replay

All instants are timezone-aware and market dates use `Asia/Shanghai`. Online discovery and enrichment accept only the current Asia/Shanghai calendar date. The service rejects a historical live `as_of`, even if a caller supplies one, rather than attaching current provider data to a past observation. The live service also rejects `trigger="replay"`.

Replay is a separate persisted snapshot replay path. It reads the stored sector observations, reuses the deterministic scoring path, and makes zero live provider calls. Referenced constituent evidence remains resolvable for audit. There is no historical provider backfill, and current membership is never used to reconstruct an old observation.

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

Each valid same-date constituent quote is classified by current price versus previous close as `up_count`, `down_count`, or `flat_count`. All three publish together only when there are at least 5 valid quotes and those quotes cover at least 80% of the normalized constituent set. Otherwise all three stay missing; an absent quote is not counted as flat.

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

With `--persist`, constituent membership is stored as immutable, content-addressed evidence. Its key is SHA-256 over market, sector ID, source, and the sorted canonical constituent codes. Identical content reuses the same row; conflicting content for the same sector, source, and observation date is rejected rather than overwritten or backdated.

The sector snapshot references its constituent-set key. One atomic transaction writes the effective-dated universe, constituent sets and observations, radar run, and sector snapshots; any validation or storage conflict rolls back the whole run. Existing Phase 1 records remain readable.

The curated ETF seed remains at `src/data/market_radar/a_share_etfs.yaml`. Persistence retains its effective-dated sector and ETF history, while an online run sends providers only mappings active on the current China market date.

## Out Of Scope

The exact Phase 2A exclusions are:

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
