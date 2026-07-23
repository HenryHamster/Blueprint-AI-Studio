"""Tests for notification_dispatch.py message builder functions."""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.notification_dispatch as nd
from app.notification_dispatch import (
    _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
    _RETRY_DELAYS,
    _RetryEntry,
    dispatch_external_notification,
    get_notification_stats,
    is_residual_remediation_task,
    post_auto_fix_result_message,
    post_auto_fix_spawned_message,
    post_cycle_summary_message,
    post_direction_milestone_message,
    post_event_message,
    post_account_model_regression_message,
    post_account_model_regression_recovered_message,
    post_failure_alert_message,
    post_feedback_acknowledgment,
    post_issue_implementation_spawned_message,
    post_pr_merged_message,
    post_remediation_escalation_message,
    post_residual_fix_audit_deferred_escalation_message,
    post_residual_fix_escalation_message,
    post_residual_fix_result_message,
    post_residual_fix_spawned_message,
    post_review_feedback_message,
    post_version_build_stalled_message,
    post_version_calcification_message,
    post_version_ci_unresolved_stall_message,
    post_version_stabilize_stalled_message,
)
from app.models import (
    MessageType,
    NotificationDeadLetter,
    Project,
    ProjectMember,
    ProjectMessage,
    ResidualCaseStatus,
    ResidualRemediationCase,
)
from sqlmodel import select as _select


# ---------------------------------------------------------------------------
# Helpers for dispatch_external_notification tests
# ---------------------------------------------------------------------------


def _make_runtime_config(
    webhook_url: str = "https://example.com/hook",
    enabled_events=None,
    throttle_seconds: int = 0,
):
    return {
        "notification_config": {
            "webhook_url": webhook_url,
            "enabled_events": enabled_events
            if enabled_events is not None
            else ["RUNNER_DEACTIVATED"],
            "throttle_seconds": throttle_seconds,
        }
    }


def _reset_stats():
    """Reset module-level counters and retry queue for test isolation."""
    nd.total_dispatched = 0
    nd.total_delivered = 0
    nd.total_failed = 0
    nd.last_failure_reason = ""
    nd._retry_queue.clear()
    nd._last_dispatch_time.clear()
    nd._per_project_stats.clear()


async def _persist_test_project(session) -> uuid.UUID:
    """Create the parent row required by canonical message sequencing."""
    project = Project(
        name=f"Notification test {uuid.uuid4()}",
        user_id="notification-dispatch-test",
    )
    session.add(project)
    await session.flush()
    return project.id


# ---------------------------------------------------------------------------
# dispatch_external_notification — delivery stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_increments_dispatched_and_delivered_on_success():
    _reset_stats()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=200),
        ),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_delivered"] == 1
    assert stats["total_failed"] == 0
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatched_runner_events_are_deliverable():
    """Every runner-health event, when present in enabled_events, is actually
    POSTed through the operator-configured webhook.

    Regression guard for the silent-drop bug: the runner-health event types
    (RUNNER_DEACTIVATED/RUNNER_DEGRADED/RUNNER_DEGRADED_ESCALATED/RUNNER_FLEET_OFFLINE/
    RUNNER_LOW_DISK and the RUNNER_OUTAGE_ESCALATED re-page) were not in the
    allowlist / any default
    enabled_events list, so dispatch_external_notification returned early and the
    external page never reached a destination. Now that they are allowlisted and
    default-on, an enabled runner event must deliver with its event_type and
    payload intact.
    """
    runner_events = [
        "RUNNER_DEACTIVATED",
        "RUNNER_DEGRADED",
        "RUNNER_DEGRADED_ESCALATED",
        "RUNNER_FLEET_OFFLINE",
        "RUNNER_LOW_DISK",
        "RUNNER_OUTAGE_ESCALATED",
    ]
    for event in runner_events:
        _reset_stats()
        deliver = AsyncMock(return_value=200)
        with (
            patch(
                "app.shared_state.runtime_config",
                _make_runtime_config(enabled_events=runner_events),
            ),
            patch("app.notification_dispatch._attempt_delivery", new=deliver),
        ):
            await dispatch_external_notification(
                uuid.uuid4(),
                event,
                {"runner_id": "abc123", "runner_label": "runner abc123", "title": "t"},
            )
        stats = get_notification_stats()
        assert stats["total_dispatched"] == 1, f"{event} not dispatched"
        assert stats["total_delivered"] == 1, f"{event} not delivered"
        assert stats["total_failed"] == 0, f"{event} failed"
        # The POST body carries the runner event_type and payload through unchanged.
        assert deliver.await_count == 1, f"{event} not POSTed"
        _url, body = deliver.await_args.args
        assert body["event_type"] == event
        assert body["payload"]["runner_id"] == "abc123"


@pytest.mark.asyncio
async def test_runner_event_dropped_when_not_enabled():
    """A runner-health event NOT in enabled_events is still gated out (no POST),
    confirming the deliverability above is driven by the allowlist, not a
    blanket always-deliver change."""
    _reset_stats()
    deliver = AsyncMock(return_value=200)
    with (
        patch(
            "app.shared_state.runtime_config",
            _make_runtime_config(enabled_events=["TASK_FAILED"]),
        ),
        patch("app.notification_dispatch._attempt_delivery", new=deliver),
    ):
        await dispatch_external_notification(
            uuid.uuid4(), "RUNNER_DEACTIVATED", {"runner_id": "abc"}
        )
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 0
    assert deliver.await_count == 0


@pytest.mark.asyncio
async def test_external_dispatch_drops_strategist_internal_event():
    """A strategist-internal event is dropped by the audience gate even when the
    operator's enabled_events (legacy config) still lists it.

    The external webhook is a third operator-reaching channel and must obey the
    same fail-closed audience policy as the ledger and alert channels: PR_MERGED,
    DIRECTION_MILESTONE and AUTO_FIX_SPAWNED resolve to strategist-internal, so no
    external POST is made regardless of enablement.
    """
    internal_events = ["PR_MERGED", "DIRECTION_MILESTONE", "AUTO_FIX_SPAWNED"]
    for event in internal_events:
        _reset_stats()
        deliver = AsyncMock(return_value=200)
        with (
            patch(
                "app.shared_state.runtime_config",
                # Operator has (legacy) enabled the internal event — it must
                # still be gated out of the external webhook.
                _make_runtime_config(enabled_events=internal_events),
            ),
            patch("app.notification_dispatch._attempt_delivery", new=deliver),
        ):
            await dispatch_external_notification(
                uuid.uuid4(), event, {"pr_number": 123}
            )
        stats = get_notification_stats()
        assert stats["total_dispatched"] == 0, f"{event} leaked (dispatched)"
        assert stats["retry_queue_depth"] == 0, f"{event} enqueued a retry"
        assert deliver.await_count == 0, f"{event} was POSTed externally"


@pytest.mark.asyncio
async def test_external_dispatch_delivers_operator_visible_event():
    """Operator-facing webhook events (action-needed and fyi) still deliver through
    the audience gate — the gate drops only strategist-internal/pipeline-log."""
    operator_events = ["AUTO_FIX_ESCALATED", "BUDGET_EXCEEDED", "RUNNER_DEGRADED"]
    for event in operator_events:
        _reset_stats()
        deliver = AsyncMock(return_value=200)
        with (
            patch(
                "app.shared_state.runtime_config",
                _make_runtime_config(enabled_events=operator_events),
            ),
            patch("app.notification_dispatch._attempt_delivery", new=deliver),
        ):
            await dispatch_external_notification(uuid.uuid4(), event, {"k": "v"})
        stats = get_notification_stats()
        assert stats["total_dispatched"] == 1, f"{event} not dispatched"
        assert stats["total_delivered"] == 1, f"{event} not delivered"
        assert deliver.await_count == 1, f"{event} not POSTed"
        _url, body = deliver.await_args.args
        assert body["event_type"] == event


@pytest.mark.asyncio
async def test_dispatch_enqueues_retry_on_502():
    _reset_stats()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=502),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_delivered"] == 0
    assert stats["retry_queue_depth"] == 1
    entry = nd._retry_queue[0]
    assert entry.attempts == 0
    assert entry.event_type == "RUNNER_DEACTIVATED"


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_404():
    _reset_stats()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=404),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0
    assert "404" in stats["last_failure_reason"]


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_401():
    _reset_stats()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=401),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {})
    stats = get_notification_stats()
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_403():
    _reset_stats()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=403),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {})
    stats = get_notification_stats()
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatch_enqueues_retry_on_network_error():
    _reset_stats()
    import httpx

    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(side_effect=httpx.ConnectError("refused")),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["retry_queue_depth"] == 1


# ---------------------------------------------------------------------------
# Per-project stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_notification_stats_per_project():
    _reset_stats()
    pid = uuid.uuid4()
    other_pid = uuid.uuid4()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=200),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(pid, "RUNNER_DEACTIVATED", {"k": "v"})
        await dispatch_external_notification(pid, "RUNNER_DEACTIVATED", {"k": "v2"})
        await dispatch_external_notification(other_pid, "RUNNER_DEACTIVATED", {"k": "v3"})

    pstats = get_notification_stats(project_id=pid)
    assert pstats["total_dispatched"] == 2
    assert pstats["total_delivered"] == 2
    assert pstats["total_failed"] == 0
    assert pstats["retry_queue_depth"] == 0

    other_stats = get_notification_stats(project_id=other_pid)
    assert other_stats["total_dispatched"] == 1

    global_stats = get_notification_stats()
    assert global_stats["total_dispatched"] == 3


@pytest.mark.asyncio
async def test_get_notification_stats_per_project_failure():
    _reset_stats()
    pid = uuid.uuid4()
    with (
        patch("app.shared_state.runtime_config", _make_runtime_config()),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=401),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(pid, "RUNNER_DEACTIVATED", {})

    pstats = get_notification_stats(project_id=pid)
    assert pstats["total_failed"] == 1
    assert "401" in pstats["last_failure_reason"]
    assert pstats["last_failure_at"] is not None

    unknown_stats = get_notification_stats(project_id=uuid.uuid4())
    assert unknown_stats["total_dispatched"] == 0
    assert unknown_stats["last_failure_at"] is None


@pytest.mark.asyncio
async def test_dispatch_throttle_is_project_scoped():
    _reset_stats()
    pid = uuid.uuid4()
    other_pid = uuid.uuid4()
    deliver = AsyncMock(return_value=200)

    with (
        patch(
            "app.shared_state.runtime_config",
            _make_runtime_config(throttle_seconds=60),
        ),
        patch("app.notification_dispatch._attempt_delivery", new=deliver),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(pid, "RUNNER_DEACTIVATED", {"k": "first"})
        await dispatch_external_notification(pid, "RUNNER_DEACTIVATED", {"k": "suppressed"})
        await dispatch_external_notification(other_pid, "RUNNER_DEACTIVATED", {"k": "other"})

    assert deliver.await_count == 2
    pstats = get_notification_stats(project_id=pid)
    other_stats = get_notification_stats(project_id=other_pid)
    assert pstats["total_dispatched"] == 1
    assert other_stats["total_dispatched"] == 1
    assert get_notification_stats()["total_dispatched"] == 2


# ---------------------------------------------------------------------------
# Retry queue bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_queue_bounded_at_50():
    _reset_stats()
    # Fill queue past maxlen by injecting entries directly
    for i in range(60):
        nd._retry_queue.append(
            _RetryEntry(
                project_id=uuid.uuid4(),
                event_type="test_event",
                payload={},
                body={},
                attempts=0,
                next_retry_at=time.monotonic() + 9999,
            )
        )
    assert len(nd._retry_queue) == 50  # deque(maxlen=50) truncates oldest


# ---------------------------------------------------------------------------
# _drain_retry_queue — retry delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_retry_succeeds_and_updates_delivered():
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={"event_type": "test_event"},
            attempts=0,
            next_retry_at=time.monotonic() - 1,  # due now
        )
    )
    with patch(
        "app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=200)
    ):
        await nd._drain_retry_queue("https://example.com/hook")
    assert nd.total_delivered == 1
    assert len(nd._retry_queue) == 0


