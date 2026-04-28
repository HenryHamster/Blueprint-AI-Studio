"""Tests for notification_dispatch.py message builder functions."""
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.notification_dispatch as nd
from app.notification_dispatch import (
    _PERMANENT_FAILURE_CODES,
    _RETRY_DELAYS,
    _RetryEntry,
    _retry_queue,
    dispatch_external_notification,
    get_notification_stats,
    is_residual_remediation_task,
    post_auto_fix_result_message,
    post_auto_fix_spawned_message,
    post_cycle_summary_message,
    post_direction_milestone_message,
    post_event_message,
    post_failure_alert_message,
    post_feedback_acknowledgment,
    post_issue_implementation_spawned_message,
    post_pr_merged_message,
    post_remediation_escalation_message,
    post_residual_fix_escalation_message,
    post_residual_fix_result_message,
    post_residual_fix_spawned_message,
    post_review_feedback_message,
)
from app.models import (
    MessageType, Project, ProjectMessage,
    ResidualCaseStatus, ResidualRemediationCase,
)
from sqlmodel import select as _select


# ---------------------------------------------------------------------------
# Helpers for dispatch_external_notification tests
# ---------------------------------------------------------------------------

def _make_runtime_config(webhook_url: str = "https://example.com/hook", enabled_events=None, throttle_seconds: int = 0):
    return {
        "notification_config": {
            "webhook_url": webhook_url,
            "enabled_events": enabled_events if enabled_events is not None else ["test_event"],
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


# ---------------------------------------------------------------------------
# dispatch_external_notification — delivery stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_increments_dispatched_and_delivered_on_success():
    _reset_stats()
    mock_resp = MagicMock(status_code=200)
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=200)):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_delivered"] == 1
    assert stats["total_failed"] == 0
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatch_enqueues_retry_on_502():
    _reset_stats()
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=502)), \
         patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_delivered"] == 0
    assert stats["retry_queue_depth"] == 1
    entry = nd._retry_queue[0]
    assert entry.attempts == 0
    assert entry.event_type == "test_event"


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_404():
    _reset_stats()
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=404)), \
         patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {"k": "v"})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0
    assert "404" in stats["last_failure_reason"]


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_401():
    _reset_stats()
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=401)), \
         patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {})
    stats = get_notification_stats()
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatch_no_retry_on_403():
    _reset_stats()
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=403)), \
         patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {})
    stats = get_notification_stats()
    assert stats["total_failed"] == 1
    assert stats["retry_queue_depth"] == 0


@pytest.mark.asyncio
async def test_dispatch_enqueues_retry_on_network_error():
    _reset_stats()
    import httpx
    with patch("app.shared_state.runtime_config", _make_runtime_config()), \
         patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(side_effect=httpx.ConnectError("refused"))), \
         patch("app.notification_dispatch._drain_retry_queue", new=AsyncMock()):
        await dispatch_external_notification(uuid.uuid4(), "test_event", {})
    stats = get_notification_stats()
    assert stats["total_dispatched"] == 1
    assert stats["retry_queue_depth"] == 1


# ---------------------------------------------------------------------------
# Retry queue bounds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_queue_bounded_at_50():
    _reset_stats()
    # Fill queue past maxlen by injecting entries directly
    for i in range(60):
        nd._retry_queue.append(_RetryEntry(
            project_id=uuid.uuid4(),
            event_type="test_event",
            payload={},
            body={},
            attempts=0,
            next_retry_at=time.monotonic() + 9999,
        ))
    assert len(nd._retry_queue) == 50  # deque(maxlen=50) truncates oldest


# ---------------------------------------------------------------------------
# _drain_retry_queue — retry delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_retry_succeeds_and_updates_delivered():
    _reset_stats()
    nd._retry_queue.append(_RetryEntry(
        project_id=uuid.uuid4(),
        event_type="test_event",
        payload={},
        body={"event_type": "test_event"},
        attempts=0,
        next_retry_at=time.monotonic() - 1,  # due now
    ))
    with patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=200)):
        await nd._drain_retry_queue("https://example.com/hook")
    assert nd.total_delivered == 1
    assert len(nd._retry_queue) == 0


@pytest.mark.asyncio
async def test_drain_retry_re_enqueues_on_502():
    _reset_stats()
    nd._retry_queue.append(_RetryEntry(
        project_id=uuid.uuid4(),
        event_type="test_event",
        payload={},
        body={},
        attempts=0,
        next_retry_at=time.monotonic() - 1,
    ))
    with patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=502)):
        await nd._drain_retry_queue("https://example.com/hook")
    # Should be re-enqueued with attempts=1
    assert len(nd._retry_queue) == 1
    assert nd._retry_queue[0].attempts == 1
    assert nd.total_failed == 0


