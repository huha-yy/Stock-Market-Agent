from __future__ import annotations

from datetime import datetime, timezone

import sys
from types import SimpleNamespace

import pandas as pd

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