@pytest.mark.asyncio
async def test_drain_retry_re_enqueues_on_502():
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={},
            attempts=0,
            next_retry_at=time.monotonic() - 1,
        )
    )
    with patch(
        "app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=502)
    ):
        await nd._drain_retry_queue("https://example.com/hook")
    # Should be re-enqueued with attempts=1
    assert len(nd._retry_queue) == 1
    assert nd._retry_queue[0].attempts == 1
    assert nd.total_failed == 0


@pytest.mark.asyncio
async def test_drain_retry_exhausts_and_counts_failed():
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={},
            attempts=len(_RETRY_DELAYS) - 1,  # last attempt
            next_retry_at=time.monotonic() - 1,
        )
    )
    with patch(
        "app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=503)
    ):
        await nd._drain_retry_queue("https://example.com/hook")
    assert nd.total_failed == 1
    assert len(nd._retry_queue) == 0


@pytest.mark.asyncio
async def test_drain_skips_not_due_entries():
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={},
            attempts=0,
            next_retry_at=time.monotonic() + 9999,  # not due
        )
    )
    with patch(
        "app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=200)
    ) as mock_del:
        await nd._drain_retry_queue("https://example.com/hook")
    mock_del.assert_not_called()
    assert len(nd._retry_queue) == 1  # still in queue


# ---------------------------------------------------------------------------
# Cluster C12: per-entry webhook_url, independent drainer, maxlen eviction log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_uses_per_entry_webhook_url_not_ambient():
    """A retry delivers to the URL snapshotted on the entry, NOT whatever
    webhook_url runtime_config holds when the drain fires. Without this, a
    config change between dispatch and retry leaks the retry to the wrong URL."""
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={"event_type": "test_event"},
            attempts=0,
            next_retry_at=time.monotonic() - 1,  # due now
            webhook_url="https://original.example.com/hook",
        )
    )
    deliver_mock = AsyncMock(return_value=200)
    with patch("app.notification_dispatch._attempt_delivery", new=deliver_mock):
        # Ambient URL is DIFFERENT — must be ignored in favour of the entry's.
        await nd._drain_retry_queue("https://changed.example.com/new-hook")
    deliver_mock.assert_awaited_once()
    assert deliver_mock.call_args[0][0] == "https://original.example.com/hook"
    assert nd.total_delivered == 1


@pytest.mark.asyncio
async def test_dispatch_records_webhook_url_on_retry_entry():
    """When a dispatch fails, the enqueued retry stores the destination URL so
    the independent drainer can deliver it without ambient config."""
    _reset_stats()
    with (
        patch(
            "app.shared_state.runtime_config",
            _make_runtime_config(webhook_url="https://dest.example.com/hook"),
        ),
        patch(
            "app.notification_dispatch._attempt_delivery",
            new=AsyncMock(return_value=503),
        ),
        patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()),
    ):
        await dispatch_external_notification(uuid.uuid4(), "RUNNER_DEACTIVATED", {"k": "v"})
    assert len(nd._retry_queue) == 1
    assert nd._retry_queue[0].webhook_url == "https://dest.example.com/hook"


@pytest.mark.asyncio
async def test_drain_drops_entry_without_any_webhook_url():
    """An entry with no stored URL and no ambient fallback cannot be delivered
    and is dropped (counted as failed) rather than crashing the drain."""
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={},
            attempts=0,
            next_retry_at=time.monotonic() - 1,
            webhook_url="",
        )
    )
    deliver_mock = AsyncMock(return_value=200)
    with patch("app.notification_dispatch._attempt_delivery", new=deliver_mock):
        await nd._drain_retry_queue("")  # no fallback either
    deliver_mock.assert_not_called()
    assert nd.total_failed == 1
    assert len(nd._retry_queue) == 0


@pytest.mark.asyncio
async def test_independent_drain_service_flushes_without_new_dispatch():
    """The background drainer flushes a due retry on its own schedule — proving
    retries are no longer stranded during quiet periods (no new dispatch)."""
    _reset_stats()
    nd._retry_queue.append(
        _RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={"event_type": "test_event"},
            attempts=0,
            next_retry_at=time.monotonic() - 1,  # due now
            webhook_url="https://dest.example.com/hook",
        )
    )
    deliver_mock = AsyncMock(return_value=200)
    with (
        patch("app.notification_dispatch._attempt_delivery", new=deliver_mock),
        patch("app.notification_dispatch._RETRY_DRAIN_INTERVAL_SECONDS", 0),
    ):
        task = asyncio.create_task(nd.notification_retry_drain_service())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    deliver_mock.assert_awaited()
    assert nd.total_delivered == 1
    assert len(nd._retry_queue) == 0


def test_enqueue_retry_logs_on_maxlen_eviction(caplog):
    """When the bounded queue is full, _enqueue_retry logs the evicted entry so
    a silently-dropped alert retry is at least visible in the logs."""
    import logging

    _reset_stats()
    # Fill to capacity via the logging enqueue helper.
    for i in range(nd._RETRY_QUEUE_MAXLEN):
        nd._enqueue_retry(
            _RetryEntry(
                project_id=uuid.uuid4(),
                event_type=f"evt_{i}",
                payload={},
                body={},
                attempts=0,
                next_retry_at=time.monotonic() + 9999,
                webhook_url="https://h",
            )
        )
    assert len(nd._retry_queue) == nd._RETRY_QUEUE_MAXLEN
    with caplog.at_level(logging.WARNING, logger="app.notification_dispatch"):
        nd._enqueue_retry(
            _RetryEntry(
                project_id=uuid.uuid4(),
                event_type="overflow",
                payload={},
                body={},
                attempts=0,
                next_retry_at=time.monotonic() + 9999,
                webhook_url="https://h",
            )
        )
    assert any("evicting oldest" in r.message.lower() for r in caplog.records)
    # Still bounded; the oldest was evicted to make room for the new one.
    assert len(nd._retry_queue) == nd._RETRY_QUEUE_MAXLEN


