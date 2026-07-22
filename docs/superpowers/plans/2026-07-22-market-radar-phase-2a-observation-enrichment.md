# Market Radar Phase 2A Observation Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the bounded, deterministic A-share observation-enrichment layer described by the approved Phase 2A design while preserving Phase 1 ranking and non-persistent CLI behavior.

**Architecture:** `MarketRadarService` keeps broad discovery, selects at most 60 candidates, asks a capability-oriented enrichment provider for normalized evidence, and passes that evidence to a pure observation builder. Ranking stays in `score_sectors(cn-v1)` and persistent runs atomically write universe history, immutable constituent evidence, the run, and snapshots.

**Tech Stack:** Python 3.10+, Pydantic v2, SQLAlchemy, pandas, AkShare/EFinance through existing data-provider adapters, `concurrent.futures`, pytest, SQLite.

## Global Constraints

- Scope is A-share (`market="cn"`) observation enrichment only; do not add ETF selection, regimes, position policy, API/Web/Desktop, scheduling, alerts, outcomes, Hong Kong, catalyst/news scoring, LLM, or execution.
- Public/free data is the default; Tushare and TickFlow remain optional and no new credential is required.
- Preserve `cn-v1` weights and lifecycle thresholds; `catalyst_score` remains `None`, so maximum confidence is `0.9231`.
- Enrich at most 60 candidates within 180 seconds and at most 6 concurrent candidate jobs by default.
- Default benchmark identity is CSI All Share `000985`; fallback may change source but never benchmark code.
- Online enrichment is current-snapshot only; replay uses stored observations and never calls live providers.
- Percent values use percentage points; missing windows stay missing and never use shortened substitutes.
- Breadth and concentration require at least 5 valid same-date quotes and 80% constituent coverage.
- Constituent sets are immutable, content-addressed, deduplicated, and persisted in the same transaction as universe/run/snapshots.
- Non-persistent CLI execution must not initialize or read the database.
- Any user-visible CLI/config behavior change updates `.env.example`, `docs/market-radar.md`, and the flat `[Unreleased]` section in `docs/CHANGELOG.md`.

---

### Task 1: Capability And Configuration Contracts

**Files:**
- Create: `src/market_radar/capabilities.py`
- Modify: `src/config.py`
- Modify: `src/core/config_registry.py`
- Test: `tests/market_radar/test_capabilities.py`
- Test: `tests/test_run_market_radar.py`

**Interfaces:**
- Produces: `CapabilityResult[T]`, `BoardBar`, `BoardBarSeries`, `BoardFlow`, `BoardFlowSeries`, `ConstituentMembership`, `ConstituentQuote`, `ConstituentQuoteBatch`, and `MarketRadarEnrichmentConfig`.
- `MarketRadarEnrichmentConfig.from_runtime(limit: int, budget_seconds: int, max_concurrency: int) -> MarketRadarEnrichmentConfig` validates operational bounds and owns code-level formula defaults.

- [ ] **Step 1: Write failing contract tests**

```python
def test_capability_result_rejects_naive_time_and_non_finite_payloads():
    with pytest.raises(ValidationError):
        CapabilityResult[BoardBarSeries](
            capability="board_history", status="ok", data=BoardBarSeries(
                code="BK001", bars=[BoardBar(data_date=date(2026, 7, 22), close=float("nan"), traded_amount=1.0)]
            ), source="fixture", observed_at=datetime(2026, 7, 22),
            data_date=date(2026, 7, 22), bar_status="finalized",
            freshness_seconds=0, trace=(), error=None,
        )

def test_enrichment_config_uses_approved_defaults():
    assert MarketRadarEnrichmentConfig() == MarketRadarEnrichmentConfig(
        candidate_limit=60, total_budget_seconds=180, max_concurrency=6,
        constituent_min_count=5, constituent_coverage_ratio=0.80,
        price_divergence_threshold_pct=1.0,
        flow_divergence_threshold_pct=0.1, default_benchmark_code="000985",
    )
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_capabilities.py tests/test_run_market_radar.py -q`

