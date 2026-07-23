# Market Radar Phase 2B ETF, Regime, And Position Policy Design

**Status:** Approved concept; written specification awaiting review

**Date:** 2026-07-23

**Base:** Market Radar Phase 2A at `650672ae`

## 1. Purpose

Phase 2B turns the persisted A-share sector evidence from Phase 2A into three deterministic policy outputs:

1. eligible and ranked ETF alternatives for supported sectors;
2. an A-share market-regime assessment;
3. a generic model position policy with explicit caps and invalidation reasons.

The implementation remains evidence driven. It does not call an LLM, read an account, place an order, or change the Phase 2A `cn-v1` sector score.

## 2. Scope

### Included

- active effective-dated ETF mapping resolution;
- bounded ETF observation collection;
- ETF hard filters, ranking, confidence, statuses, and exclusion reason codes;
- market-regime scoring from the current benchmark and sector snapshot;
- generic total-position ranges and sector/ETF/correlation caps;
- atomic persistence of ETF evidence and all policy outputs with the Radar run;
- persisted-evidence replay with no provider access;
- manual CLI JSON output for the new fields.

### Excluded

- lifecycle hysteresis, signals, alerts, scheduling, and notifications;
- API, Web, Desktop, and report rendering;
- outcome maturation, backtest calibration, and parameter optimization;
- Hong Kong data or A/H linkage;
- news, catalyst, policy, or LLM narrative enrichment;
- order execution, broker integration, account holdings, suitability, leverage, or margin.

Phase 2B must not add a second sector-ranking implementation or modify `cn-v1` weights, thresholds, confidence, or state classification.

## 3. Pipeline And Ownership

The Phase 2A service remains responsible for universe discovery, sector evidence, and `cn-v1` scoring. Phase 2B is downstream:

```text
effective universe + Phase 2A sector observations
  -> bounded ETF observation collection
  -> pure ETF eligibility and ranking
  -> pure market-regime assessment
  -> pure generic position policy
  -> one atomic persisted run
```

New code stays under `src/market_radar/` and follows the existing provider capability, immutable Pydantic model, repository, and replay boundaries. Network and database access never enter the selector, regime calculator, or position-policy functions.

## 4. Immutable Contracts

All timestamps are timezone-aware. Market dates use `Asia/Shanghai`; percentages and basis points are explicitly named. Non-finite numeric values are invalid rather than missing neutral evidence.

### 4.1 `EtfObservation`

One observation represents one ETF at one run anchor and contains:

- identity: `market`, `sector_id`, `code`, `name`;
- point in time: `observed_at`, authoritative provider `data_date`, `bar_status`;
- mapping: mapping effective dates and mapped benchmark/index code;
- hard-filter evidence: listed/active state, listing date or finalized-session count, suspension state, current price, current traded amount, prior-20-session average traded amount, quote freshness, spread, and premium/discount;
- ranking evidence: 20/60-session returns, the aligned 60-session daily-return series, tracking difference/error, annual total fee, supported size proxy, and liquidity stability;
- provenance: field-level source references, provider trace, quality, and exact missing fields.

Current fetch time never substitutes for a missing provider timestamp. A current mapping is not backdated into replay.

### 4.2 `EtfSelection`

Each mapped alternative remains visible and contains:

- ETF and sector identity;
- `status`: `best_supported`, `candidate`, `rejected`, or `insufficient_data`;
- `eligible`, stable rank when eligible, total score when scoreable, and confidence;
- available component scores and their effective weights;
- ordered reason codes;
- the immutable observation used by the decision.

At most one ETF per sector is `best_supported`. Missing optional evidence cannot be serialized as zero, cannot improve rank, and prevents `best_supported`.

### 4.3 `MarketRegimeAssessment`

The assessment contains `regime_version="cn-regime-v1"`, score, regime, confidence, coverage, component scores, cohort sector IDs, missing fields, reasons, and `as_of`.

Regime values are `risk_on`, `selective`, `defensive`, `risk_off`, and `insufficient_data`.

### 4.4 `PositionPlan`

The plan contains `policy_version="cn-position-v1"`, regime identity, total-position range, up to three ordered sector suggestions, selected ETF alternatives and caps, known correlation groups, confidence, invalidation reason codes, and `as_of`.

This is a generic model policy. It contains no currency amount, share quantity, account balance, actual holding, order, leverage, or individualized instruction.

### 4.5 Run Compatibility

`RadarRunSnapshot` gains optional tuple/nullable fields for ETF selections, regime, and position plan. Defaults preserve deserialization of Phase 1 and Phase 2A records. Existing sector fields and serialized values do not change.

## 5. ETF Collection Boundary

Only ETFs in mappings effective on the run's authoritative China market date are considered. Expired, future, duplicate, or malformed mappings are not sent to providers and remain diagnosable through bounded run trace entries.

