from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

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
    MarketRadarEnrichmentConfig,
)


NOW = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)


def test_capability_result_rejects_naive_time_and_non_finite_payloads() -> None:
    with pytest.raises(ValidationError):
        CapabilityResult[BoardBarSeries](
            capability="board_history",
            status="ok",
            data=BoardBarSeries(
                code="BK001",
                bars=[
                    BoardBar(
                        data_date=date(2026, 7, 22),
                        close=float("nan"),
                        traded_amount=1.0,
                    )
                ],
            ),
            source="fixture",
            observed_at=datetime(2026, 7, 22),
            data_date=date(2026, 7, 22),
            bar_status="finalized",
            freshness_seconds=0,
            trace=(),
            error=None,
        )


def test_normalized_payloads_are_ordered_immutable_and_timezone_aware() -> None:
    bar_series = BoardBarSeries(
        code="BK001",
        bars=[
            BoardBar(data_date=date(2026, 7, 21), close=10.0, traded_amount=100.0),
            BoardBar(data_date=date(2026, 7, 22), close=11.0, traded_amount=110.0),
        ],
    )
    flow_series = BoardFlowSeries(
        code="BK001",
        flows=[
            BoardFlow(
                data_date=date(2026, 7, 22),
                net_main_inflow=5.0,
                traded_amount=110.0,
            )
        ],
    )
    membership = ConstituentMembership(
        codes=["000001", "600519"], data_date=date(2026, 7, 22)
    )
    unversioned_membership = ConstituentMembership(
        codes=["000001", "600519"], data_date=None
    )
    quotes = ConstituentQuoteBatch(
        quotes=[
            ConstituentQuote(
                code="000001",
                current_price=10.0,
                previous_close=9.5,
                traded_amount=100.0,
                quoted_at=NOW,
            )
        ]
    )

    assert isinstance(bar_series.bars, tuple)
    assert isinstance(flow_series.flows, tuple)
    assert isinstance(membership.codes, tuple)
    assert unversioned_membership.data_date is None
    assert isinstance(quotes.quotes, tuple)
    with pytest.raises(ValidationError, match="timezone-aware"):
        ConstituentQuote(
            code="000001",
            current_price=10.0,
            previous_close=9.5,
            traded_amount=100.0,
            quoted_at=datetime(2026, 7, 22),
        )


def test_normalized_series_reject_non_monotonic_dates_and_non_finite_numbers() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        BoardBarSeries(
            code="BK001",
            bars=[
                BoardBar(data_date=date(2026, 7, 22), close=11.0, traded_amount=110.0),
                BoardBar(data_date=date(2026, 7, 21), close=10.0, traded_amount=100.0),
            ],
        )
    with pytest.raises(ValidationError):
        BoardFlow(
            data_date=date(2026, 7, 22),
            net_main_inflow=float("inf"),
            traded_amount=1.0,
        )


def test_etf_capability_data_is_ordered_immutable_and_preserves_optional_facts() -> None:
    payload = EtfCapabilityData(
        code="510300",
        bars=[
            EtfBar(data_date=date(2026, 7, 21), close=4.0, traded_amount=100.0),
            EtfBar(data_date=date(2026, 7, 22), close=4.1, traded_amount=120.0),
        ],
        quoted_at=NOW,
        current_price=4.12,
        current_traded_amount=150.0,
        active=True,
        suspended=False,
        bid_price=4.11,
        ask_price=4.12,
        nav=4.10,
        tracking_error_pct=0.2,
        tracking_difference_pct=-0.1,
        annual_fee_pct=0.6,
        net_assets_cny=10_000_000.0,
        shares=2_000_000.0,
    )

    assert isinstance(payload.bars, tuple)
    assert payload.bars[-1].data_date == date(2026, 7, 22)
    assert payload.tracking_difference_pct == -0.1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": 1.0},
                {"data_date": date(2026, 7, 22), "close": 4.2, "traded_amount": 2.0},
            ],
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 0.0, "traded_amount": 1.0}
            ],
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": -1.0}
            ],
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": 1.0}
            ],
            "quoted_at": datetime(2026, 7, 22),
            "current_price": 4.1,
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": 1.0}
            ],
            "current_price": float("nan"),
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": 1.0}
            ],
            "active": True,
        },
        {
            "code": "510300",
            "bars": [
                {"data_date": date(2026, 7, 22), "close": 4.1, "traded_amount": 1.0}
            ],
            "suspended": False,
        },
    ],
)
def test_etf_capability_data_rejects_malformed_or_non_finite_evidence(payload) -> None:
    with pytest.raises(ValidationError):
        EtfCapabilityData(**payload)


def test_enrichment_config_uses_approved_defaults_and_runtime_values() -> None:
    assert MarketRadarEnrichmentConfig() == MarketRadarEnrichmentConfig(
        candidate_limit=60,
        total_budget_seconds=180,
        max_concurrency=6,
        constituent_min_count=5,
        constituent_coverage_ratio=0.80,
        price_divergence_threshold_pct=1.0,
        flow_divergence_threshold_pct=0.1,
        default_benchmark_code="000985",
    )
    assert MarketRadarEnrichmentConfig.from_runtime(75, 240, 4) == (
        MarketRadarEnrichmentConfig(
            candidate_limit=75,
            total_budget_seconds=240,
            max_concurrency=4,
        )
    )


@pytest.mark.parametrize(
    ("limit", "budget_seconds", "max_concurrency"),
    [(0, 180, 6), (60, 9, 6), (60, 180, 17)],
)
def test_enrichment_runtime_rejects_out_of_bounds_values(
    limit: int, budget_seconds: int, max_concurrency: int
) -> None:
    with pytest.raises(ValidationError):
        MarketRadarEnrichmentConfig.from_runtime(limit, budget_seconds, max_concurrency)