Expected: collection fails because `src.market_radar.capabilities` and the three config fields do not exist.

- [ ] **Step 3: Implement immutable normalized contracts and bounded runtime settings**

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
    freshness_seconds: int = Field(ge=0)
    trace: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None

class MarketRadarEnrichmentConfig(FrozenModel):
    candidate_limit: int = Field(default=60, ge=1, le=200)
    total_budget_seconds: int = Field(default=180, ge=10, le=900)
    max_concurrency: int = Field(default=6, ge=1, le=16)
    constituent_min_count: int = 5
    constituent_coverage_ratio: float = 0.80
    price_divergence_threshold_pct: float = 1.0
    flow_divergence_threshold_pct: float = 0.1
    default_benchmark_code: str = "000985"
```

Add `market_radar_enrichment_limit`, `market_radar_enrichment_budget_seconds`, and `market_radar_enrichment_max_concurrency` to `Config`, parse them with the existing bounded integer helper, and register the environment names as hidden backend settings.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/market_radar/test_capabilities.py tests/test_run_market_radar.py tests/test_config_registry.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/capabilities.py src/config.py src/core/config_registry.py tests/market_radar/test_capabilities.py tests/test_run_market_radar.py tests/test_config_registry.py
git commit -m "feat: add Market Radar enrichment contracts"
```

### Task 2: Deterministic Candidate Selection

**Files:**
- Create: `src/market_radar/candidates.py`
- Test: `tests/market_radar/test_candidates.py`

**Interfaces:**
- Consumes: active `SectorDefinition` items, discovery `SectorObservation` items, and optional `RadarRunSnapshot`.
- Produces: `EnrichmentCandidate(sector: SectorDefinition, observation: SectorObservation, reasons: tuple[str, ...])` and `CandidateSelector.select(universe, observations, previous_snapshot, limit) -> tuple[EnrichmentCandidate, ...]`.

- [ ] **Step 1: Write failing priority, deduplication, and fairness tests**

```python
def test_selector_prioritizes_seeds_then_prior_states_then_round_robin_extremes():
    selected = CandidateSelector().select(
        universe=universe, observations=observations,
        previous_snapshot=previous_snapshot, limit=7,
    )
    assert [item.sector.sector_id for item in selected[:3]] == [
        "industry:seed", "industry:prior-leading", "concept:prior-improving"
    ]
    assert [item.reasons[-1] for item in selected[3:]] == [
        "current_industry_leader", "current_industry_laggard",
        "current_concept_leader", "current_concept_laggard",
    ]

def test_selector_merges_all_reasons_and_cuts_off_stably_at_sixty():
    selected = CandidateSelector().select(universe, observations, previous, limit=60)
    assert len(selected) == 60
    assert len({item.sector.sector_id for item in selected}) == 60
    assert selected == CandidateSelector().select(universe, observations, previous, limit=60)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_candidates.py -q`

Expected: import fails because `candidates.py` does not exist.

- [ ] **Step 3: Implement pure selection queues**

```python
class CandidateSelector:
    def select(
        self,
        universe: Sequence[SectorDefinition],
        observations: Sequence[SectorObservation],
        previous_snapshot: RadarRunSnapshot | None,
        limit: int,
    ) -> tuple[EnrichmentCandidate, ...]:
        by_id = self._canonical_candidates(universe, observations)
        reasons = self._collect_reasons(by_id, previous_snapshot)
        ordered_ids = self._priority_order(by_id, previous_snapshot)
        return tuple(
            EnrichmentCandidate(
                sector=by_id[sector_id].sector,
                observation=by_id[sector_id].observation,
                reasons=tuple(reasons[sector_id]),
            )
            for sector_id in ordered_ids[:limit]
        )
```

