from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.market_radar.candidates import EnrichmentCandidate
from src.market_radar.capabilities import MarketRadarEnrichmentConfig
from src.market_radar.enrichment import EnrichmentBatch
from src.market_radar.etf_collection import EtfCollectionBatch
from src.market_radar.lifecycle import LifecycleContext, LifecycleEvaluation
from src.market_radar.models import (
    EtfDefinition,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
)
from src.market_radar.observation_builder import (
    ConstituentEvidence,
    canonical_constituent_set_key,
)
from src.market_radar.providers import ProviderBatch
from src.market_radar.ranking import RankingConfig
from src.market_radar.repository import MarketRadarRepository
from src.market_radar import service as service_module
from src.market_radar.service import MarketRadarService
from src.market_radar.universe import UniverseLoader
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

    def load_with_history(
        self,
        as_of: date,
    ) -> tuple[list[SectorDefinition], list[SectorDefinition]]:
        return self.load(as_of), self.sectors


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
        self.previous: RadarRunSnapshot | None = None
        self.latest_calls: list[tuple[str, datetime | None]] = []
        self.enriched_writes: list[
            tuple[list[SectorDefinition], tuple[object, ...], RadarRunSnapshot]
        ] = []
        self.lifecycle_context = LifecycleContext()
        self.lifecycle_loads = 0
        self.scheduled_writes: list[tuple[object, ...]] = []

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

    def get_latest_run(
        self,
        market: str,
        before: datetime | None = None,
    ) -> RadarRunSnapshot | None:
        if self.events is not None:
            self.events.append("latest")
        self.latest_calls.append((market, before))
        return self.previous

    def save_enriched_run(
        self,
        sectors: list[SectorDefinition],
        evidence: tuple[object, ...],
        etf_observations=(),
        snapshot: RadarRunSnapshot | None = None,
    ) -> int:
        if isinstance(etf_observations, RadarRunSnapshot):
            snapshot = etf_observations
        if self.events is not None:
            self.events.append("persist")
        if self.error is not None:
            raise self.error
        self.universe = sectors
        self.snapshot = snapshot
        self.enriched_writes.append((sectors, evidence, snapshot))
        return 7

    def load_lifecycle_context(self) -> LifecycleContext:
        if self.events is not None:
            self.events.append("load_lifecycle")
        self.lifecycle_loads += 1
        return self.lifecycle_context

    def save_scheduled_enriched_run(
        self,
        sectors,
        evidence,
        etf_observations,
        snapshot,
        evaluation,
        *,
        attempt_key,
        attempt_owner_token,
    ) -> int:
        if self.events is not None:
            self.events.append("persist_scheduled")
        self.universe = sectors
        self.snapshot = snapshot
        self.scheduled_writes.append(
            (
                sectors,
                evidence,
                etf_observations,
                snapshot,
                evaluation,
                attempt_key,
                attempt_owner_token,
            )
        )
        return 7


class RecordingSelector:
    def __init__(
        self,
        result: tuple[EnrichmentCandidate, ...] = (),
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.events = events
        self.calls: list[tuple[object, ...]] = []

    def select(self, universe, observations, previous, limit):
        if self.events is not None:
            self.events.append("select")
        self.calls.append((universe, observations, previous, limit))
        return self.result


class RecordingEnricher:
    def __init__(
        self,
        result: EnrichmentBatch,
        events: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.events = events
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    def enrich(self, candidates, as_of):
        if self.events is not None:
            self.events.append("enrich")
        self.calls.append((candidates, as_of))
        if self.error is not None:
            raise self.error
        return self.result


class RecordingLifecycleEngine:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.calls: list[tuple[object, ...]] = []

    def evaluate(self, snapshot, context, *, run_kind):
        if self.events is not None:
            self.events.append("evaluate_lifecycle")
        self.calls.append((snapshot, context, run_kind))
        return LifecycleEvaluation(
            run_key=snapshot.run_key,
            signals=(),
            transitions=(),
        )


def _service(
    *,
    universe: FakeUniverse | None = None,
    provider: FakeProvider | None = None,
    repository: FakeRepository | MarketRadarRepository | None = None,
    enricher: RecordingEnricher | None = None,
    candidate_selector: RecordingSelector | None = None,
    enrichment_config: MarketRadarEnrichmentConfig | None = None,
    etf_collector=None,
    lifecycle_engine=None,
    clock=lambda: NOW,
) -> MarketRadarService:
    service_kwargs = dict(
        universe_loader=universe or FakeUniverse(),
        provider=provider or FakeProvider(),
        repository=repository or FakeRepository(),
        ranking_config=RankingConfig(),
        enricher=enricher,
        candidate_selector=candidate_selector,
        enrichment_config=enrichment_config,
        etf_collector=etf_collector,
        clock=clock,
    )
    if lifecycle_engine is not None:
        service_kwargs["lifecycle_engine"] = lifecycle_engine
    return MarketRadarService(**service_kwargs)


class EmptyEtfCollector:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def collect(self, universe, sectors, as_of):
        if self.events is not None:
            self.events.append("collect_etfs")
        return EtfCollectionBatch((), (), as_of)


def test_schedule_run_requires_persistence_and_schedule_kind() -> None:
    service = _service(repository=FakeRepository())

    with pytest.raises(ValueError, match="schedule runs require persistence"):
        service.run(
            trigger="schedule",
            persist=False,
            schedule_kind="intraday",
        )
    with pytest.raises(ValueError, match="schedule_kind"):
        service.run(trigger="schedule", persist=True)
    with pytest.raises(ValueError, match="only valid for schedule runs"):
        service.run(trigger="manual", schedule_kind="eod")


def test_invalid_trigger_rejects_before_dependencies() -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match="trigger"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
            lifecycle_engine=RecordingLifecycleEngine(events),
            clock=lambda: (events.append("clock"), NOW)[1],
        ).run(trigger="cron")

    assert events == []


def test_invalid_schedule_kind_rejects_before_dependencies() -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match="schedule_kind"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
            enricher=RecordingEnricher(EnrichmentBatch((), (), ()), events),
            candidate_selector=RecordingSelector(events=events),
            etf_collector=EmptyEtfCollector(events),
            lifecycle_engine=RecordingLifecycleEngine(events),
            clock=lambda: (events.append("clock"), NOW)[1],
        ).run(trigger="schedule", schedule_kind="weekly")

    assert events == []


