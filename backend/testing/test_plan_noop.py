"""Tests for the affirmative plan no-op path (#7021 principle, one layer up).

A plan agent that has VERIFIED there is genuinely nothing to decompose (work
already shipped / not actionable) emits the sentinel
``{"subtasks": [], "no_op_reason": "<>=30 chars>"}``. The backend lands such a
plan CANCELLED — a clean terminal — instead of FAILED/CODE_ERROR, so it:

  - does NOT burn retry budget,
  - does NOT fire a failure alert,
  - is NOT classified CODE_ERROR,
  - is NOT counted as a decomposition-quality failure (subtask_merge_rate=0.0).

GUARDRAIL (load-bearing): the clean no-op requires the affirmative reason of at
least 30 chars. An empty plan with NO reason — or a reason under 30 chars, or a
malformed/None plan, or a circular plan — still FAILS. A crashed/timed-out agent
produces no reason, so it cannot masquerade as a no-op completion.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.failure_classification import SCOPE_EMPTY
from app.models import Task, TaskStatus
from app.plan_completion import _handle_plan_completion
from testing.factories import make_project, make_task


@pytest.fixture
def _quiet_mocks(mocker):
    alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)
    return {"alert": alert}


async def _setup_plan_task(db_session, mocker, base_branch="main"):
    """Create a project + plan task and patch _ensure_branch to skip GitHub."""
    project = make_project(name="PlanNoOpTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Plan: de-hardcode repo layout",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "de-hardcode", "base_branch": base_branch}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch _ensure_branch (imported lazily from app.task_completion) so a
    # subtask-producing plan never reaches GitHub. The no-op path returns
    # before _ensure_branch, so this is a no-op for the no-op tests.
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )
    return task


# ── Affirmative no-op → clean CANCELLED terminal ──────────────────────────────


@pytest.mark.asyncio
async def test_affirmative_no_op_lands_cancelled_clean(db_session, mocker, _quiet_mocks):
    """A plan with empty subtasks + a >=30-char no_op_reason → CANCELLED, clean.

    No failure alert, no CODE_ERROR classification (SCOPE_EMPTY instead), and the
    reason is persisted on task.context so operators see WHY there's no plan.
    """
    reason = (
        "shared_state.get_repo_root() already derives every path from "
        "settings.REPO_LAYOUT; no hardcoded layout remains to de-hardcode"
    )
    assert len(reason) >= 30
    plan_logs = json.dumps({"subtasks": [], "no_op_reason": reason})

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    # Clean terminal, NOT failed.
    assert task.status == TaskStatus.CANCELLED, f"expected CANCELLED, got {task.status}"
    # Reason persisted for operator visibility (#7021 plumbing).
    assert (task.context or {}).get("no_op_reason") == reason
    # Classified as the no-retry SCOPE_EMPTY clean-no-op class, NOT CODE_ERROR.
    assert (task.context or {}).get("failure_category") == SCOPE_EMPTY
    # No failure alert fired.
    _quiet_mocks["alert"].assert_not_awaited()
    # No child subtasks created.
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_affirmative_no_op_does_not_call_record_task_result(
    db_session, mocker, _quiet_mocks
):
    """The clean no-op path must NOT record a failure outcome (no retry-budget ding)."""
    rec = mocker.patch("app.routers.agent_status.record_task_result")
    reason = "endpoint /api/admin/foo already exists in routers/admin.py with the requested shape"
    assert len(reason) >= 30
    plan_logs = json.dumps({"subtasks": [], "no_op_reason": reason})

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.CANCELLED
    rec.assert_not_called()


# ── Guardrail: empty plan WITHOUT affirmation still FAILS ──────────────────────


@pytest.mark.asyncio
async def test_empty_plan_no_reason_still_fails(db_session, mocker, _quiet_mocks):
    """An empty subtasks array WITHOUT a no_op_reason is a failed plan (guardrail)."""
    plan_logs = json.dumps({"subtasks": []})

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED, f"expected FAILED, got {task.status}"
    assert (task.context or {}).get("no_op_reason") is None
    assert (task.context or {}).get("failure_category") != SCOPE_EMPTY
    _quiet_mocks["alert"].assert_awaited()


@pytest.mark.asyncio
async def test_short_reason_under_floor_still_fails(db_session, mocker, _quiet_mocks):
    """A no_op_reason under the 30-char floor cannot trivially auto-fill a no-op."""
    plan_logs = json.dumps({"subtasks": [], "no_op_reason": "already done"})  # 12 chars

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED, f"expected FAILED, got {task.status}"
    assert (task.context or {}).get("failure_category") != SCOPE_EMPTY
    _quiet_mocks["alert"].assert_awaited()


@pytest.mark.asyncio
async def test_reason_without_subtasks_key_still_fails(db_session, mocker, _quiet_mocks):
    """A plan whose JSON never parses (no subtasks key at all) → FAILED.

    _parse_plan_output returns None for output with no ``"subtasks": [`` anchor,
    so a stray no_op_reason in free text cannot reach the clean-no-op branch.
    """
    plan_logs = (
        'The work is already done because the migration already exists, '
        '"no_op_reason": "this should not be honored without a subtasks key"'
    )

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED, f"expected FAILED, got {task.status}"
    assert (task.context or {}).get("failure_category") != SCOPE_EMPTY
    _quiet_mocks["alert"].assert_awaited()


@pytest.mark.asyncio
async def test_malformed_plan_still_fails(db_session, mocker, _quiet_mocks):
    """Garbage / non-JSON output (crashed agent) → FAILED, never a no-op."""
    plan_logs = "Traceback (most recent call last): RuntimeError: agent crashed"

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("no_op_reason") is None
    _quiet_mocks["alert"].assert_awaited()


@pytest.mark.asyncio
async def test_circular_plan_still_fails(db_session, mocker, _quiet_mocks):
    """A plan with real subtasks but circular deps still FAILS (unchanged path).

    Confirms the no-op branch did not weaken the existing circular-dependency
    guard: a plan WITH subtasks never enters the empty-subtasks branch at all.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "A",
                    "prompt": "a",
                    "scope": "small",
                    "depends_on_indices": [1],
                },
                {
                    "title": "B",
                    "prompt": "b",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED
    assert "circular" in (task.latest_status_message or "").lower()
    assert (task.context or {}).get("failure_category") != SCOPE_EMPTY
    _quiet_mocks["alert"].assert_awaited()


@pytest.mark.asyncio
async def test_normal_plan_with_subtasks_unaffected(db_session, mocker, _quiet_mocks):
    """A normal plan with real subtasks is unaffected — children are created."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "feat: real work", "prompt": "do it", "scope": "small"},
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert task.status == TaskStatus.EVALUATING
    _quiet_mocks["alert"].assert_not_awaited()
