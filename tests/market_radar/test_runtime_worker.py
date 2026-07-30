from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from src.market_radar.models import RadarRunSnapshot
from src.market_radar.repository import AttemptReservation, MarketRadarRepository
from src.market_radar.runtime_worker import (
    MarketRadarRuntimeWorker,
    _RADAR_RUNTIME_LOCK,
    sanitize_runtime_failure,
)
from src.market_radar.service import MarketRadarService
from src.market_radar.session_policy import (
    MarketRadarSessionPolicy,
    RadarRunDecision,
)
from src.services.runtime_scheduler import _RUNTIME_ANALYSIS_LOCK


NOW = datetime(2026, 7, 30, 2, 30, tzinfo=timezone.utc)


def decision(
    *,
    kind: str = "intraday_due",
    reason: str | None = None,
    attempt_key: str | None = "cn:intraday:2026-07-30:morning:1030",
) -> RadarRunDecision:
    return RadarRunDecision(
        kind=kind,
        decided_at=NOW,
        trading_date=date(2026, 7, 30),
        attempt_key=attempt_key,
        reason=reason,
    )


@pytest.fixture
def policy() -> Mock:
    value = Mock(spec=MarketRadarSessionPolicy)
    value.decide.return_value = decision()
    return value


@pytest.fixture
def repository() -> Mock:
    value = Mock(spec=MarketRadarRepository)
    value.reserve_scheduled_attempt.return_value = AttemptReservation(
        "cn:intraday:2026-07-30:morning:1030",
        True,
        "started",
        owner_token="owner-token",
    )
    value.get_run_id_by_key.return_value = 12
    return value


@pytest.fixture
def service() -> Mock:
    value = Mock(spec=MarketRadarService)
    value.run.return_value = RadarRunSnapshot(
        run_key="cn:20260730T023000Z:schedule",
        market="cn",
        trigger="schedule",
        as_of=NOW,
        quality="partial",
        scoring_version="cn-v1",
        sectors=(),
        provider_trace=(),
    )
    return value


@pytest.fixture
def service_factory(service: Mock) -> Mock:
    return Mock(return_value=service)


@pytest.fixture
def worker(
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
) -> MarketRadarRuntimeWorker:
    return MarketRadarRuntimeWorker(
        policy=policy,
        repository=repository,
        service_factory=service_factory,
        clock=lambda: NOW,
    )


def test_not_due_does_not_touch_repository(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
) -> None:
    policy.decide.return_value = decision(
        kind="not_due",
        reason="lunch_break",
        attempt_key=None,
    )

    assert worker.run_once() == {"status": "skipped", "reason": "lunch_break"}
    repository.reserve_scheduled_attempt.assert_not_called()


def test_calendar_unavailable_persists_bounded_skip_without_service(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
) -> None:
    policy.decide.return_value = decision(
        kind="calendar_unavailable",
        reason="calendar_unavailable",
        attempt_key="cn:calendar-error:2026-07-30:1030",
    )
    repository.reserve_scheduled_attempt.return_value = AttemptReservation(
        "cn:calendar-error:2026-07-30:1030",
        True,
        "started",
        owner_token="calendar-owner",
    )

    assert worker.run_once() == {
        "status": "skipped",
        "reason": "calendar_unavailable",
        "attempt_key": "cn:calendar-error:2026-07-30:1030",
    }
    repository.reserve_scheduled_attempt.assert_called_once_with(
        policy.decide.return_value,
        lease_seconds=900,
    )
    repository.finish_scheduled_attempt.assert_called_once_with(
        "cn:calendar-error:2026-07-30:1030",
        owner_token="calendar-owner",
        status="skipped",
        reason_code="calendar_unavailable",
    )
    service_factory.assert_not_called()


def test_duplicate_calendar_skip_does_not_build_service(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
) -> None:
    policy.decide.return_value = decision(
        kind="calendar_unavailable",
        reason="calendar_unavailable",
        attempt_key="cn:calendar-error:2026-07-30:1030",
    )
    repository.reserve_scheduled_attempt.return_value = AttemptReservation(
        "cn:calendar-error:2026-07-30:1030",
        False,
        "skipped",
    )

    assert worker.run_once()["reason"] == "calendar_unavailable"
    repository.finish_scheduled_attempt.assert_not_called()
    service_factory.assert_not_called()


