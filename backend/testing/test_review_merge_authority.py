"""Focused invariants for the final review/merge authority boundary."""

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from app.ci_merge_helpers import _review_not_required
from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.issue_ledger import build_issue_ledger
from app.models import Project, Task, TaskStatus
from app.review_loop_convergence import REVIEW_LOOP_ABANDON_CANDIDATE_KEY
from app.review_merge_authority import (
    attempt_merge_with_authority,
    enforce_merge_authority,
)
from app.review_merge_gate import (
    _EXACT_TARGET_REVIEW_SPAWN_BUDGET,
    _enforce_authoritative_review_merge_gate,
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


def _clean_approval(project: Project, pr_number: int, iteration: int) -> dict:
    target = _target(project, pr_number)
    return {
        "iteration": iteration,
        "verdict": "APPROVE",
        "review_task_id": str(uuid.uuid4()),
        "target_verified_by_backend": True,
        "reviewed_base_sha": target["base_sha"],
        "reviewed_head_sha": target["head_sha"],
        "issues": [],
        "resolutions": [],
    }


async def _gate_task(db_session, project: Project, pr_number: int, loop: dict) -> Task:
    task = Task(
        project_id=project.id,
        title="Ledger lineage gate source",
        payloads=[
            {
                "type": "create",
                "prompt": "do the thing",
                "pr_number": pr_number,
                "branch": f"blueprint/task-{pr_number}",
                "base_branch": "main",
            }
        ],
        context={"review_loop": loop},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("latched", [True, False])
async def test_a_latched_ledger_is_repaired_and_does_not_reach_the_operator(
    db_session, mocker, latched
):
    """A gate no review could satisfy is now repaired, not escalated.

    Defect this exists for: once the durable ledger latched ``complete=false``
    with a lineage marker at the history cap, the gate re-raised the synthetic
    ``BP-ledger-proof-incomplete`` blocker on every pass.  NeurIPS PR #1769
    burned 65 byte-identical APPROVE reviews across 78 attempts that way, and
    the stop was then attributed to the fix agent failing to converge.

    This test used to assert the gate stopped buying reviews and handed the PR
    to an operator instead.  That was the right call for an unsatisfiable gate,
    but the gate is no longer unsatisfiable: ``build_issue_ledger`` re-derives
    the ledger from the retained window on every pass and now re-anchors a seed
    whose marker has fallen out of that window, so the very input the old
    escalation fired on resolves to a complete proof.  Escalating a state that
    self-heals is strictly worse than the burn it replaced -- it parks a
    mergeable PR on a human.  So the ``latched`` leg asserts the repair and the
    merge, and the marker's continued absence pins that the hand-off is gone.

    The ``latched=False`` leg is unchanged and is the control: an ordinary open
    blocker must still spawn its review and still block, so this cannot pass by
    merging everything.
    """
    project = Project(name=f"Lineage {latched}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pr_number = 17801 if latched else 17802
    target = _target(project, pr_number)
    history = [
        _clean_approval(project, pr_number, iteration)
        for iteration in range(1, 1 + REVIEW_HISTORY_MAX_ENTRIES)
    ]
    if latched:
        # The exact live shape read off task 70c4d06b: a marker, complete=false,
        # and a full retained window.  The marker matches no row in the window.
        #
        # It carries the SAME real open blocker as the control leg, so both legs
        # stay fail-closed and this test can only ever be about the bookkeeping
        # difference between them -- never about whether a merge is allowed.
        # That the repaired ledger goes on to clear a genuinely clean gate is
        # asserted separately, by
        # ``test_repaired_ledger_lets_a_clean_pr_merge_without_an_operator``.
        ledger = {
            "open": [
                {
                    "issue_id": "BP-real-blocker",
                    "severity": "major",
                    "description": "an actual finding in the diff",
                }
            ],
            "resolved": [],
            "schema_version": 1,
            "complete": False,
            "through_entry_key": "883a8476fd18685e2939b861",
        }
    else:
        # Same history length, but a ledger whose lineage was maintained round
        # by round the way the review loop does it -- so it is complete, the
        # proof holds, and only the real finding blocks the merge.
        ledger = None
        for index in range(1, len(history) + 1):
            ledger = build_issue_ledger(
                history[:index],
                **({"seed_ledger": ledger} if ledger is not None else {}),
                stamp_complete=True,
            )
        assert ledger["complete"] is True
        ledger["open"] = [
            {
                "issue_id": "BP-real-blocker",
                "severity": "major",
                "description": "an actual finding in the diff",
            }
        ]

    loop = {
        "pr_number": pr_number,
        "branch": f"blueprint/task-{pr_number}",
        "base_branch": "main",
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
        "review_history": history,
        "issue_ledger": ledger,
        "review_target": target,
    }
    task = await _gate_task(db_session, project, pr_number, loop)

    spawned: list[str] = []

    async def _spawn(_task, kind, *_args, **_kwargs):
        spawned.append(kind)
        return None, False

    mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)

    allowed = await _enforce_authoritative_review_merge_gate(
        task,
        db_session,
        ci_loop=dict(loop),
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        live_review_target=target,
    )

    assert allowed is False, "the gate stays fail-closed in both legs"

    context = task.context or {}
    marker = context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY)

    if latched:
        rebuilt = context["review_loop"]["issue_ledger"]
        assert rebuilt["complete"] is True, (
            "a seed whose marker has fallen out of the window must be recovered "
            "by replaying that window, not stamped unprovable forever"
        )
        assert rebuilt["through_entry_key"] != "883a8476fd18685e2939b861", (
            "the recovery must re-anchor the marker; handing back the marker "
            "this window could not find is what made the latch permanent"
        )
        assert not any(
            str(blocker.get("issue_id") or "") == "BP-ledger-proof-incomplete"
            for blocker in rebuilt.get("open") or []
        ), "the synthetic bookkeeping blocker must not be minted any more"
        assert marker is None, (
            "the operator hand-off must be gone: a state that self-heals on the "
            "next build must never be escalated to a human"
        )
        assert "adjudication" not in (task.latest_status_message or "").lower()
        assert spawned == ["review"], (
            "the loop must be alive again -- the real blocker still gets its "
            "exact-target review instead of the chain being parked"
        )
    else:
        assert spawned == ["review"], (
            "an ordinary blocker must still get its exact-target review"
        )
        assert marker is None, (
            "a recoverable gate must never be escalated to the operator"
        )


@pytest.mark.asyncio
async def test_repaired_ledger_lets_a_clean_pr_merge_without_an_operator(
    db_session, mocker
):
    """The unwedge, end to end: latched bookkeeping over a genuinely clean PR.

    Fifty backend-verified approvals of the live target, no findings anywhere,
    and a durable ledger whose lineage marker matches no retained row. Nothing
    about this PR is unresolved -- the only thing that ever held it was a hash
    that stopped matching. It used to be held forever and then handed to a
    human; it must merge.

    Kept separate from the fail-closed pair above on purpose: that test proves
    the bookkeeping repair while both its legs stay blocked, so no assertion
    there can ever be relaxed into allowing a merge. This one is where an
    allowed merge is the subject, and it earns it with an empty open set.
    """
    project = Project(name="Repaired clean", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pr_number = 17803
    target = _target(project, pr_number)
    history = [
        _clean_approval(project, pr_number, iteration)
        for iteration in range(1, 1 + REVIEW_HISTORY_MAX_ENTRIES)
    ]
    loop = {
        "pr_number": pr_number,
        "branch": f"blueprint/task-{pr_number}",
        "base_branch": "main",
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
        "review_history": history,
        "issue_ledger": {
            "open": [],
            "resolved": [],
            "schema_version": 1,
            "complete": False,
            "through_entry_key": "883a8476fd18685e2939b861",
        },
        "review_target": target,
    }
    task = await _gate_task(db_session, project, pr_number, loop)

    spawned: list[str] = []

    async def _spawn(_task, kind, *_args, **_kwargs):
        spawned.append(kind)
        return None, False

    mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)

    allowed = await _enforce_authoritative_review_merge_gate(
        task,
        db_session,
        ci_loop=dict(loop),
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        live_review_target=target,
    )

    assert allowed is True
    assert spawned == [], "nothing was blocking, so nothing needed reviewing"
    context = task.context or {}
    assert context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None
    assert context["review_loop"]["review_approved"] is True


def _blocked_loop(project: Project, pr_number: int) -> dict:
    """A gate state that is fail-closed on a real finding, not on bookkeeping.

    Short history on purpose: the synthetic ``BP-ledger-proof-incomplete``
    blocker is only minted at ``REVIEW_HISTORY_MAX_ENTRIES``, so this exercises
    the ordinary-blocker path rather than the lineage escalation next to it.
    """
    target = _target(project, pr_number)
    approval = _clean_approval(project, pr_number, 1)
    ledger = build_issue_ledger([approval], stamp_complete=True)
    ledger["open"] = [
        {
            "issue_id": "BP-real-blocker",
            "severity": "major",
            "description": "an actual finding in the diff",
        }
    ]
    return {
        "pr_number": pr_number,
        "branch": f"blueprint/task-{pr_number}",
        "base_branch": "main",
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
        "review_history": [approval],
        "issue_ledger": ledger,
        "review_target": target,
    }


async def _drive_gate(task, db_session, pr_number: int, target: dict) -> bool:
    """Run one merge-gate tick the way the CI monitor does.

    The loop is re-read from the task's own context every time, which is the
    only way a durable per-target budget can be observed at all.
    """
    return await _enforce_authoritative_review_merge_gate(
        task,
        db_session,
        ci_loop=dict((task.context or {}).get("review_loop") or {}),
        pr_number=pr_number,
        branch=f"blueprint/task-{pr_number}",
        base_branch="main",
        original_task_id=str(task.id),
        live_review_target=target,
    )


@pytest.mark.asyncio
async def test_exact_target_review_respawn_is_budgeted_then_escalated(
    db_session, mocker
):
    """A head that never moves cannot be re-reviewed without end.

    Defect this exists for: PR #17693 took 441 exact-target review spawns over
    9.5 hours -- one every ~66 seconds -- each occupying a slot on a
    three-runner fleet, and nothing stopped it. The per-chain iteration cap
    could not: it counts actioned *generations*, and
    ``actioned_review_cap_tasks`` deliberately collapses every review of one
    target into a single generation, so ``chain_iterations`` stayed at 1
    forever. Nothing else was counting, and 441 identical tasks raised no
    alert.

    Re-reviewing the same target a small number of times is legitimate -- the
    blocker set accumulates across rows, so a later reviewer can refute an
    earlier one. Doing it without end is not: with the head pinned, every
    spawn reads the identical diff.
    """
    project = Project(name="Respawn budget", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pr_number = 17693
    target = _target(project, pr_number)
    task = await _gate_task(
        db_session, project, pr_number, _blocked_loop(project, pr_number)
    )

    spawned: list[str] = []

    async def _spawn(_task, kind, *_args, **_kwargs):
        spawned.append(kind)
        return types.SimpleNamespace(id=uuid.uuid4()), True

    mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)

    # Ten ticks against an unchanged head. The pre-fix gate spawned on every
    # single one; there was no number of ticks that would have stopped it.
    for _ in range(10):
        assert await _drive_gate(task, db_session, pr_number, target) is False

    assert len(spawned) == _EXACT_TARGET_REVIEW_SPAWN_BUDGET, (
        "the gate must stop buying reviews of a diff it has already had "
        f"adjudicated {_EXACT_TARGET_REVIEW_SPAWN_BUDGET} times"
    )

    context = task.context or {}
    marker = context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY)
    assert isinstance(marker, dict), (
        "stopping silently would just be a quieter wedge -- the stop has to "
        "reach an operator"
    )
    assert marker["reason"] == "exact_target_review_budget_exhausted"
    assert marker["pr_number"] == pr_number
    assert marker["spawned_reviews"] == _EXACT_TARGET_REVIEW_SPAWN_BUDGET
    assert marker["review_target"] == {
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
    }
    assert "BP-real-blocker" in marker["open_issue_ids"]

    # Surface the reason, not the state.
    message = task.latest_status_message or ""
    assert str(_EXACT_TARGET_REVIEW_SPAWN_BUDGET) in message
    assert "unchanged head" in message
    assert "adjudication" in message.lower()


