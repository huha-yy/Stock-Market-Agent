# Market Radar Phase 2B ETF, Regime, And Position Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic A-share ETF selection, market-regime assessment, generic model position policy, atomic persistence, and provider-free replay downstream of the unchanged Phase 2A sector ranking.

**Architecture:** Extend the immutable Market Radar run contract with ETF evidence and pure policy outputs. Keep provider orchestration in a bounded ETF collector, calculations in three dependency-free modules, and storage in the existing repository transaction; online runs and replay call the same pure functions.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy, pandas, pytest, existing `DataFetcherManager` provider fallback, SQLite.

## Global Constraints

- Keep `cn-v1` sector scoring byte-for-byte unchanged.
- Support `market="cn"` only; do not add Hong Kong behavior.
- Collect at most 30 unique ETFs with a 90-second monotonic budget and maximum concurrency 6.
- Hard-filter defaults are 60 finalized sessions, CNY 10,000,000 prior-20-session average amount, 2,700-second freshness, 50 bps spread, and 2.0% absolute premium/discount.
- ETF ranking weights are liquidity 35, trend 25, tracking quality 20, cost 10, and size 10.
- Regime weights are benchmark trend 30, positive-sector diffusion 25, flow diffusion 20, liquidity diffusion 10, and non-risk sector share 15.
- Regime thresholds are `risk_on >= 75`, `selective >= 55`, `defensive >= 35`, else `risk_off`; fewer than 5 valid sectors, coverage below 60%, or missing/stale benchmark yields `insufficient_data`.
- Position ranges are 60-80, 35-60, 10-35, 0-15, and 0-10 percent for the five regimes respectively.
- Enforce maximum 3 suggested sectors, 15% per sector, 15% per ETF, and 25% per correlation group at correlation `>= 0.80`.
- Missing optional evidence remains `None`, lowers confidence, and prevents `best_supported`; never fabricate zero evidence.
- Non-persistent CLI execution must not initialize or read SQLite; replay must not call providers or read the current universe.
- Persist universe, constituent evidence, sector snapshots, ETF observations/selections, regime, and position plan in one transaction.
- Do not add lifecycle hysteresis, alerts, scheduling, API/Web/Desktop, reports, outcomes, calibration, LLM/news, orders, account state, leverage, or margin.
- Follow red-green-refactor, keep each task independently reviewable, and use English commit messages without `Co-Authored-By`.

---

## File Map

- Modify `src/market_radar/models.py`: immutable Phase 2B public contracts and backward-compatible optional run fields.
- Create `src/market_radar/policy_config.py`: validated code-owned Phase 2B thresholds, weights, versions, and runtime bounds.
- Create `src/market_radar/etf_selection.py`: pure ETF eligibility, percentile ranking, confidence, and status assignment.
- Create `src/market_radar/regime.py`: pure coverage gate, component calculation, and regime classification.
- Create `src/market_radar/position_policy.py`: pure sector selection, caps, correlation grouping, and plan confidence.
- Extend `src/market_radar/capabilities.py`: normalized provider ETF payload contract.
- Extend `src/market_radar/capability_provider.py`: provider payload validation/normalization into ETF facts.
- Create `src/market_radar/etf_collection.py`: stable candidate cutoff and bounded concurrent collection.
- Modify `data_provider/base.py`: optional `get_market_radar_etf` capability and ordered fallback routing.
- Modify `data_provider/akshare_fetcher.py`: AkShare-native ETF history/quote payload using existing ETF calls.
- Modify `src/storage.py`: four additive Phase 2B SQLAlchemy records.
- Modify `src/market_radar/repository.py`: atomic writes, equality validation, reads, and replay evidence loading.
- Modify `src/market_radar/service.py`: downstream policy orchestration without changing sector scoring.
- Modify `src/market_radar/replay.py`: persisted Phase 2B recomputation and equality verification.
- Modify `src/market_radar/__init__.py`: lazy exports for new public contracts/components.
- Modify `scripts/run_market_radar.py`: wire the ETF provider/collector for non-discovery runs.
- Modify `docs/market-radar.md` and `docs/CHANGELOG.md`: user-visible CLI and policy semantics.
- Add focused tests under `tests/market_radar/` and extend `tests/test_run_market_radar.py`.

---

### Task 1: Immutable Phase 2B Contracts And Configuration

**Files:**
- Modify: `src/market_radar/models.py`
- Create: `src/market_radar/policy_config.py`
- Modify: `src/market_radar/__init__.py`
- Modify: `tests/market_radar/test_models.py`
- Create: `tests/market_radar/test_policy_config.py`

**Interfaces:**
- Produces: `EtfObservation`, `EtfComponentScores`, `EtfSelection`, `MarketRegimeAssessment`, `PositionSuggestion`, `CorrelationGroup`, `PositionPlan`.
- Produces: `EtfPolicyConfig`, `RegimeConfig`, and `PositionPolicyConfig` with approved immutable defaults.
- Changes: `RadarRunSnapshot.etfs`, `.regime`, and `.position_plan` are optional/default-empty and preserve legacy validation.