@pytest.mark.asyncio
async def test_drain_retry_exhausts_and_counts_failed():
    _reset_stats()
    nd._retry_queue.append(_RetryEntry(
        project_id=uuid.uuid4(),
        event_type="test_event",
        payload={},
        body={},
        attempts=len(_RETRY_DELAYS) - 1,  # last attempt
        next_retry_at=time.monotonic() - 1,
    ))
    with patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=503)):
        await nd._drain_retry_queue("https://example.com/hook")
    assert nd.total_failed == 1
    assert len(nd._retry_queue) == 0


@pytest.mark.asyncio
async def test_drain_skips_not_due_entries():
    _reset_stats()
    nd._retry_queue.append(_RetryEntry(
        project_id=uuid.uuid4(),
        event_type="test_event",
        payload={},
        body={},
        attempts=0,
        next_retry_at=time.monotonic() + 9999,  # not due
    ))
    with patch("app.notification_dispatch._attempt_delivery", new=AsyncMock(return_value=200)) as mock_del:
        await nd._drain_retry_queue("https://example.com/hook")
    mock_del.assert_not_called()
    assert len(nd._retry_queue) == 1  # still in queue


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
    project_id = uuid.uuid4()
    msg = await post_event_message(
        db_session, project_id, "test_event", "Test summary"
    )
    assert msg.project_id == project_id
    assert msg.content == "Test summary"
    assert msg.context["event_type"] == "test_event"
    assert msg.message_type == MessageType.NOTE
    assert msg.from_type == "system"
    assert msg.from_task_id is None
    # Should be added to session (not yet committed, but tracked)
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_event_message_with_context_and_task_id(db_session):
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    extra_ctx = {"key": "value", "num": 42}
    msg = await post_event_message(
        db_session, project_id, "my_event", "Summary", context=extra_ctx, task_id=task_id
    )
    assert msg.context["event_type"] == "my_event"
    assert msg.context["key"] == "value"
    assert msg.context["num"] == 42
    assert msg.from_task_id == task_id