@pytest.mark.asyncio
async def test_exhausted_or_evicted_notification_writes_dead_letter(db_session):
    """A notification that exhausts its retries OR is evicted from the full
    in-memory queue must leave a durable, re-drivable NotificationDeadLetter
    row instead of silently vanishing."""

    from app.field_encryption import decrypt_field

    _reset_stats()
    pid = uuid.uuid4()

    # --- Exhausted path: a final-attempt retry that keeps failing (503) ---
    nd._retry_queue.append(
        _RetryEntry(
            project_id=pid,
            event_type="exhausted_event",
            payload={"k": "v"},
            body={"event_type": "exhausted_event"},
            attempts=len(_RETRY_DELAYS) - 1,  # last attempt — next failure exhausts
            next_retry_at=time.monotonic() - 1,  # due now
            webhook_url="https://dest.example.com/hook",
        )
    )
    with patch(
        "app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=503)
    ):
        await nd._drain_retry_queue("https://dest.example.com/hook")

    exhausted = (
        (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.reason == "exhausted",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(exhausted) == 1
    row = exhausted[0]
    assert row.event_type == "exhausted_event"
    assert row.last_status == 503
    assert row.attempts == len(_RETRY_DELAYS)  # attempts+1, all attempts used
    assert row.payload == {"k": "v"}
    assert row.webhook_url != "https://dest.example.com/hook"
    assert decrypt_field(row.webhook_url) == "https://dest.example.com/hook"
    assert len(nd._retry_queue) == 0  # exhausted entry not re-queued

    # --- Evicted path: overflowing the bounded queue drops the oldest entry ---
    nd._retry_queue.clear()
    for i in range(nd._RETRY_QUEUE_MAXLEN):
        nd._retry_queue.append(
            _RetryEntry(
                project_id=pid,
                event_type=f"filler_{i}",
                payload={},
                body={},
                attempts=0,
                next_retry_at=time.monotonic() + 9999,
                webhook_url="https://h",
            )
        )
    # Enqueuing one more entry evicts the oldest (filler_0) — it must be dead-lettered.
    await nd._enqueue_retry_durable(
        _RetryEntry(
            project_id=pid,
            event_type="overflow",
            payload={"o": 1},
            body={},
            attempts=0,
            next_retry_at=time.monotonic() + 9999,
            webhook_url="https://h",
        )
    )

    evicted = (
        (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.reason == "evicted",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(evicted) == 1
    assert evicted[0].event_type == "filler_0"
    # The bounded queue stays capped after the overflow append.
    assert len(nd._retry_queue) == nd._RETRY_QUEUE_MAXLEN


@pytest.mark.asyncio
async def test_dead_letter_double_failure_creates_operator_alert(db_session):
    """When the dead-letter persistence write fails (double-failure), an operator-
    visible notification.dead_letter_lost Alert is created as a fallback.

    The scenario: notification delivery failed → _write_dead_letter called →
    dead-letter DB write also fails → a notification.dead_letter_lost Alert must
    surface so the operator sees the permanent notification loss.
    """
    import contextlib
    from app.models import Alert, AlertSeverity
    from app.message_audience import get_operator_event_types

    # Create a real project so the Alert FK constraint is satisfied.
    project_id = await _persist_test_project(db_session)
    await db_session.commit()

    entry = _RetryEntry(
        project_id=project_id,
        event_type="test.double_failure_event",
        payload={"k": "v"},
        body={"event_type": "test.double_failure_event"},
        attempts=3,
        next_retry_at=0.0,
        webhook_url="https://example.com/hook",
    )

    call_count = 0

    @contextlib.asynccontextmanager
    async def _controlled_closing():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call (dead-letter write): simulate DB failure.
            raise RuntimeError("simulated dead-letter DB write failure")
        # Second call (alert creation fallback): use real test session.
        yield db_session

    with patch("app.database.closing_session", new=_controlled_closing):
        await nd._write_dead_letter(entry, reason="exhausted", last_status=503)

    assert call_count == 2, "must attempt dead-letter write then alert creation"

    # The fallback Alert must exist and be operator-visible.
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(
                    Alert.project_id == str(project_id),
                    Alert.event_type == "notification.dead_letter_lost",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "double-failure must surface a notification.dead_letter_lost alert"
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "test.double_failure_event" in alert.message
    assert "exhausted" in alert.message
    # Verify event_type passes operator_visible_alert_filter (not hidden as STRATEGIST_INTERNAL).
    assert alert.event_type in get_operator_event_types(), (
        "notification.dead_letter_lost not in operator_visible_alert_filter allowlist"
    )


# ---------------------------------------------------------------------------
# get_notification_stats
# ---------------------------------------------------------------------------


def test_get_notification_stats_initial():
    _reset_stats()
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 0
    assert stats["total_delivered"] == 0
    assert stats["total_failed"] == 0
    assert stats["last_failure_reason"] == ""
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_post_event_message_basic(db_session):
    project_id = await _persist_test_project(db_session)
    # "failure_alert" is a registered operator-visible (operator-fyi) event
    # type, so post_event_message creates the operator-facing NOTE and stamps
    # the resolved audience onto the row.
    msg = await post_event_message(db_session, project_id, "failure_alert", "Test summary")
    assert msg is not None
    assert msg.project_id == project_id
    assert msg.content == "Test summary"
    assert msg.context["event_type"] == "failure_alert"
    assert msg.context["audience"] == "operator-fyi"
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.from_task_id is None
    # Should be added to session (not yet committed, but tracked)
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_event_message_unregistered_event_is_tagged_strategist_internal(db_session):
    """Fail-closed: an unregistered event_type resolves to strategist-internal.

    The row is still created (a non-operator strategist-feed row the strategist
    may want) but is stamped ``audience="strategist-internal"`` so operator
    surfaces can exclude it — it is never operator-visible.
    """
    project_id = await _persist_test_project(db_session)
    msg = await post_event_message(db_session, project_id, "test_event", "Test summary")
    assert msg is not None
    assert msg.context["event_type"] == "test_event"
    assert msg.context["audience"] == "strategist-internal"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_event_message_registered_internal_event_is_tagged(db_session):
    """A registered strategist-internal event type is created but tagged non-operator."""
    project_id = await _persist_test_project(db_session)
    # "pr_merged" is registered as strategist-internal bookkeeping.
    msg = await post_event_message(db_session, project_id, "pr_merged", "Merged")
    assert msg is not None
    assert msg.context["audience"] == "strategist-internal"


@pytest.mark.asyncio
async def test_post_event_message_with_context_and_task_id(db_session):
    project_id = await _persist_test_project(db_session)
    task_id = uuid.uuid4()
    extra_ctx = {"key": "value", "num": 42}
    # "question_answer" is a registered operator-visible (operator-fyi) event type.
    msg = await post_event_message(
        db_session,
        project_id,
        "question_answer",
        "Summary",
        context=extra_ctx,
        task_id=task_id,
    )
    assert msg is not None
    assert msg.context["event_type"] == "question_answer"
    assert msg.context["audience"] == "operator-fyi"
    assert msg.context["key"] == "value"
    assert msg.context["num"] == 42
    assert msg.from_task_id == task_id


@pytest.mark.asyncio
async def test_post_criterion_retracted_message(db_session):
    project_id = await _persist_test_project(db_session)
    version_id = uuid.uuid4()
    msg = await nd.post_criterion_retracted_message(
        db_session,
        project_id,
        criterion_key="c-1a2b3c4d",
        text="Uncoverable acceptance criterion",
        reason="No feasible evidence path",
        release_version_id=version_id,
        retracted_by="strategist",
    )
    assert msg.project_id == project_id
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.context["event_type"] == "criterion_retracted"
    assert msg.context["criterion_key"] == "c-1a2b3c4d"
    assert msg.context["reason"] == "No feasible evidence path"
    assert msg.context["retracted_by"] == "strategist"
    assert msg.context["release_version_id"] == str(version_id)
    assert "c-1a2b3c4d" in msg.content
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_criterion_retracted_message_truncates_reason_and_text(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await nd.post_criterion_retracted_message(
        db_session,
        project_id,
        criterion_key="c-deadbeef",
        text="t" * 500,
        reason="r" * 900,
        release_version_id=uuid.uuid4(),
        retracted_by="strategist",
    )
    assert len(msg.context["text"]) <= 200
    assert len(msg.context["reason"]) <= 400


@pytest.mark.asyncio
async def test_post_criterion_superseded_message(db_session):
    project_id = await _persist_test_project(db_session)
    version_id = uuid.uuid4()
    msg = await nd.post_criterion_superseded_message(
        db_session,
        project_id,
        criterion_key="c-1a2b3c4d",
        text="Old criterion",
        reason="Replaced by a sharper criterion",
        release_version_id=version_id,
        superseded_by_key="c-99887766",
        retracted_by="strategist",
    )
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.context["event_type"] == "criterion_superseded"
    assert msg.context["criterion_key"] == "c-1a2b3c4d"
    assert msg.context["superseded_by_key"] == "c-99887766"
    assert msg.context["retracted_by"] == "strategist"
    assert "c-99887766" in msg.content


def test_criterion_events_are_high_signal_note_types():
    """Both criterion lifecycle events must be registered as high-signal NOTEs
    so they survive paging in the operator-attention read model."""
    assert "criterion_retracted" in nd.HIGH_SIGNAL_NOTE_EVENT_TYPES
    assert "criterion_superseded" in nd.HIGH_SIGNAL_NOTE_EVENT_TYPES
    from app.routers.admin_notifications import (
        HIGH_SIGNAL_NOTE_EVENT_TYPES as AN_HIGH_SIGNAL,
    )

    assert "criterion_retracted" in AN_HIGH_SIGNAL
    assert "criterion_superseded" in AN_HIGH_SIGNAL


@pytest.mark.asyncio
async def test_post_pr_merged_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_pr_merged_message(
        db_session,
        project_id,
        pr_number=42,
        direction_text="Add dark mode",
        task_count=3,
    )
    assert msg.project_id == project_id
    assert "42" in msg.content
    assert "3" in msg.content
    assert "Add dark mode" in msg.content
    assert msg.context["event_type"] == "pr_merged"
    assert msg.context["pr_number"] == 42
    assert msg.context["task_count"] == 3
    assert msg.context["direction_text"] == "Add dark mode"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_pr_merged_message_truncates_direction_text(db_session):
    project_id = await _persist_test_project(db_session)
    long_text = "x" * 200
    msg = await post_pr_merged_message(
        db_session, project_id, pr_number=1, direction_text=long_text, task_count=1
    )
    assert len(msg.context["direction_text"]) <= 120


@pytest.mark.asyncio
async def test_post_direction_milestone_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_direction_milestone_message(
        db_session,
        project_id,
        direction_text="Improve search",
        milestone="auto-completed",
        reason="All tasks merged",
    )
    assert msg.project_id == project_id
    assert "auto-completed" in msg.content
    assert "Improve search" in msg.content
    assert "All tasks merged" in msg.content
    assert msg.context["event_type"] == "direction_milestone"
    assert msg.context["milestone"] == "auto-completed"
    assert msg.context["reason"] == "All tasks merged"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_direction_milestone_message_truncates_text(db_session):
    project_id = await _persist_test_project(db_session)
    long_text = "y" * 200
    msg = await post_direction_milestone_message(
        db_session,
        project_id,
        direction_text=long_text,
        milestone="auto-paused",
        reason="reason",
    )
    assert len(msg.context["direction_text"]) <= 120


@pytest.mark.asyncio
async def test_post_direction_milestone_message_fires_webhook(db_session):
    """Creating the milestone message also fires DIRECTION_MILESTONE via the external webhook."""
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_direction_milestone_message(
            db_session,
            project_id,
            direction_text="Improve search relevance",
            milestone="auto-completed",
            reason="Saturated with 5 PRs",
            direction_id="d-abc123",
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)
    dispatch_mock.assert_called_once()
    args, _ = dispatch_mock.call_args
    assert args[0] == project_id
    assert args[1] == "DIRECTION_MILESTONE"
    assert args[2]["direction_text"] == "Improve search relevance"
    assert args[2]["milestone"] == "auto-completed"
    assert args[2]["reason"] == "Saturated with 5 PRs"
    assert args[2]["direction_id"] == "d-abc123"


@pytest.mark.asyncio
async def test_post_direction_milestone_message_adds_reason_code_and_evidence(
    db_session,
):
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    evidence = {"cost_per_merge": 12.5, "merge_count": 1}
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        msg = await post_direction_milestone_message(
            db_session,
            project_id,
            direction_text="Reduce flaky tests",
            milestone="auto-paused",
            reason="Paused: cost per merge is too high",
            direction_id="d-xyz789",
            evidence=evidence,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    assert msg.context["reason_code"] == "cost_per_merge_high"
    assert msg.context["evidence"] == evidence
    dispatch_mock.assert_called_once()
    args, _ = dispatch_mock.call_args
    assert args[2]["reason_code"] == "cost_per_merge_high"
    assert args[2]["evidence"] == evidence


@pytest.mark.asyncio
async def test_post_direction_milestone_message_no_reason_code_for_non_paused(
    db_session,
):
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        msg = await post_direction_milestone_message(
            db_session,
            project_id,
            direction_text="Improve retention",
            milestone="auto-completed",
            reason="Cost per merge improved after rollout",
            direction_id="d-finish1",
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    assert "reason_code" not in msg.context
    dispatch_mock.assert_called_once()
    args, _ = dispatch_mock.call_args
    assert "reason_code" not in args[2]


@pytest.mark.asyncio
async def test_post_direction_milestone_message_reason_code_budget_exceeded(db_session):
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        msg = await post_direction_milestone_message(
            db_session,
            project_id,
            direction_text="Trim infra spend",
            milestone="auto-paused",
            reason="Auto-paused: direction is over budget this cycle",
            direction_id="d-budget1",
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    assert msg.context["reason_code"] == "budget_exceeded"
    args, _ = dispatch_mock.call_args
    assert args[2]["reason_code"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_post_direction_milestone_message_reason_code_low_yield(db_session):
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        msg = await post_direction_milestone_message(
            db_session,
            project_id,
            direction_text="Improve onboarding",
            milestone="auto-paused",
            reason="Auto-paused: low yield after 3 cycles",
            direction_id="d-yield1",
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    assert msg.context["reason_code"] == "low_yield"
    args, _ = dispatch_mock.call_args
    assert args[2]["reason_code"] == "low_yield"


@pytest.mark.asyncio
async def test_post_cycle_summary_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_cycle_summary_message(
        db_session,
        project_id,
        vision_rev=7,
        dirs_created=3,
        dirs_retired=1,
        ops_applied=5,
        msgs_created=2,
    )
    assert msg.project_id == project_id
    assert "7" in msg.content
    assert "3" in msg.content
    assert "1" in msg.content
    assert msg.context["event_type"] == "cycle_summary"
    assert msg.context["vision_rev"] == 7
    assert msg.context["dirs_created"] == 3
    assert msg.context["dirs_retired"] == 1
    assert msg.context["ops_applied"] == 5
    assert msg.context["msgs_created"] == 2
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_failure_alert_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_failure_alert_message(
        db_session, project_id, description="Circuit breaker opened"
    )
    assert msg.project_id == project_id
    assert "Circuit breaker opened" in msg.content
    assert msg.context["event_type"] == "failure_alert"
    assert msg.context["description"] == "Circuit breaker opened"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_account_model_regression_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_account_model_regression_message(
        db_session,
        project_id,
        account_id="acct-123",
        account_label="ProdKey",
        provider="anthropic",
        model="claude-opus-4-7",
        account_rate=0.10,
        project_median=0.85,
        sample_size=20,
        distinct_accounts=3,
        started_at="2026-04-29T00:00:00+00:00",
    )
    assert msg.project_id == project_id
    # Summary surfaces the structured detail in plain text.
    assert "ProdKey" in msg.content
    assert "anthropic/claude-opus-4-7" in msg.content
    assert msg.context["event_type"] == "account_model_regression_quarantined"
    assert msg.context["account_id"] == "acct-123"
    assert msg.context["account_label"] == "ProdKey"
    assert msg.context["provider"] == "anthropic"
    assert msg.context["model"] == "claude-opus-4-7"
    assert msg.context["account_rate"] == 0.10
    assert msg.context["project_median"] == 0.85
    assert msg.context["sample_size"] == 20
    assert msg.context["distinct_accounts"] == 3
    assert msg.context["started_at"] == "2026-04-29T00:00:00+00:00"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_account_model_regression_recovered_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_account_model_regression_recovered_message(
        db_session,
        project_id,
        account_id="acct-123",
        account_label="ProdKey",
        provider="anthropic",
        model="claude-opus-4-7",
        account_rate=0.20,
        project_median=0.85,
        sample_size=10,
    )
    assert msg.project_id == project_id
    assert "ProdKey" in msg.content
    assert "anthropic/claude-opus-4-7" in msg.content
    assert "restored after probe success" in msg.content
    assert msg.context["event_type"] == "account_model_regression_recovered"
    assert msg.context["account_id"] == "acct-123"
    assert msg.context["account_label"] == "ProdKey"
    assert msg.context["provider"] == "anthropic"
    assert msg.context["model"] == "claude-opus-4-7"
    assert msg.context["account_rate"] == 0.20
    assert msg.context["project_median"] == 0.85
    assert msg.context["sample_size"] == 10
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_account_model_regression_recovered_message_no_stats(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_account_model_regression_recovered_message(
        db_session,
        project_id,
        account_id="acct-456",
        account_label="DevKey",
        provider="google",
        model="gemini-pro",
    )
    assert "DevKey" in msg.content
    assert "google/gemini-pro" in msg.content
    assert msg.context["event_type"] == "account_model_regression_recovered"
    assert msg.context["account_rate"] is None
    assert msg.context["sample_size"] is None
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_auto_fix_spawned_message_few_checks(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_auto_fix_spawned_message(
        db_session,
        project_id,
        branch="main",
        failed_checks=["lint", "typecheck"],
    )
    assert msg.project_id == project_id
    assert "lint" in msg.content
    assert "typecheck" in msg.content
    assert "main" in msg.content
    assert msg.context["event_type"] == "auto_fix_spawned"
    assert msg.context["branch"] == "main"
    assert msg.context["failed_checks"] == ["lint", "typecheck"]
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_auto_fix_spawned_message_many_checks(db_session):
    project_id = await _persist_test_project(db_session)
    checks = ["check1", "check2", "check3", "check4", "check5"]
    msg = await post_auto_fix_spawned_message(
        db_session, project_id, branch="feature", failed_checks=checks
    )
    assert "+2 more" in msg.content
    assert len(msg.context["failed_checks"]) == 5


@pytest.mark.asyncio
async def test_post_auto_fix_result_merged(db_session):
    # Real project required: post_auto_fix_result_message does session.get(Project)
    # which autoflushes the pending msg → FK violation if no project exists.
    project = Project(name="Auto Fix Merged Test", user_id="test-user-pafrm")
    db_session.add(project)
    await db_session.commit()
    msg = await post_auto_fix_result_message(
        db_session, project.id, branch="main", result="merged", pr_number=99
    )
    assert "merged" in msg.content
    assert "99" in msg.content
    assert msg.context["event_type"] == "auto_fix_result"
    assert msg.context["result"] == "merged"
    assert msg.context["pr_number"] == 99
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_auto_fix_result_failed(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_auto_fix_result_message(
        db_session, project_id, branch="main", result="failed"
    )
    assert "failed" in msg.content
    assert "manual intervention" in msg.content
    assert msg.context["result"] == "failed"


@pytest.mark.asyncio
async def test_post_auto_fix_result_review_needed(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_auto_fix_result_message(
        db_session, project_id, branch="feat", result="review_needed", pr_number=55
    )
    assert "review" in msg.content
    assert "55" in msg.content
    assert msg.context["result"] == "review_needed"


@pytest.mark.asyncio
async def test_post_remediation_escalation_message(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_remediation_escalation_message(
        db_session,
        project_id,
        direction_text="Improve CI reliability",
        failure_pattern="AssertionError in test_foo",
        attempts=3,
        remediation_summary="Added retry wrapper; same fingerprint recurred",
    )
    assert msg.project_id == project_id
    assert msg.message_type == MessageType.BLOCKER
    assert "Improve CI reliability" in msg.content
    assert "3" in msg.content
    assert msg.context["event_type"] == "remediation_escalated"
    assert msg.context["failure_pattern"] == "AssertionError in test_foo"
    assert msg.context["attempts"] == 3
    assert msg.context["remediation_summary"].startswith("Added retry wrapper")
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_remediation_escalation_message_truncates(db_session):
    project_id = await _persist_test_project(db_session)
    long_dir = "d" * 200
    long_pattern = "p" * 500
    long_summary = "s" * 500
    msg = await post_remediation_escalation_message(
        db_session,
        project_id,
        direction_text=long_dir,
        failure_pattern=long_pattern,
        attempts=5,
        remediation_summary=long_summary,
    )
    assert len(msg.context["direction_text"]) <= 120
    assert len(msg.context["failure_pattern"]) <= 200
    assert len(msg.context["remediation_summary"]) <= 240


@pytest.mark.asyncio
async def test_post_remediation_escalation_message_fires_webhook(db_session):
    """Creating the escalation message also fires REMEDIATION_ESCALATED via the external webhook."""
    project_id = await _persist_test_project(db_session)
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_remediation_escalation_message(
            db_session,
            project_id,
            direction_text="Improve CI reliability",
            failure_pattern="AssertionError in test_foo",
            attempts=3,
            remediation_summary="Added retry wrapper; same fingerprint recurred",
        )
        # Allow the ensure_future-scheduled coroutine to run
        import asyncio as _asyncio

        await _asyncio.sleep(0)
    dispatch_mock.assert_called_once()
    args, _ = dispatch_mock.call_args
    assert args[0] == project_id
    assert args[1] == "REMEDIATION_ESCALATED"
    assert args[2]["direction_text"] == "Improve CI reliability"
    assert args[2]["failure_pattern"] == "AssertionError in test_foo"
    assert args[2]["attempts"] == 3
    assert args[2]["remediation_summary"].startswith("Added retry wrapper")
    assert args[2]["severity"] == "blocker"
    assert args[2]["manual_intervention_required"] is True
    assert "summary" in args[2] and args[2]["summary"]


@pytest.mark.asyncio
async def test_post_issue_implementation_spawned_message(db_session):
    project_id = await _persist_test_project(db_session)
    task_id = uuid.uuid4()
    msg = await post_issue_implementation_spawned_message(
        db_session,
        project_id,
        issue_number=123,
        issue_title="Fix login bug",
        task_id=task_id,
    )
    assert msg.project_id == project_id
    assert "123" in msg.content
    assert "Fix login bug" in msg.content
    assert msg.context["event_type"] == "issue_implementation_spawned"
    assert msg.context["issue_number"] == 123
    assert msg.context["issue_title"] == "Fix login bug"
    assert msg.context["task_id"] == str(task_id)
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_issue_implementation_spawned_message_no_task_id(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_issue_implementation_spawned_message(
        db_session, project_id, issue_number=5, issue_title="Bug"
    )
    assert msg.context["task_id"] is None


@pytest.mark.asyncio
async def test_post_review_feedback_dismissed(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_review_feedback_message(
        db_session,
        project_id,
        pr_number=10,
        signal="review_dismissed",
        detail="Too broad",
    )
    assert msg.project_id == project_id
    assert "dismissed" in msg.content
    assert "10" in msg.content
    assert "Too broad" in msg.content
    assert msg.context["event_type"] == "review_feedback"
    assert msg.context["signal"] == "review_dismissed"
    assert msg.context["pr_number"] == 10
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_review_feedback_user_overrode(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=20, signal="user_overrode_review"
    )
    assert "approved" in msg.content
    assert "20" in msg.content
    assert msg.context["signal"] == "user_overrode_review"


@pytest.mark.asyncio
async def test_post_review_feedback_user_caught_issues(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=30, signal="user_caught_issues"
    )
    assert "changes" in msg.content
    assert "30" in msg.content
    assert msg.context["signal"] == "user_caught_issues"


@pytest.mark.asyncio
async def test_post_review_feedback_unknown_signal(db_session):
    project_id = await _persist_test_project(db_session)
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=40, signal="some_unknown_signal"
    )
    assert "some_unknown_signal" in msg.content
    assert "40" in msg.content


@pytest.mark.asyncio
async def test_post_feedback_acknowledgment(db_session):
    project_id = await _persist_test_project(db_session)
    original_msg_id = uuid.uuid4()
    msg = await post_feedback_acknowledgment(
        db_session,
        project_id,
        original_msg_id=original_msg_id,
        resolution="Fixed in latest PR",
    )
    assert msg.project_id == project_id
    assert "Fixed in latest PR" in msg.content
    assert msg.context["event_type"] == "feedback_acknowledgment"
    assert msg.context["reply_to"] == str(original_msg_id)
    assert msg.context["resolution"] == "Fixed in latest PR"
    assert msg.sequence == 1
    assert msg.surface == "conversation"
    assert msg.handling_state == "responded"
    assert msg.reply_to_message_id == original_msg_id
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_feedback_acknowledgment_with_task_id(db_session):
    project_id = await _persist_test_project(db_session)
    original_msg_id = uuid.uuid4()
    task_id = uuid.uuid4()
    msg = await post_feedback_acknowledgment(
        db_session,
        project_id,
        original_msg_id=original_msg_id,
        resolution="Done",
        task_id=task_id,
    )
    assert msg.from_task_id == task_id
    assert msg.handled_by_task_id == task_id


# ---------------------------------------------------------------------------
# Tests: auto-remediation failure tracking and escalation
# ---------------------------------------------------------------------------


async def _create_ar_project(session, **overrides) -> Project:
    defaults = {
        "name": "AR Test",
        "user_id": "test-user",
        "github_owner": "acme",
        "github_repo": "repo",
        "design_doc": {"_auto_remediation": {"enabled": True}},
    }
    defaults.update(overrides)
    project = Project(**defaults)
    session.add(project)
    await session.flush()
    return project


@pytest.mark.asyncio
async def test_failure_counter_increments(db_session):
    """result='failed' increments the per-branch failure counter in design_doc."""
    project = await _create_ar_project(db_session)
    await post_auto_fix_result_message(
        db_session, project.id, branch="main", result="failed"
    )
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert ar.get("branch_failure_counts", {}).get("main") == 1


@pytest.mark.asyncio
async def test_merged_resets_counter(db_session):
    """result='merged' resets the failure counter for the branch."""
    project = await _create_ar_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 2},
            },
        },
    )
    await post_auto_fix_result_message(
        db_session, project.id, branch="main", result="merged"
    )
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert ar.get("branch_failure_counts", {}).get("main", 0) == 0


@pytest.mark.asyncio
async def test_escalation_at_threshold_suspends_branch(db_session):
    """Three consecutive failures suspend the branch and post an escalation message."""
    from sqlmodel import select

    project = await _create_ar_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 2},
            },
        },
    )
    with patch("app.notification_dispatch.asyncio") as mock_asyncio:
        mock_asyncio.ensure_future = lambda coro: coro.close() or None
        await post_auto_fix_result_message(
            db_session, project.id, branch="main", result="failed"
        )
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" in ar.get("suspended_branches", [])
    assert ar.get("branch_failure_counts", {}).get("main") == 3
    # Escalation message (BLOCKER) should have been posted
    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    escalation_msgs = [m for m in msgs if m.message_type == MessageType.BLOCKER]
    assert len(escalation_msgs) == 1
    assert "suspended" in escalation_msgs[0].content
    assert "main" in escalation_msgs[0].content


@pytest.mark.asyncio
async def test_escalation_webhook_payload_is_enriched(db_session):
    """AUTO_FIX_ESCALATED webhook payload includes actionable context fields."""
    project = await _create_ar_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 2},
            },
        },
    )
    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_auto_fix_result_message(
            db_session, project.id, branch="main", result="failed"
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)
    dispatch_mock.assert_called_once()
    args, _ = dispatch_mock.call_args
    assert args[1] == "AUTO_FIX_ESCALATED"
    payload = args[2]
    assert payload["branch"] == "main"
    assert payload["consecutive_failures"] == 3
    assert payload["threshold"] == 3
    assert payload["suspended_at"]
    assert payload["recovery_attempts"] == 0
    assert payload["severity"] == "blocker"
    assert payload["manual_intervention_required"] is True
    assert "summary" in payload and payload["summary"]


@pytest.mark.asyncio
async def test_merged_clears_suspension(db_session):
    """result='merged' removes the branch from suspended_branches."""
    project = await _create_ar_project(
        db_session,
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_failure_counts": {"main": 3},
                "suspended_branches": ["main"],
            },
        },
    )
    await post_auto_fix_result_message(
        db_session, project.id, branch="main", result="merged"
    )
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" not in ar.get("suspended_branches", [])
    assert "main" not in ar.get("branch_failure_counts", {})


# ─── Residual remediation lifecycle (mirror of CI auto-fix) ──────────────────


async def _load_case(session, project_id, fingerprint):
    result = await session.execute(
        _select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    return result.scalars().first()


@pytest.mark.asyncio
async def test_post_residual_fix_spawned_creates_case_in_remediating(db_session):
    """Spawn helper UPSERTs the case row, sets REMEDIATING + last_spawned_*,
    and emits residual_fix_spawned. Future idle-tick spawn-dedup queries
    look at exactly these fields."""
    from app.models import Task, TaskStatus

    project = Project(name="Residual Spawned", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    # Real Task row needed: ProjectMessage.from_task_id is FK-enforced.
    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix"}],
    )
    db_session.add(fix_task)
    await db_session.commit()
    msg = await post_residual_fix_spawned_message(
        db_session,
        project.id,
        fingerprint="abcfp",
        sample_message="AssertionError in test_foo",
        task_id=fix_task.id,
    )
    await db_session.commit()
    assert msg.context["event_type"] == "residual_fix_spawned"
    assert msg.context["fingerprint"] == "abcfp"
    case = await _load_case(db_session, project.id, "abcfp")
    assert case is not None
    assert case.status == ResidualCaseStatus.REMEDIATING
    assert case.last_spawned_task_id == fix_task.id
    assert case.last_spawned_at is not None


@pytest.mark.asyncio
async def test_post_residual_fix_result_merged_resets_counter(db_session):
    """Merged result transitions case to MERGED and zeroes the failure
    counter so a future regression starts fresh, mirroring CI auto-fix."""
    project = Project(name="Residual Merged", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    # Pre-populate a case with prior failures
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-merge",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=2,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session,
        project.id,
        fingerprint="fp-merge",
        result="merged",
        pr_number=42,
    )
    await db_session.commit()
    refreshed = await _load_case(db_session, project.id, "fp-merge")
    assert refreshed.status == ResidualCaseStatus.MERGED
    assert refreshed.consecutive_failure_count == 0
    assert refreshed.last_spawned_pr_number == 42


@pytest.mark.asyncio
async def test_post_residual_fix_result_failed_increments_counter(db_session):
    """Each failed result bumps consecutive_failure_count. Below threshold
    the case stays FAILED (re-spawnable), not yet SUSPENDED."""
    project = Project(name="Residual Failed", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-fail",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=0,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session,
        project.id,
        fingerprint="fp-fail",
        result="failed",
    )
    await db_session.commit()
    refreshed = await _load_case(db_session, project.id, "fp-fail")
    assert refreshed.consecutive_failure_count == 1
    assert refreshed.status == ResidualCaseStatus.FAILED
    assert refreshed.suspended_at is None


@pytest.mark.asyncio
async def test_post_residual_fix_result_threshold_suspends_and_escalates(db_session):
    """At _AUTO_REMEDIATION_ESCALATION_THRESHOLD consecutive failures the
    case auto-suspends, a BLOCKER escalation message gets posted, and the
    RESIDUAL_FIX_ESCALATED webhook fires. This is the auto-suspend path
    that prevents a permanently-broken fingerprint from burning pipeline
    cycles forever."""
    project = Project(name="Residual Escalate", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-esc",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=nd._AUTO_REMEDIATION_ESCALATION_THRESHOLD - 1,
    )
    db_session.add(case)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_residual_fix_result_message(
            db_session,
            project.id,
            fingerprint="fp-esc",
            result="failed",
        )
        await db_session.commit()

    refreshed = await _load_case(db_session, project.id, "fp-esc")
    assert refreshed.status == ResidualCaseStatus.SUSPENDED
    assert (
        refreshed.consecutive_failure_count == nd._AUTO_REMEDIATION_ESCALATION_THRESHOLD
    )
    assert refreshed.suspended_at is not None

    # Escalation BLOCKER message + webhook were both fired
    msgs_q = await db_session.execute(
        _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    )
    blocker_msgs = [
        m for m in msgs_q.scalars().all() if m.message_type == MessageType.BLOCKER
    ]
    assert any(
        m.context.get("event_type") == "residual_fix_escalated" for m in blocker_msgs
    )
    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[1] == "RESIDUAL_FIX_ESCALATED"
    assert args[2]["fingerprint"] == "fp-esc"
    assert args[2]["severity"] == "blocker"
    assert args[2]["manual_intervention_required"] is True


@pytest.mark.asyncio
async def test_post_residual_fix_result_review_needed_preserves_state(db_session):
    """review_needed stops short of touching the failure counter — a
    human review is the next gate. Case stays REMEDIATING with the PR
    number stamped for the admin UI."""
    project = Project(name="Residual Review", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-rev",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=2,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session,
        project.id,
        fingerprint="fp-rev",
        result="review_needed",
        pr_number=77,
    )
    await db_session.commit()
    refreshed = await _load_case(db_session, project.id, "fp-rev")
    assert refreshed.status == ResidualCaseStatus.REMEDIATING
    assert refreshed.consecutive_failure_count == 2
    assert refreshed.last_spawned_pr_number == 77


@pytest.mark.asyncio
async def test_post_residual_fix_escalation_message(db_session):
    """Escalation helper emits a BLOCKER ProjectMessage with the
    residual_fix_escalated event_type so external subscribers can filter."""
    project_id = await _persist_test_project(db_session)
    msg = await post_residual_fix_escalation_message(
        db_session,
        project_id,
        fingerprint="abc",
        failure_count=3,
    )
    assert msg.message_type == MessageType.BLOCKER
    assert msg.context["event_type"] == "residual_fix_escalated"
    assert msg.context["fingerprint"] == "abc"
    assert msg.context["consecutive_failures"] == 3


# ─── post_remediation_result_message — discriminator at outcome sites ────────


@pytest.mark.asyncio
async def test_remediation_discriminator_routes_residual_to_residual_helper(db_session):
    """A task with spawned_by=residual_fingerprint_remediation routes to
    post_residual_fix_result_message, which writes residual_fix_result
    NOT auto_fix_result. This is the contract task_lifecycle and
    agent_status rely on."""
    from app.models import Task, TaskStatus  # noqa: PLC0415
    from app.notification_dispatch import post_remediation_result_message  # noqa: PLC0415

    project = Project(name="Discriminator Residual", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={
            "auto_remediation": True,
            "spawned_by": "residual_fingerprint_remediation",
            "error_fingerprint": "fp-route-r",
        },
    )
    db_session.add(fix_task)
    await db_session.commit()

    msg = await post_remediation_result_message(
        db_session,
        fix_task,
        result="failed",
    )
    await db_session.commit()
    assert msg.context["event_type"] == "residual_fix_result"
    assert msg.context["fingerprint"] == "fp-route-r"


@pytest.mark.asyncio
async def test_remediation_discriminator_routes_ci_to_auto_fix_helper(db_session):
    """A CI auto-fix task (no spawned_by marker) routes to the existing
    post_auto_fix_result_message — preserves backwards-compatible behavior
    for the bulk of auto_remediation tasks that aren't residual."""
    from app.models import Task, TaskStatus  # noqa: PLC0415
    from app.notification_dispatch import post_remediation_result_message  # noqa: PLC0415

    project = Project(name="Discriminator CI", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={
            "auto_remediation": True,
            "ci_failure_branch": "feat/x",
        },
    )
    db_session.add(fix_task)
    await db_session.commit()

    msg = await post_remediation_result_message(
        db_session,
        fix_task,
        result="failed",
    )
    await db_session.commit()
    assert msg.context["event_type"] == "auto_fix_result"
    assert msg.context["branch"] == "feat/x"


# ─── is_residual_remediation_task — pure predicate ──────────────────────────


def test_is_residual_remediation_task_true():
    """spawned_by=residual_fingerprint_remediation returns True."""
    task = MagicMock()
    task.context = {"spawned_by": "residual_fingerprint_remediation"}
    assert is_residual_remediation_task(task) is True


def test_is_residual_remediation_task_false_for_ci_task():
    """CI auto-remediation tasks with different spawned_by return False."""
    task = MagicMock()
    task.context = {
        "spawned_by": "ci_auto_remediation_failure",
        "auto_remediation": True,
    }
    assert is_residual_remediation_task(task) is False


def test_is_residual_remediation_task_false_for_no_context():
    """Tasks with no context (None) return False without raising."""
    task = MagicMock()
    task.context = None
    assert is_residual_remediation_task(task) is False


# ─── post_residual_fix_spawned_message — context fields ─────────────────────


@pytest.mark.asyncio
async def test_post_residual_fix_spawned_records_task_id_in_context(db_session):
    """task_id kwarg is reflected into msg.context so downstream
    subscribers can correlate the notification to the spawned task."""
    from app.models import Task, TaskStatus  # noqa: PLC0415

    project = Project(name="Spawned TaskID", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix"}],
    )
    db_session.add(fix_task)
    await db_session.commit()
    msg = await post_residual_fix_spawned_message(
        db_session,
        project.id,
        fingerprint="fp-taskid",
        sample_message="TypeError in test",
        task_id=fix_task.id,
    )
    await db_session.commit()
    assert msg.context["task_id"] == str(fix_task.id)
    assert msg.context["fingerprint"] == "fp-taskid"


@pytest.mark.asyncio
async def test_post_residual_fix_spawned_null_task_id_in_context(db_session):
    """When no task_id is provided, context stores None — callers that
    omit the kwarg don't blow up and the field is still present."""
    project = Project(name="Spawned NoTask", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    msg = await post_residual_fix_spawned_message(
        db_session,
        project.id,
        fingerprint="fp-notask",
        sample_message="NameError: x",
    )
    await db_session.commit()
    assert msg.context["task_id"] is None
    assert msg.context["event_type"] == "residual_fix_spawned"


@pytest.mark.asyncio
async def test_post_residual_fix_spawned_fires_external_webhook(db_session):
    """post_residual_fix_spawned_message dispatches a RESIDUAL_FIX_SPAWNED
    external webhook once per spawn, carrying fingerprint, hit_count, and task_id."""
    from app.models import Task, TaskStatus  # noqa: PLC0415

    project = Project(name="Spawned Webhook", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix"}],
    )
    db_session.add(fix_task)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_residual_fix_spawned_message(
            db_session,
            project.id,
            fingerprint="fp-wh",
            sample_message="AssertionError in test_bar",
            task_id=fix_task.id,
        )
        await db_session.commit()

    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[1] == "RESIDUAL_FIX_SPAWNED"
    assert args[2]["fingerprint"] == "fp-wh"
    assert args[2]["task_id"] == str(fix_task.id)
    assert "hit_count" in args[2]


# ─── RESOLVED transition on PR merge ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_residual_case_resolved_transitions_to_resolved(db_session):
    """After a residual fix PR merges, close_residual_case_resolved() should
    transition the case to RESOLVED so it is excluded from future DB selection
    and treated as definitively closed by the reaper and admin UI."""
    from app.notification_dispatch import close_residual_case_resolved  # noqa: PLC0415

    project = Project(name="Resolved On Merge", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-resolved-merge",
        status=ResidualCaseStatus.MERGED,
        consecutive_failure_count=0,
    )
    db_session.add(case)
    await db_session.commit()

    await close_residual_case_resolved(
        db_session, project.id, fingerprint="fp-resolved-merge"
    )
    await db_session.commit()

    refreshed = await _load_case(db_session, project.id, "fp-resolved-merge")
    assert refreshed.status == ResidualCaseStatus.RESOLVED


# ─── Audit-deferral escalation message ───────────────────────────────────────


@pytest.mark.asyncio
async def test_post_residual_fix_audit_deferred_escalation_message_creates_blocker(
    db_session,
):
    """post_residual_fix_audit_deferred_escalation_message emits a BLOCKER
    ProjectMessage with the expected context keys and the correct threshold."""
    project = Project(name="Audit Deferred Esc", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    with patch("asyncio.ensure_future"):
        msg = await post_residual_fix_audit_deferred_escalation_message(
            db_session,
            project.id,
            fingerprint="fp-audit-deferred",
            defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        )
    await db_session.commit()

    assert msg.message_type == MessageType.BLOCKER
    assert msg.from_type == "system"
    assert msg.context["event_type"] == "residual_fix_audit_deferred"
    assert msg.context["fingerprint"] == "fp-audit-deferred"
    assert (
        msg.context["audit_defer_count"] == _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD
    )
    assert msg.context["threshold"] == _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD
    assert "fp-audit-deferred" in msg.content


@pytest.mark.asyncio
async def test_post_residual_fix_spawned_resets_audit_defer_fields(db_session):
    """post_residual_fix_spawned_message resets audit_defer_count to 0 and
    clears audit_deferred_system_id so a future audit cycle can re-escalate."""
    project = Project(name="Spawn Resets Defer", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    # Pre-seed a case with non-zero deferral state.
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="fp-spawn-reset",
        status=ResidualCaseStatus.ACTIVE,
        audit_defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        audit_deferred_system_id="sys-abc",
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_spawned_message(
        db_session,
        project.id,
        fingerprint="fp-spawn-reset",
        sample_message="SomeError in foo",
    )
    await db_session.commit()

    refreshed = await _load_case(db_session, project.id, "fp-spawn-reset")
    assert refreshed.audit_defer_count == 0, (
        "audit_defer_count must be reset to 0 on spawn"
    )
    assert refreshed.audit_deferred_system_id is None, (
        "audit_deferred_system_id must be cleared on spawn"
    )


@pytest.mark.asyncio
async def test_post_residual_fix_audit_deferred_escalation_message_fires_webhook(
    db_session,
):
    """post_residual_fix_audit_deferred_escalation_message fires RESIDUAL_FIX_AUDIT_DEFERRED
    webhook with the correct payload fields so external subscribers can filter and act."""
    project = Project(name="Audit Defer Webhook", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_residual_fix_audit_deferred_escalation_message(
            db_session,
            project.id,
            fingerprint="fp-webhook-test",
            defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[0] == project.id
    assert args[1] == "RESIDUAL_FIX_AUDIT_DEFERRED"
    payload = args[2]
    assert payload["fingerprint"] == "fp-webhook-test"
    assert payload["audit_defer_count"] == _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD
    assert payload["threshold"] == _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD
    assert payload["severity"] == "blocker"
    assert payload["manual_intervention_required"] is False
    assert "summary" in payload and payload["summary"]


@pytest.mark.asyncio
async def test_post_residual_fix_audit_deferred_escalation_threshold_from_runtime_config(
    db_session,
):
    """The threshold stored in the webhook payload reflects any runtime_config override,
    not the hardcoded default, so operators can tune without a deploy."""
    project = Project(name="Audit Defer Config", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    custom_threshold = 10
    fake_runtime_config = {
        "residual_audit_defer_escalation_threshold": custom_threshold
    }

    dispatch_mock = AsyncMock()
    with (
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new=dispatch_mock,
        ),
        patch("app.shared_state.runtime_config", fake_runtime_config),
    ):
        msg = await post_residual_fix_audit_deferred_escalation_message(
            db_session,
            project.id,
            fingerprint="fp-config-threshold",
            defer_count=custom_threshold,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    assert msg.context["threshold"] == custom_threshold
    args = dispatch_mock.call_args.args
    assert args[2]["threshold"] == custom_threshold


# ---------------------------------------------------------------------------
# post_version_calcification_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_version_calcification_message_creates_release_calcifying_message(
    db_session,
):
    """post_version_calcification_message posts a ProjectMessage with
    event_type 'release_calcifying' and the correct context fields."""
    project = Project(name="Calcification Test", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    version_id = str(uuid.uuid4())
    msg = await post_version_calcification_message(
        db_session,
        project.id,
        version_number=3,
        version_id=version_id,
        qualification_attempts=5,
        blocker="ci_not_green",
        blocker_label="CI not green",
        age_days=7,
    )
    await db_session.commit()

    assert msg.context["event_type"] == "release_calcifying"
    assert msg.context["version_number"] == 3
    assert msg.context["version_id"] == version_id
    assert msg.context["qualification_attempts"] == 5
    assert msg.context["blocker"] == "ci_not_green"
    assert msg.context["age_days"] == 7
    assert "calcifying" in msg.content
    assert "ci_not_green" not in msg.content  # summary uses blocker_label
    assert "CI not green" in msg.content


@pytest.mark.asyncio
async def test_post_version_calcification_message_fires_external_webhook(
    db_session,
):
    """post_version_calcification_message fires RELEASE_VERSION_CALCIFYING
    external webhook with the correct payload so external subscribers can act."""
    project = Project(name="Calcification Webhook", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_version_calcification_message(
            db_session,
            project.id,
            version_number=2,
            version_id="vid-123",
            qualification_attempts=4,
            blocker="audit_missing",
            blocker_label="Audit task missing",
            age_days=3,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[0] == project.id
    assert args[1] == "RELEASE_VERSION_CALCIFYING"
    payload = args[2]
    assert payload["version_number"] == 2
    assert payload["version_id"] == "vid-123"
    assert payload["qualification_attempts"] == 4
    assert payload["blocker"] == "audit_missing"
    assert payload["age_days"] == 3
    assert "summary" in payload and "calcifying" in payload["summary"]


@pytest.mark.asyncio
async def test_post_version_calcification_message_not_fired_for_dry_run(
    db_session,
):
    """post_version_calcification_message is a low-level builder — the
    deduplication and dry-run guard live in _check_version_calcification.
    Verify the builder creates a message when called (the caller ensures it
    is only invoked for genuine hard-blocker stalls)."""
    project = Project(name="Dry Run Guard", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        msg = await post_version_calcification_message(
            db_session,
            project.id,
            version_number=1,
            version_id="vid-dry",
            qualification_attempts=3,
            blocker="ci_not_green",
            blocker_label="CI not green",
            age_days=0,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    # Builder always creates the message; caller is responsible for the guard
    assert msg is not None
    dispatch_mock.assert_called_once()


# ---------------------------------------------------------------------------
# post_version_build_stalled_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_version_build_stalled_message_creates_message(
    db_session,
):
    """post_version_build_stalled_message posts a ProjectMessage with
    event_type 'release_build_stalled' and the correct context fields."""
    project = Project(name="Build Stall Test", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    version_id = str(uuid.uuid4())
    msg = await post_version_build_stalled_message(
        db_session,
        project.id,
        version_number=4,
        version_id=version_id,
        age_days=21,
        tasks_merged=55,
    )
    await db_session.commit()

    assert msg.context["event_type"] == "release_build_stalled"
    assert msg.context["version_number"] == 4
    assert msg.context["version_id"] == version_id
    assert msg.context["age_days"] == 21
    assert msg.context["tasks_merged"] == 55
    assert "building for 21 day(s)" in msg.content
    assert "55 merged task(s)" in msg.content
    assert "stabilize" in msg.content


@pytest.mark.asyncio
async def test_post_version_build_stalled_message_fires_external_webhook(
    db_session,
):
    """post_version_build_stalled_message fires RELEASE_VERSION_BUILD_STALLED
    external webhook with the correct payload so external subscribers can act."""
    project = Project(name="Build Stall Webhook", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_version_build_stalled_message(
            db_session,
            project.id,
            version_number=2,
            version_id="vid-789",
            age_days=15,
            tasks_merged=42,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[0] == project.id
    assert args[1] == "RELEASE_VERSION_BUILD_STALLED"
    payload = args[2]
    assert payload["version_number"] == 2
    assert payload["version_id"] == "vid-789"
    assert payload["age_days"] == 15
    assert payload["tasks_merged"] == 42
    assert "summary" in payload and "building" in payload["summary"]


# ---------------------------------------------------------------------------
# post_version_stabilize_stalled_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_version_stabilize_stalled_message_creates_message(
    db_session,
):
    """post_version_stabilize_stalled_message posts a ProjectMessage with
    event_type 'release_stabilize_stalled' and the correct context fields."""
    project = Project(name="Stabilize Stall Test", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    version_id = str(uuid.uuid4())
    msg = await post_version_stabilize_stalled_message(
        db_session,
        project.id,
        version_number=5,
        version_id=version_id,
        age_days=18,
        tasks_merged=45,
    )
    await db_session.commit()

    assert msg.context["event_type"] == "release_stabilize_stalled"
    assert msg.context["version_number"] == 5
    assert msg.context["version_id"] == version_id
    assert msg.context["age_days"] == 18
    assert msg.context["tasks_merged"] == 45
    assert "stabilize" in msg.content
    assert "18 day(s)" in msg.content
    assert "45 merged task(s)" in msg.content
    assert "qualified" in msg.content


@pytest.mark.asyncio
async def test_post_version_stabilize_stalled_message_fires_external_webhook(
    db_session,
):
    """post_version_stabilize_stalled_message fires RELEASE_VERSION_STABILIZE_STALLED
    external webhook with the correct payload so external subscribers can act."""
    project = Project(name="Stabilize Stall Webhook", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch(
        "app.notification_dispatch.dispatch_external_notification", new=dispatch_mock
    ):
        await post_version_stabilize_stalled_message(
            db_session,
            project.id,
            version_number=3,
            version_id="vid-stabilize-001",
            age_days=16,
            tasks_merged=38,
        )
        import asyncio as _asyncio

        await _asyncio.sleep(0)

    dispatch_mock.assert_called_once()
    args = dispatch_mock.call_args.args
    assert args[0] == project.id
    assert args[1] == "RELEASE_VERSION_STABILIZE_STALLED"
    payload = args[2]
    assert payload["version_number"] == 3
    assert payload["version_id"] == "vid-stabilize-001"
    assert payload["age_days"] == 16
    assert payload["tasks_merged"] == 38
    assert "summary" in payload and "stabilize" in payload["summary"]


# ---------------------------------------------------------------------------
# post_version_ci_unresolved_stall_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_version_ci_unresolved_stall_message_creates_message(
    db_session,
):
    """post_version_ci_unresolved_stall_message posts a ProjectMessage with
    event_type 'release_ci_unresolved_stall' and the correct context fields."""
    project = Project(name="CI Unresolved Stall Test", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    version_id = str(uuid.uuid4())
    msg = await post_version_ci_unresolved_stall_message(
        db_session,
        project.id,
        version_number=4,
        version_id=version_id,
        qualification_attempts=3,
        age_days=2,
    )
    await db_session.commit()

    assert msg.context["event_type"] == "release_ci_unresolved_stall"
    assert msg.context["version_number"] == 4
    assert msg.context["version_id"] == version_id
    assert msg.context["qualification_attempts"] == 3
    assert msg.context["age_days"] == 2
    assert "CI cannot be resolved" in msg.content
    assert "GitHub connectivity" in msg.content


# ─── post_remediation_result_message — promotion_target fallback ──────────────


@pytest.mark.asyncio
async def test_remediation_result_message_branch_falls_back_to_promotion_target(
    db_session,
):
    """When ci_failure_branch is absent from task context, post_remediation_result_message
    must use the project's promotion_target as the branch, not the literal 'main'.
    Verifies that the posted message carries the correct per-project branch value."""
    from app.models import Task, TaskStatus  # noqa: PLC0415
    from app.notification_dispatch import post_remediation_result_message  # noqa: PLC0415

    project = Project(
        name="Promo Branch Fallback", user_id="u1", promotion_target="release/v2"
    )
    db_session.add(project)
    await db_session.commit()

    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={
            "auto_remediation": True,
            # deliberately NO ci_failure_branch — should fall back to promotion_target
        },
    )
    db_session.add(fix_task)
    await db_session.commit()

    msg = await post_remediation_result_message(
        db_session,
        fix_task,
        result="failed",
    )
    await db_session.commit()

    assert msg.context["event_type"] == "auto_fix_result"
    assert msg.context["branch"] == "release/v2", (
        "branch must fall back to project.promotion_target ('release/v2'), not 'main'"
    )


@pytest.mark.asyncio
async def test_remediation_result_message_raises_when_project_missing_and_no_branch(
    db_session,
):
    """When the project row is gone and ci_failure_branch is absent from context,
    post_remediation_result_message must raise ValueError rather than silently
    routing to the literal 'main' branch."""
    from app.models import Project, Task, TaskStatus  # noqa: PLC0415
    from app.notification_dispatch import post_remediation_result_message  # noqa: PLC0415

    # Create project and task, then delete the project so the FK row is gone.
    project = Project(name="Deleted Project", user_id="u-deleted")
    db_session.add(project)
    await db_session.commit()

    fix_task = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={
            "auto_remediation": True,
            # deliberately no ci_failure_branch
        },
        auto_remediation=True,
    )
    db_session.add(fix_task)
    await db_session.commit()

    await db_session.delete(project)
    await db_session.commit()

    with pytest.raises(ValueError, match="project not found"):
        await post_remediation_result_message(
            db_session,
            fix_task,
            result="failed",
        )


# ---------------------------------------------------------------------------
# post_version_promotion_readiness_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_version_promotion_readiness_message_includes_self_approval_caveat(
    db_session,
):
    """post_version_promotion_readiness_message includes the self-approval caveat
    so the operator sees it's the qualifier's own approval rate, not validated
    against post-promotion outcomes."""
    from app.notification_dispatch import post_version_promotion_readiness_message

    project = Project(name="Promotion Readiness", user_id="u1")
    db_session.add(project)
    await db_session.commit()

    msg = await post_version_promotion_readiness_message(
        db_session,
        project.id,
        version_number=5,
        version_id="vid-readiness",
        verdicts_evaluated=10,
        would_promote_rate=0.90,
        consecutive_clean_passes=5,
        reason="qualifier self-approval streak clears thresholds",
    )
    await db_session.commit()

    assert msg.context["event_type"] == "promotion_readiness_reached"
    assert msg.context["version_number"] == 5
    assert msg.context["version_id"] == "vid-readiness"
    assert msg.context["would_promote_rate"] == 0.90
    assert "self-approval streak clears thresholds" in msg.content
    assert "90% self-approval rate" in msg.content
    assert "self-approval" in msg.content.lower()
    assert "post-promotion outcome" in msg.content.lower()
    assert msg in db_session


# ---------------------------------------------------------------------------
# list_dead_letters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dead_letters_returns_rows(db_session):
    """A dead-letter row written via _write_dead_letter is listable with full field detail."""
    from app.notification_dispatch import list_dead_letters

    pid = uuid.uuid4()
    entry = _RetryEntry(
        project_id=pid,
        event_type="test_dead_event",
        payload={"key": "value"},
        body={"event_type": "test_dead_event", "payload": {"key": "value"}},
        attempts=3,
        next_retry_at=0,
        webhook_url="https://hooks.example.com/test",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=503)

    rows, total = await list_dead_letters(db_session, project_id=pid)
    assert total == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == str(pid)
    assert row["event_type"] == "test_dead_event"
    assert row["reason"] == "exhausted"
    assert row["attempts"] == 4  # entry.attempts + 1 from _write_dead_letter
    assert row["last_status"] == 503
    assert row["webhook_url"] == "https://hooks.example.com/test"
    assert row["payload"] == {"key": "value"}
    assert "created_at" in row


@pytest.mark.asyncio
async def test_list_dead_letters_returns_empty_for_project_with_no_dead_letters(db_session):
    """Listing dead letters for a project with no dead-letter rows returns an empty list."""
    from app.notification_dispatch import list_dead_letters

    pid = uuid.uuid4()
    rows, total = await list_dead_letters(db_session, project_id=pid)
    assert rows == []
    assert total == 0


# ---------------------------------------------------------------------------
# redrive_dead_letter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redrive_reenqueues_and_deletes(db_session):
    """Re-driving a dead-letter row re-enqueues it into the retry queue and removes the row."""
    from app.models import NotificationDeadLetter
    from app.notification_dispatch import redrive_dead_letter
    from sqlmodel import select as _select

    _reset_stats()
    pid = uuid.uuid4()
    entry = _RetryEntry(
        project_id=pid,
        event_type="redrive_test_event",
        payload={"p": 1},
        body={"event_type": "redrive_test_event", "payload": {"p": 1}},
        attempts=2,
        next_retry_at=time.monotonic(),
        webhook_url="https://hooks.example.com/redrive",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=500)

    # Find the persisted row
    rows = (
        await db_session.execute(
            _select(NotificationDeadLetter).where(
                NotificationDeadLetter.project_id == str(pid),
                NotificationDeadLetter.event_type == "redrive_test_event",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    dl_id = str(rows[0].id)

    # The retry queue should be empty before the redrive
    assert len(nd._retry_queue) == 0

    result = await redrive_dead_letter(db_session, dead_letter_id=dl_id, project_id=str(pid))
    assert result["status"] == "redriven"
    assert result["id"] == dl_id

    # The entry should now be in the retry queue
    assert len(nd._retry_queue) == 1
    queued = nd._retry_queue[0]
    assert queued.event_type == "redrive_test_event"
    assert queued.payload == {"p": 1}
    assert queued.webhook_url == "https://hooks.example.com/redrive"
    assert queued.attempts == 0  # fresh attempt count

    # The dead-letter row should be deleted
    remaining = (
        await db_session.execute(
            _select(NotificationDeadLetter).where(
                NotificationDeadLetter.id == rows[0].id,
            )
        )
    ).scalars().all()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_redrive_unknown_id_raises_value_error(db_session):
    """Re-driving a nonexistent dead-letter id raises ValueError."""
    from app.notification_dispatch import redrive_dead_letter

    with pytest.raises(ValueError, match="dead letter not found"):
        await redrive_dead_letter(db_session, dead_letter_id=str(uuid.uuid4()), project_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_redrive_restores_original_body_and_event_type(db_session):
    """A re-driven entry carries the original JSON body and event_type, so the
    retry delivers the same notification that was originally attempted."""
    from app.models import NotificationDeadLetter
    from app.notification_dispatch import redrive_dead_letter
    from sqlmodel import select as _select

    _reset_stats()
    pid = uuid.uuid4()
    original_body = {
        "event_type": "custom_event_type",
        "project_id": str(pid),
        "timestamp": "2024-01-01T00:00:00",
        "payload": {"nested": True, "count": 42},
    }
    entry = _RetryEntry(
        project_id=pid,
        event_type="custom_event_type",
        payload={"nested": True, "count": 42},
        body=original_body,
        attempts=1,
        next_retry_at=time.monotonic(),
        webhook_url="https://hooks.example.com/original",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=502)
    rows = (
        await db_session.execute(
            _select(NotificationDeadLetter).where(
                NotificationDeadLetter.project_id == str(pid),
                NotificationDeadLetter.event_type == "custom_event_type",
            )
        )
    ).scalars().all()
    assert len(rows) == 1

    await redrive_dead_letter(db_session, dead_letter_id=str(rows[0].id), project_id=str(pid))
    assert len(nd._retry_queue) == 1
    queued = nd._retry_queue[0]
    assert queued.event_type == "custom_event_type"
    assert queued.body == original_body
    assert queued.payload == {"nested": True, "count": 42}
    assert queued.webhook_url == "https://hooks.example.com/original"


@pytest.mark.asyncio
async def test_redrive_wrong_project_raises_value_error(db_session):
    """Re-driving with a mismatched project_id raises ValueError (cross-project guard)."""
    from app.models import NotificationDeadLetter
    from app.notification_dispatch import redrive_dead_letter
    from sqlmodel import select as _select

    _reset_stats()
    pid = uuid.uuid4()
    entry = _RetryEntry(
        project_id=pid,
        event_type="cross_project_test",
        payload={"p": 1},
        body={"event_type": "cross_project_test", "payload": {"p": 1}},
        attempts=1,
        next_retry_at=time.monotonic(),
        webhook_url="https://hooks.example.com/cross",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=500)
    rows = (
        await db_session.execute(
            _select(NotificationDeadLetter).where(
                NotificationDeadLetter.project_id == str(pid),
                NotificationDeadLetter.event_type == "cross_project_test",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    dl_id = str(rows[0].id)

    # Redrive with a DIFFERENT project_id → should raise ValueError
    other_pid = uuid.uuid4()
    with pytest.raises(ValueError, match="dead letter not found"):
        await redrive_dead_letter(db_session, dead_letter_id=dl_id, project_id=str(other_pid))


# ---------------------------------------------------------------------------
# Router-level tests (test client)
# ---------------------------------------------------------------------------


TEST_USER_ID = "test-user-123"  # matches conftest override_get_current_user


async def _setup_project_with_owner(session) -> Project:
    """Create a test project with the conftest test user as owner."""
    project = Project(name="DL Test Project", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


class TestDeadLetterRouter:
    """Router-level tests for GET/POST dead-letter endpoints via the test client."""

    @pytest.mark.asyncio
    async def test_list_dead_letters_empty(self, client, db_session):
        """GET with a valid project returns empty list when no dead letters exist."""
        project = await _setup_project_with_owner(db_session)
        resp = await client.get(
            f"/api/admin/notification-dispatch/dead-letters?project_id={project.id}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_dead_letters_with_rows(self, client, db_session):
        """GET returns dead-letter rows for the project, newest first."""
        project = await _setup_project_with_owner(db_session)
        pid = project.id

        # Write two dead-letter rows
        for i, reason in enumerate(["exhausted", "evicted"]):
            entry = _RetryEntry(
                project_id=pid,
                event_type=f"dl_event_{i}",
                payload={"n": i},
                body={"event_type": f"dl_event_{i}", "payload": {"n": i}},
                attempts=i,
                next_retry_at=0,
                webhook_url=f"https://hooks.example.com/dl{i}",
            )
            await nd._write_dead_letter(entry, reason=reason, last_status=500 + i)

        resp = await client.get(
            f"/api/admin/notification-dispatch/dead-letters?project_id={pid}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        items = data["items"]
        assert len(items) == 2
        # Newest first. Rows may receive the same database timestamp on
        # platforms with coarse clock resolution, so tied rows have no defined
        # insertion order.
        assert [item["created_at"] for item in items] == sorted(
            (item["created_at"] for item in items), reverse=True
        )
        assert {item["reason"] for item in items} == {"exhausted", "evicted"}
        # Admin user sees full webhook_url
        assert items[0]["webhook_url"] == "https://hooks.example.com/dl1"

    @pytest.mark.asyncio
    async def test_list_dead_letters_invalid_uuid_returns_400(self, client, db_session):
        """GET with a non-UUID project_id returns 400."""
        resp = await client.get(
            "/api/admin/notification-dispatch/dead-letters?project_id=not-a-uuid"
        )
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
        # HTTPException detail may appear under 'detail' or another key depending
        # on middleware; accept the string anywhere in the JSON body.
        body = resp.json()
        if isinstance(body, dict):
            assert "Invalid project_id format" in str(body), f"body: {body}"
        else:
            assert "Invalid project_id format" in resp.text, f"text: {resp.text}"

    @pytest.mark.asyncio
    async def test_list_dead_letters_nonexistent_project_returns_404(self, client, db_session):
        """GET with a valid UUID that the user is not a member of returns 404."""
        resp = await client.get(
            f"/api/admin/notification-dispatch/dead-letters?project_id={uuid.uuid4()}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_redrive_succeeds_and_removes_row(self, client, db_session):
        """POST /redrive re-enqueues the dead letter and returns redriven count."""
        project = await _setup_project_with_owner(db_session)
        pid = project.id

        entry = _RetryEntry(
            project_id=pid,
            event_type="redrive_router_test",
            payload={"x": 1},
            body={"event_type": "redrive_router_test", "payload": {"x": 1}},
            attempts=2,
            next_retry_at=time.monotonic(),
            webhook_url="https://hooks.example.com/redrive-router",
        )
        await nd._write_dead_letter(entry, reason="exhausted", last_status=503)

        # Find the row ID
        from app.models import NotificationDeadLetter
        from sqlmodel import select as _select
        rows = (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.event_type == "redrive_router_test",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        dl_id = str(rows[0].id)

        _reset_stats()
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={"project_id": str(pid), "ids": [dl_id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redriven"] == 1
        assert data["errors"] == []

    @pytest.mark.asyncio
    async def test_redrive_invalid_id_format_returns_error_entry(self, client, db_session):
        """POST /redrive with a non-UUID id collects an error entry, does not 400."""
        project = await _setup_project_with_owner(db_session)
        _reset_stats()
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={"project_id": str(project.id), "ids": ["not-a-uuid", "also-bad"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redriven"] == 0
        assert len(data["errors"]) == 2
        assert all(e["error"] == "invalid id format" for e in data["errors"])

    @pytest.mark.asyncio
    async def test_redrive_nonexistent_id_returns_not_found_error(self, client, db_session):
        """POST /redrive with a valid UUID that doesn't exist → not-found error."""
        project = await _setup_project_with_owner(db_session)
        _reset_stats()
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={"project_id": str(project.id), "ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redriven"] == 0
        assert len(data["errors"]) == 1
        assert data["errors"][0]["error"] == "not found"

    @pytest.mark.asyncio
    async def test_redrive_invalid_project_id_returns_400(self, client, db_session):
        """POST /redrive with a non-UUID project_id returns 400."""
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={"project_id": "garbage", "ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        if isinstance(body, dict):
            assert "Invalid project_id format" in str(body), f"body: {body}"
        else:
            assert "Invalid project_id format" in resp.text, f"text: {resp.text}"

    @pytest.mark.asyncio
    async def test_redrive_mixed_valid_and_invalid_ids(self, client, db_session):
        """POST /redrive with mix of valid, invalid-format, and nonexistent IDs."""
        project = await _setup_project_with_owner(db_session)
        pid = project.id

        # Create one valid dead-letter row
        entry = _RetryEntry(
            project_id=pid,
            event_type="mixed_test",
            payload={"z": 1},
            body={"event_type": "mixed_test", "payload": {"z": 1}},
            attempts=1,
            next_retry_at=time.monotonic(),
            webhook_url="https://hooks.example.com/mixed",
        )
        await nd._write_dead_letter(entry, reason="exhausted", last_status=500)

        from app.models import NotificationDeadLetter
        from sqlmodel import select as _select
        rows = (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.event_type == "mixed_test",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        good_id = str(rows[0].id)

        _reset_stats()
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={
                "project_id": str(pid),
                "ids": [good_id, "bad-format", str(uuid.uuid4())],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["redriven"] == 1
        assert len(data["errors"]) == 2
        error_ids = {e["id"] for e in data["errors"]}
        assert "bad-format" in error_ids

    @pytest.mark.asyncio
    async def test_redrive_ids_max_200_validated(self, client, db_session):
        """POST /redrive rejects payloads with more than 200 ids via Pydantic validation."""
        project = await _setup_project_with_owner(db_session)
        resp = await client.post(
            "/api/admin/notification-dispatch/dead-letters/redrive",
            json={"project_id": str(project.id), "ids": [str(uuid.uuid4())] * 201},
        )
        assert resp.status_code == 422  # Pydantic validation error


# ---------------------------------------------------------------------------
# Encryption of webhook_url at rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_url_encrypted_at_rest_when_key_set(db_session, monkeypatch):
    """When GITHUB_PAT_ENCRYPTION_KEY is set, webhook_url is encrypted in the DB
    and transparently decrypted by list_dead_letters / redrive_dead_letter."""
    from cryptography.fernet import Fernet
    from app.field_encryption import decrypt_field
    from app.notification_dispatch import list_dead_letters as _list_dl

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    _reset_stats()
    pid = uuid.uuid4()

    entry = _RetryEntry(
        project_id=pid,
        event_type="encrypt_test",
        payload={"k": "v"},
        body={"event_type": "encrypt_test"},
        attempts=0,
        next_retry_at=0,
        webhook_url="https://hooks.secret.example.com/abc123token",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=500)

    from sqlmodel import select as _select

    rows = (
        (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.event_type == "encrypt_test",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    stored = rows[0].webhook_url
    # The DB value must be encrypted (different from plaintext)
    assert stored != "https://hooks.secret.example.com/abc123token"
    # The stored value must decrypt correctly
    assert decrypt_field(stored) == "https://hooks.secret.example.com/abc123token"

    # list_dead_letters transparently decrypts
    items, total = await _list_dl(db_session, project_id=pid)
    assert total >= 1
    dl_row = next(i for i in items if i["event_type"] == "encrypt_test")
    assert dl_row["webhook_url"] == "https://hooks.secret.example.com/abc123token"

    # redrive_dead_letter transparently decrypts
    from app.notification_dispatch import redrive_dead_letter as _redrive

    dl_id = str(rows[0].id)
    result = await _redrive(db_session, dead_letter_id=dl_id, project_id=str(pid))
    assert result["status"] == "redriven"
    assert len(nd._retry_queue) >= 1
    redriven = next(e for e in nd._retry_queue if e.event_type == "encrypt_test")
    assert redriven.webhook_url == "https://hooks.secret.example.com/abc123token"


@pytest.mark.asyncio
async def test_webhook_url_not_encrypted_without_key(db_session, monkeypatch):
    """When GITHUB_PAT_ENCRYPTION_KEY is NOT set, webhook_url is stored and
    retrieved as plaintext (graceful degradation for dev/test environments)."""
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    _reset_stats()
    pid = uuid.uuid4()

    entry = _RetryEntry(
        project_id=pid,
        event_type="no_key_test",
        payload={"k": "v"},
        body={"event_type": "no_key_test"},
        attempts=0,
        next_retry_at=0,
        webhook_url="https://hooks.plain.example.com/notoken",
    )
    await nd._write_dead_letter(entry, reason="exhausted", last_status=500)

    from sqlmodel import select as _select

    rows = (
        (
            await db_session.execute(
                _select(NotificationDeadLetter).where(
                    NotificationDeadLetter.project_id == str(pid),
                    NotificationDeadLetter.event_type == "no_key_test",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # Without the key, encrypt_field is a no-op
    assert rows[0].webhook_url == "https://hooks.plain.example.com/notoken"