@pytest.mark.parametrize(
    ("enricher", "etf_collector", "discovery_only"),
    [
        (None, EmptyEtfCollector(), False),
        (RecordingEnricher(EnrichmentBatch((), (), ())), None, False),
        (
            RecordingEnricher(EnrichmentBatch((), (), ())),
            EmptyEtfCollector(),
            True,
        ),
    ],
)
def test_schedule_run_requires_full_phase2b_enrichment(
    enricher,
    etf_collector,
    discovery_only,
) -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match="full Phase 2B enrichment"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
            enricher=enricher,
            etf_collector=etf_collector,
            lifecycle_engine=RecordingLifecycleEngine(events),
        ).run(
            trigger="schedule",
            schedule_kind="intraday",
            discovery_only=discovery_only,
        )

    assert events == []


@pytest.mark.parametrize("schedule_kind", ["intraday", "eod"])
def test_schedule_run_saves_snapshot_and_lifecycle_once(schedule_kind) -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)
    lifecycle_engine = RecordingLifecycleEngine(events)
    service = _service(
        universe=FakeUniverse(events=events),
        provider=FakeProvider(events=events),
        repository=repository,
        enricher=RecordingEnricher(EnrichmentBatch((), (), ()), events=events),
        candidate_selector=RecordingSelector(events=events),
        etf_collector=EmptyEtfCollector(events),
        lifecycle_engine=lifecycle_engine,
    )

    result = service.run(
        trigger="schedule",
        persist=True,
        schedule_kind=schedule_kind,
        attempt_key="cn:intraday:2026-07-21:morning:1400",
        attempt_owner_token="owner-token",
    )

    assert result.trigger == "schedule"
    assert repository.lifecycle_loads == 1
    assert len(repository.scheduled_writes) == 1
    assert repository.enriched_writes == []
    assert repository.scheduled_writes[0][-2:] == (
        "cn:intraday:2026-07-21:morning:1400",
        "owner-token",
    )
    assert lifecycle_engine.calls == [
        (repository.snapshot, repository.lifecycle_context, schedule_kind)
    ]
    assert events == [
        "load",
        "fetch",
        "latest",
        "select",
        "enrich",
        "collect_etfs",
        "load_lifecycle",
        "evaluate_lifecycle",
        "persist_scheduled",
        "get",
    ]


def test_manual_run_does_not_load_or_save_lifecycle() -> None:
    repository = FakeRepository()
    lifecycle_engine = RecordingLifecycleEngine()

    _service(
        repository=repository,
        lifecycle_engine=lifecycle_engine,
    ).run(trigger="manual", persist=True)

    assert repository.lifecycle_loads == 0
    assert repository.scheduled_writes == []
    assert lifecycle_engine.calls == []


