from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import inspect
import math
import re
from typing import Any, Callable, Protocol
import time
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.base import normalize_stock_code, sanitize_persisted_text
from src.market_radar.capabilities import (
    BoardBar,
    BoardBarSeries,
    BoardFlow,
    BoardFlowSeries,
    CapabilityResult,
    ConstituentMembership,
    ConstituentQuote,
    ConstituentQuoteBatch,
    EtfBar,
    EtfCapabilityData,
)
from src.market_radar.models import EtfDefinition, SectorDefinition


_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_ERROR_LIMIT = 256
_TRACE_LIMIT = 1000
_DATE_ALIASES = ("data_date", "trade_date", "date", "数据日期", "交易日期", "日期")
_CLOSE_ALIASES = ("close", "close_price", "收盘", "收盘价")
_CODE_ALIASES = ("code", "stock_code", "symbol", "代码", "股票代码", "证券代码")
_CURRENT_PRICE_ALIASES = ("current_price", "price", "最新价", "现价")
_PREVIOUS_CLOSE_ALIASES = ("previous_close", "pre_close", "昨收", "昨收价")
_QUOTE_TIME_ALIASES = ("quoted_at", "provider_timestamp", "quote_time", "报价时间")
_ETF_BARS_ALIASES = ("bars", "history")
_ETF_CURRENT_AMOUNT_ALIASES = (
    "current_traded_amount",
    "current_amount",
    "当前成交额",
    "成交额",
    "成交额(元)",
    "成交额（元）",
)
_AMOUNT_FACTORS = {
    "traded_amount": 1.0,
    "amount": 1.0,
    "成交额": 1.0,
    "成交额(元)": 1.0,
    "成交额（元）": 1.0,
    "成交额(万元)": 10_000.0,
    "成交额（万元）": 10_000.0,
    "成交额(亿元)": 100_000_000.0,
    "成交额（亿元）": 100_000_000.0,
}
_FLOW_FACTORS = {
    "net_main_inflow": 1.0,
    "主力净流入": 1.0,
    "主力净流入-净额": 1.0,
    "主力净流入净额": 1.0,
    "主力净流入(元)": 1.0,
    "主力净流入（元）": 1.0,
    "主力净流入(万元)": 10_000.0,
    "主力净流入（万元）": 10_000.0,
    "主力净流入(亿元)": 100_000_000.0,
    "主力净流入（亿元）": 100_000_000.0,
}


