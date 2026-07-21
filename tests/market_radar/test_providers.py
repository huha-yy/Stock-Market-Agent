from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.market_radar.models import SectorDefinition
from src.market_radar.providers import LegacyRankingProvider, ProviderBatch


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


class FakeManager:
    def get_sector_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [
                {"name": "Semiconductor", "change_pct": 2.5},
                {"name": "Zero Return", "change_pct": 0.0},
            ],
            [{"name": "Coal", "change_pct": -1.2}],
            [{"provider": "AkshareFetcher", "result": "ok", "duration_ms": 12}],
            "",
        )

    def get_concept_rankings_with_meta(self, n: int):
        assert n == 1000
        return (
            [{"name": "AI Computing", "change_pct": 3.1}],
            [],
            [{"provider": "AkshareFetcher", "result": "ok", "duration_ms": 9}],
            "",
        )


def test_legacy_adapter_preserves_partial_quality_missing_metrics_and_provenance() -> None:
    universe = [
        SectorDefinition(
            sector_id="industry:semiconductor",
            kind="industry",
            name="Semiconductor",
            aliases=["Chips"],
            effective_from=NOW.date(),
        )
    ]

    batch = LegacyRankingProvider(FakeManager(), limit=1000).fetch("cn", NOW, universe)

    semiconductor = next(
        item for item in batch.observations if item.name == "Semiconductor"
    )
    zero_return = next(item for item in batch.observations if item.name == "Zero Return")
    assert semiconductor.return_1d_pct == 2.5
    assert semiconductor.quality == "partial"
    assert semiconductor.source == "AkshareFetcher"
    assert set(semiconductor.missing_fields) == {
        field for field in semiconductor.tracked_metric_fields if field != "return_1d_pct"
    }
    assert zero_return.return_1d_pct == 0.0
    assert "return_1d_pct" not in zero_return.missing_fields
    assert batch.trace[0]["dataset"] == "industry"
    assert batch.trace[0]["provider"] == "AkshareFetcher"
    assert [item.sector_id for item in batch.discovered_sectors] == [
        "concept:ai-computing",
        "industry:coal",
        "industry:zero-return",
    ]


def test_legacy_adapter_rejects_unsupported_market() -> None:
    provider = LegacyRankingProvider(FakeManager(), limit=1000)

    with pytest.raises(ValueError, match="^Market Radar Phase 1 supports market=cn only$"):
        provider.fetch("hk", NOW, [])


@pytest.mark.parametrize(
    ("row", "case"),
    [
        ({"name": "Missing Change"}, "missing"),
        ({"name": "Text Change", "change_pct": "not-a-number"}, "nonnumeric"),
        ({"name": "Nan Change", "change_pct": float("nan")}, "nan"),
        ({"name": "Positive Infinity", "change_pct": float("inf")}, "positive infinity"),
        ({"name": "Negative Infinity", "change_pct": float("-inf")}, "negative infinity"),
    ],
)
def test_legacy_adapter_marks_invalid_change_pct_as_unavailable(
    row: dict[str, object], case: str
) -> None:
    class InvalidValueManager:
        def get_sector_rankings_with_meta(self, n: int):
            return [row], [], [{"provider": "Legacy", "result": "ok", "duration_ms": 1}], ""

        def get_concept_rankings_with_meta(self, n: int):
            return [], [], [], ""

    batch = LegacyRankingProvider(InvalidValueManager()).fetch("cn", NOW, [])

    observation = batch.observations[0]
    assert observation.return_1d_pct is None, case
    assert set(observation.missing_fields) == set(observation.tracked_metric_fields), case
    assert observation.quality == "partial"
    assert observation.raw_reference == row