def test_phase2b_policy_runs_after_sector_scoring_and_persists_once(
    monkeypatch,
) -> None:
    events: list[str] = []

    class Collector:
        def collect(self, universe, sectors, as_of):
            events.append("collect_etfs")
            return EtfCollectionBatch((), (), as_of)

    actual_score = service_module.score_sectors
    actual_select = service_module.select_etfs
    actual_regime = service_module.assess_market_regime
    actual_position = service_module.build_position_plan

    def record(name, function):
        def wrapped(*args, **kwargs):
            events.append(name)
            return function(*args, **kwargs)
        return wrapped

    monkeypatch.setattr(service_module, "score_sectors", record("score_sectors", actual_score))
    monkeypatch.setattr(service_module, "select_etfs", record("select_etfs", actual_select))
    monkeypatch.setattr(service_module, "assess_market_regime", record("assess_regime", actual_regime))
    monkeypatch.setattr(service_module, "build_position_plan", record("build_position_plan", actual_position))
    repository = FakeRepository(events=events)

    snapshot = _service(
        universe=FakeUniverse(events=events),
        provider=FakeProvider(events=events),
        repository=repository,
        etf_collector=Collector(),
    ).run()

    assert events == [
        "load", "fetch", "score_sectors", "collect_etfs", "select_etfs",
        "assess_regime", "build_position_plan", "persist", "get",
    ]
    assert snapshot.etfs == ()
    assert snapshot.regime is not None
    assert snapshot.position_plan is not None


def _previous_snapshot(as_of: datetime) -> RadarRunSnapshot:
    return RadarRunSnapshot(
        run_key=f"cn:{as_of.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}:manual",
        market="cn",
        trigger="manual",
        as_of=as_of,
        quality="unavailable",
        scoring_version="cn-v1",
        sectors=(),
        provider_trace=(),
    )


def _evidence(
    sector_id: str,
    *,
    source: str = "membership-fixture",
    codes: tuple[str, ...] = ("000001", "600519"),
    observed_at: datetime = NOW,
) -> ConstituentEvidence:
    return ConstituentEvidence(
        market="cn",
        sector_id=sector_id,
        source=source,
        data_date=NOW.date(),
        observed_at=observed_at,
        codes=codes,
        set_key=canonical_constituent_set_key("cn", sector_id, source, codes),
    )


def _assert_enrichment_rejected_before_rank_or_persistence(
    monkeypatch,
    *,
    candidates: tuple[EnrichmentCandidate, ...],
    enrichment: EnrichmentBatch,
    match: str,
) -> None:
    repository = FakeRepository()
    monkeypatch.setattr(
        service_module,
        "score_sectors",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid enrichment output must not be ranked"
        ),
    )

    with pytest.raises(ValueError, match=match):
        _service(
            repository=repository,
            candidate_selector=RecordingSelector(candidates),
            enricher=RecordingEnricher(enrichment),
        ).run(previous_snapshot=_previous_snapshot(NOW - timedelta(hours=1)))

    assert repository.enriched_writes == []
    assert repository.snapshot is None


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
    assert snapshot.provider_trace == ({"source": "fixture"},)
    assert universe.loaded_as_of == NOW.date()
    assert provider.arguments == ("cn", NOW, universe.sectors)
    assert repository.snapshot == snapshot
    assert events == ["load", "fetch", "persist", "get"]


def test_enriched_run_selects_merges_ranks_once_and_persists_atomically(
    monkeypatch,
) -> None:
    events: list[str] = []
    shared = _definition("industry:shared", name="Shared")
    configured_only = _definition("concept:configured", kind="concept", name="Configured")
    universe = FakeUniverse([shared, configured_only], events=events)
    discovered_shared = _observation(
        "industry:shared", name="Shared", return_1d_pct=1.0
    )
    unselected = _observation(
        "concept:unselected", kind="concept", name="Unselected", return_1d_pct=-2.0
    )
    batch = ProviderBatch(
        observations=[discovered_shared, unselected],
        trace=[{"provider": "discovery", "result": "ok"}],
        discovered_sectors=[
            _definition("concept:unselected", kind="concept", name="Unselected")
        ],
    )
    provider = FakeProvider(batch, events=events)
    candidates = (
        EnrichmentCandidate(shared, discovered_shared, ("configured_seed",)),
        EnrichmentCandidate(configured_only, None, ("configured_seed",)),
    )
    selector = RecordingSelector(candidates, events=events)
    enriched_shared = _observation(
        "industry:shared", name="Shared", return_1d_pct=8.0
    ).model_copy(update={"source": "enriched"})
    enriched_configured = _observation(
        "concept:configured",
        kind="concept",
        name="Configured",
        return_1d_pct=4.0,
    ).model_copy(update={"source": "enriched"})
    enricher = RecordingEnricher(
        EnrichmentBatch(
            observations=(enriched_shared, enriched_configured),
            constituent_evidence=(),
            trace=(
                {"capability": "board_history", "result": "ok"},
                {"capability": "quotes", "result": "partial"},
            ),
        ),
        events=events,
    )
    previous = _previous_snapshot(NOW - timedelta(hours=1))
    repository = FakeRepository(events=events)
    repository.previous = previous
    ranking_calls: list[list[SectorObservation]] = []
    actual_score_sectors = service_module.score_sectors

    def record_score(observations, config):
        ranking_calls.append(list(observations))
        return actual_score_sectors(observations, config)

    monkeypatch.setattr(service_module, "score_sectors", record_score)

    snapshot = _service(
        universe=universe,
        provider=provider,
        repository=repository,
        enricher=enricher,
        candidate_selector=selector,
        enrichment_config=MarketRadarEnrichmentConfig(candidate_limit=2),
    ).run(as_of=NOW)

    assert events == ["load", "fetch", "latest", "select", "enrich", "persist", "get"]
    assert repository.latest_calls == [("cn", NOW)]
    assert selector.calls == [
        (universe.sectors, batch.observations, previous, 2)
    ]
    assert enricher.calls == [(candidates, NOW)]
    assert [[item.sector_id for item in call] for call in ranking_calls] == [[
        "industry:shared",
        "concept:unselected",
        "concept:configured",
    ]]
    assert ranking_calls[0][0] == enriched_shared
    assert ranking_calls[0][1] == unselected
    assert ranking_calls[0][2] == enriched_configured
    assert repository.enriched_writes == [
        (repository.universe, (), snapshot)
    ]
    assert snapshot.provider_trace[:1] == tuple(batch.trace)
    assert [item["stage"] for item in snapshot.provider_trace[1:]] == [
        "enrichment",
        "enrichment",
    ]
    assert [item["dataset"] for item in snapshot.provider_trace[1:]] == [
        "board_history",
        "quotes",
    ]


