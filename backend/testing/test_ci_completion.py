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

from app.ci_completion import _normalize_merge_error
from app.models import Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# _normalize_merge_error — unit tests
# ---------------------------------------------------------------------------


def test_normalize_merge_error_empty_string():
    assert _normalize_merge_error("") == ""


def test_normalize_merge_error_strips_uuid():
    out = _normalize_merge_error(
        "merge failed for job 550e8400-e29b-41d4-a716-446655440000 retry"
    )
    assert "550e8400" not in out
    assert "<uuid>" in out


def test_normalize_merge_error_strips_timestamp():
    out = _normalize_merge_error("at 2026-04-19T12:34:56.789Z merge aborted")
    assert "2026" not in out
    assert "<ts>" in out


def test_normalize_merge_error_strips_long_hex():
    # Long hex (>=12 chars) gets <HEX>; short hex stays literal
    out = _normalize_merge_error("sha deadbeefcafe1234 invalid")
    assert "deadbeefcafe1234" not in out
    assert "<hex>" in out


def test_normalize_merge_error_replaces_numbers():
    short = _normalize_merge_error("retry 5 of 10")
    # both short ints replaced with <n>
    assert "5" not in short and "10" not in short
    assert "<n>" in short
    long_num = _normalize_merge_error("request id 1234567890 failed")
    assert "1234567890" not in long_num
    assert "<n>" in long_num


def test_normalize_merge_error_mixed_variable_data():
    msg = (
        "Job 550e8400-e29b-41d4-a716-446655440000 at 2026-04-19T12:34:56Z "
        "failed after 3 retries with sha deadbeefcafe9999"
    )
    out = _normalize_merge_error(msg)
    assert "<uuid>" in out
    assert "<ts>" in out
    assert "<hex>" in out
    assert "<n>" in out
    # whitespace collapsed, lowercased
    assert out == out.lower()
    assert "  " not in out


def test_normalize_merge_error_structural_equivalence():
    a = "Merge attempt 1 for job 550e8400-e29b-41d4-a716-446655440000 at 2026-04-19T12:34:56Z failed"
    b = "Merge attempt 7 for job 11111111-2222-3333-4444-555555555555 at 2026-04-20T01:02:03Z failed"
    assert _normalize_merge_error(a) == _normalize_merge_error(b)


# ---------------------------------------------------------------------------
# Merge retry / escalation tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    db_session, project, pr_number: int = 42, ci_loop_extra: dict | None = None
):
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
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
            }
        ],
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
    assert merge_mock.call_count == 2, (
        "Should have called merge twice (initial + 1 retry)"
    )
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

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test validates the retry-counter increment path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

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
    assert review_loop.get("merge_failures") == 1, (
        "merge_failures should be incremented"
    )


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

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test exercises the webhook one-shot path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert merge_mock.call_count == 1, (
        "Webhook source must not retry — exactly 1 merge call"
    )
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_merge_conflict_skips_retries_and_spawns_fix(db_session, mocker):
    """
    When merge returns conflict=True (409), the retry loop breaks immediately
    AND a fix task is spawned with merge_conflict reason — not silently falling
    through to the merge-failure retry counter.
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
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
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
    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "merge_conflict"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures", 0) == 0, (
        "merge_conflict must not increment the retry counter"
    )
    assert review_loop.get("ci_fix_spawned") == str(fake_fix.id)


@pytest.mark.asyncio
async def test_mergeable_state_behind_calls_update_branch(db_session, mocker):
    """
    When mergeable_state='behind', update_branch is invoked and the task re-enters
    CI polling (ci_passed=False, ci_waiting unchanged) instead of incrementing
    the merge-failure retry counter.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    update_mock = mocker.patch(
        "app.github.update_branch",
        return_value={"updated": True, "message": "Update queued"},
    )

    project = Project(name="Behind base test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, ci_loop_extra={"ci_passed": True})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    update_mock.assert_called_once()
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_passed") is False, (
        "ci_passed must be reset for fresh CI run"
    )
    assert review_loop.get("merge_failures", 0) == 0, (
        "behind must not increment retry counter"
    )
    assert task.requires_approval is not True


@pytest.mark.asyncio
async def test_update_branch_failure_increments_counter(db_session, mocker):
    """
    When update_branch fails in the behind-state handler, update_branch_failures
    counter increments. Under 3 failures, no fix task is spawned.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.update_branch",
        return_value={"updated": False, "message": "422 validation failed"},
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(None, False),
    )

    project = Project(name="Update branch fail test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_passed": True, "update_branch_failures": 1},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="webhook",
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("update_branch_failures") == 2
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_branch_failure_threshold_spawns_fix(db_session, mocker):
    """
    After 3 consecutive update_branch failures, the behind-state handler spawns
    a fix task with fix_reason='update_branch_failed' instead of looping.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.update_branch",
        return_value={"updated": False, "message": "422 validation failed"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Update branch escalate test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_passed": True, "update_branch_failures": 2},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="webhook",
    )

    assert result is False
    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "update_branch_failed"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("update_branch_failures") == 3
    assert review_loop.get("ci_fix_spawned") == str(fake_fix.id)