Missing daily returns sort after finite returns; every tie breaks on `sector_id`. Treat the configured active universe as seed definitions and synthesize definitions for discovery-only rows without mutating inputs.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/market_radar/test_candidates.py tests/market_radar/test_models.py -q`

Expected: all selected tests pass, including exact stable cutoff and reason aggregation.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/candidates.py tests/market_radar/test_candidates.py
git commit -m "feat: select Market Radar enrichment candidates"
```

### Task 3: Capability Provider Boundary And AkShare Adapters

**Files:**
- Create: `src/market_radar/capability_provider.py`
- Modify: `data_provider/base.py`
- Modify: `data_provider/akshare_fetcher.py`
- Test: `tests/market_radar/test_capability_provider.py`
- Test: `tests/market_radar/test_akshare_capabilities.py`

**Interfaces:**
- `BaseFetcher` optional methods return provider-native `pandas.DataFrame | list[dict[str, Any]] | None`: `get_sector_history(kind, name)`, `get_sector_flow(kind, name)`, and `get_sector_constituents(kind, name)`.
- `DataFetcherManager.get_market_radar_capability_with_meta(capability, *, kind, name) -> tuple[Any | None, list[dict[str, Any]], str]` preserves ordered trace and falls through empty/malformed results.
- `ProviderCapabilityAdapter` converts manager payloads into Task 1 normalized contracts without leaking provider-native column names.

- [ ] **Step 1: Write failing adapter and fallback tests**

```python
def test_manager_continues_after_empty_wrong_date_and_non_finite_payloads():
    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体"
    )
    assert data is valid_second_source_payload
    assert [item["result"] for item in trace] == ["empty", "invalid", "ok"]
    assert error == ""

def test_akshare_history_aliases_normalize_to_canonical_bars():
    result = adapter.fetch_board_history(sector, as_of)
    assert result.status == "ok"
    assert result.data.bars[-1].close == 1234.5
    assert result.data.bars[-1].traded_amount == 987654.0
    assert result.data_date == date(2026, 7, 22)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_capability_provider.py tests/market_radar/test_akshare_capabilities.py -q`

Expected: missing manager capability method and adapter imports.

- [ ] **Step 3: Add optional base methods, metadata fallback, and AkShare endpoint calls**

```python
class MarketRadarEnrichmentProvider(Protocol):
    def fetch_board_history(self, sector: SectorDefinition, as_of: datetime) -> CapabilityResult[BoardBarSeries]:
        raise NotImplementedError
    def fetch_benchmark_history(self, code: str, as_of: datetime) -> CapabilityResult[BoardBarSeries]:
        raise NotImplementedError
    def fetch_board_flow(self, sector: SectorDefinition, as_of: datetime) -> CapabilityResult[BoardFlowSeries]:
        raise NotImplementedError
    def fetch_constituents(self, sector: SectorDefinition, as_of: datetime) -> CapabilityResult[ConstituentMembership]:
        raise NotImplementedError
    def fetch_constituent_quotes(self, codes: tuple[str, ...], as_of: datetime) -> CapabilityResult[ConstituentQuoteBatch]:
        raise NotImplementedError
```

Use explicit alias maps for date, close, amount, net-main-flow, code, current price, previous close, and quote time. Reject booleans, non-finite numbers, unknown units, empty code sets, and wrong terminal dates. Bound trace/error strings and never include headers, cookies, credentials, or exception stacks.

- [ ] **Step 4: Run focused provider tests and existing ranking regressions**

Run: `python -m pytest tests/market_radar/test_capability_provider.py tests/market_radar/test_akshare_capabilities.py tests/market_radar/test_providers.py tests/test_akshare_history_timeout.py -q`

