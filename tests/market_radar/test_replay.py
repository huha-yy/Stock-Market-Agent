from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.config import Config
from src.market_radar.models import (
    DataQuality,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
)
from src.market_radar.observation_builder import (
    ConstituentEvidence,
    canonical_constituent_set_key,
)
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.replay import MarketRadarReplayEngine, ReplayFrame
from src.storage import DatabaseManager, RadarConstituentSetRecord


START = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)
TRACKED_METRICS = tuple(SectorObservation.tracked_metric_fields)


def observation(
    observed_at: datetime,
    return_1d_pct: float | None,
    *,
    sector_id: str = "industry:semiconductor",
    name: str = "Semiconductor",
    quality: DataQuality = "partial",
    constituent_set_key: str | None = None,
    membership_source: str = "membership-fixture",
) -> SectorObservation:
    raw_reference = {"return_1d_pct": return_1d_pct}
    if constituent_set_key is not None:
        raw_reference.update(
            {
                "schema": "market-radar-observation-v2a",
                "data_date": observed_at.date(),
                "capabilities": {
                    "membership": {"source": membership_source},
                },
                "constituent_set_key": constituent_set_key,
            }
        )
    return SectorObservation(
        sector_id=sector_id,
        kind="industry",
        name=name,
        observed_at=observed_at,
        source="fixture",
        freshness_seconds=0,
        quality=quality,
        return_1d_pct=return_1d_pct,
        missing_fields=tuple(
            field
            for field in TRACKED_METRICS
            if field != "return_1d_pct" or return_1d_pct is None
        ),
        raw_reference=raw_reference,
    )


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "replay.db"))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield MarketRadarRepository(db)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_replay_is_chronological_and_uses_deterministic_replay_identity() -> None:
    frames = [
        ReplayFrame(
            as_of=START,
            observations=[observation(START, 1.0)],
        ),
        ReplayFrame(
            as_of=START + timedelta(days=1),
            observations=[observation(START + timedelta(days=1), 2.0)],
        ),
    ]

    snapshots = MarketRadarReplayEngine(RankingConfig()).replay(frames)

    assert [item.as_of for item in snapshots] == [frame.as_of for frame in frames]
    assert [item.run_key for item in snapshots] == [
        "cn:20260720T070000Z:replay",
        "cn:20260721T070000Z:replay",
    ]
    assert all(item.trigger == "replay" for item in snapshots)
    assert all(
        item.provider_trace == ({"source": "replay_frame", "result": "ok"},)
        for item in snapshots
    )


def test_replay_allows_duplicate_frame_times_and_preserves_input_order() -> None:
    frames = [
        ReplayFrame(
            as_of=START,
            observations=[observation(START, 1.0)],
        ),
        ReplayFrame(
            as_of=START,
            observations=[observation(START, 2.0)],
        ),
    ]

    snapshots = MarketRadarReplayEngine(RankingConfig()).replay(frames)

    assert [item.run_key for item in snapshots] == [
        "cn:20260720T070000Z:replay",
        "cn:20260720T070000Z:replay",
    ]
    assert [
        item.sectors[0].model_dump(mode="json")["observation"]["return_1d_pct"]
        for item in snapshots
    ] == [1.0, 2.0]


def test_second_precision_keys_collide_for_distinct_subsecond_instants() -> None:
    first = START.replace(microsecond=100)
    second = START.replace(microsecond=900)
    frames = [
        ReplayFrame(as_of=first, observations=[]),
        ReplayFrame(as_of=second, observations=[]),
    ]

    snapshots = MarketRadarReplayEngine(RankingConfig()).replay(frames)

    assert first < second
    assert snapshots[0].run_key == snapshots[1].run_key


def test_replay_frame_copies_input_into_an_immutable_observation_tuple() -> None:
    first = observation(START, 1.0)
    second = observation(START, 2.0)
    caller_observations = [first]

    frame = ReplayFrame(as_of=START, observations=caller_observations)
    caller_observations.append(second)

    assert frame.observations == (first,)
    with pytest.raises(AttributeError):
        frame.observations.append(second)