@pytest.mark.asyncio
async def test_update_branch_threshold_scales_with_merge_budget(db_session, mocker):
    """
    When the task has nearly exhausted its merge hard cap, the update_branch
    failure threshold shrinks so a single update_branch failure escalates.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.update_branch",
        return_value={"updated": False, "message": "422 validation failed"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Update branch dynamic threshold test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Small scope → hard cap 8. With 7 merge_failures consumed, only 1
    # update_branch attempt should be allowed before escalating.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "ci_passed": True,
            "update_branch_failures": 0,
            "merge_failures": 7,
        },
    )
    task.context = {**(task.context or {}), "scope": "small"}
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="webhook",
    )

    assert result is False
    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "update_branch_failed"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("update_branch_failures") == 1


@pytest.mark.asyncio
async def test_update_branch_success_resets_counter(db_session, mocker):
    """
    A successful update_branch call resets update_branch_failures to 0, so
    transient API failures don't accumulate across eventual successes.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.update_branch",
        return_value={"updated": True, "message": "Update queued"},
    )

    project = Project(name="Update branch reset test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_passed": True, "update_branch_failures": 2},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="webhook",
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("update_branch_failures") == 0


@pytest.mark.asyncio
async def test_mergeable_state_dirty_spawns_fix(db_session, mocker):
    """
    When mergeable_state='dirty' (real conflict per GitHub) but the merge call
    didn't surface conflict=True, a fix task is still spawned with
    fix_reason='merge_conflict'.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "dirty"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "merge failed"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Dirty state test", user_id="test-user")
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
    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "merge_conflict"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures", 0) == 0
    assert review_loop.get("ci_fix_spawned") == str(fake_fix.id)


# ---------------------------------------------------------------------------
# 3. Rule-block keyword detection → immediate escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_message",
    [
        "Repository rule violations: rule violation found",
        "Required review by branch protection not met",
        "required status: pending",
        "merge restrictions apply",
        "not allowed to merge",
        "at least 1 approving review required",
        "approving review from code owner required",
    ],
)
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
    assert task.requires_approval is True, (
        f"requires_approval must be set for: {rule_message}"
    )
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True
    assert review_loop.get("merge_failures", 0) == 0, (
        "rule blocks must not increment merge_failures"
    )
    assert "manual intervention" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_rule_block_does_not_escalate_when_gates_off(db_session, mocker):
    """With require_approval_gates OFF, a rule-blocked merge must NOT set
    requires_approval — otherwise services.py's 2h approval-gate timeout would
    FAIL the CI task and, via the orig escalation, strand the parent. The block
    is still recorded (merge_rule_blocked) and surfaced (status message + alert),
    so visibility is preserved; only the approval gate is skipped.

    Companion to test_rule_block_keywords_trigger_escalation (gates on)."""
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
        return_value={"merged": False, "message": "not allowed to merge"},
    )
    mocker.patch.dict(
        "app.shared_state.runtime_config", {"require_approval_gates": False}
    )

    project = Project(name="Rule block gates off", user_id="test-user")
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
    # The fix: gates off → no approval gate on the CI task.
    assert task.requires_approval is not True, (
        "rule block must NOT approval-gate when require_approval_gates is off"
    )
    # Block still recorded + surfaced (visibility preserved).
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True
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

    # Pre-existing in-flight fix task for this PR → `already_fixing` gate skips
    # the rebase-fix spawn in _attempt_merge_with_retry_and_escalation, which
    # lets this test exercise the ordinary transient-retry accumulation path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    task = _make_task(db_session, project, ci_loop_extra={"merge_failures": 4})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 5, (
        "merge_failures should have incremented from 4 to 5"
    )
    assert review_loop.get("merge_rule_blocked") is None, (
        "merge_rule_blocked must not be set"
    )
    assert task.requires_approval is not True, (
        "requires_approval must not be set for transient failure"
    )
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

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test validates the 10-failure alert escalation.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    task = _make_task(db_session, project, ci_loop_extra={"merge_failures": 9})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 10
    assert review_loop.get("merge_escalated") is True, (
        "merge_escalated must be set at 10 failures"
    )
    alert_mock.assert_called_once()
    alert_msg = alert_mock.call_args[0][2]
    assert "merge failed" in alert_msg.lower() or "10" in alert_msg


@pytest.mark.asyncio
async def test_merge_failure_final_warn_at_13_failures(db_session, mocker):
    """
    When merge_failures reaches MERGE_FAILURE_HARD_CAP - 2 (13) and the task is
    already escalated, a second 'hard-cap imminent' alert fires exactly once.
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

    project = Project(name="Final warn at 13", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test validates the final-warn alert path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    # Start at HARD_CAP - 3 with merge_escalated already set; this call pushes
    # failures to HARD_CAP - 2 (13) and should trigger the final-warn alert.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": MERGE_FAILURE_HARD_CAP - 3,
            "merge_escalated": True,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == MERGE_FAILURE_HARD_CAP - 2
    assert review_loop.get("merge_final_warn") is True
    alert_mock.assert_called_once()
    assert "imminent" in alert_mock.call_args[0][2].lower()

    # A second failure at HARD_CAP - 1 must not fire the final-warn alert again.
    alert_mock.reset_mock()
    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )
    for call in alert_mock.call_args_list:
        assert "imminent" not in call[0][2].lower()


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

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so the escalated path proceeds without double-alerting.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    # Already escalated with 10 failures; no new alert should fire on the next
    # transient failure (still below final-warn and hard-cap thresholds).
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": 10,
            "merge_escalated": True,
        },
    )
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

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test exercises the hard-cap path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    # One below the cap so this call crosses it; merge_escalated already True
    # so we test that the cap fires independently of the 10-failure escalation.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": MERGE_FAILURE_HARD_CAP - 1,
            "merge_escalated": True,
            "merge_final_warn": True,
        },
    )
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
    # Merge hard-cap must classify as EXTERNAL_API (infra), not CODE_ERROR —
    # the phrase "merge failed" in the status message is a keyword match.
    from app.failure_classification import classify_failure

    assert classify_failure(task) == "EXTERNAL_API"


