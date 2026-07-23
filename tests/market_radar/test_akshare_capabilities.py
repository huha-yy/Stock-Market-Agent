from __future__ import annotations

from datetime import datetime, timezone

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider import akshare_fetcher as akshare_module
from data_provider.akshare_fetcher import AkshareFetcher


def _fetcher(monkeypatch, akshare) -> AkshareFetcher:
    fetcher = AkshareFetcher.__new__(AkshareFetcher)
    fetcher._history_call_timeout = 7
    monkeypatch.setitem(sys.modules, "akshare", akshare)
    monkeypatch.setattr(fetcher, "_set_random_user_agent", lambda: None)
    monkeypatch.setattr(fetcher, "_enforce_rate_limit", lambda: None)
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
    return fetcher


def test_realtime_em_spot_call_uses_bounded_timeout_wrapper(monkeypatch) -> None:
    captured = []
    raw_calls = []

    def raw_spot():
        raw_calls.append(True)
        return pd.DataFrame()

    def bounded_call(func, *args, **kwargs):
        captured.append((func, args, kwargs))
        return pd.DataFrame()

    fake_akshare = SimpleNamespace(stock_zh_a_spot_em=raw_spot)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setattr(akshare_module, "_akshare_call_with_timeout", bounded_call)
    monkeypatch.setattr(
        akshare_module,
        "_realtime_cache",
        {"data": None, "timestamp": 0, "ttl": 1200},
    )
    fetcher = AkshareFetcher.__new__(AkshareFetcher)
    fetcher._history_call_timeout = 7
    monkeypatch.setattr(fetcher, "_set_random_user_agent", lambda: None)
    monkeypatch.setattr(fetcher, "_enforce_rate_limit", lambda: None)

    result = fetcher._get_stock_realtime_quote_em("000001")

    assert result is None
    assert raw_calls == []
    assert captured == [
        (
            raw_spot,
            (),
            {
                "timeout": 7,
                "call_name": "ak.stock_zh_a_spot_em",
            },
        )
    ]


def test_sector_history_dispatches_explicit_industry_and_concept_endpoints(
    monkeypatch,
) -> None:
    calls = []

    def industry(**kwargs):
        calls.append(("industry", kwargs))
        return pd.DataFrame({"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [2.0]})

    def concept(**kwargs):
        calls.append(("concept", kwargs))
        return pd.DataFrame({"日期": ["2026-07-22"], "收盘": [3.0], "成交额": [4.0]})

    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(
            stock_board_industry_hist_em=industry,
            stock_board_concept_hist_em=concept,
        ),
    )

    as_of = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
    industry_result = fetcher.get_sector_history(
        "industry", "半导体", as_of=as_of
    )
    concept_result = fetcher.get_sector_history(
        "concept", "先进封装", as_of=as_of
    )

    assert industry_result.iloc[-1]["收盘"] == 1.0
    assert concept_result.iloc[-1]["收盘"] == 3.0
    assert [item[0] for item in calls] == ["industry", "concept"]
    assert calls[0][1]["symbol"] == "半导体"
    assert calls[0][1]["period"] == "日k"
    assert calls[0][1]["start_date"] == "20260123"
    assert calls[0][1]["end_date"] == "20260722"
    assert calls[1][1]["symbol"] == "先进封装"
    assert calls[1][1]["period"] == "daily"


def test_sector_flow_merges_same_source_history_amount_and_rejects_concepts(
    monkeypatch,
) -> None:
    calls = []

    def flow(**kwargs):
        calls.append(("flow", kwargs))
        return pd.DataFrame(
            {
                "日期": ["2025-01-02", "2026-07-22"],
                "主力净流入-净额": [50.0, 100.0],
            }
        )

    def history(**kwargs):
        calls.append(("history", kwargs))
        return pd.DataFrame({"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [500.0]})

    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(
            stock_sector_fund_flow_hist=flow,
            stock_board_industry_hist_em=history,
        ),
    )

    result = fetcher.get_sector_flow("industry", "半导体")
    concept = fetcher.get_sector_flow("concept", "先进封装")

    assert result.iloc[-1]["成交额"] == 500.0
    assert result["日期"].tolist() == [pd.Timestamp("2026-07-22").date()]
    assert [item[0] for item in calls] == ["flow", "history"]
    assert concept is None


def test_sector_constituents_dispatch_by_kind_without_inventing_data_date(
    monkeypatch,
) -> None:
    calls = []

    def industry_cons(**kwargs):
        calls.append(("industry_cons", kwargs))
        return pd.DataFrame({"代码": ["000001", "600519"]})

    def concept_cons(**kwargs):
        calls.append(("concept_cons", kwargs))
        return pd.DataFrame({"代码": ["300001"]})

    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(
            stock_board_industry_cons_em=industry_cons,
            stock_board_concept_cons_em=concept_cons,
        ),
    )

    as_of = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
    industry = fetcher.get_sector_constituents(
        "industry", "半导体", as_of=as_of
    )
    concept = fetcher.get_sector_constituents(
        "concept", "先进封装", as_of=as_of
    )

    assert "数据日期" not in industry.columns
    assert "数据日期" not in concept.columns
    assert [item[0] for item in calls] == ["industry_cons", "concept_cons"]


def test_benchmark_history_uses_index_endpoint_and_preserves_000985(monkeypatch) -> None:
    calls = []

    def index_history(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [2.0]})

    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(index_zh_a_hist=index_history),
    )

    result = fetcher.get_index_history("000985")

    assert result.iloc[-1]["收盘"] == 1.0
    assert calls[0]["symbol"] == "000985"


