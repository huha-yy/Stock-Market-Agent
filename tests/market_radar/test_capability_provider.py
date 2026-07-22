from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import sys
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pandas as pd

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.base import BaseFetcher, DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.market_radar.capability_provider import ProviderCapabilityAdapter
from src.market_radar.enrichment import RunScopedCapabilityCircuit
from src.market_radar.models import SectorDefinition


AS_OF = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SECTOR = SectorDefinition(
    sector_id="industry:semiconductor",
    kind="industry",
    name="半导体",
    effective_from=date(2026, 1, 1),
)


class _MinimalFetcher(BaseFetcher):
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str):
        return pd.DataFrame()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str):
        return df


def test_base_fetcher_market_radar_capabilities_are_optional() -> None:
    fetcher = _MinimalFetcher()

    assert fetcher.get_sector_history("industry", "半导体") is None
    assert fetcher.get_sector_flow("industry", "半导体") is None
    assert fetcher.get_sector_constituents("industry", "半导体") is None


def test_manager_continues_after_empty_wrong_date_and_non_finite_payloads() -> None:
    valid_second_source_payload = pd.DataFrame(
        {"日期": ["2026-07-22"], "收盘": [1234.5], "成交额": [987654.0]}
    )

    class EmptyFetcher:
        name = "EmptyFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            return None

    class InvalidFetcher:
        name = "InvalidFetcher"
        priority = 1

        def get_sector_history(self, kind: str, name: str):
            return pd.DataFrame(
                {"日期": ["not-a-date"], "收盘": [float("nan")], "成交额": [1.0]}
            )

    class WorkingFetcher:
        name = "WorkingFetcher"
        priority = 2

        def get_sector_history(self, kind: str, name: str):
            return valid_second_source_payload

    manager = DataFetcherManager(
        fetchers=[EmptyFetcher(), InvalidFetcher(), WorkingFetcher()]
    )

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体"
    )

    assert data is valid_second_source_payload
    assert [item["result"] for item in trace] == ["empty", "invalid", "ok"]
    assert error == ""


def test_run_scoped_circuit_opens_one_capability_source_and_keeps_fallback() -> None:
    calls = {"A": 0, "B": 0}

    class FailingFetcher:
        name = "A"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            calls["A"] += 1
            raise RuntimeError("upstream failed")

    class WorkingFetcher:
        name = "B"
        priority = 1

        def get_sector_history(self, kind: str, name: str):
            calls["B"] += 1
            return pd.DataFrame(
                {
                    "data_date": ["2026-07-22"],
                    "close": [1.0],
                    "traded_amount": [2.0],
                }
            )

    adapter = ProviderCapabilityAdapter(
        DataFetcherManager(fetchers=[FailingFetcher(), WorkingFetcher()])
    )
    circuit = RunScopedCapabilityCircuit(failure_threshold=3)

    results = [
        adapter.fetch_board_history(SECTOR, AS_OF, attempt_policy=circuit)
        for _ in range(4)
    ]

    assert calls == {"A": 3, "B": 4}
    assert all(result.status == "ok" for result in results)
    assert [item["result"] for item in results[-1].trace] == [
        "circuit_open",
        "ok",
    ]


