from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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


def test_unknown_calendar_uses_bounded_error_identity(unknown_context):
    decision = MarketRadarSessionPolicy(context_builder=unknown_context).decide(
        datetime.fromisoformat("2026-07-30T10:17:00+08:00")
    )

    assert decision.kind == "calendar_unavailable"
    assert decision.attempt_key == "cn:calendar-error:2026-07-30:1000"
