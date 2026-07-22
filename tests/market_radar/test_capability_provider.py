from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd

from data_provider.base import BaseFetcher, DataFetcherManager
from src.market_radar.capability_provider import ProviderCapabilityAdapter
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


def test_manager_bounds_and_redacts_persisted_failure_text() -> None:
    class FailingFetcher:
        name = "FailingFetcher"
        priority = 0

        def get_sector_history(self, kind: str, name: str):
            raise RuntimeError(
                "token=top-secret cookie=session-secret Authorization=Bearer bearer-secret "
                "headers={'Authorization': 'Basic basic-secret', "
                "'X-Api-Key': 'quoted-key', 'Cookie': 'sid=quoted-cookie'} "
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