def test_concurrent_capability_admission_stops_failing_source_at_threshold() -> None:
    calls = {"A": 0, "B": 0}
    calls_lock = Lock()

    class FailingFetcher:
        name = "A"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            assert all_waiting.wait(timeout=1.0)
            with calls_lock:
                calls["A"] += 1
            raise RuntimeError("upstream failed")

    class WorkingFetcher:
        name = "B"
        priority = 1

        def get_sector_history(self, kind: str, name: str):
            with calls_lock:
                calls["B"] += 1
            return pd.DataFrame(
                {
                    "data_date": ["2026-07-22"],
                    "close": [1.0],
                    "traded_amount": [2.0],
                }
            )

    manager = DataFetcherManager(fetchers=[FailingFetcher(), WorkingFetcher()])
    original_get_lock = manager._get_fetcher_call_lock
    lock_requests = 0
    lock_requests_guard = Lock()
    all_waiting = Event()

    def synchronized_get_lock(fetcher):
        nonlocal lock_requests
        lock = original_get_lock(fetcher)
        if fetcher.name == "A":
            with lock_requests_guard:
                lock_requests += 1
                if lock_requests >= 6:
                    all_waiting.set()
        return lock

    manager._get_fetcher_call_lock = synchronized_get_lock
    adapter = ProviderCapabilityAdapter(manager)
    circuit = RunScopedCapabilityCircuit(failure_threshold=3)
    start = Barrier(6)

    def fetch_once():
        start.wait()
        result = adapter.fetch_board_history(
            SECTOR, AS_OF, attempt_policy=circuit
        )
        return result

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = tuple(executor.map(lambda _: fetch_once(), range(6)))

    assert calls == {"A": 3, "B": 6}
    assert all(result.status == "ok" for result in results)
    assert sum(
        item.get("result") == "circuit_open"
        for result in results
        for item in result.trace
    ) == 3


def test_quote_circuit_opens_failed_source_and_keeps_fallback(
    monkeypatch,
) -> None:
    calls = {"efinance": 0, "akshare_em": 0}

    class FailingQuoteFetcher:
        name = "EfinanceFetcher"
        priority = 0

        def get_realtime_quote(self, code: str):
            calls["efinance"] += 1
            raise RuntimeError("quote failed")

    class WorkingQuoteFetcher:
        name = "AkshareFetcher"
        priority = 1

        def get_realtime_quote(self, code: str, source: str = "em"):
            calls["akshare_em"] += 1
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.AKSHARE_EM,
                price=10.0,
                pre_close=9.0,
                amount=100.0,
                provider_timestamp="2026-07-22T14:30:00+08:00",
                volume_ratio=1.0,
                turnover_rate=1.0,
                pe_ratio=10.0,
                pb_ratio=1.0,
                total_mv=100.0,
                circ_mv=80.0,
                amplitude=1.0,
            )

    monkeypatch.setattr(
        "src.config.get_config",
        lambda: SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,akshare_em",
            realtime_cache_ttl=600,
        ),
    )
    adapter = ProviderCapabilityAdapter(
        DataFetcherManager(
            fetchers=[FailingQuoteFetcher(), WorkingQuoteFetcher()]
        )
    )
    circuit = RunScopedCapabilityCircuit(failure_threshold=3)

    results = [
        adapter.fetch_constituent_quotes(
            ("000001",), AS_OF, attempt_policy=circuit
        )
        for _ in range(4)
    ]

    assert calls == {"efinance": 3, "akshare_em": 4}
    assert all(result.status in {"ok", "stale"} for result in results)
    assert any(
        item.get("provider") == "efinance"
        and item.get("result") == "circuit_open"
        for item in results[-1].trace
    )
    assert any(
        item.get("provider") == "akshare_em"
        and item.get("result") == "ok"
        for item in results[-1].trace
    )


