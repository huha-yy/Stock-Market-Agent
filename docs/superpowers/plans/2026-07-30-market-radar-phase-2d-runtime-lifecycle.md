# Market Radar Phase 2D Runtime and Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the A-share Market Radar once per eligible 30-minute session slot and once after close, while atomically persisting deterministic suggestion lifecycle transitions.

**Architecture:** A pure session policy converts the existing trading-calendar context into stable scheduled identities. A pure lifecycle engine evaluates committed scheduled snapshots, while the existing repository owns atomic snapshot/signal persistence and a runtime worker owns attempt leases and process re-entry protection. `RuntimeSchedulerService` hosts the worker as an opt-in background task without coupling it to daily stock analysis.

**Tech Stack:** Python 3.10+, Pydantic v2, SQLAlchemy, `exchange_calendars`, existing `schedule` runtime loop, pytest.

## Global Constraints

- Support only `market="cn"` and use `Asia/Shanghai` for scheduled identities.
- Use one eligible intraday run per 30-minute slot and one end-of-day run per trading date.
- Use a 60-second scheduler tick and a 900-second started-attempt lease.
- Keep `MARKET_RADAR_SCHEDULE_ENABLED=false` as the default and the only new environment setting.
- Never let a manual run or replay mutate live lifecycle state or consume a scheduled identity.
- Require two consecutive qualifying intraday runs or one qualifying finalized end-of-day observation for `CONFIRMED`.
- Permit immediate risk downgrade; move `DOWNGRADED` to `EXITED` on the next successful eligible scheduled run.
- Keep the LLM, alerts, notifications, reports, Web mutation controls, outcomes, calibration, Hong Kong support, and broker behavior out of scope.
- Preserve Phase 1 through Phase 2C snapshot JSON and read-only API compatibility.
- Use English commit messages and do not add `Co-Authored-By`.

## File Map

- Create `src/market_radar/session_policy.py`: pure 30-minute slot and end-of-day decision logic.
- Create `src/market_radar/lifecycle.py`: immutable lifecycle contracts and pure transition engine.
- Create `src/market_radar/runtime_worker.py`: attempt leasing, Radar lock, service invocation, and task diagnostics.
- Create `src/market_radar/factory.py`: reusable production service construction currently owned by the CLI script.
- Modify `src/core/trading_calendar.py`: expose fail-closed session/break bounds without duplicating calendar access.
- Modify `src/market_radar/models.py`: add the additive `schedule` run trigger.
- Modify `src/market_radar/service.py`: evaluate scheduled lifecycle data and select the atomic scheduled save path.
- Modify `src/market_radar/repository.py`: attempt operations, lifecycle reads, and atomic scheduled run writes.
- Modify `src/storage.py`: add attempt, signal-instance, and transition tables plus SQLite compatibility migration.
- Modify `src/services/runtime_scheduler.py`: register Radar background work and support background-only runtime scheduling.
- Modify `scripts/run_market_radar.py`: reuse the production factory while preserving CLI behavior.
- Modify `src/config.py`, `.env.example`, `docs/market-radar.md`, and `docs/CHANGELOG.md`: document the opt-in runtime.
- Create `tests/market_radar/test_session_policy.py`, `tests/market_radar/test_lifecycle.py`, and `tests/market_radar/test_runtime_worker.py`.
- Modify `tests/test_trading_calendar.py`, `tests/market_radar/test_repository.py`, `tests/market_radar/test_service.py`, `tests/market_radar/test_integration.py`, `tests/test_runtime_scheduler_service.py`, `tests/test_config_env_compat.py`, and `tests/test_run_market_radar.py`.

---

### Task 1: Fail-Closed A-Share Session Decisions

**Files:**
- Create: `src/market_radar/session_policy.py`
- Modify: `src/core/trading_calendar.py`
- Create: `tests/market_radar/test_session_policy.py`
- Modify: `tests/test_trading_calendar.py`

**Interfaces:**
- Consumes: `build_market_phase_context(market="cn", current_time=now)`.
- Produces: `get_market_session_bounds(market: str, current_time: datetime) -> MarketSessionBounds | None`, `RadarRunDecision`, and `MarketRadarSessionPolicy.decide(now: datetime) -> RadarRunDecision`.

- [ ] **Step 1: Write failing trading-calendar and session-policy tests**

```python
# tests/test_trading_calendar.py
def test_get_market_session_bounds_is_fail_closed(monkeypatch):
    monkeypatch.setattr(trading_calendar, "_XCALS_AVAILABLE", False)
    assert trading_calendar.get_market_session_bounds(
        "cn", datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
    ) is None


# tests/market_radar/test_session_policy.py
@pytest.mark.parametrize(
    ("local_time", "kind", "reason", "slot"),
    [
        ("2026-07-30T09:29:59+08:00", "not_due", "premarket", None),
        ("2026-07-30T09:30:00+08:00", "intraday_due", None, "09:30"),
        ("2026-07-30T09:59:59+08:00", "intraday_due", None, "09:30"),
        ("2026-07-30T10:00:00+08:00", "intraday_due", None, "10:00"),
        ("2026-07-30T11:45:00+08:00", "not_due", "lunch_break", None),
        ("2026-07-30T13:00:00+08:00", "intraday_due", None, "13:00"),
        ("2026-07-30T15:00:00+08:00", "eod_due", None, None),
    ],
)
def test_cn_session_decisions(local_time, kind, reason, slot, session_context):
    decision = MarketRadarSessionPolicy(context_builder=session_context).decide(
        datetime.fromisoformat(local_time)
    )
    assert decision.kind == kind
    assert decision.reason == reason
    assert (decision.slot_start.strftime("%H:%M") if decision.slot_start else None) == slot


def test_unknown_calendar_uses_bounded_error_identity(unknown_context):
    decision = MarketRadarSessionPolicy(context_builder=unknown_context).decide(
        datetime.fromisoformat("2026-07-30T10:17:00+08:00")
    )
    assert decision.kind == "calendar_unavailable"
    assert decision.attempt_key == "cn:calendar-error:2026-07-30:1000"
```

- [ ] **Step 2: Run the focused tests and verify the missing interfaces fail**

Run: `python -m pytest tests/test_trading_calendar.py::test_get_market_session_bounds_is_fail_closed tests/market_radar/test_session_policy.py -q`