The collector observes no more than 30 unique ETFs per run. Candidate priority is deterministic:

1. sectors ordered by current `cn-v1` rank;
2. ETF mappings in curated universe order within a sector;
3. stable ETF code as the final tie breaker.

Collection has a 90-second monotonic total budget and maximum concurrency of 6. It reuses provider-level timeouts, ordered fallbacks, request deduplication, and bounded error summaries. Deadline expiry stops new submissions and marks unfinished observations unavailable. One ETF/provider failure cannot fail other ETFs or sector ranking. Validation/programming errors in pure policy code fail before persistence and are not converted into broad fallback.

The three runtime values are code-owned defaults in Phase 2B. They are not new environment settings because the strict scope has no operational requirement to tune them.

## 6. ETF Eligibility

Hard filters run before ranking. Reason codes use the order below, and an ETF retains every applicable reason:

| Order | Reason code | Rule |
| ---: | --- | --- |
| 1 | `inactive_mapping` | mapping is not effective on `data_date` |
| 2 | `not_active` | provider identifies the instrument as unlisted, delisted, or inactive |
| 3 | `insufficient_history` | fewer than 60 finalized trading sessions |
| 4 | `invalid_price` | current price is absent, non-finite, or not positive |
| 5 | `invalid_amount` | current or required history amount is absent, non-finite, or negative |
| 6 | `low_liquidity` | prior-20-session average traded amount is below CNY 10,000,000 |
| 7 | `stale_quote` | quote freshness exceeds 2,700 seconds |
| 8 | `suspended` | suspension is confirmed |
| 9 | `data_integrity_failure` | dates, units, identity, or normalized fields conflict |
| 10 | `spread_too_wide` | available bid/ask spread exceeds 50 bps |
| 11 | `premium_discount_too_large` | available absolute premium/discount exceeds 2.0% |

Zero current traded amount is allowed only when the provider explicitly identifies a valid fresh auction/session state; otherwise it is `invalid_amount`. Missing spread or premium/discount is optional evidence: it does not reject the ETF but lowers confidence and prevents `best_supported`. An unavailable suspension state is also missing evidence and prevents `best_supported`; a confirmed suspension rejects it.

An eligible ETF must pass every hard filter. A rejected ETF has `status="rejected"`, no score, and the ordered hard-filter reasons. A provider-unavailable observation that cannot evaluate required filters has `status="insufficient_data"` with explicit missing-field reasons; it is not treated as rejected market evidence.

## 7. ETF Ranking

Eligible ETFs are compared only with eligible alternatives mapped to the same sector. Every component is normalized to `[0, 100]` with a stable percentile rank; higher raw values are better except for tracking error/difference and fee, where lower is better. For `n > 1`, the best ordinal position scores 100 and the worst scores 0 using `100 * (n - 1 - position) / (n - 1)`; ties use their mean ordinal position. A sole observed value scores 100. ETF code breaks only final total-score ties and never changes equal component scores.

| Component | Weight | Raw evidence |
| --- | ---: | --- |
| liquidity | 35% | prior-20-session average traded amount, with liquidity stability as a tie breaker |
| trend | 25% | equal mean of available 20- and 60-session returns |
| tracking quality | 20% | lower tracking error when available, otherwise lower absolute tracking difference |
| cost | 10% | lower annual total fee |
| size | 10% | higher fund net assets, otherwise provider-reported shares times the point-in-time price |

Liquidity and trend are required for a score. Optional missing components remain absent; available weights are renormalized to 100 for the numerical score. This renormalization is for comparability only and cannot disguise missing evidence.

ETF confidence is deterministic:

```text
ranking_coverage = sum(original weights of available ranking components) / 100
safety_coverage = 0.8 + 0.2 * (available spread, premium/discount,
                               and suspension checks / 3)
quality_multiplier = complete: 1.0, partial: 0.85, stale/unavailable: 0.0
confidence = ranking_coverage * safety_coverage * quality_multiplier
```

The public confidence is clamped to `[0, 1]` and rounded to four decimals. Thus every optional gap lowers confidence; no absent field becomes neutral or favorable evidence.

The eligible sort key is total score descending, confidence descending, prior-20-session average traded amount descending, then ETF code ascending. The first eligible ETF is `best_supported` only when all five components plus spread, premium/discount, and suspension state are present and observation quality is neither stale nor unavailable. Otherwise every scoreable eligible ETF is `candidate`. If a sector has no eligible scoreable ETF, it has no selected ETF and retains all rejected/insufficient alternatives.

## 8. Market Regime

Regime uses finalized or point-in-time-compatible Phase 2A evidence from the same run. It never fetches or infers a missing sector value.

### 8.1 Coverage Gate