def test_quote_circuit_classifies_adapter_invalid_results_before_success(
    monkeypatch,
) -> None:
    calls = {"efinance": 0, "akshare_em": 0}

    def quote(
        code: str,
        source: RealtimeSource,
        *,
        provider_timestamp: str = "2026-07-22T14:30:00+08:00",
        amount: float = 100.0,
    ) -> UnifiedRealtimeQuote:
        return UnifiedRealtimeQuote(
            code=code,
            source=source,
            price=10.0,
            pre_close=9.0,
            amount=amount,
            provider_timestamp=provider_timestamp,
            volume_ratio=1.0,
            turnover_rate=1.0,
            pe_ratio=10.0,
            pb_ratio=1.0,
            total_mv=100.0,
            circ_mv=80.0,
            amplitude=1.0,
        )

    class StructurallyInvalidFetcher:
        name = "EfinanceFetcher"
        priority = 0

        def get_realtime_quote(self, code: str):
            calls["efinance"] += 1
            if calls["efinance"] == 1:
                return quote("600519", RealtimeSource.EFINANCE)
            if calls["efinance"] == 2:
                return quote(
                    code,
                    RealtimeSource.EFINANCE,
                    provider_timestamp="2026-07-22T17:00:00+08:00",
                )
            return quote(code, RealtimeSource.EFINANCE, amount=-1.0)

    class WorkingFetcher:
        name = "AkshareFetcher"
        priority = 1

        def get_realtime_quote(self, code: str, source: str = "em"):
            calls["akshare_em"] += 1
            return quote(code, RealtimeSource.AKSHARE_EM)

    monkeypatch.setattr(
        "src.config.get_config",
        lambda: SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,akshare_em",
            realtime_cache_ttl=600,
        ),
    )
    adapter = ProviderCapabilityAdapter(
        DataFetcherManager(
            fetchers=[StructurallyInvalidFetcher(), WorkingFetcher()]
        )
    )
    circuit = RunScopedCapabilityCircuit(failure_threshold=3)

    results = [
        adapter.fetch_constituent_quotes(
            ("000001",), AS_OF, attempt_policy=circuit
        )
        for _ in range(4)
    ]

    assert calls == {"efinance": 3, "akshare_em": 4}
    assert all(result.status in {"ok", "stale"} for result in results)
    assert all(
        not any(
            item.get("provider") == "efinance"
            and item.get("result") == "ok"
            for item in result.trace
        )
        for result in results
    )
    assert any(
        item.get("provider") == "efinance"
        and item.get("result") == "circuit_open"
        for item in results[-1].trace
    )


def test_quote_batch_stops_requesting_codes_after_deadline() -> None:
    calls: list[str] = []

    class AdvancingQuoteManager:
        def get_realtime_quote(self, code: str, *, log_final_failure: bool = True):
            calls.append(code)
            clock.now = 10.0
            return {
                "code": code,
                "price": 10.0,
                "pre_close": 9.0,
                "amount": 100.0,
                "provider_timestamp": "2026-07-22T14:30:00+08:00",
                "source": "fixture",
            }

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()

    result = ProviderCapabilityAdapter(
        AdvancingQuoteManager()
    ).fetch_constituent_quotes(
        ("000001", "600000", "600519"),
        AS_OF,
        deadline_monotonic=10.0,
        monotonic=clock,
    )

    assert calls == ["000001"]
    assert result.status == "partial"
    assert result.data is not None
    assert tuple(quote.code for quote in result.data.quotes) == ("000001",)
    assert any(
        item.get("result") == "deadline_exceeded" for item in result.trace
    )


def test_manager_internal_deadline_is_not_rewritten_as_invalid(
    monkeypatch,
) -> None:
    calls = []

    class NoCallFetcher:
        name = "EfinanceFetcher"
        priority = 0

        def get_realtime_quote(self, code: str):
            calls.append(code)
            raise AssertionError("provider must not be called after deadline")

    class SequenceClock:
        def __init__(self, values):
            self.values = iter(values)

        def __call__(self):
            return next(self.values, 10.0)

    monkeypatch.setattr(
        "src.config.get_config",
        lambda: SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance",
            realtime_cache_ttl=600,
        ),
    )
    result = ProviderCapabilityAdapter(
        DataFetcherManager(fetchers=[NoCallFetcher()])
    ).fetch_constituent_quotes(
        ("000001",),
        AS_OF,
        deadline_monotonic=10.0,
        monotonic=SequenceClock((0.0, 10.0)),
    )

    assert calls == []
    assert result.status == "unavailable"
    assert result.error == "deadline_exceeded"
    assert result.trace[0]["result"] == "deadline_exceeded"
    assert not any(item.get("result") == "invalid" for item in result.trace)