def test_etf_snapshot_reuses_native_history_and_spot_cache_with_provider_time(
    monkeypatch,
) -> None:
    calls = []

    def history(**kwargs):
        calls.append(("history", kwargs))
        return pd.DataFrame(
            {
                "日期": ["2026-07-21", "2026-07-22"],
                "收盘": [4.0, 4.1],
                "成交额": [10_000.0, 20_000.0],
            }
        )

    def spot():
        calls.append(("spot", {}))
        return pd.DataFrame(
            {
                "代码": ["510300"],
                "最新价": [4.12],
                "成交额": [30_000.0],
                "报价时间": ["2026-07-22T14:59:00+08:00"],
                "active": [True],
            }
        )

    monkeypatch.setattr(
        akshare_module,
        "_etf_realtime_cache",
        {"data": None, "timestamp": 0, "ttl": 1200},
    )
    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(fund_etf_hist_em=history, fund_etf_spot_em=spot),
    )
    as_of = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)

    first = fetcher.get_market_radar_etf("510300", as_of=as_of)
    second = fetcher.get_market_radar_etf("510300", as_of=as_of)

    assert first["code"] == "510300"
    assert first["bars"].iloc[-1]["日期"] == "2026-07-22"
    assert first["quoted_at"] == "2026-07-22T14:59:00+08:00"
    assert first["current_price"] == 4.12
    assert first["current_traded_amount"] == 30_000.0
    assert first["active"] is True
    for unknown in (
        "suspended",
        "bid_price",
        "ask_price",
        "nav",
        "tracking_error_pct",
        "tracking_difference_pct",
        "annual_fee_pct",
        "net_assets_cny",
        "shares",
    ):
        assert first[unknown] is None
    assert second["current_price"] == 4.12
    assert [name for name, _ in calls].count("spot") == 1
    assert calls[0][1]["symbol"] == "510300"
    assert calls[0][1]["end_date"] == "20260722"


@pytest.mark.parametrize(
    "spot",
    [
        pd.DataFrame(
            {
                "代码": ["510500"],
                "最新价": [4.12],
                "成交额": [30_000.0],
                "报价时间": ["2026-07-22T14:59:00+08:00"],
            }
        ),
        pd.DataFrame(
            {"代码": ["510300"], "最新价": [4.12], "成交额": [30_000.0]}
        ),
        pd.DataFrame(
            {
                "代码": ["510300"],
                "最新价": [4.12],
                "成交额": ["not-an-amount"],
                "报价时间": ["2026-07-22T14:59:00+08:00"],
            }
        ),
    ],
    ids=["wrong-code", "missing-time", "malformed-amount"],
)
def test_etf_snapshot_omits_unverifiable_current_quote(monkeypatch, spot) -> None:
    history = pd.DataFrame(
        {"日期": ["2026-07-22"], "收盘": [4.1], "成交额": [20_000.0]}
    )
    monkeypatch.setattr(
        akshare_module,
        "_etf_realtime_cache",
        {"data": None, "timestamp": 0, "ttl": 1200},
    )
    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(
            fund_etf_hist_em=lambda **kwargs: history,
            fund_etf_spot_em=lambda: spot,
        ),
    )

    result = fetcher.get_market_radar_etf(
        "510300",
        as_of=datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc),
    )

    assert result["bars"] is history
    assert result["quoted_at"] is None
    assert result["current_price"] is None
    assert result["current_traded_amount"] is None