Expected: FAIL because `get_market_session_bounds` and `MarketRadarSessionPolicy` do not exist.

- [ ] **Step 3: Expose calendar bounds and implement the pure policy**

```python
# src/core/trading_calendar.py
@dataclass(frozen=True)
class MarketSessionBounds:
    session_date: date
    open_at: datetime
    close_at: datetime
    break_start: Optional[datetime]
    break_end: Optional[datetime]


def get_market_session_bounds(
    market: str, current_time: datetime
) -> Optional[MarketSessionBounds]:
    if market not in MARKET_EXCHANGE or market not in MARKET_TIMEZONE or not _XCALS_AVAILABLE:
        return None
    market_now = get_market_now(market, current_time=current_time)
    try:
        cal = xcals.get_calendar(MARKET_EXCHANGE[market])
        if not cal.is_session(market_now.date()):
            return None
        session = cal.date_to_session(market_now.date(), direction="previous")
        open_at = _as_market_datetime(cal.session_open(session), MARKET_TIMEZONE[market])
        close_at = _as_market_datetime(cal.session_close(session), MARKET_TIMEZONE[market])
        if open_at is None or close_at is None:
            return None
        has_break = bool(cal.session_has_break(session)) if hasattr(cal, "session_has_break") else True
        break_start = _as_market_datetime(cal.session_break_start(session), MARKET_TIMEZONE[market]) if has_break else None
        break_end = _as_market_datetime(cal.session_break_end(session), MARKET_TIMEZONE[market]) if has_break else None
        return MarketSessionBounds(session.date(), open_at, close_at, break_start, break_end)
    except Exception as exc:
        logger.warning("trading_calendar.get_market_session_bounds fail-closed: %s", exc)
        return None
```

```python
# src/market_radar/session_policy.py
class RadarRunDecision(FrozenModel):
    kind: Literal["intraday_due", "eod_due", "not_due", "calendar_unavailable"]
    market: Literal["cn"] = "cn"
    decided_at: datetime
    trading_date: date
    attempt_key: str | None = None
    session_segment: Literal["morning", "afternoon"] | None = None
    slot_start: datetime | None = None
    reason: str | None = None


class MarketRadarSessionPolicy:
    def __init__(self, context_builder=build_market_phase_context, bounds_loader=get_market_session_bounds):
        self._context_builder = context_builder
        self._bounds_loader = bounds_loader

    def decide(self, now: datetime) -> RadarRunDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(ZoneInfo("Asia/Shanghai"))
        context = self._context_builder(market="cn", current_time=now)
        if context.phase == MarketPhase.UNKNOWN:
            window = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
            return RadarRunDecision(
                kind="calendar_unavailable", decided_at=now, trading_date=local.date(),
                attempt_key=f"cn:calendar-error:{local.date()}:{window:%H%M}",
                reason="calendar_unavailable",
            )
        reason_by_phase = {
            MarketPhase.NON_TRADING: "market_closed",
            MarketPhase.PREMARKET: "premarket",
            MarketPhase.LUNCH_BREAK: "lunch_break",
        }
        if context.phase in reason_by_phase:
            return RadarRunDecision(kind="not_due", decided_at=now, trading_date=local.date(), reason=reason_by_phase[context.phase])
        bounds = self._bounds_loader("cn", now)
        if bounds is None:
            window = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
            return RadarRunDecision(kind="calendar_unavailable", decided_at=now, trading_date=local.date(), attempt_key=f"cn:calendar-error:{local.date()}:{window:%H%M}", reason="calendar_unavailable")
        if context.phase == MarketPhase.POSTMARKET:
            return RadarRunDecision(kind="eod_due", decided_at=now, trading_date=bounds.session_date, attempt_key=f"cn:eod:{bounds.session_date}")
        segment = "afternoon" if bounds.break_end and local >= bounds.break_end else "morning"
        segment_start = bounds.break_end if segment == "afternoon" else bounds.open_at
        elapsed = int((local - segment_start).total_seconds() // 60)
        slot_start = segment_start + timedelta(minutes=(elapsed // 30) * 30)
        return RadarRunDecision(
            kind="intraday_due", decided_at=now, trading_date=bounds.session_date,
            attempt_key=f"cn:intraday:{bounds.session_date}:{segment}:{slot_start:%H%M}",
            session_segment=segment, slot_start=slot_start,
        )
```

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_trading_calendar.py tests/market_radar/test_session_policy.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the session policy**

```bash
git add src/core/trading_calendar.py src/market_radar/session_policy.py tests/test_trading_calendar.py tests/market_radar/test_session_policy.py
git commit -m "feat: add Market Radar session policy"
```

---

### Task 2: Deterministic Lifecycle Engine

**Files:**
- Create: `src/market_radar/lifecycle.py`
- Modify: `src/market_radar/models.py`
- Create: `tests/market_radar/test_lifecycle.py`

**Interfaces:**
- Consumes: `RadarRunSnapshot`, its ordered sector scores, position suggestions, and `run_kind: Literal["intraday", "eod"]`.
- Produces: `LifecycleContext`, `LifecycleSignal`, `LifecycleTransition`, `LifecycleEvaluation`, and `MarketRadarLifecycleEngine.evaluate(snapshot, context, run_kind)`.

- [ ] **Step 1: Write the lifecycle table tests**

```python
# tests/market_radar/test_lifecycle.py
@pytest.mark.parametrize(
    ("previous", "qualifying", "run_kind", "bar_status", "expected"),
    [
        (None, True, "intraday", "provisional", "candidate"),
        (None, True, "eod", "finalized", "confirmed"),
        ("candidate", True, "intraday", "provisional", "confirmed"),
        ("candidate", True, "eod", "finalized", "confirmed"),
        ("candidate", True, "eod", "provisional", "candidate"),
        ("confirmed", True, "intraday", "provisional", "active"),
        ("active", False, "intraday", "provisional", "downgraded"),
        ("downgraded", True, "intraday", "provisional", "exited"),
    ],
)
def test_lifecycle_transition_table(previous, qualifying, run_kind, bar_status, expected, snapshot_factory, context_factory):
    snapshot = snapshot_factory(qualifying=qualifying, bar_status=bar_status)
    context = context_factory(previous_state=previous)
    evaluation = MarketRadarLifecycleEngine().evaluate(snapshot, context, run_kind=run_kind)
    assert evaluation.signals[0].state == expected


def test_failed_attempt_is_not_an_engine_input():
    assert "attempt" not in inspect.signature(MarketRadarLifecycleEngine.evaluate).parameters


def test_reentry_after_exit_increments_instance(snapshot_factory, context_factory):
    evaluation = MarketRadarLifecycleEngine().evaluate(
        snapshot_factory(qualifying=True),
        context_factory(previous_state="exited", latest_instance=3),
        run_kind="intraday",
    )
    signal = evaluation.signals[0]
    assert signal.state == "candidate"
    assert signal.instance_number == 4
```

