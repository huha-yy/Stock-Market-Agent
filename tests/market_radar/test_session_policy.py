from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core import trading_calendar
from src.core.trading_calendar import MarketPhase, MarketPhaseContext
from src.market_radar.session_policy import MarketRadarSessionPolicy


@pytest.fixture
def session_context():
    from src.core.trading_calendar import build_market_phase_context

    return build_market_phase_context


@pytest.fixture
def unknown_context():
    def build_unknown_context(*, market, current_time):
        local = current_time.astimezone(ZoneInfo("Asia/Shanghai"))
        return MarketPhaseContext(
            market=market,
            phase=MarketPhase.UNKNOWN,
            market_local_time=local,
            session_date=local.date(),
            effective_daily_bar_date=local.date(),
            is_trading_day=None,
            is_market_open_now=None,
            is_partial_bar=None,
        )

    return build_unknown_context


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


@pytest.mark.parametrize(
    ("local_time", "attempt_key"),
    [
        ("2026-07-30T10:00:00+08:00", "cn:intraday:2026-07-30:morning:1000"),
        ("2026-07-30T15:00:00+08:00", "cn:eod:2026-07-30"),
    ],
)
def test_cn_session_decisions_use_stable_attempt_keys(local_time, attempt_key, session_context):
    decision = MarketRadarSessionPolicy(context_builder=session_context).decide(
        datetime.fromisoformat(local_time)
    )

    assert decision.attempt_key == attempt_key


class _PartialBreakCalendar:
    def __init__(self, *, missing_bound: str):
        self._missing_bound = missing_bound

    def is_session(self, check_date: date) -> bool:
        return check_date == date(2026, 7, 30)

    def date_to_session(self, check_date: date, direction: str) -> datetime:
        assert direction == "previous"
        return datetime.combine(check_date, time.min)

    def session_open(self, session: datetime) -> datetime:
        return datetime.combine(session.date(), time(9, 30), tzinfo=ZoneInfo("Asia/Shanghai"))

    def session_close(self, session: datetime) -> datetime:
        return datetime.combine(session.date(), time(15, 0), tzinfo=ZoneInfo("Asia/Shanghai"))

    def session_has_break(self, session: datetime) -> bool:
        return True

    def session_break_start(self, session: datetime) -> datetime | None:
        if self._missing_bound == "start":
            return None
        return datetime.combine(session.date(), time(11, 30), tzinfo=ZoneInfo("Asia/Shanghai"))

    def session_break_end(self, session: datetime) -> datetime | None:
        if self._missing_bound == "end":
            return None
        return datetime.combine(session.date(), time(13, 0), tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("missing_bound", ["start", "end"])
def test_partial_break_calendar_fails_closed_for_policy(monkeypatch, missing_bound):
    calendar = _PartialBreakCalendar(missing_bound=missing_bound)
    monkeypatch.setattr(trading_calendar, "_XCALS_AVAILABLE", True)
    monkeypatch.setattr(
        trading_calendar,
        "xcals",
        SimpleNamespace(get_calendar=lambda _exchange: calendar),
    )

    now = datetime.fromisoformat("2026-07-30T10:00:00+08:00")
    assert trading_calendar.get_market_session_bounds("cn", now) is None

    def intraday_context(*, market, current_time):
        local = current_time.astimezone(ZoneInfo("Asia/Shanghai"))
        return MarketPhaseContext(
            market=market,
            phase=MarketPhase.INTRADAY,
            market_local_time=local,
            session_date=local.date(),
            effective_daily_bar_date=local.date(),
            is_trading_day=True,
            is_market_open_now=True,
            is_partial_bar=True,
        )

    decision = MarketRadarSessionPolicy(
        context_builder=intraday_context,
        bounds_loader=trading_calendar.get_market_session_bounds,
    ).decide(now)

    assert decision.kind == "calendar_unavailable"
    assert decision.attempt_key == "cn:calendar-error:2026-07-30:1000"


def test_unknown_calendar_uses_bounded_error_identity(unknown_context):
    decision = MarketRadarSessionPolicy(context_builder=unknown_context).decide(
        datetime.fromisoformat("2026-07-30T10:17:00+08:00")
    )

    assert decision.kind == "calendar_unavailable"
    assert decision.attempt_key == "cn:calendar-error:2026-07-30:1000"
