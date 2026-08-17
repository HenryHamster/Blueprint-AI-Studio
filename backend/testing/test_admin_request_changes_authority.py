"""PostgreSQL races for the admin request-changes spawn boundary."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.admin import RequestChangesPayload, request_changes
from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    APPROVAL_MERGE_LEDGER_KEY,
    approval_hold_provenance,
)
from app.closed_pr_reconciliation import (
    REVIEW_WAIT_APPROVAL_HOLD_KEY,
    REVIEW_WAIT_APPROVAL_HOLD_OWNER,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    ProjectMember,
    Task,
    TaskHistory,
    TaskStatus,
)


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("independent-session lock race requires PostgreSQL")


def _owned_hold(project_id, pr_number: int, timer: str, head: str, base: str):
    return {
        "owner": REVIEW_WAIT_APPROVAL_HOLD_OWNER,
        "project_id": str(project_id),
        "pr_number": pr_number,
        "head_sha": head,
        "base_sha": base,
        "set_at": timer,
    }


async def _seed_request_changes_attempt(db_session, *, name: str):
    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()

    timer = "2026-08-17T08:00:00+00:00"
    task = Task(
        project_id=project.id,
        title="Request changes authority source",
        description="Review this exact attempt",
        status=TaskStatus.EVALUATING,
        payloads=[
            {
                "type": "create",
                "branch": "blueprint/request-changes-authority",
                "base_branch": "automain",
                "prompt": "Review this exact attempt",
            }
        ],
        requires_approval=True,
        context={
            "_approval_set_at": timer,
            APPROVAL_FOLLOWUP_CLAIM_KEY: {"token": "obsolete-approval-followup"},
            "review_loop": {
                "iteration": 1,
                "original_task_id": "",
                "pr_number": 42,
                "branch": "blueprint/request-changes-authority",
                "base_branch": "automain",
                "head_sha": "head-42",
                "base_sha": "base-42",
                REVIEW_WAIT_APPROVAL_HOLD_KEY: _owned_hold(
                    project.id, 42, timer, "head-42", "base-42"
                ),
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task.context = {
        **(task.context or {}),
        "review_loop": {
            **((task.context or {}).get("review_loop") or {}),
            "original_task_id": str(task.id),
        },
    }
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/42",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)
    return project, task, artifact, timer


@pytest.mark.asyncio
async def test_request_changes_atomically_publishes_child_histories_and_clear(
    db_session, mocker
):
    """The exact hold clears in the same commit as child and both audits."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    project, task, _artifact, timer = await _seed_request_changes_attempt(
        db_session, name="Request changes atomic publication"
    )
    project_id = project.id
    task_id = task.id
    claim_token = "request-changes-consumed-episode"
    claim_timer = "2026-08-17T11:00:00+00:00"
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    review_loop.pop(REVIEW_WAIT_APPROVAL_HOLD_KEY, None)
    context.update(
        {
            "_approval_set_at": claim_timer,
            APPROVAL_HOLD_EPISODE_KEY: claim_token,
            APPROVAL_HOLD_PROVENANCE_KEY: {
                "episode": claim_token,
                "owner": "operator_approval_followup",
                "reason": "stalled review escalation",
                "set_at": claim_timer,
            },
            "approval_hold_reason": "stalled review escalation",
            "review_loop_terminal_outcome": {
                "kind": "review_stuck",
                "resolved": False,
            },
            "touched_critical": ["keep-this-fact.py"],
            "review_loop": review_loop,
        }
    )
    task.context = context
    claim_episode = approval_hold_provenance(task)
    task.context = {
        **context,
        APPROVAL_FOLLOWUP_CLAIM_KEY: {
            "token": claim_token,
            "approval_episode": claim_episode,
        },
    }
    db_session.add(task)
    await db_session.commit()

    result = await request_changes(
        task_id,
        RequestChangesPayload(feedback="Please fix this", action="fix"),
        db_session,
        {"user_id": "test-user-123", "email": "operator@example.com"},
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id.in_([task_id, children[0].id])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert len(children) == 1
    child = children[0]
    assert result == {"spawned_task_id": str(child.id), "action": "fix"}
    assert child.status == TaskStatus.QUEUED
    assert source.requires_approval is False
    assert (source.context or {}).get("_approval_set_at") is None
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) is None
    assert (source.context or {}).get(APPROVAL_HOLD_EPISODE_KEY) is None
    assert (source.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY) is None
    assert (source.context or {}).get("approval_hold_reason") is None
    assert (source.context or {}).get("review_loop_terminal_outcome") is None
    assert (source.context or {}).get("touched_critical") == ["keep-this-fact.py"]
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in (
        (source.context or {}).get("review_loop") or {}
    )
    assert timer not in str(source.context or {})
    assert source.latest_status_message == (
        "Changes requested — spawned fix task"
    )

    child_histories = [row for row in histories if row.task_id == child.id]
    source_histories = [row for row in histories if row.task_id == task_id]
    assert len(child_histories) == 1
    assert child_histories[0].action == "SPAWNED"
    assert child_histories[0].details == {
        "spawned_by": "REVIEW_PIPELINE",
        "type": "fix",
        "pr_number": 42,
    }
    assert len(source_histories) == 1
    assert source_histories[0].action == "MODIFIED"
    assert source_histories[0].details["changes"]["requires_approval"] is False
    assert source_histories[0].details["modified_by"] == "operator@example.com"