class MarketRadarEnrichmentProvider(Protocol):
    def fetch_board_history(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardBarSeries]:
        raise NotImplementedError


    def fetch_benchmark_history(
        self,
        code: str,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardBarSeries]:
        raise NotImplementedError

    def fetch_board_flow(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardFlowSeries]:
        raise NotImplementedError

    def fetch_constituents(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[ConstituentMembership]:
        raise NotImplementedError

    def fetch_constituent_quotes(
        self,
        codes: tuple[str, ...],
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[ConstituentQuoteBatch]:
        raise NotImplementedError


class MarketRadarEtfProvider(Protocol):
    def fetch_etf(
        self,
        etf: EtfDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[EtfCapabilityData]:
        raise NotImplementedError


def _bounded_text(value: Any) -> str:
    return sanitize_persisted_text(value, _ERROR_LIMIT)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, pd.DataFrame):
        return [dict(row) for row in payload.to_dict(orient="records")]
    if isinstance(payload, list) and all(isinstance(row, Mapping) for row in payload):
        return [dict(row) for row in payload]
    return []


def _value(row: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    raise ValueError(f"missing required field: {aliases[0]}")


def _scaled_value(
    row: Mapping[str, Any],
    factors: Mapping[str, float],
    semantic_name: str,
) -> float:
    for column, factor in factors.items():
        if column in row:
            return _finite_number(row[column], semantic_name) * factor

    hints = ("成交额", "amount") if semantic_name == "traded amount" else (
        "主力净流入",
        "net_main_inflow",
    )
    if any(any(hint in str(column) for hint in hints) for column in row):
        raise ValueError(f"unknown unit for {semantic_name}")
    raise ValueError(f"missing required field: {semantic_name}")


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _canonical_cn_code(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("stock code must not be boolean")
    code = normalize_stock_code(str(value or "").strip()).upper()
    if re.fullmatch(r"\d{6}", code) is None:
        raise ValueError("stock code must be a canonical 6-digit A-share code")
    return code


def _data_date(value: Any) -> date:
    if isinstance(value, bool) or value is None:
        raise ValueError("data date is invalid")
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("data date is invalid")
    return parsed.date()


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("quote time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("quote time must be timezone-aware")
    return parsed


def _payload_membership_date(
    payload: Any,
    rows: list[dict[str, Any]],
) -> date | None:
    row_dates: list[date] = []
    for row in rows:
        for alias in _DATE_ALIASES:
            if alias in row:
                row_dates.append(_data_date(row[alias]))
                break
    if not row_dates and isinstance(payload, pd.DataFrame):
        for alias in _DATE_ALIASES:
            if alias in payload.attrs:
                row_dates.append(_data_date(payload.attrs[alias]))
                break
    if not row_dates:
        return None
    if len(set(row_dates)) != 1:
        raise ValueError("membership requires one provider-reported data date")
    return row_dates[0]


def _normalize_bars(payload: Any, code: str) -> BoardBarSeries:
    bars = []
    for row in _rows(payload):
        close = _finite_number(_value(row, _CLOSE_ALIASES), "close")
        amount = _scaled_value(row, _AMOUNT_FACTORS, "traded amount")
        if close <= 0 or amount < 0:
            raise ValueError("history prices and amounts must be non-negative")
        bars.append(
            BoardBar(
                data_date=_data_date(_value(row, _DATE_ALIASES)),
                close=close,
                traded_amount=amount,
            )
        )
    if not bars:
        raise ValueError("empty result")
    bars.sort(key=lambda item: item.data_date)
    return BoardBarSeries(code=code, bars=bars)


def _normalize_flows(payload: Any, code: str) -> BoardFlowSeries:
    flows = []
    for row in _rows(payload):
        amount = _scaled_value(row, _AMOUNT_FACTORS, "traded amount")
        if amount < 0:
            raise ValueError("traded amount must be non-negative")
        flows.append(
            BoardFlow(
                data_date=_data_date(_value(row, _DATE_ALIASES)),
                net_main_inflow=_scaled_value(
                    row, _FLOW_FACTORS, "net main inflow"
                ),
                traded_amount=amount,
            )
        )
    if not flows:
        raise ValueError("empty result")
    flows.sort(key=lambda item: item.data_date)
    return BoardFlowSeries(code=code, flows=flows)


def _normalize_membership(payload: Any) -> ConstituentMembership:
    rows = _rows(payload)
    data_date = _payload_membership_date(payload, rows)
    codes: list[str] = []
    for row in rows:
        code = _canonical_cn_code(_value(row, _CODE_ALIASES))
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("empty result")
    return ConstituentMembership(codes=codes, data_date=data_date)


def _mapping_value(
    payload: Mapping[str, Any], aliases: tuple[str, ...], default: Any = None
) -> Any:
    for alias in aliases:
        if alias in payload:
            return payload[alias]
    return default


def _optional_number(
    payload: Mapping[str, Any], aliases: tuple[str, ...], field_name: str
) -> float | None:
    value = _mapping_value(payload, aliases)
    if value is None:
        return None
    return _finite_number(value, field_name)


def _optional_bool(
    payload: Mapping[str, Any], aliases: tuple[str, ...], field_name: str
) -> bool | None:
    value = _mapping_value(payload, aliases)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _normalize_etf(
    payload: Any, expected_code: str | None = None
) -> EtfCapabilityData:
    if not isinstance(payload, Mapping):
        raise ValueError("ETF snapshot must be a mapping")
    code = _canonical_cn_code(_value(payload, _CODE_ALIASES))
    if expected_code is not None and code != expected_code:
        raise ValueError("provider ETF code does not match requested code")
    raw_bars = _mapping_value(payload, _ETF_BARS_ALIASES)
    bars = tuple(
        EtfBar(
            data_date=_data_date(_value(row, _DATE_ALIASES)),
            close=_finite_number(_value(row, _CLOSE_ALIASES), "close"),
            traded_amount=_scaled_value(row, _AMOUNT_FACTORS, "traded amount"),
        )
        for row in _rows(raw_bars)
    )
    if not bars:
        raise ValueError("empty result")
    bars = tuple(sorted(bars, key=lambda item: item.data_date))
    quoted_value = _mapping_value(payload, _QUOTE_TIME_ALIASES)
    return EtfCapabilityData(
        code=code,
        bars=bars,
        quoted_at=(
            _aware_datetime(quoted_value) if quoted_value is not None else None
        ),
        current_price=_optional_number(
            payload, _CURRENT_PRICE_ALIASES, "current price"
        ),
        current_traded_amount=_optional_number(
            payload, _ETF_CURRENT_AMOUNT_ALIASES, "current traded amount"
        ),
        active=_optional_bool(payload, ("active",), "active"),
        suspended=_optional_bool(payload, ("suspended",), "suspended"),
        bid_price=_optional_number(payload, ("bid_price", "bid"), "bid price"),
        ask_price=_optional_number(payload, ("ask_price", "ask"), "ask price"),
        nav=_optional_number(payload, ("nav", "单位净值"), "nav"),
        tracking_error_pct=_optional_number(
            payload, ("tracking_error_pct",), "tracking error"
        ),
        tracking_difference_pct=_optional_number(
            payload, ("tracking_difference_pct",), "tracking difference"
        ),
        annual_fee_pct=_optional_number(
            payload, ("annual_fee_pct",), "annual fee"
        ),
        net_assets_cny=_optional_number(
            payload, ("net_assets_cny",), "net assets"
        ),
        shares=_optional_number(payload, ("shares",), "shares"),
    )


def validate_provider_capability_payload(
    capability: str,
    payload: Any,
    *,
    as_of: datetime | None = None,
) -> tuple[bool, str]:
    """Validate provider-native payloads before manager fallback stops."""
    if payload is None:
        return False, "empty result"
    if isinstance(payload, pd.DataFrame) and payload.empty:
        return False, "empty result"
    if isinstance(payload, list) and not payload:
        return False, "empty result"
    try:
        terminal = provider_capability_data_date(capability, payload)
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("as_of must be timezone-aware")
            if (
                terminal is not None
                and terminal > as_of.astimezone(_CN_TIMEZONE).date()
            ):
                raise ValueError("provider terminal date is later than requested as_of")
    except Exception as exc:
        return False, _bounded_text(exc) or "invalid result"
    return True, ""


def provider_capability_data_date(
    capability: str,
    payload: Any,
) -> date | None:
    if capability in {"sector_history", "benchmark_history"}:
        return _normalize_bars(payload, "validation").bars[-1].data_date
    if capability == "sector_flow":
        return _normalize_flows(payload, "validation").flows[-1].data_date
    if capability == "sector_constituents":
        return _normalize_membership(payload).data_date
    if capability == "etf_snapshot":
        return _normalize_etf(payload).bars[-1].data_date
    raise ValueError("unsupported capability")


def _safe_trace(trace: Any) -> tuple[Mapping[str, Any], ...]:
    cleaned = []
    for item in tuple(trace or ())[:_TRACE_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {}
        for key in (
            "provider",
            "result",
            "duration_ms",
            "code",
            "selected",
            "error",
        ):
            if key not in item:
                continue
            value = item[key]
            entry[key] = (
                _bounded_text(value)
                if key in {"provider", "code", "error"}
                else value
            )
        cleaned.append(entry)
    return tuple(cleaned)


def _source_from_trace(trace: Any, fallback: str) -> str:
    for item in reversed(tuple(trace or ())):
        if isinstance(item, Mapping) and item.get("result") == "ok":
            source = _bounded_text(item.get("provider"))[:80]
            if source:
                return source
    for item in reversed(tuple(trace or ())):
        if (
            isinstance(item, Mapping)
            and item.get("result") in {"stale", "partial"}
            and item.get("selected") is True
        ):
            source = _bounded_text(item.get("provider"))[:80]
            if source:
                return source
    return fallback


class ProviderCapabilityAdapter:
    def __init__(
        self,
        manager: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manager = manager
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _acquisition_time(self, as_of: datetime) -> datetime:
        acquired_at = self.clock()
        self._require_as_of(acquired_at)
        requested_at = as_of.astimezone(timezone.utc)
        acquired_at = acquired_at.astimezone(timezone.utc)
        if acquired_at < requested_at:
            raise ValueError("quote acquisition time precedes requested as_of")
        return acquired_at

    @staticmethod
    def _require_as_of(as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

    @staticmethod
    def _market_date(as_of: datetime) -> date:
        return as_of.astimezone(_CN_TIMEZONE).date()

    @classmethod
    def _bar_status(cls, data_date: date, as_of: datetime) -> str:
        local = as_of.astimezone(_CN_TIMEZONE)
        if data_date < local.date() or local.hour >= 15:
            return "finalized"
        return "provisional"

    def _unavailable(
        self,
        capability: str,
        as_of: datetime,
        *,
        trace: Any = (),
        error: Any = "unavailable",
        source: str = "provider_unavailable",
    ) -> CapabilityResult:
        return CapabilityResult(
            capability=capability,
            status="unavailable",
            data=None,
            source=source,
            observed_at=as_of,
            data_date=None,
            bar_status=None,
            freshness_seconds=0,
            trace=_safe_trace(trace),
            error=_bounded_text(error) or "unavailable",
        )

    def _manager_capability(
        self,
        capability: str,
        *,
        kind: str,
        name: str,
        as_of: datetime,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[Any | None, Any, str]:
        method = self.manager.get_market_radar_capability_with_meta
        kwargs: dict[str, Any] = {"kind": kind, "name": name}
        parameters = inspect.signature(method).parameters.values()
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        parameter_names = {parameter.name for parameter in parameters}
        if "as_of" in parameter_names or accepts_kwargs:
            kwargs["as_of"] = as_of
        if "attempt_policy" in parameter_names or accepts_kwargs:
            kwargs["attempt_policy"] = attempt_policy
        if "deadline_monotonic" in parameter_names or accepts_kwargs:
            kwargs["deadline_monotonic"] = deadline_monotonic
        if "monotonic" in parameter_names or accepts_kwargs:
            kwargs["monotonic"] = monotonic
        return method(capability, **kwargs)

    def _bar_result(
        self,
        capability: str,
        series: BoardBarSeries,
        as_of: datetime,
        source: str,
        trace: Any,
    ) -> CapabilityResult[BoardBarSeries]:
        terminal = series.bars[-1].data_date
        if terminal > self._market_date(as_of):
            return self._unavailable(
                capability,
                as_of,
                trace=trace,
                error="provider terminal date is later than requested as_of",
                source=source,
            )
        stale = terminal < self._market_date(as_of)
        return CapabilityResult[BoardBarSeries](
            capability=capability,
            status="stale" if stale else "ok",
            data=series,
            source=source,
            observed_at=as_of,
            data_date=terminal,
            bar_status=self._bar_status(terminal, as_of),
            freshness_seconds=(
                (self._market_date(as_of) - terminal).days * 86400
                if stale
                else 0
            ),
            trace=_safe_trace(trace),
            error=None,
        )

    def fetch_board_history(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardBarSeries]:
        self._require_as_of(as_of)
        trace: Any = ()
        source = "sector_history"
        try:
            payload, trace, error = self._manager_capability(
                "sector_history",
                kind=sector.kind,
                name=sector.name,
                as_of=as_of,
                attempt_policy=attempt_policy,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
            if payload is None:
                return self._unavailable(
                    "board_history", as_of, trace=trace, error=error
                )
            source = _source_from_trace(trace, source)
            return self._bar_result(
                "board_history",
                _normalize_bars(payload, sector.sector_id),
                as_of,
                source,
                trace,
            )
        except Exception as exc:
            return self._unavailable(
                "board_history",
                as_of,
                trace=trace,
                error=exc,
                source=source,
            )

    def fetch_benchmark_history(
        self,
        code: str,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardBarSeries]:
        self._require_as_of(as_of)
        try:
            benchmark_code = _canonical_cn_code(code)
        except ValueError as exc:
            return self._unavailable(
                "benchmark_history", as_of, error=exc
            )
        try:
            payload, trace, error = self._manager_capability(
                "benchmark_history",
                kind="index",
                name=benchmark_code,
                as_of=as_of,
                attempt_policy=attempt_policy,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
            if payload is None:
                return self._unavailable(
                    "benchmark_history", as_of, trace=trace, error=error
                )
            source = _source_from_trace(trace, "benchmark_history")
            return self._bar_result(
                "benchmark_history",
                _normalize_bars(payload, benchmark_code),
                as_of,
                source,
                trace,
            )
        except Exception as exc:
            return self._unavailable("benchmark_history", as_of, error=exc)

    def fetch_etf(
        self,
        etf: EtfDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[EtfCapabilityData]:
        self._require_as_of(as_of)
        trace: Any = ()
        source = "etf_snapshot"
        try:
            code = _canonical_cn_code(etf.code)
            payload, trace, error = self._manager_capability(
                "etf_snapshot",
                kind="etf",
                name=code,
                as_of=as_of,
                attempt_policy=attempt_policy,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
            if payload is None:
                return self._unavailable(
                    "etf_snapshot", as_of, trace=trace, error=error
                )
            data = _normalize_etf(payload, code)
            terminal = data.bars[-1].data_date
            if terminal > self._market_date(as_of):
                return self._unavailable(
                    "etf_snapshot",
                    as_of,
                    trace=trace,
                    error="provider terminal date is later than requested as_of",
                )
            observed_at = as_of.astimezone(timezone.utc)
            freshness_seconds = max(
                0, (self._market_date(as_of) - terminal).days * 86400
            )
            if data.quoted_at is not None:
                acquired_at = self._acquisition_time(as_of)
                if data.quoted_at.astimezone(timezone.utc) > acquired_at:
                    raise ValueError("quote time is later than acquisition time")
                observed_at = acquired_at
                freshness_seconds = max(
                    0,
                    int(
                        (
                            acquired_at
                            - data.quoted_at.astimezone(timezone.utc)
                        ).total_seconds()
                    ),
                )
            stale = terminal < self._market_date(as_of)
            source = _source_from_trace(trace, source)
            return CapabilityResult[EtfCapabilityData](
                capability="etf_snapshot",
                status="stale" if stale else "ok",
                data=data,
                source=source,
                observed_at=observed_at,
                data_date=terminal,
                bar_status=self._bar_status(terminal, as_of),
                freshness_seconds=freshness_seconds,
                trace=_safe_trace(trace),
                error=None,
            )
        except Exception as exc:
            return self._unavailable(
                "etf_snapshot",
                as_of,
                trace=trace,
                error=exc,
                source=source,
            )

    def fetch_board_flow(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[BoardFlowSeries]:
        self._require_as_of(as_of)
        trace: Any = ()
        source = "sector_flow"
        try:
            payload, trace, error = self._manager_capability(
                "sector_flow",
                kind=sector.kind,
                name=sector.name,
                as_of=as_of,
                attempt_policy=attempt_policy,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
            if payload is None:
                return self._unavailable(
                    "board_flow", as_of, trace=trace, error=error
                )
            series = _normalize_flows(payload, sector.sector_id)
            terminal = series.flows[-1].data_date
            source = _source_from_trace(trace, source)
            if terminal > self._market_date(as_of):
                return self._unavailable(
                    "board_flow",
                    as_of,
                    trace=trace,
                    error="provider terminal date is later than requested as_of",
                    source=source,
                )
            stale = terminal < self._market_date(as_of)
            return CapabilityResult[BoardFlowSeries](
                capability="board_flow",
                status="stale" if stale else "ok",
                data=series,
                source=source,
                observed_at=as_of,
                data_date=terminal,
                bar_status=self._bar_status(terminal, as_of),
                freshness_seconds=(
                    (self._market_date(as_of) - terminal).days * 86400
                    if stale
                    else 0
                ),
                trace=_safe_trace(trace),
                error=None,
            )
        except Exception as exc:
            return self._unavailable(
                "board_flow",
                as_of,
                trace=trace,
                error=exc,
                source=source,
            )

    def fetch_constituents(
        self,
        sector: SectorDefinition,
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[ConstituentMembership]:
        self._require_as_of(as_of)
        trace: Any = ()
        source = "sector_constituents"
        try:
            payload, trace, error = self._manager_capability(
                "sector_constituents",
                kind=sector.kind,
                name=sector.name,
                as_of=as_of,
                attempt_policy=attempt_policy,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
            )
            if payload is None:
                return self._unavailable(
                    "constituents", as_of, trace=trace, error=error
                )
            membership = _normalize_membership(payload)
            source = _source_from_trace(trace, source)
            if membership.data_date is None:
                return CapabilityResult[ConstituentMembership](
                    capability="constituents",
                    status="partial",
                    data=membership,
                    source=source,
                    observed_at=as_of,
                    data_date=None,
                    bar_status=None,
                    freshness_seconds=0,
                    trace=_safe_trace(trace),
                    error="unversioned_current_membership",
                )
            if membership.data_date > self._market_date(as_of):
                return self._unavailable(
                    "constituents",
                    as_of,
                    trace=trace,
                    error="provider data date is later than requested as_of",
                    source=source,
                )
            stale = membership.data_date < self._market_date(as_of)
            return CapabilityResult[ConstituentMembership](
                capability="constituents",
                status="stale" if stale else "ok",
                data=membership,
                source=source,
                observed_at=as_of,
                data_date=membership.data_date,
                bar_status=self._bar_status(membership.data_date, as_of),
                freshness_seconds=(
                    (self._market_date(as_of) - membership.data_date).days
                    * 86400
                    if stale
                    else 0
                ),
                trace=_safe_trace(trace),
                error=None,
            )
        except Exception as exc:
            return self._unavailable(
                "constituents",
                as_of,
                trace=trace,
                error=exc,
                source=source,
            )

    @staticmethod
    def _quote_mapping(quote: Any) -> dict[str, Any]:
        if isinstance(quote, Mapping):
            return dict(quote)
        if hasattr(quote, "to_dict"):
            return dict(quote.to_dict())
        try:
            return dict(vars(quote))
        except TypeError:
            return {}

    @classmethod
    def _parse_constituent_quote(
        cls,
        raw_quote: Any,
        requested_code: str,
        as_of: datetime,
    ) -> tuple[ConstituentQuote, str, bool, int]:
        row = cls._quote_mapping(raw_quote)
        quoted_at = _aware_datetime(_value(row, _QUOTE_TIME_ALIASES))
        if quoted_at > as_of:
            raise ValueError("quote time is later than requested as_of")
        code = _canonical_cn_code(_value(row, _CODE_ALIASES))
        if code != requested_code:
            raise ValueError("provider quote code does not match requested code")
        current = _finite_number(
            _value(row, _CURRENT_PRICE_ALIASES), "current price"
        )
        if current <= 0:
            raise ValueError("current price must be positive")
        previous = _finite_number(
            _value(row, _PREVIOUS_CLOSE_ALIASES), "previous close"
        )
        if previous <= 0:
            raise ValueError("previous close must be positive")
        amount = _scaled_value(row, _AMOUNT_FACTORS, "traded amount")
        if amount < 0:
            raise ValueError("traded amount must be nonnegative")
        quote = ConstituentQuote(
            code=code,
            current_price=current,
            previous_close=previous,
            traded_amount=amount,
            quoted_at=quoted_at,
        )
        source_value = row.get("source", "realtime_quote")
        source = getattr(source_value, "value", source_value)
        stale = bool(row.get("is_stale", False))
        stale_seconds = int(row.get("stale_seconds") or 0)
        return quote, str(source), stale, max(0, stale_seconds)

    @staticmethod
    def _prioritize_deadline_trace(trace: list[dict[str, Any]]) -> None:
        for index, item in enumerate(trace):
            if item.get("result") == "deadline_exceeded":
                trace.insert(0, trace.pop(index))
                return

    def fetch_constituent_quotes(
        self,
        codes: tuple[str, ...],
        as_of: datetime,
        *,
        attempt_policy: Any = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> CapabilityResult[ConstituentQuoteBatch]:
        self._require_as_of(as_of)
        try:
            normalized_codes = tuple(
                dict.fromkeys(_canonical_cn_code(code) for code in codes)
            )
        except ValueError as exc:
            return self._unavailable("constituent_quotes", as_of, error=exc)
        if not normalized_codes:
            return self._unavailable(
                "constituent_quotes", as_of, error="constituent code set is empty"
            )

        parsed: list[tuple[ConstituentQuote, str, bool, int]] = []
        trace: list[dict[str, Any]] = []
        deadline_exceeded = False
        observed_at = as_of.astimezone(timezone.utc)
        for requested_code in normalized_codes:
            if (
                deadline_monotonic is not None
                and monotonic() >= deadline_monotonic
            ):
                trace.insert(
                    0,
                    {
                        "provider": "constituent_quotes",
                        "result": "deadline_exceeded",
                        "code": requested_code,
                    },
                )
                deadline_exceeded = True
                break
            try:
                method = self.manager.get_realtime_quote
                parameters = inspect.signature(method).parameters.values()
                parameter_names = {parameter.name for parameter in parameters}
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
                optional = {
                    "attempt_policy": attempt_policy,
                    "deadline_monotonic": deadline_monotonic,
                    "monotonic": monotonic,
                    "attempt_trace": trace,
                    "result_validator": lambda raw: bool(
                        self._parse_constituent_quote(
                            raw,
                            requested_code,
                            self._acquisition_time(as_of),
                        )
                    ),
                }
                kwargs = {
                    "log_final_failure": False,
                    **{
                        name: value
                        for name, value in optional.items()
                        if accepts_kwargs or name in parameter_names
                    },
                }
                deadline_count = sum(
                    item.get("result") == "deadline_exceeded"
                    for item in trace
                )
                raw_quote = method(
                    requested_code,
                    **kwargs,
                )
                manager_deadline = sum(
                    item.get("result") == "deadline_exceeded"
                    for item in trace
                ) > deadline_count
                if manager_deadline:
                    deadline_exceeded = True
                    self._prioritize_deadline_trace(trace)
                if raw_quote is None:
                    if manager_deadline:
                        break
                    raise ValueError("empty quote")
                acquired_at = self._acquisition_time(as_of)
                parsed_quote = self._parse_constituent_quote(
                    raw_quote, requested_code, acquired_at
                )
                quote, source, _, _ = parsed_quote
                parsed.append(parsed_quote)
                observed_at = max(observed_at, acquired_at)
                trace.append(
                    {"provider": source, "result": "ok", "code": quote.code}
                )
                if manager_deadline:
                    break
            except Exception as exc:
                trace.append(
                    {
                        "provider": "realtime_quote",
                        "result": "invalid",
                        "code": requested_code,
                        "error": _bounded_text(exc),
                    }
                )

        if not parsed:
            return self._unavailable(
                "constituent_quotes",
                as_of,
                trace=trace,
                error=(
                    "deadline_exceeded"
                    if deadline_exceeded
                    else "no valid constituent quotes"
                ),
            )

        terminal = max(item[0].quoted_at.astimezone(_CN_TIMEZONE).date() for item in parsed)
        same_date = [
            item
            for item in parsed
            if item[0].quoted_at.astimezone(_CN_TIMEZONE).date() == terminal
        ]
        batch = ConstituentQuoteBatch(quotes=[item[0] for item in same_date])
        sources = tuple(dict.fromkeys(item[1] for item in same_date))
        source = sources[0] if len(sources) == 1 else "mixed_realtime_quotes"
        status = "ok" if len(same_date) == len(normalized_codes) else "partial"
        if deadline_exceeded:
            status = "partial"
        elif any(item[2] for item in same_date):
            status = "stale"
        return CapabilityResult[ConstituentQuoteBatch](
            capability="constituent_quotes",
            status=status,
            data=batch,
            source=_bounded_text(source)[:80] or "realtime_quote",
            observed_at=observed_at,
            data_date=terminal,
            bar_status=self._bar_status(terminal, observed_at),
            freshness_seconds=max(item[3] for item in same_date),
            trace=_safe_trace(trace),
            error="deadline_exceeded" if deadline_exceeded else None,
        )
