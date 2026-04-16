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
    post_auto_fix_result_message,
    post_auto_fix_spawned_message,
    post_cycle_summary_message,
    post_direction_milestone_message,
    post_event_message,
    post_failure_alert_message,
    post_feedback_acknowledgment,
    post_issue_implementation_spawned_message,
    post_pr_merged_message,
    post_review_feedback_message,
)
from app.models import MessageType, Project, ProjectMessage


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
    project_id = uuid.uuid4()
    msg = await post_auto_fix_result_message(
        db_session, project_id, branch="main", result="merged", pr_number=99
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
    from sqlmodel import select  # noqa: PLC0415
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
