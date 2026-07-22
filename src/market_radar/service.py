from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from data_provider.base import sanitize_persisted_text
from src.market_radar.candidates import CandidateSelector, EnrichmentCandidate
from src.market_radar.capabilities import MarketRadarEnrichmentConfig
from src.market_radar.enrichment import EnrichmentBatch
from src.market_radar.models import (
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    aggregate_run_quality,
)
from src.market_radar.providers import MarketRadarProvider
from src.market_radar.ranking import RankingConfig, score_sectors
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.universe import UniverseLoader


_CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
_PROVIDER_TRACE_LIMIT = 1200
_TRACE_TEXT_LIMIT = 128
_TRACE_ERROR_LIMIT = 256
_TRACE_NUMBER_LIMIT = 86_400_000
_PROVIDER_TRACE_FIELDS = (
    "dataset",
    "stage",
    "sector_id",
    "sector",
    "capability",
    "provider",
    "result",
    "source",
    "code",
    "selected",
    "duration_ms",
    "duration",
    "error",
)


def _merge_discovered_sectors(
    configured_history: list[SectorDefinition],
    discovered_sectors: list[SectorDefinition],
) -> list[SectorDefinition]:
    configured_by_id: dict[str, list[SectorDefinition]] = {}
    for configured in configured_history:
        configured_by_id.setdefault(configured.sector_id, []).append(configured)

    merged: list[SectorDefinition] = []
    for discovered in discovered_sectors:
        intervals = configured_by_id.get(discovered.sector_id, [])
        if any(
            configured.effective_from <= discovered.effective_from
            and (
                configured.effective_to is None
                or configured.effective_to >= discovered.effective_from
            )
            for configured in intervals
        ):
            continue

        future_starts = [
            configured.effective_from
            for configured in intervals
            if configured.effective_from > discovered.effective_from
        ]
        if not future_starts:
            merged.append(discovered)
            continue

        boundary = min(future_starts) - timedelta(days=1)
        if (
            discovered.effective_to is not None
            and discovered.effective_to <= boundary
        ):
            merged.append(discovered)
            continue

        payload = discovered.model_dump(mode="python")
        payload["effective_to"] = boundary
        merged.append(SectorDefinition.model_validate(payload))
    return merged


def _merge_observations(
    discovered: Sequence[SectorObservation],
    enriched: Sequence[SectorObservation],
) -> list[SectorObservation]:
    enriched_by_id: dict[str, SectorObservation] = {}
    for observation in enriched:
        if observation.sector_id in enriched_by_id:
            raise ValueError(
                "duplicate enrichment output sector_id: "
                f"{observation.sector_id}"
            )
        enriched_by_id[observation.sector_id] = observation

    merged = [
        enriched_by_id.get(observation.sector_id, observation)
        for observation in discovered
    ]
    discovered_ids = {observation.sector_id for observation in discovered}
    merged.extend(
        observation
        for observation in enriched
        if observation.sector_id not in discovered_ids
    )
    return merged


def _validate_unique_observations(
    observations: Sequence[SectorObservation],
    stage: str,
) -> None:
    seen: set[str] = set()
    for observation in observations:
        if observation.sector_id in seen:
            raise ValueError(
                f"duplicate {stage} observation sector_id: "
                f"{observation.sector_id}"
            )
        seen.add(observation.sector_id)


def _clean_trace_scalar(key: str, value: Any) -> str | int | float | bool | None:
    if isinstance(value, str):
        limit = _TRACE_ERROR_LIMIT if key == "error" else _TRACE_TEXT_LIMIT
        return sanitize_persisted_text(value, limit)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if key in {"duration", "duration_ms"} and value < 0:
            return None
        return max(-_TRACE_NUMBER_LIMIT, min(value, _TRACE_NUMBER_LIMIT))
    if isinstance(value, float) and isfinite(value):
        if key in {"duration", "duration_ms"} and value < 0:
            return None
        return max(
            -float(_TRACE_NUMBER_LIMIT),
            min(value, float(_TRACE_NUMBER_LIMIT)),
        )
    return None


