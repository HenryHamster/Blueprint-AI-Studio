"""Exact two-window DEDUP authority for CI review-wait recovery."""

import uuid
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.models import Project, Task, TaskStatus


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


@pytest.mark.parametrize("spawn_outcome", ["created", "dedup"])
@pytest.mark.asyncio
async def test_exact_backstop_owned_gate_can_spawn_and_is_consumed(
    db_session, mocker, spawn_outcome
):
    """The exact automatic hold is resumable and released by its spawn result."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.closed_pr_reconciliation import (
        REVIEW_WAIT_APPROVAL_HOLD_KEY,
        REVIEW_WAIT_APPROVAL_HOLD_OWNER,
    )

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name=f"Owned gate {spawn_outcome}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = _review_wait_target(project.id)
    child = _make_fake_review_task(project.id, review_target=target)
    if spawn_outcome == "dedup":
        db_session.add(child)

    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    approval_set_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={
            "review_wait_started": old_started,
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
                "project_id": str(project.id),
                "pr_number": 42,
                "base_sha": _REVIEW_WAIT_BASE_SHA,
                "head_sha": _REVIEW_WAIT_HEAD_SHA,
                "set_at": approval_set_at,
            },
        },
    )
    task.context = {**(task.context or {}), "_approval_set_at": approval_set_at}
    task.requires_approval = True
    await db_session.commit()

    async def _spawn(_source, _task_type, source_session, *args, **kwargs):
        authoritative_source = await kwargs["source_spawn_authority"](source_session)
        assert authoritative_source is not None
        return (child, spawn_outcome == "created")

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock(side_effect=_spawn)
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
    spawn_mock.assert_awaited_once()
    await db_session.refresh(task)
    loop = (task.context or {})["review_loop"]
    assert task.requires_approval is not True
    assert (task.context or {}).get("_approval_set_at") is None
    assert loop.get(REVIEW_WAIT_APPROVAL_HOLD_KEY) is None
    assert loop.get("review_wait_respawn_task_id") == str(child.id)
    assert loop.get("review_wait_respawn_count") == 1
    alert_mock.assert_not_awaited()


@pytest.mark.parametrize("existing_status", [TaskStatus.BLOCKED, TaskStatus.PARKED])
@pytest.mark.asyncio
async def test_review_wait_exact_dedup_consumes_one_timeout_budget_then_escalates(
    db_session, mocker, existing_status
):
    """An exact stalled review gets one adopted window, not infinite windows."""
    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import SPAWN_REASON_DEDUP, SPAWN_REASON_KEY

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))

    project = Project(name="Review wait exact dedup", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = _review_wait_target(project.id)
    existing = _make_fake_review_task(
        project.id,
        review_target=target,
        status=existing_status,
    )
    db_session.add(existing)
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
        authoritative_source = await kwargs["source_spawn_authority"](source_session)
        assert authoritative_source is not None
        authoritative_source.context = {
            **(authoritative_source.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_DEDUP,
        }
        source_session.add(authoritative_source)
        return (existing, False)

    spawn_mock = mocker.patch(
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
    spawn_mock.assert_awaited_once()
    await db_session.refresh(task)
    loop = (task.context or {})["review_loop"]
    assert loop.get("review_wait_respawn_task_id") == str(existing.id)
    assert loop.get("authoritative_review_task_id") == str(existing.id)
    assert loop.get("authoritative_review_target") == target
    assert loop.get("review_wait_started") != old_started
    assert loop.get("review_wait_respawn_count") == 1
    assert loop.get("review_approved") is not True
    assert task.requires_approval is not True
    alert_mock.assert_not_called()

    # The same exact active row is still stalled for another complete window.
    # The first DEDUP adoption must have consumed the per-episode budget, so this
    # pass escalates without invoking the spawner or creating a duplicate child.
    loop = dict(loop)
    loop["review_wait_started"] = old_started
    task.context = {**(task.context or {}), "review_loop": loop}
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

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
    assert spawn_mock.await_count == 1
    await db_session.refresh(task)
    loop = (task.context or {})["review_loop"]
    assert loop.get("review_wait_respawn_count") == 1
    assert loop.get("review_wait_respawn_task_id") == str(existing.id)
    assert loop.get("review_wait_escalated") is True
    assert task.requires_approval is True
    alert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_wait_cross_type_defer_is_bounded_then_escalates(
    db_session, mocker
):
    """Repeated active-work defers preserve elapsed time and cannot park forever."""

    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import SPAWN_REASON_DEFERRED, SPAWN_REASON_KEY

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    project = Project(name="Bounded review defer", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = _review_wait_target(project.id)
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={"review_wait_started": old_started},
    )
    await db_session.commit()

    async def _defer(source_task, _task_type, source_session, *args, **kwargs):
        assert kwargs.get("review_target") == target
        authoritative_source = await kwargs["source_spawn_authority"](source_session)
        assert authoritative_source is not None
        authoritative_source.context = {
            **(authoritative_source.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_DEFERRED,
        }
        source_session.add(authoritative_source)
        return (None, False)

    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock(side_effect=_defer)
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
    loop = dict((task.context or {})["review_loop"])
    assert loop["review_wait_started"] == old_started
    assert loop.get("review_wait_deferred_at")
    assert task.requires_approval is not True
    alert_mock.assert_not_called()

    loop["review_wait_deferred_at"] = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=6)
    ).isoformat()
    task.context = {**(task.context or {}), "review_loop": loop}
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

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
    loop = (task.context or {})["review_loop"]
    assert loop.get("review_wait_deferred_at") is None
    assert loop.get("review_wait_escalated") is True
    assert task.requires_approval is True
    alert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_wait_admission_pause_resets_clock_without_escalation(
    db_session, mocker
):
    """A real project-admission pause remains a pause, not a human gate."""

    from datetime import datetime, timedelta

    from app.ci_completion import handle_ci_completion
    from app.review_pipeline import (
        SPAWN_REASON_ADMISSION_PAUSED,
        SPAWN_REASON_KEY,
    )

    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    project = Project(name="Paused review wait", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    old_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=70)
    ).isoformat()
    task = _make_task_no_review_approved(
        db_session,
        project,
        ci_loop_extra={
            "review_wait_started": old_started,
            "review_wait_deferred_at": old_started,
        },
    )
    await db_session.commit()

    async def _pause(_source, _task_type, source_session, *args, **kwargs):
        authoritative_source = await kwargs["source_spawn_authority"](source_session)
        assert authoritative_source is not None
        authoritative_source.context = {
            **(authoritative_source.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_ADMISSION_PAUSED,
        }
        source_session.add(authoritative_source)
        return (None, False)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task", new=AsyncMock(side_effect=_pause)
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
    assert loop["review_wait_started"] != old_started
    assert loop.get("review_wait_deferred_at") is None
    assert loop.get("review_wait_escalated") is not True
    assert task.requires_approval is not True
    alert_mock.assert_not_called()
