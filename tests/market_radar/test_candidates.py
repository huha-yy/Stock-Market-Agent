from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.market_radar.candidates import CandidateSelector
from src.market_radar.models import (
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)


NOW = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
TRACKED_METRICS = tuple(SectorObservation.tracked_metric_fields)


def definition(sector_id: str, *, name: str | None = None) -> SectorDefinition:
    kind, _ = sector_id.split(":", maxsplit=1)
    return SectorDefinition(
        sector_id=sector_id,
        kind=kind,  # type: ignore[arg-type]
        name=name or sector_id,
        effective_from=date(2026, 1, 1),
    )


def observation(
    sector_id: str,
    *,
    daily_return: float | None,
    name: str | None = None,
) -> SectorObservation:
    kind, _ = sector_id.split(":", maxsplit=1)
    return SectorObservation(
        sector_id=sector_id,
        kind=kind,  # type: ignore[arg-type]
        name=name or sector_id,
        observed_at=NOW,
        source="discovery",
        freshness_seconds=0,
        quality="partial",
        return_1d_pct=daily_return,
        missing_fields=tuple(
            field_name
            for field_name in TRACKED_METRICS
            if field_name != "return_1d_pct" or daily_return is None
        ),
    )


def score(sector_id: str, state: str, *, name: str | None = None) -> SectorScore:
    kind, _ = sector_id.split(":", maxsplit=1)
    return SectorScore(
        sector_id=sector_id,
        name=name or sector_id,
        kind=kind,  # type: ignore[arg-type]
        scoring_version="cn-v1",
        gross_score=80.0,
        risk_deduction=0.0,
        score=80.0,
        confidence=0.8,
        state=state,  # type: ignore[arg-type]
        factors={},
        risk_reasons=(),
        missing_fields=(),
        source="previous-run",
        observed_at=NOW,
        quality="complete",
    )


def snapshot(*sectors: SectorScore) -> RadarRunSnapshot:
    return RadarRunSnapshot(
        run_key="cn:20260722T060000Z:manual",
        market="cn",
        trigger="manual",
        as_of=NOW,
        quality="complete",
        scoring_version="cn-v1",
        sectors=sectors,
        provider_trace=(),
    )


def test_selector_prioritizes_seeds_then_prior_states_then_round_robin_extremes() -> None:
    selected = CandidateSelector().select(
        universe=(definition("industry:seed"),),
        observations=(
            observation("industry:leader", daily_return=4.0),
            observation("industry:laggard", daily_return=-4.0),
            observation("concept:leader", daily_return=3.0),
            observation("concept:laggard", daily_return=-3.0),
        ),
        previous_snapshot=snapshot(
            score("industry:prior-leading", "leading"),
            score("concept:prior-improving", "improving"),
        ),
        limit=7,
    )

    assert [item.sector.sector_id for item in selected[:3]] == [
        "industry:seed",
        "industry:prior-leading",
        "concept:prior-improving",
    ]
    assert selected[0].observation is None
    assert [item.sector.sector_id for item in selected[3:]] == [
        "industry:leader",
        "industry:laggard",
        "concept:leader",
        "concept:laggard",
    ]


def test_selector_merges_all_priority_reasons_and_cuts_off_stably_at_sixty() -> None:
    observations = tuple(
        observation(f"industry:sector-{index:03}", daily_return=float(index))
        for index in range(61)
    )
    previous = snapshot(
        score("industry:seed", "leading"),
        score("industry:improving", "improving"),
    )
    universe = (definition("industry:seed"),)

    selected = CandidateSelector().select(universe, observations, previous, limit=60)

    assert len(selected) == 60
    assert len({item.sector.sector_id for item in selected}) == 60
    assert selected[0].reasons == ("configured_seed", "previous_leading")
    assert selected == CandidateSelector().select(
        tuple(reversed(universe)),
        tuple(reversed(observations)),
        previous,
        limit=60,
    )