def _sanitize_trace_entry(
    entry: Mapping[str, Any],
    *,
    enrichment: bool,
) -> dict[str, Any]:
    values = dict(entry)
    if enrichment:
        values["stage"] = "enrichment"
        values["dataset"] = entry.get("capability", "enrichment")
    item: dict[str, Any] = {}
    for key in _PROVIDER_TRACE_FIELDS:
        if key not in values:
            continue
        value = _clean_trace_scalar(key, values[key])
        if value is not None:
            item[key] = value
    return item


def _sanitize_provider_trace(
    discovery: Sequence[Mapping[str, Any]],
    enrichment: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    selected: list[tuple[int, dict[str, Any], bool]] = []
    index = 0
    for entries, is_enrichment in (
        (discovery, False),
        (enrichment, True),
    ):
        for entry in entries:
            current_index = index
            index += 1
            if not isinstance(entry, Mapping):
                continue
            item = _sanitize_trace_entry(entry, enrichment=is_enrichment)
            if not item:
                continue
            is_deadline = (
                item.get("result") == "deadline_exceeded"
                or item.get("error") == "deadline_exceeded"
            )
            if len(selected) < _PROVIDER_TRACE_LIMIT:
                selected.append((current_index, item, is_deadline))
                continue
            if not is_deadline:
                continue
            replacement = next(
                (
                    position
                    for position in range(len(selected) - 1, -1, -1)
                    if not selected[position][2]
                ),
                None,
            )
            if replacement is not None:
                selected[replacement] = (current_index, item, True)
    return tuple(item for _, item, _ in sorted(selected, key=lambda value: value[0]))


def _validate_previous_snapshot(
    previous: RadarRunSnapshot,
    effective_as_of: datetime,
) -> None:
    if previous.market != "cn":
        raise ValueError("previous_snapshot must use market=cn")
    if previous.as_of.tzinfo is None or previous.as_of.utcoffset() is None:
        raise ValueError("previous_snapshot.as_of must be timezone-aware")
    if previous.as_of.astimezone(timezone.utc) >= effective_as_of:
        raise ValueError(
            "previous_snapshot.as_of must be strictly earlier than as_of"
        )


def _validate_enrichment_output(
    candidates: Sequence[EnrichmentCandidate],
    enrichment: EnrichmentBatch,
) -> None:
    selected_by_id: dict[str, EnrichmentCandidate] = {}
    selected_ids: list[str] = []
    for candidate in candidates:
        sector_id = candidate.sector.sector_id
        if sector_id in selected_by_id:
            raise ValueError(f"duplicate selected candidate sector_id: {sector_id}")
        selected_by_id[sector_id] = candidate
        selected_ids.append(sector_id)

    output_ids = [item.sector_id for item in enrichment.observations]
    if len(output_ids) != len(set(output_ids)):
        raise ValueError("duplicate enrichment output sector_id")
    if output_ids != selected_ids:
        raise ValueError(
            "enrichment observation sector IDs must exactly match selected candidates"
        )

    output_by_id = {
        observation.sector_id: observation
        for observation in enrichment.observations
    }
    for sector_id in selected_ids:
        sector = selected_by_id[sector_id].sector
        observation = output_by_id[sector_id]
        for field_name in ("market", "sector_id", "kind", "name"):
            if getattr(observation, field_name) != getattr(sector, field_name):
                raise ValueError(
                    f"enrichment observation {field_name} mismatch for {sector_id}"
                )

    evidence_sector_ids: set[str] = set()
    evidence_keys: set[str] = set()
    for evidence in enrichment.constituent_evidence:
        if evidence.sector_id not in selected_by_id:
            raise ValueError(
                "constituent evidence sector_id was not selected: "
                f"{evidence.sector_id}"
            )
        if (
            evidence.sector_id in evidence_sector_ids
            or evidence.set_key in evidence_keys
        ):
            raise ValueError("duplicate constituent evidence sector_id or set_key")
        observation = output_by_id[evidence.sector_id]
        if observation.raw_reference.get("constituent_set_key") != evidence.set_key:
            raise ValueError(
                "constituent evidence is not referenced by selected output: "
                f"{evidence.sector_id}"
            )
        evidence_sector_ids.add(evidence.sector_id)
        evidence_keys.add(evidence.set_key)


class MarketRadarService:
    def __init__(
        self,
        *,
        universe_loader: UniverseLoader,
        provider: MarketRadarProvider,
        repository: MarketRadarRepository | None,
        ranking_config: RankingConfig,
        enricher: Any | None = None,
        candidate_selector: CandidateSelector | None = None,
        enrichment_config: MarketRadarEnrichmentConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.universe_loader = universe_loader
        self.provider = provider
        self.repository = repository
        self.ranking_config = ranking_config
        self.enricher = enricher
        self.candidate_selector = (
            candidate_selector
            if candidate_selector is not None or enricher is None
            else CandidateSelector()
        )
        inherited_config = getattr(enricher, "config", None)
        self.enrichment_config = (
            enrichment_config
            or (
                inherited_config
                if isinstance(inherited_config, MarketRadarEnrichmentConfig)
                else None
            )
            or MarketRadarEnrichmentConfig()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        market: str = "cn",
        as_of: datetime | None = None,
        trigger: Literal["manual", "replay"] = "manual",
        persist: bool = True,
        discovery_only: bool = False,
        previous_snapshot: RadarRunSnapshot | None = None,
    ) -> RadarRunSnapshot:
        if market != "cn":
            raise ValueError("Market Radar supports market=cn only")
        if trigger == "replay":
            raise ValueError(
                "MarketRadarService.run does not perform live replay; use "
                "MarketRadarReplayEngine.replay_persisted_run"
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        effective_as_of = now.astimezone(timezone.utc)
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("as_of must be timezone-aware")
            requested_as_of = as_of.astimezone(timezone.utc)
            if requested_as_of != effective_as_of:
                raise ValueError("as_of must be the exact same instant as the clock")
        repository = self.repository
        if persist and repository is None:
            raise ValueError("repository is required when persist=True")
        if previous_snapshot is not None:
            _validate_previous_snapshot(previous_snapshot, effective_as_of)

        market_date = effective_as_of.astimezone(_CN_MARKET_TIMEZONE).date()
        universe, configured_history = self.universe_loader.load_with_history(
            market_date
        )
        batch = self.provider.fetch(market, effective_as_of, universe)
        _validate_unique_observations(batch.observations, "discovery")
        enrichment = None
        observations = list(batch.observations)
        enrichment_enabled = not discovery_only and self.enricher is not None
        if enrichment_enabled:
            previous = previous_snapshot
            if persist and previous is None:
                previous = repository.get_latest_run(
                    market="cn",
                    before=effective_as_of,
                )
                if previous is not None:
                    _validate_previous_snapshot(previous, effective_as_of)
            selector = self.candidate_selector
            if selector is None:
                raise RuntimeError(
                    "candidate_selector is required when enrichment is enabled"
                )
            candidates = selector.select(
                universe,
                batch.observations,
                previous,
                self.enrichment_config.candidate_limit,
            )
            enrichment = self.enricher.enrich(candidates, effective_as_of)
            _validate_enrichment_output(candidates, enrichment)
            observations = _merge_observations(
                batch.observations,
                enrichment.observations,
            )

        _validate_unique_observations(observations, "final")
        sectors = score_sectors(observations, self.ranking_config)
        provider_trace = _sanitize_provider_trace(
            batch.trace,
            enrichment.trace if enrichment is not None else (),
        )
        snapshot = RadarRunSnapshot(
            run_key=(
                f"{market}:"
                f"{effective_as_of.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}:"
                f"{trigger}"
            ),
            market="cn",
            trigger=trigger,
            as_of=effective_as_of,
            quality=aggregate_run_quality(
                item.quality for item in observations
            ),
            scoring_version=self.ranking_config.scoring_version,
            sectors=sectors,
            provider_trace=provider_trace,
        )
        if persist:
            combined_universe = [
                *configured_history,
                *_merge_discovered_sectors(
                    configured_history,
                    batch.discovered_sectors,
                ),
            ]
            sorted_universe = sorted(
                combined_universe,
                key=lambda item: (
                    item.kind,
                    item.sector_id,
                    item.effective_from,
                ),
            )
            if enrichment is None:
                run_id = repository.save_run_with_universe(
                    sorted_universe,
                    snapshot,
                )
            else:
                run_id = repository.save_enriched_run(
                    sorted_universe,
                    enrichment.constituent_evidence,
                    snapshot,
                )
            stored_snapshot = repository.get_run(run_id)
            if stored_snapshot is None:
                raise RuntimeError(f"Persisted Market Radar run {run_id} was not found")
            return stored_snapshot
        return snapshot