Expected: all selected tests pass and Phase 1 discovery behavior is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/capability_provider.py data_provider/base.py data_provider/akshare_fetcher.py tests/market_radar/test_capability_provider.py tests/market_radar/test_akshare_capabilities.py
git commit -m "feat: add Market Radar data capabilities"
```

### Task 4: Pure Observation Formula Builder

**Files:**
- Create: `src/market_radar/observation_builder.py`
- Test: `tests/market_radar/test_observation_builder.py`
- Test: `tests/market_radar/test_ranking.py`

**Interfaces:**
- Consumes: base `SectorObservation`, candidate reasons, benchmark code, Task 1 capability results, and `MarketRadarEnrichmentConfig`.
- Produces: `ObservationBuildResult(observation: SectorObservation, constituent_evidence: ConstituentEvidence | None)` from `CnSectorObservationBuilder.build`.

- [ ] **Step 1: Write failing formula and point-in-time tests**

```python
@pytest.mark.parametrize("window,expected", [(1, 2.0), (5, 10.0), (20, 25.0)])
def test_returns_require_exact_prior_finalized_sessions(window, expected):
    result = build_with_prices(window=window)
    assert getattr(result.observation, f"return_{window}d_pct") == pytest.approx(expected)

def test_breadth_and_concentration_publish_at_exact_coverage_boundary():
    result = build_with_constituents(total=10, valid=8, amounts=range(1, 9))
    assert (result.observation.up_count, result.observation.down_count, result.observation.flat_count) == (4, 3, 1)
    assert result.observation.concentration_ratio == pytest.approx(sum(range(4, 9)) / sum(range(1, 9)))

def test_divergence_is_false_at_noise_boundary_without_opposite_signs():
    assert build_with_price_and_flow(price_5d=1.0, flow_5d=0.1).observation.price_flow_divergence is False
    assert build_with_price_and_flow(price_5d=1.0, flow_5d=-0.1).observation.price_flow_divergence is True
```

Also cover: insufficient 1/5/20 history; aligned `000985` dates; normalized flow denominator; provisional versus finalized MA20/liquidity windows; sample log-return volatility; zero benchmark volatility; same-date constituent quote gate; stale/unavailable quality; base-value preservation on failed enrichment; exhaustive `missing_fields`; structured v2a provenance; and scorer confidence `0.9231` when every market field is present.

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_observation_builder.py tests/market_radar/test_ranking.py -q`

Expected: builder import fails.

- [ ] **Step 3: Implement pure formulas and provenance**

```python
class CnSectorObservationBuilder:
    def build(
        self, *, base: SectorObservation, candidate_reasons: tuple[str, ...],
        benchmark_code: str, board_history: CapabilityResult[BoardBarSeries],
        benchmark_history: CapabilityResult[BoardBarSeries],
        board_flow: CapabilityResult[BoardFlowSeries],
        membership: CapabilityResult[ConstituentMembership],
        quotes: CapabilityResult[ConstituentQuoteBatch],
        observed_at: datetime,
    ) -> ObservationBuildResult:
        metrics = self._compute_metrics(
            board_history=board_history, benchmark_history=benchmark_history,
            board_flow=board_flow, membership=membership, quotes=quotes,
        )
        observation = self._assemble_observation(
            base=base, metrics=metrics, candidate_reasons=candidate_reasons,
            benchmark_code=benchmark_code, observed_at=observed_at,
        )
        return ObservationBuildResult(
            observation=observation,
            constituent_evidence=self._constituent_evidence(membership),
        )
```

Keep each computation in a small private pure function. Align sector/benchmark by trading date, use exact windows, use sample standard deviation, compute freshness from price capabilities actually used, and recompute `missing_fields` from the final immutable observation. Set `source="market_radar_enrichment_v2a"` and serialize the approved versioned `raw_reference` structure.

- [ ] **Step 4: Run formula and scorer tests and confirm GREEN**

Run: `python -m pytest tests/market_radar/test_observation_builder.py tests/market_radar/test_ranking.py tests/market_radar/test_models.py -q`