def test_enriched_run_persists_final_observation_anchor() -> None:
    acquired_at = NOW + timedelta(seconds=60)
    candidate = EnrichmentCandidate(
        _definition(), _observation(), ("configured_seed",)
    )
    evidence = _evidence(
        candidate.sector.sector_id,
        observed_at=acquired_at,
    )
    enriched = _observation().model_copy(
        update={
            "observed_at": acquired_at,
            "raw_reference": {"constituent_set_key": evidence.set_key},
        }
    )
    repository = FakeRepository()

    snapshot = _service(
        repository=repository,
        candidate_selector=RecordingSelector((candidate,)),
        enricher=RecordingEnricher(
            EnrichmentBatch(
                observations=(enriched,),
                constituent_evidence=(evidence,),
                trace=(),
                as_of=acquired_at,
            )
        ),
    ).run(as_of=NOW)

    assert snapshot.as_of == acquired_at
    assert snapshot.run_key == "cn:20260721T060100Z:manual"
    assert repository.enriched_writes == [
        (repository.universe, (evidence,), snapshot)
    ]


def test_enriched_run_derives_missing_batch_anchor_from_observations() -> None:
    acquired_at = NOW + timedelta(seconds=60)
    candidate = EnrichmentCandidate(
        _definition(), _observation(), ("configured_seed",)
    )
    enriched = _observation().model_copy(update={"observed_at": acquired_at})

    snapshot = _service(
        candidate_selector=RecordingSelector((candidate,)),
        enricher=RecordingEnricher(
            EnrichmentBatch(
                observations=(enriched,),
                constituent_evidence=(),
                trace=(),
            )
        ),
    ).run(as_of=NOW)

    assert snapshot.as_of == acquired_at
    assert snapshot.run_key == "cn:20260721T060100Z:manual"


def test_enriched_run_rejects_naive_constituent_evidence_time() -> None:
    candidate = EnrichmentCandidate(
        _definition(), _observation(), ("configured_seed",)
    )
    evidence = _evidence(
        candidate.sector.sector_id,
        observed_at=NOW.replace(tzinfo=None),
    )
    enriched = _observation().model_copy(
        update={"raw_reference": {"constituent_set_key": evidence.set_key}}
    )

    with pytest.raises(
        ValueError,
        match="constituent evidence observed_at must be timezone-aware",
    ):
        _service(
            candidate_selector=RecordingSelector((candidate,)),
            enricher=RecordingEnricher(
                EnrichmentBatch(
                    observations=(enriched,),
                    constituent_evidence=(evidence,),
                    trace=(),
                )
            ),
        ).run(as_of=NOW, persist=False)


def test_explicit_previous_wins_and_nonpersistent_run_never_reads_repository() -> None:
    explicit = _previous_snapshot(NOW - timedelta(hours=2))
    repository = FakeRepository()
    repository.previous = _previous_snapshot(NOW - timedelta(hours=1))
    selector = RecordingSelector()
    enricher = RecordingEnricher(EnrichmentBatch((), (), ()))

    _service(
        repository=repository,
        candidate_selector=selector,
        enricher=enricher,
    ).run(persist=False, previous_snapshot=explicit)

    assert repository.latest_calls == []
    assert selector.calls[0][2] is explicit


