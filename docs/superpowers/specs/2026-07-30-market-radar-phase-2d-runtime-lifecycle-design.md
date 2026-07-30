# Market Radar Phase 2D Runtime and Lifecycle Design

**Status:** Draft for written review

**Date:** 2026-07-30

**Scope:** A-share runtime scheduling, deterministic suggestion lifecycle, and immutable transition persistence

## 1. Purpose

Phase 2D turns the existing manually persisted Market Radar snapshot into a reliable shadow-mode runtime. It schedules A-share scans during supported market sessions, finalizes one end-of-day run, derives deterministic suggestion lifecycle transitions from persisted successful runs, and records every run decision and transition for later alerts, reports, and outcome evaluation.

This phase establishes runtime and persistence contracts only. It does not deliver notifications, daily reports, historical outcomes, calibration, Web mutation controls, Hong Kong support, or LLM-generated decisions.

## 2. Confirmed Product Decisions

- Market scope is A shares only (`market="cn"`).
- Intraday cadence is one eligible scan per 30-minute slot while the regular session is open.
- The lunch break is not an open-session scan window.
- One end-of-day finalization is eligible after the official session close for each trading date.
- Lifecycle decisions are deterministic and use only committed Market Radar snapshots.
- An upgrade requires two consecutive qualifying intraday observations or one qualifying finalized end-of-day observation.
- A risk deterioration can downgrade immediately.
- Scheduler and lifecycle behavior are opt-in and disabled by default.
- Phase 2D does not send notifications or render reports.
- LLM output cannot create, suppress, or mutate a lifecycle transition.

## 3. Architecture

### 3.1 Runtime Scheduler Integration

The existing `RuntimeSchedulerService` remains the owner of long-lived API/Web/Desktop scheduling. It is extended so the scheduler loop can run when either daily stock analysis or a registered background capability is enabled. Enabling Market Radar alone must not register or execute the daily stock-analysis task.

Market Radar registers one lightweight background tick. The tick runs every 60 seconds and delegates eligibility to a market-session policy; it does not execute a scan on every tick. This keeps calendar and idempotency rules in the Market Radar domain rather than encoding dozens of clock-time jobs in the generic scheduler.

The runtime scheduler continues to isolate background task failures so a Radar failure cannot stop the API process, the ordinary scheduled analysis, or the existing Event Monitor.

### 3.2 `MarketRadarSessionPolicy`

`MarketRadarSessionPolicy` is a pure component that receives a timezone-aware instant and trading-calendar result and returns exactly one decision:

- `intraday_due` with a canonical 30-minute slot;
- `eod_due` with the canonical trading date;
- `not_due` with a reason code.

It reuses `src/core/trading_calendar.py` for trading days, holidays, session open/close, and lunch-break boundaries. It never treats calendar failure as an open market. A calendar error produces `calendar_unavailable`, records a skipped attempt, and performs no provider call.

Each intraday slot is identified by market, trading date, session segment, and slot start. The morning and afternoon segments are separate, so the lunch break cannot shift the afternoon cadence. End-of-day eligibility begins only after the calendar's official close. Late process startup may run the still-missing end-of-day finalization for the current trading date, but it does not backfill missed intraday slots.

### 3.3 `MarketRadarRuntimeWorker`

The worker coordinates one eligible attempt:

1. Capture one timezone-aware decision instant.
2. Ask the session policy whether an intraday or end-of-day run is due.
3. Persist or load the deterministic attempt identity.
4. Acquire a Market Radar-specific non-blocking process lock.
5. Invoke `MarketRadarService.run(..., trigger="schedule")` with persistence enabled.
6. In the same successful snapshot transaction, derive and persist lifecycle state and transition records.
7. Mark the attempt successful only after the transaction commits.

The worker uses a Radar-specific lock rather than the global stock-analysis lock. The Radar and ordinary analysis pipelines may run concurrently because they have separate providers and persistence ownership; duplicate Radar execution remains prohibited. Provider-level concurrency stays bounded by existing Market Radar budgets.

### 3.4 Lifecycle Engine

`MarketRadarLifecycleEngine` is a pure deterministic component. It compares the current committed snapshot candidate set with the previous successful lifecycle state and produces the next state plus zero or one transition per sector suggestion.

Lifecycle states are:

```text
WATCHING -> CANDIDATE -> CONFIRMED -> ACTIVE -> DOWNGRADED -> EXITED
```

The engine operates on position-policy suggestions and their immediately related sector evidence rather than every ranked sector. A sector first becomes `WATCHING` when its state is `leading` or `improving` and confidence is at least `0.60`, but it is not yet an eligible position suggestion. It becomes `CANDIDATE` when the current position plan contains a supported ETF suggestion. A second consecutive qualifying intraday run, or one qualifying finalized end-of-day run, moves it to `CONFIRMED`. The next qualifying observation moves `CONFIRMED` to `ACTIVE`.