@pytest.mark.asyncio
async def test_a_review_still_in_flight_does_not_spend_the_respawn_budget(
    db_session, mocker
):
    """Budget is spent by new reviews, not by ticks.

    The gate is re-entered on every CI poll while a spawned review is still
    running, and ``_spawn_pipeline_task`` answers those with the same active
    row and ``created=False``. Charging them would exhaust the budget within
    minutes against a single review that had not reported yet -- turning a
    fix for a runaway into a brand-new false park.
    """
    project = Project(name="In-flight reuse", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pr_number = 17694
    target = _target(project, pr_number)
    task = await _gate_task(
        db_session, project, pr_number, _blocked_loop(project, pr_number)
    )

    reused_id = uuid.uuid4()
    calls: list[str] = []

    async def _spawn(_task, kind, *_args, **_kwargs):
        calls.append(kind)
        return types.SimpleNamespace(id=reused_id), False

    mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)

    ticks = _EXACT_TARGET_REVIEW_SPAWN_BUDGET * 4
    for _ in range(ticks):
        assert await _drive_gate(task, db_session, pr_number, target) is False

    assert len(calls) == ticks, "every tick still reaches the idempotent spawn"
    context = task.context or {}
    assert context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None, (
        "one in-flight review must never be mistaken for a respawn runaway"
    )
    loop = context.get("review_loop") or {}
    assert not loop.get("authoritative_review_spawns")