def test_replay_compares_frame_order_by_absolute_instant_across_timezones() -> None:
    cn_start = datetime(
        2026,
        7,
        21,
        0,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    later_utc = datetime(2026, 7, 20, 16, 45, tzinfo=timezone.utc)
    frames = [
        ReplayFrame(as_of=cn_start, observations=[]),
        ReplayFrame(as_of=later_utc, observations=[]),
    ]

    snapshots = MarketRadarReplayEngine(RankingConfig()).replay(frames)

    assert [item.run_key for item in snapshots] == [
        "cn:20260720T163000Z:replay",
        "cn:20260720T164500Z:replay",
    ]
    assert snapshots[0].as_of == cn_start
    assert snapshots[1].as_of == later_utc


def test_replay_rejects_future_observation() -> None:
    frame = ReplayFrame(
        as_of=START,
        observations=[observation(START + timedelta(minutes=1), 1.0)],
    )

    with pytest.raises(ValueError, match="future observation"):
        MarketRadarReplayEngine(RankingConfig()).replay([frame])


def test_replay_allows_observation_at_same_instant_with_different_offset() -> None:
    same_instant = START.astimezone(timezone(timedelta(hours=8)))
    frame = ReplayFrame(
        as_of=START,
        observations=[observation(same_instant, 1.0)],
    )

    snapshot = MarketRadarReplayEngine(RankingConfig()).replay([frame])[0]

    assert snapshot.sectors[0].observed_at == START


def test_replay_rejects_out_of_order_frames_across_timezones() -> None:
    first = datetime(
        2026,
        7,
        21,
        0,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    earlier = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
    frames = [
        ReplayFrame(as_of=first, observations=[]),
        ReplayFrame(as_of=earlier, observations=[]),
    ]

    with pytest.raises(ValueError, match="chronological order"):
        MarketRadarReplayEngine(RankingConfig()).replay(frames)


def test_replay_rejects_out_of_order_dst_folds_with_same_zoneinfo() -> None:
    zone = ZoneInfo("America/New_York")
    earlier = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    later = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    frames = [
        ReplayFrame(as_of=later, observations=[]),
        ReplayFrame(as_of=earlier, observations=[]),
    ]

    assert earlier == later
    assert earlier.astimezone(timezone.utc) < later.astimezone(timezone.utc)
    with pytest.raises(ValueError, match="chronological order"):
        MarketRadarReplayEngine(RankingConfig()).replay(frames)


def test_replay_rejects_future_observation_in_later_dst_fold() -> None:
    zone = ZoneInfo("America/New_York")
    frame_as_of = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    future_observed_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    frame = ReplayFrame(
        as_of=frame_as_of,
        observations=[observation(future_observed_at, 1.0)],
    )

    assert future_observed_at == frame_as_of
    assert future_observed_at.astimezone(timezone.utc) > frame_as_of.astimezone(
        timezone.utc
    )
    with pytest.raises(ValueError, match="future observation"):
        MarketRadarReplayEngine(RankingConfig()).replay([frame])


def test_replay_rejects_timezone_naive_frame() -> None:
    frame = ReplayFrame(
        as_of=datetime(2026, 7, 20, 7, 0),
        observations=[],
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        MarketRadarReplayEngine(RankingConfig()).replay([frame])


def test_replay_scores_are_exactly_the_direct_ranking_output() -> None:
    observations = [
        observation(
            START,
            3.0,
            sector_id="industry:strong",
            name="Strong",
        ),
        observation(
            START,
            -2.0,
            sector_id="industry:weak",
            name="Weak",
        ),
    ]
    config = RankingConfig()

    snapshot = MarketRadarReplayEngine(config).replay(
        [ReplayFrame(as_of=START, observations=observations)]
    )[0]

    assert list(snapshot.sectors) == score_sectors(observations, config)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        ([], "unavailable"),
        ([observation(START, 1.0, quality="complete")], "complete"),
        ([observation(START, 1.0, quality="stale")], "stale"),
        (
            [
                observation(START, 1.0, quality="complete"),
                observation(
                    START,
                    None,
                    sector_id="industry:unavailable",
                    name="Unavailable",
                    quality="unavailable",
                ),
            ],
            "partial",
        ),
    ],
)
def test_replay_aggregates_frame_quality(
    observations: list[SectorObservation],
    expected: DataQuality,
) -> None:
    snapshot = MarketRadarReplayEngine(RankingConfig()).replay(
        [ReplayFrame(as_of=START, observations=observations)]
    )[0]

    assert snapshot.quality == expected


def _persisted_replay_fixture() -> tuple[
    ConstituentEvidence,
    SectorDefinition,
    RadarRunSnapshot,
]:
    codes = ("000001", "300750", "600519")
    set_key = canonical_constituent_set_key(
        "cn",
        "industry:semiconductor",
        "membership-fixture",
        codes,
    )
    evidence = ConstituentEvidence(
        market="cn",
        sector_id="industry:semiconductor",
        source="membership-fixture",
        data_date=START.date(),
        observed_at=START,
        codes=codes,
        set_key=set_key,
    )
    item = observation(START, 2.0, constituent_set_key=set_key)
    config = RankingConfig()
    snapshot = RadarRunSnapshot(
        run_key="cn:20260720T070000Z:manual",
        market="cn",
        trigger="manual",
        as_of=START,
        quality="partial",
        scoring_version=config.scoring_version,
        sectors=score_sectors([item], config),
        provider_trace=[{"source": "fixture", "result": "ok"}],
    )
    sector = SectorDefinition(
        sector_id=item.sector_id,
        kind=item.kind,
        name=item.name,
        effective_from=date(2026, 1, 1),
    )
    return evidence, sector, snapshot


def test_repository_backed_replay_resolves_evidence_without_a_provider(
    repository,
) -> None:
    evidence, sector, stored = _persisted_replay_fixture()
    repository.save_enriched_run([sector], [evidence], stored)
    provider_calls: list[str] = []

    class ProviderSpy:
        def __getattr__(self, name: str):
            provider_calls.append(name)
            raise AssertionError("persisted replay must not access a live provider")

    repository.provider = ProviderSpy()
    replayed = MarketRadarReplayEngine(RankingConfig()).replay_persisted_run(
        repository,
        stored.run_key,
    )

    assert replayed.run_key == "cn:20260720T070000Z:replay"
    assert replayed.sectors == stored.sectors
    assert repository.resolve_snapshot_constituent_evidence(stored) == (evidence,)
    assert provider_calls == []


def test_repository_backed_replay_resolves_evidence_before_scoring(
    repository,
    monkeypatch,
) -> None:
    evidence, sector, stored = _persisted_replay_fixture()
    repository.save_enriched_run([sector], [evidence], stored)
    with repository.db.session_scope() as session:
        row = session.get(RadarConstituentSetRecord, evidence.set_key)
        assert row is not None
        session.delete(row)
    scoring_calls = 0

    def score_spy(*_args, **_kwargs):
        nonlocal scoring_calls
        scoring_calls += 1
        return []

    monkeypatch.setattr("src.market_radar.replay.score_sectors", score_spy)

    with pytest.raises(ValueError, match="missing referenced constituent set"):
        MarketRadarReplayEngine(RankingConfig()).replay_persisted_run(
            repository,
            stored.run_key,
        )

    assert scoring_calls == 0