def test_selector_synthesizes_discovery_definition_without_rewriting_observation() -> None:
    discovered = observation(
        "concept:discovered-only", daily_return=2.0, name="Discovered Only"
    )

    selected = CandidateSelector().select((), (discovered,), None, limit=1)

    assert selected[0].sector == SectorDefinition(
        sector_id="concept:discovered-only",
        kind="concept",
        name="Discovered Only",
        effective_from=NOW.date(),
    )
    assert selected[0].observation is discovered
    assert selected[0].reasons == ("current_concept_leader",)


def test_selector_collects_current_reasons_for_configured_and_prior_candidates() -> None:
    selected = CandidateSelector().select(
        universe=(definition("industry:configured"),),
        observations=(
            observation("industry:configured", daily_return=2.0),
            observation("industry:previous", daily_return=1.0),
        ),
        previous_snapshot=snapshot(
            score("industry:configured", "leading"),
            score("industry:previous", "improving"),
        ),
        limit=2,
    )

    assert selected[0].reasons == (
        "configured_seed",
        "previous_leading",
        "current_industry_leader",
    )
    assert selected[1].reasons == (
        "previous_improving",
        "current_industry_laggard",
    )


def test_selector_collects_current_reasons_before_priority_cutoff() -> None:
    selected = CandidateSelector().select(
        universe=(definition("industry:configured-a"), definition("industry:configured-b")),
        observations=(
            observation("industry:configured-a", daily_return=2.0),
            observation("industry:configured-b", daily_return=1.0),
        ),
        previous_snapshot=None,
        limit=1,
    )

    assert selected[0].reasons == (
        "configured_seed",
        "current_industry_leader",
    )
    assert len(selected[0].reasons) == len(set(selected[0].reasons))


def test_selector_sorts_missing_daily_returns_after_finite_values_with_sector_id_ties() -> None:
    selected = CandidateSelector().select(
        (),
        (
            observation("industry:z-tie", daily_return=1.0),
            observation("industry:missing", daily_return=None),
            observation("industry:a-tie", daily_return=1.0),
        ),
        None,
        limit=3,
    )

    assert [item.sector.sector_id for item in selected] == [
        "industry:a-tie",
        "industry:z-tie",
        "industry:missing",
    ]


@pytest.mark.parametrize(
    ("daily_return", "expected_reason"),
    (
        (0.0, "current_industry_leader"),
        (1.0, "current_industry_leader"),
        (-1.0, "current_industry_laggard"),
        (None, "current_industry_laggard"),
    ),
)
def test_single_current_row_uses_sign_to_choose_one_extreme_reason(
    daily_return: float | None,
    expected_reason: str,
) -> None:
    selected = CandidateSelector().select(
        (),
        (observation("industry:only", daily_return=daily_return),),
        None,
        limit=1,
    )

    assert selected[0].reasons == (expected_reason,)


def test_current_extreme_reasons_are_disjoint_and_cover_every_observation() -> None:
    observations = (
        observation("industry:best", daily_return=4.0),
        observation("industry:upper", daily_return=2.0),
        observation("industry:lower", daily_return=-1.0),
        observation("industry:missing", daily_return=None),
        observation("concept:best", daily_return=3.0),
        observation("concept:worst", daily_return=-3.0),
    )
    selected = CandidateSelector().select(
        universe=(definition("industry:best"),),
        observations=observations,
        previous_snapshot=snapshot(score("concept:worst", "leading")),
        limit=len(observations),
    )

    current_reasons = {
        item.sector.sector_id: tuple(
            reason for reason in item.reasons if reason.startswith("current_")
        )
        for item in selected
    }

    assert set(current_reasons) == {item.sector_id for item in observations}
    assert all(len(reasons) == 1 for reasons in current_reasons.values())
    assert current_reasons["industry:best"] == ("current_industry_leader",)
    assert current_reasons["industry:missing"] == ("current_industry_laggard",)
    assert current_reasons["concept:best"] == ("current_concept_leader",)
    assert current_reasons["concept:worst"] == ("current_concept_laggard",)


@pytest.mark.parametrize("limit", [0, -1])
def test_selector_rejects_nonpositive_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        CandidateSelector().select((), (), None, limit=limit)