@pytest.mark.asyncio
async def test_post_pr_merged_message(db_session):
    project_id = uuid.uuid4()
    msg = await post_pr_merged_message(
        db_session, project_id, pr_number=42, direction_text="Add dark mode", task_count=3
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
    project_id = uuid.uuid4()
    long_text = "x" * 200
    msg = await post_pr_merged_message(
        db_session, project_id, pr_number=1, direction_text=long_text, task_count=1
    )
    assert len(msg.context["direction_text"]) <= 120


@pytest.mark.asyncio
async def test_post_direction_milestone_message(db_session):
    project_id = uuid.uuid4()
    msg = await post_direction_milestone_message(
        db_session, project_id,
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
    project_id = uuid.uuid4()
    long_text = "y" * 200
    msg = await post_direction_milestone_message(
        db_session, project_id,
        direction_text=long_text,
        milestone="auto-paused",
        reason="reason",
    )
    assert len(msg.context["direction_text"]) <= 120


@pytest.mark.asyncio
async def test_post_direction_milestone_message_fires_webhook(db_session):
    """Creating the milestone message also fires DIRECTION_MILESTONE via the external webhook."""
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        await post_direction_milestone_message(
            db_session, project_id,
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
async def test_post_direction_milestone_message_adds_reason_code_and_evidence(db_session):
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    evidence = {"cost_per_merge": 12.5, "merge_count": 1}
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        msg = await post_direction_milestone_message(
            db_session, project_id,
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
async def test_post_direction_milestone_message_no_reason_code_for_non_paused(db_session):
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        msg = await post_direction_milestone_message(
            db_session, project_id,
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
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        msg = await post_direction_milestone_message(
            db_session, project_id,
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
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        msg = await post_direction_milestone_message(
            db_session, project_id,
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
    project_id = uuid.uuid4()
    msg = await post_cycle_summary_message(
        db_session, project_id,
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
    project_id = uuid.uuid4()
    msg = await post_failure_alert_message(
        db_session, project_id, description="Circuit breaker opened"
    )
    assert msg.project_id == project_id
    assert "Circuit breaker opened" in msg.content
    assert msg.context["event_type"] == "failure_alert"
    assert msg.context["description"] == "Circuit breaker opened"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_auto_fix_spawned_message_few_checks(db_session):
    project_id = uuid.uuid4()
    msg = await post_auto_fix_spawned_message(
        db_session, project_id,
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
    project_id = uuid.uuid4()
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
    project_id = uuid.uuid4()
    msg = await post_auto_fix_result_message(
        db_session, project_id, branch="main", result="failed"
    )
    assert "failed" in msg.content
    assert "manual intervention" in msg.content
    assert msg.context["result"] == "failed"


@pytest.mark.asyncio
async def test_post_auto_fix_result_review_needed(db_session):
    project_id = uuid.uuid4()
    msg = await post_auto_fix_result_message(
        db_session, project_id, branch="feat", result="review_needed", pr_number=55
    )
    assert "review" in msg.content
    assert "55" in msg.content
    assert msg.context["result"] == "review_needed"


@pytest.mark.asyncio
async def test_post_remediation_escalation_message(db_session):
    project_id = uuid.uuid4()
    msg = await post_remediation_escalation_message(
        db_session, project_id,
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
    project_id = uuid.uuid4()
    long_dir = "d" * 200
    long_pattern = "p" * 500
    long_summary = "s" * 500
    msg = await post_remediation_escalation_message(
        db_session, project_id,
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
    project_id = uuid.uuid4()
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        await post_remediation_escalation_message(
            db_session, project_id,
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
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    msg = await post_issue_implementation_spawned_message(
        db_session, project_id, issue_number=123, issue_title="Fix login bug", task_id=task_id
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
    project_id = uuid.uuid4()
    msg = await post_issue_implementation_spawned_message(
        db_session, project_id, issue_number=5, issue_title="Bug"
    )
    assert msg.context["task_id"] is None


@pytest.mark.asyncio
async def test_post_review_feedback_dismissed(db_session):
    project_id = uuid.uuid4()
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=10, signal="review_dismissed", detail="Too broad"
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
    project_id = uuid.uuid4()
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=20, signal="user_overrode_review"
    )
    assert "approved" in msg.content
    assert "20" in msg.content
    assert msg.context["signal"] == "user_overrode_review"


@pytest.mark.asyncio
async def test_post_review_feedback_user_caught_issues(db_session):
    project_id = uuid.uuid4()
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=30, signal="user_caught_issues"
    )
    assert "changes" in msg.content
    assert "30" in msg.content
    assert msg.context["signal"] == "user_caught_issues"


@pytest.mark.asyncio
async def test_post_review_feedback_unknown_signal(db_session):
    project_id = uuid.uuid4()
    msg = await post_review_feedback_message(
        db_session, project_id, pr_number=40, signal="some_unknown_signal"
    )
    assert "some_unknown_signal" in msg.content
    assert "40" in msg.content


@pytest.mark.asyncio
async def test_post_feedback_acknowledgment(db_session):
    project_id = uuid.uuid4()
    original_msg_id = uuid.uuid4()
    msg = await post_feedback_acknowledgment(
        db_session, project_id,
        original_msg_id=original_msg_id,
        resolution="Fixed in latest PR",
    )
    assert msg.project_id == project_id
    assert "Fixed in latest PR" in msg.content
    assert msg.context["event_type"] == "feedback_acknowledgment"
    assert msg.context["reply_to"] == str(original_msg_id)
    assert msg.context["resolution"] == "Fixed in latest PR"
    assert msg in db_session


@pytest.mark.asyncio
async def test_post_feedback_acknowledgment_with_task_id(db_session):
    project_id = uuid.uuid4()
    original_msg_id = uuid.uuid4()
    task_id = uuid.uuid4()
    msg = await post_feedback_acknowledgment(
        db_session, project_id,
        original_msg_id=original_msg_id,
        resolution="Done",
        task_id=task_id,
    )
    assert msg.from_task_id == task_id


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
    await post_auto_fix_result_message(db_session, project.id, branch="main", result="failed")
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert ar.get("branch_failure_counts", {}).get("main") == 1


@pytest.mark.asyncio
async def test_merged_resets_counter(db_session):
    """result='merged' resets the failure counter for the branch."""
    project = await _create_ar_project(db_session, design_doc={
        "_auto_remediation": {"enabled": True, "branch_failure_counts": {"main": 2}},
    })
    await post_auto_fix_result_message(db_session, project.id, branch="main", result="merged")
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert ar.get("branch_failure_counts", {}).get("main", 0) == 0


@pytest.mark.asyncio
async def test_escalation_at_threshold_suspends_branch(db_session):
    """Three consecutive failures suspend the branch and post an escalation message."""
    from sqlmodel import select
    project = await _create_ar_project(db_session, design_doc={
        "_auto_remediation": {"enabled": True, "branch_failure_counts": {"main": 2}},
    })
    with patch("app.notification_dispatch.asyncio") as mock_asyncio:
        mock_asyncio.ensure_future = lambda coro: coro.close() or None
        await post_auto_fix_result_message(db_session, project.id, branch="main", result="failed")
    await db_session.flush()
    await db_session.refresh(project)
    ar = (project.design_doc or {}).get("_auto_remediation", {})
    assert "main" in ar.get("suspended_branches", [])
    assert ar.get("branch_failure_counts", {}).get("main") == 3
    # Escalation message (BLOCKER) should have been posted
    msgs = (await db_session.execute(
        select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    )).scalars().all()
    escalation_msgs = [m for m in msgs if m.message_type == MessageType.BLOCKER]
    assert len(escalation_msgs) == 1
    assert "suspended" in escalation_msgs[0].content
    assert "main" in escalation_msgs[0].content


@pytest.mark.asyncio
async def test_escalation_webhook_payload_is_enriched(db_session):
    """AUTO_FIX_ESCALATED webhook payload includes actionable context fields."""
    project = await _create_ar_project(db_session, design_doc={
        "_auto_remediation": {"enabled": True, "branch_failure_counts": {"main": 2}},
    })
    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        await post_auto_fix_result_message(db_session, project.id, branch="main", result="failed")
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
    project = await _create_ar_project(db_session, design_doc={
        "_auto_remediation": {
            "enabled": True,
            "branch_failure_counts": {"main": 3},
            "suspended_branches": ["main"],
        },
    })
    await post_auto_fix_result_message(db_session, project.id, branch="main", result="merged")
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
        project_id=project.id, status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix"}],
    )
    db_session.add(fix_task)
    await db_session.commit()
    msg = await post_residual_fix_spawned_message(
        db_session, project.id, fingerprint="abcfp",
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
        project_id=project.id, fingerprint="fp-merge",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=2,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session, project.id, fingerprint="fp-merge",
        result="merged", pr_number=42,
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
        project_id=project.id, fingerprint="fp-fail",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=0,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session, project.id, fingerprint="fp-fail", result="failed",
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
        project_id=project.id, fingerprint="fp-esc",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=nd._AUTO_REMEDIATION_ESCALATION_THRESHOLD - 1,
    )
    db_session.add(case)
    await db_session.commit()

    dispatch_mock = AsyncMock()
    with patch("app.notification_dispatch.dispatch_external_notification", new=dispatch_mock):
        await post_residual_fix_result_message(
            db_session, project.id, fingerprint="fp-esc", result="failed",
        )
        await db_session.commit()

    refreshed = await _load_case(db_session, project.id, "fp-esc")
    assert refreshed.status == ResidualCaseStatus.SUSPENDED
    assert refreshed.consecutive_failure_count == nd._AUTO_REMEDIATION_ESCALATION_THRESHOLD
    assert refreshed.suspended_at is not None

    # Escalation BLOCKER message + webhook were both fired
    msgs_q = await db_session.execute(
        _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    )
    blocker_msgs = [m for m in msgs_q.scalars().all() if m.message_type == MessageType.BLOCKER]
    assert any(m.context.get("event_type") == "residual_fix_escalated" for m in blocker_msgs)
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
        project_id=project.id, fingerprint="fp-rev",
        status=ResidualCaseStatus.REMEDIATING,
        consecutive_failure_count=2,
    )
    db_session.add(case)
    await db_session.commit()

    await post_residual_fix_result_message(
        db_session, project.id, fingerprint="fp-rev",
        result="review_needed", pr_number=77,
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
    project_id = uuid.uuid4()
    msg = await post_residual_fix_escalation_message(
        db_session, project_id, fingerprint="abc", failure_count=3,
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
        project_id=project.id, status=TaskStatus.FAILED,
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
        db_session, fix_task, result="failed",
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
        project_id=project.id, status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={
            "auto_remediation": True,
            "ci_failure_branch": "feat/x",
        },
    )
    db_session.add(fix_task)
    await db_session.commit()

    msg = await post_remediation_result_message(
        db_session, fix_task, result="failed",
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
    task.context = {"spawned_by": "ci_auto_remediation_failure", "auto_remediation": True}
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
        project_id=project.id, status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix"}],
    )
    db_session.add(fix_task)
    await db_session.commit()
    msg = await post_residual_fix_spawned_message(
        db_session, project.id, fingerprint="fp-taskid",
        sample_message="TypeError in test", task_id=fix_task.id,
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
        db_session, project.id, fingerprint="fp-notask",
        sample_message="NameError: x",
    )
    await db_session.commit()
    assert msg.context["task_id"] is None
    assert msg.context["event_type"] == "residual_fix_spawned"