- [ ] **Step 1: Write failing contract tests**

Add tests that instantiate complete models, reject timezone-naive `observed_at`, non-finite values, duplicate ETF codes, invalid status/range/cap values, and prove this legacy construction still works:

```python
legacy = RadarRunSnapshot(
    run_key="cn:20260723T070000Z:manual",
    market="cn",
    trigger="manual",
    as_of=aware,
    quality="partial",
    scoring_version="cn-v1",
    sectors=(),
    provider_trace=(),
)
assert legacy.etfs == ()
assert legacy.regime is None
assert legacy.position_plan is None
```

Test exact config defaults and validation for all weights summing to 100, ordered regime thresholds, runtime bounds, position range ordering, and correlation in `[0, 1]`.

- [ ] **Step 2: Run the tests and confirm the missing contracts fail**

Run: `python -m pytest tests/market_radar/test_models.py tests/market_radar/test_policy_config.py -q`

Expected: collection/import failure for the new contract names.

- [ ] **Step 3: Implement the immutable models**

Use the existing `FrozenModel`, `DataQuality`, and timezone validators. Define the public shape explicitly:

```python
EtfSelectionStatus = Literal[
    "best_supported", "candidate", "rejected", "insufficient_data"
]
MarketRegime = Literal[
    "risk_on", "selective", "defensive", "risk_off", "insufficient_data"
]

class EtfObservation(FrozenModel):
    market: MarketRadarMarket = "cn"
    sector_id: str
    code: str = Field(pattern=r"^\d{6}$")
    name: str
    observed_at: datetime
    data_date: date | None
    bar_status: Literal["provisional", "finalized"] | None
    source: str
    quality: DataQuality
    freshness_seconds: int = Field(ge=0)
    mapping_effective_from: date
    mapping_effective_to: date | None = None
    benchmark_code: str | None = None
    active: bool | None = None
    finalized_session_count: int | None = Field(default=None, ge=0)
    suspended: bool | None = None
    current_price: float | None = Field(default=None, allow_inf_nan=False)
    current_traded_amount: float | None = Field(default=None, allow_inf_nan=False)
    average_traded_amount_20d: float | None = Field(default=None, allow_inf_nan=False)
    spread_bps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    premium_discount_pct: float | None = Field(default=None, allow_inf_nan=False)
    return_20d_pct: float | None = Field(default=None, allow_inf_nan=False)
    return_60d_pct: float | None = Field(default=None, allow_inf_nan=False)
    daily_return_dates_60: tuple[date, ...] = ()
    daily_returns_60: tuple[float, ...] = ()
    tracking_error_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tracking_difference_pct: float | None = Field(default=None, allow_inf_nan=False)
    annual_fee_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    size_cny: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    liquidity_stability: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    missing_fields: tuple[str, ...]
    raw_reference: Mapping[str, Any] = Field(default_factory=dict)
```

Define the dependent public models with these stable fields (all tuples immutable, all public floats finite and bounded):

```python
class EtfComponentScores(FrozenModel):
    liquidity: float | None = Field(default=None, ge=0, le=100)
    trend: float | None = Field(default=None, ge=0, le=100)
    tracking_quality: float | None = Field(default=None, ge=0, le=100)
    cost: float | None = Field(default=None, ge=0, le=100)
    size: float | None = Field(default=None, ge=0, le=100)

class EtfSelection(FrozenModel):
    sector_id: str
    code: str
    name: str
    status: EtfSelectionStatus
    eligible: bool
    rank: int | None = Field(default=None, ge=1)
    score: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    components: EtfComponentScores
    effective_weights: Mapping[str, float] = Field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    observation: EtfObservation

class RegimeComponents(FrozenModel):
    benchmark_trend: float = Field(ge=0, le=100)
    positive_sector_diffusion: float = Field(ge=0, le=100)
    flow_diffusion: float = Field(ge=0, le=100)
    liquidity_diffusion: float = Field(ge=0, le=100)
    non_risk_sector_share: float = Field(ge=0, le=100)

class MarketRegimeAssessment(FrozenModel):
    regime_version: Literal["cn-regime-v1"] = "cn-regime-v1"
    as_of: datetime
    score: float | None = Field(default=None, ge=0, le=100)
    regime: MarketRegime
    confidence: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    components: RegimeComponents | None = None
    cohort_sector_ids: tuple[str, ...] = ()
    excluded_sector_reasons: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

class PositionSuggestion(FrozenModel):
    sector_id: str
    sector_name: str
    sector_rank: int = Field(ge=1)
    etf_code: str
    etf_status: Literal["best_supported", "candidate"]
    minimum_pct: None = None
    sector_cap_pct: float = Field(ge=0, le=15)
    etf_cap_pct: float = Field(ge=0, le=15)
    joint_confidence: float = Field(ge=0, le=1)
    invalidation_codes: tuple[str, ...] = ()

class CorrelationGroup(FrozenModel):
    etf_codes: tuple[str, ...] = Field(min_length=2)
    maximum_total_pct: float = Field(default=25, ge=0, le=25)

class PositionPlan(FrozenModel):
    policy_version: Literal["cn-position-v1"] = "cn-position-v1"
    as_of: datetime
    regime: MarketRegime
    total_position_min_pct: float = Field(ge=0, le=100)
    total_position_max_pct: float = Field(ge=0, le=100)
    suggestions: tuple[PositionSuggestion, ...] = Field(default=(), max_length=3)
    correlation_groups: tuple[CorrelationGroup, ...] = ()
    correlation_coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...] = ()
```