@pytest.mark.parametrize(
    "invoke",
    [
        lambda fetcher, clock: fetcher.get_sector_history(
            "industry",
            "semiconductor",
            deadline_monotonic=10.0,
            monotonic=clock,
        ),
        lambda fetcher, clock: fetcher.get_index_history(
            "000985",
            deadline_monotonic=10.0,
            monotonic=clock,
        ),
        lambda fetcher, clock: fetcher.get_sector_flow(
            "industry",
            "semiconductor",
            deadline_monotonic=10.0,
            monotonic=clock,
        ),
        lambda fetcher, clock: fetcher.get_sector_constituents(
            "industry",
            "semiconductor",
            deadline_monotonic=10.0,
            monotonic=clock,
        ),
    ],
    ids=["board", "index", "flow", "constituents"],
)
def test_capability_does_not_call_akshare_after_rate_limit_crosses_deadline(
    invoke, monkeypatch
) -> None:
    calls = []
    clock = SimpleNamespace(now=0.0)

    def endpoint(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    fake_akshare = SimpleNamespace(
        stock_board_industry_hist_em=endpoint,
        index_zh_a_hist=endpoint,
        stock_sector_fund_flow_hist=endpoint,
        stock_board_industry_cons_em=endpoint,
    )
    fetcher = _fetcher(monkeypatch, fake_akshare)
    monkeypatch.setattr(
        fetcher,
        "_enforce_rate_limit",
        lambda: setattr(clock, "now", 10.0),
    )

    with pytest.raises(TimeoutError, match="deadline"):
        invoke(fetcher, lambda: clock.now)

    assert calls == []


def test_flow_does_not_start_history_call_after_first_call_consumes_budget(
    monkeypatch,
) -> None:
    calls = []
    clock = SimpleNamespace(now=0.0)

    def flow(**kwargs):
        calls.append("flow")
        clock.now = 10.0
        return pd.DataFrame(
            {
                "日期": ["2026-07-22"],
                "主力净流入-净额": [100.0],
            }
        )

    def history(**kwargs):
        calls.append("history")
        return pd.DataFrame(
            {"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [500.0]}
        )

    fetcher = _fetcher(
        monkeypatch,
        SimpleNamespace(
            stock_sector_fund_flow_hist=flow,
            stock_board_industry_hist_em=history,
        ),
    )

    with pytest.raises(TimeoutError, match="deadline"):
        fetcher.get_sector_flow(
            "industry",
            "semiconductor",
            deadline_monotonic=10.0,
            monotonic=lambda: clock.now,
        )

    assert calls == ["flow"]


def test_akshare_timeout_is_capped_to_positive_remaining_budget(monkeypatch) -> None:
    captured = []
    result = pd.DataFrame(
        {"日期": ["2026-07-22"], "收盘": [1.0], "成交额": [2.0]}
    )

    def bounded_call(func, *args, **kwargs):
        captured.append(kwargs)
        return result

    fake_akshare = SimpleNamespace(index_zh_a_hist=lambda **kwargs: result)
    fetcher = _fetcher(monkeypatch, fake_akshare)
    monkeypatch.setattr(
        "data_provider.akshare_fetcher._akshare_call_with_timeout",
        bounded_call,
    )

    actual = fetcher.get_index_history(
        "000985",
        deadline_monotonic=10.0,
        monotonic=lambda: 7.25,
    )

    assert actual is result
    assert captured[0]["timeout"] == pytest.approx(2.75)
    assert captured[0]["timeout"] > 0
