"""Pure scheduling eligibility decisions for Market Radar A-share runs."""

from datetime import date, datetime, timedelta
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from src.core.trading_calendar import (
    MarketPhase,
    MarketPhaseContext,
    MarketSessionBounds,
    build_market_phase_context,
    get_market_session_bounds,
)
from src.market_radar.models import FrozenModel


class RadarRunDecision(FrozenModel):
    kind: Literal["intraday_due", "eod_due", "not_due", "calendar_unavailable"]
    market: Literal["cn"] = "cn"
    decided_at: datetime
    trading_date: date
    attempt_key: str | None = None
    session_segment: Literal["morning", "afternoon"] | None = None
    slot_start: datetime | None = None
    reason: str | None = None


ContextBuilder = Callable[..., MarketPhaseContext]
BoundsLoader = Callable[[str, datetime], MarketSessionBounds | None]


class MarketRadarSessionPolicy:
    """Derive a stable run identity from the current A-share market session."""

    def __init__(
        self,
        context_builder: ContextBuilder = build_market_phase_context,
        bounds_loader: BoundsLoader = get_market_session_bounds,
    ) -> None:
        self._context_builder = context_builder
        self._bounds_loader = bounds_loader

    def decide(self, now: datetime) -> RadarRunDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        local = now.astimezone(ZoneInfo("Asia/Shanghai"))
        context = self._context_builder(market="cn", current_time=now)
        if context.phase == MarketPhase.UNKNOWN:
            return self._calendar_unavailable(now, local)

        reason_by_phase = {
            MarketPhase.NON_TRADING: "market_closed",
            MarketPhase.PREMARKET: "premarket",
            MarketPhase.LUNCH_BREAK: "lunch_break",
        }
        if context.phase in reason_by_phase:
            return RadarRunDecision(
                kind="not_due",
                decided_at=now,
                trading_date=local.date(),
                reason=reason_by_phase[context.phase],
            )

        bounds = self._bounds_loader("cn", now)
        if bounds is None:
            return self._calendar_unavailable(now, local)

        if context.phase == MarketPhase.POSTMARKET:
            return RadarRunDecision(
                kind="eod_due",
                decided_at=now,
                trading_date=bounds.session_date,
                attempt_key=f"cn:eod:{bounds.session_date}",
            )

        segment = "afternoon" if bounds.break_end and local >= bounds.break_end else "morning"
        segment_start = bounds.break_end if segment == "afternoon" else bounds.open_at
        elapsed = int((local - segment_start).total_seconds() // 60)
        slot_start = segment_start + timedelta(minutes=(elapsed // 30) * 30)
        return RadarRunDecision(
            kind="intraday_due",
            decided_at=now,
            trading_date=bounds.session_date,
            attempt_key=f"cn:intraday:{bounds.session_date}:{segment}:{slot_start:%H%M}",
            session_segment=segment,
            slot_start=slot_start,
        )

    @staticmethod
    def _calendar_unavailable(now: datetime, local: datetime) -> RadarRunDecision:
        window = local.replace(
            minute=(local.minute // 30) * 30,
            second=0,
            microsecond=0,
        )
        return RadarRunDecision(
            kind="calendar_unavailable",
            decided_at=now,
            trading_date=local.date(),
            attempt_key=f"cn:calendar-error:{local.date()}:{window:%H%M}",
            reason="calendar_unavailable",
        )