def test_partial_quotes_preserve_manager_internal_deadline(
    monkeypatch,
) -> None:
    calls = []

    class WorkingFetcher:
        name = "EfinanceFetcher"
        priority = 0

        def get_realtime_quote(self, code: str):
            calls.append(code)
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.EFINANCE,
                price=10.0,
                pre_close=9.0,
                amount=100.0,
                provider_timestamp="2026-07-22T14:30:00+08:00",
                volume_ratio=1.0,
                turnover_rate=1.0,
                pe_ratio=10.0,
                pb_ratio=1.0,
                total_mv=100.0,
                circ_mv=80.0,
                amplitude=1.0,
            )

    class SequenceClock:
        def __init__(self):
            self.values = iter((0.0, 0.0, 0.0, 10.0))

        def __call__(self):
            return next(self.values, 10.0)

    monkeypatch.setattr(
        "src.config.get_config",
        lambda: SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance",
            realtime_cache_ttl=600,
        ),
    )
    result = ProviderCapabilityAdapter(
        DataFetcherManager(fetchers=[WorkingFetcher()])
    ).fetch_constituent_quotes(
        ("000001", "600000"),
        AS_OF,
        deadline_monotonic=10.0,
        monotonic=SequenceClock(),
    )

    assert calls == ["000001"]
    assert result.status == "partial"
    assert result.error == "deadline_exceeded"
    assert result.data is not None
    assert tuple(item.code for item in result.data.quotes) == ("000001",)
    assert result.trace[0]["result"] == "deadline_exceeded"


def test_capability_deadline_is_rechecked_after_waiting_for_source_lock() -> None:
    calls = []
    clock = SimpleNamespace(now=0.0)

    class Fetcher:
        name = "A"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            calls.append((kind, name))
            return pd.DataFrame(
                {
                    "data_date": ["2026-07-22"],
                    "close": [1.0],
                    "traded_amount": [2.0],
                }
            )

    fetcher = Fetcher()
    manager = DataFetcherManager(fetchers=[fetcher])
    source_lock = manager._get_fetcher_call_lock(fetcher)
    lock_requested = Event()
    original_get_lock = manager._get_fetcher_call_lock

    def instrumented_get_lock(item):
        lock_requested.set()
        return original_get_lock(item)

    manager._get_fetcher_call_lock = instrumented_get_lock
    source_lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                ProviderCapabilityAdapter(manager).fetch_board_history,
                SECTOR,
                AS_OF,
                deadline_monotonic=10.0,
                monotonic=lambda: clock.now,
            )
            assert lock_requested.wait(timeout=1.0)
            clock.now = 10.0
            source_lock.release()
            result = future.result(timeout=1.0)
    finally:
        try:
            source_lock.release()
        except RuntimeError:
            pass

    assert calls == []
    assert result.status == "unavailable"
    assert result.error == "deadline_exceeded"
    assert result.trace[0]["result"] == "deadline_exceeded"


def test_manager_bounds_and_redacts_persisted_failure_text() -> None:
    class FailingFetcher:
        name = "FailingFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            raise RuntimeError(
                "token=top-secret cookie=session-secret Authorization=Bearer bearer-secret "
                "headers={'Authorization': 'Basic basic-secret', "
                "'X-Api-Key': 'quoted-key', 'Cookie': 'sid=quoted-cookie'} "
                "wrapper=Headers({'Proxy-Authorization': 'proxy-secret', "
                "'Set-Cookie': 'wrapped-cookie'}) "
                + "x" * 1000
            )

    manager = DataFetcherManager(fetchers=[FailingFetcher()])

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体"
    )

    assert data is None
    assert trace[0]["result"] == "failed"
    assert len(trace[0]["error"]) <= 256
    assert len(error) <= 256
    combined = f"{trace[0]['error']} {error}"
    assert "top-secret" not in combined
    assert "session-secret" not in combined
    assert "bearer-secret" not in combined
    assert "basic-secret" not in combined
    assert "quoted-key" not in combined
    assert "quoted-cookie" not in combined
    assert "proxy-secret" not in combined
    assert "wrapped-cookie" not in combined

    adapter_result = ProviderCapabilityAdapter(manager).fetch_board_history(
        SECTOR, AS_OF
    )
    persisted = f"{adapter_result.error} {adapter_result.trace}"
    assert "top-secret" not in persisted
    assert "proxy-secret" not in persisted

    class ExplodingManager:
        def get_market_radar_capability_with_meta(self, capability, *, kind, name):
            raise RuntimeError(
                "outer(headers=Headers({'X-Api-Key': 'adapter-secret'}))"
            )

    result = ProviderCapabilityAdapter(ExplodingManager()).fetch_board_history(
        SECTOR, AS_OF
    )
    assert "adapter-secret" not in (result.error or "")
    assert len(result.error or "") <= 256