def test_legacy_adapter_dedupes_aliases_by_final_sector_id_with_first_row_winning() -> None:
    class AliasManager:
        def get_sector_rankings_with_meta(self, n: int):
            return (
                [{"name": "Semiconductor", "change_pct": 2.5}],
                [{"name": "Chips", "change_pct": -1.2}],
                [{"provider": "Legacy", "result": "ok", "duration_ms": 1}],
                "",
            )

        def get_concept_rankings_with_meta(self, n: int):
            return [], [], [], ""

    universe = [
        SectorDefinition(
            sector_id="industry:semiconductor",
            kind="industry",
            name="Semiconductor",
            aliases=["Chips"],
            effective_from=NOW.date(),
        )
    ]

    batch = LegacyRankingProvider(AliasManager()).fetch("cn", NOW, universe)

    assert [item.sector_id for item in batch.observations] == ["industry:semiconductor"]
    assert batch.observations[0].return_1d_pct == 2.5
    assert batch.observations[0].raw_reference == {
        "name": "Semiconductor",
        "change_pct": 2.5,
    }


def test_provider_batch_rejects_field_reassignment_and_serializes_conventionally() -> None:
    batch = ProviderBatch(
        observations=[],
        trace=[{"dataset": "industry", "provider": "Legacy", "result": "ok"}],
    )

    with pytest.raises(ValidationError, match="frozen"):
        batch.trace = []
    assert batch.model_dump() == {
        "observations": [],
        "trace": [{"dataset": "industry", "provider": "Legacy", "result": "ok"}],
        "discovered_sectors": [],
    }


def test_manager_metadata_methods_preserve_fallback_chain() -> None:
    from data_provider.base import DataFetcherManager

    class EmptyFetcher:
        name = "EmptyFetcher"
        priority = 0

        def get_sector_rankings(self, n: int):
            return None

        def get_concept_rankings(self, n: int):
            return None

    class WorkingFetcher:
        name = "WorkingFetcher"
        priority = 1

        def get_sector_rankings(self, n: int):
            return ([{"name": "Bank", "change_pct": 1.0}], [])

        def get_concept_rankings(self, n: int):
            return ([{"name": "Special Valuation", "change_pct": 1.5}], [])

    manager = DataFetcherManager(fetchers=[EmptyFetcher(), WorkingFetcher()])
    _, _, sector_trace, _ = manager.get_sector_rankings_with_meta(100)
    _, _, concept_trace, _ = manager.get_concept_rankings_with_meta(100)

    assert [item["provider"] for item in sector_trace] == [
        "EmptyFetcher",
        "WorkingFetcher",
    ]
    assert [item["result"] for item in sector_trace] == ["empty", "ok"]
    assert [item["provider"] for item in concept_trace] == [
        "EmptyFetcher",
        "WorkingFetcher",
    ]


def test_manager_concept_metadata_all_empty_preserves_legacy_error_and_trace() -> None:
    from data_provider.base import DataFetcherManager

    class EmptyFetcher:
        name = "EmptyFetcher"
        priority = 0

        def get_concept_rankings(self, n: int):
            return None

    manager = DataFetcherManager(fetchers=[EmptyFetcher()])

    top, bottom, trace, last_error = manager.get_concept_rankings_with_meta(100)

    assert top == []
    assert bottom == []
    assert trace == [
        {
            "provider": "EmptyFetcher",
            "result": "empty",
            "duration_ms": trace[0]["duration_ms"],
            "error": "EmptyFetcher返回空结果",
        }
    ]
    assert last_error == "EmptyFetcher返回空结果"


def test_manager_concept_metadata_records_exception_without_breaking_trace_order() -> None:
    from data_provider.base import DataFetcherManager

    class EmptyFetcher:
        name = "EmptyFetcher"
        priority = 0

        def get_concept_rankings(self, n: int):
            return None

    class FailingFetcher:
        name = "FailingFetcher"
        priority = 1

        def get_concept_rankings(self, n: int):
            raise RuntimeError("upstream unavailable")

    manager = DataFetcherManager(fetchers=[EmptyFetcher(), FailingFetcher()])

    _, _, trace, last_error = manager.get_concept_rankings_with_meta(100)

    assert [item["provider"] for item in trace] == ["EmptyFetcher", "FailingFetcher"]
    assert [item["result"] for item in trace] == ["empty", "failed"]
    assert trace[0]["error"] == "EmptyFetcher返回空结果"
    assert trace[1]["error"] == "upstream unavailable"
    assert last_error == "FailingFetcher (RuntimeError) upstream unavailable"
