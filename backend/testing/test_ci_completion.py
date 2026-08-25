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
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from app.ci_completion import _normalize_merge_error, _review_approved
from app.migration_reservations import allocate_migration_reservation
from app.models import Alert, Project, Task, TaskStatus
from app.rollup_migration_validation import PremergeMigrationGuardResult
from app.rollup_size_preflight import SizeCapPreflightResult


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    ``_attempt_merge_with_retry_and_escalation`` now runs
    ``enforce_premerge_migration_guard`` before merging, which calls
    ``github.get_pr_files``. Existing merge tests only mock
    ``merge_pull_request``; without this, an unmocked ``get_pr_files`` would make
    the guard ``defer`` and HOLD every merge. Returning an empty (no-migration)
    file list short-circuits the guard to ``ok``. Tests exercising the guard
    itself re-patch ``app.github.get_pr_files`` to override this default.
    """
    mocker.patch(
        "app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}])
    )


@pytest.fixture(autouse=True)
def _default_size_cap_ok(mocker):
    """Default size-cap preflight to 'ok' so existing merge tests are unaffected.

    ``_attempt_merge_with_retry_and_escalation`` now runs
    ``preflight_rollup_size_caps`` for direct task PRs. Existing merge tests
    don't mock the GitHub calls the preflight makes, so without this default
    the unmocked ``get_file_text_at_ref`` would make every merge test fail.
    Tests exercising the size-cap gate re-patch
    ``app.rollup_size_preflight.preflight_rollup_size_caps`` to override this.
    """
    async def _bound_ok(
        *_args, expected_head_sha=None, target_branch=None, expected_base_sha=None, **_kwargs
    ):
        return SizeCapPreflightResult(
            "ok", None, expected_head_sha, target_branch, expected_base_sha
        )

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(side_effect=_bound_ok),
    )


@pytest.fixture(autouse=True)
def _default_external_review_decision_clear(mocker):
    """Existing merge tests model PRs with no external review veto."""
    mocker.patch(
        "app.github_pulls.get_pull_request_review_decision",
        AsyncMock(return_value=None),
    )


@pytest.fixture(autouse=True)
def _default_exact_merge_target(mocker):
    """Bind ordinary merge fixtures to a deterministic live base/head pair."""

    async def _get_target(
        _token, _owner, _repo, pr_number, *, project_id, **_kwargs
    ):
        return {
            "project_id": project_id,
            "pr_number": pr_number,
            "base_sha": f"test-base-{pr_number}",
            "head_sha": f"test-head-{pr_number}",
        }

    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(side_effect=_get_target),
    )


@pytest_asyncio.fixture(autouse=True)
async def _patch_dedicated_sessions_to_test_engine(mocker, db_session):
    """Dedicated AsyncSessionLocal scopes in ci_completion must use the shared
    test engine, not the production lazy engine."""
    dedicated_session_local = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.database.AsyncSessionLocal", dedicated_session_local)


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


def test_review_merge_permission_requires_durable_flag_or_policy_marker():
    assert _review_approved({"verdict": "APPROVE"}) is False
    assert _review_approved({"review_approved": True}) is True
    assert _review_approved({"review_not_required": True}) is False
    assert (
        _review_approved(
            {
                "review_not_required": True,
                "review_policy_reason": "small scoped policy bypass",
            }
        )
        is True
    )
    assert (
        _review_approved(
            {
                "review_not_required": True,
                "review_policy_reason": "stale policy marker",
                "review_history": [{"verdict": "REQUEST_CHANGES"}],
            }
        )
        is False
    )
    assert (
        _review_approved(
            {
                "review_not_required": True,
                "review_policy_reason": "malformed history must fail closed",
                "review_history": {},
            }
        )
        is False
    )
    assert _review_approved({"review_approved": "true"}) is False
    assert _review_approved({"review_not_required": "true"}) is False


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
        # Most merge-helper tests exercise a deliberate no-initial-review
        # policy path. Empty history is authorized only by this durable marker;
        # authoritative-review tests explicitly override it to False.
        "review_not_required": True,
        "review_policy_reason": "test fixture explicit no-review policy",
    }
    if ci_loop_extra:
        loop.update(ci_loop_extra)
    checked_head = str(loop.get("ci_checked_head_sha") or loop.get("head_sha") or f"test-head-{pr_number}")
    checked_base = str(loop.get("ci_checked_base_sha") or loop.get("base_sha") or f"test-base-{pr_number}")
    loop.setdefault("head_sha", checked_head)
    loop.setdefault("base_sha", checked_base)
    loop.setdefault("ci_checked_head_sha", checked_head)
    loop.setdefault("ci_checked_base_sha", checked_base)
    loop.setdefault(
        "review_target",
        {
            "project_id": str(project.id),
            "pr_number": pr_number,
            "base_sha": checked_base,
            "head_sha": checked_head,
        },
    )
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


@pytest.mark.asyncio
async def test_external_changes_requested_vetoes_internal_approve(
    db_session, mocker
):
    """GitHub CHANGES_REQUESTED is a hard veto even after Blueprint APPROVE."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    decision_mock = mocker.patch(
        "app.github_pulls.get_pull_request_review_decision",
        AsyncMock(return_value="CHANGES_REQUESTED"),
    )
    request_mock = mocker.patch("app.github._github_request", new=AsyncMock())
    project = Project(name="External review veto", user_id="test-user")
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
    assert task.status == TaskStatus.EVALUATING
    assert "outstanding changes requested" in task.latest_status_message
    decision_mock.assert_awaited_once()
    request_mock.assert_not_awaited()


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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"head_sha": "reviewed-head-sha"},
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(
            return_value={
                "project_id": str(project.id),
                "pr_number": 42,
                "base_sha": "test-base-42",
                "head_sha": "reviewed-head-sha",
            }
        ),
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, (
        "Should return True after successful retry; "
        f"status={task.latest_status_message!r} context={task.context!r}"
    )
    assert task.status == TaskStatus.COMPLETED
    assert merge_mock.call_count == 2, (
        "Should have called merge twice (initial + 1 retry)"
    )
    assert all(
        call.kwargs["expected_head_sha"] == "reviewed-head-sha"
        for call in merge_mock.call_args_list
    )
    # sleep called once for attempt=0 → 5*(3^0)=5 seconds
    sleep_mock.assert_called_once_with(5)


@pytest.mark.asyncio
async def test_merge_retry_all_attempts_fail_poll(db_session, mocker):
    """
    When all 3 retries fail, the function increments merge_failures and returns False.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
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
    assert review_loop.get("merge_failures") == 1, (
        "merge_failures should be incremented"
    )


@pytest.mark.asyncio
async def test_merge_retry_uses_exponential_backoff_delays(db_session, mocker):
    """
    Retry delays follow 5*(3^attempt): 5s, 15s, 45s for attempts 0, 1, 2.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    sleep_mock = mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Failure counter test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Escalation at 10", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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
async def test_merge_failure_escalation_dedups_until_hard_cap(db_session, mocker):
    """
    Consolidated ladder: once the single escalation rung has fired
    (merge_escalated set), every later transient failure up to the hard cap is
    silent — no second 'final-warn'/'imminent' alert. The next and only further
    operator signal is the terminal hard-cap FAIL (covered separately).
    """
    from app.ci_completion import (
        MERGE_FAILURE_HARD_CAP,
        _attempt_merge_with_retry_and_escalation,
    )

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "server error"},
    )

    project = Project(name="Escalation dedup", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Already escalated, mid-way to the cap (HARD_CAP - 3). This call pushes
    # failures to HARD_CAP - 2 — still below the cap — and must NOT alert again.
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
    # No 'final-warn' latch exists anymore and no second warning alert fires.
    assert "merge_final_warn" not in review_loop
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_merge_failure_does_not_double_escalate(db_session, mocker):
    """
    If merge_escalated is already set, a subsequent failure must NOT create
    another alert.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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
    # transient failure (escalation rung already latched, still below hard cap).
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

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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
    # so we test that the cap fires independently of the escalation rung.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "merge_failures": MERGE_FAILURE_HARD_CAP - 1,
            "merge_escalated": True,
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
async def test_merge_success_leaves_orig_pr_merged_unset_on_promotion_target(
    db_session, mocker
):
    """Merged review PRs that land on promotion_target must not mark orig pr_merged."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion._post_merge_summary", new_callable=AsyncMock)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "deadbeef"},
    )

    project = Project(
        name="Promotion target merge",
        user_id="test-user",
        integration_branch="automain",
        promotion_target="release/main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="Original task",
        payloads=[{"type": "research", "prompt": "orig"}],
        context={},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "original_task_id": str(orig.id),
            "base_branch": "release/main",
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    await db_session.refresh(orig)
    alerts = (
        (await db_session.execute(_select(Alert).where(Alert.task_id == orig.id)))
        .scalars()
        .all()
    )

    assert result is True
    assert orig.status == TaskStatus.COMPLETED
    assert (orig.context or {}).get("pr_merged") is None
    assert len(alerts) == 1
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1


@pytest.mark.asyncio
async def test_merge_success_keeps_rollup_orig_evaluating_on_intermediate_branch(
    db_session, mocker
):
    """Merged rollup PRs on staging branches must await downstream landing."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion._post_merge_summary", new_callable=AsyncMock)
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "feedface"},
    )

    project = Project(
        name="Intermediate rollup merge",
        user_id="test-user",
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="Rollup parent",
        payloads=[{"type": "research", "prompt": "orig"}],
        context={"_is_rollup_review": True},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "original_task_id": str(orig.id),
            "base_branch": "release/candidate-1",
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    await db_session.refresh(orig)
    alerts = (
        (await db_session.execute(_select(Alert).where(Alert.task_id == orig.id)))
        .scalars()
        .all()
    )

    assert result is True
    assert orig.status == TaskStatus.EVALUATING
    assert (orig.context or {}).get("pr_merged") is None
    assert "awaiting downstream landing" in (orig.latest_status_message or "")
    assert alerts == []
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []


@pytest.mark.asyncio
async def test_closed_not_merged_no_marker_defers_not_completes(db_session, mocker):
    """C4 regression: a closed-but-NOT-merged PR with NO deliberate self-close
    marker must NOT be treated as a success-close.

    Pre-fix the gate fired on ``state == 'closed'`` regardless of ``merged``,
    so a transient orchestrator mergeability-reset (close+reopen) that briefly
    showed closed-not-merged was marked COMPLETED + pr_closed_success — falsely
    succeeding an unmerged rollup that _verify_pr_merged then cemented.

    Corrected behavior: defer (return False), leave the parent EVALUATING for
    _verify_pr_merged to classify (genuine rejection vs. transient reset).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    assert result is False, (
        "Closed-not-merged with no self-close marker must NOT be a success-close"
    )
    assert task.status == TaskStatus.EVALUATING, (
        "Parent must stay EVALUATING for _verify_pr_merged to classify"
    )
    assert not (task.context or {}).get("pr_closed_success"), (
        "Must not falsely stamp pr_closed_success on an unmerged closed PR"
    )


@pytest.mark.asyncio
async def test_closed_not_merged_pending_reopen_defers(db_session, mocker):
    """C4: a closed-not-merged PR with review_loop.pending_reopen=True is OUR
    own mergeability-reset window — defer (return False), do not complete."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    project = Project(name="Closed PR pending_reopen", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, ci_loop_extra={"pending_reopen": True})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is False
    assert task.status == TaskStatus.EVALUATING
    assert not (task.context or {}).get("pr_closed_success")


@pytest.mark.asyncio
async def test_closed_not_merged_with_success_marker_completes(db_session, mocker):
    """C4: a closed-not-merged PR WITH an explicit deliberate-close marker
    (pr_closed_success) IS a genuine success-close (work landed via a sibling
    PR) and completes the task."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    project = Project(name="Closed PR success marker", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    # Stamp the deliberate-close marker the way ci_completion / review pipeline
    # would when the same change merged via a sibling PR.
    task.context = {**(task.context or {}), "pr_closed_success": True}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="webhook"
    )

    assert result is True, "Deliberate success-close marker completes the task"
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
async def test_self_orig_merge_sets_pr_merged_without_lock_conflict(db_session, mocker):
    """Direct merge where original_task_id == the merge task's OWN id.

    This is the common lane (research/plan/create tasks merging their own PR).
    The dedicated ``_orig_session`` writes to the SAME tasks row the caller's
    session holds FOR UPDATE from ``_relock_merge_task``; without the early
    commit-and-release, its autoflush UPDATE blocked for the full lock_timeout
    and died with LockNotAvailableError — silently dropping pr_merged, the
    direction-outcome refresh, and the reservation finalize on every direct
    merge (prod 2026-06-01..06-07, 144 drops).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.landed_signal import (
        corroborate_and_apply_merged_landed_signal as _real_landing,
    )

    landing_session_owns_orig: list[bool] = []

    async def _guard_landing_session(orig, orig_project, landing_session, **kwargs):
        """Fail fast instead of waiting on a cross-session Project row lock."""
        from sqlalchemy import inspect as sa_inspect

        owns_orig = (
            sa_inspect(orig).session is landing_session.sync_session
            and sa_inspect(orig_project).session is landing_session.sync_session
        )
        landing_session_owns_orig.append(owns_orig)
        if not owns_orig:
            return False
        return await _real_landing(orig, orig_project, landing_session, **kwargs)

    mocker.patch(
        "app.landed_signal.corroborate_and_apply_merged_landed_signal",
        new=_guard_landing_session,
    )

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Self-orig merge", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    # Point original_task_id back at the task itself — the direct-merge shape.
    loop = dict((task.context or {}).get("review_loop", {}))
    loop["original_task_id"] = str(task.id)
    task.context = {**(task.context or {}), "review_loop": loop}
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert landing_session_owns_orig == [True], (
        "landing corroboration must use the dedicated session that owns the "
        "original task and project"
    )

    # Both the terminal status AND the orig-side flag must be durable. Read
    # back from the DB: the orig write runs in its own AsyncSessionLocal
    # session, so the in-memory instance can be stale.
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True, (
        "self-orig merge must set pr_merged on the task itself — a lock "
        "conflict between the caller's FOR UPDATE and the dedicated orig "
        "session must not silently drop it"
    )


@pytest.mark.asyncio
async def test_integration_landing_triggers_build_config_refresh(db_session, mocker):
    """When a feature task's PR lands on the integration branch, the empty
    build_config auto-refresh runs against the landed project."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )
    refresh_mock = mocker.patch(
        "app.build_config_detection.maybe_refresh_empty_build_config",
        new_callable=AsyncMock,
        return_value=False,
    )

    project = Project(
        name="Refresh on integration landing",
        user_id="test-user",
        github_owner="acme",
        github_repo="scaffolded",
        integration_branch="main",
        build_config={"language": "python", "directories": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-refresh",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    refresh_mock.assert_awaited_once()
    # Called with the landed project and the integration branch ref.
    assert refresh_mock.await_args.args[0].id == project.id
    assert refresh_mock.await_args.kwargs.get("ref") == "main"


@pytest.mark.asyncio
async def test_criterion_evidence_persisted_via_attempt_merge_proven_verdict_covers_criterion(
    db_session, mocker
):
    """Direct CI merge with criterion_ids records proven task_merge evidence.

    The direct merge path (_attempt_merge_with_retry_and_escalation) now records
    CriterionEvidence for each criterion the task carries when the merge succeeds.
    The evidence must be recorded with proven=True so the criterion coverage
    transitions from 'uncovered' to 'covered' (not just 'partial').

    This pins the direct merge path to the same proven-evidence standard that
    the rollup path enforces, ensuring v22 criteria do not depend on one
    merge topology.
    """
    from app.acceptance_criteria import (
        compute_criterion_coverage,
        create_criterion,
        get_evidence_for_criterion,
    )
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    # Create project with release version and criterion
    project = Project(name="Criterion merge test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create an acceptance criterion
    criterion = await create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Direct merge must record proven evidence",
        created_by="strategist",
    )

    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    # Create the original task (what carries the criterion)
    orig_task = Task(
        project_id=project.id,
        title="Original task with criterion",
        payloads=[{"type": "research", "prompt": "test"}],
        context={CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True}},
        criterion_ids=[criterion.criterion_key],  # Task carries the criterion
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    # Create the CI parking task that will merge and record evidence
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    # Trigger the merge
    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED

    # Refresh the original task to read the committed state from the dedicated _orig_session
    await db_session.refresh(orig_task)

    # Verify evidence was recorded with proven=True
    evidence = await get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) > 0, "Evidence should be recorded after merge"

    # Find the task_merge evidence entry
    task_merge_evidence = [e for e in evidence if e.source_kind == "task_merge"]
    assert len(task_merge_evidence) > 0, "task_merge evidence should be recorded"
    assert task_merge_evidence[0].detail.get("proven") is True, (
        "task_merge evidence must have proven=True to satisfy criterion coverage"
    )

    # Verify coverage transitions to 'covered' due to the proven evidence
    coverage = compute_criterion_coverage(evidence)
    assert coverage == "covered", (
        f"Coverage should be 'covered' with proven task_merge evidence, got '{coverage}'"
    )


@pytest.mark.asyncio
async def test_orig_task_with_promotion_target_merge_withholds_pr_merged(
    db_session, mocker
):
    """
    When the merged PR lands on the project's promotion target instead of its
    integration branch, pr_merged must stay unset and the branch-safety alert
    must be staged.
    """
    import app.branch_resolution as branch_resolution
    from app.branch_resolution import flush_pending_branch_alerts
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from sqlmodel import select

    # The synchronous resolver records the branch-misconfiguration alert intent
    # off the hot path (no inline DB I/O); the durable persist runs in an
    # async-safe context. Reset the process-local queue + dedupe set so this
    # test sees only its own enqueue (mirrors test_shared_state isolation).
    branch_resolution._blocking_project_branch_alerts.clear()
    branch_resolution._pending_durable_branch_alerts.clear()

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.github.merge_pull_request", return_value={"merged": True})

    project = Project(
        name="Promotion target merge",
        user_id="test-user",
        integration_branch="main",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-004",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "base_branch": "release",
            "original_task_id": str(orig_task.id),
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    await db_session.refresh(orig_task)
    assert orig_task.status == TaskStatus.COMPLETED
    assert (orig_task.context or {}).get("pr_merged") is not True

    # Drain the deferred branch-alert queue into durable Alerts the way the
    # orchestrator monitoring loop does, then assert the operator signal landed.
    await flush_pending_branch_alerts(db_session)
    await db_session.commit()

    alert = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "Blocking branch configuration",
                )
            )
        )
        .scalars()
        .first()
    )
    assert alert is not None
    assert "promotion target" in alert.message