def test_manager_skips_inherited_noop_capabilities_and_preserves_failure() -> None:
    class FailingFetcher(_MinimalFetcher):
        name = "FailingFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str, *, as_of=None):
            raise RuntimeError("actionable upstream failure")

    class UnsupportedFetcher(_MinimalFetcher):
        name = "UnsupportedFetcher"
        priority = 1

    manager = DataFetcherManager(
        fetchers=[FailingFetcher(), UnsupportedFetcher()]
    )

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体", as_of=AS_OF
    )

    assert data is None
    assert [item["provider"] for item in trace] == ["FailingFetcher"]
    assert "actionable upstream failure" in error


def test_manager_falls_through_malformed_constituent_codes() -> None:
    class InvalidFetcher:
        name = "InvalidFetcher"
        priority = 0

        def get_sector_constituents(self, kind: str, name: str):
            return [{"代码": "not-a-code", "数据日期": "2026-07-22"}]

    class WorkingFetcher:
        name = "WorkingFetcher"
        priority = 1

        def get_sector_constituents(self, kind: str, name: str):
            return [{"代码": "000001", "数据日期": "2026-07-22"}]

    manager = DataFetcherManager(fetchers=[InvalidFetcher(), WorkingFetcher()])

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_constituents", kind="industry", name="半导体"
    )

    assert data == [{"代码": "000001", "数据日期": "2026-07-22"}]
    assert [item["result"] for item in trace] == ["invalid", "ok"]
    assert error == ""


def test_manager_falls_through_future_terminal_date_for_requested_as_of() -> None:
    class FutureFetcher:
        name = "FutureFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            return pd.DataFrame(
                {"日期": ["2026-07-23"], "收盘": [1.0], "成交额": [2.0]}
            )

    class CurrentFetcher:
        name = "CurrentFetcher"
        priority = 1

        def get_sector_history(self, kind: str, name: str):
            return pd.DataFrame(
                {"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [2.0]}
            )

    manager = DataFetcherManager(fetchers=[FutureFetcher(), CurrentFetcher()])

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体", as_of=AS_OF
    )

    assert data.iloc[-1]["日期"] == "2026-07-22"
    assert [item["result"] for item in trace] == ["invalid", "ok"]
    assert error == ""


def test_manager_prefers_exact_date_after_stale_candidate() -> None:
    captured_as_of = []

    class StaleFetcher:
        name = "StaleFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str, *, as_of=None):
            captured_as_of.append(as_of)
            return pd.DataFrame(
                {"日期": ["2026-07-21"], "收盘": [1.0], "成交额": [2.0]}
            )

    class ExactFetcher:
        name = "ExactFetcher"
        priority = 1

        def get_sector_history(self, kind: str, name: str, *, as_of=None):
            captured_as_of.append(as_of)
            return pd.DataFrame(
                {"日期": ["2026-07-22"], "收盘": [3.0], "成交额": [4.0]}
            )

    manager = DataFetcherManager(fetchers=[StaleFetcher(), ExactFetcher()])

    data, trace, error = manager.get_market_radar_capability_with_meta(
        "sector_history", kind="industry", name="半导体", as_of=AS_OF
    )

    assert data.iloc[-1]["收盘"] == 3.0
    assert [item["result"] for item in trace] == ["stale", "ok"]
    assert captured_as_of == [AS_OF, AS_OF]
    assert error == ""