@pytest.mark.asyncio
async def test_request_changes_review_publishes_one_live_exact_target(
    db_session, mocker
):
    """A manual re-review is bound to one backend-fetched base/head snapshot."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    credentials = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    async def _fetch_live_pr(*_args, **_kwargs):
        assert db_session.in_transaction() is False
        return {
            "state": "open",
            "merged": False,
            "base": {"sha": "live-base-42"},
            "head": {"sha": "live-head-42"},
        }

    live_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_fetch_live_pr),
    )
    project, task, _artifact, _timer = await _seed_request_changes_attempt(
        db_session, name="Request changes exact live review target"
    )
    project_id = project.id
    task_id = task.id
    exact_target = {
        "project_id": str(project_id),
        "pr_number": 42,
        "base_sha": "live-base-42",
        "head_sha": "live-head-42",
    }

    result = await request_changes(
        task_id,
        RequestChangesPayload(feedback="Review the current PR", action="review"),
        db_session,
        {"user_id": "test-user-123", "email": "operator@example.com"},
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(
                        TaskHistory.task_id.in_([task_id, children[0].id])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert len(children) == 1
    child = children[0]
    assert result == {"spawned_task_id": str(child.id), "action": "review"}
    assert child.first_payload["type"] == "review"
    assert ((child.context or {}).get("review_loop") or {}).get(
        "review_target"
    ) == exact_target
    child_histories = [row for row in histories if row.task_id == child.id]
    source_histories = [row for row in histories if row.task_id == task_id]
    assert len(child_histories) == 1
    assert child_histories[0].action == "SPAWNED"
    assert child_histories[0].details == {
        "spawned_by": "REVIEW_PIPELINE",
        "type": "review",
        "pr_number": 42,
    }
    assert len(source_histories) == 1
    assert source_histories[0].action == "MODIFIED"
    assert source_histories[0].details["request_changes"]["action"] == "review"
    assert source.requires_approval is False
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) is None
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in (
        (source.context or {}).get("review_loop") or {}
    )
    credentials.assert_awaited_once_with(
        task_id,
        db_session,
        release_dedicated_session_before_io=True,
    )
    live_pr.assert_awaited_once_with(
        "token", "owner", "repo", 42, project_id=str(project_id)
    )


@pytest.mark.parametrize(
    "live_pr",
    [
        {
            "state": "open",
            "merged": False,
            "base": {"sha": ""},
            "head": {"sha": "live-head-42"},
        },
        {
            "state": "open",
            "merged": False,
            "base": {"sha": "live-base-42"},
            "head": {},
        },
    ],
    ids=["missing-base", "missing-head"],
)
@pytest.mark.asyncio
async def test_request_changes_review_rejects_incomplete_live_target_without_writes(
    db_session, mocker, live_pr
):
    """An unresolved live target cannot consume the approval hold or claim."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value=live_pr),
    )
    project, task, _artifact, _timer = await _seed_request_changes_attempt(
        db_session, name=f"Request changes unresolved target {live_pr!r}"
    )
    project_id = project.id
    task_id = task.id
    original_context = task.context

    with pytest.raises(HTTPException) as exc_info:
        await request_changes(
            task_id,
            RequestChangesPayload(feedback="Review the current PR", action="review"),
            db_session,
            {"user_id": "test-user-123", "email": "operator@example.com"},
        )
    assert exc_info.value.status_code == 409

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert source.requires_approval is True
    assert source.context == original_context
    assert (source.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) == {
        "token": "obsolete-approval-followup"
    }
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY in (
        (source.context or {}).get("review_loop") or {}
    )
    assert children == []
    assert histories == []


