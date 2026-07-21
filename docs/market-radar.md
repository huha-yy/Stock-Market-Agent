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

Price/flow divergence is nullable risk evidence: `false` means the provider explicitly observed no divergence, while `null` means the provider did not supply the evidence. It carries 6 coverage points, matching its maximum risk deduction, so the complete confidence coverage authority is 130 points.

## Universe

The curated ETF seed is stored at `src/data/market_radar/a_share_etfs.yaml`. Persistence retains the complete effective-dated sector and ETF history, while each online provider fetch receives only the mappings active on the current China market date. Historical reads apply the same effective-date filtering to sectors and their nested ETFs. Code/name pairs must be checked against an exchange or fund-manager source before release.