Expected: all selected tests pass; the maximum Phase 2A score confidence assertion is `0.9231`.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/observation_builder.py tests/market_radar/test_observation_builder.py tests/market_radar/test_ranking.py
git commit -m "feat: build enriched sector observations"
```

### Task 5: Bounded Enrichment Orchestration

**Files:**
- Create: `src/market_radar/enrichment.py`
- Test: `tests/market_radar/test_enrichment.py`

**Interfaces:**
- Consumes: Task 2 candidates, Task 3 `MarketRadarEnrichmentProvider`, Task 4 builder, monotonic clock, and executor factory.
- Produces: `EnrichmentBatch(observations, constituent_evidence, trace)` from `MarketRadarEnricher.enrich(candidates, as_of)`.

- [ ] **Step 1: Write failing budget, concurrency, cache, and isolation tests**

```python
def test_enricher_fetches_benchmark_once_and_deduplicates_quote_codes():
    batch = enricher.enrich(candidates, as_of)
    assert provider.benchmark_calls == [("000985", as_of)]
    assert provider.quote_calls == [("000001", "600000", "600519")]
    assert len(batch.observations) == len(candidates)

def test_deadline_stops_submission_and_marks_unfinished_capabilities_unavailable():
    batch = deadline_enricher.enrich(candidates, as_of)
    assert executor.max_active <= 6
    assert provider.started_count < len(candidates)
    assert any(item.get("result") == "deadline_exceeded" for item in batch.trace)
```

Add a test where one capability fails but ranking inputs remain, and a test where three consecutive failures open a run-scoped capability/source circuit while the next source still gets tried.

- [ ] **Step 2: Run tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_enrichment.py -q`

Expected: enrichment module import fails.

- [ ] **Step 3: Implement deadline-aware candidate jobs**

```python
class MarketRadarEnricher:
    def enrich(
        self, candidates: Sequence[EnrichmentCandidate], as_of: datetime
    ) -> EnrichmentBatch:
        deadline = self.monotonic() + self.config.total_budget_seconds
        scheduler = _BoundedCandidateScheduler(
            executor=self.executor_factory(self.config.max_concurrency),
            max_active=self.config.max_concurrency,
            deadline=deadline,
            monotonic=self.monotonic,
        )
        completed, unfinished = scheduler.run(
            candidates, lambda candidate: self._enrich_candidate(candidate, as_of)
        )
        return self._materialize_batch(completed, unfinished, as_of)
```

Cache benchmark results by `(benchmark_code, data_date)` and quote batches by canonical code set. The orchestration layer catches provider failures and emits bounded unavailable evidence, but lets builder validation/programming errors abort the run before persistence.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/market_radar/test_enrichment.py tests/market_radar/test_observation_builder.py -q`

Expected: all selected tests pass without real network calls or sleeps.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/enrichment.py tests/market_radar/test_enrichment.py
git commit -m "feat: orchestrate bounded Market Radar enrichment"
```

### Task 6: Immutable Constituent Evidence And Atomic Persistence

**Files:**
- Modify: `src/storage.py`
- Modify: `src/market_radar/repository.py`
- Modify: `src/market_radar/replay.py`
- Test: `tests/market_radar/test_repository.py`
- Test: `tests/market_radar/test_replay.py`

**Interfaces:**
- Produces: SQLAlchemy `RadarConstituentSetRecord` and `RadarConstituentObservationRecord`.
- Produces: `canonical_constituent_set_key(market, sector_id, source, codes) -> str` and `MarketRadarRepository.save_enriched_run(sectors, evidence, snapshot) -> int`.
- Produces: repository read methods that resolve a referenced set for audit without any provider dependency.

- [ ] **Step 1: Write failing schema, identity, conflict, and rollback tests**