def test_discovery_only_and_missing_enricher_never_read_previous_state() -> None:
    repository = FakeRepository()
    selector = RecordingSelector()
    enricher = RecordingEnricher(EnrichmentBatch((), (), ()))

    _service(
        repository=repository,
        candidate_selector=selector,
        enricher=enricher,
    ).run(discovery_only=True)
    _service(repository=repository).run()

    assert repository.latest_calls == []
    assert selector.calls == []
    assert enricher.calls == []


@pytest.mark.parametrize(
    ("previous", "message"),
    [
        (_previous_snapshot(NOW), "strictly earlier"),
        (_previous_snapshot(NOW + timedelta(seconds=1)), "strictly earlier"),
        (
            _previous_snapshot(NOW - timedelta(seconds=1)).model_copy(
                update={"as_of": datetime(2026, 7, 21, 5, 59, 59)}
            ),
            "timezone-aware",
        ),
        (
            _previous_snapshot(NOW - timedelta(seconds=1)).model_copy(
                update={"market": "hk"}
            ),
            "market=cn",
        ),
    ],
)
def test_previous_snapshot_is_validated_before_loading_data(
    previous: RadarRunSnapshot,
    message: str,
) -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match=message):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
        ).run(previous_snapshot=previous)

    assert events == []


def test_duplicate_enrichment_output_aborts_before_ranking_or_persistence(
    monkeypatch,
) -> None:
    duplicate = _observation()
    repository = FakeRepository()
    selector = RecordingSelector()
    enricher = RecordingEnricher(
        EnrichmentBatch((duplicate, duplicate), (), ())
    )
    monkeypatch.setattr(
        service_module,
        "score_sectors",
        lambda *_args, **_kwargs: pytest.fail("duplicate output must not be ranked"),
    )

    with pytest.raises(ValueError, match="duplicate enrichment.*sector_id"):
        _service(
            repository=repository,
            candidate_selector=selector,
            enricher=enricher,
        ).run()

    assert repository.enriched_writes == []
    assert repository.snapshot is None


def test_enrichment_output_rejects_unknown_sector_before_rank_or_persistence(
    monkeypatch,
) -> None:
    selected = _definition()
    candidate = EnrichmentCandidate(
        selected,
        _observation(),
        ("configured_seed",),
    )

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(candidate,),
        enrichment=EnrichmentBatch(
            (_observation("industry:unknown", name="Unknown"),),
            (),
            (),
        ),
        match="enrichment observation sector IDs must exactly match selected candidates",
    )


def test_enrichment_output_rejects_missing_selected_sector_before_rank_or_persistence(
    monkeypatch,
) -> None:
    first = EnrichmentCandidate(
        _definition(),
        _observation(),
        ("configured_seed",),
    )
    second_definition = _definition("industry:second", name="Second")
    second = EnrichmentCandidate(
        second_definition,
        _observation("industry:second", name="Second"),
        ("current_industry_laggard",),
    )

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(first, second),
        enrichment=EnrichmentBatch((_observation(),), (), ()),
        match="enrichment observation sector IDs must exactly match selected candidates",
    )


@pytest.mark.parametrize(
    ("updates", "field"),
    [
        ({"market": "hk"}, "market"),
        ({"kind": "concept"}, "kind"),
        ({"name": "Changed"}, "name"),
    ],
)
def test_enrichment_output_rejects_candidate_identity_mismatch_before_rank(
    monkeypatch,
    updates: dict[str, str],
    field: str,
) -> None:
    candidate = EnrichmentCandidate(
        _definition(),
        _observation(),
        ("configured_seed",),
    )
    mismatched = _observation().model_copy(update=updates)

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(candidate,),
        enrichment=EnrichmentBatch((mismatched,), (), ()),
        match=f"enrichment observation {field} mismatch",
    )


def test_enrichment_output_rejects_evidence_for_unselected_sector_before_rank(
    monkeypatch,
) -> None:
    candidate = EnrichmentCandidate(
        _definition(),
        _observation(),
        ("configured_seed",),
    )

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(candidate,),
        enrichment=EnrichmentBatch(
            (_observation(),),
            (_evidence("industry:unselected"),),
            (),
        ),
        match="constituent evidence sector_id was not selected",
    )


def test_enrichment_output_rejects_duplicate_evidence_before_rank(
    monkeypatch,
) -> None:
    candidate = EnrichmentCandidate(
        _definition(),
        _observation(),
        ("configured_seed",),
    )
    evidence = _evidence(candidate.sector.sector_id)
    observation = _observation().model_copy(
        update={"raw_reference": {"constituent_set_key": evidence.set_key}}
    )

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(candidate,),
        enrichment=EnrichmentBatch(
            (observation,),
            (evidence, evidence),
            (),
        ),
        match="duplicate constituent evidence",
    )