def test_adapter_returns_newest_past_payload_as_stale_with_freshness() -> None:
    class OlderFetcher:
        name = "OlderFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str, *, as_of=None):
            return pd.DataFrame(
                {"日期": ["2026-07-20"], "收盘": [1.0], "成交额": [2.0]}
            )

    class NewerFetcher:
        name = "NewerFetcher"
        priority = 1

        def get_sector_history(self, kind: str, name: str, *, as_of=None):
            return pd.DataFrame(
                {"日期": ["2026-07-21"], "收盘": [3.0], "成交额": [4.0]}
            )

    result = ProviderCapabilityAdapter(
        DataFetcherManager(fetchers=[OlderFetcher(), NewerFetcher()])
    ).fetch_board_history(SECTOR, AS_OF)

    assert result.status == "stale"
    assert result.data is not None
    assert result.data.bars[-1].close == 3.0
    assert result.data_date == date(2026, 7, 21)
    assert result.freshness_seconds > 0
    assert [item["result"] for item in result.trace] == ["stale", "stale"]
    assert result.trace[1]["selected"] is True


class _CapabilityManager:
    def __init__(self, payload, trace=None, error="") -> None:
        self.payload = payload
        self.trace = trace or [{"provider": "Fixture", "result": "ok"}]
        self.error = error

    def get_market_radar_capability_with_meta(self, capability, *, kind, name):
        return self.payload, self.trace, self.error


def test_history_aliases_normalize_to_canonical_bars() -> None:
    manager = _CapabilityManager(
        pd.DataFrame(
            {
                "交易日期": ["2026-07-22", "2026-07-21"],
                "收盘价": [1234.5, 1200.0],
                "成交额(元)": [987654.0, 900000.0],
            }
        )
    )
    adapter = ProviderCapabilityAdapter(manager)

    result = adapter.fetch_board_history(SECTOR, AS_OF)

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.code == SECTOR.sector_id
    assert result.data.bars[-1].close == 1234.5
    assert result.data.bars[-1].traded_amount == 987654.0
    assert result.data_date == date(2026, 7, 22)
    assert result.bar_status == "finalized"
    assert result.source == "Fixture"


def test_history_rejects_unknown_amount_unit_and_future_terminal_date() -> None:
    unknown_unit = ProviderCapabilityAdapter(
        _CapabilityManager(
            pd.DataFrame(
                {"日期": ["2026-07-22"], "收盘": [1.0], "成交额(千元)": [2.0]}
            )
        )
    ).fetch_board_history(SECTOR, AS_OF)
    future = ProviderCapabilityAdapter(
        _CapabilityManager(
            pd.DataFrame(
                {"日期": ["2026-07-23"], "收盘": [1.0], "成交额": [2.0]}
            )
        )
    ).fetch_board_history(SECTOR, AS_OF)

    assert unknown_unit.status == "unavailable"
    assert unknown_unit.data is None
    assert unknown_unit.trace[0]["provider"] == "Fixture"
    assert future.status == "unavailable"
    assert future.data is None
    assert future.data_date is None


def test_flow_membership_and_quotes_normalize_without_native_aliases_leaking() -> None:
    flow = ProviderCapabilityAdapter(
        _CapabilityManager(
            pd.DataFrame(
                {
                    "日期": ["2026-07-21", "2026-07-22"],
                    "主力净流入-净额": [-100.0, 250.0],
                    "成交额": [1000.0, 2000.0],
                }
            )
        )
    ).fetch_board_flow(SECTOR, AS_OF)
    membership = ProviderCapabilityAdapter(
        _CapabilityManager(
            pd.DataFrame(
                {
                    "股票代码": ["SZ000001", "600519.SH", "SZ000001"],
                    "数据日期": ["2026-07-22"] * 3,
                }
            )
        )
    ).fetch_constituents(SECTOR, AS_OF)

    class QuoteManager:
        def get_realtime_quote(self, code: str, *, log_final_failure: bool = True):
            if code == "000001":
                return {
                    "股票代码": code,
                    "最新价": 10.5,
                    "昨收": 10.0,
                    "成交额": 5000.0,
                    "报价时间": "2026-07-22T14:30:00+08:00",
                    "source": "fixture_quote",
                }
            return SimpleNamespace(
                code=code,
                price=float("nan"),
                pre_close=20.0,
                amount=1000.0,
                provider_timestamp="2026-07-22T14:30:00+08:00",
                source="fixture_quote",
            )

    quotes = ProviderCapabilityAdapter(QuoteManager()).fetch_constituent_quotes(
        ("000001", "600519"), AS_OF
    )

    assert flow.status == "ok"
    assert flow.data is not None
    assert flow.data.flows[-1].net_main_inflow == 250.0
    assert flow.data.model_dump()["flows"][-1] == {
        "data_date": date(2026, 7, 22),
        "net_main_inflow": 250.0,
        "traded_amount": 2000.0,
    }
    assert membership.status == "ok"
    assert membership.data is not None
    assert membership.data.codes == ("000001", "600519")
    assert membership.data.data_date == date(2026, 7, 22)
    assert quotes.status == "partial"
    assert quotes.data is not None
    assert tuple(item.code for item in quotes.data.quotes) == ("000001",)
    assert quotes.data_date == date(2026, 7, 22)
    assert quotes.trace[0]["code"] == "000001"