- [ ] **Step 2: Run lifecycle tests and verify failure**

Run: `python -m pytest tests/market_radar/test_lifecycle.py -q`

Expected: FAIL because `src.market_radar.lifecycle` does not exist.

- [ ] **Step 3: Add immutable lifecycle contracts and pure evaluation**

```python
# src/market_radar/lifecycle.py
LifecycleState = Literal["watching", "candidate", "confirmed", "active", "downgraded", "exited"]
RunKind = Literal["intraday", "eod"]


class LifecycleSignal(FrozenModel):
    signal_key: str
    market: Literal["cn"] = "cn"
    sector_id: str
    instance_number: int = Field(ge=1)
    state: LifecycleState
    lifecycle_version: Literal["cn-lifecycle-v1"] = "cn-lifecycle-v1"
    first_run_key: str
    previous_run_key: str | None = None
    current_run_key: str
    effective_at: datetime
    qualifying_streak: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    etf_code: str | None = None
    reason_codes: tuple[str, ...] = ()
    closed_at: datetime | None = None
    terminal_reason: str | None = None


class LifecycleTransition(FrozenModel):
    transition_key: str
    signal_key: str
    previous_state: LifecycleState | None
    new_state: LifecycleState
    effective_run_key: str
    effective_at: datetime
    lifecycle_version: Literal["cn-lifecycle-v1"] = "cn-lifecycle-v1"
    reason_codes: tuple[str, ...] = ()


class LifecycleContext(FrozenModel):
    open_signals: tuple[LifecycleSignal, ...] = ()
    latest_instance_by_sector: Mapping[str, int] = Field(default_factory=dict)


class LifecycleEvaluation(FrozenModel):
    run_key: str
    signals: tuple[LifecycleSignal, ...]
    transitions: tuple[LifecycleTransition, ...]


class MarketRadarLifecycleEngine:
    def evaluate(self, snapshot: RadarRunSnapshot, context: LifecycleContext, *, run_kind: RunKind) -> LifecycleEvaluation:
        previous = {item.sector_id: item for item in context.open_signals}
        sectors = {item.sector_id: item for item in snapshot.sectors}
        suggestions = {
            item.sector_id: item
            for item in (snapshot.position_plan.suggestions if snapshot.position_plan else ())
        }
        sector_ids = sorted(set(previous) | set(sectors))
        signals: list[LifecycleSignal] = []
        transitions: list[LifecycleTransition] = []
        for sector_id in sector_ids:
            sector = sectors.get(sector_id)
            old = previous.get(sector_id)
            suggestion = suggestions.get(sector_id)
            watch = bool(sector and sector.state in {"leading", "improving"} and sector.confidence >= 0.60 and sector.quality not in {"stale", "unavailable"})
            candidate = watch and suggestion is not None
            risk_down = bool(old and old.state in {"confirmed", "active"} and not candidate)
            finalized = bool(sector and sector.observation.bar_status == "finalized")
            new_state, close_signal = _next_state(old, watch=watch, candidate=candidate, risk_down=risk_down, eod_confirmed=run_kind == "eod" and finalized)
            if new_state is None:
                continue
            signal = _build_signal(
                snapshot, sector_id, sector, suggestion, old, new_state,
                context.latest_instance_by_sector, close_signal=close_signal,
            )
            signals.append(signal)
            if old is None or old.state != signal.state:
                transitions.append(_build_transition(signal, old))
        return LifecycleEvaluation(run_key=snapshot.run_key, signals=tuple(signals), transitions=tuple(transitions))
```

Add the helpers with the complete transition table below; `_build_signal` copies the selected ETF/confidence from the current snapshot, increments the instance after an exited signal, sets `closed_at` only for pre-confirmation loss or `exited`, and computes keys with SHA-256 over canonical JSON:

```python
def _next_state(
    old: LifecycleSignal | None, *, watch: bool, candidate: bool,
    risk_down: bool, eod_confirmed: bool,
) -> tuple[LifecycleState | None, bool]:
    if old is None or old.state == "exited":
        if candidate:
            return ("confirmed" if eod_confirmed else "candidate"), False
        if watch:
            return "watching", False
        return None, False
    if old.state == "downgraded":
        return "exited", True
    if old.state in {"confirmed", "active"} and risk_down:
        return "downgraded", False
    if old.state == "confirmed":
        return "active", False
    if old.state == "active":
        return "active", False
    if old.state == "candidate":
        if candidate and (eod_confirmed or old.qualifying_streak >= 1):
            return "confirmed", False
        return ("candidate", not candidate)
    if old.state == "watching":
        if candidate:
            return ("confirmed" if eod_confirmed else "candidate"), False
        return ("watching", not watch)
    raise ValueError(f"unsupported lifecycle state: {old.state}")


def _stable_key(prefix: str, payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha256(rendered.encode('utf-8')).hexdigest()}"


def _build_signal(
    snapshot: RadarRunSnapshot,
    sector_id: str,
    sector: SectorScore | None,
    suggestion: PositionSuggestion | None,
    old: LifecycleSignal | None,
    new_state: LifecycleState,
    latest_instances: Mapping[str, int],
    *,
    close_signal: bool,
) -> LifecycleSignal:
    new_instance = old is None or old.state == "exited"
    instance_number = (
        int(latest_instances.get(sector_id, 0)) + 1
        if new_instance
        else old.instance_number
    )
    signal_key = f"cn:{sector_id}:{instance_number}"
    streak = (
        old.qualifying_streak + 1
        if old is not None and suggestion is not None and not close_signal
        else (1 if suggestion is not None and not close_signal else 0)
    )
    reason_set = set()
    if close_signal:
        reason_set.add("preconfirmation_no_longer_qualifies")
    elif new_state == "downgraded":
        reason_set.add("position_suggestion_no_longer_qualifies")
    elif new_state == "exited":
        reason_set.add("downgrade_confirmed")
    elif new_state == "confirmed":
        reason_set.add("qualification_confirmed")
    reasons = tuple(sorted(reason_set))
    return LifecycleSignal(
        signal_key=signal_key,
        sector_id=sector_id,
        instance_number=instance_number,
        state=new_state,
        first_run_key=snapshot.run_key if new_instance else old.first_run_key,
        previous_run_key=old.current_run_key if old else None,
        current_run_key=snapshot.run_key,
        effective_at=snapshot.as_of,
        qualifying_streak=streak,
        confidence=(suggestion.joint_confidence if suggestion else sector.confidence if sector else old.confidence),
        etf_code=(suggestion.etf_code if suggestion else old.etf_code if old else None),
        reason_codes=reasons,
        closed_at=snapshot.as_of if close_signal or new_state == "exited" else None,
        terminal_reason=("preconfirmation_no_longer_qualifies" if close_signal else "lifecycle_exited" if new_state == "exited" else None),
    )


def _build_transition(signal: LifecycleSignal, old: LifecycleSignal | None) -> LifecycleTransition:
    payload = {
        "signal_key": signal.signal_key,
        "run_key": signal.current_run_key,
        "previous": old.state if old else None,
        "new": signal.state,
    }
    return LifecycleTransition(
        transition_key=_stable_key("radar-transition", payload),
        signal_key=signal.signal_key,
        previous_state=old.state if old else None,
        new_state=signal.state,
        effective_run_key=signal.current_run_key,
        effective_at=signal.effective_at,
        reason_codes=signal.reason_codes,
    )
```

