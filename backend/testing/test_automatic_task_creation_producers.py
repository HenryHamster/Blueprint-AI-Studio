"""Focused pause/manual-semantics regressions for mixed task producers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from sqlalchemy import select

from app.models import Project, Task, TaskStatus
from app.repo_bootstrap import spawn_bootstrap_task
from app.strategic_spawning import (
    _finalize_strategic_review_spawn,
    _spawn_design_doc_update,
)
from app.task_lifecycle_requeue import _requeue_parent_task
from app.task_locking import AUTOMATIC_TASK_CREATION_PROJECT_PAUSED


@pytest.mark.asyncio
async def test_automatic_design_doc_spawn_stops_at_paused_authority(mocker):
    authority = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(return_value=AUTOMATIC_TASK_CREATION_PROJECT_PAUSED),
    )
    session = SimpleNamespace(rollback=AsyncMock())
    project = SimpleNamespace(id=uuid.uuid4())

    spawned = await _spawn_design_doc_update(
        project,
        session,
        apply_spawn_budget_guard=True,
        expected_legacy_generation=1,
    )

    assert spawned is False
    authority.assert_awaited_once_with(session, project.id, skip_locked=True)
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_design_doc_path_does_not_use_automatic_authority(mocker):
    authority = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(side_effect=AssertionError("manual path used automatic authority")),
    )
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=True),
    )

    spawned = await _spawn_design_doc_update(
        SimpleNamespace(id=uuid.uuid4()),
        SimpleNamespace(),
        apply_spawn_budget_guard=False,
        expected_legacy_generation=1,
    )

    assert spawned is False
    authority.assert_not_awaited()


def _strategic_finalize_kwargs(project_id):
    return {
        "project_id": project_id,
        "project_name": "pause fence",
        "payload": {"type": "strategic_review"},
        "cycle_id": str(uuid.uuid4()),
        "strategy_only": True,
        "base_vision": {},
        "base_problem_model": {},
        "base_running_update": "",
        "local_runner_intent": {},
        "preferred_runner_target_id": None,
        "extra_context": None,
        "operator_steered": False,
        "steering_excerpt": None,
    }


@pytest.mark.asyncio
async def test_automatic_strategic_finalize_rechecks_pause_after_payload_build(mocker):
    authority = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(return_value=AUTOMATIC_TASK_CREATION_PROJECT_PAUSED),
    )
    session = SimpleNamespace(rollback=AsyncMock())
    project_id = uuid.uuid4()

    spawned = await _finalize_strategic_review_spawn(
        session,
        **_strategic_finalize_kwargs(project_id),
        automatic_creation=True,
    )

    assert spawned is False
    authority.assert_awaited_once_with(session, project_id, skip_locked=True)
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_strategic_finalize_preserves_explicit_path(mocker):
    authority = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(side_effect=AssertionError("manual path used automatic authority")),
    )
    mocker.patch(
        "app.autonomous_research._try_research_spawn_advisory_lock",
        new=AsyncMock(return_value=False),
    )
    session = SimpleNamespace(rollback=AsyncMock())

    spawned = await _finalize_strategic_review_spawn(
        session,
        **_strategic_finalize_kwargs(uuid.uuid4()),
        automatic_creation=False,
    )

    assert spawned is False
    authority.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_repo_bootstrap_fences_automatic_but_not_operator_retry(mocker):
    authority = mocker.patch(
        "app.task_locking.acquire_automatic_task_creation_authority",
        new=AsyncMock(return_value=AUTOMATIC_TASK_CREATION_PROJECT_PAUSED),
    )
    mocker.patch("app.repo_bootstrap.project_is_deploy_repo", return_value=False)
    project = Project(
        id=uuid.uuid4(),
        name="bootstrap pause",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        build_config={"repo_is_empty": False},
    )
    empty_result = MagicMock()
    empty_result.first.return_value = None
    session = SimpleNamespace(
        execute=AsyncMock(return_value=empty_result),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    automatic = await spawn_bootstrap_task(
        project,
        session,
        is_first_bootstrap=True,
    )
    explicit = await spawn_bootstrap_task(
        project,
        session,
        spawn_reason="operator_retry",
    )

    assert automatic.outcome == "not_eligible"
    assert automatic.reason == "automatic_task_creation:project_paused"
    assert explicit.outcome == "spawned"
    assert session.add.call_count == 3  # Project state + Task + TaskHistory
    authority.assert_awaited_once()


@pytest.mark.asyncio
async def test_paused_automatic_requeue_is_suppressed_but_explicit_retry_clones(
    db_session,
):
    project = Project(
        name="requeue pause",
        user_id="test-user-123",
        paused=True,
    )
    db_session.add(project)
    await db_session.flush()
    parent = Task(
        project_id=project.id,
        title="parent plan",
        description="plan",
        payloads=[{"type": "plan", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
    )
    db_session.add(parent)
    await db_session.flush()
    failed = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="failed child",
        description="implementation",
        payloads=[{"type": "create", "prompt": "ship", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        latest_status_message="Traceback: deterministic code failure",
        created_by="ORCHESTRATOR",
    )
    db_session.add(failed)
    await db_session.commit()

    assert await _requeue_parent_task(parent, db_session) == 0
    rows = list(
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == parent.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [failed.id]

    assert (
        await _requeue_parent_task(parent, db_session, automatic_creation=False) == 1
    )
    rows = list(
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == parent.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {row.status for row in rows} == {TaskStatus.CANCELLED, TaskStatus.QUEUED}
