from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
)
from src.market_radar.providers import ProviderBatch
from src.market_radar.ranking import RankingConfig
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.service import MarketRadarService
from src.storage import DatabaseManager


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
TRACKED_METRICS = tuple(SectorObservation.tracked_metric_fields)


def _definition(
    sector_id: str = "industry:semiconductor",
    *,
    kind: str = "industry",
    name: str = "Semiconductor",
) -> SectorDefinition:
    return SectorDefinition(
        sector_id=sector_id,
        kind=kind,
        name=name,
        effective_from=date(2026, 1, 1),
    )


def _observation(
    sector_id: str = "industry:semiconductor",
    *,
    kind: str = "industry",
    name: str = "Semiconductor",
    quality: str = "partial",
    return_1d_pct: float | None = 2.0,
) -> SectorObservation:
    return SectorObservation(
        sector_id=sector_id,
        kind=kind,
        name=name,
        observed_at=NOW,
        source="fixture",
        freshness_seconds=0,
        quality=quality,
        return_1d_pct=return_1d_pct,
        missing_fields=tuple(
            field
            for field in TRACKED_METRICS
            if field != "return_1d_pct" or return_1d_pct is None
        ),
        raw_reference={"source_row": {"name": name}},
    )


class FakeUniverse:
    def __init__(
        self,
        sectors: list[SectorDefinition] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.sectors = sectors if sectors is not None else [_definition()]
        self.events = events
        self.loaded_as_of: date | None = None

    def load(self, as_of: date) -> list[SectorDefinition]:
        self.loaded_as_of = as_of
        if self.events is not None:
            self.events.append("load")
        return self.sectors


class FakeProvider:
    def __init__(
        self,
        batch: ProviderBatch | None = None,
        events: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.batch = batch or ProviderBatch(
            observations=[_observation()],
            trace=[{"source": "fixture", "result": {"status": "ok"}}],
        )
        self.events = events
        self.error = error
        self.arguments: tuple[str, datetime, list[SectorDefinition]] | None = None

    def fetch(
        self,
        market: str,
        as_of: datetime,
        universe: list[SectorDefinition],
    ) -> ProviderBatch:
        self.arguments = (market, as_of, universe)
        if self.events is not None:
            self.events.append("fetch")
        if self.error is not None:
            raise self.error
        return self.batch


class FakeRepository:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.universe: list[SectorDefinition] | None = None
        self.snapshot: RadarRunSnapshot | None = None

    def save_run_with_universe(
        self,
        sectors: list[SectorDefinition],
        snapshot: RadarRunSnapshot,
    ) -> int:
        if self.events is not None:
            self.events.append("persist")
        if self.error is not None:
            raise self.error
        self.universe = sectors
        self.snapshot = snapshot
        return 7

    def get_run(self, run_id: int) -> RadarRunSnapshot | None:
        if self.events is not None:
            self.events.append("get")
        return self.snapshot if run_id == 7 else None


def _service(
    *,
    universe: FakeUniverse | None = None,
    provider: FakeProvider | None = None,
    repository: FakeRepository | MarketRadarRepository | None = None,
    clock=lambda: NOW,
) -> MarketRadarService:
    return MarketRadarService(
        universe_loader=universe or FakeUniverse(),
        provider=provider or FakeProvider(),
        repository=repository or FakeRepository(),
        ranking_config=RankingConfig(),
        clock=clock,
    )


def test_run_builds_stable_snapshot_and_persists_in_dependency_order() -> None:
    events: list[str] = []
    universe = FakeUniverse(events=events)
    provider = FakeProvider(events=events)
    repository = FakeRepository(events=events)

    snapshot = _service(
        universe=universe,
        provider=provider,
        repository=repository,
    ).run()

    assert snapshot.run_key == "cn:20260721T060000Z:manual"
    assert snapshot.as_of == NOW
    assert snapshot.quality == "partial"
    assert snapshot.scoring_version == "cn-v1"
    assert [item.sector_id for item in snapshot.sectors] == [
        "industry:semiconductor"
    ]
    assert snapshot.sectors[0].model_dump(mode="json")[
        "observation"
    ] == _observation().model_dump(mode="json")
    assert snapshot.provider_trace == (
        {"source": "fixture", "result": {"status": "ok"}},
    )
    assert universe.loaded_as_of == NOW.date()
    assert provider.arguments == ("cn", NOW, universe.sectors)
    assert repository.snapshot == snapshot
    assert events == ["load", "fetch", "persist", "get"]


def test_explicit_as_of_and_trigger_determine_key_without_reading_clock() -> None:
    as_of = datetime(
        2026,
        7,
        21,
        14,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )

    def unexpected_clock() -> datetime:
        raise AssertionError("clock must not be read when as_of is explicit")

    snapshot = _service(clock=unexpected_clock).run(
        as_of=as_of,
        trigger="replay",
        persist=False,
    )

    assert snapshot.run_key == "cn:20260721T063000Z:replay"
    assert snapshot.as_of == as_of.astimezone(timezone.utc)


def test_run_canonicalizes_equivalent_instants_and_uses_cn_market_date() -> None:
    utc_as_of = datetime(2026, 7, 20, 16, 30, tzinfo=timezone.utc)
    cn_as_of = datetime(
        2026,
        7,
        21,
        0,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    first_universe = FakeUniverse()
    second_universe = FakeUniverse()
    first_provider = FakeProvider()
    second_provider = FakeProvider()

    first = _service(
        universe=first_universe,
        provider=first_provider,
    ).run(as_of=utc_as_of, persist=False)
    second = _service(
        universe=second_universe,
        provider=second_provider,
    ).run(as_of=cn_as_of, persist=False)

    assert first.run_key == second.run_key == "cn:20260720T163000Z:manual"
    assert first.as_of == second.as_of == utc_as_of
    assert first_universe.loaded_as_of == second_universe.loaded_as_of == date(
        2026, 7, 21
    )
    assert first_provider.arguments[1] == second_provider.arguments[1] == utc_as_of


def test_injected_clock_is_read_exactly_once() -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    snapshot = _service(clock=clock).run(persist=False)

    assert calls == 1
    assert snapshot.as_of == NOW


@pytest.mark.parametrize(
    ("qualities", "expected"),
    [
        ([], "unavailable"),
        (["unavailable"], "unavailable"),
        (["complete", "unavailable"], "partial"),
        (["complete", "partial"], "partial"),
        (["complete", "stale"], "stale"),
        (["partial", "stale", "unavailable"], "stale"),
        (["complete", "complete"], "complete"),
    ],
)
def test_run_aggregates_observation_quality(
    qualities: list[str],
    expected: str,
) -> None:
    observations = [
        _observation(
            sector_id=f"industry:sector-{position}",
            name=f"Sector {position}",
            quality=quality,
            return_1d_pct=None if quality == "unavailable" else float(position),
        )
        for position, quality in enumerate(qualities)
    ]
    provider = FakeProvider(
        ProviderBatch(
            observations=observations,
            trace=[{"source": "fixture", "result": "empty" if not qualities else "ok"}],
        )
    )

    snapshot = _service(provider=provider).run(persist=False)

    assert snapshot.quality == expected
    assert len(snapshot.sectors) == len(observations)
    assert snapshot.provider_trace[0]["result"] == (
        "empty" if not qualities else "ok"
    )


def test_persistence_syncs_configured_and_discovered_universe_stably() -> None:
    configured_etf = EtfDefinition(
        code="512480",
        name="Configured ETF",
        sector_id="industry:shared",
        benchmark_code="000300",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )
    configured = [
        SectorDefinition(
            sector_id="industry:shared",
            kind="industry",
            name="Configured name",
            aliases=["Configured alias"],
            benchmark_code="000300",
            etfs=[configured_etf],
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
        ),
        _definition("concept:zeta", kind="concept", name="Zeta"),
    ]
    discovered = [
        _definition("industry:beta", name="Beta"),
        SectorDefinition(
            sector_id="industry:shared",
            kind="industry",
            name="Discovered name",
            aliases=["Discovered alias"],
            benchmark_code="000905",
            effective_from=date(2026, 7, 1),
        ),
        _definition("concept:alpha", kind="concept", name="Alpha"),
    ]
    repository = FakeRepository()
    provider = FakeProvider(
        ProviderBatch(
            observations=[],
            trace=[],
            discovered_sectors=discovered,
        )
    )

    _service(
        universe=FakeUniverse(configured),
        provider=provider,
        repository=repository,
    ).run()

    assert [item.sector_id for item in repository.universe] == [
        "concept:alpha",
        "concept:zeta",
        "industry:beta",
        "industry:shared",
    ]
    assert repository.universe[-1] == configured[0]


def test_run_without_persistence_does_not_write() -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)

    snapshot = _service(repository=repository).run(persist=False)

    assert snapshot.sectors
    assert repository.universe is None
    assert repository.snapshot is None
    assert events == []


@pytest.mark.parametrize("market", ["hk", "us"])
def test_run_rejects_unsupported_market_before_using_dependencies(market: str) -> None:
    events: list[str] = []

    with pytest.raises(
        ValueError,
        match="^Market Radar Phase 1 supports market=cn only$",
    ):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
        ).run(market=market)

    assert events == []


def test_run_rejects_naive_as_of_before_using_dependencies() -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match="^as_of must be timezone-aware$"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
        ).run(as_of=datetime(2026, 7, 21, 6, 0))

    assert events == []