@pytest.mark.asyncio
async def test_a_landed_fix_restores_the_full_respawn_budget(db_session, mocker):
    """The budget is scoped to the target, so real progress always clears it.

    This is what keeps the cap off the healthy path: a fix commit mints a new
    ``(base_sha, head_sha)`` and the count starts over. Only a loop in which
    nothing whatsoever is moving can reach the escalation.
    """
    project = Project(name="Budget resets", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pr_number = 17695
    target = _target(project, pr_number)
    task = await _gate_task(
        db_session, project, pr_number, _blocked_loop(project, pr_number)
    )

    spawned: list[str] = []

    async def _spawn(_task, kind, *_args, **_kwargs):
        spawned.append(kind)
        return types.SimpleNamespace(id=uuid.uuid4()), True

    mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)

    for _ in range(_EXACT_TARGET_REVIEW_SPAWN_BUDGET - 1):
        await _drive_gate(task, db_session, pr_number, target)
    assert len(spawned) == _EXACT_TARGET_REVIEW_SPAWN_BUDGET - 1

    # A fix lands: same PR, new head.
    moved = dict(target, head_sha="head-after-the-fix")
    for _ in range(_EXACT_TARGET_REVIEW_SPAWN_BUDGET):
        assert await _drive_gate(task, db_session, pr_number, moved) is False

    assert len(spawned) == (_EXACT_TARGET_REVIEW_SPAWN_BUDGET * 2) - 1, (
        "the new head gets its own full budget"
    )
    assert (task.context or {}).get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY) is None