def test_enrichment_output_rejects_evidence_not_referenced_by_selected_output(
    monkeypatch,
) -> None:
    candidate = EnrichmentCandidate(
        _definition(),
        _observation(),
        ("configured_seed",),
    )

    _assert_enrichment_rejected_before_rank_or_persistence(
        monkeypatch,
        candidates=(candidate,),
        enrichment=EnrichmentBatch(
            (_observation(),),
            (_evidence(candidate.sector.sector_id),),
            (),
        ),
        match="constituent evidence is not referenced by selected output",
    )


def test_enrichment_trace_is_bounded_tagged_and_drops_unknown_secret_fields() -> None:
    trace = tuple(
        {
            "capability": "board_history",
            "result": "ok",
            "secret": f"token-{index}",
        }
        for index in range(1300)
    )
    selector = RecordingSelector()
    enricher = RecordingEnricher(EnrichmentBatch((), (), trace))

    snapshot = _service(
        candidate_selector=selector,
        enricher=enricher,
    ).run(persist=False)

    enrichment_trace = snapshot.provider_trace[1:]
    assert len(snapshot.provider_trace) == 1200
    assert len(enrichment_trace) == 1199
    assert all(item["stage"] == "enrichment" for item in enrichment_trace)
    assert all(item["dataset"] == "board_history" for item in enrichment_trace)
    assert "secret" not in str(enrichment_trace)


def test_total_trace_cap_preserves_a_late_deadline_in_original_order() -> None:
    provider = FakeProvider(
        ProviderBatch(
            observations=[_observation()],
            trace=[
                {
                    "dataset": "industry",
                    "provider": f"discovery-{index}",
                    "result": "failed",
                }
                for index in range(1200)
            ],
        )
    )
    enricher = RecordingEnricher(
        EnrichmentBatch(
            (),
            (),
            (
                {
                    "capability": "candidate_enrichment",
                    "result": "deadline_exceeded",
                    "error": "deadline_exceeded",
                },
            ),
        )
    )

    snapshot = _service(
        provider=provider,
        candidate_selector=RecordingSelector(),
        enricher=enricher,
    ).run(persist=False)

    assert len(snapshot.provider_trace) == 1200
    assert snapshot.provider_trace[0]["provider"] == "discovery-0"
    assert snapshot.provider_trace[-1] == {
        "stage": "enrichment",
        "dataset": "candidate_enrichment",
        "capability": "candidate_enrichment",
        "result": "deadline_exceeded",
        "error": "deadline_exceeded",
    }


@pytest.mark.parametrize(
    "mode",
    ["discovery_only", "no_enricher", "enriched"],
)
def test_duplicate_discovery_observations_abort_before_selection_rank_or_persist(
    monkeypatch,
    mode: str,
) -> None:
    duplicate = _observation()
    provider = FakeProvider(
        ProviderBatch(
            observations=[duplicate, duplicate],
            trace=[],
        )
    )
    repository = FakeRepository()
    selector = RecordingSelector()
    enricher = RecordingEnricher(EnrichmentBatch((), (), ()))
    monkeypatch.setattr(
        service_module,
        "score_sectors",
        lambda *_args, **_kwargs: pytest.fail(
            "duplicate discovery must not be ranked"
        ),
    )
    service = _service(
        provider=provider,
        repository=repository,
        candidate_selector=selector if mode == "enriched" else None,
        enricher=enricher if mode == "enriched" else None,
    )

    with pytest.raises(ValueError, match="duplicate discovery observation sector_id"):
        service.run(discovery_only=mode == "discovery_only")

    assert selector.calls == []
    assert enricher.calls == []
    assert repository.latest_calls == []
    assert repository.snapshot is None


def test_duplicate_final_observations_abort_before_rank_or_persist(
    monkeypatch,
) -> None:
    observation = _observation()
    candidate = EnrichmentCandidate(
        _definition(),
        observation,
        ("configured_seed",),
    )
    repository = FakeRepository()
    monkeypatch.setattr(
        service_module,
        "_merge_observations",
        lambda *_args: [observation, observation],
    )
    monkeypatch.setattr(
        service_module,
        "score_sectors",
        lambda *_args, **_kwargs: pytest.fail(
            "duplicate final observations must not be ranked"
        ),
    )

    with pytest.raises(ValueError, match="duplicate final observation sector_id"):
        _service(
            repository=repository,
            candidate_selector=RecordingSelector((candidate,)),
            enricher=RecordingEnricher(
                EnrichmentBatch((observation,), (), ())
            ),
        ).run(previous_snapshot=_previous_snapshot(NOW - timedelta(hours=1)))

    assert repository.enriched_writes == []
    assert repository.snapshot is None