```python
def test_constituent_set_key_is_order_independent_and_deduplicated():
    first = canonical_constituent_set_key("cn", "industry:x", "akshare", ["600000", "000001", "600000"])
    second = canonical_constituent_set_key("cn", "industry:x", "akshare", ["000001", "600000"])
    assert first == second
    assert first.startswith("sha256:")

def test_same_date_source_conflicting_membership_rolls_back_whole_run(repository):
    repository.save_enriched_run(universe, [evidence_a], snapshot_a)
    with pytest.raises(ValueError, match="conflicting constituent membership"):
        repository.save_enriched_run(changed_universe, [evidence_b], snapshot_b)
    assert repository.get_run_by_key(snapshot_b.run_key) is None
    assert repository.list_universe(as_of) == original_universe
```

Inspect tables, unique constraints, indexes, and foreign keys. Verify identical sets reuse one row, old Phase 1 snapshots still load, and replay resolves stored evidence while a provider spy records zero calls.

- [ ] **Step 2: Run repository tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_repository.py tests/market_radar/test_replay.py -q`

Expected: new table/model imports and `save_enriched_run` are absent.

- [ ] **Step 3: Add additive models and one transaction boundary**

```python
def canonical_constituent_set_key(market, sector_id, source, codes):
    canonical = tuple(sorted(set(normalize_stock_code(code) for code in codes)))
    payload = json.dumps([market, sector_id, source, canonical], ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

def save_enriched_run(self, sectors, evidence, snapshot):
    def write(session):
        self._sync_universe_in_session(session, sectors)
        self._save_constituent_evidence_in_session(session, evidence)
        return self._save_run_in_session(session, snapshot)
    return self._run_idempotent_write(f"save_market_radar_enriched_run[{snapshot.run_key}]", write)
```

Store sorted unique codes JSON. Treat an existing `(market, sector_id, data_date, source)` row with another `set_key` as a domain `ValueError`, not an overwrite or integrity retry. Add tables via existing `Base.metadata.create_all` startup flow; no destructive migration.

- [ ] **Step 4: Run storage/repository/replay tests and confirm GREEN**

Run: `python -m pytest tests/market_radar/test_repository.py tests/market_radar/test_replay.py tests/test_storage.py -q`

Expected: all selected tests pass, including forced snapshot-insert rollback.

- [ ] **Step 5: Commit**

```bash
git add src/storage.py src/market_radar/repository.py src/market_radar/replay.py tests/market_radar/test_repository.py tests/market_radar/test_replay.py
git commit -m "feat: persist Market Radar constituent evidence"
```

### Task 7: Service And CLI Integration

**Files:**
- Modify: `src/market_radar/service.py`
- Modify: `scripts/run_market_radar.py`
- Modify: `src/market_radar/__init__.py`
- Test: `tests/market_radar/test_service.py`
- Test: `tests/market_radar/test_integration.py`
- Test: `tests/test_run_market_radar.py`

**Interfaces:**
- `MarketRadarService.__init__` accepts optional `enricher` and `candidate_selector`; `run(market="cn", as_of=None, trigger="manual", persist=True, discovery_only=False, previous_snapshot=None)` preserves existing defaults.
- Persistent runs may query only their existing repository for the latest prior snapshot; non-persistent runs use only explicitly supplied prior snapshots.
- CLI `--discovery-only` bypasses enrichment explicitly.

- [ ] **Step 1: Write failing integration and database-isolation tests**

```python
def test_service_discovers_selects_enriches_ranks_once_and_persists_atomically():
    snapshot = service.run(market="cn", as_of=as_of, persist=True)
    assert selector.calls == [(configured, discovery.observations, previous, 60)]
    assert enricher.calls == [(selector.result, as_of)]
    assert repository.enriched_writes == [(combined_universe, enricher.evidence, snapshot)]
    assert [item.sector_id for item in snapshot.sectors] == expected_rank_order

def test_non_persistent_enriched_run_never_reads_or_initializes_database():
    snapshot = build_service(persist=False).run(market="cn", persist=False)
    assert snapshot.sectors
    assert database_spy.calls == []

def test_discovery_only_cli_preserves_phase_one_path():
    assert main(["--discovery-only"]) == 0
    assert captured_run_kwargs["discovery_only"] is True
```

The offline end-to-end fixture must cover discovery -> deterministic selection -> mixed capability statuses -> formulas -> `cn-v1` ranking -> SQLite atomic persistence -> readback/replay with zero network calls.

- [ ] **Step 2: Run service and CLI tests and confirm RED**

Run: `python -m pytest tests/market_radar/test_service.py tests/market_radar/test_integration.py tests/test_run_market_radar.py -q`

Expected: constructor/run signatures and CLI switch are missing.

- [ ] **Step 3: Wire one ranking and persistence path**

```python
batch = self.provider.fetch(market, effective_as_of, universe)
previous = previous_snapshot
if persist and previous is None:
    previous = repository.get_latest_run(market="cn", before=effective_as_of)
if not discovery_only and self.enricher is not None:
    candidates = self.candidate_selector.select(universe, batch.observations, previous, self.enrichment_config.candidate_limit)
    enriched = self.enricher.enrich(candidates, effective_as_of)
    observations = merge_base_and_enriched(batch.observations, enriched.observations)
else:
    observations = batch.observations
sectors = score_sectors(observations, self.ranking_config)
```

Combine discovery/enrichment traces in the one snapshot. Ensure enrichment failures remain visible and partial, while builder validation aborts before `save_enriched_run`. Build the manager once in the CLI, compose the capability adapter/enricher only when not discovery-only, and keep repository construction conditional on `persist`.

- [ ] **Step 4: Run integration tests and confirm GREEN**

Run: `python -m pytest tests/market_radar tests/test_run_market_radar.py -q`

Expected: all Market Radar and CLI tests pass offline.

- [ ] **Step 5: Commit**

```bash
git add src/market_radar/service.py scripts/run_market_radar.py src/market_radar/__init__.py tests/market_radar/test_service.py tests/market_radar/test_integration.py tests/test_run_market_radar.py
git commit -m "feat: integrate Market Radar observation enrichment"
```

### Task 8: Configuration Documentation And Final Verification

**Files:**
- Modify: `.env.example`
- Modify: `docs/market-radar.md`
- Modify: `docs/INDEX.md` only if the existing index requires a new anchor or document entry
- Modify: `docs/CHANGELOG.md`
- Modify: tests touched by verification findings only when the failure is in Phase 2A scope

**Interfaces:**
- Documents the three optional settings, current-snapshot/replay rules, `--discovery-only`, partial-data semantics, constituent evidence, and exact out-of-scope boundary.

- [ ] **Step 1: Add documentation assertions before editing prose**

```python
def test_market_radar_docs_cover_phase_2a_operational_contract():
    text = Path("docs/market-radar.md").read_text(encoding="utf-8")
    for token in (
        "MARKET_RADAR_ENRICHMENT_LIMIT", "MARKET_RADAR_ENRICHMENT_BUDGET_SECONDS",
        "MARKET_RADAR_ENRICHMENT_MAX_CONCURRENCY", "--discovery-only", "000985",
    ):
        assert token in text
```

Place this assertion in `tests/market_radar/test_integration.py` or the repository's nearest existing documentation-contract test.

- [ ] **Step 2: Run the documentation assertion and confirm RED**

Run: `python -m pytest tests/market_radar/test_integration.py -q`

Expected: the first missing Phase 2A token fails.

- [ ] **Step 3: Update configuration examples, focused docs, and flat changelog**

Add the three settings with `60`, `180`, and `6` defaults to `.env.example`. Document source fallback, exact metric semantics, current-only online behavior, persisted replay, `--discovery-only`, and the non-persistent database guarantee in `docs/market-radar.md`. Add independent flat lines under `[Unreleased]`, for example:

```markdown
- [新功能] Market Radar 增加 A 股板块多周期行情、资金流、成分股广度与集中度观测增强
- [改进] Market Radar 增加有界候选筛选、超时并发控制和不可变成分股证据存储
- [文档] 补充 Market Radar Phase 2A 配置、数据时点、降级与回放说明
```

Do not add an `[Unreleased]` subsection heading and do not expand `README.md`.

- [ ] **Step 4: Run full verification with fresh evidence**

Run in order:

```bash
python -m pytest tests/market_radar tests/test_run_market_radar.py tests/test_config_registry.py tests/test_storage.py -q
python -m py_compile src/market_radar/capabilities.py src/market_radar/candidates.py src/market_radar/capability_provider.py src/market_radar/observation_builder.py src/market_radar/enrichment.py src/market_radar/service.py src/market_radar/repository.py scripts/run_market_radar.py
python scripts/check_ai_assets.py
git diff --check origin/main...HEAD
./scripts/ci_gate.sh
```

Expected: all pytest tests pass; compilation and AI asset validation exit 0; diff check is empty; CI gate exits 0. If PowerShell cannot execute the shell gate, run it through the available Git Bash/WSL and record the exact limitation if neither exists.

Perform a scope scan:

```bash
git diff --name-only origin/main...HEAD
git diff --stat origin/main...HEAD
rg -n "ETF selection|market regime|position policy|scheduler|alert|LLM|order" src/market_radar scripts/run_market_radar.py
```

Expected: no implementation leakage into API/Web/Desktop/scheduler/alerts/LLM/order paths. Any matches are comments/docs describing exclusions, not functionality.

Optional live smoke (never required for the deterministic gate): run one current AkShare candidate with persistence disabled and record source, provider date, duration, coverage, and missing fields in the PR body; do not commit transient output.

- [ ] **Step 5: Commit documentation and verification-related changes**

```bash
git add .env.example docs/market-radar.md docs/INDEX.md docs/CHANGELOG.md tests/market_radar/test_integration.py
git commit -m "docs: document Market Radar Phase 2A enrichment"
```

### Task 9: Independent Review, Push, And PR Handoff

**Files:**
- Modify only files required to resolve verified review findings within Phase 2A scope.

**Interfaces:**
- Produces a review-clean branch and a ready-for-review PR; does not merge without explicit user authorization.

- [ ] **Step 1: Request two-stage review**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. First verify complete spec compliance, then inspect correctness, failure semantics, point-in-time guarantees, transactionality, and test realism. Treat any finding by root cause across runtime, CLI, docs, storage, tests, and provider paths rather than patching only the cited line.

- [ ] **Step 2: Re-run affected focused tests after review fixes**

Run the smallest tests that reproduce every finding, then repeat the full Task 8 verification commands.

Expected: each regression test passes and the full gate remains green.

- [ ] **Step 3: Commit review fixes if needed**

```bash
git add --update
git commit -m "fix: address Market Radar Phase 2A review findings"
```

Skip this commit when review produces no code changes.

- [ ] **Step 4: Push and create the PR**

```bash
git push -u origin codex/market-radar-phase-2a
gh pr create --base main --head codex/market-radar-phase-2a --title "feat: enrich Market Radar A-share observations" --body-file .tmp/market-radar-phase-2a-pr.md
```

The PR body must match the final diff and include: goal/scope, formulas and point-in-time contract, source fallback, persistence transaction, compatibility, verification evidence, optional network evidence or its omission, risks, rollback, and screenshots. This is a CLI/data change with no report or Web UI visual delta, so explicitly state that screenshots are not applicable and use structured CLI JSON/test evidence instead.

- [ ] **Step 5: Monitor CI to a terminal state**

Run:

```bash
gh pr checks --watch
gh pr view --json mergeStateStatus,reviewDecision,statusCheckRollup,url
```

Expected: all blocking checks pass. Investigate failures from logs, fix root causes, refresh the PR body if scope/evidence changed, push, and monitor again. Stop at merge-ready handoff; do not merge without explicit authorization.
