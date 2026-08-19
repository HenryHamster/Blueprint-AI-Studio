"""Focused invariants for the final review/merge authority boundary."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.ci_merge_helpers import _review_not_required
from app.issue_ledger import build_issue_ledger
from app.models import Project, Task, TaskStatus
from app.review_merge_authority import (
    attempt_merge_with_authority,
    enforce_merge_authority,
)


@pytest.mark.parametrize(
    "prior_state",
    [
        {"issue_ledger": {"open": [], "resolved": []}},
        {"review_wait_started": "2026-08-01T00:00:00"},
        {"authoritative_review_required": True},
        {"review_approved": True},
    ],
)
def test_no_review_marker_cannot_bypass_prior_review_state(prior_state):
    loop = {
        "review_not_required": True,
        "review_policy_reason": "cheap path",
        "review_history": [],
        **prior_state,
    }

    assert _review_not_required(loop) is False


def _target(project: Project, pr_number: int) -> dict:
    return {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": f"base-{pr_number}",
        "head_sha": f"head-{pr_number}",
    }


def _loop(project: Project, pr_number: int, mode: str) -> dict:
    target = _target(project, pr_number)
    loop = {
        "pr_number": pr_number,
        "branch": f"blueprint/task-{pr_number}",
        "base_branch": "main",
        "ci_passed": True,
        "ci_checked_base_sha": target["base_sha"],
        "ci_checked_head_sha": target["head_sha"],
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
    }
    if mode == "bypass":
        loop.update(
            {
                "review_not_required": True,
                "review_policy_reason": "small exact-target change",
                "review_target": target,
                "review_history": [],
            }
        )
    else:
        loop["review_approved"] = True
        approval = {
            "iteration": 1,
            "verdict": "APPROVE",
            "review_task_id": str(uuid.uuid4()),
            "target_verified_by_backend": True,
            "reviewed_base_sha": target["base_sha"],
            "reviewed_head_sha": target["head_sha"],
            "issues": [],
        }
        loop["review_history"] = [approval]
        loop["issue_ledger"] = build_issue_ledger(
            [approval], stamp_complete=True
        )
    return loop


async def _make_task(db_session, project: Project, pr_number: int, mode: str) -> Task:
    task = Task(
        project_id=project.id,
        title="Merge authority source",
        payloads=[
            {
                "type": "create",
                "pr_number": pr_number,
                "branch": f"blueprint/task-{pr_number}",
                "base_branch": "main",
            }
        ],
        context={"review_loop": _loop(project, pr_number, mode)},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bypass", "approval"])
async def test_exact_live_pair_locks_before_query_and_merge(
    db_session, mocker, mode
):
    project = Project(name=f"Authority {mode}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17001 if mode == "bypass" else 17002
    task = await _make_task(db_session, project, pr_number, mode)
    target = _target(project, pr_number)
    events: list[str] = []

    async def _get_pr(*_args, **_kwargs):
        events.append("get")
        return {
            "base": {"sha": target["base_sha"]},
            "head": {"sha": target["head_sha"]},
        }

    async def _lock(*_args, **_kwargs):
        events.append("lock")

    original_execute = db_session.execute

    async def _execute(*args, **kwargs):
        events.append("select")
        return await original_execute(*args, **kwargs)

    async def _merge(expected_head: str):
        events.append("merge")
        assert expected_head == target["head_sha"]
        return {"merged": True}

    mocker.patch("app.github.get_pull_request", new=AsyncMock(side_effect=_get_pr))
    mocker.patch(
        "app.task_locking.acquire_merge_submission_locks",
        new=AsyncMock(side_effect=_lock),
    )
    mocker.patch.object(db_session, "execute", side_effect=_execute)

    decision, result = await attempt_merge_with_authority(
        task,
        db_session,
        merge_call=_merge,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
    )

    assert decision.allowed is True
    assert result == {"merged": True}
    assert (task.context or {}).get("pr_merged") is None
    assert events[-1] == "merge"
    assert events.index("lock") < events.index("get") < events.index("merge")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bypass", "approval"])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_pending_exact_proof_is_flushed_after_merge_locks(
    db_session, mocker, mode, autoflush
):
    """A clean caller's exact proof is persisted only after authority locks.

    Lifecycle keeps the proof local through its pre-merge ORM reads.  The
    boundary must stage it after base -> PR locks and preserve it across the
    locked reload for both sessionmaker configurations.
    """
    project = Project(name=f"Pending authority {mode}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17010 if mode == "bypass" else 17011
    task = Task(
        project_id=project.id,
        title="Pending merge authority source",
        payloads=[
            {
                "type": "create",
                "pr_number": pr_number,
                "branch": f"blueprint/task-{pr_number}",
                "base_branch": "main",
            }
        ],
        context={"review_loop": {}},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    pending_loop = _loop(project, pr_number, mode)

    target = _target(project, pr_number)
    events: list[str] = []
    lock_autoflush: list[bool] = []
    flush_objects: list[object] = []

    async def _lock(*_args, **_kwargs):
        events.append("lock")
        lock_autoflush.append(db_session.sync_session.autoflush)

    original_flush = db_session.flush

    async def _flush(objects=None):
        events.append("flush")
        flush_objects.append(objects)
        return await original_flush(objects)

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": target["base_sha"], "ref": "main"},
                "head": {"sha": target["head_sha"]},
            }
        ),
    )
    lock_mock = mocker.patch(
        "app.task_locking.acquire_merge_submission_locks",
        new=AsyncMock(side_effect=_lock),
    )
    mocker.patch.object(db_session, "flush", new=AsyncMock(side_effect=_flush))
    merge_mock = AsyncMock(return_value={"merged": True})

    previous_autoflush = db_session.sync_session.autoflush
    db_session.sync_session.autoflush = autoflush
    try:
        decision, result = await attempt_merge_with_authority(
            task,
            db_session,
            merge_call=merge_mock,
            gh_token="token",
            owner="owner",
            repo="repo",
            ci_loop=pending_loop,
            pr_number=pr_number,
            branch=f"blueprint/task-{pr_number}",
            base_branch="main",
            original_task_id=str(task.id),
            checked_head_sha=target["head_sha"],
            checked_base_sha=target["base_sha"],
        )
        assert db_session.sync_session.autoflush is autoflush
    finally:
        db_session.sync_session.autoflush = previous_autoflush

    assert decision.allowed is True
    assert result == {"merged": True}
    lock_mock.assert_awaited_once()
    assert events[:2] == ["lock", "flush"]
    assert lock_autoflush == [False]
    assert flush_objects == [[task]]
    merge_mock.assert_awaited_once_with(target["head_sha"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bypass", "approval"])
@pytest.mark.parametrize("advanced", ["base", "head"])
async def test_live_pair_advance_fails_inside_lock_before_merge(
    db_session, mocker, mode, advanced
):
    project = Project(name=f"Advance {mode} {advanced}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17100 + (1 if mode == "approval" else 0)
    task = await _make_task(db_session, project, pr_number, mode)
    target = _target(project, pr_number)
    live_base = "advanced-base" if advanced == "base" else target["base_sha"]
    live_head = "advanced-head" if advanced == "head" else target["head_sha"]
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"base": {"sha": live_base}, "head": {"sha": live_head}}
        ),
    )
    lock_mock = mocker.patch(
        "app.task_locking.acquire_merge_submission_locks", new_callable=AsyncMock
    )
    merge_mock = AsyncMock(return_value={"merged": True})

    decision, result = await attempt_merge_with_authority(
        task,
        db_session,
        merge_call=merge_mock,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
    )

    assert decision.allowed is False
    assert result is None
    lock_mock.assert_awaited_once()
    merge_mock.assert_not_awaited()
    assert decision.review_loop["ci_passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fix_status", [TaskStatus.BLOCKED, TaskStatus.PARKED])
async def test_unrelated_active_task_cannot_hide_active_fix(
    db_session, mocker, fix_status
):
    project = Project(name=f"Active fix {fix_status}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17200
    task = await _make_task(db_session, project, pr_number, "bypass")
    target = _target(project, pr_number)
    sibling_review = Task(
        project_id=project.id,
        title="Unrelated active review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        status=TaskStatus.QUEUED,
    )
    active_fix = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Binding active fix",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        status=fix_status,
    )
    db_session.add_all([sibling_review, active_fix])
    await db_session.commit()
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "base": {"sha": target["base_sha"]},
                "head": {"sha": target["head_sha"]},
            }
        ),
    )

    decision = await enforce_merge_authority(
        task,
        db_session,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
    )

    assert decision.allowed is False
    assert decision.review_loop["ci_fix_spawned"] == str(active_fix.id)
    assert decision.review_loop["review_approved"] is False
    assert "review_not_required" not in decision.review_loop
    await db_session.refresh(task)
    assert (task.context or {})["review_loop"] == decision.review_loop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "terminal_status"),
    [("review", TaskStatus.COMPLETED), ("fix", TaskStatus.FAILED)],
)
async def test_terminal_post_commit_work_holds_publication(
    db_session, mocker, task_type, terminal_status
):
    project = Project(name=f"Pending {task_type}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17300 if task_type == "review" else 17301
    task = await _make_task(db_session, project, pr_number, "bypass")
    target = _target(project, pr_number)
    pending = Task(
        project_id=project.id,
        title=f"Terminal pending {task_type}",
        payloads=[{"type": task_type, "pr_number": pr_number}],
        context={"review_loop": {"review_target": target}},
        status=terminal_status,
        post_commit_pending=True,
    )
    db_session.add(pending)
    await db_session.commit()
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "base": {"sha": target["base_sha"]},
                "head": {"sha": target["head_sha"]},
            }
        ),
    )

    decision = await enforce_merge_authority(
        task,
        db_session,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
    )

    assert decision.allowed is False
    if task_type == "fix":
        assert decision.review_loop["ci_fix_spawned"] == str(pending.id)
        assert decision.review_loop["review_approved"] is False
    else:
        assert decision.review_loop["authoritative_review_task_id"] == str(
            pending.id
        )


@pytest.mark.asyncio
async def test_archived_pending_fix_cannot_hold_publication(db_session, mocker):
    project = Project(name="Archived pending authority", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17302
    task = await _make_task(db_session, project, pr_number, "bypass")
    target = _target(project, pr_number)
    archived_fix = Task(
        project_id=project.id,
        title="Explicitly retired continuation",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        status=TaskStatus.FAILED,
        post_commit_pending=True,
        archived=True,
    )
    db_session.add(archived_fix)
    await db_session.commit()
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": target["base_sha"], "ref": "main"},
                "head": {"sha": target["head_sha"]},
            }
        ),
    )
    merge = AsyncMock(return_value={"merged": True})

    decision, result = await attempt_merge_with_authority(
        task,
        db_session,
        merge_call=merge,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
    )

    assert decision.allowed is True
    assert result == {"merged": True}
    merge.assert_awaited_once_with(target["head_sha"])


@pytest.mark.asyncio
async def test_pat_credential_refusal_is_terminal_for_the_autonomous_retry_loop(
    db_session, mocker
):
    """A local PAT refusal must stop the retry loop after ONE attempt.

    ``merge_pull_request`` refuses before any PUT when the only resolvable
    credential is the operator's own PAT, and returns the pending-CI shape
    ``{"merged": False, "conflict": False}``.  Without an explicit terminal
    test that shape is read as a transient GitHub-side block, so the
    ``retry_nonterminal`` lane burns three more identically-refused attempts.
    """
    project = Project(name="PAT refusal terminal", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 17410
    task = await _make_task(db_session, project, pr_number, "bypass")
    target = _target(project, pr_number)
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": target["base_sha"], "ref": "main"},
                "head": {"sha": target["head_sha"]},
            }
        ),
    )
    refusal = {
        "merged": False,
        "conflict": False,
        "pat_fallback_blocked": True,
        "message": "refusing to merge with an operator PAT credential",
    }
    merge = AsyncMock(return_value=refusal)

    decision, result = await attempt_merge_with_authority(
        task,
        db_session,
        merge_call=merge,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=(task.context or {})["review_loop"],
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        checked_head_sha=target["head_sha"],
        checked_base_sha=target["base_sha"],
        retry_nonterminal=True,
    )

    assert decision.allowed is True
    assert result == refusal
    merge.assert_awaited_once_with(target["head_sha"])
