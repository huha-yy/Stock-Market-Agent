from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from src.market_radar.models import (
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
)


@dataclass(frozen=True)
class EnrichmentCandidate:
    sector: SectorDefinition
    observation: SectorObservation | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _CanonicalCandidate:
    sector: SectorDefinition
    observation: SectorObservation | None


class CandidateSelector:
    def select(
        self,
        universe: Sequence[SectorDefinition],
        observations: Sequence[SectorObservation],
        previous_snapshot: RadarRunSnapshot | None,
        limit: int,
    ) -> tuple[EnrichmentCandidate, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be at least 1")

        by_id = self._canonical_candidates(
            universe, observations, previous_snapshot
        )
        reasons = {sector_id: [] for sector_id in by_id}
        seed_ids = sorted({sector.sector_id for sector in universe})
        for sector_id in seed_ids:
            reasons[sector_id].append("configured_seed")

        previous_ids = self._previous_ids(previous_snapshot)
        for reason, sector_ids in previous_ids:
            for sector_id in sector_ids:
                reasons[sector_id].append(reason)

        ordered_ids = self._seed_and_previous_order(seed_ids, previous_ids)
        seen_ids = set(ordered_ids)
        current_queues = self._current_extreme_queues(by_id)
        self._collect_current_reasons(current_queues, reasons)
        self._consume_current_queues(
            current_queues,
            ordered_ids,
            seen_ids,
            limit,
        )

        return tuple(
            EnrichmentCandidate(
                sector=by_id[sector_id].sector,
                observation=by_id[sector_id].observation,
                reasons=tuple(reasons[sector_id]),
            )
            for sector_id in ordered_ids[:limit]
        )

    @staticmethod
    def _canonical_candidates(
        universe: Sequence[SectorDefinition],
        observations: Sequence[SectorObservation],
        previous_snapshot: RadarRunSnapshot | None,
    ) -> dict[str, _CanonicalCandidate]:
        current_by_id: dict[str, SectorObservation] = {}
        for observation in observations:
            current_by_id.setdefault(observation.sector_id, observation)

        by_id: dict[str, _CanonicalCandidate] = {}
        for sector in sorted(universe, key=lambda item: item.sector_id):
            by_id.setdefault(
                sector.sector_id,
                _CanonicalCandidate(
                    sector=sector,
                    observation=current_by_id.get(sector.sector_id),
                ),
            )

        for sector_id, observation in sorted(current_by_id.items()):
            by_id.setdefault(
                sector_id,
                _CanonicalCandidate(
                    sector=CandidateSelector._definition_from_observation(observation),
                    observation=observation,
                ),
            )

        if previous_snapshot is not None:
            for score in previous_snapshot.sectors:
                by_id.setdefault(
                    score.sector_id,
                    _CanonicalCandidate(
                        sector=CandidateSelector._definition_from_score(
                            score, previous_snapshot.market
                        ),
                        observation=current_by_id.get(score.sector_id),
                    ),
                )

        return by_id

    @staticmethod
    def _definition_from_observation(
        observation: SectorObservation,
    ) -> SectorDefinition:
        return SectorDefinition(
            sector_id=observation.sector_id,
            market=observation.market,
            kind=observation.kind,
            name=observation.name,
            effective_from=observation.observed_at.date(),
        )

    @staticmethod
    def _definition_from_score(
        score: SectorScore,
        market: str,
    ) -> SectorDefinition:
        return SectorDefinition(
            sector_id=score.sector_id,
            market=market,  # type: ignore[arg-type]
            kind=score.kind,
            name=score.name,
            effective_from=score.observed_at.date(),
        )

    @staticmethod
    def _previous_ids(
        previous_snapshot: RadarRunSnapshot | None,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if previous_snapshot is None:
            return ()

        ranked = tuple(enumerate(previous_snapshot.sectors))
        return (
            (
                "previous_leading",
                tuple(
                    score.sector_id
                    for _, score in sorted(
                        (
                            item
                            for item in ranked
                            if item[1].state == "leading"
                        ),
                        key=lambda item: (item[0], item[1].sector_id),
                    )
                ),
            ),
            (
                "previous_improving",
                tuple(
                    score.sector_id
                    for _, score in sorted(
                        (
                            item
                            for item in ranked
                            if item[1].state == "improving"
                        ),
                        key=lambda item: (item[0], item[1].sector_id),
                    )
                ),
            ),
        )

    @staticmethod
    def _seed_and_previous_order(
        seed_ids: Sequence[str],
        previous_ids: Sequence[tuple[str, Sequence[str]]],
    ) -> list[str]:
        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        for sector_id in (*seed_ids, *(item for _, ids in previous_ids for item in ids)):
            if sector_id not in seen_ids:
                ordered_ids.append(sector_id)
                seen_ids.add(sector_id)
        return ordered_ids

    @staticmethod
    def _current_extreme_queues(
        by_id: dict[str, _CanonicalCandidate],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        observations = tuple(
            (sector_id, candidate.observation)
            for sector_id, candidate in by_id.items()
            if candidate.observation is not None
        )

        def queues(kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
            rows = [
                (sector_id, observation.return_1d_pct)
                for sector_id, observation in observations
                if observation.kind == kind
            ]
            if len(rows) == 1:
                sector_id, value = rows[0]
                if value is not None and isfinite(value) and value >= 0:
                    return (sector_id,), ()
                return (), (sector_id,)

            finite = sorted(
                (
                    (sector_id, value)
                    for sector_id, value in rows
                    if value is not None and isfinite(value)
                ),
                key=lambda item: CandidateSelector._return_sort_key(
                    item[0], item[1], True
                ),
            )
            leader_count = (len(finite) + 1) // 2
            leaders = tuple(sector_id for sector_id, _ in finite[:leader_count])
            laggards = tuple(
                sector_id
                for sector_id, _ in sorted(
                    finite[leader_count:],
                    key=lambda item: CandidateSelector._return_sort_key(
                        item[0], item[1], False
                    ),
                )
            )
            missing = tuple(
                sorted(
                    sector_id
                    for sector_id, value in rows
                    if value is None or not isfinite(value)
                )
            )
            return leaders, (*laggards, *missing)

        industry_leaders, industry_laggards = queues("industry")
        concept_leaders, concept_laggards = queues("concept")

        return (
            ("current_industry_leader", industry_leaders),
            ("current_industry_laggard", industry_laggards),
            ("current_concept_leader", concept_leaders),
            ("current_concept_laggard", concept_laggards),
        )

    @staticmethod
    def _return_sort_key(
        sector_id: str, value: float | None, reverse: bool
    ) -> tuple[bool, float, str]:
        if value is None or not isfinite(value):
            return (True, 0.0, sector_id)
        return (False, -value if reverse else value, sector_id)

    @staticmethod
    def _collect_current_reasons(
        queues: Sequence[tuple[str, Sequence[str]]],
        reasons: dict[str, list[str]],
    ) -> None:
        for reason, queue in queues:
            for sector_id in queue:
                if reason not in reasons[sector_id]:
                    reasons[sector_id].append(reason)

    @staticmethod
    def _consume_current_queues(
        queues: Sequence[tuple[str, Sequence[str]]],
        ordered_ids: list[str],
        seen_ids: set[str],
        limit: int,
    ) -> None:
        positions = [0] * len(queues)
        while len(ordered_ids) < limit:
            made_progress = False
            for index, (_, queue) in enumerate(queues):
                while positions[index] < len(queue):
                    sector_id = queue[positions[index]]
                    positions[index] += 1
                    if sector_id in seen_ids:
                        continue
                    ordered_ids.append(sector_id)
                    seen_ids.add(sector_id)
                    made_progress = True
                    break
                if len(ordered_ids) >= limit:
                    return
            if not made_progress:
                return