def test_provider_failure_propagates_without_persistence() -> None:
    events: list[str] = []
    error = RuntimeError("provider unavailable")
    repository = FakeRepository(events=events)

    with pytest.raises(RuntimeError, match="provider unavailable") as caught:
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events, error=error),
            repository=repository,
        ).run()

    assert caught.value is error
    assert events == ["load", "fetch"]
    assert repository.snapshot is None


def test_atomic_persistence_failure_propagates_instead_of_returning_success() -> None:
    events: list[str] = []
    error = RuntimeError("snapshot transaction failed")
    repository = FakeRepository(events=events, error=error)

    with pytest.raises(RuntimeError, match="snapshot transaction failed") as caught:
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=repository,
        ).run()

    assert caught.value is error
    assert events == ["load", "fetch", "persist"]
    assert repository.snapshot is None


@pytest.fixture()
def isolated_db(tmp_path):
    old_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "market_radar_service.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_path


def test_persisted_run_returns_first_write_winner(isolated_db) -> None:
    repository = MarketRadarRepository(isolated_db)
    original_provider = FakeProvider(
        ProviderBatch(
            observations=[_observation(return_1d_pct=2.0)],
            trace=[{"source": "original", "result": "ok"}],
        )
    )
    conflicting_provider = FakeProvider(
        ProviderBatch(
            observations=[_observation(return_1d_pct=-5.0)],
            trace=[{"source": "conflicting", "result": "ok"}],
        )
    )

    original = _service(
        provider=original_provider,
        repository=repository,
    ).run()
    retry = _service(
        provider=conflicting_provider,
        repository=repository,
    ).run()

    assert retry == original
    assert retry.provider_trace[0]["source"] == "original"


def test_failed_service_persistence_rolls_back_universe_and_snapshot(
    isolated_db,
) -> None:
    repository = MarketRadarRepository(isolated_db)
    original = _service(repository=repository).run()
    rejected_sector_id = "industry:reject-service-insert"
    later = NOW + timedelta(hours=1)
    rejected = _observation(
        sector_id=rejected_sector_id,
        name="Rejected",
    ).model_copy(update={"observed_at": later})
    provider = FakeProvider(
        ProviderBatch(
            observations=[rejected],
            trace=[{"source": "fixture", "result": "ok"}],
            discovered_sectors=[
                _definition(rejected_sector_id, name="Rejected")
            ],
        )
    )
    with isolated_db._engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER reject_market_radar_service_insert
            BEFORE INSERT ON radar_sector_snapshots
            WHEN NEW.sector_id = '{rejected_sector_id}'
            BEGIN
                SELECT RAISE(ABORT, 'rejected service sector');
            END
            """
        )

    with pytest.raises(IntegrityError, match="rejected service sector"):
        _service(
            provider=provider,
            repository=repository,
        ).run(as_of=later)

    assert repository.get_latest_run("cn") == original
    assert repository.list_universe(later.date()) == [_definition()]