@pytest.mark.asyncio
async def test_orig_task_evaluating_without_ci_waiting_completes(db_session, mocker):
    """
    When merge succeeds and ci_orig_id is set, if the original task is EVALUATING
    and does NOT have ci_waiting set, it should also be marked COMPLETED.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
async def test_merge_success_without_orig_id_triggers_build_config_refresh(
    db_session, mocker
):
    """A CI-driven merge onto the integration branch refreshes empty
    build_config even when no original_task_id linkage exists."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.github.merge_pull_request", return_value={"merged": True})
    refresh_mock = mocker.patch(
        "app.ci_completion._refresh_empty_build_config_after_integration_landing",
        new_callable=AsyncMock,
        return_value=False,
    )

    project = Project(
        name="No orig id refresh",
        user_id="test-user",
        github_owner="acme",
        github_repo="direct-merge",
        integration_branch="main",
        build_config={"repo_is_empty": True},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    refresh_mock.assert_awaited_once_with(project.id, "main")


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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "clean"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": False, "message": "different error"},
    )

    project = Project(name="Different errors", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    # 2026-08-20: this spawn now DOES set fix_reason. The previous assertion
    # pinned its ABSENCE purely as a call-signature fingerprint (see the old
    # comment: "identifiable by the absence of update_branch_failed/
    # merge_conflict") — the subject of this test is suppress_warm_handoff, not
    # the label. The fingerprint moves to the prompt, which is unique to this
    # site. Flagging the self-certifying shape explicitly: this diff changes
    # both the emitted value and the assertion on it, so the independent
    # evidence lives elsewhere — the provenance guards in test_pipeline.py and
    # the agent-side tests in agent/src/handlers/review-fix.test.ts that make
    # the obsolete-exit conditional on the label.
    spawn_mock.assert_called_once()
    kw = spawn_mock.call_args.kwargs
    assert "GitHub rejected the merge" in kw.get("fix_prompt", ""), (
        "wrong spawn site fired — this test targets the merge-blocked rebase path"
    )
    assert kw.get("fix_reason") == "merge_conflict", (
        "the merge-blocked rebase spawn is conflict-only work; it must be "
        "labelled so the agent's obsolete-exit may fire when a sibling merge "
        "already cleared the block"
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
    db_session, expire_fidelity, mocker
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
                update(Task).where(Task.id == task.id).values(status=TaskStatus.FAILED)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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
                "review_not_required": True,
                "review_policy_reason": "test fixture explicit no-review policy",
                "head_sha": f"test-head-{pr_number}",
                "base_sha": f"test-base-{pr_number}",
                "ci_checked_head_sha": f"test-head-{pr_number}",
                "ci_checked_base_sha": f"test-base-{pr_number}",
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": pr_number,
                    "base_sha": f"test-base-{pr_number}",
                    "head_sha": f"test-head-{pr_number}",
                },
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
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


# ---------------------------------------------------------------------------
# Pre-merge Alembic dual-head guard
# ---------------------------------------------------------------------------

# A diff that adds a migration file whose revision id ("0002") collides with an
# existing revision already on the target branch — the dual-head vector.
_DUAL_HEAD_DIFF = (
    "diff --git a/backend/alembic/versions/0002_dup.py "
    "b/backend/alembic/versions/0002_dup.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/backend/alembic/versions/0002_dup.py\n"
    '+"""dup migration"""\n'
    '+revision = "0002"\n'
    '+down_revision = "0001"\n'
)


@pytest.mark.asyncio
async def test_merge_dual_head_migration_spawns_guard_fix(db_session, mocker):
    """A deterministic dual-head block spawns a migration fix and exits CI wait."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.github.create_pr_comment", return_value=None)
    # Guard sees a touched migration file, a colliding diff, and an existing
    # chain that already owns revision "0002".
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            return_value=[
                {
                    "filename": "backend/alembic/versions/0002_dup.py",
                    "status": "added",
                    "patch": '+"""dup migration"""\n+revision = "0002"\n+down_revision = "0001"',
                }
            ]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        AsyncMock(return_value=_DUAL_HEAD_DIFF),
    )
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "test-base-42", "ref": "main"},
                "head": {
                    "sha": "test-head-42",
                    "ref": "blueprint/test-branch",
                },
            }
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        AsyncMock(return_value=["0001_init.py", "0002_existing.py"]),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-42", "ref": "automain"},
                "head": {"sha": "head-42", "ref": "blueprint/test-branch"},
            }
        ),
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(return_value=(fake_fix, True)),
    )

    project = Project(name="dual-head hold", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Guard must hold the merge on a dual-head collision"
    merge_mock.assert_not_called()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs.get("fix_reason") == "migration_guard"
    assert spawn_mock.await_args.kwargs.get("fix_target") == {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "base-42",
        "head_sha": "head-42",
    }
    assert spawn_mock.await_args.kwargs.get("suppress_warm_handoff") is True
    assert "0002" in (spawn_mock.await_args.kwargs.get("fix_prompt") or "")
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is False
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("migration_guard_state") == "fix_spawned"
    assert review_loop.get("migration_guard_fix_spawned") == str(fake_fix.id)
    assert review_loop.get("ci_fix_spawned") == str(fake_fix.id)
    assert "_migration_finalization" not in (task.context or {})
    assert "spawned migration fix" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_merge_dual_head_fix_spawn_defer_retries_ci_wait(db_session, mocker):
    """A transient migration-fix spawn deferral remains retryable."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.review_pipeline import SPAWN_REASON_DEFERRED, SPAWN_REASON_KEY

    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "test-base-42", "ref": "main"},
                "head": {
                    "sha": "test-head-42",
                    "ref": "blueprint/test-branch",
                },
            }
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            return_value=[
                {
                    "filename": "backend/alembic/versions/0002_dup.py",
                    "status": "added",
                    "patch": '+"""dup migration"""\n+revision = "0002"\n+down_revision = "0001"',
                }
            ]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        AsyncMock(return_value=_DUAL_HEAD_DIFF),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        AsyncMock(return_value=["0001_init.py", "0002_existing.py"]),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )

    async def _defer_spawn(source_task, *_args, **_kwargs):
        source_task.context = {
            **(source_task.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_DEFERRED,
        }
        return None, False

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(side_effect=_defer_spawn),
    )

    project = Project(name="dual-head deferred fix spawn", user_id="test-user")
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
    merge_mock.assert_not_called()
    spawn_mock.assert_awaited_once()
    assert task.status == TaskStatus.EVALUATING
    assert task.requires_approval is not True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_passed") is True
    assert review_loop.get("migration_guard_state") == "fix_deferred"
    assert review_loop.get("migration_guard_fix_spawned") is None
    assert "spawn was deferred" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_migration_guard_moved_pr_replaces_queued_stale_fix(db_session, mocker):
    """A moved PR replaces an unclaimed stale fix without retargeting it."""
    from app.migration_guard_recovery import spawn_migration_guard_fix_task

    project = Project(name="migration fix immutable target", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    source = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "review_target": {
                "project_id": str(project.id),
                "pr_number": 42,
                "base_sha": "old-base",
                "head_sha": "old-head",
            }
        },
    )
    old_target = {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "old-base",
        "head_sha": "old-head",
    }
    active_fix = Task(
        project_id=project.id,
        title="existing migration fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.QUEUED,
        context={
            "review_loop": {
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "base_branch": "automain",
                "fix_target": old_target,
            }
        },
    )
    db_session.add(active_fix)
    await db_session.commit()

    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "new-base", "ref": "automain"},
                "head": {"sha": "new-head", "ref": "blueprint/test-branch"},
            }
        ),
    )
    replacement = Task(
        project_id=project.id,
        title="replacement migration fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.QUEUED,
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(return_value=(replacement, True)),
    )

    spawned, created = await spawn_migration_guard_fix_task(
        source,
        db_session,
        pr_number=42,
        branch="blueprint/test-branch",
        base_branch="automain",
        guard_reason="duplicate revision",
        review_loop=(source.context or {}).get("review_loop"),
        gh_token="fake-token",
        owner="owner",
        repo="repo",
    )

    assert spawned is replacement
    assert created is True
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs["fix_target"] == {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "new-base",
        "head_sha": "new-head",
    }
    await db_session.refresh(active_fix)
    assert (active_fix.context or {})["review_loop"]["fix_target"] == old_target
    assert active_fix.status == TaskStatus.CANCELLED
    assert (active_fix.context or {}).get("failure_detail") == (
        "superseded_by_moved_pr_target"
    )
    assert source.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_migration_guard_base_advance_reuses_same_head_fix(db_session, mocker):
    """Base-only movement does not supersede a queued same-head fix."""
    from app.migration_guard_recovery import spawn_migration_guard_fix_task

    project = Project(name="migration fix ambient base", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    old_target = {
        "project_id": str(project.id),
        "pr_number": 43,
        "base_sha": "old-base",
        "head_sha": "same-head",
    }
    source = _make_task(
        db_session,
        project,
        pr_number=43,
        ci_loop_extra={"review_target": old_target},
    )
    active_fix = Task(
        project_id=project.id,
        title="existing same-head migration fix",
        payloads=[{"type": "fix", "pr_number": 43}],
        status=TaskStatus.QUEUED,
        context={"review_loop": {"fix_target": old_target}},
    )
    db_session.add(active_fix)
    await db_session.commit()

    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "new-base", "ref": "automain"},
                "head": {"sha": "same-head", "ref": "blueprint/test-branch"},
            }
        ),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )

    existing, created = await spawn_migration_guard_fix_task(
        source,
        db_session,
        pr_number=43,
        branch="blueprint/test-branch",
        base_branch="automain",
        guard_reason="duplicate revision",
        review_loop=(source.context or {}).get("review_loop"),
        gh_token="fake-token",
        owner="owner",
        repo="repo",
    )

    assert existing is active_fix
    assert created is False
    spawn_mock.assert_not_awaited()
    await db_session.refresh(active_fix)
    assert active_fix.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_migration_guard_moved_pr_retries_after_running_stale_fix_finishes(
    db_session, mocker
):
    """A running old-target fix defers without leaving a permanent skip latch."""
    from app.migration_guard_recovery import spawn_migration_guard_fix_task

    project = Project(name="migration running stale target", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    old_target = {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "old-base",
        "head_sha": "old-head",
    }
    source = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "review_target": old_target,
            "review_approved": False,
            "ci_fix_spawned": "old-fix-id",
            "migration_guard_fix_spawned": "old-fix-id",
        },
    )
    old_fix = Task(
        project_id=project.id,
        title="running old-target migration fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.IN_PROGRESS,
        context={"review_loop": {"fix_target": old_target}},
    )
    db_session.add(old_fix)
    await db_session.commit()

    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "new-base", "ref": "automain"},
                "head": {"sha": "new-head", "ref": "blueprint/test-branch"},
            }
        ),
    )
    replacement = Task(
        project_id=project.id,
        title="current-target migration fix",
        payloads=[{"type": "fix", "pr_number": 42}],
        status=TaskStatus.QUEUED,
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(return_value=(replacement, True)),
    )

    deferred, created = await spawn_migration_guard_fix_task(
        source,
        db_session,
        pr_number=42,
        branch="blueprint/test-branch",
        base_branch="automain",
        guard_reason="duplicate revision",
        review_loop=(source.context or {}).get("review_loop"),
        gh_token="fake-token",
        owner="owner",
        repo="repo",
    )

    assert deferred is None and created is False
    spawn_mock.assert_not_awaited()
    deferred_loop = (source.context or {})["review_loop"]
    assert deferred_loop["migration_guard_state"] == "fix_deferred"
    assert deferred_loop["review_approved"] is False
    assert "ci_fix_spawned" not in deferred_loop
    assert "migration_guard_fix_spawned" not in deferred_loop

    old_fix.status = TaskStatus.COMPLETED
    db_session.add(old_fix)
    await db_session.commit()

    spawned, created = await spawn_migration_guard_fix_task(
        source,
        db_session,
        pr_number=42,
        branch="blueprint/test-branch",
        base_branch="automain",
        guard_reason="duplicate revision",
        review_loop=(source.context or {}).get("review_loop"),
        gh_token="fake-token",
        owner="owner",
        repo="repo",
    )

    assert spawned is replacement and created is True
    assert spawn_mock.await_args.kwargs["fix_target"]["head_sha"] == "new-head"


@pytest.mark.asyncio
async def test_migration_guard_target_lookup_defer_clears_stale_fix_latches(
    db_session, mocker
):
    """A failed live lookup remains retryable while merge authority stays revoked."""
    from app.migration_guard_recovery import spawn_migration_guard_fix_task

    project = Project(name="migration target lookup retry", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    source = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "review_approved": False,
            "ci_fix_spawned": "stale-fix-id",
            "migration_guard_fix_spawned": "stale-fix-id",
        },
    )
    await db_session.commit()
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(side_effect=RuntimeError("temporary GitHub failure")),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock()
    )

    fix, created = await spawn_migration_guard_fix_task(
        source,
        db_session,
        pr_number=42,
        branch="blueprint/test-branch",
        base_branch="automain",
        guard_reason="duplicate revision",
        review_loop=(source.context or {}).get("review_loop"),
        gh_token="fake-token",
        owner="owner",
        repo="repo",
    )

    assert fix is None and created is False
    spawn_mock.assert_not_awaited()
    loop = (source.context or {})["review_loop"]
    assert loop["migration_guard_state"] == "fix_deferred"
    assert loop["ci_waiting"] is True
    assert loop["review_approved"] is False
    assert "ci_fix_spawned" not in loop
    assert "migration_guard_fix_spawned" not in loop