@pytest.mark.asyncio
async def test_merge_failure_hard_cap_records_deploy_monitoring_result(
    db_session, mocker
):
    """
    Hard-cap FAILED terminations must appear in deploy_monitoring.task_results so
    per-type failure gates and anomaly detection see them. Previously these paths
    set status=FAILED silently without calling record_task_result.
    """
    from app.ci_completion import (
        MERGE_FAILURE_HARD_CAP,
        _attempt_merge_with_retry_and_escalation,
    )
    from app.routers.agent_status import _recorded_task_ids
    from app.shared_state import deploy_monitoring, direction_failure_streaks

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

    _recorded_task_ids.clear()
    deploy_monitoring["task_results"].clear()
    direction_id = uuid.uuid4().hex
    direction_failure_streaks.pop(direction_id, None)

    project = Project(name="Hard cap monitoring test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": MERGE_FAILURE_HARD_CAP - 1,
        },
    )
    task.context = {**(task.context or {}), "direction_id": direction_id}
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert task.status == TaskStatus.FAILED
    results = list(deploy_monitoring["task_results"])
    assert any(r[1] == "failure" for r in results), (
        f"Expected a failure entry in deploy_monitoring.task_results, got {results}"
    )
    assert str(task.id) in _recorded_task_ids
    # Direction health hook should still be called (proves the merge-hard-cap
    # path wired it up), but the merge hard-cap is classified as EXTERNAL_API
    # (infra) and infra failures no longer bump direction counters — they're
    # platform noise, not the direction's fault. setdefault leaves a zeroed
    # streak entry as evidence the hook ran.
    streak = direction_failure_streaks.get(direction_id)
    assert streak is not None, "record_direction_result should register a streak entry"
    assert not streak["held"]
    assert streak["consecutive_failures"] == 0


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

    assert result is True, (
        "Closed PR (state=closed) should be treated as already handled"
    )
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
    assert review_loop.get("merge_failures", 0) == 0, (
        "rule blocks must not increment merge_failures"
    )


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
        return_value={
            "merged": False,
            "message": "at least 1 approving review required",
        },
    )

    project = Project(name="mergeable_state clean", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test exercises the clean-state non-escalation path.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

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
    assert review_loop.get("merge_failures") == 1, (
        "treat as transient — counter should tick"
    )


@pytest.mark.asyncio
async def test_mergeable_state_clean_no_existing_fix_does_not_spawn_rebase(
    db_session, mocker
):
    """
    When mergeable_state='clean' and there is NO pre-existing fix task, a
    transient merge API failure must NOT spawn a rebase fix.  GitHub has
    confirmed the PR is conflict-free, so there is nothing to rebase — the
    only correct action is to retry the merge.

    Regression guard for the clean-state short-circuit in the rebase-fix
    spawn block (_ms == 'clean' condition).
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
        return_value={"merged": False, "message": "transient server error"},
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(None, False),
    )

    project = Project(name="clean no fix spawn", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No existing fix task — without the clean-state guard, the rebase-fix
    # spawn path would fire here.
    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    spawn_mock.assert_not_called()
    assert result is False
    assert task.requires_approval is not True, "clean state must not escalate"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 1, (
        "clean-state transient failure must still tick the retry counter"
    )


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
        return_value={
            "merged": False,
            "message": "Repository rule violations: rule violation",
        },
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
    assert task.requires_approval is True, (
        "unknown mergeable_state must fall back to keyword match"
    )
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
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED

    # Verify pr_merged was propagated to orig_task.
    # C1: the orig (rollup-orig) write now runs in its OWN AsyncSessionLocal
    # session (poison isolation), so it no longer shares the identity map with
    # this test's ``db_session``. Refresh from the DB to read the committed
    # value rather than the stale in-memory object.
    await db_session.refresh(orig_task)
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

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    # C1: orig write runs on its own dedicated session now — refresh to read
    # the committed state rather than the stale in-memory object.
    await db_session.refresh(orig_task)
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

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    # C1: orig write runs on its own dedicated session now — refresh to read
    # the committed state rather than the stale in-memory object.
    await db_session.refresh(orig_task)
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


@pytest.mark.asyncio
async def test_orig_pr_merged_write_failure_does_not_poison_caller_session(
    db_session, mocker
):
    """C1 containment test for the NEW dedicated-session orig (rollup-orig)
    pr_merged write: when that write's OWN ``AsyncSessionLocal`` scope fails, the
    failure must stay fully contained — the caller's merge-task session is never
    touched, so the parking task is still COMPLETED in-memory and the caller can
    still commit.

    NOTE: this test does NOT reproduce the pre-fix poison, and cannot — pre-fix
    that block had no ``AsyncSessionLocal`` at all (it wrote orig directly on the
    caller's ``session`` via ``session.get``), so patching
    ``app.database.AsyncSessionLocal`` would have been a no-op against the old
    code and the test would have passed pre-fix too. It instead asserts the
    *new* dedicated-session design's containment property: a failure opening or
    using the dedicated session is swallowed by the block's ``except`` and does
    not propagate to (or poison) the caller's session. The pre-/post-fix
    behavioural difference for the merge-task session is covered by the three
    sibling tests above (which now ``db_session.refresh(orig_task)`` because the
    committed orig state lives in a different session's identity map).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.github.merge_pull_request", return_value={"merged": True})
    # Make ONLY the dedicated orig-session fail. The caller's ``db_session`` uses
    # TestingSessionLocal and is unaffected by patching app.database.AsyncSessionLocal.
    mocker.patch(
        "app.database.AsyncSessionLocal",
        side_effect=RuntimeError("simulated dedicated-session DB error"),
    )

    project = Project(name="Orig write failure isolation", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-iso",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session, project, ci_loop_extra={"original_task_id": str(orig_task.id)}
    )
    await db_session.commit()
    await db_session.refresh(task)

    # Must not raise — the orig-write failure is swallowed inside the dedicated
    # session's try/except and the caller session stays clean.
    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result is True
    # Parking task mutation survived on the (un-poisoned) caller session.
    assert task.status == TaskStatus.COMPLETED
    # Caller session is committable (would raise PendingRollbackError pre-fix).
    await db_session.commit()


@pytest.mark.asyncio
async def test_ci_wait_started_resets_when_checks_restart(db_session, mocker):
    """
    When a merge attempt returns 'in progress' (checks re-running), ci_wait_started
    must be reset to the current time — not preserved from the original CI wait —
    so the 30-minute backstop timeout windows the new CI run, not the old one.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={
            "merged": False,
            "message": "Required status check is in progress",
        },
    )

    project = Project(name="ci_wait_started reset", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_ts = "2020-01-01T00:00:00+00:00"
    task = _make_task(db_session, project, ci_loop_extra={"ci_wait_started": stale_ts})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    new_ts = review_loop.get("ci_wait_started")
    assert new_ts is not None
    assert new_ts != stale_ts, (
        "ci_wait_started must be reset, not preserved from prior CI wait"
    )


@pytest.mark.asyncio
async def test_merge_repeated_identical_error_triggers_early_exit(db_session, mocker):
    """
    When the merge API returns the same error message 2 times in a row, the
    handler escalates immediately (FAILED + requires_approval) instead of
    burning all 15 retries.
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
        return_value={"merged": False, "message": "upstream timeout"},
    )

    project = Project(name="Repeat error early exit", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Prior attempt returned "upstream timeout"; this call is the 2nd.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": 1,
            "merge_last_error": "upstream timeout",
            "merge_error_streak": 1,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_error_streak") == 2
    assert review_loop.get("merge_failures") == 2, "must exit well before hard cap"
    assert "identical" in (task.latest_status_message or "").lower()
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_merge_deterministic_error_triggers_early_exit_on_first_occurrence(
    db_session, mocker
):
    """
    Deterministic non-retryable merge errors (e.g. "head sha can't be blank")
    should trigger immediate early exit on the first occurrence, without
    waiting for a 2-streak.
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
        return_value={"merged": False, "message": "Head sha can't be blank"},
    )

    project = Project(name="Deterministic early exit", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No prior failures — this is the first attempt.
    task = _make_task(db_session, project, ci_loop_extra={})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.FAILED
    assert task.requires_approval is True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures") == 1, "must exit on first occurrence"
    assert review_loop.get("merge_error_streak") == 1
    assert "deterministic" in (task.latest_status_message or "").lower()
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_merge_different_errors_do_not_trigger_early_exit(db_session, mocker):
    """
    When the merge error message changes between attempts, the streak resets
    and the handler continues retrying (no early FAILED escalation).
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
        return_value={"merged": False, "message": "different error"},
    )

    project = Project(name="Different errors", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-existing in-flight fix task → `already_fixing` gate skips the
    # rebase-fix spawn so this test exercises the streak-reset path on new error.
    existing_fix = Task(
        project_id=project.id,
        title="existing fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-fix-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(existing_fix)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": 2,
            "merge_last_error": "upstream timeout",
            "merge_error_streak": 2,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.EVALUATING, "should keep retrying on new error"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_error_streak") == 1, (
        "streak resets when error changes"
    )
    assert review_loop.get("merge_last_error") == "different error"


# ---------------------------------------------------------------------------
# Cross-PR merge error pattern detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_pr_merge_pattern_pauses_retries_and_alerts(db_session, mocker):
    """When 3 PRs in the same project fail to merge with the same normalized
    error, the third attempt is paused (no rebase-fix spawn, no hard-cap
    escalation) and an alert is fired once."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.shared_state import merge_error_occurrences

    merge_error_occurrences.clear()
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", return_value=(None, None)
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request", return_value={"merged": False, "state": "open"}
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "base branch rule requires X"},
    )

    project = Project(name="Cross-PR pattern", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Simulate two prior PRs hitting the same error so the window is primed.
    from app.shared_state import record_merge_error_occurrence
    from app.ci_completion import _normalize_merge_error

    norm = _normalize_merge_error("base branch rule requires X")
    record_merge_error_occurrence(str(project.id), norm, 101)
    record_merge_error_occurrence(str(project.id), norm, 102)

    task = _make_task(db_session, project, pr_number=103)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.EVALUATING, "pattern pause keeps task parked"
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_pattern_paused") is True
    assert review_loop.get("merge_pattern_pr_count") == 3
    assert review_loop.get("merge_pattern_alerted") is True
    alert_mock.assert_called_once()
    # Rebase-fix spawn must be short-circuited by the pattern pause.
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cross_pr_merge_pattern_alert_dedups_on_second_hit(db_session, mocker):
    """The alert fires only once per task even if the pattern keeps matching."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.shared_state import merge_error_occurrences, record_merge_error_occurrence
    from app.ci_completion import _normalize_merge_error

    merge_error_occurrences.clear()
    alert_mock = mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.review_pipeline._spawn_pipeline_task", return_value=(None, None))
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request", return_value={"merged": False, "state": "open"}
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "protected branch blocks merge"},
    )

    project = Project(name="Pattern dedup", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    norm = _normalize_merge_error("protected branch blocks merge")
    record_merge_error_occurrence(str(project.id), norm, 201)
    record_merge_error_occurrence(str(project.id), norm, 202)

    task = _make_task(db_session, project, pr_number=203)
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )
    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert alert_mock.call_count == 1, "pattern alert should only fire once per task"


def test_record_merge_error_occurrence_counts_distinct_prs():
    from app.shared_state import (
        merge_error_occurrences,
        record_merge_error_occurrence,
        count_merge_error_pattern_prs,
    )

    merge_error_occurrences.clear()
    assert record_merge_error_occurrence("proj-a", "err", 1) == 1
    assert record_merge_error_occurrence("proj-a", "err", 1) == 1  # same PR
    assert record_merge_error_occurrence("proj-a", "err", 2) == 2
    assert record_merge_error_occurrence("proj-a", "err", 3) == 3
    assert count_merge_error_pattern_prs("proj-a", "err") == 3
    assert count_merge_error_pattern_prs("proj-b", "err") == 0
    assert count_merge_error_pattern_prs("proj-a", "other") == 0


# ---------------------------------------------------------------------------
# Issue 9b — suppress_warm_handoff coverage for ci_completion spawn sites
# ---------------------------------------------------------------------------
#
# review_pipeline._spawn_pipeline_task accepts suppress_warm_handoff. The
# warm-VM continuity Phase 2d design deliberately ENDS the warm chain when
# a PR has been review-approved + CI-gated, so any fix spawned downstream
# from that point must cold-provision. ci_completion.py has 4 spawn sites
# where suppress_warm_handoff=True is required:
#   - line 334: update_branch_failed escalation (3+ consecutive failures)
#   - line 412: merge_conflict (mergeable_state='dirty' or 409 conflict)
#   - line 568: merge-blocked rebase fix (transient merge failure path)
#   - line 804: ci_failure (CI checks failed in handle_ci_completion)
#
# The CI failure path at line 804 lives inside handle_ci_completion which
# requires more elaborate fixture setup (it re-locks the task, calls into
# get_check_runs, etc.); a coverage test for that one already exists in
# test_task_lifecycle.py::test_approve_branch_does_not_propagate_warm_target
# via _process_post_commit's APPROVE branch. The 3 remaining sites all live
# in _attempt_merge_with_retry_and_escalation and are exercised here.


@pytest.mark.asyncio
async def test_suppress_warm_handoff_update_branch_escalation(db_session, mocker):
    """Issue 9b: ci_completion.py:334 update_branch escalation spawn must
    pass suppress_warm_handoff=True."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.update_branch",
        return_value={"updated": False, "message": "422 validation failed"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Update branch suppress warm", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_passed": True, "update_branch_failures": 2},
    )
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="webhook",
    )

    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "update_branch_failed"
    assert spawn_mock.call_args.kwargs.get("suppress_warm_handoff") is True, (
        "update_branch_failed escalation (ci_completion.py:334) must pass "
        "suppress_warm_handoff=True — Phase 2d post-APPROVE chain end"
    )


@pytest.mark.asyncio
async def test_suppress_warm_handoff_merge_conflict(db_session, mocker):
    """Issue 9b: ci_completion.py:412 merge_conflict spawn (conflict=True or
    mergeable_state='dirty') must pass suppress_warm_handoff=True."""
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
        return_value={"merged": False, "conflict": True, "message": "merge conflict"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Merge conflict suppress warm", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "merge_conflict"
    assert spawn_mock.call_args.kwargs.get("suppress_warm_handoff") is True, (
        "merge_conflict spawn (ci_completion.py:412) must pass "
        "suppress_warm_handoff=True — Phase 2d post-APPROVE chain end"
    )


@pytest.mark.asyncio
async def test_merge_conflict_uses_pr_head_ref_on_drift(db_session, mocker, caplog):
    """When the spawn-derived ``ci_branch`` diverges from the PR's actual
    ``head.ref`` (incident 2026-05-26..28: misconfigured
    ``task_branch_namespace`` made the orchestrator derive
    ``blueprintaistudio/refactor-X`` while the agent had pushed
    ``blueprint/refactor-X``), the fix-spawn must use ``PR.head.ref`` — not
    the stale derived branch — and log a drift warning.

    Without this correction the fix task's ``git clone --branch`` runs
    against a nonexistent branch and exhausts its 3 retries, leaving the
    parent PR stranded and burning an agent-VM provision per incident."""
    import logging

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={
            "merged": False,
            "state": "open",
            "head": {"ref": "blueprint/test-branch", "sha": "abc"},
        },
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "conflict": True, "message": "merge conflict"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Drift correction test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent task has the stale derived branch in review_loop + payload —
    # exactly the production state when the wedge was missing.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"branch": "blueprintaistudio/test-branch"},
    )
    task.payloads = [
        {
            "type": "review",
            "pr_number": 42,
            "branch": "blueprintaistudio/test-branch",
            "base_branch": "main",
        }
    ]
    await db_session.commit()
    await db_session.refresh(task)

    with caplog.at_level(logging.WARNING, logger="app.ci_completion"):
        await _attempt_merge_with_retry_and_escalation(
            task, "fake-token", "owner", "repo", db_session, source="poll"
        )

    spawn_mock.assert_called_once()
    # branch is the 5th positional argument to _spawn_pipeline_task
    # (task, "fix", session, ci_pr_number, branch, base, ...)
    spawn_branch_arg = spawn_mock.call_args.args[4]
    assert spawn_branch_arg == "blueprint/test-branch", (
        f"Drift correction must use PR.head.ref='blueprint/test-branch', "
        f"not the stale derived branch — got {spawn_branch_arg!r}"
    )
    drift_warnings = [r for r in caplog.records if "branch drift" in r.getMessage()]
    assert drift_warnings, "Expected a 'branch drift' WARNING to be logged"


@pytest.mark.asyncio
async def test_suppress_warm_handoff_merge_blocked_rebase(db_session, mocker):
    """Issue 9b: ci_completion.py:568 merge-blocked rebase spawn (transient
    merge failure that survives retries with no existing in-flight fix) must
    pass suppress_warm_handoff=True."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        # Use 'unknown' so keyword-based rule-block detection runs and the
        # transient path (no clean-state guard) reaches the rebase-fix spawn.
        return_value={"merged": False, "state": "open", "mergeable_state": "unknown"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "transient server error"},
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="Merge blocked rebase suppress warm", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No existing fix → already_fixing gate is False → rebase-fix spawn fires.
    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    # The rebase-fix path passes neither fix_reason nor a custom kwarg beyond
    # suppress_warm_handoff=True; the call signature is identifiable by the
    # absence of update_branch_failed/merge_conflict.
    spawn_mock.assert_called_once()
    kw = spawn_mock.call_args.kwargs
    assert kw.get("fix_reason", "") == "", (
        f"merge-blocked rebase spawn must NOT set fix_reason, got {kw.get('fix_reason')!r}"
    )
    assert kw.get("suppress_warm_handoff") is True, (
        "merge-blocked rebase spawn (ci_completion.py:568) must pass "
        "suppress_warm_handoff=True — Phase 2d post-APPROVE chain end"
    )


@pytest.mark.asyncio
async def test_suppress_warm_handoff_ci_failure(db_session, mocker):
    """Issue 9b: ci_completion.py:804 ci_failure spawn (CI checks failed in
    handle_ci_completion) must pass suppress_warm_handoff=True.

    Drives handle_ci_completion directly with a failed-CI result so the
    spawn path at line 804 fires."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="CI failure suppress warm", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        # review_approved is the gate path used by both branches; ci_passed
        # is irrelevant for the failed-CI path because conclusion='failure'.
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "FAILED test_x\nbuild failed",
    }
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="owner",
        repo="repo",
        session=db_session,
        source="poll",
    )

    spawn_mock.assert_called_once()
    assert spawn_mock.call_args.kwargs.get("fix_reason") == "ci_failure"
    assert spawn_mock.call_args.kwargs.get("suppress_warm_handoff") is True, (
        "ci_failure spawn (ci_completion.py:804) must pass "
        "suppress_warm_handoff=True — Phase 2d post-APPROVE chain end"
    )


@pytest.mark.asyncio
async def test_ci_failure_broadcast_uses_status_snapshot_not_post_commit_read(
    db_session, mocker
):
    """Regression for the ci_completion.py fix-spawn broadcast MissingGreenlet.

    On the CI-failure fix-spawned path the handler sets task.status, commits,
    then broadcasts a task_updated event. A rollback earlier on that path (e.g.
    inside _spawn_pipeline_task) expires the `task` ORM instance even with
    expire_on_commit=False, so reading task.status when building the broadcast
    payload did a *sync* lazy-load in async context → MissingGreenlet in prod.
    The fix snapshots the status BEFORE commit.

    Simulates the post-commit expiry by expiring `task` and rewriting the
    persisted status out-of-band ONLY on the final broadcast commit (once the
    handler has set the mutation-time status to COMPLETED). The broadcast must
    still carry the snapshotted COMPLETED, never a re-fetched/expired value
    (which would be FAILED here).

    C2 note: the CI-failure path now commit-releases the task FOR UPDATE lock
    around create_pr_comment and re-locks with snapshot validation before the
    fix spawn. That earlier relock commit happens while status is still the
    captured snapshot value (EVALUATING). We must NOT corrupt the status on
    that commit — a spurious out-of-band status flip there would make the
    legitimate relock false-defer (returning None) and short-circuit the
    handler before the fix-spawn/broadcast path under test. The relock
    re-validating the row is correct new behavior; the test artifact only
    simulates the post-commit expiry on the FINAL broadcast commit (gated on
    the in-session status already being COMPLETED).
    """
    from sqlalchemy import update

    from app.ci_completion import handle_ci_completion

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    bcast = mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="CI status snapshot bcast", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    # Wrap commit so that ONLY the final broadcast commit (once the handler
    # has set the mutation-time status to COMPLETED) expires `task` and
    # rewrites the persisted status out-of-band. With the bug, the post-commit
    # task.status read would lazy-load the corrupted value (FAILED); with the
    # snapshot fix the broadcast carries the mutation-time COMPLETED. We gate
    # on the in-session status so the earlier C2 relock commit (status still
    # EVALUATING) is left untouched and the relock validates cleanly instead of
    # false-deferring.
    _real_commit = db_session.commit

    async def _commit_then_expire():
        # Read the in-session status BEFORE committing/expiring. Only the final
        # broadcast commit runs after the handler set status=COMPLETED; the
        # earlier relock commit runs with status=EVALUATING and must be left
        # uncorrupted so the legitimate snapshot-validated relock succeeds.
        is_final_broadcast_commit = task.status == TaskStatus.COMPLETED
        await _real_commit()
        if is_final_broadcast_commit:
            await db_session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(status=TaskStatus.FAILED)
            )
            await _real_commit()
            db_session.expire(task)

    mocker.patch.object(db_session, "commit", side_effect=_commit_then_expire)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "boom",
    }
    result = await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="owner",
        repo="repo",
        session=db_session,
        source="poll",
    )

    assert result is True
    task_updated_calls = [
        c
        for c in bcast.call_args_list
        if len(c.args) >= 2 and c.args[1] == "task_updated"
    ]
    assert task_updated_calls, "expected a task_updated broadcast on the fix-spawn path"
    payload = task_updated_calls[-1].args[2]
    assert payload["status"] == TaskStatus.COMPLETED, (
        "broadcast must carry the snapshotted mutation-time status, not a "
        f"re-fetched/expired value (got {payload['status']!r})"
    )


@pytest.mark.asyncio
async def test_ci_failure_spawn_extracts_failing_test_names_into_prompt(
    db_session, mocker
):
    """Failing test names from pytest's "FAILED <path>::<name>" lines should
    appear as a structured list at the top of the fix-PR prompt.

    Without this, agents had to scan ~2KB of CI log noise to find what to fix.
    Regression for the 2026-05-07 rollup-loop where 3 fix-tasks burnt out
    chasing tests that were already broken on main.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="CI failing-tests prompt", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": (
            "============================= test session starts ==============================\n"
            "FAILED testing/test_pack_under_budget.py::test_meta_preserved\n"
            "FAILED testing/test_anomaly_detection.py::test_proactive_fingerprint_spawns\n"
            "FAILED testing/test_invariants.py::TestSize::test_lifecycle\n"
            "5 errors, 3 failed in 12.5s\n"
        ),
    }
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="owner",
        repo="repo",
        session=db_session,
        source="poll",
    )

    spawn_mock.assert_called_once()
    fix_prompt = spawn_mock.call_args.kwargs.get("fix_prompt") or ""
    assert "Failing tests (extracted from CI summary" in fix_prompt
    assert "testing/test_pack_under_budget.py::test_meta_preserved" in fix_prompt
    assert (
        "testing/test_anomaly_detection.py::test_proactive_fingerprint_spawns"
        in fix_prompt
    )
    assert "testing/test_invariants.py::TestSize::test_lifecycle" in fix_prompt
    # The extracted-list block must come BEFORE the verbatim CI errors dump,
    # so the agent reads the structured list first.
    assert fix_prompt.index("Failing tests (extracted") < fix_prompt.index(
        "CI errors captured"
    )
    # And the broken-on-main fix-as-part-of-PR guidance should be there.
    assert "broken on `main`" in fix_prompt