Return no signal when `_next_state` returns `None`; return a closed signal but no transition when the boolean close flag is true. Preserve ordered sector traversal and sort transitions by `(signal_key, transition_key)` before returning.

Also change `RadarRunSnapshot.trigger` in `src/market_radar/models.py` to:

```python
trigger: Literal["manual", "schedule", "replay"]
```

- [ ] **Step 4: Run lifecycle and model tests**

Run: `python -m pytest tests/market_radar/test_lifecycle.py tests/market_radar/test_models.py -q`

Expected: PASS, including deterministic repeated evaluation.

- [ ] **Step 5: Commit the lifecycle engine**

```bash
git add src/market_radar/lifecycle.py src/market_radar/models.py tests/market_radar/test_lifecycle.py
git commit -m "feat: add Market Radar lifecycle engine"
```

---

### Task 3: Attempt and Lifecycle Persistence

**Files:**
- Modify: `src/storage.py`
- Modify: `src/market_radar/repository.py`
- Modify: `tests/market_radar/test_repository.py`
- Modify: `tests/market_radar/test_integration.py`

**Interfaces:**
- Consumes: `RadarRunDecision`, `LifecycleContext`, and `LifecycleEvaluation` from Tasks 1-2.
- Produces: `ScheduledAttempt`, `AttemptReservation`, `reserve_scheduled_attempt`, `finish_scheduled_attempt`, `load_lifecycle_context`, and `save_scheduled_enriched_run`.

- [ ] **Step 1: Write failing schema, lease, semantic retry, and rollback tests**

```python
# tests/market_radar/test_repository.py
def test_reserve_attempt_reuses_terminal_identity(repo, intraday_decision, persisted_run_id):
    first = repo.reserve_scheduled_attempt(intraday_decision, lease_seconds=900)
    repo.finish_scheduled_attempt(first.attempt_key, status="succeeded", run_id=persisted_run_id)
    second = repo.reserve_scheduled_attempt(intraday_decision, lease_seconds=900)
    assert second.acquired is False
    assert second.status == "succeeded"
    assert second.run_id == persisted_run_id


def test_started_attempt_can_only_be_reclaimed_after_lease(repo, intraday_decision, clock):
    first = repo.reserve_scheduled_attempt(intraday_decision, lease_seconds=900, now=clock.now())
    assert repo.reserve_scheduled_attempt(intraday_decision, lease_seconds=900, now=clock.after(seconds=899)).acquired is False
    assert repo.reserve_scheduled_attempt(intraday_decision, lease_seconds=900, now=clock.after(seconds=900)).acquired is True


def test_scheduled_snapshot_and_transitions_roll_back_together(repo, scheduled_bundle, monkeypatch):
    monkeypatch.setattr(repo, "_save_lifecycle_in_session", lambda *args: (_ for _ in ()).throw(RuntimeError("transition failed")))
    with pytest.raises(RuntimeError, match="transition failed"):
        repo.save_scheduled_enriched_run(**scheduled_bundle)
    assert repo.get_run_by_key(scheduled_bundle["snapshot"].run_key) is None
    assert repo.load_lifecycle_context().open_signals == ()
```

- [ ] **Step 2: Run repository tests and verify missing methods fail**

Run: `python -m pytest tests/market_radar/test_repository.py -q`

Expected: FAIL because scheduled-attempt and lifecycle repository methods do not exist.

- [ ] **Step 3: Add SQLAlchemy records and SQLite compatibility migration**

```python
# src/storage.py
class RadarRunAttemptRecord(Base):
    __tablename__ = "radar_run_attempts"
    attempt_key = Column(String(192), primary_key=True)
    market = Column(String(16), nullable=False, index=True)
    trigger_type = Column(String(32), nullable=False)
    trading_date = Column(Date, nullable=False, index=True)
    decided_at = Column(DateTime, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False)
    reason_code = Column(String(96), nullable=True)
    run_id = Column(Integer, ForeignKey("radar_runs.id", ondelete="SET NULL"), nullable=True)
    failure_category = Column(String(96), nullable=True)
    failure_summary = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)
    updated_at = Column(DateTime, default=utc_naive_now, nullable=False)


class RadarSignalInstanceRecord(Base):
    __tablename__ = "radar_signal_instances"
    signal_key = Column(String(192), primary_key=True)
    market = Column(String(16), nullable=False, index=True)
    sector_id = Column(String(160), nullable=False, index=True)
    instance_number = Column(Integer, nullable=False)
    state = Column(String(32), nullable=False)
    lifecycle_version = Column(String(32), nullable=False)
    first_run_id = Column(Integer, ForeignKey("radar_runs.id"), nullable=False)
    last_run_id = Column(Integer, ForeignKey("radar_runs.id"), nullable=False)
    signal_json = Column(Text, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)
    updated_at = Column(DateTime, default=utc_naive_now, nullable=False)


class RadarSignalTransitionRecord(Base):
    __tablename__ = "radar_signal_transitions"
    transition_key = Column(String(224), primary_key=True)
    signal_key = Column(String(192), ForeignKey("radar_signal_instances.signal_key"), nullable=False, index=True)
    effective_run_id = Column(Integer, ForeignKey("radar_runs.id"), nullable=False, index=True)
    previous_state = Column(String(32), nullable=True)
    new_state = Column(String(32), nullable=False)
    transition_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utc_naive_now, nullable=False)
```