@pytest.mark.asyncio
async def test_merge_hold_auto_finalizes_reserved_migration_and_requeues_ci(
    db_session, mocker
):
    """When the blocked PR owns an ACTIVE reservation, the CI merge path
    rewrites the branch, records the finalization payload, and waits for fresh
    CI instead of attempting the stale merge."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(return_value=(False, "dual-head: duplicate migration revision")),
    )
    finalization_mock = mocker.patch(
        "app.rollup_management.attempt_pr_migration_finalization",
        new=AsyncMock(
            return_value={
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "old_revision": "0153",
                "finalized_revision": "0152",
                "finalized_down_revision": "0151",
                "expected_schema_revision": "0152",
            }
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    project = Project(name="dual-head auto-finalize", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={
            "head_sha": "deadbeef",
            "polls_since_pr_fetch": 2,
            "mergeable_state": "clean",
            "last_merge_msg": "old merge result",
        },
    )
    await db_session.commit()
    await db_session.refresh(task)
    await allocate_migration_reservation(db_session, task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False
    merge_mock.assert_not_called()
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("head_sha") == ""
    assert review_loop.get("polls_since_pr_fetch") == 999
    assert "mergeable_state" not in review_loop
    assert "last_merge_msg" not in review_loop
    finalization = (task.context or {}).get("_migration_finalization")
    assert finalization is not None
    assert finalization["old_revision"] == "0153"
    assert finalization["finalized_revision"] == "0152"
    finalization_mock.assert_awaited_once()
    assert finalization_mock.await_args.kwargs["promotion_target"] == "main"
    assert (
        "auto-finalized provisional alembic migration"
        in (task.latest_status_message or "").lower()
    )


@pytest.mark.asyncio
async def test_merge_held_on_migration_fetch_failure(db_session, mocker):
    """A GitHub fetch failure during the guard DEFERS — fail-safe, no merge."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(side_effect=RuntimeError("github 503")),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(return_value=(None, False)),
    )

    project = Project(name="guard defer", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_waiting": True, "ci_passed": True},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "A failed safety fetch must hold the merge (defer)"
    merge_mock.assert_not_called()
    spawn_mock.assert_not_called()
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("migration_guard_state") == "deferred"
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_passed") is True


@pytest.mark.asyncio
async def test_migration_guard_hold_reason_includes_http_exception_detail(
    db_session, mocker
):
    """HTTPException.detail is included in the hold-reason status message.

    When github.get_pr_files raises an HTTPException (e.g. a 502 from
    upstream), the hold reason surfaced in task.latest_status_message must
    contain the detail string — not a bare 'HTTPException: ' with nothing
    after the colon.
    """
    from fastapi import HTTPException

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            side_effect=HTTPException(
                status_code=502, detail="GitHub 502: upstream error"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        AsyncMock(return_value=(None, False)),
    )

    project = Project(name="guard http exc", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"ci_waiting": True, "ci_passed": True},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "HTTPException from guard must hold the merge"
    merge_mock.assert_not_called()
    spawn_mock.assert_not_called()
    status_msg = task.latest_status_message or ""
    assert "upstream error" in status_msg, (
        f"HTTPException detail must appear in status message, got: {status_msg!r}"
    )
    assert "HTTPException: " not in status_msg or "HTTPException(502)" in status_msg, (
        "Must not render as bare 'HTTPException: ' with empty detail"
    )


@pytest.mark.asyncio
async def test_clean_migration_pr_merges(db_session, mocker):
    """A migration PR that extends the chain cleanly merges normally."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    clean_diff = (
        "diff --git a/backend/alembic/versions/0003_next.py "
        "b/backend/alembic/versions/0003_next.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/backend/alembic/versions/0003_next.py\n"
        '+"""next migration"""\n'
        '+revision = "0003"\n'
        '+down_revision = "0002"\n'
    )
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            return_value=[
                {
                    "filename": "backend/alembic/versions/0003_next.py",
                    "status": "added",
                    "patch": '+"""next migration"""\n+revision = "0003"\n+down_revision = "0002"',
                }
            ]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        AsyncMock(return_value=clean_diff),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        AsyncMock(return_value=["0001_init.py", "0002_existing.py"]),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )

    project = Project(name="clean migration merge", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "A clean chain extension must merge"
    merge_mock.assert_called_once()
    assert task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Rollup-orig ancestry gate before stamping the shipped badge
# (verify_rollup_merge_landed integrated into the original-merge-success path)
# ---------------------------------------------------------------------------


def _patch_ancestry_gate_deps(
    mocker, *, is_ancestor, merge_commit_sha="deadbeefcafe1234"
):
    """Patch the GitHub seams the orig ancestry gate consults.

    The gate resolves project GitHub creds, fetches the merged PR for its
    ``merge_commit_sha``, then calls ``is_ancestor_of``. Returns the
    ``is_ancestor_of`` mock so callers can assert it ran.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "merge_commit_sha": merge_commit_sha},
    )
    return mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=is_ancestor,
    )


async def _run_orig_merge_with_gate(db_session, mocker, *, is_ancestor):
    """Drive the merge-success path for a rollup-orig with the ancestry gate
    armed to return ``is_ancestor``. Returns the refreshed orig task plus its
    alerts."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )
    ancestry_mock = _patch_ancestry_gate_deps(mocker, is_ancestor=is_ancestor)

    project = Project(name="Orig ancestry gate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-gate",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result is True

    await db_session.refresh(orig_task)
    alerts = (
        (await db_session.execute(_select(Alert).where(Alert.task_id == orig_task.id)))
        .scalars()
        .all()
    )
    return orig_task, alerts, ancestry_mock


@pytest.mark.asyncio
async def test_orig_not_stamped_when_merge_commit_not_ancestor(db_session, mocker):
    """is_ancestor_of -> False: the rollup merge commit never reached the
    integration branch, so pr_merged/shipped must NOT be stamped on the orig; a
    durable stranded marker is recorded and an operator alert fires."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY

    # Let _maybe_create_failure_alert actually create the alert; silence its
    # broadcast / external-notification side effects.
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    orig_task, alerts, ancestry_mock = await _run_orig_merge_with_gate(
        db_session, mocker, is_ancestor=False
    )

    ancestry_mock.assert_awaited()
    ctx = orig_task.context or {}
    assert ctx.get("pr_merged") is None, (
        "a non-ancestor merge commit must NOT stamp the shipped badge"
    )
    assert ctx.get("landed_on_integration") is False
    assert STRANDED_LANDING_REASON_KEY in ctx
    assert len(alerts) == 1, "a stranded rollup landing must raise one operator alert"