# ---------------------------------------------------------------------------
# Per-project base_branch resolution
# ---------------------------------------------------------------------------


def _make_task_no_base(db_session, project, pr_number: int = 77):
    """Create a minimal EVALUATING task whose payload and context omit base_branch."""
    task = Task(
        project_id=project.id,
        title="CI base-branch test task",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "blueprint/feature-x",
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": "blueprint/feature-x",
                "iteration": 1,
                "review_approved": True,
            }
        },
        vm_instance_id=f"agent-vm-base-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


@pytest.mark.asyncio
async def test_handle_ci_completion_uses_project_integration_branch(db_session, mocker):
    """handle_ci_completion resolves base_branch from project.integration_branch
    when neither review_loop nor payload carry one.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )
    mocker.patch(
        "app.review_pipeline.build_ci_failure_fix_prompt",
        return_value="fix prompt",
    )

    project = Project(
        name="automain-project",
        user_id="test-user",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task_no_base(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "failure", "failed_logs": "error"}
    await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    spawn_mock.assert_called_once()
    # Third positional arg to _spawn_pipeline_task is ci_base
    called_base = spawn_mock.call_args.args[5]
    assert called_base == "automain", (
        f"Expected ci_base='automain' from project.integration_branch, got {called_base!r}"
    )


@pytest.mark.asyncio
async def test_merge_conflict_spawn_uses_project_integration_branch(db_session, mocker):
    """_attempt_merge_with_retry_and_escalation resolves ci_base from
    project.integration_branch when payload and review_loop both omit base_branch.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "dirty"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "merge failed"},
    )

    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(
        name="automain-merge-test",
        user_id="test-user",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task_no_base(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    spawn_mock.assert_called_once()
    called_base = spawn_mock.call_args.args[5]
    assert called_base == "automain", (
        f"Expected ci_base='automain' from project.integration_branch, got {called_base!r}"
    )


@pytest.mark.asyncio
async def test_behind_skipped_when_pr_is_not_behind(db_session, mocker):
    """
    When mergeable_state='behind' but pr_is_not_behind returns True (base tip is
    already reachable from PR head), update_branch must NOT be called.
    The task re-enters CI polling with ci_passed=False and no update_branch_failures
    increment.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={
            "merged": False,
            "state": "open",
            "mergeable_state": "behind",
            "head": {"sha": "abc123def456"},
        },
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "Base branch was modified"},
    )
    mocker.patch(
        "app.github.get_branch_sha",
        return_value="base456tip789",
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        return_value=True,
    )
    update_mock = mocker.patch("app.github.update_branch")

    project = Project(name="Not-behind skip test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, ci_loop_extra={"ci_passed": True})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    update_mock.assert_not_called()
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_passed") is False, "ci_passed must be reset for re-poll"
    assert review_loop.get("merge_failures", 0) == 0, (
        "behind must not increment retry counter"
    )
    assert review_loop.get("update_branch_failures", 0) == 0


# ---------------------------------------------------------------------------
# In-progress reset cap (Fix 2B item 3)
# ---------------------------------------------------------------------------
#
# Each "checks in progress" merge-API response used to unconditionally reset
# ``ci_wait_started`` to now — bumping the CI-wait deadline by another full
# backstop window every cycle.  A PR whose required checks never resolved
# would spin on this path until the 6-hour parent timeout.  The cap stops
# the reset after N attempts; the normal backstop then runs out on schedule.


@pytest.mark.asyncio
async def test_in_progress_reset_cap_honored(db_session, mocker):
    """
    Once ``merge_in_progress_resets`` reaches the cap (default 3), subsequent
    "checks in progress" merge responses MUST NOT reset ``ci_wait_started``
    nor increment the counter.  Without the cap a PR with permanently-pending
    checks polls forever until the 6-hour parent timeout fires.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={
            "merged": False,
            "message": "Required status check is in progress",
        },
    )

    project = Project(name="in-progress cap test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pinned_ts = "2026-01-01T00:00:00+00:00"
    # Task already at the cap — the next in-progress response must NOT reset.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "ci_wait_started": pinned_ts,
            "merge_in_progress_resets": 3,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    review_loop = (task.context or {}).get("review_loop", {})
    # ci_wait_started must be UNCHANGED — the cap stops the reset so the
    # backstop runs out on schedule rather than rolling forward forever.
    assert review_loop.get("ci_wait_started") == pinned_ts, (
        "ci_wait_started must not be reset once the in-progress cap is reached"
    )
    # Counter not incremented past the cap.
    assert review_loop.get("merge_in_progress_resets") == 3
    # Diagnostics for the EVALUATING-timeout reason-picker are persisted.
    assert review_loop.get("last_merge_msg")
    assert "mergeable_state" in review_loop


@pytest.mark.asyncio
async def test_in_progress_reset_increments_under_cap(db_session, mocker):
    """
    Under the cap, an in-progress response resets ``ci_wait_started`` AND
    increments ``merge_in_progress_resets`` so the counter is observable for
    the next cap check.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={
            "merged": False,
            "message": "Required status check is in progress",
        },
    )

    project = Project(name="in-progress cap increment", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_ts = "2020-01-01T00:00:00+00:00"
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "ci_wait_started": stale_ts,
            "merge_in_progress_resets": 1,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_in_progress_resets") == 2
    assert review_loop.get("ci_wait_started") != stale_ts
