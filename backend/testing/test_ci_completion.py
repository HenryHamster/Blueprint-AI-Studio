"""
Tests for _attempt_merge_with_retry_and_escalation in app/ci_completion.py.

Covers:
  - Retry with exponential backoff (poll source)
  - Retry skipped for webhook source
  - Rule-block keyword detection → immediate escalation
  - Transient merge failure counter and escalation at 10 failures
  - Already-merged race condition (PR closed by another task)
  - ci_orig_id status propagation on successful merge
"""
import uuid
import pytest

from app.models import Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(db_session, project, pr_number: int = 42, ci_loop_extra: dict | None = None):
    """Create a minimal EVALUATING task with a review_loop context."""
    loop = {
        "pr_number": pr_number,
        "branch": "blueprint/test-branch",
        "base_branch": "main",
        "iteration": 1,
        "review_approved": True,
    }
    if ci_loop_extra:
        loop.update(ci_loop_extra)
    task = Task(
        project_id=project.id,
        title="CI merge test task",
        payloads=[{"type": "review", "pr_number": pr_number, "branch": "blueprint/test-branch", "base_branch": "main"}],
        context={"review_loop": loop},
        vm_instance_id=f"agent-vm-ci-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


# ---------------------------------------------------------------------------
# 1. Retry with exponential backoff (source != 'webhook')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_retry_succeeds_on_second_attempt_poll(db_session, mocker):
    """
    When merge_pull_request returns merged=False on the first call and merged=True
    on the second, _attempt_merge_with_retry_and_escalation retries (source='poll')
    and returns True after the first retry.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    sleep_mock = mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)

    # First call returns not merged; second returns merged
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        side_effect=[
            {"merged": False, "message": "transient error"},
            {"merged": True},
        ],
    )

    project = Project(name="Retry poll test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "Should return True after successful retry"
    assert task.status == TaskStatus.COMPLETED
    assert merge_mock.call_count == 2, "Should have called merge twice (initial + 1 retry)"
    # sleep called once for attempt=0 → 5*(3^0)=5 seconds
    sleep_mock.assert_called_once_with(5)


@pytest.mark.asyncio
async def test_merge_retry_all_attempts_fail_poll(db_session, mocker):
    """
    When all 3 retries fail, the function increments merge_failures and returns False.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    # All 4 calls (initial + 3 retries) fail
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "transient error"},
    )

    project = Project(name="All retries fail poll", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False
    # initial call + 3 retry attempts = 4 total
    assert merge_mock.call_count == 4
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 1, "merge_failures should be incremented"


@pytest.mark.asyncio
async def test_merge_retry_uses_exponential_backoff_delays(db_session, mocker):
    """
    Retry delays follow 5*(3^attempt): 5s, 15s, 45s for attempts 0, 1, 2.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    sleep_mock = mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "transient"},
    )

    project = Project(name="Backoff delay test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # Verify sleep was called with 5, 15, 45
    calls = [c.args[0] for c in sleep_mock.call_args_list]
    assert calls == [5, 15, 45], f"Expected backoff delays [5, 15, 45], got {calls}"


# ---------------------------------------------------------------------------
# 2. Retry skipped for webhook source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_no_retry_for_webhook_source(db_session, mocker):
    """
    When source='webhook', merge failure must NOT trigger retries (to avoid
    blocking the webhook delivery thread). Only one merge call should be made.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    sleep_mock = mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "transient error"},
    )

    project = Project(name="Webhook no retry", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert merge_mock.call_count == 1, "Webhook source must not retry — exactly 1 merge call"
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_merge_conflict_skips_retries(db_session, mocker):
    """
    When merge returns conflict=True (409), the retry loop must break immediately.
    Conflicts cannot be resolved by retrying — only by code changes.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    sleep_mock = mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "conflict": True, "message": "merge conflict"},
    )

    project = Project(name="Conflict no retry", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False
    assert merge_mock.call_count == 1, "Conflict must not retry — exactly 1 merge call"
    sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Rule-block keyword detection → immediate escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_message", [
    "Repository rule violations: rule violation found",
    "Required review by branch protection not met",
    "required status: pending",
    "merge restrictions apply",
    "not allowed to merge",
    "at least 1 approving review required",
    "approving review from code owner required",
])
@pytest.mark.asyncio
async def test_rule_block_keywords_trigger_escalation(db_session, mocker, rule_message):
    """
    Each rule-block keyword triggers requires_approval=True + merge_rule_blocked flag,
    and does NOT increment merge_failures.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": rule_message},
    )

    project = Project(name=f"Rule block: {rule_message[:20]}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.requires_approval is True, f"requires_approval must be set for: {rule_message}"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True
    assert review_loop.get("merge_failures", 0) == 0, "rule blocks must not increment merge_failures"
    assert "manual intervention" in (task.latest_status_message or "")


# ---------------------------------------------------------------------------
# 4. Merge failure counter and escalation at 10 failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_failure_counter_increments(db_session, mocker):
    """
    Transient failures (non-rule-block) increment merge_failures and update
    ci_poll_interval without setting requires_approval or merge_rule_blocked.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Failure counter test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Start with merge_failures=4
    task = _make_task(db_session, project, ci_loop_extra={"merge_failures": 4})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 5, "merge_failures should have incremented from 4 to 5"
    assert review_loop.get("merge_rule_blocked") is None, "merge_rule_blocked must not be set"
    assert task.requires_approval is not True, "requires_approval must not be set for transient failure"
    # ci_poll_interval = min(30 * 5, 120) = 120
    assert review_loop.get("ci_poll_interval") == 120


@pytest.mark.asyncio
async def test_merge_failure_escalates_at_10_failures(db_session, mocker):
    """
    When merge_failures reaches 10, merge_escalated flag is set and a failure
    alert is created.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Escalation at 10", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Start with merge_failures=9, so this call pushes it to 10
    task = _make_task(db_session, project, ci_loop_extra={"merge_failures": 9})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 10
    assert review_loop.get("merge_escalated") is True, "merge_escalated must be set at 10 failures"
    alert_mock.assert_called_once()
    alert_msg = alert_mock.call_args[0][2]
    assert "merge failed" in alert_msg.lower() or "10" in alert_msg


@pytest.mark.asyncio
async def test_merge_failure_does_not_double_escalate(db_session, mocker):
    """
    If merge_escalated is already set, a subsequent failure must NOT create
    another alert.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="No double escalation", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Already escalated with 15 failures
    task = _make_task(db_session, project, ci_loop_extra={
        "merge_failures": 15,
        "merge_escalated": True,
    })
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_merge_failure_hard_cap_marks_failed(db_session, mocker):
    """
    When merge_failures crosses MERGE_FAILURE_HARD_CAP, the task is marked FAILED
    with requires_approval and a failure alert is created — preventing indefinite
    retry every 120 seconds.
    """
    from app.ci_completion import (
        MERGE_FAILURE_HARD_CAP,
        _attempt_merge_with_retry_and_escalation,
    )

    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Hard cap test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One below the cap so this call crosses it; merge_escalated already True
    # so we test that the cap fires independently of the 10-failure escalation.
    task = _make_task(db_session, project, ci_loop_extra={
        "merge_failures": MERGE_FAILURE_HARD_CAP - 1,
        "merge_escalated": True,
    })
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == MERGE_FAILURE_HARD_CAP
    alert_mock.assert_called_once()
    assert "hard-capped" in alert_mock.call_args[0][2].lower()


# ---------------------------------------------------------------------------
# 5. Already-merged race condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_merged_pr_completes_task(db_session, mocker):
    """
    When merge_pull_request fails but get_pull_request shows the PR is already
    merged, the task is completed successfully.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "conflict"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": True, "state": "closed"},
    )

    project = Project(name="Already merged race", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is True, "Should return True when PR was already merged"
    assert task.status == TaskStatus.COMPLETED
    assert "already merged" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_closed_pr_state_also_completes_task(db_session, mocker):
    """
    When get_pull_request shows state='closed' (even if merged=False),
    treat it as already handled and complete the task.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "conflict"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "closed"},
    )

    project = Project(name="Closed PR state", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is True, "Closed PR (state=closed) should be treated as already handled"
    assert task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 5b. mergeable_state is the primary signal for rule-block detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mergeable_state_blocked_escalates_without_keywords(db_session, mocker):
    """
    When get_pull_request reports mergeable_state='blocked', escalate as rule
    block even if the merge error message lacks the legacy keywords.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "blocked"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "some future GitHub error wording"},
    )

    project = Project(name="mergeable_state blocked", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.requires_approval is True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True
    assert review_loop.get("merge_failures", 0) == 0, "rule blocks must not increment merge_failures"


@pytest.mark.asyncio
async def test_mergeable_state_clean_does_not_escalate_on_keyword(db_session, mocker):
    """
    When mergeable_state is a known non-blocked value (e.g. 'clean'), do not
    escalate even if the error message happens to contain a legacy rule-block
    keyword — mergeable_state is authoritative.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "at least 1 approving review required"},
    )

    project = Project(name="mergeable_state clean", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.requires_approval is not True, "clean mergeable_state must not escalate"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is None
    assert review_loop.get("merge_failures") == 1, "treat as transient — counter should tick"


@pytest.mark.asyncio
async def test_mergeable_state_unknown_falls_back_to_keywords(db_session, mocker):
    """
    When mergeable_state is 'unknown' (GitHub still computing), fall back to
    keyword matching on the error message.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "unknown"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Repository rule violations: rule violation"},
    )

    project = Project(name="mergeable_state unknown", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.requires_approval is True, "unknown mergeable_state must fall back to keyword match"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True


# ---------------------------------------------------------------------------
# 6. ci_orig_id status propagation on successful merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orig_task_gets_pr_merged_flag_on_success(db_session, mocker):
    """
    When merge succeeds and ci_orig_id is set in review_loop, pr_merged=True
    is set on the original task's context.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Orig task propagation", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create the original task
    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    # Create the CI parking task that references orig_task
    task = _make_task(db_session, project, ci_loop_extra={
        "original_task_id": str(orig_task.id),
    })
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED

    # Verify pr_merged was propagated to orig_task.
    # SQLAlchemy identity map means session.get(Task, orig_task.id) inside the
    # function returns the SAME Python object, so assert in-memory (no flush/refresh).
    assert (orig_task.context or {}).get("pr_merged") is True, (
        "pr_merged=True must be set on the original task's context after merge"
    )


@pytest.mark.asyncio
async def test_orig_task_evaluating_without_ci_waiting_completes(db_session, mocker):
    """
    When merge succeeds and ci_orig_id is set, if the original task is EVALUATING
    and does NOT have ci_waiting set, it should also be marked COMPLETED.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Orig task auto complete", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Original task: EVALUATING, no ci_waiting in review_loop
    orig_task = Task(
        project_id=project.id,
        title="Original task (no ci_waiting)",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={"review_loop": {}},  # ci_waiting absent
        vm_instance_id="agent-vm-orig-002",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(db_session, project, ci_loop_extra={
        "original_task_id": str(orig_task.id),
    })
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    # Identity map: orig_task in test == orig inside function — check in-memory
    assert orig_task.status == TaskStatus.COMPLETED, (
        "EVALUATING orig task without ci_waiting must be completed after rollup merge"
    )
    assert (orig_task.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_orig_task_with_ci_waiting_stays_evaluating(db_session, mocker):
    """
    When merge succeeds and ci_orig_id is set, if the original task is EVALUATING
    and still has ci_waiting=True, it should NOT be auto-completed (it has its own
    CI check to wait on).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Orig stays evaluating", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Original task: EVALUATING with ci_waiting=True
    orig_task = Task(
        project_id=project.id,
        title="Original task (ci_waiting=True)",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={"review_loop": {"ci_waiting": True}},
        vm_instance_id="agent-vm-orig-003",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(db_session, project, ci_loop_extra={
        "original_task_id": str(orig_task.id),
    })
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    # Identity map: orig_task in test == orig inside function — check in-memory
    assert orig_task.status == TaskStatus.EVALUATING, (
        "Orig task with ci_waiting=True must remain EVALUATING — it still has a CI check pending"
    )
    # pr_merged is still set though
    assert (orig_task.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_merge_success_without_orig_id(db_session, mocker):
    """
    When merge succeeds and ci_orig_id is absent, the function completes without
    errors — no attempt to look up an original task.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="No orig id", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)  # no original_task_id
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED
    assert "merged" in (task.latest_status_message or "").lower()