@pytest.mark.asyncio
async def test_orig_indeterminate_ancestry_preserves_flag_trust(db_session, mocker):
    """is_ancestor_of -> None: a transient/indeterminate ancestry result must
    PRESERVE the current flag-trust stamping (pr_merged=True) and NOT strand or
    alert, so a GitHub outage never blocks rollup completion."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    orig_task, alerts, ancestry_mock = await _run_orig_merge_with_gate(
        db_session, mocker, is_ancestor=None
    )

    ancestry_mock.assert_awaited()
    ctx = orig_task.context or {}
    assert ctx.get("pr_merged") is True, (
        "indeterminate ancestry must preserve flag-trust stamping"
    )
    assert STRANDED_LANDING_REASON_KEY not in ctx
    assert alerts == []
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_orig_positive_ancestry_stamps_as_before(db_session, mocker):
    """is_ancestor_of -> True: a confirmed integration landing stamps pr_merged
    exactly as before the gate existed, with no stranded marker or alert."""
    from app.landed_signal import STRANDED_LANDING_REASON_KEY

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    orig_task, alerts, ancestry_mock = await _run_orig_merge_with_gate(
        db_session, mocker, is_ancestor=True
    )

    ancestry_mock.assert_awaited()
    ctx = orig_task.context or {}
    assert ctx.get("pr_merged") is True
    assert ctx.get("landed_on_integration") is True
    assert STRANDED_LANDING_REASON_KEY not in ctx
    assert alerts == []
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_orig_ancestry_gate_degrades_to_indeterminate_on_github_exception(
    db_session, mocker
):
    """When is_ancestor_of raises (e.g. network timeout), the orig ancestry gate
    must degrade to indeterminate and preserve flag-trust: the orig still receives
    pr_merged=True and must NOT be stranded or alert.

    A transient GitHub exception in the ancestry check must behave identically to
    is_ancestor_of returning None — the last-known credit state is preserved and
    the gate re-checks on the next cycle.
    """
    from app.landed_signal import STRANDED_LANDING_REASON_KEY

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "merge_commit_sha": "deadbeefcafe1234"},
    )
    # Simulate a transient GitHub API failure on the ancestry check
    mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        side_effect=Exception("connection timeout"),
    )

    project = Project(name="Ancestry gate exception", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "do something"}],
        context={},
        vm_instance_id="agent-vm-orig-exc",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    await db_session.refresh(orig_task)
    ctx = orig_task.context or {}
    # Exception in is_ancestor_of must degrade to indeterminate → preserves flag-trust
    assert ctx.get("pr_merged") is True, (
        "Ancestry gate must preserve pr_merged when is_ancestor_of raises — "
        "a transient GitHub exception must degrade to indeterminate, not strand the task"
    )
    assert STRANDED_LANDING_REASON_KEY not in ctx, (
        "A transient GitHub exception must NOT record a durable stranded marker"
    )
    alert_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Guard: ci_errors must never produce an empty code block in the PR comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_failure_comment_never_has_empty_code_block(db_session, mocker):
    """Regression for CI poll path posting empty ``` code blocks.

    Before the fix, the orchestrator hardcoded failed_logs='' in the ci_result
    for the red path, which caused handle_ci_completion to post:
        **Blueprint CI**: Checks failed.

        ```

        ```

    Fix agents had no failing-test selectors so they pushed blind guesses and
    burned the review-fix iteration cap (incident 2026-06-10, PRs #9308/#9353).

    The guard in handle_ci_completion must substitute failing check names (or a
    fallback message) whenever failed_logs is blank, ensuring the code block is
    never empty.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    comment_mock = mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )

    project = Project(name="CI failure comment fallback", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cases = [
        (
            {
                "failed_logs": "",
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "ci-build", "conclusion": "failure"}],
            },
            "CI checks failed: ci-build",
        ),
        (
            {
                "failed_logs": None,
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            },
            "no detail available",
        ),
        (
            {
                "failed_logs": "   ",
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            },
            "no detail available",
        ),
        (
            {
                "failed_logs": "",
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {"name": "backend", "conclusion": "failure"},
                    {"name": "frontend", "conclusion": "failure"},
                    {"name": "lint", "conclusion": "success"},  # non-failure excluded
                ],
            },
            "CI checks failed: backend, frontend",
        ),
    ]
    for index, (ci_result, expected_substring) in enumerate(cases, start=1):
        task = _make_task(db_session, project, pr_number=8000 + index)
        await db_session.commit()
        await db_session.refresh(task)

        await handle_ci_completion(
            task=task,
            ci_result=ci_result,
            gh_token="fake-token",
            owner="owner",
            repo="repo",
            session=db_session,
            source="poll",
        )

        comment_body = comment_mock.call_args_list[-1].args[4]
        assert "```\n\n```" not in comment_body, (
            f"Empty code block for ci_result={ci_result!r}: {comment_body!r}"
        )
        assert expected_substring in comment_body, (
            f"Expected {expected_substring!r} in comment for ci_result={ci_result!r}: {comment_body!r}"
        )


# ===========================================================================
# SITE A lock-window fix: precomputed_migration_guard skips inline guard call
# ===========================================================================


def _make_ci_waiting_task(session, project, *, pr_number: int = 1001) -> Task:
    """Create a minimal EVALUATING task with ci_waiting+ci_passed+review_approved."""
    task = Task(
        project_id=project.id,
        title="test-task",
        payloads=[{"type": "create", "base_branch": "automain"}],
        vm_instance_id=f"vm-guard-{pr_number}",
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": f"blueprint/t-{pr_number}",
                "base_branch": "automain",
                "ci_waiting": True,
                "ci_passed": True,
                "review_approved": True,
                "review_not_required": True,
                "review_policy_reason": "test fixture explicit no-review policy",
            }
        },
    )
    session.add(task)
    return task


@pytest.mark.asyncio
async def test_precomputed_migration_guard_skips_inline_enforce_call(
    db_session, mocker
):
    """When precomputed_migration_guard is supplied, enforce_premerge_migration_guard
    must NOT be called inside _attempt_merge_with_retry_and_escalation.

    This is the SITE A lock-window fix: the rollup-handoff path resolves the
    guard before acquiring FOR UPDATE on the rollup parent, then passes the
    result in so the function does not re-issue GitHub calls under the row lock.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    project = Project(
        name="Guard-precomputed test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        github_installation_id="inst-guard-1",
    )
    db_session.add(project)
    await db_session.flush()
    task = _make_ci_waiting_task(db_session, project, pr_number=5500)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)

    guard_mock = mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(return_value=(True, None)),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": "abc123" * 6}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "abc123" * 6, "ref": "blueprint/t-5500"},
                "base": {"ref": "automain"},
                "number": 5500,
                "html_url": "http://pr/5500",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )

    # Pass a pre-computed guard result bound to the same merge target the
    # chokepoint resolves ("automain") — guard must NOT be called inline.
    await _attempt_merge_with_retry_and_escalation(
        task,
        "gh-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_migration_guard=PremergeMigrationGuardResult(
            "ok", None, target_branch="automain"
        ),
    )

    guard_mock.assert_not_called()


@pytest.mark.asyncio
async def test_precomputed_migration_guard_for_other_target_is_not_adopted(
    db_session, mocker
):
    """A guard verdict evaluated against a different base must be recomputed.

    The pre-lock site resolves the merge target from a narrower view of the
    parent task than this chokepoint does. Adopting its verdict unconditionally
    would let a verdict about branch X authorize a merge into branch Y.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    project = Project(
        name="Guard mismatched-target test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        github_installation_id="inst-guard-3",
    )
    db_session.add(project)
    await db_session.flush()
    task = _make_ci_waiting_task(db_session, project, pr_number=5502)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)

    guard_mock = mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(
            return_value=PremergeMigrationGuardResult(
                "ok", None, target_branch="automain"
            )
        ),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": "abc123" * 6}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "abc123" * 6, "ref": "blueprint/t-5502"},
                "base": {"ref": "automain"},
                "number": 5502,
                "html_url": "http://pr/5502",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )

    await _attempt_merge_with_retry_and_escalation(
        task,
        "gh-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        # Verdict about a DIFFERENT branch than the resolved "automain" base.
        precomputed_migration_guard=PremergeMigrationGuardResult(
            "ok", None, target_branch="release-v99"
        ),
    )

    guard_mock.assert_called_once()
    assert guard_mock.call_args.args[4] == "automain"


@pytest.mark.asyncio
async def test_legacy_tuple_migration_guard_is_not_adopted(db_session, mocker):
    """An unbound legacy tuple names no target, so it cannot skip the guard."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    project = Project(
        name="Guard legacy-tuple test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        github_installation_id="inst-guard-4",
    )
    db_session.add(project)
    await db_session.flush()
    task = _make_ci_waiting_task(db_session, project, pr_number=5503)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)

    guard_mock = mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(
            return_value=PremergeMigrationGuardResult(
                "ok", None, target_branch="automain"
            )
        ),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": "abc123" * 6}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "abc123" * 6, "ref": "blueprint/t-5503"},
                "base": {"ref": "automain"},
                "number": 5503,
                "html_url": "http://pr/5503",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )

    await _attempt_merge_with_retry_and_escalation(
        task,
        "gh-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_migration_guard=(True, None),
    )

    guard_mock.assert_called_once()


@pytest.mark.asyncio
async def test_no_precomputed_guard_calls_enforce_inline(db_session, mocker):
    """Without precomputed_migration_guard, enforce_premerge_migration_guard
    is called inline as before (backwards-compatible behaviour).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    project = Project(
        name="Guard-inline test",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        github_installation_id="inst-guard-2",
    )
    db_session.add(project)
    await db_session.flush()
    task = _make_ci_waiting_task(db_session, project, pr_number=5501)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)

    guard_mock = mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new=AsyncMock(return_value=(True, None)),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": "abc123" * 6}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "head": {"sha": "abc123" * 6, "ref": "blueprint/t-5501"},
                "base": {"ref": "automain"},
                "number": 5501,
                "html_url": "http://pr/5501",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )

    # No precomputed guard — the inline enforce call must fire.
    await _attempt_merge_with_retry_and_escalation(
        task,
        "gh-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    guard_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Onboarding-proof: first merge-gate terminal verdict stamp
#
# Verifies the truthfulness invariant wired at the merge-success site:
#   * a verified merge (no unverified_merge_reason) stamps
#     first_merge_gate_executed_tests = True and no fall-open reason;
#   * an unverified merge (gate fell open) stamps
#     first_merge_gate_executed_tests = False AND the honest reason;
#   * the stamp is idempotent — a later verdict never overwrites the first.
# ---------------------------------------------------------------------------


def _patch_merge_success(mocker):
    """Patch the merge hot path so a single merge call lands successfully."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.alert_dispatch._maybe_create_unverified_merge_alert",
        AsyncMock(return_value=None),
    )
    return mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "deadbeef"},
    )


@pytest.mark.asyncio
async def test_first_merge_gate_verdict_stamped_when_real_tests_executed(
    db_session, mocker
):
    """A verified merge (no unverified reason) stamps executed_tests=True."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _patch_merge_success(mocker)

    project = Project(name="gate verdict verified", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    bc = project.build_config or {}
    assert bc.get("first_merge_gate_verdict_at") is not None
    assert bc.get("first_merge_gate_executed_tests") is True
    # No fall-open reason recorded when the gate ran real tests.
    assert bc.get("first_merge_gate_unverified_reason") is None


@pytest.mark.asyncio
async def test_first_merge_gate_verdict_stamped_false_when_gate_fell_open(
    db_session, mocker
):
    """An unverified merge stamps executed_tests=False AND the honest reason."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _patch_merge_success(mocker)

    project = Project(name="gate verdict unverified", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"unverified_merge_reason": "no_test_command"},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    bc = project.build_config or {}
    assert bc.get("first_merge_gate_verdict_at") is not None
    # Truthfulness invariant: gate fell open ⇒ executed_tests must be False.
    assert bc.get("first_merge_gate_executed_tests") is False
    assert bc.get("first_merge_gate_unverified_reason") == "no_test_command"


@pytest.mark.asyncio
async def test_first_merge_gate_verdict_is_idempotent(db_session, mocker):
    """A later verdict never overwrites the first stamp's truthful values."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _patch_merge_success(mocker)

    # Pre-stamp a prior VERIFIED verdict; the merge below carries an unverified
    # reason. Idempotency must preserve the original True verdict and timestamp.
    project = Project(
        name="gate verdict idempotent",
        user_id="test-user",
        build_config={
            "first_merge_gate_verdict_at": "2020-01-01T00:00:00",
            "first_merge_gate_executed_tests": True,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"unverified_merge_reason": "no_test_command"},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    bc = project.build_config or {}
    assert bc.get("first_merge_gate_verdict_at") == "2020-01-01T00:00:00"
    assert bc.get("first_merge_gate_executed_tests") is True
    # The fall-open reason from the second (skipped) verdict was NOT recorded.
    assert bc.get("first_merge_gate_unverified_reason") is None


# ---------------------------------------------------------------------------
# Review-wait throttle and orphan backstop (handle_ci_completion parked path)
# ---------------------------------------------------------------------------


_REVIEW_WAIT_HEAD_SHA = "review-wait-head"
_REVIEW_WAIT_BASE_SHA = "review-wait-base"


def _make_task_no_review_approved(
    db_session, project, pr_number: int = 42, ci_loop_extra: dict | None = None
):
    """Like _make_task but WITHOUT review_approved — forces the review-wait parked path."""
    loop = {
        "pr_number": pr_number,
        "branch": "blueprint/test-branch",
        "base_branch": "main",
        "ci_waiting": True,
        "iteration": 1,
    }
    if ci_loop_extra:
        loop.update(ci_loop_extra)
    task = Task(
        project_id=project.id,
        title="CI review-wait test task",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
            }
        ],
        context={"review_loop": loop},
        vm_instance_id=f"agent-vm-rw-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


@pytest.mark.asyncio
async def test_review_wait_stamps_ci_last_polled_and_raises_poll_interval(
    db_session, mocker
):
    """When CI passes but review is not yet approved, the parked path must
    stamp ci_last_polled and raise ci_poll_interval to >= 120s."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Review wait throttle", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task_no_review_approved(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_last_polled") is not None, (
        "ci_last_polled must be stamped when parked in review-wait"
    )
    assert (review_loop.get("ci_poll_interval") or 0) >= 120, (
        "ci_poll_interval must be raised to >= 120s when parked in review-wait"
    )


@pytest.mark.asyncio
async def test_review_wait_does_not_spawn_before_backstop_timeout(db_session, mocker):
    """First-green review-wait remains a park, not a second spawn trigger."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )

    project = Project(name="Review wait no first-green spawn", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task_no_review_approved(db_session, project)
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    spawn_mock.assert_not_awaited()
    await db_session.refresh(task)
    assert task.requires_approval is not True
    assert not (task.context or {})["review_loop"].get("review_approved")


@pytest.mark.asyncio
async def test_review_wait_started_set_on_first_entry_not_overwritten(
    db_session, mocker
):
    """review_wait_started is stamped on first entry into the parked path and
    must NOT be overwritten on subsequent polls."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Review wait started idempotent", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task_no_review_approved(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}

    # First poll — review_wait_started should be stamped.
    await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )
    rw_started_first = (
        (task.context or {}).get("review_loop", {}).get("review_wait_started")
    )
    assert rw_started_first is not None, (
        "review_wait_started must be set on first entry"
    )

    # Second poll — simulate a fresh handle_ci_completion call.  We keep the
    # same task object (context already has review_wait_started) and call again.
    result2 = await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result2 is True
    rw_started_second = (
        (task.context or {}).get("review_loop", {}).get("review_wait_started")
    )
    assert rw_started_second == rw_started_first, (
        "review_wait_started must NOT be overwritten on subsequent polls"
    )


@pytest.mark.asyncio
async def test_review_wait_backstop_fires_after_timeout(db_session, mocker):
    """After the 60-minute backstop threshold, when respawn is suppressed,
    requires_approval and review_wait_escalated are set, and a failure alert
    is created."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import SPAWN_REASON_CAP

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    # Respawn-first runs before the human gate; mock it as suppressed
    # (None, False) so the requires_approval fallback is what we assert here.
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(
            side_effect=_canonical_review_wait_spawn(
                (None, False), reason=SPAWN_REASON_CAP
            )
        ),
    )

    project = Project(name="Review wait backstop", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # _utcnow() returns naive UTC; review_wait_started must match so the
    # subtraction doesn't raise TypeError (caught → silent backstop skip).
    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_escalated") is True, (
        "review_wait_escalated must be set after 60-minute backstop"
    )
    assert task.requires_approval is True, (
        "requires_approval must be set after 60-minute backstop"
    )
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_review_wait_backstop_does_not_refire(db_session, mocker):
    """Once review_wait_escalated=True, the backstop must NOT fire again."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Review wait backstop idempotent", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Naive UTC timestamp matching _utcnow() format.
    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=120)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={
            "review_wait_started": old_started,
            "review_wait_escalated": True,
        },
    )
    task.requires_approval = True
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    # handle_ci_completion's review-wait park path always returns True (an action
    # was taken: the poll signal is persisted). The "does not refire" property is
    # asserted by alert_mock.assert_not_called() below — because review_wait_escalated
    # is already True, the backstop block is skipped and no second alert fires.
    # (Do NOT flip this to False: there is no requires_approval early-return in
    # handle_ci_completion; that gate lives in the separate merge helper.)
    assert result is True
    alert_mock.assert_not_called()


def _make_fake_review_task(
    project_id,
    *,
    review_target: dict | None = None,
    status: TaskStatus = TaskStatus.QUEUED,
):
    """A minimal stand-in for the freshly spawned review task."""
    return Task(
        id=uuid.uuid4(),
        project_id=project_id,
        title="Respawned review",
        payloads=[{"type": "review"}],
        status=status,
        context=(
            {"review_loop": {"review_target": dict(review_target)}}
            if review_target
            else {}
        ),
    )


def _review_wait_target(project_id, pr_number: int = 42) -> dict:
    return {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": _REVIEW_WAIT_BASE_SHA,
        "head_sha": _REVIEW_WAIT_HEAD_SHA,
    }


def _canonical_review_wait_spawn(result, *, reason: str | None = None):
    """Model the canonical spawner's exact source-authority boundary."""

    async def _spawn(source_task, _task_type, source_session, *args, **kwargs):
        authority = kwargs.get("source_spawn_authority")
        assert authority is not None
        authoritative_source = await authority(source_session)
        assert authoritative_source is not None
        if reason is not None:
            from app.review_pipeline import SPAWN_REASON_KEY

            authoritative_source.context = {
                **(authoritative_source.context or {}),
                SPAWN_REASON_KEY: reason,
            }
            source_session.add(authoritative_source)
        return result

    return _spawn


@pytest.mark.asyncio
async def test_review_wait_deferred_spawn_retries_after_resume(db_session, mocker):
    """A canonical admission defer resets the episode instead of escalating."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import (
        SPAWN_REASON_ADMISSION_PAUSED,
        SPAWN_REASON_KEY,
        SPAWN_REASON_SPAWNED,
    )

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Review wait pause resume", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = _review_wait_target(project.id)
    new_review = _make_fake_review_task(project.id, review_target=target)
    attempts = 0

    async def _spawn(source_task, _task_type, source_session, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        assert kwargs.get("review_target") == target
        authoritative_source = await kwargs["source_spawn_authority"](
            source_session
        )
        assert authoritative_source is not None
        ctx = dict(authoritative_source.context or {})
        ctx[SPAWN_REASON_KEY] = (
            SPAWN_REASON_ADMISSION_PAUSED
            if attempts == 1
            else SPAWN_REASON_SPAWNED
        )
        authoritative_source.context = ctx
        source_session.add(authoritative_source)
        if attempts == 1:
            return (None, False)
        return (new_review, True)

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock(side_effect=_spawn)
    )
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    first_loop = (task.context or {})["review_loop"]
    assert first_loop.get("review_wait_escalated") is not True
    assert first_loop.get("review_wait_started") != old_started
    assert task.requires_approval is not True
    alert_mock.assert_not_called()

    # Model the project resuming and the replacement backstop window elapsing.
    resumed_loop = dict(first_loop)
    resumed_loop["review_wait_started"] = old_started
    task.context = {**(task.context or {}), "review_loop": resumed_loop}
    db_session.add(task)
    await db_session.commit()

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    assert spawn_mock.await_count == 2
    await db_session.refresh(task)
    resumed_loop = (task.context or {})["review_loop"]
    assert resumed_loop.get("review_wait_respawn_task_id") == str(new_review.id)
    assert resumed_loop.get("review_wait_escalated") is not True
    assert task.requires_approval is not True
    alert_mock.assert_not_called()


@pytest.mark.parametrize(
    "retry_case",
    ["authority_contention", "preauthority_contention", "missing_base"],
)
@pytest.mark.asyncio
async def test_review_wait_transient_retry_preserves_elapsed_backstop(
    db_session, mocker, retry_case
):
    """Lock contention and an incomplete poll snapshot retry without +60m."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import SPAWN_REASON_CONTENDED

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name=f"Review wait retry {retry_case}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    if retry_case == "authority_contention":
        # Supported legacy approval shape: this used to raise while building
        # the exact claim because bool is not iterable.
        task.context = {**(task.context or {}), "touched_critical": True}
    await db_session.commit()

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(
            return_value=(None, False)
            if retry_case == "preauthority_contention"
            else None,
            side_effect=(
                _canonical_review_wait_spawn(
                    (None, False), reason=SPAWN_REASON_CONTENDED
                )
                if retry_case != "preauthority_contention"
                else None
            ),
        ),
    )
    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=(
            _REVIEW_WAIT_BASE_SHA if retry_case != "missing_base" else ""
        ),
    )

    assert result is True
    if retry_case == "missing_base":
        spawn_mock.assert_not_awaited()
    else:
        spawn_mock.assert_awaited_once()
    await db_session.refresh(task)
    review_loop = (task.context or {})["review_loop"]
    assert review_loop.get("review_wait_started") == old_started
    assert review_loop.get("ci_poll_interval") == 30
    assert review_loop.get("review_wait_escalated") is not True
    if retry_case == "missing_base":
        assert review_loop.get("polls_since_pr_fetch") == 999
    assert task.requires_approval is not True
    if retry_case == "authority_contention":
        assert (task.context or {}).get("touched_critical") is True
    alert_mock.assert_not_called()




@pytest.mark.parametrize("candidate_case", ["terminal", "wrong_target"])
@pytest.mark.asyncio
async def test_review_wait_dedup_dead_or_wrong_row_is_non_authority(
    db_session, mocker, candidate_case
):
    """A malformed canonical dedup result cannot open or stand in for review."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.closed_pr_reconciliation import REVIEW_WAIT_APPROVAL_HOLD_KEY
    from app.review_pipeline import SPAWN_REASON_DEDUP, SPAWN_REASON_KEY

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name=f"Review wait bad dedup {candidate_case}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = _review_wait_target(project.id)
    candidate_target = (
        {**target, "head_sha": "wrong-head"}
        if candidate_case == "wrong_target"
        else target
    )
    candidate = _make_fake_review_task(
        project.id,
        review_target=candidate_target,
        status=(TaskStatus.FAILED if candidate_case == "terminal" else TaskStatus.QUEUED),
    )
    db_session.add(candidate)
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()

    async def _dedup(source_task, _task_type, source_session, *args, **kwargs):
        assert kwargs.get("review_target") == target
        authoritative_source = await kwargs["source_spawn_authority"](
            source_session
        )
        assert authoritative_source is not None
        authoritative_source.context = {
            **(authoritative_source.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_DEDUP,
        }
        source_session.add(authoritative_source)
        return (candidate, False)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock(side_effect=_dedup)
    )

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    loop = (task.context or {})["review_loop"]
    assert loop.get("authoritative_review_task_id") != str(candidate.id)
    assert loop.get("review_approved") is not True
    assert task.requires_approval is True
    assert loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY)


@pytest.mark.parametrize(
    "race_case", ["replacement", "approve", "archive", "manual_hold"]
)
@pytest.mark.asyncio
async def test_review_wait_spawn_commit_race_does_not_overwrite_new_attempt(
    db_session, mocker, race_case
):
    """Independent revocation wins before the canonical child commit."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal
    from sqlmodel import select as _select

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Review wait helper commit race", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    project_id = project.id
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()

    async def _spawn_after_independent_revoke(
        source_task, _task_type, source_session, *args, **kwargs
    ):
        async with AsyncSessionLocal() as competing_session:
            competing_task = await competing_session.get(Task, task.id)
            assert competing_task is not source_task
            competing_context = dict(competing_task.context or {})
            raced_loop = dict(competing_context.get("review_loop") or {})
            raced_loop["race_sentinel"] = f"preserve-{race_case}"
            if race_case == "replacement":
                raced_loop.update(
                    {
                        "pr_number": 99,
                        "branch": "blueprint/rebound",
                        "base_branch": "automain",
                        "ci_wait_started": "2026-08-17T00:00:00",
                        "head_sha": "new-attempt-head",
                    }
                )
                competing_context["rollup_pr_number"] = 99
            elif race_case == "approve":
                raced_loop["review_approved"] = True
            elif race_case == "archive":
                competing_task.archived = True
            else:
                competing_task.requires_approval = True
                competing_context["approval_hold_reason"] = "manual"
            competing_task.context = {
                **competing_context,
                "review_loop": raced_loop,
            }
            competing_session.add(competing_task)
            await competing_session.commit()

        authoritative_source = await kwargs["source_spawn_authority"](
            source_session
        )
        assert authoritative_source is None
        return (None, False)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_spawn_after_independent_revoke),
    )

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    loop = (task.context or {})["review_loop"]
    assert loop.get("race_sentinel") == f"preserve-{race_case}"
    if race_case == "replacement":
        assert loop.get("pr_number") == 99
    elif race_case == "approve":
        assert loop.get("review_approved") is True
    elif race_case == "archive":
        assert task.archived is True
    else:
        assert task.requires_approval is True
        assert (task.context or {}).get("approval_hold_reason") == "manual"
    assert loop.get("review_wait_respawned") is not True
    reviews = (
        (
            await db_session.execute(
                _select(Task).where(Task.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    assert not [
        row
        for row in reviews
        if row.id != task.id and row.first_payload.get("type") == "review"
    ]


@pytest.mark.parametrize(
    "early_exit", ["terminal", "repeat_fingerprint", "supersession_churn"]
)
@pytest.mark.asyncio
async def test_review_wait_postcommit_spawn_reason_cannot_overwrite_replacement(
    db_session, mocker, early_exit
):
    """A canonical post-commit source annotation is never flushed stale.

    Stuck, repeat-fingerprint, and supersession-churn exits commit authoritative
    escalation work before returning. This models a replacement winning after
    that commit and before the old ORM identity is annotated with its reason.
    """
    from copy import deepcopy
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal
    from app.review_pipeline import (
        SPAWN_REASON_BLOCKED_REPEAT,
        SPAWN_REASON_KEY,
        SPAWN_REASON_STUCK,
        SPAWN_REASON_SUPERSEDE_CHURN,
    )

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name=f"Review wait stale {early_exit}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    old_reason = {
        "terminal": SPAWN_REASON_STUCK,
        "repeat_fingerprint": SPAWN_REASON_BLOCKED_REPEAT,
        "supersession_churn": SPAWN_REASON_SUPERSEDE_CHURN,
    }[early_exit]

    async def _postcommit_early_exit(
        source_task, _task_type, source_session, *args, **kwargs
    ):
        authoritative_source = await kwargs["source_spawn_authority"](
            source_session
        )
        assert authoritative_source is not None
        assert authoritative_source is source_task
        stale_context = deepcopy(authoritative_source.context or {})

        # Match the canonical early exits: commit releases source authority,
        # then a peer replacement changes the attempt in an independent session.
        await source_session.commit()
        async with AsyncSessionLocal() as competing_session:
            competing_task = await competing_session.get(Task, task.id)
            assert competing_task is not authoritative_source
            competing_context = dict(competing_task.context or {})
            competing_loop = dict(competing_context.get("review_loop") or {})
            competing_loop.update(
                {
                    "pr_number": 99,
                    "branch": "blueprint/rebound",
                    "head_sha": "replacement-head",
                    "race_sentinel": f"preserve-{early_exit}",
                }
            )
            competing_context["rollup_pr_number"] = 99
            competing_task.context = {
                **competing_context,
                "review_loop": competing_loop,
            }
            competing_session.add(competing_task)
            await competing_session.commit()

        # This is the unsafe historical shape: the canonical spawner dirties
        # the now-stale source identity after its internal commit.
        authoritative_source.context = {
            **stale_context,
            SPAWN_REASON_KEY: old_reason,
        }
        source_session.add(authoritative_source)
        return (None, False)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_postcommit_early_exit),
    )

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {})["review_loop"]
    assert review_loop.get("pr_number") == 99
    assert review_loop.get("race_sentinel") == f"preserve-{early_exit}"
    assert review_loop.get("review_wait_respawned") is not True
    assert task.requires_approval is not True


@pytest.mark.asyncio
async def test_review_wait_real_supersession_commit_cannot_overwrite_replacement(
    db_session, mocker
):
    """The real churn-halt second commit cannot resurrect a superseded attempt."""
    from datetime import datetime, timedelta

    from app.alert_dispatch import (
        _maybe_create_escalation_alert as _real_escalation_alert,
    )
    from app.ci_completion import handle_ci_completion
    from app.database import AsyncSessionLocal
    from app.review_pipeline import PIPELINE_TASK_STALE_MINUTES

    mocker.patch("app.review_pipeline._REVIEW_SUPERSEDE_CHURN_LIMIT", 1)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Review wait real churn race", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    stale_review = Task(
        project_id=project.id,
        title="Stale exact review",
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
                "original_task_id": str(task.id),
            }
        ],
        context={"review_loop": {"review_target": _review_wait_target(project.id)}},
        status=TaskStatus.IN_PROGRESS,
        started_at=(
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5)
        ),
        last_heartbeat=None,
    )
    db_session.add(stale_review)
    await db_session.commit()

    async def _alert_then_replace(*args, **kwargs):
        # Exercise the helper's real commit, then let a separate ORM identity
        # install the replacement before the canonical spawner resumes.
        await _real_escalation_alert(*args, **kwargs)
        async with AsyncSessionLocal() as competing_session:
            competing = await competing_session.get(Task, task.id)
            assert competing is not task
            context = dict(competing.context or {})
            review_loop = dict(context.get("review_loop") or {})
            review_loop.pop("review_wait_spawn_claim", None)
            review_loop.update(
                {
                    "pr_number": 99,
                    "branch": "blueprint/rebound",
                    "base_branch": "automain",
                    "head_sha": "replacement-head",
                    "ci_checked_head_sha": "replacement-head",
                    "ci_checked_base_sha": "replacement-base",
                    "review_wait_started": "2026-08-17T02:00:00",
                    "race_sentinel": "preserve-real-churn-replacement",
                }
            )
            context["rollup_pr_number"] = 99
            context.pop("_approval_set_at", None)
            competing.requires_approval = False
            competing.context = {**context, "review_loop": review_loop}
            competing_session.add(competing)
            await competing_session.commit()

    mocker.patch(
        "app.alert_dispatch._maybe_create_escalation_alert",
        new=AsyncMock(side_effect=_alert_then_replace),
    )

    result = await handle_ci_completion(
        task,
        {"status": "completed", "conclusion": "success"},
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {})["review_loop"]
    assert review_loop.get("pr_number") == 99
    assert review_loop.get("race_sentinel") == "preserve-real-churn-replacement"
    assert review_loop.get("review_wait_escalated") is not True
    assert task.requires_approval is False


@pytest.mark.asyncio
async def test_orphan_review_wait_respawns_review(db_session, mocker):
    """When CI passed but review approval never arrived past the backstop
    deadline, a fresh review task is respawned BEFORE gating on a human:
    requires_approval stays False, no failure alert fires, and the respawn is
    recorded in review_loop with the window reset for the new reviewer."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Review wait respawn", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fake_review = _make_fake_review_task(project.id)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(
            side_effect=_canonical_review_wait_spawn((fake_review, True))
        ),
    )

    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    # A fresh review was spawned with task_type "review".
    spawn_mock.assert_awaited_once()
    spawn_args = spawn_mock.await_args
    assert spawn_args.args[1] == "review", "must respawn a review task"

    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_respawned") is True
    assert review_loop.get("review_wait_respawn_task_id") == str(fake_review.id)
    assert review_loop.get("review_wait_respawn_count") == 1
    # Window reset for the new reviewer — review_wait_started moved forward.
    assert review_loop.get("review_wait_started") != old_started
    # No human gate, no orphan escalation, no alert.
    assert task.requires_approval is not True, (
        "requires_approval must NOT be set when a review respawn succeeds"
    )
    assert review_loop.get("review_wait_escalated") is not True
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_wait_respawn_suppressed_falls_back_to_approval(
    db_session, mocker
):
    """If respawn is suppressed (None, False) — cap/merged/closed — the human
    gate fallback fires: requires_approval + review_wait_escalated + one alert."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import SPAWN_REASON_CAP

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(
            side_effect=_canonical_review_wait_spawn(
                (None, False), reason=SPAWN_REASON_CAP
            )
        ),
    )

    project = Project(name="Review wait respawn suppressed", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_respawned") is not True
    assert review_loop.get("review_wait_escalated") is True
    assert task.requires_approval is True
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_review_wait_respawn_capped(db_session, mocker):
    """A second timeout with review_wait_respawn_count already at the cap must
    NOT respawn again — it falls back to the human gate."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(_make_fake_review_task(uuid.uuid4()), True)),
    )

    project = Project(name="Review wait respawn capped", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        # Already respawned once (cap is 1) — next timeout must gate.
        ci_loop_extra={
            "review_wait_started": old_started,
            "review_wait_respawn_count": 1,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    spawn_mock.assert_not_awaited()
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_escalated") is True
    assert task.requires_approval is True
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_review_wait_auto_respawn_disabled(db_session, mocker):
    """With review_wait_auto_respawn=False, the legacy immediate
    requires_approval behavior is preserved (no respawn attempt)."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(_make_fake_review_task(uuid.uuid4()), True)),
    )
    # Disable auto-respawn via runtime config.
    from app.shared_state import runtime_config

    mocker.patch.dict(runtime_config, {"review_wait_auto_respawn": False})

    project = Project(name="Review wait respawn disabled", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    spawn_mock.assert_not_awaited()
    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_escalated") is True
    assert task.requires_approval is True
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_review_wait_respawn_capped_resets_for_superseded_review(
    db_session, mocker
):
    """When the previously respawned review was CANCELLED (superseded by a fix),
    the effective respawn count must decrement, giving the new episode its own
    budget. Without this, a single consumed respawn in an earlier review-wait
    episode permanently exhausts the cap."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Respawn cap reset", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a previous respawned review that was CANCELLED (superseded by fix).
    superseded_review = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Superseded review",
        payloads=[{"type": "review"}],
        status=TaskStatus.CANCELLED,
    )
    db_session.add(superseded_review)
    await db_session.commit()

    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={
            "review_wait_started": old_started,
            # Count is at the cap (1) but the respawned review was CANCELLED.
            "review_wait_respawn_count": 1,
            "review_wait_respawn_task_id": str(superseded_review.id),
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    # The previous respawned review was superseded — a new spawn must fire.
    fake_review = _make_fake_review_task(project.id)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(
            side_effect=_canonical_review_wait_spawn((fake_review, True))
        ),
    )

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        checked_head_sha=_REVIEW_WAIT_HEAD_SHA,
        checked_base_sha=_REVIEW_WAIT_BASE_SHA,
    )

    assert result is True
    # The spawn must have been attempted despite the count being at cap,
    # because the previous respawned review was superseded.
    spawn_mock.assert_awaited_once()
    spawn_args = spawn_mock.await_args
    assert spawn_args.args[1] == "review", "must spawn a review task"

    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    # The effective count was decremented, so the respawn should have fired.
    assert review_loop.get("review_wait_respawned") is True
    assert review_loop.get("review_wait_escalated") is not True
    assert task.requires_approval is not True
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_wait_recent_window_does_not_bypass_backstop(
    db_session, mocker
):
    """A recent review-wait window cannot create a pre-60-minute review."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Recent window waits for backstop", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
    )

    # A recent wait cannot bypass the 60m gate.
    recent_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    ).isoformat()

    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": recent_started},
    )
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success"}
    result = await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    spawn_mock.assert_not_awaited()

    await db_session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("review_wait_respawned") is not True
    assert task.requires_approval is not True
    alert_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Non-terminal check-run status: waiting / requested / pending
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal httpx response mock for standalone get_check_runs tests."""

    def __init__(self, status_code, json_data=None, links=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = ""
        self.headers = {}
        self.links = links or {}

    def json(self):
        return self._json_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_terminal_status",
    ["waiting", "requested", "pending"],
)
async def test_waiting_check_run_does_not_resolve_green(mocker, non_terminal_status):
    """A check-run in waiting/requested/pending must not make the aggregate green.

    GitHub Actions uses these statuses for deployment-protection gates and queued
    reusable workflows.  Previously only 'queued' and 'in_progress' were treated as
    non-terminal, so a single passing sibling would yield a green aggregate while a
    required check had not run.
    """
    from app.github import get_check_runs

    payload = {
        "check_runs": [
            {
                "id": 1,
                "name": "deploy-gate",
                "status": non_terminal_status,
                "conclusion": None,
            },
            {
                "id": 2,
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }

    async def mock_request(method, *args, **kwargs):
        return _MockResponse(200, payload, links={})

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await get_check_runs("token", "owner", "repo", "abc123sha")

    # The non-terminal check-run must keep the aggregate in "in_progress",
    # not allow a false-green "completed/success" verdict.
    assert result["status"] == "in_progress", (
        f"status '{non_terminal_status}' must be treated as non-terminal "
        f"but got aggregate status={result['status']!r}"
    )
    # Conclusion should not be 'success' while a check has not completed
    assert result["conclusion"] != "success" or result["status"] != "completed", (
        "aggregate must not resolve green while a check-run is still non-terminal"
    )


# ---------------------------------------------------------------------------
# CriterionEvidence recorded through _attempt_merge_with_retry_and_escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_evidence_persisted_via_attempt_merge(db_session, mocker):
    """CriterionEvidence rows are persisted through the full merge path.

    Exercises the production code path at ci_completion.py:655 — the block that
    records task_merge evidence for each criterion_id on the original task after
    a PR lands on the integration branch.  A regression that removes or bypasses
    that block would fail this test, unlike unit tests that directly call
    get_criterion_by_key/record_evidence.
    """
    from app import acceptance_criteria as ac
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    # Merge succeeds on the first attempt
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "abc123merge"},
    )
    # Ancestry gate returns "indeterminate" (no GitHub credentials) → record_pr_merged stays True
    mocker.patch(
        "app.rollup_pr_verification.verify_rollup_merge_landed",
        new=AsyncMock(return_value="indeterminate"),
    )

    project = Project(name="CriterionEvidence merge test", user_id="test-user-ce")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create an open release version and a criterion
    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Feature ships green",
        created_by="strategist",
    )

    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    # Create the original (implementation) task with criterion_ids set
    orig_task = Task(
        project_id=project.id,
        title="Implement feature",
        payloads=[{"type": "create", "prompt": "build the feature", "base_branch": "main"}],
        context={"direction_id": "d-ce-test", CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True}},
        status=TaskStatus.EVALUATING,
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
        criterion_ids=[criterion.criterion_key],
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    # Create the CI review task pointing to the original task
    ci_task = _make_task(
        db_session,
        project,
        pr_number=99,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
            "base_branch": "main",
        },
    )
    await db_session.commit()
    await db_session.refresh(ci_task)

    result = await _attempt_merge_with_retry_and_escalation(
        ci_task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "merge should have succeeded"

    # Re-query criterion evidence using a fresh session path (dedicated session
    # in _attempt_merge_with_retry_and_escalation commits via AsyncSessionLocal,
    # which is patched to the test engine by the autouse fixture)
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    assert len(evidence) >= 1, (
        "At least one CriterionEvidence row must be persisted for the merged task"
    )
    task_merge_ev = [e for e in evidence if e.source_kind == "task_merge"]
    assert len(task_merge_ev) == 1, "exactly one task_merge evidence row expected"
    assert task_merge_ev[0].source_id == str(orig_task.id)
    # Evidence must include proven=True so the criterion coverage transitions to 'covered'.
    # The detail dict may also contain direction_key and other metadata fields.
    assert task_merge_ev[0].detail.get("proven") is True, (
        "task_merge evidence must have proven=True to satisfy criterion coverage"
    )
    # Verify coverage is 'covered' due to the proven evidence
    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "covered", (
        f"Coverage should be 'covered' with proven task_merge evidence, got '{coverage}'"
    )


@pytest.mark.asyncio
async def test_direct_merge_unproven_verdict_records_no_proven_evidence(
    db_session, mocker
):
    """Direct CI merge WITHOUT a proven code-acceptance verdict records NO evidence.

    When orig_task.context lacks a proven verdict (or the verdict is absent),
    record_task_merge_evidence must not write any CriterionEvidence rows.
    This is the acceptance-gate regression guard: bare merge badges must NOT
    fabricate 'covered' status for criteria that have not been genuinely proven.
    """
    from app import acceptance_criteria as ac
    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "abc123noproven"},
    )
    mocker.patch(
        "app.rollup_pr_verification.verify_rollup_merge_landed",
        new=AsyncMock(return_value="indeterminate"),
    )

    project = Project(name="Unproven verdict merge test", user_id="test-user-upv")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Must be covered only by proven merge",
        created_by="strategist",
    )

    # orig_task has NO code-acceptance verdict — simulates an unproven merge
    orig_task = Task(
        project_id=project.id,
        title="Unproven feature task",
        payloads=[{"type": "create", "prompt": "build it", "base_branch": "main"}],
        context={"direction_id": "d-upv-test"},  # no CODE_ACCEPTANCE_VERDICT_KEY
        status=TaskStatus.EVALUATING,
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
        criterion_ids=[criterion.criterion_key],
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    ci_task = _make_task(
        db_session,
        project,
        pr_number=77,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
            "base_branch": "main",
        },
    )
    await db_session.commit()
    await db_session.refresh(ci_task)

    result = await _attempt_merge_with_retry_and_escalation(
        ci_task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "merge should have succeeded"

    # Verify that NO evidence rows were written — unproven verdict must be gated out
    evidence = await ac.get_evidence_for_criterion(db_session, criterion.id)
    task_merge_ev = [e for e in evidence if e.source_kind == "task_merge"]
    assert len(task_merge_ev) == 0, (
        "No task_merge evidence should be recorded when the code-acceptance verdict "
        f"is absent or unproven; got {task_merge_ev}"
    )

    # Coverage must remain uncovered — no fabricated proven evidence
    coverage = ac.compute_criterion_coverage(evidence)
    assert coverage == "uncovered", (
        f"Coverage must stay 'uncovered' when verdict is absent, got '{coverage}'"
    )


# ---------------------------------------------------------------------------
# Size-cap preflight integration — direct (non-rollup) task PR gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_cap_hold_sets_requires_approval_and_returns_false(db_session, mocker):
    """Size-cap preflight 'hold' → requires_approval=True, merge deferred."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult(
                "hold", "chronic offender: models.py (3 bumps)"
            )
        ),
    )

    project = Project(name="Size hold test", user_id="test-user")
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
    assert task.requires_approval is True
    assert "size-cap" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_size_cap_bumped_rearms_ci_and_returns_false(db_session, mocker):
    """Size-cap preflight 'bumped' → CI re-armed (ci_waiting=True, ci_passed=False)."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult("bumped", "models.py: 500 -> 800")
        ),
    )

    project = Project(name="Size bump test", user_id="test-user")
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
    loop = (task.context or {}).get("review_loop", {})
    assert loop.get("ci_waiting") is True
    assert loop.get("ci_passed") is False
    assert "size-cap" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_size_cap_hold_gates_off_does_not_set_requires_approval(db_session, mocker):
    """Size-cap 'hold' with gates OFF must hold the merge but NOT set requires_approval.

    With gates off the approval-gate timeout in services.py would FAIL the task
    if requires_approval were set — the hold must still surface but the flag stays clear.
    """
    from app import shared_state
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult("hold", "chronic offender: models.py")
        ),
    )
    original_gates = shared_state.runtime_config.get("require_approval_gates", True)
    shared_state.runtime_config["require_approval_gates"] = False
    try:
        project = Project(name="Size hold gates-off test", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(db_session, project)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _attempt_merge_with_retry_and_escalation(
            task, "fake-token", "owner", "repo", db_session, source="poll"
        )

        assert result is False, "hold verdict must still defer the merge"
        assert not task.requires_approval, (
            "C5 invariant: with gates off, requires_approval must NOT be set"
        )
        assert "size-cap" in (task.latest_status_message or "").lower()
    finally:
        shared_state.runtime_config["require_approval_gates"] = original_gates


@pytest.mark.asyncio
async def test_size_cap_defer_returns_false_without_state_mutation(db_session, mocker):
    """Size-cap preflight 'defer' (transient error) → returns False, no task state change."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(return_value=SizeCapPreflightResult("defer", "github rate limit")),
    )

    project = Project(name="Size defer test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)
    original_status = task.status
    original_requires_approval = task.requires_approval

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False
    assert task.requires_approval == original_requires_approval, (
        "'defer' must not set requires_approval"
    )
    assert task.status == original_status, "'defer' must not change task status"


@pytest.mark.asyncio
async def test_size_cap_old_precomputed_head_is_rechecked_before_direct_merge(
    db_session, mocker
):
    """A verdict from an older head is re-evaluated at the merge chokepoint."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )
    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult(
                "ok", None, "test-head-42", "main"
                , "test-base-42"
            )
        ),
    )
    project = Project(name="Stale size preflight test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_size_cap=SizeCapPreflightResult("ok", None, "old-pr-head"),
        checked_head_sha="test-head-42",
        checked_base_sha="test-base-42",
    )

    assert result is True
    preflight_mock.assert_awaited_once()
    assert preflight_mock.await_args.kwargs["target_branch"] == "main"
    merge_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_precomputed_size_cap_without_merge_target_is_not_adopted(
    db_session, mocker
):
    """A targetless pre-lock verdict is rechecked against the project trunk."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    project = Project(
        name="Targetless size preflight test",
        user_id="test-user",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    task.payloads = [{**task.payloads[0], "base_branch": ""}]
    task.context = {
        **(task.context or {}),
        "review_loop": {
            **(task.context or {}).get("review_loop", {}),
            "base_branch": "",
        },
    }
    await db_session.commit()
    await db_session.refresh(task)

    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult(
                "ok", None, "test-head-42", "automain"
                , "test-base-42"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_size_cap=SizeCapPreflightResult(
            "defer", "targetless pre-lock evaluation", "test-head-42"
        ),
        checked_head_sha="test-head-42",
        checked_base_sha="test-base-42",
    )

    assert result is True
    preflight_mock.assert_awaited_once()
    assert preflight_mock.await_args.kwargs["target_branch"] == "automain"
    merge_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_size_cap_matching_precomputed_target_is_reused(db_session, mocker):
    """A pre-lock verdict with matching target and head skips recomputation."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(side_effect=AssertionError("matching result must be reused")),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )

    project = Project(name="Matching size target test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_size_cap=SizeCapPreflightResult(
            "ok", None, "test-head-42", "main"
            , "test-base-42"
        ),
        checked_head_sha="test-head-42",
        checked_base_sha="test-base-42",
    )

    assert result is True
    preflight_mock.assert_not_awaited()
    merge_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_size_cap_precomputed_base_mismatch_is_rechecked(db_session, mocker):
    """A verdict for the right head/target but prior base cannot authorize a merge."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult(
                "ok", None, "test-head-42", "main", "test-base-42"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )
    project = Project(name="Mismatched size base test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    task = _make_task(db_session, project)
    await db_session.commit()

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="review_approval",
        precomputed_size_cap=SizeCapPreflightResult(
            "ok", None, "test-head-42", "main", "old-base"
        ),
        checked_head_sha="test-head-42",
        checked_base_sha="test-base-42",
    )

    assert result is True
    preflight_mock.assert_awaited_once()
    assert preflight_mock.await_args.kwargs["expected_base_sha"] == "test-base-42"
    merge_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_size_cap_holds_rollup_parent_at_merge_with_ci_checked_head(
    db_session, mocker
):
    """A rollup parent is held at the merge chokepoint on an un-repaired cap."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(
            return_value=SizeCapPreflightResult(
                "hold", "models.py: 500 -> 750 requires cap repair"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )

    project = Project(name="Rollup skip test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    task.context = {**(task.context or {}), "rollup_pr_number": 999}
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Rollup parent must not merge without the cap repair"
    preflight_mock.assert_awaited_once()
    assert preflight_mock.await_args.kwargs["expected_head_sha"] == "test-head-42"
    merge_mock.assert_not_awaited()
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_rollup_precomputed_size_clearance_does_not_repeat_or_bump_at_merge(
    db_session, mocker
):
    """The pre-lock rollup result is reused at merge rather than checked twice."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    preflight_mock = mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(side_effect=AssertionError("precomputed result must be reused")),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )

    project = Project(name="Rollup precomputed cap test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    task.context = {**(task.context or {}), "rollup_pr_number": 999}
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
        precomputed_size_cap=SizeCapPreflightResult(
            "ok", None, "test-head-42", "main"
            , "test-base-42"
        ),
    )

    assert result is True
    preflight_mock.assert_not_awaited()
    merge_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# effective_pr_number / arm_ci_wait — PR-identity split-brain accessors
# ---------------------------------------------------------------------------
from app.ci_completion import arm_ci_wait, effective_pr_number  # noqa: E402


class _CtxHolder:
    """Minimal Task-like stand-in exposing a ``.context`` attribute."""

    def __init__(self, context):
        self.context = context


def test_effective_pr_number_prefers_review_loop_pr_number():
    holder = _CtxHolder({"review_loop": {"pr_number": 111}, "rollup_pr_number": 222})
    assert effective_pr_number(holder) == 111


def test_effective_pr_number_falls_back_to_rollup_pr_number():
    # The split-brain shape: ci_waiting armed with no review_loop.pr_number, the
    # PR identity living only at context.rollup_pr_number.
    holder = _CtxHolder({"review_loop": {"ci_waiting": True}, "rollup_pr_number": 222})
    assert effective_pr_number(holder) == 222


def test_effective_pr_number_none_when_absent():
    assert effective_pr_number(_CtxHolder({"review_loop": {}})) is None
    assert effective_pr_number(_CtxHolder(None)) is None


def test_effective_pr_number_accepts_bare_context_dict():
    assert effective_pr_number({"review_loop": {"pr_number": 5}}) == 5
    assert effective_pr_number({"rollup_pr_number": 9}) == 9
    assert effective_pr_number({}) is None


def test_arm_ci_wait_costamps_identity_and_clock():
    review_loop = {"iteration": 3}
    now = "2026-07-05T00:00:00"
    out = arm_ci_wait(review_loop, pr_number=42, now_iso=now)
    assert out is review_loop  # mutates in place
    assert review_loop["ci_waiting"] is True
    assert review_loop["pr_number"] == 42
    assert review_loop["ci_wait_started"] == now
    assert review_loop["ci_wait_pause_baseline"] == 0.0
    assert review_loop["iteration"] == 3  # unrelated fields preserved
    # Optional identity fields are omitted when not supplied.
    assert "head_sha" not in review_loop
    assert "base_branch" not in review_loop


def test_arm_ci_wait_sets_optional_head_and_base():
    review_loop = {}
    arm_ci_wait(
        review_loop,
        pr_number=1,
        head_sha="abc123",
        base_branch="automain",
        now_iso="t",
        pause_baseline=12.5,
    )
    assert review_loop["head_sha"] == "abc123"
    assert review_loop["base_branch"] == "automain"
    assert review_loop["ci_wait_pause_baseline"] == 12.5


def test_arm_ci_wait_asserts_pr_number_present():
    with pytest.raises(AssertionError):
        arm_ci_wait({}, pr_number=0, now_iso="t")
    with pytest.raises(AssertionError):
        arm_ci_wait({}, pr_number=None, now_iso="t")


# ---------------------------------------------------------------------------
# Auto-resolve audit findings on merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_does_not_auto_resolve_finding_keys_on_orig_task(db_session, mocker):
    """A merge declaration is not current-HEAD evidence for carried findings."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import AuditFindingResolution, ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Auto-resolve finding merge test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open release version for the project (required for auto-resolve).
    version = ReleaseVersion(
        project_id=project.id, version_number=1, status="open"
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    finding_key_a = "abcd1234abcd1234"
    finding_key_b = "ef567890ef567890"

    # Original fix task carries the finding keys to resolve on merge.
    orig_task = Task(
        project_id=project.id,
        title="Fix task for audit findings",
        payloads=[{"type": "fix", "prompt": "fix the finding"}],
        context={"resolving_finding_keys": [finding_key_a, finding_key_b]},
        vm_instance_id=f"agent-vm-orig-resolve-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    # CI parking task that will trigger the merge.
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED

    # No merge-time helper may write resolution rows. An explicit audit or
    # operator resolution remains the only path that can advance this ledger.
    rows = (
        (
            await db_session.execute(
                _select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_merge_auto_resolve_skips_when_no_open_version(db_session, mocker):
    """When the orig task has resolving_finding_keys but no open ReleaseVersion
    exists for the project, the auto-resolve silently skips without error."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import AuditFindingResolution

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Auto-resolve no version", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No open ReleaseVersion — auto-resolve must skip gracefully.

    orig_task = Task(
        project_id=project.id,
        title="Fix task — no open version",
        payloads=[{"type": "fix", "prompt": "fix something"}],
        context={"resolving_finding_keys": ["deadbeefdeadbeef"]},
        vm_instance_id=f"agent-vm-orig-nov-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # Merge must still succeed — no open version must not block the merge.
    assert result is True
    assert task.status == TaskStatus.COMPLETED

    # No AuditFindingResolution rows should have been written.
    rows = (
        (
            await db_session.execute(
                _select(AuditFindingResolution).where(
                    AuditFindingResolution.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0, (
        "No finding-resolution rows must be written when no open version exists"
    )


@pytest.mark.asyncio
async def test_merge_auto_resolve_no_keys_writes_no_rows(db_session, mocker):
    """A merge that declares no finding keys credits NOTHING.

    Regression guard for the blanket-credit defect: the open version here carries
    genuine UNRESOLVED gating findings (critical/high/medium).  A merge whose task
    carries no ``resolving_finding_keys`` is not evidence that those findings were
    addressed, so no AuditFindingResolution row may be written — otherwise an
    unrelated refactor/rollup merge would drive ``gating_resolved`` to
    ``gating_total`` and promote the version with the findings unaddressed.
    Crediting requires an explicit declaration: task-context keys, or the
    ``record_finding_resolution`` strategic op / operator trigger.

    This also pins the narrower rule that TOUCHING a finding's file is not
    evidence the finding was addressed: the merged PR's verified changed-file
    list here contains the exact path every carried finding sits on, and the
    ledger must still be empty.  A changed blob may prove STALENESS (see
    ``auto_mark_stale_audit_findings``, which requires a recorded ``blob_sha``
    and a SHA mismatch against the audited surface), never a fix."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import AuditFindingResolution, ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="No keys — no rows", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={
            "audit_findings": [
                {
                    "key": "aaaabbbbccccdddd",
                    "severity": "critical",
                    "summary": "genuine unresolved critical finding",
                    "file": "backend/app/touched.py",
                },
                {
                    "key": "eeeeffff00001111",
                    "severity": "high",
                    "summary": "genuine unresolved high finding",
                    "file": "backend/app/touched.py",
                },
                {
                    "key": "2222333344445555",
                    "severity": "medium",
                    "summary": "genuine unresolved medium finding",
                    "file": "backend/app/touched.py",
                },
            ]
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    orig_task = Task(
        project_id=project.id,
        title="Ordinary fix task (no finding keys)",
        payloads=[{"type": "fix", "prompt": "fix something"}],
        # No resolving_finding_keys.  The PR *did* touch the exact file every
        # carried finding sits on, and that touch is GitHub-verified — still
        # not evidence of a fix.
        context={
            "_pr_changed_files": ["backend/app/touched.py"],
            "_pr_changed_files_github_verified": True,
        },
        vm_instance_id=f"agent-vm-orig-nokeys-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    rows = (
        (
            await db_session.execute(
                _select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0, (
        "No AuditFindingResolution rows expected for a task that declares no "
        f"finding keys; carried gating findings must stay unresolved. Got: "
        f"{[(r.finding_key, r.state) for r in rows]}"
    )


@pytest.mark.asyncio
async def test_merge_auto_resolve_no_orig_no_keys_writes_no_rows(db_session, mocker):
    """No-orig fallback: a parking task without declared keys credits NOTHING.

    Same invariant as ``test_merge_auto_resolve_no_keys_writes_no_rows`` for the
    second call site (``ci_orig_id`` absent).  A merge that names no finding is
    not evidence any carried finding was addressed."""
    from sqlmodel import select as _select

    from app.ci_completion import _attempt_merge_with_retry_and_escalation
    from app.models import AuditFindingResolution, ReleaseVersion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="No orig, no keys — no rows", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        last_qualification={
            "audit_findings": [
                {
                    "key": "6666777788889999",
                    "severity": "critical",
                    "summary": "genuine unresolved critical finding",
                },
                {
                    "key": "aaaa0000bbbb1111",
                    "severity": "high",
                    "summary": "genuine unresolved high finding",
                },
            ]
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # No ``original_task_id`` in the review loop → the no-orig fallback runs.
    task = _make_task(db_session, project, pr_number=44)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    rows = (
        (
            await db_session.execute(
                _select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0, (
        "No-orig merge without declared finding keys must not credit carried "
        f"gating findings. Got: {[(r.finding_key, r.state) for r in rows]}"
    )


# ─── General CI rerun gate (flake-tolerant) tests ──────────────────────────


@pytest.mark.asyncio
async def test_ci_failure_general_rerun_fires(db_session, mocker):
    """CI failure with rerun budget remaining calls rerun_failed_jobs,
    increments ci_rerun_consumed in task context, updates latest_status_message,
    and returns False (defers fix spawn to next sweep)."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        return_value=True,
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(type("FT", (), {"id": uuid.uuid4()})(), True),
    )

    project = Project(name="General rerun test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "FAILED test_x",
        "failed_run_ids": [12345],
        "setup_phase_failure": False,
    }
    result = await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # Must return False — no fix spawned, deferring to next sweep
    assert result is False, "Expected False (rerun defers fix spawn to next sweep)"

    # rerun_failed_jobs must have been called with the failed run ID
    rerun_mock.assert_called_once_with("fake-token", "owner", "repo", 12345)

    # spawn_pipeline_task must NOT have been called
    spawn_mock.assert_not_called()

    # Task context must track the consumed rerun
    await db_session.refresh(task)
    assert task.context.get("ci_rerun_consumed") == 1, (
        f"Expected ci_rerun_consumed=1, got {task.context.get('ci_rerun_consumed')}"
    )
    # latest_status_message must reflect the rerun
    assert "re-ran failed jobs" in (task.latest_status_message or ""), (
        f"Expected status message to mention rerun, got {task.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_ci_failure_general_rerun_already_consumed(db_session, mocker):
    """CI failure with ci_rerun_consumed already at budget does NOT call
    rerun_failed_jobs and falls through to spawn a fix task."""
    from app.ci_completion import _CI_RERUN_BUDGET, handle_ci_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        return_value=True,
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )
    mocker.patch(
        "app.review_pipeline.build_ci_failure_fix_prompt",
        return_value="fix prompt",
    )

    project = Project(name="Rerun exhausted", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create task with ci_rerun_consumed already at budget
    task = _make_task(
        db_session,
        project,
        ci_loop_extra={},
    )
    task.context = {
        **(task.context or {}),
        "ci_rerun_consumed": _CI_RERUN_BUDGET,
    }
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "FAILED test_x",
        "failed_run_ids": [12345],
        "setup_phase_failure": False,
    }
    await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # rerun_failed_jobs must NOT have been called
    rerun_mock.assert_not_called()

    # A fix task must have been spawned
    spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_ci_failure_general_rerun_no_failed_run_ids(db_session, mocker):
    """CI failure with empty failed_run_ids (API failure or quota active) does
    NOT call rerun_failed_jobs and falls through to spawn a fix task."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        return_value=True,
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(fake_fix, True),
    )
    mocker.patch(
        "app.review_pipeline.build_ci_failure_fix_prompt",
        return_value="fix prompt",
    )

    project = Project(name="No failed run IDs", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "FAILED test_x",
        "failed_run_ids": [],
        "setup_phase_failure": False,
    }
    await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # rerun_failed_jobs must NOT have been called (no run IDs to re-run)
    rerun_mock.assert_not_called()

    # A fix task must have been spawned
    spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_ci_failure_general_rerun_infra_takes_priority(db_session, mocker):
    """When setup_phase_failure is True, the infra rerun gate fires instead
    of the general rerun gate. The general rerun budget is not consumed."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        return_value=True,
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(type("FT", (), {"id": uuid.uuid4()})(), True),
    )

    project = Project(name="Infra rerun priority", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "FAILED test_x",
        "failed_run_ids": [12345],
        "setup_phase_failure": True,
    }
    await handle_ci_completion(
        task, ci_result, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # rerun_failed_jobs must have been called (infra rerun path)
    rerun_mock.assert_called_once_with("fake-token", "owner", "repo", 12345)

    # No fix spawned
    spawn_mock.assert_not_called()

    # ci_rerun_consumed must NOT have been incremented (general rerun did not fire)
    await db_session.refresh(task)
    assert task.context.get("ci_rerun_consumed", 0) == 0, (
        "General rerun gate must NOT consume its budget when infra rerun fires"
    )


# ---------------------------------------------------------------------------
# Empty-effective-diff merge gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_gate_blocks_empty_effective_diff_pr(db_session, mocker):
    """A direct task PR whose effective diff went EMPTY by merge time must NOT
    auto-merge and must NOT report COMPLETED-success — it lands an explicit
    SCOPE_EMPTY no-op terminal (PR closed, pr_closed_empty set) instead.

    Regression guard for the 2026-07-12 incident (PR #14500 / commit 683abce38:
    a fix PR merged 'green' with an empty ``git show --stat`` yet reported
    success).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    # Confirmed-empty effective diff (get_pr_files returns []). This overrides
    # the autouse _default_no_migration_pr fixture's non-empty default.
    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[]))
    # merge_pull_request must NEVER be called for an empty PR.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", AsyncMock(return_value=True)
    )
    mocker.patch("app.github.create_pr_comment", AsyncMock(return_value=None))
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    project = Project(name="Empty diff block", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, pr_number=14500)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Empty-diff PR must not merge"
    merge_mock.assert_not_called()
    close_mock.assert_awaited_once()

    await db_session.refresh(task)
    # For a direct create task the chain original is the task itself.
    assert task.status == TaskStatus.CANCELLED, (
        "Empty-diff task must NOT be COMPLETED-success"
    )
    assert task.status != TaskStatus.COMPLETED
    ctx = task.context or {}
    assert ctx.get("failure_detail") == "empty_effective_diff_at_merge"
    assert ctx.get("pr_closed_empty") is True
    assert ctx.get("failure_category") == "SCOPE_EMPTY"
    assert ctx.get("pr_rejected") is not True, "Empty no-op is not a review rejection"
    # The benign no-op is recorded for monitoring as SCOPE_EMPTY.
    assert rec_task.call_args.kwargs.get("failure_class") == "SCOPE_EMPTY"


@pytest.mark.asyncio
async def test_indeterminate_effective_diff_defers_merge(db_session, mocker):
    """When the effective-diff probe is INDETERMINATE (get_pr_files raises), the
    merge is DEFERRED — not merged and not no-op'd (PR left open, task untouched
    terminal-wise)."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    # First call (migration guard) succeeds with a non-migration file so the
    # guard is 'ok'; the empty-diff probe's call then raises → indeterminate.
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(side_effect=[[{"filename": "README.md"}], RuntimeError("gh 502")]),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", AsyncMock(return_value=True)
    )

    project = Project(name="Indeterminate diff defer", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, pr_number=14501)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Indeterminate probe must defer (return False)"
    merge_mock.assert_not_called()
    close_mock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        "Deferred merge must leave the task EVALUATING, not terminalize it"
    )


@pytest.mark.asyncio
async def test_nonempty_effective_diff_still_merges(db_session, mocker):
    """Regression: a PR with a non-empty effective diff merges normally and the
    task is marked COMPLETED — the empty-diff gate does not interfere."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    # Non-empty effective diff: rely on the autouse _default_no_migration_pr
    # fixture (get_pr_files → [{"filename": "README.md"}]) so the empty-diff gate
    # sees a non-empty diff and falls through to merge — exactly the happy path.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", AsyncMock(return_value={"merged": True})
    )

    project = Project(name="Nonempty diff merges", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, pr_number=14502)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "Non-empty PR must merge"
    merge_mock.assert_awaited_once()
    # The merged happy path sets COMPLETED in-memory on the relocked task
    # instance (committed downstream by the caller / orig-session); assert the
    # in-memory status directly, mirroring the other merge-success tests.
    assert task.status == TaskStatus.COMPLETED


# ===========================================================================
# Authoritative exact-target review gate at the merge boundary
# ===========================================================================


@pytest.mark.asyncio
async def test_same_head_blocker_survives_later_approve_and_spawn_rollback(
    db_session, mocker
):
    """A same-head APPROVE cannot erase an earlier binding blocker.

    Exercise the parked CI-success entrypoint and simulate the rollback/expiry
    behavior that _spawn_pipeline_task can trigger. The gate state must still be
    restored durably, the exact target must be passed to the reviewer, and a
    second CI signal must not request another review.
    """
    from app.ci_completion import handle_ci_completion

    head_sha = "a" * 40
    base_sha = "b" * 40
    pr_number = 15001
    project = Project(name="Authoritative blocker gate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    review_history = [
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [
                {
                    "issue_id": "BP-ci-boundary-blocker",
                    "severity": "major",
                    "description": "Merge path can publish unresolved work",
                    "file": "backend/app/ci_completion.py",
                    "line": 1,
                }
            ],
        },
        {
            "iteration": 2,
            "verdict": "APPROVE",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [],
        },
    ]
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            # Simulate a stale policy marker surviving into a reviewed chain.
            # Real history must defeat and clear it.
            "review_not_required": True,
            "review_history": review_history,
            # The latest reviewer did claim approval, which enters the merge
            # boundary; the binding same-head blocker must still defeat it.
            "review_approved": True,
            "head_sha": head_sha,
            "base_sha": base_sha,
            # A stale scalar token must be cleared when authority fails.
            "verdict": "APPROVE",
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    review_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Exact target review",
        payloads=[{"type": "review"}],
        status=TaskStatus.QUEUED,
    )

    async def _spawn_after_rollback(*args, **kwargs):
        await args[2].rollback()
        return review_task, True

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_spawn_after_rollback),
    )
    ci_result = {"status": "completed", "conclusion": "success"}

    first_result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert first_result is True
    merge_mock.assert_not_awaited()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs["review_target"] == live_target

    await db_session.refresh(task)
    gated_loop = (task.context or {})["review_loop"]
    assert gated_loop["review_approved"] is False
    assert gated_loop["authoritative_review_required"] is True
    assert gated_loop["authoritative_review_task_id"] == str(review_task.id)
    assert gated_loop["authoritative_review_target"] == live_target
    assert gated_loop["issue_ledger"]["open"][0]["description"] == (
        "Merge path can publish unresolved work"
    )
    assert "verdict" not in gated_loop
    assert "review_not_required" not in gated_loop
    assert "review_policy_reason" not in gated_loop

    # The durable gate state sends later CI signals down the ordinary parked
    # path, so the same unresolved episode cannot create a duplicate reviewer.
    second_result = await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )
    assert second_result is True
    assert spawn_mock.await_count == 1
    merge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_history_with_claimed_approval_requires_exact_review(
    db_session, mocker
):
    """review_approved without history or a policy marker is unproven."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    head_sha = "c" * 40
    base_sha = "d" * 40
    pr_number = 15002
    project = Project(name="Empty history gate", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            "review_not_required": False,
            "review_history": [],
            # The lifecycle hint is deliberately unproven without exact history.
            "review_approved": True,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    review_task = _make_fake_review_task(project.id)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(review_task, True)),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert result is False
    merge_mock.assert_not_awaited()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs["review_target"] == live_target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailing_review",
    [
        {"iteration": 2, "verdict": "REQUEST_CHANGES", "issues": []},
        {"iteration": 2, "verdict": "COMMENT", "feedback": "Needs another look"},
        {"iteration": 2, "feedback": "Malformed reviewer output"},
    ],
)
async def test_latest_non_approval_cannot_reuse_older_exact_approval(
    db_session, mocker, trailing_review
):
    """Every newer review row, even malformed, invalidates an older APPROVE."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    head_sha = "e" * 40
    base_sha = "f" * 40
    pr_number = 15003
    project = Project(name="Latest review authority", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    history = [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [],
        },
        {
            **trailing_review,
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
        },
    ]
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            "review_not_required": True,
            "review_history": history,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(_make_fake_review_task(project.id), True)),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert result is False
    merge_mock.assert_not_awaited()
    spawn_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_later_comment_with_major_blocker_invalidates_exact_approval(
    db_session, mocker
):
    """Structured COMMENT evidence cannot be skipped to reuse an older APPROVE."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    head_sha = "7" * 40
    base_sha = "8" * 40
    pr_number = 15006
    project = Project(name="Comment review authority", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    blocker_description = "Comment found a same-head merge-boundary defect"
    history = [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [],
        },
        {
            "iteration": 2,
            "verdict": "COMMENT",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [
                {
                    "severity": "MAJOR",
                    "description": blocker_description,
                    "file": "backend/app/ci_completion.py",
                    "line": 204,
                }
            ],
        },
    ]
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            "review_history": history,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(_make_fake_review_task(project.id), True)),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert result is False
    merge_mock.assert_not_awaited()
    spawn_mock.assert_awaited_once()
    gated_loop = (task.context or {})["review_loop"]
    assert gated_loop["authoritative_review_required"] is True
    assert [
        row["description"] for row in gated_loop["issue_ledger"]["open"]
    ] == [blocker_description]


@pytest.mark.asyncio
async def test_unresolved_live_target_retries_exact_review_before_parking(
    db_session, mocker
):
    """A transient target lookup failure cannot strand an unproven merge."""
    from app.ci_completion import handle_ci_completion

    head_sha = "3" * 40
    base_sha = "4" * 40
    pr_number = 15005
    project = Project(name="Retry exact review target", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            "review_not_required": False,
            "review_history": [],
            "review_approved": True,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    target_mock = mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(
            side_effect=[
                RuntimeError("temporary GitHub failure"),
                live_target,
            ]
        ),
    )
    review_task = _make_fake_review_task(project.id)
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(review_task, True)),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None)
    )
    ci_result = {"status": "completed", "conclusion": "success"}

    await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )
    await db_session.refresh(task)
    first_loop = (task.context or {})["review_loop"]
    assert first_loop["authoritative_review_required"] is True
    assert "authoritative_review_task_id" not in first_loop
    assert "authoritative_review_target_unresolved_at" in first_loop
    spawn_mock.assert_not_awaited()

    await handle_ci_completion(
        task,
        ci_result,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert target_mock.await_count == 2
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs["review_target"] == live_target
    merge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_exact_approval_with_empty_ledger_can_merge(db_session, mocker):
    """A blocker-free latest APPROVE for the live target reaches the merger."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    head_sha = "1" * 40
    base_sha = "2" * 40
    pr_number = 15004
    project = Project(name="Exact approval merge", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    history = [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "target_verified_by_backend": True,
            "review_task_id": str(uuid.uuid4()),
            "issues": [],
        }
    ]
    task = _make_task(
        db_session,
        project,
        pr_number=pr_number,
        ci_loop_extra={
            "review_not_required": True,
            "review_history": history,
            # The lifecycle has consumed this exact approval before entering
            # the merge helper; history is the proof, while this scalar is part
            # of the lock-window snapshot that protects against revocation.
            "review_approved": True,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    live_target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    # Keep the merge-helper contract test isolated from the unrelated
    # dedicated-session build-config refresh.  SQLite's single test connection
    # cannot model that production session boundary without one scope rolling
    # back the other's uncommitted lifecycle mutation.
    mocker.patch(
        "app.ci_completion._refresh_empty_build_config_after_integration_landing",
        new=AsyncMock(return_value=False),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new_callable=AsyncMock
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(return_value=live_target),
    )

    result = await _attempt_merge_with_retry_and_escalation(
        task,
        "fake-token",
        "owner",
        "repo",
        db_session,
        source="poll",
    )

    assert result is True
    merge_mock.assert_awaited_once()
    assert merge_mock.await_args.kwargs["expected_head_sha"] == head_sha
    spawn_mock.assert_not_awaited()
    # The helper owns both the successful lifecycle transition and the
    # authoritative proof-state normalization.  Its caller owns the commit, so
    # model that boundary and then assert the durable row rather than the stale
    # pre-relock Python object.
    await db_session.commit()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    authoritative_loop = (task.context or {})["review_loop"]
    assert authoritative_loop["issue_ledger"]["open"] == []
    assert authoritative_loop["review_target"] == live_target
    assert "review_not_required" not in authoritative_loop
    assert "review_policy_reason" not in authoritative_loop


# ---------------------------------------------------------------------------
# Merge-time GitHub-verified changed-files provenance
#
# Root-cause regression guards for the acceptance-window-never-covered bug:
# this pipeline merges PRs PROGRAMMATICALLY here, so GitHub's
# ``pull_request.closed`` webhook — the only OTHER writer of
# ``_pr_changed_files_github_verified`` — never fires. Without the merge-time
# stamp the named_test acceptance gate evaluates every declared test against an
# EMPTY diff, classifies it ``declared_unsatisfied``, and the release
# acceptance window stays permanently 0-proven.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_success_stamps_github_verified_changed_files_on_orig(
    db_session, mocker
):
    """A successful programmatic merge stamps the merged PR's GitHub-verified
    changed-file list onto the original task before the acceptance verdict runs.

    The stamp must (a) override any untrusted agent self-report, (b) carry the
    authoritative ``_pr_changed_files_github_verified`` sentinel, and (c)
    summarize test/prod co-change computed from the REAL merged diff.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )
    # The REAL merged diff: a production module co-changed with its test.
    real_files = ["backend/app/widget.py", "backend/testing/test_widget.py"]
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(return_value=[{"filename": p} for p in real_files]),
    )

    project = Project(
        name="Merge provenance stamp test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widgets",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # orig carries a STALE, untrusted agent self-report (no github_verified
    # sentinel) that the merge-time stamp must override.
    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "add widget"}],
        context={"_pr_changed_files": ["backend/app/stale_self_report.py"]},
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    # Re-read the committed row written by the dedicated _orig_session.
    await db_session.refresh(orig_task)
    ctx = orig_task.context or {}
    assert ctx.get("_pr_changed_files_github_verified") is True, (
        "merge-time stamp must mark the changed-file list as GitHub-verified"
    )
    assert set(ctx.get("_pr_changed_files") or []) == set(real_files), (
        "stamp must REPLACE the stale agent self-report with the real PR diff"
    )
    assert ctx.get("_pr_changed_files_has_test") is True
    assert ctx.get("_pr_changed_files_has_prod") is True


@pytest.mark.asyncio
async def test_merge_success_changed_files_stamp_is_fail_silent(db_session, mocker):
    """A GitHub failure while fetching the merged PR's changed files must NEVER
    block the merge. The merge completes and the acceptance gate falls back to
    the agent self-report (no ``_pr_changed_files_github_verified`` sentinel).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        AsyncMock(return_value={"merged": True}),
    )

    # get_pr_files must succeed for the PRE-merge migration guard but fail for
    # the POST-merge provenance stamp. The reachability gate
    # (verify_rollup_merge_landed) is the last step before the stamp; mock it to
    # the test-env "indeterminate" verdict (keeps record_pr_merged True) and use
    # its invocation to arm the changed-files fetch failure — deterministically
    # separating the two get_pr_files phases without depending on the gate's own
    # internal GitHub I/O.
    phase = {"arm_failure": False}

    async def _verify_landed(*_a, **_k):
        phase["arm_failure"] = True
        return "indeterminate"

    async def _get_pr_files(*_a, **_k):
        if phase["arm_failure"]:
            raise RuntimeError("simulated GitHub outage during changed-files fetch")
        return [{"filename": "README.md"}]

    mocker.patch(
        "app.rollup_pr_verification.verify_rollup_merge_landed",
        side_effect=_verify_landed,
    )
    mocker.patch("app.github.get_pr_files", side_effect=_get_pr_files)

    project = Project(
        name="Merge provenance fail-silent test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widgets",
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task = Task(
        project_id=project.id,
        title="Original create task",
        payloads=[{"type": "create", "prompt": "add widget"}],
        context={},
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    task = _make_task(
        db_session,
        project,
        ci_loop_extra={"original_task_id": str(orig_task.id)},
    )
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    # The merge is NOT blocked by the changed-files fetch failure.
    assert result is True
    await db_session.refresh(orig_task)
    ctx = orig_task.context or {}
    assert ctx.get("_pr_changed_files_github_verified") is not True, (
        "a failed changed-files fetch must not leave a github_verified stamp"
    )


# ---------------------------------------------------------------------------
# Rollup deliverable guard — ghost-success detection (PR #17480 class)
# ---------------------------------------------------------------------------


def _deliverable_guard_common_mocks(mocker):
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True, "sha": "abc123rollupmerge"},
    )
    mocker.patch(
        "app.rollup_pr_verification.verify_rollup_merge_landed",
        new=AsyncMock(return_value="indeterminate"),
    )


async def _make_rollup_with_child(
    db_session,
    project,
    *,
    orig_files: list[str],
    child_files: list[str],
    child_pr_number: int = 555,
):
    """Rollup parent (EVALUATING, verified changed-file stamp) + merged child."""
    orig_task = Task(
        project_id=project.id,
        title="Rollup parent",
        payloads=[
            {"type": "create", "prompt": "rollup", "base_branch": "main"}
        ],
        context={
            "_pr_changed_files": orig_files,
            "_pr_changed_files_github_verified": True,
        },
        status=TaskStatus.EVALUATING,
        vm_instance_id=f"agent-vm-orig-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    child = Task(
        project_id=project.id,
        parent_task_id=orig_task.id,
        title="Child deliverable task",
        payloads=[
            {
                "type": "create",
                "prompt": "child work",
                "pr_number": child_pr_number,
            }
        ],
        context={
            "pr_merged": True,
            "_pr_changed_files": child_files,
            "_pr_changed_files_github_verified": True,
        },
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-child-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    return orig_task, child


@pytest.mark.asyncio
async def test_rollup_completes_when_child_deliverables_intact(
    db_session, mocker
):
    """A rollup whose merged diff still carries every child's files completes.

    Regression guard for the guard itself: overlap between a child's verified
    changed files and the rollup PR's verified diff must NOT trip the dropped
    or warned paths — the parent lands as plain COMPLETED "Rollup PR merged".
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _deliverable_guard_common_mocks(mocker)

    project = Project(name="Deliverable intact rollup", user_id="test-user-dg1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task, _child = await _make_rollup_with_child(
        db_session,
        project,
        orig_files=["backend/app/feature_a.py", "backend/app/feature_b.py"],
        child_files=["backend/app/feature_a.py"],
    )

    ci_task = _make_task(
        db_session,
        project,
        pr_number=901,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
            "base_branch": "main",
        },
    )
    await db_session.commit()
    await db_session.refresh(ci_task)

    result = await _attempt_merge_with_retry_and_escalation(
        ci_task, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result is True

    await db_session.refresh(orig_task)
    assert orig_task.status == TaskStatus.COMPLETED
    assert orig_task.latest_status_message == "Rollup PR merged"
    ctx = orig_task.context or {}
    assert "rollup_dropped_deliverables" not in ctx
    assert "rollup_deliverable_warning" not in ctx


@pytest.mark.asyncio
async def test_rollup_dropped_child_deliverable_blocks_ghost_success(
    db_session, mocker
):
    """Incident signature (PR #17480): conflict-fix + vanished child files.

    A conflict-fix intervened on the rollup PR and NONE of the child's
    GitHub-verified changed files survived into the merged rollup diff. The
    parent must NOT ghost-complete with "Rollup PR merged": it fails with a
    reason naming exactly WHICH child PR was dropped, stamps the durable
    receipt, and records the failure category.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _deliverable_guard_common_mocks(mocker)
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new=AsyncMock(return_value=None),
    )

    project = Project(name="Deliverable dropped rollup", user_id="test-user-dg2")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task, child = await _make_rollup_with_child(
        db_session,
        project,
        # Merged rollup diff no longer contains ANY of the child's files —
        # wholesale conflict resolution toward base erased the deliverable.
        orig_files=["backend/app/other_feature.py"],
        child_files=[
            "backend/app/dropped_feature.py",
            "backend/testing/test_dropped.py",
        ],
        child_pr_number=555,
    )

    ci_task = _make_task(
        db_session,
        project,
        pr_number=902,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
            "base_branch": "main",
            # The incident signature: a conflict-fix ran against this PR.
            "conflict_fix_spawned": str(uuid.uuid4()),
        },
    )
    await db_session.commit()
    await db_session.refresh(ci_task)

    result = await _attempt_merge_with_retry_and_escalation(
        ci_task, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result is True, "the merge itself succeeded on GitHub"

    await db_session.refresh(orig_task)
    assert orig_task.status == TaskStatus.FAILED, (
        "rollup parent must not ghost-complete when a child deliverable "
        "was dropped during conflict resolution"
    )
    assert orig_task.latest_status_message != "Rollup PR merged"
    # The WHY must name the dropped child PR.
    assert "PR #555" in (orig_task.latest_status_message or "")
    ctx = orig_task.context or {}
    dropped = ctx.get("rollup_dropped_deliverables")
    assert isinstance(dropped, list) and len(dropped) == 1
    assert dropped[0]["task_id"] == str(child.id)
    assert dropped[0]["pr_number"] == 555
    assert "backend/app/dropped_feature.py" in dropped[0]["missing_files"]
    assert "PR #555" in ctx.get("rollup_dropped_deliverables_reason", "")
    assert ctx.get("failure_category") == "CODE_ERROR"
    alert_mock.assert_awaited()


@pytest.mark.asyncio
async def test_rollup_dropped_without_conflict_evidence_warns_not_blocks(
    db_session, mocker
):
    """Zero overlap WITHOUT a conflict-fix on record is ambiguous → warn only.

    The base branch may have legitimately absorbed identical content, hiding
    the child's files from the PR diff while the deliverable IS in the merged
    result. False-blocking healthy rollups is also a platform defect, so the
    guard stamps a loud warning receipt and completion proceeds.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    _deliverable_guard_common_mocks(mocker)

    project = Project(name="Deliverable warn rollup", user_id="test-user-dg3")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task, _child = await _make_rollup_with_child(
        db_session,
        project,
        orig_files=["backend/app/other_feature.py"],
        child_files=["backend/app/superseded_feature.py"],
        child_pr_number=556,
    )

    ci_task = _make_task(
        db_session,
        project,
        pr_number=903,
        ci_loop_extra={
            "original_task_id": str(orig_task.id),
            "base_branch": "main",
            # NO conflict_fix_spawned — no incident signature.
        },
    )
    await db_session.commit()
    await db_session.refresh(ci_task)

    result = await _attempt_merge_with_retry_and_escalation(
        ci_task, "fake-token", "owner", "repo", db_session, source="poll"
    )
    assert result is True

    await db_session.refresh(orig_task)
    assert orig_task.status == TaskStatus.COMPLETED, (
        "ambiguous absence (no conflict-fix evidence) must not block completion"
    )
    assert orig_task.latest_status_message == "Rollup PR merged"
    ctx = orig_task.context or {}
    assert "rollup_dropped_deliverables" not in ctx
    warning = ctx.get("rollup_deliverable_warning")
    assert isinstance(warning, str) and "PR #556" in warning


@pytest.mark.asyncio
async def test_deliverable_guard_truncated_stamp_uses_live_pr_files(
    db_session, mocker
):
    """A truncated rollup stamp is unsound for absence testing → live fetch.

    When the parent's stamped list carries ``_pr_changed_files_truncated``,
    the guard must resolve the authoritative diff with ONE ``get_pr_files``
    call instead of trusting the 50-file sample, and still detect the drop.
    """
    from app.rollup_deliverable_guard import verify_rollup_child_deliverables

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new=AsyncMock(return_value=None),
    )
    pr_files_mock = mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(return_value=[{"filename": "backend/app/other.py"}]),
    )

    project = Project(
        name="Deliverable guard truncated", user_id="test-user-dg4"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task, child = await _make_rollup_with_child(
        db_session,
        project,
        orig_files=["backend/app/stale_sample.py"],
        child_files=["backend/app/dropped_feature.py"],
        child_pr_number=557,
    )
    orig_task.context = {
        **(orig_task.context or {}),
        "_pr_changed_files_truncated": True,
    }
    db_session.add(orig_task)
    await db_session.commit()

    verdict = await verify_rollup_child_deliverables(
        orig_task,
        project,
        db_session,
        pr_number=904,
        github_credentials=("fake-token", "owner", "repo"),
        ci_loop={"conflict_fix_spawned": str(uuid.uuid4())},
        merge_task=None,
        source="test.direct",
    )

    assert verdict == "dropped"
    pr_files_mock.assert_awaited_once()
    ctx = orig_task.context or {}
    assert ctx["rollup_dropped_deliverables"][0]["task_id"] == str(child.id)
    assert "PR #557" in ctx["rollup_dropped_deliverables_reason"]
    alert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_deliverable_guard_unverifiable_children_never_block(
    db_session, mocker
):
    """Children without a usable verified stamp are skipped, not failed.

    No GitHub-verified own-PR file list (parent-derived provenance only) and
    no credentials to gap-fill → the child is unverifiable; the guard must
    return "intact" rather than fabricate a drop verdict.
    """
    from app.rollup_deliverable_guard import verify_rollup_child_deliverables

    project = Project(name="Deliverable guard skip", user_id="test-user-dg5")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig_task, child = await _make_rollup_with_child(
        db_session,
        project,
        orig_files=["backend/app/other.py"],
        child_files=["backend/app/unrelated.py"],
    )
    # Parent-derived provenance is circular for this guard → unusable.
    child.context = {
        **(child.context or {}),
        "_pr_changed_files_from_rollup_parent": True,
    }
    db_session.add(child)
    await db_session.commit()

    verdict = await verify_rollup_child_deliverables(
        orig_task,
        project,
        db_session,
        pr_number=905,
        github_credentials=None,
        ci_loop={"conflict_fix_spawned": str(uuid.uuid4())},
        merge_task=None,
        source="test.direct",
    )

    assert verdict == "intact"
    ctx = orig_task.context or {}
    assert "rollup_dropped_deliverables" not in ctx


@pytest.mark.asyncio
async def test_pat_credential_refusal_never_writes_to_github_and_terminalizes(
    db_session, mocker
):
    """A local PAT refusal must not drive the GitHub-side escalation machinery.

    ``merge_pull_request`` refuses before any PUT when the only resolvable
    credential is the operator's own PAT, returning the same
    ``{"merged": False, "conflict": False}`` shape as a pending-checks 405.
    Treated as that, the post-merge recheck runs and the ``behind`` branch
    issues ``update_branch`` with the SAME operator PAT — an automated GitHub
    write that mints exactly the operator-attributed commit the refusal exists
    to prevent — before escalating a merge failure that is not the PR's fault.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.ci_completion.asyncio.sleep", return_value=None)
    get_pr_mock = mocker.patch(
        "app.github.get_pull_request",
        return_value={"merged": False, "state": "open", "mergeable_state": "behind"},
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={
            "merged": False,
            "conflict": False,
            "pat_fallback_blocked": True,
            "message": "refusing to merge with an operator PAT credential",
        },
    )
    update_mock = mocker.patch("app.github.update_branch", return_value={})
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(type("FT", (), {"id": uuid.uuid4()})(), True),
    )

    project = Project(name="PAT refusal terminal", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_task(db_session, project, ci_loop_extra={"ci_passed": True})
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "fake-token", "owner", "repo", db_session, source="poll"
    )

    assert result is False
    assert merge_mock.call_count == 1, "a permanent local refusal must not retry"
    update_mock.assert_not_called()
    get_pr_mock.assert_not_called()
    spawn_mock.assert_not_called()
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_failures", 0) == 0
    assert "personal access token" in (task.latest_status_message or "")
    assert "GitHub App" in (task.latest_status_message or "")