The denominator is every sector with a current `cn-v1` score in the run. A sector belongs to the regime cohort only when it has `return_20d_pct`, `capital_flow_5d`, `turnover_ratio_20d`, and a state other than `insufficient_data`, with no stale critical price evidence.

The canonical benchmark is `000985`, as in Phase 2A. Its persisted 20-session return values must identify the same terminal date and value across cohort observations; conflicting values are a data-integrity error rather than an average. The benchmark 20-session return is required. The result is `insufficient_data` with no numeric regime score when:

- fewer than 5 sectors are in the cohort;
- cohort size divided by denominator is below 60%; or
- the benchmark 20-session return is missing or stale.

### 8.2 Components

The score is the weighted sum of five `[0, 100]` components:

| Component | Weight | Calculation |
| --- | ---: | --- |
| benchmark 20-day trend | 30% | piecewise score below |
| positive-sector diffusion | 25% | percentage of cohort sectors with `return_20d_pct > 0` |
| flow diffusion | 20% | percentage with `capital_flow_5d > 0` |
| liquidity diffusion | 10% | percentage with `turnover_ratio_20d >= 1.0` |
| non-risk sector share | 15% | percentage whose state is not `weakening` or `avoid` |

Benchmark trend scores are deterministic:

| Benchmark return | Score |
| --- | ---: |
| `>= 5%` | 100 |
| `>= 2%` and `< 5%` | 75 |
| `>= 0%` and `< 2%` | 55 |
| `> -2%` and `< 0%` | 35 |
| `<= -2%` | 0 |

The weighted score is rounded to four decimals only at the public boundary. Regime thresholds are inclusive:

| Score | Regime |
| ---: | --- |
| 75-100 | `risk_on` |
| 55-<75 | `selective` |
| 35-<55 | `defensive` |
| 0-<35 | `risk_off` |

Confidence equals coverage multiplied by the mean `cn-v1` confidence of cohort sectors, capped to `[0, 1]`. The assessment lists excluded sectors and exact reasons so high score cannot hide low coverage.

## 9. Generic Position Policy

### 9.1 Total Range

| Regime | Model total-position range |
| --- | ---: |
| `risk_on` | 60%-80% |
| `selective` | 35%-60% |
| `defensive` | 10%-35% |
| `risk_off` | 0%-15% |
| `insufficient_data` | 0%-10% |

The range describes a generic portfolio-level risk budget, not a requirement that the listed Radar sectors sum to that range. Phase 2B suggests only the supported sector sleeve; any residual capacity remains explicitly unallocated and is not converted into an ETF or cash instruction.

### 9.2 Sector And ETF Suggestions

Eligible sectors must be `leading` or `improving`, have confidence at least 0.60, and have at least one non-rejected ETF selection. Candidates sort by sector score descending, sector confidence descending, then sector ID ascending. At most three are retained.

For each retained sector:

```text
joint_confidence = min(sector confidence, selected ETF confidence, regime confidence)
sector_cap_pct = round_down_to_0.1(15 * joint_confidence)
ETF_cap_pct = min(15, sector_cap_pct)
```

The selected ETF is `best_supported` when present; otherwise it is the highest-ranked `candidate` and its weaker status remains visible. A zero cap removes the suggestion. No lower allocation target is generated.

### 9.3 Correlation Guardrail

Pairwise correlation uses exactly 60 aligned finalized daily returns from persisted ETF evidence. Values that are missing, non-finite, or have zero variance do not produce a correlation. ETFs connected by correlation `>= 0.80` form deterministic transitive groups. The sum of caps in a known group is limited to 25%; caps are reduced from the lowest-ranked member first, with code ascending as the final deterministic tie breaker.

Unknown correlation does not fabricate independence. It lowers plan confidence by multiplying it by `known_pair_count / total_pair_count` when two or more ETFs are suggested and adds `correlation_coverage_incomplete`. With fewer than two suggestions, correlation coverage is 1.

Plan confidence is the minimum of regime confidence and retained joint confidences, adjusted by correlation coverage. When there are no retained suggestions, it equals regime confidence and the plan records `no_supported_sector_suggestions`. Suggestions retain these invalidation codes as applicable:

- `sector_state_deteriorated`;
- `sector_confidence_below_threshold`;
- `etf_became_ineligible`;
- `critical_evidence_stale`;
- `market_regime_deteriorated`;
- `correlation_cap_reached`.

These are machine-readable conditions for later lifecycle work. Phase 2B does not monitor transitions or emit alerts.

## 10. Persistence And Replay

Phase 2B adds additive storage owned by `MarketRadarRepository`:

- `radar_etf_observations`: immutable normalized evidence keyed to run, sector, and ETF;
- `radar_etf_selections`: status, rank, scores, confidence, and reason codes for every mapped alternative;
- `radar_regime_assessments`: one optional versioned assessment per run;
- `radar_position_plans`: one optional versioned plan per run, including suggestions and correlation evidence.