`ACTIVE` or `CONFIRMED` becomes `DOWNGRADED` immediately when any existing position invalidation condition is present, the sector state falls below `improving`, confidence falls below the policy threshold, the ETF becomes unsupported, or critical evidence becomes stale. A later successful run moves `DOWNGRADED` to `EXITED`. Requalification after `EXITED` starts a new lifecycle instance at `CANDIDATE`; it never rewrites the exited instance.

`WATCHING` and `CANDIDATE` can stop qualifying without producing an exited recommendation because they were never confirmed. The signal instance is closed with a terminal reason while its latest lifecycle state remains unchanged and auditable. Phase 2E will not treat this pre-confirmation closure as an alertable transition.

Consecutive confirmation requires the immediately previous successful eligible Radar run. A failed, skipped, duplicate, or calendar-unavailable attempt neither confirms nor breaks the sequence. A successful eligible run in which the sector no longer qualifies breaks the sequence.

### 3.5 Persistence

Phase 2D adds three auditable persistence concepts:

- `radar_run_attempts`: scheduled decision identity, trigger type, slot or trading date, decision time, status, reason code, linked run ID, and failure summary;
- `radar_signal_instances`: one lifecycle instance for a sector, its stable signal key, first/last run IDs, current state, version, validity, and terminal reason;
- `radar_signal_transitions`: immutable transition key, signal key, previous/new state, effective run ID, effective time, reason codes, and lifecycle rule version.

Attempt status is one of `started`, `succeeded`, `skipped`, or `failed`. A due attempt can be skipped with `duplicate_slot` or `radar_already_running`; calendar evaluation failure is recorded with `calendar_unavailable`. Routine 60-second decisions such as `market_closed`, `premarket`, `lunch_break`, `postmarket`, and `slot_not_due` update the worker's latest diagnostic state but are not persisted repeatedly. A successful existing intraday or end-of-day attempt is the durable evidence for `duplicate_slot` or `eod_already_finalized`; it is returned rather than replaced by a second skip row.

The scheduled attempt key is stable:

```text
cn:intraday:<trading-date>:<session-segment>:<slot-start>
cn:eod:<trading-date>
```

A calendar failure uses `cn:calendar-error:<local-date>:<30-minute-window>` so a prolonged outage remains visible without writing one row per scheduler tick. Attempt rows move from `started` to exactly one terminal status; lifecycle transitions remain append-only. A `started` attempt lease expires after 900 seconds, which exceeds the current bounded provider budgets and permits crash recovery without immediate concurrent re-entry.

The transition key is derived from signal key, effective run key, previous state, and new state. Same-key retries must be semantically identical. Conflicting retries are corruption errors.

The successful Radar run, signal instance changes, and transitions commit in one database transaction. No transition may reference an uncommitted run. A provider or lifecycle error rolls back the complete successful-run transaction and marks the separate attempt as failed. Existing manual and replay snapshots remain readable.

## 4. Data Contract Changes

`RadarRunSnapshot.trigger` gains additive value `schedule`; existing `manual` and `replay` meanings remain unchanged. Replay continues to make no live provider calls and cannot create runtime transitions.

Lifecycle outputs use version `cn-lifecycle-v1`. Every state record exposes:

- market and sector identity;
- signal key and lifecycle instance number;
- state and lifecycle version;
- first, previous, and current run keys;
- effective time and finalized/intraday confirmation source;
- ordered reason codes;
- confidence and selected ETF identity when applicable.

No lifecycle field is added to the immutable sector score itself. This prevents historical sector ranking bytes from changing and keeps lifecycle comparison separate from ranking computation.

## 5. Configuration

Phase 2D adds one operator setting:

```text
MARKET_RADAR_SCHEDULE_ENABLED=false
```

The default is disabled so upgrading an existing deployment cannot unexpectedly start provider traffic. The 30-minute cadence, 60-second scheduler tick, A-share session rules, confirmation count, and lifecycle thresholds are versioned domain policy in this phase rather than environment knobs.

The setting is added to `src/config.py`, `.env.example`, and `docs/market-radar.md`. It is not exposed as a Web mutation control in Phase 2D. Changing the environment-backed setting follows the existing runtime configuration reconciliation behavior.

## 6. Runtime Data Flow

```text
runtime scheduler tick
  -> MarketRadarSessionPolicy
  -> not due: return or record one canonical skip
  -> due: reserve scheduled attempt key
  -> acquire Radar lock
  -> collect and compute deterministic Radar snapshot
  -> load previous successful lifecycle state
  -> MarketRadarLifecycleEngine
  -> atomically persist snapshot + signal instances + transitions
  -> mark scheduled attempt succeeded
```