def test_akshare_unversioned_membership_remains_usable_as_partial(
    monkeypatch,
) -> None:
    constituents = pd.DataFrame({"代码": ["000001", "600519"]})
    fake_akshare = SimpleNamespace(
        stock_board_industry_cons_em=lambda **kwargs: constituents
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setattr(
        "data_provider.akshare_fetcher._akshare_call_with_timeout",
        lambda func, *args, **kwargs: func(
            *args,
            **{
                key: value
                for key, value in kwargs.items()
                if key not in {"timeout", "call_name"}
            },
        ),
    )
    fetcher = AkshareFetcher.__new__(AkshareFetcher)
    fetcher._history_call_timeout = 7
    monkeypatch.setattr(fetcher, "_set_random_user_agent", lambda: None)
    monkeypatch.setattr(fetcher, "_enforce_rate_limit", lambda: None)

    result = ProviderCapabilityAdapter(
        DataFetcherManager(fetchers=[fetcher])
    ).fetch_constituents(SECTOR, AS_OF)

    assert result.status == "partial"
    assert result.data is not None
    assert result.data.codes == ("000001", "600519")
    assert result.data.data_date is None
    assert result.data_date is None
    assert result.error == "unversioned_current_membership"
    assert any(
        item.get("error") == "unversioned_current_membership"
        for item in result.trace
    )


def test_benchmark_history_reuses_manager_daily_api_without_changing_identity() -> None:
    class BenchmarkManager:
        def __init__(self) -> None:
            self.calls = []

        def get_market_radar_capability_with_meta(
            self, capability: str, *, kind: str, name: str
        ):
            self.calls.append((capability, kind, name))
            return (
                pd.DataFrame(
                    {"date": ["2026-07-22"], "close": [6543.2], "amount": [12.0]}
                ),
                [{"provider": "BenchmarkFixture", "result": "ok"}],
                "",
            )

    manager = BenchmarkManager()

    result = ProviderCapabilityAdapter(manager).fetch_benchmark_history(
        "000985", AS_OF
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.code == "000985"
    assert manager.calls == [("benchmark_history", "index", "000985")]
    assert result.source == "BenchmarkFixture"


def test_empty_constituent_code_set_is_unavailable_without_quote_calls() -> None:
    class NoCallManager:
        def get_realtime_quote(self, code: str, *, log_final_failure: bool = True):
            raise AssertionError("quote API must not be called")

    result = ProviderCapabilityAdapter(NoCallManager()).fetch_constituent_quotes(
        (), AS_OF
    )

    assert result.status == "unavailable"
    assert result.data is None


def test_quote_with_mismatched_provider_code_is_rejected() -> None:
    class WrongIdentityManager:
        def get_realtime_quote(self, code: str, *, log_final_failure: bool = True):
            return {
                "code": "600519",
                "price": 10.0,
                "pre_close": 9.0,
                "amount": 100.0,
                "provider_timestamp": "2026-07-22T14:30:00+08:00",
            }

    result = ProviderCapabilityAdapter(
        WrongIdentityManager()
    ).fetch_constituent_quotes(("000001",), AS_OF)

    assert result.status == "unavailable"
    assert result.data is None
    assert result.trace[0]["result"] == "invalid"