def test_calendar_terminalization_failure_is_safely_logged(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy.decide.return_value = decision(
        kind="calendar_unavailable",
        reason="calendar_unavailable",
        attempt_key="cn:calendar-error:2026-07-30:1030",
    )
    repository.reserve_scheduled_attempt.return_value = AttemptReservation(
        "cn:calendar-error:2026-07-30:1030",
        True,
        "started",
        owner_token="calendar-owner",
    )
    repository.finish_scheduled_attempt.side_effect = RuntimeError(
        "database password=calendar-secret"
    )

    assert worker.run_once() == {
        "status": "failed",
        "attempt_key": "cn:calendar-error:2026-07-30:1030",
        "reason": "persistence_error",
    }
    assert "attempt status persistence failed" in caplog.text
    assert "calendar-secret" not in caplog.text
    service_factory.assert_not_called()


def test_calendar_reservation_failure_is_not_logged_as_terminalization(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy.decide.return_value = decision(
        kind="calendar_unavailable",
        reason="calendar_unavailable",
        attempt_key="cn:calendar-error:2026-07-30:1030",
    )
    repository.reserve_scheduled_attempt.side_effect = RuntimeError("database down")

    assert worker.run_once()["reason"] == "persistence_error"
    assert "attempt status persistence failed" not in caplog.text


def test_due_run_passes_attempt_identity_to_atomic_service_commit(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service: Mock,
    service_factory: Mock,
) -> None:
    result = worker.run_once()

    service_factory.assert_called_once_with(persist=True, repository=repository)
    service.run.assert_called_once_with(
        market="cn",
        trigger="schedule",
        schedule_kind="intraday",
        persist=True,
        attempt_key="cn:intraday:2026-07-30:morning:1030",
        attempt_owner_token="owner-token",
    )
    repository.finish_scheduled_attempt.assert_not_called()
    assert result == {
        "status": "succeeded",
        "attempt_key": "cn:intraday:2026-07-30:morning:1030",
        "run_id": 12,
    }


def test_eod_due_uses_eod_schedule_kind(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    service: Mock,
) -> None:
    policy.decide.return_value = decision(
        kind="eod_due",
        attempt_key="cn:eod:2026-07-30",
    )

    worker.run_once()

    assert service.run.call_args.kwargs["schedule_kind"] == "eod"


def test_duplicate_terminal_attempt_does_not_call_provider(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service_factory: Mock,
) -> None:
    repository.reserve_scheduled_attempt.return_value = AttemptReservation(
        "key",
        False,
        "succeeded",
        12,
    )

    assert worker.run_once() == {
        "status": "succeeded",
        "reason": "duplicate_slot",
        "attempt_key": "key",
        "run_id": 12,
    }
    service_factory.assert_not_called()


@pytest.mark.parametrize(
    ("run_kind", "reservation", "expected_status", "expected_reason"),
    [
        (
            "intraday_due",
            AttemptReservation("key", False, "started"),
            "skipped",
            "radar_already_running",
        ),
        (
            "eod_due",
            AttemptReservation("key", False, "succeeded", 12),
            "succeeded",
            "eod_already_finalized",
        ),
        (
            "intraday_due",
            AttemptReservation(
                "key",
                False,
                "skipped",
                reason_code="calendar_unavailable",
            ),
            "skipped",
            "calendar_unavailable",
        ),
        (
            "intraday_due",
            AttemptReservation(
                "key",
                False,
                "failed",
                failure_category="provider_error",
            ),
            "failed",
            "provider_error",
        ),
    ],
)
def test_non_acquired_attempt_preserves_distinct_durable_reason(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
    run_kind: str,
    reservation: AttemptReservation,
    expected_status: str,
    expected_reason: str,
) -> None:
    policy.decide.return_value = decision(
        kind=run_kind,
        attempt_key=("cn:eod:2026-07-30" if run_kind == "eod_due" else "key"),
    )
    repository.reserve_scheduled_attempt.return_value = reservation

    result = worker.run_once()

    assert result["status"] == expected_status
    assert result["reason"] == expected_reason
    service_factory.assert_not_called()


def test_radar_lock_contention_does_not_reserve_or_call_provider(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service_factory: Mock,
) -> None:
    assert _RADAR_RUNTIME_LOCK.acquire(blocking=False)
    try:
        assert worker.run_once()["reason"] == "radar_already_running"
    finally:
        _RADAR_RUNTIME_LOCK.release()

    repository.reserve_scheduled_attempt.assert_not_called()
    service_factory.assert_not_called()


def test_worker_uses_radar_specific_lock_not_ordinary_analysis_lock(
    worker: MarketRadarRuntimeWorker,
) -> None:
    assert _RUNTIME_ANALYSIS_LOCK.acquire(blocking=False)
    try:
        assert worker.run_once()["status"] == "succeeded"
    finally:
        _RUNTIME_ANALYSIS_LOCK.release()


def test_worker_failure_is_bounded_redacted_and_fail_open(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service: Mock,
) -> None:
    service.run.side_effect = RuntimeError(
        "provider token=secret password='p@ss' cookie=session-value " + "x" * 600
    )

    result = worker.run_once()

    assert result == {
        "status": "failed",
        "attempt_key": "cn:intraday:2026-07-30:morning:1030",
        "reason": "provider_error",
    }
    kwargs = repository.finish_scheduled_attempt.call_args.kwargs
    assert kwargs["failure_category"] == "provider_error"
    assert len(kwargs["failure_summary"]) <= 512
    assert "secret" not in kwargs["failure_summary"]
    assert "p@ss" not in kwargs["failure_summary"]
    assert "session-value" not in kwargs["failure_summary"]


def test_policy_failure_is_calendar_error_and_fail_open(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    repository: Mock,
) -> None:
    policy.decide.side_effect = RuntimeError("calendar password=secret")

    assert worker.run_once() == {
        "status": "failed",
        "attempt_key": None,
        "reason": "calendar_error",
    }
    assert worker.status()["last_error"] == "calendar_error"
    repository.reserve_scheduled_attempt.assert_not_called()


def test_decision_serialization_failure_does_not_change_execution_result(
    worker: MarketRadarRuntimeWorker,
    policy: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    due = Mock(spec=RadarRunDecision)
    due.kind = "intraday_due"
    due.reason = None
    due.attempt_key = "cn:intraday:2026-07-30:morning:1030"
    due.model_dump.side_effect = RuntimeError("diagnostic token=decision-secret")
    policy.decide.return_value = due

    assert worker.run_once()["status"] == "succeeded"
    assert worker.status()["last_decision"] is None
    assert "status update failed" in caplog.text
    assert "decision-secret" not in caplog.text


def test_success_status_clock_failure_preserves_durable_success(
    policy: Mock,
    repository: Mock,
    service_factory: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Mock(
        side_effect=[
            NOW,
            RuntimeError("diagnostic api_key=clock-secret"),
        ]
    )
    worker = MarketRadarRuntimeWorker(
        policy=policy,
        repository=repository,
        service_factory=service_factory,
        clock=clock,
    )

    assert worker.run_once() == {
        "status": "succeeded",
        "attempt_key": "cn:intraday:2026-07-30:morning:1030",
        "run_id": 12,
    }
    repository.finish_scheduled_attempt.assert_not_called()
    assert worker.status()["last_success_at"] is None
    assert worker.status()["last_error"] is None
    assert "status update failed" in caplog.text
    assert "clock-secret" not in caplog.text


def test_reservation_failure_is_persistence_error_without_provider_call(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service_factory: Mock,
) -> None:
    repository.reserve_scheduled_attempt.side_effect = RuntimeError(
        "database key=secret"
    )

    assert worker.run_once()["reason"] == "persistence_error"
    service_factory.assert_not_called()


def test_service_factory_failure_is_provider_error(
    worker: MarketRadarRuntimeWorker,
    service_factory: Mock,
    repository: Mock,
) -> None:
    service_factory.side_effect = RuntimeError("provider authorization=Bearer secret")

    assert worker.run_once()["reason"] == "provider_error"
    assert repository.finish_scheduled_attempt.call_args.kwargs[
        "failure_category"
    ] == "provider_error"


def test_failure_summary_redacts_serialized_and_prefixed_credentials() -> None:
    _, summary = sanitize_runtime_failure(
        RuntimeError(
            "payload={\"token\":\"json-secret\"} "
            "headers={'Authorization': 'Bearer header-secret'} "
            "api_key=query-secret access_token=access-secret"
        ),
        stage="provider",
    )

    assert "json-secret" not in summary
    assert "header-secret" not in summary
    assert "query-secret" not in summary
    assert "access-secret" not in summary


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        ("client_secret=client-secret-value", "client-secret-value"),
        (
            "request https://db-user:db-password@example.test/private failed",
            "db-password",
        ),
        (
            "payload={'outer': {'credentials': {'client_secret': 'nested-secret'}}}",
            "nested-secret",
        ),
        (
            "headers={'X-Api-Key': 'header-api-secret', 'Cookie': 'sid=cookie-secret'}",
            "header-api-secret",
        ),
        ("opaque=" + "Z" * 96, "Z" * 96),
    ],
)
def test_failure_summary_never_persists_unclassified_secret_material(
    message: str,
    forbidden: str,
) -> None:
    _, summary = sanitize_runtime_failure(RuntimeError(message), stage="provider")

    assert forbidden not in summary


def test_failed_attempt_terminalization_failure_is_safely_logged(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
    service: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.run.side_effect = RuntimeError("provider token=provider-secret")
    repository.finish_scheduled_attempt.side_effect = RuntimeError(
        "database password=persistence-secret"
    )

    assert worker.run_once()["reason"] == "persistence_error"
    assert "attempt status persistence failed" in caplog.text
    assert "persistence-secret" not in caplog.text
    assert "provider-secret" not in caplog.text


def test_atomic_success_does_not_issue_a_separate_terminal_write(
    worker: MarketRadarRuntimeWorker,
    repository: Mock,
) -> None:
    repository.finish_scheduled_attempt.side_effect = RuntimeError(
        "database api_key=persistence-secret"
    )

    result = worker.run_once()

    assert result["status"] == "succeeded"
    repository.finish_scheduled_attempt.assert_not_called()


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("calendar", "calendar_error"),
        ("provider", "provider_error"),
        ("persistence", "persistence_error"),
        ("runtime", "runtime_error"),
    ],
)
def test_failure_categories_are_stable(stage: str, expected: str) -> None:
    assert sanitize_runtime_failure(RuntimeError("boom"), stage=stage)[0] == expected


def test_status_is_a_deep_copy(worker: MarketRadarRuntimeWorker) -> None:
    worker.run_once()

    status = worker.status()
    status["last_decision"]["kind"] = "not_due"

    assert worker.status()["last_decision"]["kind"] == "intraday_due"
    assert worker.status()["last_success_at"] == NOW.isoformat()
    assert worker.status()["last_error"] is None