@pytest.mark.asyncio
async def test_request_changes_replacement_and_new_hold_revoke_old_callback(
    db_session, mocker
):
    """A peer replacement/new TRUE hold leaves no stale clear or obsolete child."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-42"},
                "head": {"sha": "head-42"},
            }
        ),
    )
    project, task, artifact, _timer = await _seed_request_changes_attempt(
        db_session, name="Request changes replacement revocation"
    )
    project_id = project.id
    task_id = task.id
    artifact_id = artifact.id

    from app import review_pipeline

    real_spawn = review_pipeline._spawn_pipeline_task

    async def _replace_then_spawn(*args, **kwargs):
        replacement_timer = "2026-08-17T08:05:00+00:00"
        async with AsyncSessionLocal() as peer:
            peer_task = await peer.get(Task, task_id)
            peer_artifact = await peer.get(Artifact, artifact_id)
            assert peer_task is not None
            assert peer_artifact is not None
            peer_task.requires_approval = True
            peer_task.context = {
                "_approval_set_at": replacement_timer,
                "approval_hold_reason": "independent-manual-hold",
                "replacement_sentinel": "preserve",
                "review_loop": {
                    "iteration": 1,
                    "original_task_id": str(task_id),
                    "pr_number": 91,
                    "branch": "blueprint/request-changes-authority",
                    "base_branch": "automain",
                    "head_sha": "head-91",
                    "base_sha": "base-91",
                    REVIEW_WAIT_APPROVAL_HOLD_KEY: _owned_hold(
                        project_id,
                        91,
                        replacement_timer,
                        "head-91",
                        "base-91",
                    ),
                },
            }
            peer_artifact.pr_url = "https://github.com/owner/repo/pull/91"
            peer.add(peer_task)
            peer.add(peer_artifact)
            await peer.commit()
        return await real_spawn(*args, **kwargs)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_replace_then_spawn),
    )

    with pytest.raises(HTTPException) as exc_info:
        await request_changes(
            task_id,
            RequestChangesPayload(feedback="Stale request", action="review"),
            db_session,
            {"user_id": "test-user-123", "email": "operator@example.com"},
        )
    assert exc_info.value.status_code == 500

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        fresh_artifact = await verifying_session.get(Artifact, artifact_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert fresh_artifact is not None
    assert source.requires_approval is True
    assert (source.context or {}).get("_approval_set_at") == (
        "2026-08-17T08:05:00+00:00"
    )
    assert (source.context or {}).get("approval_hold_reason") == (
        "independent-manual-hold"
    )
    assert (source.context or {}).get("replacement_sentinel") == "preserve"
    assert ((source.context or {}).get("review_loop") or {}).get(
        "pr_number"
    ) == 91
    assert fresh_artifact.pr_url.endswith("/91")
    assert children == []
    assert histories == []


@pytest.mark.asyncio
async def test_request_changes_fences_ambiguous_human_merge_intent(
    db_session, mocker
):
    """A durable maybe-sent merge receipt outranks a stale fix publication."""

    _require_postgres(db_session)
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    project, task, _artifact, _timer = await _seed_request_changes_attempt(
        db_session, name="Request changes ambiguous merge fence"
    )
    project_id = project.id
    task_id = task.id

    from app import review_pipeline

    real_spawn = review_pipeline._spawn_pipeline_task

    async def _start_merge_then_spawn(*args, **kwargs):
        async with AsyncSessionLocal() as peer:
            peer_task = await peer.get(Task, task_id)
            assert peer_task is not None
            peer_task.context = {
                **(peer_task.context or {}),
                APPROVAL_MERGE_LEDGER_KEY: {
                    "intent_id": "intent-maybe-sent",
                    "state": "ambiguous",
                    "pr_number": 42,
                    "head_sha": "head-42",
                    "base_sha": "base-42",
                },
            }
            peer.add(peer_task)
            await peer.commit()
        return await real_spawn(*args, **kwargs)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_start_merge_then_spawn),
    )

    with pytest.raises(HTTPException) as exc_info:
        await request_changes(
            task_id,
            RequestChangesPayload(feedback="Do not race the merge", action="fix"),
            db_session,
            {"user_id": "test-user-123", "email": "operator@example.com"},
        )
    assert exc_info.value.status_code == 500

    await db_session.rollback()
    async with AsyncSessionLocal() as verifying_session:
        source = await verifying_session.get(Task, task_id)
        children = (
            (
                await verifying_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != task_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        histories = (
            (
                await verifying_session.execute(
                    select(TaskHistory).where(TaskHistory.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )

    assert source is not None
    assert source.requires_approval is True
    assert (source.context or {})[APPROVAL_MERGE_LEDGER_KEY]["state"] == (
        "ambiguous"
    )
    assert children == []
    assert histories == []