Add `_ensure_market_radar_lifecycle_schema()` beside `_ensure_market_radar_snapshot_position_schema()` and call it after `Base.metadata.create_all`. It must add missing indexes with `CREATE INDEX IF NOT EXISTS` and must never drop or rewrite existing Radar tables.

- [ ] **Step 4: Implement attempt lease and lifecycle repository methods**

```python
# src/market_radar/repository.py
@dataclass(frozen=True)
class AttemptReservation:
    attempt_key: str
    acquired: bool
    status: str
    run_id: int | None = None


def reserve_scheduled_attempt(self, decision: RadarRunDecision, *, lease_seconds: int = 900, now: datetime | None = None) -> AttemptReservation:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_expires = current + timedelta(seconds=lease_seconds)
    def write(session: Any) -> AttemptReservation:
        row = session.get(RadarRunAttemptRecord, decision.attempt_key)
        if row is None:
            row = RadarRunAttemptRecord(
                attempt_key=decision.attempt_key, market="cn",
                trigger_type=decision.kind, trading_date=decision.trading_date,
                decided_at=to_utc_naive_datetime(decision.decided_at),
                lease_expires_at=to_utc_naive_datetime(lease_expires), status="started",
            )
            session.add(row)
            session.flush()
            return AttemptReservation(row.attempt_key, True, "started", None)
        expired = row.status == "started" and row.lease_expires_at <= to_utc_naive_datetime(current)
        if expired:
            row.decided_at = to_utc_naive_datetime(decision.decided_at)
            row.lease_expires_at = to_utc_naive_datetime(lease_expires)
            row.updated_at = to_utc_naive_datetime(current)
            return AttemptReservation(row.attempt_key, True, "started", row.run_id)
        return AttemptReservation(row.attempt_key, False, row.status, row.run_id)
    return self._run_idempotent_write(f"reserve_radar_attempt[{decision.attempt_key}]", write)


def finish_scheduled_attempt(
    self, attempt_key: str, *, status: Literal["succeeded", "skipped", "failed"],
    run_id: int | None = None, reason_code: str | None = None,
    failure_category: str | None = None, failure_summary: str | None = None,
) -> None:
    values = {
        "status": status, "run_id": run_id, "reason_code": reason_code,
        "failure_category": failure_category,
        "failure_summary": (failure_summary[:512] if failure_summary else None),
    }
    def write(session: Any) -> None:
        row = session.get(RadarRunAttemptRecord, attempt_key)
        if row is None:
            raise ValueError(f"unknown scheduled attempt: {attempt_key}")
        actual = {key: getattr(row, key) for key in values}
        if row.status != "started":
            if actual == values:
                return
            raise ValueError(f"scheduled attempt is already terminal: {attempt_key}")
        for key, value in values.items():
            setattr(row, key, value)
        row.lease_expires_at = None
        row.updated_at = utc_naive_now()
    self._run_idempotent_write(f"finish_radar_attempt[{attempt_key}]", write)


def load_lifecycle_context(self) -> LifecycleContext:
    with self.db.get_session() as session:
        rows = session.execute(
            select(RadarSignalInstanceRecord).order_by(
                RadarSignalInstanceRecord.sector_id,
                RadarSignalInstanceRecord.instance_number,
            )
        ).scalars().all()
        latest: dict[str, int] = {}
        open_signals: list[LifecycleSignal] = []
        for row in rows:
            latest[row.sector_id] = max(latest.get(row.sector_id, 0), row.instance_number)
            if row.closed_at is None:
                open_signals.append(LifecycleSignal.model_validate_json(row.signal_json))
        return LifecycleContext(
            open_signals=tuple(open_signals),
            latest_instance_by_sector=latest,
        )


def save_scheduled_enriched_run(
    self, sectors: list[SectorDefinition], evidence: Sequence[ConstituentEvidence],
    etf_observations: Sequence[EtfObservation], snapshot: RadarRunSnapshot,
    evaluation: LifecycleEvaluation,
) -> int:
    if snapshot.trigger != "schedule" or evaluation.run_key != snapshot.run_key:
        raise ValueError("scheduled snapshot and lifecycle evaluation must match")
    self._validate_universe(sectors)
    self._validate_snapshot_traceability(snapshot)
    validated_observations = tuple(etf_observations)
    self._validate_etf_observations(snapshot, validated_observations)
    validated_evidence = self._validate_enriched_traceability(snapshot, evidence)
    def write(session: Any) -> int:
        existing_id = session.execute(
            select(RadarRunRecord.id).where(RadarRunRecord.run_key == snapshot.run_key)
        ).scalar_one_or_none()
        if existing_id is not None:
            self._assert_run_semantically_equal_in_session(
                session, int(existing_id), snapshot, validated_observations
            )
            self._save_lifecycle_in_session(session, int(existing_id), evaluation)
            return int(existing_id)
        self._sync_universe_in_session(session, sectors)
        self._save_constituent_evidence_in_session(session, validated_evidence)
        self._validate_effective_constituent_evidence_in_session(
            session, validated_evidence, snapshot
        )
        run_id = self._save_run_in_session(session, snapshot, validated_observations)
        self._save_lifecycle_in_session(session, run_id, evaluation)
        return run_id
    return self._run_idempotent_write(f"save_scheduled_market_radar_run[{snapshot.run_key}]", write)
```

`_save_lifecycle_in_session` must compare existing signal/transition JSON for semantic equality before accepting a retry, translate run keys to IDs inside the same session, update only the current signal-instance row, and insert transitions append-only. Bound `failure_summary` to 512 characters before persistence.

- [ ] **Step 5: Run repository and integration tests**

Run: `python -m pytest tests/market_radar/test_repository.py tests/market_radar/test_integration.py -q`

