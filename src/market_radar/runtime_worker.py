from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from src.market_radar.factory import build_market_radar_service
from src.market_radar.repository import AttemptReservation, MarketRadarRepository
from src.market_radar.session_policy import (
    MarketRadarSessionPolicy,
    RadarRunDecision,
)


logger = logging.getLogger(__name__)

_RADAR_RUNTIME_LOCK = threading.Lock()
_FAILURE_CATEGORIES = {
    "calendar": "calendar_error",
    "provider": "provider_error",
    "persistence": "persistence_error",
    "runtime": "runtime_error",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?P<quote>[\"']?)"
    r"(?P<name>(?:[A-Za-z0-9]+[_-])*(?:token|key|authorization|cookie|password))"
    r"\b(?P=quote)\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|(?:Bearer\s+)?[^\s,;]+)",
    re.IGNORECASE,
)

FailureStage = Literal["calendar", "provider", "persistence", "runtime"]


def sanitize_runtime_failure(
    exc: Exception,
    *,
    stage: FailureStage = "runtime",
    limit: int = 512,
) -> tuple[str, str]:
    """Return a stable category and a bounded, redacted diagnostic summary."""
    category = _FAILURE_CATEGORIES[stage]
    if isinstance(exc, SQLAlchemyError) or exc.__class__.__module__.startswith(
        ("sqlalchemy", "sqlite3")
    ):
        category = "persistence_error"
    summary = f"{type(exc).__name__}: {exc}"
    summary = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('name')}=[REDACTED]",
        summary,
    )
    return category, summary[:limit]


class MarketRadarRuntimeWorker:
    def __init__(
        self,
        *,
        policy: MarketRadarSessionPolicy | None = None,
        repository: MarketRadarRepository | None = None,
        service_factory: Any = None,
        clock: Any = None,
    ) -> None:
        self.policy = policy or MarketRadarSessionPolicy()
        self.repository = repository or MarketRadarRepository()
        self.service_factory = service_factory or build_market_radar_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._status: dict[str, Any] = {
            "running": False,
            "last_decision": None,
            "last_success_at": None,
            "last_error": None,
        }

    def run_once(self) -> dict[str, Any]:
        try:
            decision = self.policy.decide(self.clock())
        except Exception as exc:
            return self._failure_result(
                exc,
                stage="calendar",
                attempt_key=None,
            )

        self._status["last_decision"] = decision.model_dump(mode="json")
        if decision.kind == "not_due":
            return {"status": "skipped", "reason": decision.reason}
        if decision.kind == "calendar_unavailable":
            return self._record_calendar_skip(decision)
        if not _RADAR_RUNTIME_LOCK.acquire(blocking=False):
            return {
                "status": "skipped",
                "reason": "radar_already_running",
                "attempt_key": decision.attempt_key,
            }

        reservation: AttemptReservation | None = None
        failure_stage: FailureStage = "persistence"
        can_terminalize_failure = False
        terminal_write_started = False
        try:
            reservation = self.repository.reserve_scheduled_attempt(
                decision,
                lease_seconds=900,
            )
            if not reservation.acquired:
                return {
                    "status": reservation.status,
                    "reason": "duplicate_slot",
                    "attempt_key": reservation.attempt_key,
                    "run_id": reservation.run_id,
                }

            can_terminalize_failure = True
            self._status["running"] = True
            schedule_kind = "eod" if decision.kind == "eod_due" else "intraday"
            failure_stage = "provider"
            service = self.service_factory(
                persist=True,
                repository=self.repository,
            )
            snapshot = service.run(
                market="cn",
                trigger="schedule",
                schedule_kind=schedule_kind,
                persist=True,
            )
            failure_stage = "persistence"
            run_id = self.repository.get_run_id_by_key(snapshot.run_key)
            can_terminalize_failure = False
            terminal_write_started = True
            self.repository.finish_scheduled_attempt(
                reservation.attempt_key,
                status="succeeded",
                run_id=run_id,
            )
            self._status.update(
                last_success_at=self.clock().isoformat(),
                last_error=None,
            )
            return {
                "status": "succeeded",
                "attempt_key": reservation.attempt_key,
                "run_id": run_id,
            }
        except Exception as exc:
            return self._failure_result(
                exc,
                stage=failure_stage,
                attempt_key=decision.attempt_key,
                reservation=(reservation if can_terminalize_failure else None),
                log_persistence_failure=terminal_write_started,
            )
        finally:
            self._status["running"] = False
            _RADAR_RUNTIME_LOCK.release()

    def _record_calendar_skip(self, decision: RadarRunDecision) -> dict[str, Any]:
        try:
            reservation = self.repository.reserve_scheduled_attempt(
                decision,
                lease_seconds=900,
            )
            if reservation.acquired:
                self.repository.finish_scheduled_attempt(
                    reservation.attempt_key,
                    status="skipped",
                    reason_code="calendar_unavailable",
                )
        except Exception as exc:
            return self._failure_result(
                exc,
                stage="persistence",
                attempt_key=decision.attempt_key,
            )
        return {
            "status": "skipped",
            "reason": "calendar_unavailable",
            "attempt_key": decision.attempt_key,
        }

    def _failure_result(
        self,
        exc: Exception,
        *,
        stage: FailureStage,
        attempt_key: str | None,
        reservation: AttemptReservation | None = None,
        log_persistence_failure: bool = False,
    ) -> dict[str, Any]:
        category, summary = sanitize_runtime_failure(exc, stage=stage, limit=512)
        if log_persistence_failure:
            self._log_attempt_persistence_failure(exc)
        if reservation is not None and reservation.acquired:
            try:
                self.repository.finish_scheduled_attempt(
                    reservation.attempt_key,
                    status="failed",
                    failure_category=category,
                    failure_summary=summary,
                )
            except Exception as persistence_exc:
                category, _ = sanitize_runtime_failure(
                    persistence_exc,
                    stage="persistence",
                    limit=512,
                )
                self._log_attempt_persistence_failure(persistence_exc)
        self._status["last_error"] = category
        return {
            "status": "failed",
            "attempt_key": attempt_key,
            "reason": category,
        }

    @staticmethod
    def _log_attempt_persistence_failure(exc: Exception) -> None:
        _, summary = sanitize_runtime_failure(
            exc,
            stage="persistence",
            limit=512,
        )
        logger.error("Market Radar attempt status persistence failed: %s", summary)

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._status)