def test_explicit_equivalent_instant_reads_clock_once_and_determines_key() -> None:
    as_of = datetime(
        2026,
        7,
        21,
        14,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    snapshot = _service(clock=clock).run(
        as_of=as_of,
        persist=False,
    )

    assert calls == 1
    assert snapshot.run_key == "cn:20260721T060000Z:manual"
    assert snapshot.as_of == as_of.astimezone(timezone.utc)


def test_replay_trigger_rejects_before_live_or_repository_dependencies() -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)

    with pytest.raises(
        ValueError,
        match="MarketRadarReplayEngine.replay_persisted_run",
    ):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=repository,
        ).run(trigger="replay")

    assert events == []
    assert repository.latest_calls == []


@pytest.mark.parametrize(
    "as_of",
    [NOW - timedelta(days=1), NOW + timedelta(days=1)],
    ids=["historical", "future"],
)
def test_manual_run_rejects_different_market_date_before_dependencies(
    as_of: datetime,
) -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)

    with pytest.raises(ValueError, match="exact same instant as the clock"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=repository,
        ).run(as_of=as_of)

    assert events == []
    assert repository.latest_calls == []


@pytest.mark.parametrize(
    "as_of",
    [NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)],
    ids=["same_day_retro", "same_day_future"],
)
def test_manual_run_rejects_caller_selected_same_day_instant_before_dependencies(
    as_of: datetime,
) -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)

    with pytest.raises(ValueError, match="exact same instant as the clock"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=repository,
        ).run(as_of=as_of)

    assert events == []
    assert repository.latest_calls == []