Expected: PASS with legacy schema/read tests unchanged.

- [ ] **Step 6: Commit persistence**

```bash
git add src/storage.py src/market_radar/repository.py tests/market_radar/test_repository.py tests/market_radar/test_integration.py
git commit -m "feat: persist Market Radar lifecycle transitions"
```

---

### Task 4: Scheduled Service Orchestration and Reusable Factory

**Files:**
- Create: `src/market_radar/factory.py`
- Modify: `src/market_radar/service.py`
- Modify: `scripts/run_market_radar.py`
- Modify: `tests/market_radar/test_service.py`
- Modify: `tests/test_run_market_radar.py`

**Interfaces:**
- Consumes: lifecycle engine/repository methods from Tasks 2-3.
- Produces: `build_market_radar_service(*, persist: bool, discovery_only: bool = False)`, and `MarketRadarService.run(..., trigger="schedule", schedule_kind="intraday" | "eod")`.

- [ ] **Step 1: Write failing schedule orchestration and CLI compatibility tests**

```python
# tests/market_radar/test_service.py
def test_schedule_run_requires_persistence_and_schedule_kind(service):
    with pytest.raises(ValueError, match="schedule runs require persistence"):
        service.run(trigger="schedule", persist=False, schedule_kind="intraday")
    with pytest.raises(ValueError, match="schedule_kind"):
        service.run(trigger="schedule", persist=True)


def test_schedule_run_saves_snapshot_and_lifecycle_once(service, repository):
    result = service.run(trigger="schedule", persist=True, schedule_kind="intraday")
    repository.save_scheduled_enriched_run.assert_called_once()
    assert result.trigger == "schedule"


def test_manual_run_does_not_load_or_save_lifecycle(service, repository):
    service.run(trigger="manual", persist=True)
    repository.load_lifecycle_context.assert_not_called()
    repository.save_scheduled_enriched_run.assert_not_called()
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/market_radar/test_service.py tests/test_run_market_radar.py -q`

Expected: FAIL because schedule orchestration and the reusable factory do not exist.

- [ ] **Step 3: Move production construction into a reusable factory**

```python
# src/market_radar/factory.py
ROOT = Path(__file__).resolve().parents[2]


def build_market_radar_service(
    *, persist: bool, discovery_only: bool = False,
    repository: MarketRadarRepository | None = None,
) -> MarketRadarService:
    config = get_config()
    manager = DataFetcherManager()
    adapter = ProviderCapabilityAdapter(manager)
    enrichment_config = MarketRadarEnrichmentConfig.from_runtime(
        config.market_radar_enrichment_limit,
        config.market_radar_enrichment_budget_seconds,
        config.market_radar_enrichment_max_concurrency,
    )
    return MarketRadarService(
        universe_loader=UniverseLoader(ROOT / "src/data/market_radar/a_share_etfs.yaml"),
        provider=LegacyRankingProvider(manager, limit=config.market_radar_provider_limit),
        repository=(repository or MarketRadarRepository()) if persist else None,
        ranking_config=RankingConfig(
            scoring_version=config.market_radar_scoring_version,
            stale_after_seconds=config.market_radar_stale_after_seconds,
        ),
        enricher=None if discovery_only else MarketRadarEnricher(provider=adapter, config=enrichment_config),
        candidate_selector=None if discovery_only else CandidateSelector(),
        enrichment_config=enrichment_config,
        etf_collector=None if discovery_only else MarketRadarEtfCollector(provider=adapter, config=EtfCollectionConfig()),
        etf_policy_config=EtfPolicyConfig(),
        regime_config=RegimeConfig(),
        position_policy_config=PositionPolicyConfig(),
        lifecycle_engine=MarketRadarLifecycleEngine(),
    )
```

Change `scripts/run_market_radar.py` to import this factory and keep a compatibility `build_service = build_market_radar_service` alias so existing tests and operator imports do not break.

- [ ] **Step 4: Add the scheduled atomic save path to the service**

```python
# src/market_radar/service.py
def run(
    self, *, market: str = "cn", as_of: datetime | None = None,
    trigger: Literal["manual", "schedule", "replay"] = "manual",
    schedule_kind: Literal["intraday", "eod"] | None = None,
    persist: bool = True, discovery_only: bool = False,
    previous_snapshot: RadarRunSnapshot | None = None,
) -> RadarRunSnapshot:
    if trigger == "schedule" and (not persist or schedule_kind is None):
        raise ValueError("schedule runs require persistence and schedule_kind")
    if trigger != "schedule" and schedule_kind is not None:
        raise ValueError("schedule_kind is only valid for schedule runs")
    # Keep existing collection and snapshot construction unchanged.
    if persist and trigger == "schedule":
        context = repository.load_lifecycle_context()
        evaluation = self.lifecycle_engine.evaluate(snapshot, context, run_kind=schedule_kind)
        run_id = repository.save_scheduled_enriched_run(
            sorted_universe, enrichment.constituent_evidence if enrichment else (),
            etf_observations, snapshot, evaluation,
        )
    elif persist and enrichment is None and not phase2b_enabled:
        run_id = repository.save_run_with_universe(sorted_universe, snapshot)
    elif persist:
        run_id = repository.save_enriched_run(
            sorted_universe,
            enrichment.constituent_evidence if enrichment is not None else (),
            etf_observations=etf_observations,
            snapshot=snapshot,
        )
```

Require full Phase 2B enrichment for schedule runs so lifecycle never silently runs on discovery-only snapshots. After either save path, retain the existing `repository.get_run(run_id)` verification and return the reconstructed immutable snapshot.

- [ ] **Step 5: Run service, replay, and CLI tests**

Run: `python -m pytest tests/market_radar/test_service.py tests/market_radar/test_replay.py tests/test_run_market_radar.py -q`

Expected: PASS and manual CLI JSON remains unchanged except that the schema accepts the unused additive trigger value.

- [ ] **Step 6: Commit orchestration**

```bash
git add src/market_radar/factory.py src/market_radar/service.py scripts/run_market_radar.py tests/market_radar/test_service.py tests/test_run_market_radar.py
git commit -m "feat: orchestrate scheduled Market Radar runs"
```

---

### Task 5: Runtime Worker and Attempt Diagnostics

**Files:**
- Create: `src/market_radar/runtime_worker.py`
- Create: `tests/market_radar/test_runtime_worker.py`