Add model validators for mapping date order, exact missing-field provenance, exactly 60 strictly increasing correlation dates paired one-to-one with 60 returns when present, unique run ETF identities, at most one `best_supported` per sector, matching run timestamps/identities, ordered total ranges, caps, and at most three suggestions.

- [ ] **Step 4: Implement code-owned configuration**

Create frozen dataclasses/Pydantic models with constants copied from the specification. Keep runtime values out of `src/config.py` and `.env.example`:

```python
class EtfPolicyConfig(FrozenModel):
    policy_version: Literal["cn-etf-v1"] = "cn-etf-v1"
    candidate_limit: int = Field(default=30, ge=1, le=30)
    total_budget_seconds: int = Field(default=90, ge=10, le=90)
    max_concurrency: int = Field(default=6, ge=1, le=6)
    minimum_finalized_sessions: int = 60
    minimum_average_amount_cny: float = 10_000_000.0
    stale_after_seconds: int = 2700
    maximum_spread_bps: float = 50.0
    maximum_abs_premium_discount_pct: float = 2.0
    component_weights: Mapping[str, float] = {
        "liquidity": 35.0, "trend": 25.0,
        "tracking_quality": 20.0, "cost": 10.0, "size": 10.0,
    }
```

Define `RegimeConfig` and `PositionPolicyConfig` with the exact defaults and validators; export public names lazily from `src/market_radar/__init__.py`:

```python
class RegimeConfig(FrozenModel):
    regime_version: Literal["cn-regime-v1"] = "cn-regime-v1"
    default_benchmark_code: str = "000985"
    minimum_sector_count: int = 5
    minimum_coverage: float = 0.60
    weights: Mapping[str, float] = {
        "benchmark_trend": 30.0,
        "positive_sector_diffusion": 25.0,
        "flow_diffusion": 20.0,
        "liquidity_diffusion": 10.0,
        "non_risk_sector_share": 15.0,
    }
    risk_on_minimum: float = 75.0
    selective_minimum: float = 55.0
    defensive_minimum: float = 35.0

class PositionPolicyConfig(FrozenModel):
    policy_version: Literal["cn-position-v1"] = "cn-position-v1"
    total_ranges: Mapping[str, tuple[float, float]] = {
        "risk_on": (60.0, 80.0),
        "selective": (35.0, 60.0),
        "defensive": (10.0, 35.0),
        "risk_off": (0.0, 15.0),
        "insufficient_data": (0.0, 10.0),
    }
    minimum_sector_confidence: float = 0.60
    maximum_suggested_sectors: int = 3
    maximum_sector_pct: float = 15.0
    maximum_etf_pct: float = 15.0
    correlation_threshold: float = 0.80
    maximum_correlated_pct: float = 25.0
    correlation_sessions: int = 60
```

- [ ] **Step 5: Run focused tests and existing model regressions**

Run: `python -m pytest tests/market_radar/test_models.py tests/market_radar/test_policy_config.py tests/market_radar/test_ranking.py -q`

Expected: all pass and existing `cn-v1` assertions remain unchanged.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/market_radar/models.py src/market_radar/policy_config.py src/market_radar/__init__.py tests/market_radar/test_models.py tests/market_radar/test_policy_config.py
git commit -m "feat: define Market Radar policy contracts"
```

---

### Task 2: Pure ETF Eligibility And Ranking

**Files:**
- Create: `src/market_radar/etf_selection.py`
- Create: `tests/market_radar/test_etf_selection.py`

**Interfaces:**
- Consumes: `EtfObservation`, `EtfPolicyConfig`.
- Produces: `select_etfs(observations: Sequence[EtfObservation], config: EtfPolicyConfig) -> tuple[EtfSelection, ...]`.
- Produces: stable ordered hard-filter reason constants for diagnostics/tests.

- [ ] **Step 1: Write failing hard-filter tests**

Use a complete observation factory and parameterized boundary cases. Assert all applicable reasons appear in specification order, `50.0` bps and `2.0%` pass, values just above fail, exactly 60 sessions and exactly CNY 10,000,000 pass, optional spread/premium/suspension gaps do not reject, and unavailable required evidence yields `insufficient_data` rather than `rejected`.

```python
result = select_etfs([observation(finalized_session_count=59)], config)
assert result[0].status == "rejected"
assert result[0].reason_codes == ("insufficient_history",)
assert result[0].score is None
```

- [ ] **Step 2: Run hard-filter tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_etf_selection.py -k "filter or boundary" -q`

