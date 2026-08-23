"""Seam tests for the approval gate as a merge-time invariant.

``requires_approval`` must be checked at the final merge primitive, including
for tasks that were parked in ``ci_waiting`` before the flag was set. The
runtime switch remains authoritative: when approval gates are disabled, a
legacy flag is advisory and cannot strand a task.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from app import shared_state
from app.models import Project, Task, TaskStatus
from app.rollup_size_preflight import SizeCapPreflightResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _patch_dedicated_sessions_to_test_engine(mocker, db_session):
    """Patch AsyncSessionLocal to use the shared test engine.

    ``_attempt_merge_with_retry_and_escalation`` opens dedicated
    ``AsyncSessionLocal`` scopes for per-task accounting.  In the test
    environment the production engine is unavailable; this fixture redirects
    those scopes to the same SQLite/Postgres engine that ``db_session`` uses,
    preventing cross-engine connection failures and ensuring the post-merge
    state is visible within the same transaction context.
    """
    dedicated_session_local = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.database.AsyncSessionLocal", dedicated_session_local)


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op.

    ``_attempt_merge_with_retry_and_escalation`` runs
    ``enforce_premerge_migration_guard`` (which calls ``github.get_pr_files``)
    AFTER the requires_approval gate. These gate tests only mock
    ``merge_pull_request``; returning an empty file list short-circuits the
    guard to ``ok`` so the gate behavior under test is what is exercised.
    """
    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}]))


@pytest.fixture(autouse=True)
def _default_size_cap_ok(mocker):
    """Default size-cap preflight to 'ok' so gate tests are unaffected.

    ``_attempt_merge_with_retry_and_escalation`` now runs
    ``preflight_rollup_size_caps`` for direct task PRs. These gate tests do
    not mock the GitHub calls that preflight makes; without this default the
    unmocked call issues a real network request and returns 'defer', causing
    the merge to return False and failing 'assert result is True'.
    """
    async def _bound_ok(*_args, expected_head_sha=None, **_kwargs):
        return SizeCapPreflightResult("ok", None, expected_head_sha)

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(side_effect=_bound_ok),
    )


@pytest.fixture
def _gates_off():
    """Set require_approval_gates=False for the test, restore after."""
    original = shared_state.runtime_config.get("require_approval_gates", True)
    shared_state.runtime_config["require_approval_gates"] = False
    try:
        yield
    finally:
        shared_state.runtime_config["require_approval_gates"] = original


@pytest.fixture
def _gates_on():
    original = shared_state.runtime_config.get("require_approval_gates", True)
    shared_state.runtime_config["require_approval_gates"] = True
    try:
        yield
    finally:
        shared_state.runtime_config["require_approval_gates"] = original


def _make_merge_task(db_session, project, *, requires_approval, pr_number=42):
    """An EVALUATING task parked at the merge gate (both review_loop gates met)."""
    checked_head = f"c5-head-{pr_number}"
    checked_base = f"c5-base-{pr_number}"
    task = Task(
        project_id=project.id,
        title="C5 merge gate task",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "blueprint/c5-branch",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": "blueprint/c5-branch",
                "base_branch": "main",
                "iteration": 1,
                "review_approved": True,
                "review_history": [
                    {
                        "review_task_id": f"c5-review-{pr_number}",
                        "verdict": "APPROVE",
                        "target_verified_by_backend": True,
                        "reviewed_base_sha": checked_base,
                        "reviewed_head_sha": checked_head,
                        "issues": [],
                    }
                ],
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": pr_number,
                    "base_sha": checked_base,
                    "head_sha": checked_head,
                },
                "ci_checked_head_sha": checked_head,
                "ci_checked_base_sha": checked_base,
                "ci_passed": True,
                "ci_waiting": True,
            }
        },
        vm_instance_id=f"agent-vm-c5-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
        requires_approval=requires_approval,
    )
    db_session.add(task)
    return task


@pytest.fixture(autouse=True)
def _exact_live_merge_target(mocker):
    """Give merge-boundary tests the same immutable pair their CI checked."""

    async def _get_target(
        _token, _owner, _repo, pr_number, *, project_id, **_kwargs
    ):
        return {
            "project_id": project_id,
            "pr_number": pr_number,
            "base_sha": f"c5-base-{pr_number}",
            "head_sha": f"c5-head-{pr_number}",
        }

    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        side_effect=_get_target,
    )
    mocker.patch(
        "app.review_policy_merge._fetch_live_review_target",
        side_effect=_get_target,
    )


# ---------------------------------------------------------------------------
# Fix #2 — merge primitive re-checks requires_approval (symptom b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_primitive_refuses_when_requires_approval_and_gates_on(
    db_session, mocker, _gates_on
):
    """With gates ON and requires_approval set AFTER ci_waiting parking, the
    merge primitive must NOT merge — it leaves the task EVALUATING and returns
    False. Pre-fix: it merged because it only checked review_approved+ci_passed.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = Project(name="C5 gate-on", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=True)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Must refuse to merge while approval gate is set"
    merge_mock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, "Task must stay EVALUATING, not FAIL"
    assert task.requires_approval is True, "Gate flag is untouched (still gated)"


@pytest.mark.asyncio
async def test_merge_primitive_proceeds_when_requires_approval_but_gates_off(
    db_session, mocker, _gates_off
):
    """With gates OFF the flag is advisory — the merge must proceed (the
    set sites no longer set it, but a legacy flag must not block forever).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.ci_completion._post_merge_summary",
        new_callable=AsyncMock,
        create=True,
    )

    project = Project(name="C5 gate-off", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=True)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "Gates off → advisory flag must not block the merge"
    merge_mock.assert_awaited()


@pytest.mark.asyncio
async def test_merge_primitive_merges_normally_when_no_gate(
    db_session, mocker, _gates_on
):
    """Control: with no approval gate set the merge proceeds as before."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = Project(name="C5 no-gate", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=False)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    merge_mock.assert_awaited()
    # _attempt_merge_with_retry_and_escalation mutates task in place; the caller
    # commits.  Check the in-memory state directly — a DB refresh here would
    # reload EVALUATING (the function sets COMPLETED but the caller has not yet
    # committed), making the assertion Postgres-only (SQLite loads uncached rows).
    assert task.status == TaskStatus.COMPLETED