The existing enriched-run transaction is extended so universe history, constituent evidence, run metadata, sector snapshots, ETF observations, ETF selections, regime, and position plan commit or roll back together. Idempotent re-save of the same `run_key` must validate semantic equality; conflicting evidence is an error, not an overwrite.

Repository reads reconstruct immutable domain models and validate run/sector/ETF identity, observation time, policy version, and referenced evidence. Existing databases migrate additively and legacy runs remain readable with absent Phase 2B fields.

Persisted replay performs zero provider calls and zero current-universe reads. It loads the effective universe and all observations stored for the selected run, re-executes the same pure ETF, regime, and position functions, and returns a replay snapshot. Recomputed results must equal stored policy outputs; a mismatch or future evidence is a corruption error. Legacy Phase 1/2A replay continues to return its original sector-only contract.

Non-persistent CLI execution must not initialize, read, or write SQLite. It can collect live ETF evidence and compute policy in memory. `--persist` uses the extended atomic transaction. JSON output adds `etfs`, `regime`, and `position_plan` without removing existing fields.

## 11. Configuration And Documentation

Formula thresholds, weights, versions, and runtime bounds are centralized immutable configuration objects with validation and unit tests. Phase 2B adds no environment variables. If implementation discovers a real operator need for a new setting, that requires a separate design update plus `.env.example` and documentation changes.

Implementation updates `docs/market-radar.md` and the flat `[Unreleased]` section of `docs/CHANGELOG.md`. README is unchanged because the feature remains a subsystem-level CLI capability. No bilingual counterpart currently exists for `docs/market-radar.md`.

## 12. Testing Strategy

### Pure Unit Tests

- every hard-filter boundary and complete ordered reason set;
- optional missing fields never become zero evidence;
- within-sector percentile ties, reversed metrics, weight renormalization, and stable ordering;
- `best_supported` completeness and single-winner invariant;
- regime coverage at exactly 5 sectors and exactly 60%;
- every benchmark step and regime threshold boundary;
- confidence calculations and insufficient-data reasons;
- total ranges, three-sector limit, confidence-adjusted caps, and zero-cap removal;
- 60-session correlation alignment, transitive groups, 0.80 boundary, cap reduction, and missing-correlation penalty.

### Provider And Orchestration Tests

- effective mapping selection and stable 30-ETF cutoff;
- authoritative timestamps, normalized units, malformed/non-finite rejection, and fallback trace;
- 90-second deadline behavior, concurrency maximum 6, request deduplication, and isolated failure;
- Phase 2A sector results are byte-for-byte unchanged by Phase 2B policy.

### Repository And Integration Tests

- additive schema migration and legacy-read compatibility;
- one transaction writes every Phase 2B entity;
- injected failure rolls back all old and new entities;
- idempotent equality and conflicting-run rejection;
- offline full run through readback;
- replay equality with providers and current universe made fail-fast;
- non-persistent CLI database isolation and additive JSON output.

## 13. Verification Gates

Implementation uses red-green-refactor. Final verification includes:

- all Market Radar and CLI tests;
- relevant configuration/storage regressions;
- changed Python files through `py_compile`;
- `git diff --check`;
- `./scripts/ci_gate.sh` in a supported shell or current-head CI evidence;
- a scope scan proving no API/Web/Desktop/scheduler/alert/report/LLM/order leakage;
- optional live-provider smoke recorded as non-blocking evidence, never as a replacement for fixtures.

## 14. Risks And Mitigations

- **ETF source gaps:** optional evidence lowers confidence and blocks `best_supported`; required gaps remain `insufficient_data`.
- **Cross-provider semantic drift:** field-level provenance, unit validation, ordered fallback, and no mixed fabricated totals.
- **False precision:** exact windows and coverage gates; no shortened history or zero substitution.
- **Request growth:** 30-ETF cap, 90-second budget, concurrency 6, deduplication, and isolated failure.
- **Look-ahead/survivorship leakage:** effective mapping and provider dates are persisted; replay reads only run evidence.
- **Policy overreach:** outputs are ranges and caps only, with no account or execution surface.
- **Atomicity regression:** a single repository transaction and injected rollback tests cover all run entities.

## 15. Rollback

Revert the Phase 2B implementation commits. New tables and optional serialized fields are additive and may remain unused. Phase 2A manual, discovery-only, persistence, and replay behavior continues against legacy snapshots without destructive data migration.

## 16. Deferred Work

Separate designs must cover lifecycle hysteresis and alerts, scheduling, API/Web/Desktop, reports, outcome calibration, Hong Kong expansion, narrative enrichment, and any execution or account-aware feature.