Expected: import failure for `select_etfs`.

- [ ] **Step 3: Implement eligibility classification**

Implement a pure `_eligibility()` that distinguishes unavailable required evidence from observed rejection and appends every reason in the fixed order. Validate mapping against `observation.data_date`; never mutate or synthesize observations.

```python
def select_etfs(
    observations: Sequence[EtfObservation],
    config: EtfPolicyConfig,
) -> tuple[EtfSelection, ...]:
    _require_unique_observations(observations)
    classified = [_classify(item, config) for item in observations]
    return _rank_by_sector(classified, config)
```

- [ ] **Step 4: Write failing ranking/confidence tests**

Cover one-item score 100, mean ordinal percentile ties, reversed tracking/cost metrics, tracking-error preference, size fallback already normalized into `size_cny`, required liquidity/trend gaps, optional weight renormalization, exact confidence formula, stable final tie breaker, and only one complete winner per sector.

```python
assert complete[0].status == "best_supported"
assert missing_fee[0].status == "candidate"
assert missing_fee[0].confidence < complete[0].confidence
assert missing_fee[0].components.cost is None
```

- [ ] **Step 5: Implement percentile ranking and confidence**

Use `statistics.fmean`, no pandas. Calculate component percentiles within eligible alternatives for one sector, renormalize only available original weights, and calculate:

```python
ranking_coverage = available_original_weight / 100.0
safety_coverage = 0.8 + 0.2 * available_safety_checks / 3.0
quality_multiplier = {
    "complete": 1.0, "partial": 0.85, "stale": 0.0, "unavailable": 0.0,
}[observation.quality]
confidence = round(ranking_coverage * safety_coverage * quality_multiplier, 4)
```

Sort eligible rows by score descending, confidence descending, average amount descending, code ascending. Append rejected/insufficient rows in input sector/code order so all alternatives remain visible.

- [ ] **Step 6: Run focused and model tests**

Run: `python -m pytest tests/market_radar/test_etf_selection.py tests/market_radar/test_models.py -q`

Expected: all pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/market_radar/etf_selection.py tests/market_radar/test_etf_selection.py
git commit -m "feat: rank eligible Market Radar ETFs"
```

---

### Task 3: Pure Market-Regime Assessment

**Files:**
- Create: `src/market_radar/regime.py`
- Create: `tests/market_radar/test_regime.py`

**Interfaces:**
- Consumes: Phase 2A `SectorScore` including its persisted `SectorObservation`, `RegimeConfig`, and `as_of`.
- Produces: `assess_market_regime(sectors: Sequence[SectorScore], config: RegimeConfig, as_of: datetime) -> MarketRegimeAssessment`.

- [ ] **Step 1: Write failing coverage and benchmark tests**

Build sector scores with real observation dictionaries. Assert fewer than 5 cohort sectors, coverage `0.5999`, missing benchmark, stale critical price, and conflicting `000985` terminal/value evidence produce the exact insufficient/integrity outcomes; exactly 5 and exactly 60% proceed.

```python
assessment = assess_market_regime(sectors, config, as_of)
assert assessment.regime == "insufficient_data"
assert assessment.score is None
assert "coverage_below_minimum" in assessment.reasons
```

- [ ] **Step 2: Run coverage tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_regime.py -k "coverage or benchmark" -q`

Expected: import failure for `assess_market_regime`.

- [ ] **Step 3: Implement cohort and canonical benchmark extraction**

Parse each `SectorScore.observation` with `SectorObservation.model_validate`, require `return_20d_pct`, `capital_flow_5d`, `turnover_ratio_20d`, non-insufficient state, and non-stale critical price. Read canonical benchmark identity/date from Phase 2A `raw_reference`; raise `ValueError` on conflicting point-in-time evidence instead of averaging.

- [ ] **Step 4: Write failing score/threshold/confidence tests**

Parameterize benchmark step boundaries `5, 2, 0, -epsilon, -2`, all four regime boundaries `75, 55, 35`, each diffusion denominator, four-decimal rounding, and:

```python
expected_confidence = round(coverage * fmean(item.confidence for item in cohort), 4)
assert assessment.confidence == expected_confidence
```

- [ ] **Step 5: Implement deterministic regime calculation**

Implement one helper per component and a single public function:

```python
components = RegimeComponents(
    benchmark_trend=_benchmark_score(benchmark_return),
    positive_sector_diffusion=_share(cohort, lambda item: return_20d(item) > 0),
    flow_diffusion=_share(cohort, lambda item: flow_5d(item) > 0),
    liquidity_diffusion=_share(cohort, lambda item: turnover(item) >= 1.0),
    non_risk_sector_share=_share(
        cohort, lambda item: item.state not in {"weakening", "avoid"}
    ),
)
score = round(sum(component * weight / 100 for component, weight in ...), 4)
```

Retain cohort IDs, excluded IDs/reasons, coverage, missing fields, and version in the returned immutable assessment.

- [ ] **Step 6: Run focused tests and prove ranking did not change**

Run: `python -m pytest tests/market_radar/test_regime.py tests/market_radar/test_ranking.py -q`

Expected: all pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/market_radar/regime.py tests/market_radar/test_regime.py
git commit -m "feat: assess Market Radar regime"
```

---

### Task 4: Pure Generic Position Policy

**Files:**
- Create: `src/market_radar/position_policy.py`
- Create: `tests/market_radar/test_position_policy.py`

**Interfaces:**
- Consumes: `Sequence[SectorScore]`, `Sequence[EtfSelection]`, `MarketRegimeAssessment`, and `PositionPolicyConfig`.
- Produces: `build_position_plan(...) -> PositionPlan`.

- [ ] **Step 1: Write failing range and candidate tests**

Assert all five exact total ranges, only `leading`/`improving` sectors with confidence `>= 0.60`, no rejected-only sector, score/confidence/ID ordering, maximum three suggestions, preference for `best_supported`, fallback to highest candidate, and no lower target allocation.

```python
plan = build_position_plan(sectors, selections, regime, config)
assert plan.total_position_min_pct == 60.0
assert plan.total_position_max_pct == 80.0
assert len(plan.suggestions) == 3
assert all(item.minimum_pct is None for item in plan.suggestions)
```

- [ ] **Step 2: Run candidate tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_position_policy.py -k "range or candidate" -q`

Expected: import failure for `build_position_plan`.

- [ ] **Step 3: Implement confidence-adjusted caps**

Use decimal/floor arithmetic to avoid binary rounding drift:

```python
joint = min(sector.confidence, etf.confidence, regime.confidence)
sector_cap = floor(15.0 * joint * 10.0) / 10.0
etf_cap = min(15.0, sector_cap)
```

Remove zero-cap suggestions, preserve weaker ETF status, and record `no_supported_sector_suggestions` when empty. Do not allocate residual total-range capacity.

- [ ] **Step 4: Write failing correlation tests**

Cover exactly 60 aligned returns, fewer/misaligned/non-finite/zero-variance series, correlation just below and exactly `0.80`, transitive grouping, deterministic lowest-ranked-first cap reduction to 25%, code tie breaker, one-suggestion coverage 1, and missing-pair confidence penalty.

- [ ] **Step 5: Implement correlation groups and plan confidence**

Use `statistics.correlation` only after validating exact aligned series. Build connected components from qualifying edges, sort member codes, reduce group caps from the lowest-ranked suggestion, and calculate:

```python
correlation_coverage = known_pair_count / total_pair_count if total_pair_count else 1.0
base_confidence = min([regime.confidence, *joint_confidences]) if suggestions else regime.confidence
plan_confidence = round(base_confidence * correlation_coverage, 4)
```

Attach exact invalidation/correlation reason codes without transition monitoring.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/market_radar/test_position_policy.py tests/market_radar/test_models.py -q`

Expected: all pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/market_radar/position_policy.py tests/market_radar/test_position_policy.py
git commit -m "feat: build Market Radar position policy"
```

---

### Task 5: ETF Provider Contract And Bounded Collection

**Files:**
- Modify: `src/market_radar/capabilities.py`
- Modify: `src/market_radar/capability_provider.py`
- Create: `src/market_radar/etf_collection.py`
- Modify: `data_provider/base.py`
- Modify: `data_provider/akshare_fetcher.py`
- Modify: `tests/market_radar/test_capabilities.py`
- Modify: `tests/market_radar/test_capability_provider.py`
- Create: `tests/market_radar/test_etf_collection.py`
- Modify: `tests/market_radar/test_akshare_capabilities.py`

**Interfaces:**
- Produces: normalized `EtfCapabilityData` with ordered `EtfBar` history and optional current/fund facts.
- Adds optional `BaseFetcher.get_market_radar_etf(code, *, as_of, deadline_monotonic, monotonic)`.
- Adds `MarketRadarEtfProvider.fetch_etf(etf, as_of, ...) -> CapabilityResult[EtfCapabilityData]` and `ProviderCapabilityAdapter.fetch_etf`.
- Produces: `EtfCollectionBatch(observations, trace, as_of)` and `MarketRadarEtfCollector.collect(universe, sectors, as_of)`.