**Interfaces:**
- Consumes: `MarketRadarSessionPolicy`, repository attempt methods, and `build_market_radar_service`.
- Produces: `MarketRadarRuntimeWorker.run_once() -> dict[str, Any]` and `MarketRadarRuntimeWorker.status() -> dict[str, Any]`.

- [ ] **Step 1: Write failing due/not-due/duplicate/busy/failure tests**

```python
# tests/market_radar/test_runtime_worker.py
def test_not_due_does_not_touch_repository(worker, policy, repository):
    policy.decide.return_value = decision(kind="not_due", reason="lunch_break")
    assert worker.run_once()["reason"] == "lunch_break"
    repository.reserve_scheduled_attempt.assert_not_called()


def test_due_run_finishes_attempt_after_service_commit(worker, repository, service):
    result = worker.run_once()
    service.run.assert_called_once_with(market="cn", trigger="schedule", schedule_kind="intraday", persist=True)
    repository.finish_scheduled_attempt.assert_called_once_with(
        result["attempt_key"], status="succeeded", run_id=result["run_id"]
    )


def test_duplicate_terminal_attempt_does_not_call_provider(worker, repository, service):
    repository.reserve_scheduled_attempt.return_value = AttemptReservation("key", False, "succeeded", 12)
    assert worker.run_once()["reason"] == "duplicate_slot"
    service.run.assert_not_called()


def test_worker_failure_is_bounded_and_fail_open(worker, repository, service):
    service.run.side_effect = RuntimeError("provider token=secret")
    result = worker.run_once()
    assert result["status"] == "failed"
    kwargs = repository.finish_scheduled_attempt.call_args.kwargs
    assert len(kwargs["failure_summary"]) <= 512
    assert "token=secret" not in kwargs["failure_summary"]
```

- [ ] **Step 2: Run worker tests and verify failure**

Run: `python -m pytest tests/market_radar/test_runtime_worker.py -q`

Expected: FAIL because `MarketRadarRuntimeWorker` does not exist.

- [ ] **Step 3: Implement the worker with a Radar-specific lock**

```python
# src/market_radar/runtime_worker.py
_RADAR_RUNTIME_LOCK = threading.Lock()


class MarketRadarRuntimeWorker:
    def __init__(self, *, policy=None, repository=None, service_factory=None, clock=None):
        self.policy = policy or MarketRadarSessionPolicy()
        self.repository = repository or MarketRadarRepository()
        self.service_factory = service_factory or build_market_radar_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._status = {"running": False, "last_decision": None, "last_success_at": None, "last_error": None}

    def run_once(self) -> dict[str, Any]:
        decision = self.policy.decide(self.clock())
        self._status["last_decision"] = decision.model_dump(mode="json")
        if decision.kind == "not_due":
            return {"status": "skipped", "reason": decision.reason}
        if decision.kind == "calendar_unavailable":
            reservation = self.repository.reserve_scheduled_attempt(decision, lease_seconds=900)
            if reservation.acquired:
                self.repository.finish_scheduled_attempt(
                    reservation.attempt_key, status="skipped", reason_code="calendar_unavailable"
                )
            return {"status": "skipped", "reason": "calendar_unavailable", "attempt_key": decision.attempt_key}
        if not _RADAR_RUNTIME_LOCK.acquire(blocking=False):
            return {"status": "skipped", "reason": "radar_already_running", "attempt_key": decision.attempt_key}
        reservation: AttemptReservation | None = None
        try:
            reservation = self.repository.reserve_scheduled_attempt(decision, lease_seconds=900)
            if not reservation.acquired:
                return {"status": reservation.status, "reason": "duplicate_slot", "attempt_key": reservation.attempt_key, "run_id": reservation.run_id}
            self._status["running"] = True
            kind = "eod" if decision.kind == "eod_due" else "intraday"
            snapshot = self.service_factory(persist=True).run(market="cn", trigger="schedule", schedule_kind=kind, persist=True)
            run_id = self.repository.get_run_id_by_key(snapshot.run_key)
            self.repository.finish_scheduled_attempt(reservation.attempt_key, status="succeeded", run_id=run_id)
            self._status.update(last_success_at=self.clock().isoformat(), last_error=None)
            return {"status": "succeeded", "attempt_key": reservation.attempt_key, "run_id": run_id}
        except Exception as exc:
            category, summary = sanitize_runtime_failure(exc, limit=512)
            if reservation is not None and reservation.acquired:
                self.repository.finish_scheduled_attempt(reservation.attempt_key, status="failed", failure_category=category, failure_summary=summary)
            self._status["last_error"] = category
            return {"status": "failed", "attempt_key": decision.attempt_key, "reason": category}
        finally:
            self._status["running"] = False
            _RADAR_RUNTIME_LOCK.release()

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._status)
```

Use stable failure categories (`calendar_error`, `provider_error`, `persistence_error`, `runtime_error`) and strip token, key, authorization, cookie, and password assignments before truncation. A `calendar_unavailable` decision uses the attempt repository but does not build the service. Lock contention relies on the already-running worker's durable attempt row and does not terminalize or consume the slot, so a later tick can still retry after a crash lease expires.

- [ ] **Step 4: Run worker tests**

Run: `python -m pytest tests/market_radar/test_runtime_worker.py -q`

Expected: PASS, including non-overlapping ordinary analysis and Radar locks.

- [ ] **Step 5: Commit the worker**

```bash
git add src/market_radar/runtime_worker.py tests/market_radar/test_runtime_worker.py
git commit -m "feat: add Market Radar runtime worker"
```

---

### Task 6: Background-Only Runtime Scheduler and Opt-In Configuration

**Files:**
- Modify: `src/services/runtime_scheduler.py`
- Modify: `src/config.py`
- Modify: `.env.example`
- Modify: `tests/test_runtime_scheduler_service.py`
- Modify: `tests/test_config_env_compat.py`

**Interfaces:**
- Consumes: `MarketRadarRuntimeWorker.run_once/status` from Task 5.
- Produces: Radar task registration named `market_radar`, background-only scheduler startup, and additive `background_tasks` status.

- [ ] **Step 1: Write failing configuration and scheduler isolation tests**

