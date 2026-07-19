"""Regression tests for the terminal-parent requeue path in admin_task_mutations.

Covers a server-side 500 in the manual task-requeue path (PATCH /api/admin/
tasks/{task_id} with body {"status": "QUEUED"}) for research/plan PARENT tasks
whose children were COMPLETED / had a MERGED rollup PR.

Two defects, both fixed in ``admin_task_mutations._apply_status_mutation`` /
``admin_task_mutations._commit_and_broadcast``:

1. The requeue path must never surface a bare, detail-less ``Internal Server
   Error``. When ``_requeue_parent_task`` (or the terminal-rollup reconciliation)
   raises, the API surfaces an actionable 500 that names the failure and rolls
   back so the task is not left half-transitioned.

2. A research parent whose children are all COMPLETED/merged is re-driven for
   rollup convergence AFTER its status has already been durably committed to
   EVALUATING. An exception in that best-effort post-commit re-drive must NOT
   turn the already-successful requeue into a 500 — failed-child parents never
   hit that re-drive, which is why they requeued fine while merged-rollup
   parents 500'd.

These live in a dedicated module (rather than the very large
``test_admin_routers.py``) so the local ``./check`` impacted-test gate selects a
fast, green file for this change instead of a pre-existing slow/failing sibling.
"""

import pytest
from fastapi import BackgroundTasks, HTTPException
from unittest.mock import AsyncMock, patch

from app.models import Project, Task, TaskStatus


async def _make_project(session) -> Project:
    project = Project(name="Requeue test project", user_id="user-1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_research_parent(session, project_id, status: TaskStatus) -> Task:
    """A terminal research PARENT carrying a tracked rollup PR number."""
    parent = Task(
        project_id=project_id,
        title="Research parent",
        payloads=[{"type": "research", "prompt": "do research"}],
        status=status,
        created_by="ORCHESTRATOR",
        context={"research_branch": "research/x", "rollup_pr_number": 42},
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)
    return parent


async def _add_child(session, project_id, parent_id, status, context=None) -> Task:
    child = Task(
        project_id=project_id,
        parent_task_id=parent_id,
        title="Child task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=status,
        created_by="ORCHESTRATOR",
        context=context or {},
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    return child


@pytest.mark.asyncio
async def test_requeue_merged_rollup_parent_survives_redrive_failure(db_session):
    """Requeuing a research parent whose child is a COMPLETED/merged rollup must
    NOT 500 when the post-commit rollup re-drive throws.

    Reproduces the operator-observed bug: a research parent with COMPLETED
    (merged-rollup) children — none FAILED — is requeued. Reconciliation re-arms
    the merged rollup, which schedules an authoritative rollup re-drive AFTER the
    parent has already been durably committed to EVALUATING. An exception in that
    best-effort re-drive must be swallowed so the already-successful requeue
    stands — it must not surface as a bare, detail-less 500.
    """
    from app.admin_helpers import modify_task_apply

    project = await _make_project(db_session)
    parent = await _make_research_parent(db_session, project.id, TaskStatus.FAILED)
    await _add_child(
        db_session,
        project.id,
        parent.id,
        TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    with (
        patch(
            "app.admin_task_mutations.broadcast_to_project",
            new_callable=AsyncMock,
        ),
        patch(
            "app.task_lifecycle.broadcast_to_project",
            new_callable=AsyncMock,
        ),
        # Reconciliation re-arms the merged rollup → schedules the re-drive.
        patch(
            "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
            new_callable=AsyncMock,
            return_value="merged_pr_rearmed",
        ),
        # The authoritative rollup re-drive blows up (github/state-specific).
        patch(
            "app.rollup_management._check_and_trigger_rollup",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom in rollup re-drive"),
        ) as redrive,
    ):
        result = await modify_task_apply(
            task=parent,
            payload={"status": TaskStatus.QUEUED.value},
            background_tasks=BackgroundTasks(),
            session=db_session,
        )

    # The re-drive was attempted (and threw) but did not propagate as a 500.
    assert redrive.await_count == 1
    await db_session.refresh(parent)
    # The parent transitioned cleanly to EVALUATING — the reaper's
    # stale-EVALUATING sweep re-drives rollup convergence later.
    assert parent.status == TaskStatus.EVALUATING
    assert result["status"] == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_requeue_failure_surfaces_actionable_detail_not_bare_500(db_session):
    """When _requeue_parent_task raises, the API surfaces the real reason.

    A requeue that cannot proceed must say WHY (a named 500), never a bare
    'Internal Server Error'. The parent must not be left half-transitioned.
    """
    from app.admin_helpers import modify_task_apply

    project = await _make_project(db_session)
    parent = await _make_research_parent(db_session, project.id, TaskStatus.FAILED)
    await _add_child(db_session, project.id, parent.id, TaskStatus.COMPLETED)

    with (
        patch(
            "app.admin_task_mutations.broadcast_to_project",
            new_callable=AsyncMock,
        ),
        patch(
            "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
            new_callable=AsyncMock,
            return_value="no_pr_cleared",
        ),
        patch(
            "app.task_lifecycle._requeue_parent_task",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom: child deref in requeue"),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await modify_task_apply(
            task=parent,
            payload={"status": TaskStatus.QUEUED.value},
            background_tasks=BackgroundTasks(),
            session=db_session,
        )

    # Actionable, non-empty detail that names the failure — never a bare 500.
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail != "Internal Server Error"
    assert "kaboom: child deref in requeue" in exc_info.value.detail
    assert "RuntimeError" in exc_info.value.detail
    # Not left half-transitioned: the rollback restored the terminal status.
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