- [ ] **Step 1: Write failing capability schema tests**

Test strictly increasing unique dates, finite positive closes, nonnegative amount, authoritative timezone quote, identity match, optional facts, malformed/non-finite rejection, and `provider_capability_data_date("etf_snapshot", payload)`.

- [ ] **Step 2: Run capability tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_capabilities.py tests/market_radar/test_capability_provider.py -k etf -q`

Expected: missing ETF capability contract/mapping.

- [ ] **Step 3: Add provider-neutral ETF capability routing**

Add `get_market_radar_etf` returning `None` to `BaseFetcher`; add `"etf_snapshot": "get_market_radar_etf"` and allowed kind `{"etf"}` to `DataFetcherManager`. Pass only `(name,)` for ETF and benchmark capabilities. Extend validation/normalization without changing existing sector capability behavior.

The normalized payload contains at least:

```python
class EtfCapabilityData(FrozenModel):
    code: str
    bars: tuple[EtfBar, ...]
    quoted_at: datetime | None = None
    current_price: float | None = None
    current_traded_amount: float | None = None
    active: bool | None = None
    suspended: bool | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    nav: float | None = None
    tracking_error_pct: float | None = None
    tracking_difference_pct: float | None = None
    annual_fee_pct: float | None = None
    net_assets_cny: float | None = None
    shares: float | None = None
```

- [ ] **Step 4: Write AkShare payload fixture tests**

Mock `fund_etf_hist_em` and `fund_etf_spot_em`; assert provider dates, CNY amount units, quote time handling, active status, current price/amount, and optional unknown facts remain `None`. Wrong code, missing timestamp, malformed amount, and future rows must be rejected or omitted visibly.

- [ ] **Step 5: Implement AkShare native capability**

Reuse the existing ETF history/spot parsing helpers and cache rather than issuing parallel duplicate APIs. Return a provider-native dictionary/DataFrame shape accepted by the normalizer. Do not invent listing status, suspension, bid/ask, NAV, fees, or assets when absent.

- [ ] **Step 6: Write failing builder/collector tests**

Test stable priority by current sector rank, curated ETF order then code, deduplication, exact 30 cutoff, active mapping date, exact 60/20-day formulas, returns, daily-return series, spread/premium formulas when present, confidence-quality provenance, isolated failure, deadline cancellation, maximum active work 6, and final observation anchor.

- [ ] **Step 7: Implement bounded ETF collection**

Reuse the Phase 2A `_BoundedScheduler` only by extracting it to a shared private helper if necessary; do not duplicate concurrency logic. Build observations with a pure `CnEtfObservationBuilder` inside `etf_collection.py`:

```python
class MarketRadarEtfCollector:
    def collect(
        self,
        universe: Sequence[SectorDefinition],
        sectors: Sequence[SectorScore],
        as_of: datetime,
    ) -> EtfCollectionBatch:
        candidates = _select_effective_candidates(universe, sectors, as_of, limit=30)
        deadline = self.monotonic() + self.config.total_budget_seconds
        results, unfinished = self._scheduler(deadline).run(
            candidates,
            lambda pair: self.provider.fetch_etf(
                pair[1], as_of, deadline_monotonic=deadline
            ),
        )
        return self._build_batch(candidates, results, unfinished, as_of)
```

Builder errors remain fatal; provider errors become bounded unavailable observations.

- [ ] **Step 8: Run provider and collector suites**

Run: `python -m pytest tests/market_radar/test_capabilities.py tests/market_radar/test_capability_provider.py tests/market_radar/test_akshare_capabilities.py tests/market_radar/test_etf_collection.py -q`

Expected: all pass, including all pre-existing capability tests.

- [ ] **Step 9: Commit Task 5**

```bash
git add src/market_radar/capabilities.py src/market_radar/capability_provider.py src/market_radar/etf_collection.py data_provider/base.py data_provider/akshare_fetcher.py tests/market_radar/test_capabilities.py tests/market_radar/test_capability_provider.py tests/market_radar/test_etf_collection.py tests/market_radar/test_akshare_capabilities.py
git commit -m "feat: collect bounded Market Radar ETF evidence"
```

---

### Task 6: Atomic Phase 2B Persistence

**Files:**
- Modify: `src/storage.py`
- Modify: `src/market_radar/repository.py`
- Modify: `tests/market_radar/test_repository.py`

**Interfaces:**
- Adds: `RadarEtfObservationRecord`, `RadarEtfSelectionRecord`, `RadarRegimeAssessmentRecord`, `RadarPositionPlanRecord`.
- Replaces/extends: `save_enriched_run(..., etf_observations=(), snapshot=...)` while preserving existing callers through defaults until Task 7.
- Adds: `load_phase2b_evidence(run_id) -> tuple[tuple[EtfObservation, ...], tuple[EtfSelection, ...], MarketRegimeAssessment | None, PositionPlan | None]`.

- [ ] **Step 1: Write failing additive-schema and round-trip tests**

Assert all four tables, foreign keys with cascade, unique run/sector/code identities, one regime/plan per run, stable selection position, JSON reconstruction, and legacy database/run reads with no Phase 2B rows.

Use table shapes:

```text
radar_etf_observations(run_id, sector_id, code, position, observation_json)
radar_etf_selections(run_id, sector_id, code, position, selection_json)
radar_regime_assessments(run_id UNIQUE, assessment_json)
radar_position_plans(run_id UNIQUE, plan_json)
```

- [ ] **Step 2: Run repository schema tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_repository.py -k "phase2b or etf or regime or position" -q`

