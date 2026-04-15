"""Tests for notification_dispatch.py message builder functions."""
import uuid

import pytest

from app.notification_dispatch import (
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
from app.models import MessageType, ProjectMessage


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