The previous baseline is selected from committed scheduled lifecycle runs and must be earlier than the current run. A failed run never becomes the baseline. Existing manual persisted runs continue to create snapshots only and do not update live lifecycle state or consume a scheduled slot in Phase 2D. Replay never updates live lifecycle state.

## 7. Error Handling

- Calendar unavailable: record `calendar_unavailable`; do not call providers.
- Closed market or lunch break: do not run; retain the last successful snapshot.
- Duplicate slot or end-of-day identity: return the existing terminal result without rerunning providers.
- Radar lock busy: record `radar_already_running`; a later tick may retry the same due slot until the slot closes.
- Provider or scoring failure: mark the attempt failed; keep the last committed snapshot and lifecycle state.
- Database failure before snapshot commit: roll back snapshot and transitions; mark the attempt failed when the independent attempt write is available.
- Attempt-status write failure: log the database error and do not claim success in runtime status.
- Process termination after reserving an attempt: a `started` attempt can be retried after its 900-second lease; semantic equality and transaction idempotency prevent duplicate state changes.
- Critical stale evidence: persist the Radar snapshot according to the current quality contract, prohibit lifecycle upgrade, and permit immediate downgrade.

Failure summaries store stable error categories and bounded messages, never secrets, provider tokens, or full raw payloads.

## 8. API and Web Impact

Phase 2D does not add mutation endpoints or Web controls. Existing read-only Phase 2C responses remain backward compatible because lifecycle records are stored separately and are not required by `/latest`, `/sectors`, or `/sectors/{sector_id}`.

Runtime status gains additive diagnostic fields for the Radar background task, but no existing field changes meaning. Recent transitions and alert-facing APIs are deferred to Phase 2E.

## 9. Testing

### Unit tests

- A-share holiday, premarket, morning session, lunch break, afternoon session, close, and post-close decisions;
- stable 30-minute intraday slot and end-of-day identities;
- two-run intraday confirmation and one-run finalized confirmation;
- immediate downgrade and next-run exit;
- failed/skipped attempts do not confirm or break a sequence;
- successful non-qualifying observations break a candidate sequence;
- exit and later re-entry create a new lifecycle instance;
- stale critical evidence cannot upgrade;
- identical inputs produce identical transitions and reason ordering.

### Repository tests

- attempt-key uniqueness and stale lease retry;
- signal-instance and transition-key uniqueness;
- same-key semantic retry succeeds;
- conflicting retry is rejected;
- snapshot, lifecycle state, and transition rollback together;
- legacy database migration and legacy snapshot reads.

### Runtime integration tests

- Market Radar-only enablement starts background scheduling without registering daily stock analysis;
- ordinary schedule, Event Monitor, and Radar tasks coexist;
- Radar re-entry protection does not block ordinary scheduled analysis;
- duplicate ticks and concurrent workers produce one committed run;
- provider, lifecycle, and database failures preserve the last successful state;
- restart after a stale `started` attempt retries without duplicate transitions.

### Compatibility verification

- existing Market Radar replay tests remain deterministic;
- Phase 2C API contract tests remain unchanged;
- backend CI gate passes;
- changed Python files compile;
- configuration examples and Market Radar documentation match runtime behavior.

## 10. Acceptance Criteria

1. With `MARKET_RADAR_SCHEDULE_ENABLED=false`, deployment behavior and provider traffic remain unchanged.
2. With only Market Radar scheduling enabled, no daily stock-analysis task is registered or executed.
3. During an A-share trading day, no more than one successful run is persisted for each eligible 30-minute slot and one end-of-day finalization.
4. Holidays, lunch breaks, calendar errors, duplicates, lock contention, and failures have explicit machine-readable outcomes.
5. Lifecycle upgrades follow the two-intraday-or-one-finalized confirmation rule; critical deterioration downgrades immediately.
6. Snapshot, lifecycle state, and transition persistence is atomic and idempotent.
7. Existing Phase 1 through Phase 2C snapshot, replay, API, and Web behavior remains compatible.
8. No notification, report, outcome, calibration, Hong Kong, broker, or LLM decision path is introduced.

## 11. Delivery Boundary

Phase 2D ends with scheduled immutable runs and auditable lifecycle transitions. Phase 2E consumes committed transition records to implement deduplicated Radar alerts and per-channel notification attempts. Phase 2F consumes the finalized end-of-day run to produce the structured daily report. Phase 3 starts only after these lifecycle transitions remain stable under shadow-mode operation.