```python
# tests/test_config_env_compat.py
def test_market_radar_schedule_defaults_disabled(monkeypatch):
    monkeypatch.delenv("MARKET_RADAR_SCHEDULE_ENABLED", raising=False)
    assert Config.from_env().market_radar_schedule_enabled is False


# tests/test_runtime_scheduler_service.py
def test_radar_only_config_starts_without_daily_analysis(fake_schedule, config):
    config.schedule_enabled = False
    config.market_radar_schedule_enabled = True
    service = RuntimeSchedulerService(config_provider=lambda: config)
    service.start()
    scheduler = service._scheduler
    assert scheduler is not None
    assert scheduler._task_callback is None
    assert [task["name"] for task in scheduler.background_tasks] == ["market_radar"]


def test_runtime_status_exposes_radar_diagnostics(service):
    status = service.status()
    assert status["background_tasks"]["market_radar"]["running"] is False
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_config_env_compat.py tests/test_runtime_scheduler_service.py -q`

Expected: FAIL because the config field and Radar task registration do not exist.

- [ ] **Step 3: Add opt-in configuration**

```python
# src/config.py Config fields
market_radar_schedule_enabled: bool = False

# Config.from_env constructor
market_radar_schedule_enabled=(
    os.getenv("MARKET_RADAR_SCHEDULE_ENABLED", "false").strip().lower() == "true"
),
```

```dotenv
# .env.example
# Enable 30-minute A-share Market Radar scans and one post-close finalization.
MARKET_RADAR_SCHEDULE_ENABLED=false
```

- [ ] **Step 4: Register the cached worker and allow background-only startup**

```python
# src/services/runtime_scheduler.py
def build_market_radar_background_task(config: Config) -> Dict[str, Any] | None:
    if not getattr(config, "market_radar_schedule_enabled", False):
        return None
    worker = MarketRadarRuntimeWorker()
    return {
        "task": worker.run_once,
        "interval_seconds": 60,
        "run_immediately": True,
        "name": "market_radar",
        "status_provider": worker.status,
    }


def _runtime_is_enabled(self, config: Config, background_tasks: List[Dict[str, Any]]) -> bool:
    return self._force_enabled or bool(getattr(config, "schedule_enabled", False)) or bool(background_tasks)
```

In `start`, compute `background_tasks` once, stop only when `_runtime_is_enabled` is false, instantiate `Scheduler`, and call `scheduler.set_daily_task(...)` only when `_is_schedule_enabled(config)` is true. Cache one worker per task name across reconciliation so the first-run flag and status survive schedule-time changes. In `status`, add:

```python
"background_tasks": {
    name: provider()
    for name, provider in sorted(self._background_status_providers.items())
},
```

Do not change `run_now`; it remains the ordinary stock-analysis command.

- [ ] **Step 5: Run scheduler, app lifecycle, and configuration tests**

Run: `python -m pytest tests/test_runtime_scheduler_service.py tests/test_config_env_compat.py -q`

Expected: PASS for ordinary schedule-only, Event Monitor-only where already supported, Radar-only, and combined configurations.

- [ ] **Step 6: Commit scheduler integration**

```bash
git add src/services/runtime_scheduler.py src/config.py .env.example tests/test_runtime_scheduler_service.py tests/test_config_env_compat.py
git commit -m "feat: schedule Market Radar runtime scans"
```

---

### Task 7: Documentation, Compatibility, and Full Verification

**Files:**
- Modify: `docs/market-radar.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: all Phase 2D behavior.
- Produces: operator documentation, changelog entry, and final verification evidence.

- [ ] **Step 1: Add the operator contract and rollback instructions**

```markdown
<!-- docs/market-radar.md -->
## Scheduled Runtime And Lifecycle (Phase 2D)

Set `MARKET_RADAR_SCHEDULE_ENABLED=true` in a long-running schedule/API/Web/Desktop process to enable the A-share Radar worker. The default is `false`. The worker evaluates the China exchange calendar every 60 seconds, persists no more than one run per 30-minute open-session slot, skips the lunch break, and finalizes one run after close.

Scheduled runs alone advance `watching -> candidate -> confirmed -> active -> downgraded -> exited`. Manual persisted runs and replay remain snapshot-only. A failed or duplicate run cannot advance the lifecycle, and lifecycle transitions commit atomically with their snapshot.

To roll back runtime execution, set `MARKET_RADAR_SCHEDULE_ENABLED=false` and reconcile or restart the long-running process. Existing snapshots, attempts, signals, and transitions remain readable; no destructive migration is required.
```

Add one flat `[Unreleased]` entry without a subsection:

```markdown
- [新功能] 新增可选的 A 股 Market Radar 盘中调度与确定性生命周期转换持久化。
```

- [ ] **Step 2: Run changed-file syntax checks**

Run:

```bash
python -m py_compile src/core/trading_calendar.py src/market_radar/session_policy.py src/market_radar/lifecycle.py src/market_radar/runtime_worker.py src/market_radar/factory.py src/market_radar/models.py src/market_radar/service.py src/market_radar/repository.py src/storage.py src/services/runtime_scheduler.py src/config.py scripts/run_market_radar.py
```

Expected: exit 0 with no output.

- [ ] **Step 3: Run deterministic offline Market Radar and scheduler tests**

Run:

```bash
python -m pytest tests/market_radar tests/test_runtime_scheduler_service.py tests/test_config_env_compat.py tests/test_run_market_radar.py -m "not network" -q
```

Expected: PASS with no failed or deselected non-network Phase 2D test.

- [ ] **Step 4: Run the repository backend gate**

Run: `bash scripts/ci_gate.sh`

Expected: exit 0. Record any environment-only skipped checks exactly; do not claim them as passed.

- [ ] **Step 5: Verify diff scope and documentation rules**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
rg -n "MARKET_RADAR_SCHEDULE_ENABLED|cn-lifecycle-v1|market_radar" .env.example docs/market-radar.md docs/CHANGELOG.md src tests
```

Expected: no whitespace errors; only Phase 2D files are modified; config, runtime, tests, and docs use the same setting and lifecycle version.

- [ ] **Step 6: Commit documentation and verification fixes**

```bash
git add docs/market-radar.md docs/CHANGELOG.md
git commit -m "docs: document Market Radar scheduled lifecycle"
```

- [ ] **Step 7: Request independent code review before push or PR**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. Treat correctness, atomicity, lifecycle semantics, scheduler isolation, config compatibility, and missing risk-path tests as blocking. Resolve review findings by checking every affected runtime, persistence, test, and documentation path before claiming readiness.