Expected: missing SQLAlchemy records/tables.

- [ ] **Step 3: Implement additive records and repository serialization**

Add records before `DatabaseManager` initializes `Base.metadata`. Extend `_save_run_in_session` to insert Phase 2B output after sector rows. Parse all JSON through Pydantic on read; never return unchecked dictionaries.

- [ ] **Step 4: Write failing atomicity/idempotency tests**

Inject a trigger failure into each new table in turn and assert zero universe/run/sector/ETF/policy rows survive. Re-save identical content and assert the same run ID. Re-save the same key with changed ETF observation, selection, regime, or plan and assert `ValueError`.

- [ ] **Step 5: Implement full semantic equality and one transaction**

Move idempotent existing-run handling behind a `_assert_run_semantically_equal_in_session` comparison covering existing and new entities. Extend `save_enriched_run` so universe, constituent evidence, snapshot, and all Phase 2B evidence share its existing `_run_write_transaction` callback.

- [ ] **Step 6: Run repository and storage regressions**

Run: `python -m pytest tests/market_radar/test_repository.py tests/test_storage.py -q`

Expected: all pass; if `tests/test_storage.py` does not exist, run `python -m pytest tests -k "storage or repository" -q` and record the exact selection.

- [ ] **Step 7: Commit Task 6**

```bash
git add src/storage.py src/market_radar/repository.py tests/market_radar/test_repository.py
git commit -m "feat: persist Market Radar policy atomically"
```

---

### Task 7: Online Service, Replay, And CLI Integration

**Files:**
- Modify: `src/market_radar/service.py`
- Modify: `src/market_radar/replay.py`
- Modify: `src/market_radar/__init__.py`
- Modify: `scripts/run_market_radar.py`
- Modify: `tests/market_radar/test_service.py`
- Modify: `tests/market_radar/test_replay.py`
- Modify: `tests/market_radar/test_integration.py`
- Modify: `tests/test_run_market_radar.py`

**Interfaces:**
- Extends `MarketRadarService.__init__` with optional `etf_collector`, `etf_policy_config`, `regime_config`, and `position_policy_config`.
- Extends `ReplayFrame` with optional persisted ETF observations while preserving sector-only frame construction.
- Wires the same `select_etfs`, `assess_market_regime`, and `build_position_plan` functions online and in replay.

- [ ] **Step 1: Write failing service-order and scope tests**

Use event-recording fakes to require:

```text
discover -> enrich -> score_sectors -> collect_etfs -> select_etfs
         -> assess_regime -> build_position_plan -> persist once
```

Assert `discovery_only=True` retains legacy empty Phase 2B fields, ETF failure does not alter `snapshot.sectors`, the final `as_of` covers accepted ETF timestamps, and non-persistent runs never call repository methods.

- [ ] **Step 2: Run service tests and confirm failure**

Run: `python -m pytest tests/market_radar/test_service.py -k "etf or regime or position or phase2b" -q`

Expected: constructor/output assertions fail because Phase 2B is not wired.

- [ ] **Step 3: Implement downstream online orchestration**

After `score_sectors` and only for a non-discovery run with an ETF collector:

```python
etf_batch = self.etf_collector.collect(universe, sectors, snapshot_as_of)
etfs = select_etfs(etf_batch.observations, self.etf_policy_config)
regime = assess_market_regime(sectors, self.regime_config, final_as_of)
position_plan = build_position_plan(
    sectors, etfs, regime, self.position_policy_config
)
```

Merge bounded traces, build one snapshot, and call only the extended atomic repository method. Do not catch pure validation/programming errors.

- [ ] **Step 4: Write failing provider-free replay tests**

Persist a complete offline Phase 2B run, replace provider/current-universe access with fail-fast fakes, replay by run key, and assert recomputed ETF selections/regime/position equal stored outputs. Corrupt future evidence and each stored output independently; require a corruption `ValueError`. Preserve all legacy replay tests.

- [ ] **Step 5: Implement Phase 2B replay recomputation**

Load stored ETF observations and stored outputs through the repository, validate no observation is after the run anchor, invoke the same three pure functions, compare semantic models, and return a `trigger="replay"` snapshot. If no Phase 2B rows exist, follow the existing sector-only path exactly.