def test_run_rejects_naive_clock_before_dependencies() -> None:
    events: list[str] = []

    with pytest.raises(ValueError, match="clock must return a timezone-aware"):
        _service(
            universe=FakeUniverse(events=events),
            provider=FakeProvider(events=events),
            repository=FakeRepository(events=events),
            clock=lambda: datetime(2026, 7, 21, 6, 0),
        ).run()

    assert events == []


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
        clock=lambda: utc_as_of,
    ).run(as_of=utc_as_of, persist=False)
    second = _service(
        universe=second_universe,
        provider=second_provider,
        clock=lambda: cn_as_of,
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


def test_repeated_service_sync_preserves_etf_history_and_fetches_only_active_etfs(
    isolated_db,
    tmp_path,
) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(
        """
version: 1
sectors:
  - kind: industry
    name: Semiconductor
    effective_from: 2020-01-01
    etfs:
      - code: "512401"
        name: Past ETF
        effective_from: 2020-01-01
        effective_to: 2021-12-31
      - code: "512402"
        name: Current ETF
        effective_from: 2022-01-01
        effective_to: 2026-12-31
      - code: "512403"
        name: Future ETF
        effective_from: 2027-01-01
""".strip(),
        encoding="utf-8",
    )

    class RecordingProvider:
        def __init__(self) -> None:
            self.active_etf_codes: list[list[str]] = []

        def fetch(self, market, as_of, universe):
            self.active_etf_codes.append(
                [etf.code for sector in universe for etf in sector.etfs]
            )
            return ProviderBatch(observations=[], trace=[])

    provider = RecordingProvider()
    repository = MarketRadarRepository(isolated_db)
    first_as_of = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    second_as_of = datetime(2027, 7, 21, 6, tzinfo=timezone.utc)
    current = [first_as_of]
    service = MarketRadarService(
        universe_loader=UniverseLoader(path),
        provider=provider,
        repository=repository,
        ranking_config=RankingConfig(),
        clock=lambda: current[0],
    )

    service.run(as_of=first_as_of)
    current[0] = second_as_of
    service.run(as_of=second_as_of)

    assert provider.active_etf_codes == [["512402"], ["512403"]]
    assert [
        [etf.code for etf in repository.list_universe(as_of)[0].etfs]
        for as_of in [date(2021, 7, 21), date(2026, 7, 21), date(2027, 7, 21)]
    ] == [["512401"], ["512402"], ["512403"]]


def test_current_discovery_is_bounded_before_future_configured_interval(
    isolated_db,
    tmp_path,
) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(
        """
version: 1
sectors:
  - kind: industry
    name: Configured Semiconductor
    aliases: [Historical Chips]
    benchmark_code: "000905"
    effective_from: 2020-01-01
    effective_to: 2025-12-31
    etfs:
      - code: "512401"
        name: Historical Semiconductor ETF
        effective_from: 2020-01-01
        effective_to: 2025-12-31
  - kind: industry
    name: Configured Semiconductor
    aliases: [Configured Chips]
    benchmark_code: "000300"
    effective_from: 2027-01-01
    etfs:
      - code: "512403"
        name: Future Semiconductor ETF
        effective_from: 2027-01-01
""".strip(),
        encoding="utf-8",
    )
    discovered = SectorDefinition(
        sector_id="industry:configured-semiconductor",
        kind="industry",
        name="configured semiconductor",
        aliases=["Discovered Chips"],
        effective_from=date(2026, 7, 21),
    )

    class DiscoveryProvider:
        def __init__(self) -> None:
            self.universes: list[list[SectorDefinition]] = []

        def fetch(self, market, as_of, universe):
            self.universes.append(universe)
            return ProviderBatch(
                observations=[],
                trace=[],
                discovered_sectors=[discovered],
            )

    provider = DiscoveryProvider()
    repository = MarketRadarRepository(isolated_db)
    as_of = datetime(2026, 7, 21, 6, tzinfo=timezone.utc)
    service = MarketRadarService(
        universe_loader=UniverseLoader(path),
        provider=provider,
        repository=repository,
        ranking_config=RankingConfig(),
        clock=lambda: as_of,
    )

    service.run(as_of=as_of)

    past = repository.list_universe(date(2025, 12, 31))
    current = repository.list_universe(date(2026, 12, 31))
    future = repository.list_universe(date(2027, 1, 1))
    assert provider.universes == [[]]
    assert len(past) == 1
    assert past[0].name == "Configured Semiconductor"
    assert [etf.code for etf in past[0].etfs] == ["512401"]
    assert len(current) == 1
    assert current[0].name == "configured semiconductor"
    assert len(future) == 1
    assert future[0].name == "Configured Semiconductor"
    assert [etf.code for etf in future[0].etfs] == ["512403"]
    assert current[0].effective_to == date(2026, 12, 31)
    assert discovered.effective_to is None


def test_run_without_persistence_does_not_write() -> None:
    events: list[str] = []
    repository = FakeRepository(events=events)

    snapshot = _service(repository=repository).run(persist=False)

    assert snapshot.sectors
    assert repository.universe is None
    assert repository.snapshot is None
    assert events == []


def test_run_without_persistence_does_not_require_repository() -> None:
    service = MarketRadarService(
        universe_loader=FakeUniverse(),
        provider=FakeProvider(),
        repository=None,
        ranking_config=RankingConfig(),
        clock=lambda: NOW,
    )

    snapshot = service.run(persist=False)

    assert snapshot.sectors


def test_persisted_run_requires_repository_before_loading_data() -> None:
    events: list[str] = []
    service = MarketRadarService(
        universe_loader=FakeUniverse(events=events),
        provider=FakeProvider(events=events),
        repository=None,
        ranking_config=RankingConfig(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="repository is required"):
        service.run(persist=True)

    assert events == []


@pytest.mark.parametrize("market", ["hk", "us"])
def test_run_rejects_unsupported_market_before_using_dependencies(market: str) -> None:
    events: list[str] = []

    with pytest.raises(
        ValueError,
        match="^Market Radar supports market=cn only$",
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


def test_persisted_discovery_and_enrichment_trace_is_allowlisted_bounded_and_redacted(
    isolated_db,
) -> None:
    secret = "Authorization: Bearer token-secret Cookie=password-secret"
    provider = FakeProvider(
        ProviderBatch(
            observations=[_observation()],
            trace=[
                {
                    "dataset": "industry",
                    "provider": "D" * 500,
                    "result": "failed",
                    "duration_ms": 10**30,
                    "source": secret,
                    "error": secret,
                    "headers": {"Authorization": secret},
                    "token": "token-secret",
                    "nested": {"password": "password-secret"},
                }
            ],
        )
    )
    enricher = RecordingEnricher(
        EnrichmentBatch(
            (),
            (),
            (
                {
                    "sector_id": "industry:semiconductor",
                    "capability": "board_history",
                    "result": "failed",
                    "duration_ms": 10**30,
                    "source": secret,
                    "error": secret,
                    "cookies": {"session": "token-secret"},
                },
            ),
        )
    )
    repository = MarketRadarRepository(isolated_db)

    stored = _service(
        provider=provider,
        repository=repository,
        candidate_selector=RecordingSelector(),
        enricher=enricher,
    ).run()
    readback = repository.get_run_by_key(stored.run_key)

    assert readback is not None
    assert readback.provider_trace == stored.provider_trace
    assert len(readback.provider_trace) == 2
    assert readback.provider_trace[0] == {
        "dataset": "industry",
        "provider": "D" * 128,
        "result": "failed",
        "duration_ms": 86_400_000,
        "source": "[REDACTED]",
        "error": "[REDACTED]",
    }
    assert readback.provider_trace[1] == {
        "stage": "enrichment",
        "dataset": "board_history",
        "sector_id": "industry:semiconductor",
        "capability": "board_history",
        "result": "failed",
        "duration_ms": 86_400_000,
        "source": "[REDACTED]",
        "error": "[REDACTED]",
    }
    persisted = str(readback.provider_trace).casefold()
    assert "token-secret" not in persisted
    assert "password-secret" not in persisted
    assert "authorization" not in persisted
    assert "headers" not in persisted
    assert "cookies" not in persisted


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
            clock=lambda: later,
        ).run(as_of=later)

    assert repository.get_latest_run("cn") == original
    assert repository.list_universe(later.date()) == [_definition()]