- [ ] **Step 6: Write failing CLI/integration tests**

Assert `build_service(persist=False)` constructs ETF collection without a repository, no SQLite singleton is initialized, JSON includes `etfs`, `regime`, and `position_plan`, `--discovery-only` stays legacy-safe, persistent offline integration round-trips every entity, and Phase 2A sector bytes remain unchanged for identical fixtures.

- [ ] **Step 7: Wire CLI and lazy exports**

Construct `MarketRadarEtfCollector(provider=ProviderCapabilityAdapter(manager), config=EtfPolicyConfig())` for normal runs. Pass no collector in discovery-only mode. Keep all new formula/runtime settings code-owned and avoid `src/config.py` changes.

- [ ] **Step 8: Run all Market Radar and CLI tests**

Run: `python -m pytest tests/market_radar tests/test_run_market_radar.py -q`

Expected: all pass, with a count greater than the 343-test Phase 2B baseline.

- [ ] **Step 9: Commit Task 7**

```bash
git add src/market_radar/service.py src/market_radar/replay.py src/market_radar/__init__.py scripts/run_market_radar.py tests/market_radar/test_service.py tests/market_radar/test_replay.py tests/market_radar/test_integration.py tests/test_run_market_radar.py
git commit -m "feat: integrate Market Radar policy pipeline"
```

---

### Task 8: Documentation, Scope Audit, And Final Verification

**Files:**
- Modify: `docs/market-radar.md`
- Modify: `docs/CHANGELOG.md`
- Review only: `.env.example`, `README.md`, `api/`, `apps/dsa-web/`, `apps/dsa-desktop/`, scheduler/notification modules.

**Interfaces:**
- Documents: Phase 2B JSON semantics, thresholds, runtime bounds, persistence/replay, confidence, generic-policy disclaimer, and exact exclusions.
- Does not add environment variables or client behavior.

- [ ] **Step 1: Update focused documentation**

Change the guide title/intro from Phase 2A to Phase 2B and add sections for ETF statuses/reasons, regime components/ranges, position caps/correlation confidence, atomic persistence/replay, and strict exclusions. Keep operational detail out of README.

Add one flat changelog line under `[Unreleased]`:

```markdown
- [新功能] Market Radar Phase 2B 新增可追溯的 ETF 筛选、市场状态评估、通用模型仓位区间及原子持久化回放。
```

- [ ] **Step 2: Verify documentation against code**

Run searches for every version, threshold, status, and reason code across code/tests/docs. Confirm `.env.example` has no Phase 2B runtime setting and record that no bilingual counterpart exists for `docs/market-radar.md`.

- [ ] **Step 3: Run formatting and compile checks**

Run:

```bash
git diff --check
python -m py_compile src/market_radar/models.py src/market_radar/policy_config.py src/market_radar/etf_selection.py src/market_radar/regime.py src/market_radar/position_policy.py src/market_radar/capabilities.py src/market_radar/capability_provider.py src/market_radar/etf_collection.py src/market_radar/repository.py src/market_radar/service.py src/market_radar/replay.py scripts/run_market_radar.py
```

Expected: exit 0.

- [ ] **Step 4: Run deterministic test gates**

Run:

```bash
python -m pytest tests/market_radar tests/test_run_market_radar.py -q
python -m pytest -m "not network" -q
```

Expected: all pass. Record counts and warnings.

- [ ] **Step 5: Run the repository CI gate**

Run `./scripts/ci_gate.sh` from Git Bash/WSL. If the current Windows environment cannot execute it, use current-head GitHub CI after push and explicitly record the local platform gap.

- [ ] **Step 6: Audit strict scope and changed files**

Run:

```bash
git diff --name-only origin/main...HEAD
rg -n "alert|scheduler|notification|api/v1|apps/dsa|LLM|order|broker|account|leverage|margin" src/market_radar scripts/run_market_radar.py docs/market-radar.md
```

Inspect every match and confirm it is an exclusion/guardrail rather than implemented scope. Confirm no API/Web/Desktop/report/scheduler/notification file changed and `src/market_radar/ranking.py` is unchanged.

- [ ] **Step 7: Commit Task 8**

```bash
git add docs/market-radar.md docs/CHANGELOG.md
git commit -m "docs: describe Market Radar Phase 2B policy"
```

- [ ] **Step 8: Final review, push, and PR**

Use `superpowers:requesting-code-review`, fix contract-level findings with full-path regression review, rerun verification via `superpowers:verification-before-completion`, push `codex/market-radar-phase-2b`, and create a PR titled:

```text
feat: add Market Radar ETF and position policy
```

The PR body must include scope, exact verification evidence, compatibility, risks, rollback, and an explanation that screenshots are not applicable because this phase has no report or Web UI changes.
