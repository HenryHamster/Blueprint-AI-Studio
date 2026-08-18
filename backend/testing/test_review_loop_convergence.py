"""Tests for review_loop_convergence cap-exhaustion preservation.

Covers FIX #3b (preserve mergeable work on cap exhaustion) and its extension to
back-merge conflict-resolver PRs: a task carrying
``context['backmerge_conflict'] == True`` is a ``create`` task (NOT a rollup),
but its PR re-applies expensive main→automain sync work, so a mergeable,
CI-green back-merge resolver PR must be PRESERVED on cap exhaustion exactly like
a rollup — left open + requires_approval for hand-merge, NOT auto-closed.

These import the handler directly from ``app.review_loop_convergence`` (rather
than the ``app.review_pipeline`` re-export) so the module owns its own tests.
"""

import copy
import uuid
from unittest.mock import AsyncMock

import pytest

from sqlalchemy import select

from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Alert, Project, Task, TaskStatus
from testing.factories import make_project, make_task


async def _make_db_project(db_session, **project_kwargs) -> Project:
    """Persist a project and return the refreshed instance."""
    project = make_project(**project_kwargs)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _authoritative_approval_loop(
    project_id: uuid.UUID,
    pr_number: int,
    *,
    head_sha: str | None = None,
    base_sha: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Build the durable exact-target proof used by cap classification."""
    head_sha = head_sha or f"head-{pr_number}"
    base_sha = base_sha or f"base-{pr_number}"
    review_history = history or [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "issues": [],
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
        }
    ]
    return {
        "pr_number": pr_number,
        # Kept to model the normal lifecycle handoff; the classifier must not
        # rely on this hint without the exact durable proof below.
        "review_approved": True,
        "review_history": review_history,
        "review_target": {
            "project_id": str(project_id),
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
        },
        "issue_ledger": {"open": [], "resolved": []},
    }


def _history_attempt(
    project_id: uuid.UUID,
    *,
    task_type: str,
    status: TaskStatus,
    iteration: int,
    original_task_id: uuid.UUID,
) -> Task:
    return make_task(
        project_id=project_id,
        status=status,
        payloads=[
            {
                "type": task_type,
                "pr_number": 17224,
                "original_task_id": str(original_task_id),
            }
        ],
        context={
            "review_loop": {
                "iteration": iteration,
                "original_task_id": str(original_task_id),
            }
        },
    )


def test_cross_episode_abandon_candidate_counts_cancelled_superseded_history():
    """The PR identity carries burden across episodes and cancelled tasks."""
    from app.review_loop_convergence import build_cross_episode_abandon_candidate

    project_id = uuid.uuid4()
    first_episode = uuid.uuid4()
    superseded_episode = uuid.uuid4()
    attempts = [
        _history_attempt(
            project_id,
            task_type="review" if index < 4 else "fix",
            status=TaskStatus.CANCELLED if index in (1, 5, 8) else TaskStatus.COMPLETED,
            iteration=index + 1 if index < 6 else (index - 5 if index < 8 else 4),
            original_task_id=first_episode if index < 6 else superseded_episode,
        )
        for index in range(10)
    ]

    candidate = build_cross_episode_abandon_candidate(attempts, threshold=10)

    assert candidate == {
        "status": "abandon_candidate",
        "threshold": 10,
        "attempts": 10,
        "cycles": 10,
        "burden": 10,
        "cancelled_attempts": 3,
        "episodes": 2,
    }


def test_cross_episode_abandon_candidate_does_not_flag_converging_pr():
    from app.review_loop_convergence import build_cross_episode_abandon_candidate

    project_id = uuid.uuid4()
    episode = uuid.uuid4()
    attempts = [
        _history_attempt(
            project_id,
            task_type="review" if index % 2 == 0 else "fix",
            status=TaskStatus.COMPLETED,
            iteration=index + 1,
            original_task_id=episode,
        )
        for index in range(4)
    ]

    assert build_cross_episode_abandon_candidate(attempts) is None


def test_cross_episode_abandon_ignores_stale_and_parallel_observation_rows():
    from app.review_loop_convergence import build_cross_episode_abandon_candidate

    project_id = uuid.uuid4()
    episode = uuid.uuid4()
    rows = []
    for index in range(20):
        task = _history_attempt(
            project_id,
            task_type="review",
            status=TaskStatus.COMPLETED,
            iteration=index + 1,
            original_task_id=episode,
        )
        task.context = {
            **(task.context or {}),
            **(
                {"_review_stale_target": True}
                if index % 2 == 0
                else {"review_was_parallelized": True}
            ),
        }
        rows.append(task)

    assert build_cross_episode_abandon_candidate(rows, threshold=1) is None


def test_cross_episode_threshold_matches_rollup_review_cap():
    """The PR-level census must not stop a rollup before its 16-cycle budget."""
    from app.review_loop_convergence import (
        CROSS_EPISODE_ABANDON_THRESHOLD,
        build_cross_episode_abandon_candidate,
    )
    from app.review_pipeline import MAX_REVIEW_FIX_ITERATIONS

    assert CROSS_EPISODE_ABANDON_THRESHOLD == MAX_REVIEW_FIX_ITERATIONS == 16

    project_id = uuid.uuid4()
    episode = uuid.uuid4()
    attempts = [
        _history_attempt(
            project_id,
            task_type="review" if index % 2 == 0 else "fix",
            status=TaskStatus.COMPLETED,
            iteration=index + 1,
            original_task_id=episode,
        )
        for index in range(15)
    ]
    assert build_cross_episode_abandon_candidate(attempts) is None


@pytest.mark.asyncio
async def test_spawn_stops_and_surfaces_cross_episode_abandon_candidate(db_session, mocker):
    """Archived exact generations still exhaust the PR-lifetime threshold."""
    from app.review_pipeline import (
        SPAWN_REASON_ABANDON_CANDIDATE,
        _spawn_pipeline_task,
    )

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_db_project(db_session)
    original_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"scope": "large"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    history = [
        _history_attempt(
            project.id,
            task_type="review" if index < 4 else "fix",
            status=TaskStatus.CANCELLED if index in (1, 5, 8) else TaskStatus.COMPLETED,
            iteration=index + 1,
            original_task_id=original_task.id,
        )
        for index in range(16)
    ]
    for task in history:
        task.archived = True
    db_session.add_all(history)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "fix",
                "pr_number": 17224,
                "branch": "blueprint/task-abandon",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "large",
            "review_loop": {
                "iteration": 10,
                "original_task_id": str(original_task.id),
            },
        },
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    result, created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=17224,
        branch="blueprint/task-abandon",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_reason="code_review",
        fix_prompt="resume the review-fix episode",
    )

    assert result is None
    assert created is False
    await db_session.refresh(original_task)
    await db_session.refresh(source_task)
    marker = (original_task.context or {}).get("review_loop_abandon_candidate")
    assert marker["attempts"] == 16
    assert marker["cancelled_attempts"] == 3
    assert marker["cycles"] == 16
    assert original_task.requires_approval is True
    assert (source_task.context or {}).get("_last_pipeline_spawn_reason") == (
        SPAWN_REASON_ABANDON_CANDIDATE
    )
    alert = (
        await db_session.execute(
            select(Alert).where(
                Alert.task_id == original_task.id,
                Alert.title.contains("abandon candidate"),
            )
        )
    ).scalar_one()
    assert "16 attempt(s)" in alert.message

    # The existing reset-iteration-cap contract is the explicit resume path.
    # Its timestamp starts a fresh cap/census window without deleting history.
    from app.time_utils import _utcnow

    reset_context = dict(original_task.context or {})
    reset_context.pop("review_loop_abandon_candidate", None)
    reset_context["review_loop"] = {"iteration_reset_at": _utcnow().isoformat()}
    original_task.context = reset_context
    db_session.add(original_task)
    await db_session.commit()
    resumed, resumed_created = await _spawn_pipeline_task(
        source_task,
        "fix",
        db_session,
        pr_number=17224,
        branch="blueprint/task-abandon",
        base_branch="main",
        original_task_id=str(original_task.id),
        fix_reason="code_review",
        fix_prompt="resume the review-fix episode",
    )
    assert resumed is not None
    assert resumed_created is True


def _mock_preservable_pr(mocker, *, changed_files: int, mergeable_state: str):
    """Mock GitHub side effects + get_pull_request for the preserve-work path.

    Patches at the source modules because ``_handle_cap_exhaustion`` /
    ``_pr_is_preservable_on_cap`` import these lazily inside the function body.
    Returns the ``close_pull_request`` mock so callers can assert the PR was
    (not) closed.
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "changed_files": changed_files,
            "mergeable_state": mergeable_state,
        },
    )
    return close_mock


def _mock_pr_lookup_fails(mocker):
    """Mock the preserve path so the GitHub PR lookup raises (unknown state)."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GitHub down"),
    )
    return close_mock


# ── Exact approval authority ────────────────────────────────────────────────


def test_cap_after_approve_requires_exact_durable_proof():
    from app.review_loop_convergence import _cap_exhausted_after_approve

    project_id = uuid.uuid4()
    loop = _authoritative_approval_loop(project_id, 701)
    orig = make_task(project_id=project_id, context={"review_loop": loop})
    source = make_task(project_id=project_id, context={})

    assert _cap_exhausted_after_approve(orig, source, 701) is True


@pytest.mark.parametrize(
    "legacy_context",
    [
        {"review_loop": {"review_approved": True}},
        {"review_loop": {"verdict": "APPROVE"}},
        {"blueprint_review_verdict": "APPROVE"},
        {
            "review_loop": {
                "review_approved": True,
                "review_history": [{"verdict": "APPROVE", "issues": []}],
            }
        },
    ],
)
def test_cap_after_approve_rejects_legacy_or_unproven_markers(legacy_context):
    from app.review_loop_convergence import _cap_exhausted_after_approve

    project_id = uuid.uuid4()
    orig = make_task(project_id=project_id, context=legacy_context)
    source = make_task(project_id=project_id, context={})

    assert _cap_exhausted_after_approve(orig, source, 702) is False


def test_cap_after_approve_rejects_same_head_open_blocker():
    from app.review_loop_convergence import _cap_exhausted_after_approve

    project_id = uuid.uuid4()
    head_sha = "same-head"
    base_sha = "same-base"
    history = [
        {
            "iteration": 1,
            "verdict": "REQUEST_CHANGES",
            "reviewed_head_sha": head_sha,
            "reviewed_base_sha": base_sha,
            "issues": [
                {
                    "severity": "MAJOR",
                    "description": "The merge gate can still fail open",
                    "file": "backend/app/gate.py",
                    "line": 20,
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
    loop = _authoritative_approval_loop(
        project_id,
        703,
        head_sha=head_sha,
        base_sha=base_sha,
        history=history,
    )
    # Exercise rebuilding from history, rather than relying on a precomputed
    # open ledger to reject the contradictory same-head approval.
    loop.pop("issue_ledger")
    orig = make_task(project_id=project_id, context={"review_loop": loop})
    source = make_task(project_id=project_id, context={})

    assert _cap_exhausted_after_approve(orig, source, 703) is False


def test_cap_after_approve_rejects_mismatched_pinned_target():
    from app.review_loop_convergence import _cap_exhausted_after_approve

    project_id = uuid.uuid4()
    loop = _authoritative_approval_loop(project_id, 704)
    loop["review_target"] = {**loop["review_target"], "head_sha": "newer-head"}
    orig = make_task(project_id=project_id, context={"review_loop": loop})
    source = make_task(project_id=project_id, context={})

    assert _cap_exhausted_after_approve(orig, source, 704) is False


# ── _pr_is_preservable_on_cap: back-merge resolver branch ────────────────────


@pytest.mark.asyncio
async def test_backmerge_resolver_mergeable_is_preservable(db_session, mocker):
    """A back-merge resolver PR (context.backmerge_conflict=True), non-empty +
    mergeable_state 'clean', is preservable even though it is NOT a rollup."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True, "backmerge_pr_number": 700},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_backmerge_resolver_dirty_is_not_preservable(db_session, mocker):
    """A back-merge resolver PR with a genuine merge conflict
    (mergeable_state 'dirty') cannot be hand-merged → NOT preservable."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="dirty")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_ci_red_unstable_is_not_preservable(
    db_session, mocker
):
    """A back-merge resolver PR that is mergeable but CI-red
    (mergeable_state 'unstable') is NOT preservable — it is not green."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="unstable")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_empty_is_not_preservable(db_session, mocker):
    """A confirmed-empty back-merge resolver PR (changed_files == 0) has nothing
    to hand-merge → NOT preservable, even though closing a resolver is normally
    the worse outcome."""
    _mock_preservable_pr(mocker, changed_files=0, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_unknown_state_preserves(db_session, mocker):
    """CONSERVATIVE: a back-merge resolver whose mergeable_state is 'unknown'
    (GitHub hasn't computed it) is PRESERVED — closing a resolver is the worse
    outcome since it can always be hand-merged. (A rollup would NOT preserve
    here.)"""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="unknown")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_backmerge_resolver_lookup_failure_preserves(db_session, mocker):
    """CONSERVATIVE: when the GitHub PR lookup FAILS for a back-merge resolver,
    PRESERVE (a rollup would fall back to close+FAIL)."""
    _mock_pr_lookup_fails(mocker)
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_rollup_lookup_failure_still_closes(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a rollup PR whose GitHub lookup
    FAILS is NOT preservable (falls back to the safe destructive path)."""
    _mock_pr_lookup_fails(mocker)
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is False


@pytest.mark.asyncio
async def test_rollup_unknown_state_still_closes(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a rollup PR with an 'unknown'
    mergeable_state is NOT preservable."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="unknown")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is False


@pytest.mark.asyncio
async def test_rollup_clean_state_is_preservable(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a non-empty, clean rollup PR is
    still preservable."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is True


@pytest.mark.asyncio
async def test_plain_create_task_is_not_preservable(db_session, mocker):
    """GUARDRAIL: a plain create task (no rollup markers, no backmerge_conflict)
    is NOT preservable even when its PR is non-empty + clean."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 900, db_session) is False


# ── _handle_cap_exhaustion: end-to-end preservation behavior ─────────────────


@pytest.mark.asyncio
async def test_cap_preserves_backmerge_resolver_pr(db_session, mocker):
    """A back-merge conflict-resolver PR (context.backmerge_conflict=True),
    non-empty + CI-green at cap exhaustion, is PRESERVED: left OPEN, flagged
    requires_approval, NO CODE_ERROR, NO pr_rejected, NO PR close — mirroring
    the rollup-preservation behavior."""
    close_mock = _mock_preservable_pr(mocker, changed_files=3, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.admin_approval_authority import APPROVAL_FOLLOWUP_CLAIM_KEY
    from app.closed_pr_reconciliation import REVIEW_WAIT_APPROVAL_HOLD_KEY
    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {"type": "create", "prompt": "resolve sync", "base_branch": "automain"}
        ],
        requires_approval=True,
        context={
            "backmerge_conflict": True,
            "backmerge_pr_number": 710,
            "_approval_set_at": "2026-08-17T01:00:00+00:00",
            "_approval_hold_episode": "old-operator-episode",
            APPROVAL_FOLLOWUP_CLAIM_KEY: {
                "token": "interrupted-old-operator-followup",
                "state": "pending",
                "status": TaskStatus.EVALUATING.value,
            },
            # Genuine non-convergence: latest round has outstanding changes.
            "review_loop": {
                "pr_number": 710,
                REVIEW_WAIT_APPROVAL_HOLD_KEY: {
                    "owner": "ci_review_wait_backstop",
                    "project_id": str(project.id),
                    "pr_number": 710,
                    "head_sha": "old-head",
                    "base_sha": "old-base",
                    "set_at": "2026-08-17T01:00:00+00:00",
                },
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 710, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 710}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 710, 16, db_session,
        scope="per-chain", verified_disposition="reject"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") == (
        "review_cap_preserved_for_handmerge"
    ), "Preservable back-merge resolver PR must be tagged for hand-merge"
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert orig.requires_approval is True, "Preserved PR must be flagged for operator"
    refreshed_context = dict(orig.context or {})
    refreshed_loop = dict(refreshed_context.get("review_loop") or {})
    assert APPROVAL_FOLLOWUP_CLAIM_KEY not in refreshed_context
    assert REVIEW_WAIT_APPROVAL_HOLD_KEY not in refreshed_loop
    assert refreshed_context["_approval_set_at"] != "2026-08-17T01:00:00+00:00"
    assert refreshed_context["_approval_hold_episode"] != "old-operator-episode"
    assert (
        refreshed_context["approval_hold_reason"]
        == "review_cap_preserved_for_handmerge"
    )
    assert (orig.context or {}).get("failure_category") != "CODE_ERROR", (
        "Preserving salvageable back-merge work must NOT record CODE_ERROR"
    )
    assert (orig.context or {}).get("pr_rejected") is not True
    close_mock.assert_not_called()  # PR LEFT OPEN
    rec_task.assert_not_called()
    rec_dir.assert_not_called()
    alert_mock.assert_awaited()  # operator still alerted


@pytest.mark.asyncio
async def test_cap_closes_nonrollup_nonbackmerge_pr(db_session, mocker):
    """GUARDRAIL: a plain create task (no rollup markers, no backmerge_conflict)
    at cap exhaustion still takes the DESTRUCTIVE path — FAILED + CODE_ERROR +
    pr_rejected + PR closed — even when its PR is non-empty + clean."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            # No rollup markers, no backmerge_conflict.
            "review_loop": {
                "pr_number": 720,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 720, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 720}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 720, 16, db_session,
        scope="per-chain", verified_disposition="reject"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    assert (orig.context or {}).get("failure_category") == "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is True
    close_mock.assert_awaited()  # destructive path closes the PR


@pytest.mark.asyncio
async def test_cap_preserves_rollup_pr_unchanged(db_session, mocker):
    """GUARDRAIL: existing rollup preservation still works — a non-empty, clean
    rollup PR at cap exhaustion is left OPEN + requires_approval, no CODE_ERROR,
    no close."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 730,
            "review_loop": {
                "pr_number": 730,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 730, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 730}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 730, 16, db_session,
        scope="per-chain", verified_disposition="reject"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") == (
        "review_cap_preserved_for_handmerge"
    )
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert orig.requires_approval is True
    assert (orig.context or {}).get("failure_category") != "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is not True
    close_mock.assert_not_called()
    rec_task.assert_not_called()
    rec_dir.assert_not_called()
    alert_mock.assert_awaited()


@pytest.mark.asyncio
async def test_supersession_churn_hold_is_clearable(db_session):
    """The churn-halt breadcrumb must remain a clearable escalation hold."""
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    task = make_task(
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "review_loop_terminal_outcome": {
                "kind": "supersession_churn_halt",
                "reason": "supersession-churn",
                "pr_number": 9179,
                "iterations": 3,
                "detail": "bound hit",
                "recorded_at": "2026-06-09T23:29:15",
            },
            "_approval_set_at": "2026-06-09T23:29:15.375394",
        },
        payloads=[{"type": "research", "prompt": "rollup parent"}],
    )

    assert clear_review_escalation_approval_gate(
        task, resolved_by="review_approve_pr_9179"
    )
    assert task.requires_approval is False
    outcome = task.context["review_loop_terminal_outcome"]
    assert outcome["kind"] == "supersession_churn_halt"
    assert outcome["resolved"] is True
    assert outcome["resolved_by"] == "review_approve_pr_9179"


@pytest.mark.asyncio
async def test_clear_escalation_gate_preserves_newer_independent_hold(db_session):
    """A stale escalation breadcrumb cannot consume a later approval episode."""

    if resolve_dialect_name(db_session) != "postgresql":
        pytest.skip("independent-session approval race requires PostgreSQL")

    from app.admin_approval_authority import establish_independent_approval_hold
    from app.review_loop_convergence import (
        _set_review_loop_terminal_outcome,
        clear_review_escalation_approval_gate,
    )

    project = await _make_db_project(db_session)
    task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        context={},
        payloads=[{"type": "research", "prompt": "approval episode race"}],
    )
    establish_independent_approval_hold(
        task,
        reason="stuck",
        owner="review_pipeline",
    )
    _set_review_loop_terminal_outcome(
        task,
        kind="stuck",
        reason="stuck",
        pr_number=9179,
        iterations=4,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    async with AsyncSessionLocal() as winner:
        fresh = (
            (
                await winner.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .one()
        )
        establish_independent_approval_hold(
            fresh,
            reason="manual branch-safety hold",
            owner="operator",
        )
        fresh.latest_status_message = "Operator owns this hold"
        winner.add(fresh)
        await winner.commit()

    async with AsyncSessionLocal() as closer:
        fresh = (
            (
                await closer.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .one()
        )
        before_context = copy.deepcopy(fresh.context)
        assert not clear_review_escalation_approval_gate(
            fresh,
            resolved_by="review_approve_pr_9179",
        )
        assert fresh.requires_approval is True
        assert fresh.latest_status_message == "Operator owns this hold"
        assert fresh.context == before_context


@pytest.mark.asyncio
async def test_clear_matching_escalation_gate_removes_exact_hold_metadata(db_session):
    """A matching escalation episode is fully consumed, not left inheritable."""

    if resolve_dialect_name(db_session) != "postgresql":
        pytest.skip("approval provenance assertion requires PostgreSQL")

    from app.admin_approval_authority import (
        APPROVAL_FOLLOWUP_CLAIM_KEY,
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        establish_independent_approval_hold,
    )
    from app.review_loop_convergence import (
        _set_review_loop_terminal_outcome,
        clear_review_escalation_approval_gate,
    )

    project = await _make_db_project(db_session)
    task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        context={},
        payloads=[{"type": "research", "prompt": "matching escalation"}],
    )
    establish_independent_approval_hold(
        task,
        reason="repeat_fingerprint_halt",
        owner="review_pipeline",
    )
    _set_review_loop_terminal_outcome(
        task,
        kind="repeat_fingerprint_halt",
        reason="repeat-fingerprint",
        pr_number=9180,
        iterations=2,
    )
    task.context = {
        **(task.context or {}),
        APPROVAL_FOLLOWUP_CLAIM_KEY: {"token": "obsolete-claim"},
    }
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert clear_review_escalation_approval_gate(
        task,
        resolved_by="review_approve_pr_9180",
    )
    assert task.requires_approval is False
    assert "approval_hold_reason" not in task.context
    assert "_approval_set_at" not in task.context
    assert APPROVAL_HOLD_EPISODE_KEY not in task.context
    assert APPROVAL_HOLD_PROVENANCE_KEY not in task.context
    assert APPROVAL_FOLLOWUP_CLAIM_KEY not in task.context


def test_terminal_breadcrumb_never_adopts_a_preexisting_independent_hold():
    """Gates-off escalation bookkeeping cannot claim an unrelated TRUE hold."""

    from app.admin_approval_authority import establish_independent_approval_hold
    from app.review_loop_convergence import (
        _set_review_loop_terminal_outcome,
        clear_review_escalation_approval_gate,
    )

    task = make_task(
        status=TaskStatus.EVALUATING,
        context={},
        payloads=[{"type": "research", "prompt": "independent policy hold"}],
    )
    establish_independent_approval_hold(
        task,
        reason="manual security hold",
        owner="operator",
    )
    before_context = copy.deepcopy(task.context)
    _set_review_loop_terminal_outcome(
        task,
        kind="stuck",
        reason="stuck",
        pr_number=9181,
        iterations=4,
    )

    assert "approval_hold_episode" not in task.context[
        "review_loop_terminal_outcome"
    ]
    assert not clear_review_escalation_approval_gate(
        task,
        resolved_by="review_approve_pr_9181",
    )
    assert task.requires_approval is True
    for key in (
        "approval_hold_reason",
        "_approval_set_at",
        "_approval_hold_episode",
        "_approval_hold_provenance",
    ):
        assert task.context[key] == before_context[key]


@pytest.mark.asyncio
async def test_is_backmerge_resolver_helper(db_session):
    """The _is_backmerge_resolver predicate is True only for
    context.backmerge_conflict == True."""
    from app.review_loop_convergence import _is_backmerge_resolver

    yes = make_task(project_id=uuid.uuid4(), context={"backmerge_conflict": True})
    no_flag = make_task(project_id=uuid.uuid4(), context={"backmerge_conflict": False})
    no_key = make_task(project_id=uuid.uuid4(), context={})
    none_ctx = make_task(project_id=uuid.uuid4())
    none_ctx.context = None

    assert _is_backmerge_resolver(yes) is True
    assert _is_backmerge_resolver(no_flag) is False
    assert _is_backmerge_resolver(no_key) is False
    assert _is_backmerge_resolver(none_ctx) is False


# ── after_approve=True: dirty rollups are preservable ────────────────────────


@pytest.mark.asyncio
async def test_rollup_dirty_preservable_after_approve(db_session, mocker):
    """SECONDARY FIX: a conflicted (dirty) rollup PR is preservable when called
    with after_approve=True — the conflict is mechanical, not a rejection, and a
    human can resolve it for hand-merge."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="dirty")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    # Without after_approve: dirty rollup is NOT preservable (current behavior).
    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is False
    # With after_approve=True: dirty rollup IS preservable (secondary fix).
    assert await _pr_is_preservable_on_cap(orig, 800, db_session, after_approve=True) is True


@pytest.mark.asyncio
async def test_rollup_unknown_state_not_preservable_even_after_approve(db_session, mocker):
    """GUARDRAIL: a rollup with 'unknown' mergeable_state is NOT preservable
    even with after_approve=True — unknown state means GitHub hasn't computed it
    yet, and we don't know if the PR is conflicted or not."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="unknown")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session, after_approve=True) is False


@pytest.mark.asyncio
async def test_approved_dirty_rollup_routed_to_preserve_not_close(db_session, mocker):
    """End-to-end: an APPROVED rollup PR that is conflicted (dirty) at cap
    exhaustion is PRESERVED (PR left open, requires_approval=True) rather than
    auto-closed. This is the secondary-fix safety net for the requeue-clone
    branch-desync incident."""
    close_mock = _mock_preservable_pr(mocker, changed_files=5, mergeable_state="dirty")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 102,
            "review_loop": _authoritative_approval_loop(project.id, 102),
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 102, "original_task_id": str(orig.id)}],
        context={"review_loop": _authoritative_approval_loop(project.id, 102)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task,
        str(orig.id),
        102,
        16,
        db_session,
        scope="global",
        verified_disposition="reject",
    )

    await db_session.refresh(orig)
    # PR must NOT have been closed — the work is preserved for hand-merge.
    close_mock.assert_not_called()
    # Task flagged for operator hand-merge.
    assert orig.requires_approval is True


# ── K8 lock-window regressions (2026-07-12 concurrency audit) ────────────────
#
# ``_handle_cap_exhaustion`` is only ever reached from inside
# ``_spawn_pipeline_task``, which holds ``pg_advisory_xact_lock(pr_number)``
# (review_pipeline.py ~:1764) until its transaction ends. Every commit between
# that acquire and the cap handler sits on a branch that RETURNS, so the lock
# was still held across the preservability GitHub GET.
#
# ``pg_advisory_xact_lock`` is TRANSACTION-scoped: it is released by transaction
# end, not by the absence of a mutation. So "the advisory lock is released
# before the GET" is exactly the assertion "there is no open transaction on the
# session when the GET fires" — which is also the assertion that the connection
# is not left idle-in-transaction across the HTTP call (racing the 60s
# idle_in_transaction_session_timeout in database.py:405-418). That invariant is
# dialect-independent, so it holds on the SQLite harness too.


def _mock_preservable_pr_asserting_unlocked(mocker, db_session, seen: list):
    """Like ``_mock_preservable_pr`` but asserts NO open txn at GET time."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    async def _get_pr(*_a, **_kw):
        # THE regression assertion: the per-PR advisory lock is xact-scoped, so
        # an open transaction here means it is still held across this HTTP call.
        seen.append(db_session.in_transaction())
        return {"state": "open", "changed_files": 5, "mergeable_state": "clean"}

    mocker.patch("app.github.get_pull_request", new_callable=AsyncMock, side_effect=_get_pr)
    return close_mock


def _mock_cap_side_effects(mocker):
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("after_approve", [False, True])
async def test_spawn_lock_released_before_preservability_get(
    db_session, mocker, after_approve
):
    """The PR spawn lock (and any open txn) is released BEFORE the preservability
    GitHub GET — on BOTH cap-exhaustion call sites.

    The two sites are the ``after_approve=True`` path (review_loop_convergence
    ~:583) and the ordinary REQUEST_CHANGES path (~:791). The original report
    named only one; fixing one leaves half the defect, so both are exercised
    here via the ``after_approve`` parametrization.
    """
    seen_in_txn: list = []
    _mock_preservable_pr_asserting_unlocked(mocker, db_session, seen_in_txn)
    _mock_cap_side_effects(mocker)

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    review_loop = (
        _authoritative_approval_loop(project.id, 140)
        if after_approve
        else {"pr_number": 140}
    )
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 140,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 140, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 140, 16, db_session,
        scope="global", verified_disposition="reject"
    )

    assert seen_in_txn, "preservability GET never fired — test setup is wrong"
    assert seen_in_txn == [False], (
        "the preservability GitHub GET ran with an open transaction: the per-PR "
        "pg_advisory_xact_lock is transaction-scoped, so it was STILL HELD across "
        "an unbounded HTTP call (and the connection was idle-in-transaction, "
        "racing the 60s idle_in_transaction_session_timeout)"
    )
    # And the terminal transition still lands (the fix must not lose the outcome).
    await db_session.refresh(orig)
    assert orig.requires_approval is True
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True


def test_preservability_deadline_exceeds_the_github_client_retry_budget():
    """The deadline must sit ABOVE ``_github_request``'s own worst-case backoff.

    BLOCKER 2 (review round 2). The first version of this guard used 45s. But the
    GitHub client does its own rate-limit backoff — max_retries=3, a 15s httpx
    timeout, and an UNGUARDED ``await asyncio.sleep(min(wait, 120))`` proactive
    reset-wait per attempt — for a worst case of 3*(15+120) + 2*5 = 415s, which is
    exactly why this call has been measured at ~285s in the wild.

    A 45s deadline therefore does not bound a hang; it AMPUTATES the client's own
    rate-limit retry and converts a routine, recoverable 429 into a timeout — in
    bulk, during precisely the GitHub rate-limit / token-exhaustion windows this
    campaign exists to prevent. Pin the ordering so nobody "tightens" it back.
    """
    from app.review_loop_convergence import _PRESERVABILITY_GET_TIMEOUT_S

    github_client_worst_case_s = 3 * (15 + 120) + 2 * 5  # = 415
    assert github_client_worst_case_s < _PRESERVABILITY_GET_TIMEOUT_S, (
        f"the preservability deadline ({_PRESERVABILITY_GET_TIMEOUT_S}s) is INSIDE "
        f"the GitHub client's own retry budget ({github_client_worst_case_s}s) — it "
        f"would abort recoverable rate-limit backoff instead of bounding a hang"
    )


@pytest.mark.asyncio
async def test_preservability_timeout_raises_unavailable_not_not_preservable(
    db_session, mocker
):
    """A DEADLINE breach must NOT be reported as 'not preservable'.

    BLOCKER 2. The original code routed the ``wait_for`` timeout into the shared
    ``except Exception`` branch, which for a rollup returns ``is_backmerge`` →
    False → not preservable → **close the PR + FAIL + CODE_ERROR**. A timeout is a
    statement about US, not about the PR: we simply stopped waiting. Treating it
    as evidence the PR is unsalvageable destroys signed-off work.

    A genuine network failure keeps its old fail-safe semantics — asserted in the
    companion test below so this fix cannot silently swallow real errors too.
    """
    import asyncio

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch("app.review_loop_convergence._PRESERVABILITY_GET_TIMEOUT_S", 0.01)

    async def _hang(*_a, **_kw):
        await asyncio.Event().wait()

    mocker.patch("app.github.get_pull_request", new_callable=AsyncMock, side_effect=_hang)

    from app.review_loop_convergence import (
        _PreservabilityUnavailable,
        _pr_is_preservable_on_cap,
    )

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 141},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    with pytest.raises(_PreservabilityUnavailable):
        await _pr_is_preservable_on_cap(orig, 141, db_session)


@pytest.mark.asyncio
async def test_preservability_network_failure_still_takes_the_failsafe_branch(
    db_session, mocker
):
    """A real lookup FAILURE (not a timeout) keeps its pre-existing semantics.

    Guards the Blocker-2 fix against over-reach: only the deadline branch became
    non-destructive. A connection error is still an answer of a kind (the client
    exhausted its retries), so a rollup still falls back to the safe destructive
    path and a back-merge resolver still prefers preserve-over-destroy.
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=ConnectionError("connection reset"),
    )

    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 142},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 142, db_session) is False


@pytest.mark.asyncio
async def test_preservability_timeout_does_not_close_the_pr_or_fail_the_task(
    db_session, mocker
):
    """END-TO-END BLOCKER 2: a preservability TIMEOUT must take NO terminal action.

    The destructive path this replaces: timeout → ``except Exception`` → rollup →
    not preservable → ``close_pull_request`` + FAILED + CODE_ERROR. Under a GitHub
    rate-limit window that fires for EVERY cap-exhausted rollup at once, closing
    them all.

    Post-fix the whole terminal transition DEFERS: the PR stays open, the orig
    stays non-terminal, and the (idempotent) cap path retries on the next sweep
    once GitHub is healthy.
    """
    import asyncio

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    comment_mock = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    _mock_cap_side_effects(mocker)
    mocker.patch("app.review_loop_convergence._PRESERVABILITY_GET_TIMEOUT_S", 0.01)

    async def _hang(*_a, **_kw):
        await asyncio.Event().wait()

    mocker.patch("app.github.get_pull_request", new_callable=AsyncMock, side_effect=_hang)

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    review_loop = {"pr_number": 143, "review_history": [{"verdict": "APPROVE", "issues": []}]}
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 143,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 143, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    # Must NOT raise: the defer is handled inside _handle_cap_exhaustion.
    await _handle_cap_exhaustion(
        source_task, str(orig.id), 143, 16, db_session,
        scope="global", verified_disposition="reject"
    )

    close_mock.assert_not_called()
    comment_mock.assert_not_called()
    await db_session.refresh(orig)
    assert orig.status == TaskStatus.EVALUATING, (
        "a preservability TIMEOUT terminalized the create task — a deadline breach "
        "must never be treated as evidence the PR is unsalvageable"
    )
    assert (orig.context or {}).get("failure_detail") is None
    assert orig.requires_approval is not True


def _empty_for_update_result():
    """A result object that yields no rows — simulates ``skip_locked`` losing."""

    class _Empty:
        def scalars(self):
            return self

        def first(self):
            return None

    return _Empty()


@pytest.mark.asyncio
async def test_cap_exhaustion_peer_locked_orig_does_not_expire_caller_source_task(
    db_session, mocker, real_orm_expiry
):
    """BLOCKER 1: the peer-locked defer must NOT expire the caller's source_task.

    ``_handle_cap_exhaustion`` re-selects ``orig`` FOR UPDATE SKIP LOCKED for the
    terminal transition. When a PEER holds that row — i.e. exactly under the
    concurrency this whole change targets — the re-select yields nothing and the
    handler defers.

    That defer used to ``await session.rollback()``. A rollback expires EVERY ORM
    instance in the session (even under ``expire_on_commit=False``), including the
    CALLER's ``source_task``. All three call sites (review_pipeline.py ~:2901,
    ~:3068, ~:3096) then immediately run
    ``_set_spawn_reason(source_task, SPAWN_REASON_CAP)``, whose first statement is
    ``dict(task.context or {})`` — an expired-attribute read that lazy-loads
    outside greenlet context and raises
    ``sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called`` at
    review_pipeline.py:751, propagating out of ``_spawn_pipeline_task`` into the
    agent-status post-commit chain as a 500 with a poisoned session. That is the
    IDENTICAL failure mode this change fixes at the acquire side.

    Nothing is pending on that branch, so the fix is ``commit()`` — it ends the
    read transaction and releases the row lock exactly as a rollback would, but
    does not expire.

    Uses ``real_orm_expiry``: the root conftest no-ops ``InstanceState._expire``
    SUITE-WIDE, so without this fixture rollback-expiry cannot happen in a test
    and this regression test would pass VACUOUSLY against the broken code.
    """
    from sqlalchemy import inspect as sa_inspect

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "changed_files": 5, "mergeable_state": "clean"},
    )
    _mock_cap_side_effects(mocker)

    from app.review_loop_convergence import _handle_cap_exhaustion
    from app.review_pipeline import (
        SPAWN_REASON_CAP,
        SPAWN_REASON_KEY,
        _set_spawn_reason,
    )

    project = await _make_db_project(db_session)
    review_loop = {"pr_number": 144, "review_history": [{"verdict": "APPROVE", "issues": []}]}
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 144,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 144, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    # ── Self-check that real_orm_expiry actually restored genuine expiry. ──
    # Without this the whole test is vacuous: the suite-wide no-op would make the
    # pre-fix rollback harmless here while it 500s in production.
    #
    # (Snapshot the id first — the rollback below expires EVERY instance, so even
    # reading ``orig.id`` afterwards would lazy-load. That is the very hazard under
    # test, and it applies to the test body too.)
    orig_id = str(orig.id)
    await db_session.refresh(source_task)  # ensure an open txn to roll back
    await db_session.rollback()
    assert sa_inspect(source_task).expired, (
        "real_orm_expiry did not restore InstanceState._expire — this "
        "rollback-expiry regression test would be vacuous"
    )
    # Re-load BOTH: the rollback expired orig as well as source_task.
    await db_session.refresh(source_task)
    await db_session.refresh(orig)

    # ── Simulate a PEER holding the orig row: SKIP LOCKED yields nothing. ──
    # We cannot take a genuine competing row lock from this single test session,
    # so intercept the for-update SELECT and hand back an EMPTY result — precisely
    # what Postgres returns when a peer holds the row.
    #
    # CRITICAL: still RUN the statement. Short-circuiting it before it reaches the
    # DB means the session never autobegins a transaction — and
    # ``session.rollback()`` with no active transaction is a NO-OP that expires
    # NOTHING, so the pre-fix bug would not reproduce and this test would pass
    # VACUOUSLY against the broken code (verified: it did). Executing for real
    # reproduces production's session state (an open txn at the defer), which is
    # what makes the rollback expire the caller's ``source_task``.
    _real_execute = db_session.execute

    async def _execute(stmt, *args, **kwargs):
        result = await _real_execute(stmt, *args, **kwargs)
        if getattr(stmt, "_for_update_arg", None) is not None:
            return _empty_for_update_result()
        return result

    mocker.patch.object(db_session, "execute", side_effect=_execute)

    await _handle_cap_exhaustion(
        source_task, orig_id, 144, 16, db_session,
        scope="global", verified_disposition="reject"
    )

    # THE regression assertion. Pre-fix this raises
    # sqlalchemy.exc.MissingGreenlet from inside _set_spawn_reason's
    # ``dict(task.context or {})`` — the caller's source_task was expired by the
    # handler's rollback.
    assert not sa_inspect(source_task).expired, (
        "_handle_cap_exhaustion EXPIRED the caller's source_task on the "
        "peer-locked defer branch — the next _set_spawn_reason(source_task, ...) "
        "will lazy-load outside greenlet context and raise MissingGreenlet"
    )
    _set_spawn_reason(source_task, SPAWN_REASON_CAP)
    assert (source_task.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_CAP

    # And the defer really did defer: no terminal transition was applied.
    assert orig.status == TaskStatus.EVALUATING


def test_shared_runner_vmcontact_does_not_mask_a_dead_reviewer():
    """MAJOR 5: the runner's own active-work report DEFEATS the VmContact override.

    R3 gave ``_is_pipeline_task_stale`` a ``VmContact.last_contact_at`` override so
    a lock-starved-but-LIVE agent is not superseded. Copied naively, that re-opens
    the shared-VmContact zombie mask #14413 closed in the reaper:
    ``orchestrator_runner_dispatch.py:626`` sets
    ``task.vm_instance_id = f"runner:{runner.id}"`` — SHARED by every task on that
    local runner — and ``VmContact`` is keyed on ``vm_instance_id``. So a hung or
    dead reviewer on a BUSY runner has a permanently-fresh ``last_contact_at``
    from its SIBLINGS and would never be superseded.

    ``reaper.py`` guards this with ``_runner_report_excludes_task`` 47 lines below
    its VmContact override; this predicate now does too. The runner self-reports
    its ground-truth ``active_task_ids`` each heartbeat — a fresh report that OMITS
    the task is proof its agent process is gone.
    """
    from datetime import timedelta

    from app.review_loop_convergence import (
        PIPELINE_TASK_STALE_MINUTES,
        _is_pipeline_task_stale,
    )
    from app.time_utils import _utcnow

    now = _utcnow()
    stale_hb = now - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 5)
    runner_vm = f"runner:{uuid.uuid4()}"

    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review", "pr_number": 900}],
        vm_instance_id=runner_vm,
    )
    task.id = uuid.uuid4()
    task.last_heartbeat = stale_hb
    task.started_at = stale_hb

    # A live SIBLING on the same runner keeps the SHARED VmContact row fresh.
    fresh_shared_contact = now - timedelta(seconds=30)

    # 1) Without the runner report, the shared contact masks the death — the
    #    reviewer looks alive forever. (This is the R3-only behaviour.)
    assert (
        _is_pipeline_task_stale(task, vm_last_contact=fresh_shared_contact) is False
    )

    # 2) The runner's OWN fresh report omits this task => its agent is gone. The
    #    shared-contact override must NOT save it.
    report_excluding_us = {
        runner_vm: (now - timedelta(seconds=30), frozenset({str(uuid.uuid4())}))
    }
    assert (
        _is_pipeline_task_stale(
            task,
            vm_last_contact=fresh_shared_contact,
            runner_report_lookup=report_excluding_us,
        )
        is True
    ), (
        "a dead reviewer on a busy local runner was masked by a SIBLING's "
        "VmContact row — the shared-VmContact zombie gap (#14413) is re-opened in "
        "the review pipeline"
    )

    # 3) A report that still LISTS the task means it is genuinely running: the
    #    override stands and the live reviewer is not superseded.
    report_including_us = {
        runner_vm: (now - timedelta(seconds=30), frozenset({str(task.id)}))
    }
    assert (
        _is_pipeline_task_stale(
            task,
            vm_last_contact=fresh_shared_contact,
            runner_report_lookup=report_including_us,
        )
        is False
    )


@pytest.mark.asyncio
async def test_preservability_rate_limit_does_not_close_the_pr_or_fail_the_task(
    db_session, mocker
):
    """MAJOR 1 (round 3): a RATE-LIMITED lookup must DEFER, not close the PR.

    This is the PR's headline threat, and the 420s deadline does NOT cover it —
    because during a rate-limit window NOTHING HANGS. ``_github_request``
    FAST-FAILS a hard 429/403 (github.py ~:536-540: a reset more than
    RATE_LIMIT_FAST_FAIL_THRESHOLD_S=5s out returns at attempt 0) and
    short-circuits a known-empty bucket to a SYNTHETIC 403 with ZERO network
    (~:471-486). So ``get_pull_request`` returns in ~0-15s.

    Pre-fix that 403 became an HTTPException(500) which landed in the generic
    lookup-failure branch -> ``return is_backmerge`` -> False for a rollup -> NOT
    preservable -> **close_pull_request + FAILED + CODE_ERROR**. During an
    install-token-budget exhaustion window (which has taken production down twice)
    that fires for EVERY cap-exhausted rollup at once, closing all their PRs.

    The fix is CLASSIFICATION, not a deadline: a rate limit is a statement about
    our QUOTA, never evidence the PR is unsalvageable.
    """
    from fastapi import HTTPException

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    comment_mock = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    _mock_cap_side_effects(mocker)

    # Exactly what github_pulls.get_pull_request now raises for the client's
    # synthetic 403 (X-RateLimit-Remaining: 0, zero network).
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(
            status_code=403,
            detail="GitHub rate limit exhausted fetching PR #145 (reset at 999)",
        ),
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    review_loop = {"pr_number": 145, "review_history": [{"verdict": "APPROVE", "issues": []}]}
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 145,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 145, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 145, 16, db_session,
        scope="global", verified_disposition="reject"
    )

    close_mock.assert_not_called()
    comment_mock.assert_not_called()
    await db_session.refresh(orig)
    assert orig.status == TaskStatus.EVALUATING, (
        "a GitHub RATE LIMIT terminalized the create task and closed its rollup PR "
        "— during a token-exhaustion window this destroys every cap-exhausted "
        "rollup at once, which is the exact incident this change exists to prevent"
    )
    assert (orig.context or {}).get("failure_detail") is None


@pytest.mark.asyncio
async def test_preservability_permission_403_still_takes_the_failsafe_branch(
    db_session, mocker
):
    """A PERMISSION 403 is a real answer and keeps the pre-existing semantics.

    Guards the MAJOR 1 fix against over-reach: only a RATE-LIMIT 403/429 defers.
    A permission 403 carries no rate-limit headers, so ``github_pulls`` raises it
    as a 500 and it still falls back to the safe destructive path for a rollup.
    """
    from fastapi import HTTPException

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=500, detail="Failed to get PR #146"),
    )

    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 146},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 146, db_session) is False


@pytest.mark.asyncio
async def test_gone_pr_404_still_takes_the_failsafe_branch(db_session, mocker):
    """A 404/410 (PR genuinely gone) is a real answer — must NOT defer forever."""
    from fastapi import HTTPException

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=404, detail="PR #147 not found"),
    )

    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 147},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 147, db_session) is False


@pytest.mark.asyncio
async def test_token_config_403_is_terminal_not_deferred_forever(db_session, mocker):
    """BLOCKER 1 (round 4): a PERMANENT config failure must NOT be classed transient.

    ``get_github_token_for_task`` -> ``github_token_resolver`` raises
    ``HTTPException(403, "Project does not have an authorized GitHub repository")``
    for a project whose github_owner/github_repo was cleared or de-authorized. That
    is a CONFIG failure — GitHub is never even reached — not a quota statement.

    An earlier draft wrapped the token resolve and the GET in ONE try, so that 403
    matched the rate-limit classifier and became ``_PreservabilityUnavailable`` ->
    DEFER. On a condition that never clears, that is a PERMANENT WEDGE: every
    re-entry re-raises and re-defers, and the task never terminalizes — the exact
    deadline-less-park class this change exists to remove, re-created by its own
    fix.

    The token resolve now sits in its own narrow try and takes the fail-safe
    (terminal) branch. The GET is the ONLY thing rate-limit classified.
    """
    from fastapi import HTTPException

    from app.review_loop_convergence import (
        _PreservabilityUnavailable,
        _pr_is_preservable_on_cap,
    )

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        side_effect=HTTPException(
            status_code=403,
            detail="Project does not have an authorized GitHub repository",
        ),
    )
    get_pr = mocker.patch("app.github.get_pull_request", new_callable=AsyncMock)

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 148},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    try:
        result = await _pr_is_preservable_on_cap(orig, 148, db_session)
    except _PreservabilityUnavailable:  # pragma: no cover - the regression
        raise AssertionError(
            "a PERMANENT GitHub-config 403 (no authorized repo) was classified as a "
            "TRANSIENT rate limit — the cap transition now DEFERS FOREVER on a "
            "condition that never clears (permanent wedge)"
        )

    assert result is False, (
        "a token/config failure must take the fail-safe branch and terminalize"
    )
    get_pr.assert_not_called()


@pytest.mark.asyncio
async def test_cap_defer_is_counted_and_alerts_after_threshold(db_session, mocker):
    """A DEFER is a PARK — count it and escalate, never park silently forever.

    Re-entry into the cap handler is EVENT-DRIVEN (``_spawn_pipeline_task`` is
    driven by CI / review-verdict completions), NOT timer-swept, so a transient
    preservability failure does NOT self-heal on a quiet PR. Without a counter this
    is an unbounded, unalerted park — the deadline-less-park class again, just
    non-destructive this time. Consecutive defers are counted on ``orig`` and an
    operator alert fires past the threshold; the transition still stays deferred
    (alerting is the escalation, NOT closing the PR).
    """
    from fastapi import HTTPException

    from app.review_loop_convergence import (
        _CAP_DEFER_ALERT_AFTER,
        _CAP_DEFER_COUNT_KEY,
        _handle_cap_exhaustion,
    )

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )
    # Rate limit, as github_pulls now surfaces it (canonical 503 + Retry-After).
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(
            status_code=503, detail="GitHub rate limit exhausted fetching PR #149"
        ),
    )

    project = await _make_db_project(db_session)
    review_loop = {"pr_number": 149, "review_history": [{"verdict": "APPROVE", "issues": []}]}
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 149,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 149, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    for expected in range(1, _CAP_DEFER_ALERT_AFTER + 1):
        await _handle_cap_exhaustion(
            source_task, str(orig.id), 149, 16, db_session,
            scope="global", verified_disposition="reject"
        )
        await db_session.refresh(orig)
        assert (orig.context or {}).get(_CAP_DEFER_COUNT_KEY) == expected, (
            "the defer was not counted — an unbounded, unalerted park is a silent "
            "wedge, because re-entry is event-driven and never self-heals"
        )
        assert orig.status == TaskStatus.EVALUATING

    # Escalated, but STILL non-destructive: alerting must not become closing.
    alert_mock.assert_awaited()
    close_mock.assert_not_called()
    assert (orig.context or {}).get("failure_detail") is None


@pytest.mark.asyncio
async def test_defer_counter_does_not_erase_a_peer_terminal_write(db_session, mocker):
    """BLOCKER (round 5): the defer-path counter RMW must not blind-overwrite a peer.

    K8 deliberately COMMITS at the top of ``_handle_cap_exhaustion`` to release the
    per-PR advisory lock, so two cap handlers CAN legitimately overlap on the same
    ``orig``. The terminal path re-establishes exclusion with a FOR UPDATE re-lock —
    but the DEFER path returns BEFORE reaching it, and still wrote ``orig.context``.

    ``orig`` at that point is a STALE snapshot (loaded before a GitHub GET spanning
    0-15s on a rate limit, up to 420s on a timeout), and ``expire_on_commit=False``
    means the commit inside ``_pr_is_preservable_on_cap`` did not refresh it. So a
    full-blob ``orig.context = dict(stale)`` ERASES whatever a peer committed in the
    window — the exact R3 defect this change fixes at the supersede sites.

    Concretely: a peer lands the terminal cap transition during our GET. Its
    ``status`` column survives (status isn't dirty in OUR session) but its
    ``failure_detail`` is wiped out of the context blob — and ``_cap_already_handled``
    requires BOTH a terminal status AND a failure_detail. So OUR OWN double-apply
    guard is defeated, and the next re-entry re-runs every cap side effect: a SECOND
    close_pull_request, a second alert, a second record_direction_result.

    Post-fix the defer path takes the same FOR UPDATE row lock, re-reads the FRESH
    context, and — seeing the peer's terminal transition — writes nothing at all.
    """
    from fastapi import HTTPException

    from app.review_loop_convergence import (
        _CAP_DEFER_COUNT_KEY,
        _cap_already_handled,
        _handle_cap_exhaustion,
    )

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    _mock_cap_side_effects(mocker)

    project = await _make_db_project(db_session)
    review_loop = {"pr_number": 150, "review_history": [{"verdict": "APPROVE", "issues": []}]}
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "rollup work", "base_branch": "main"}],
        context={
            "_is_rollup_review": True,
            "rollup_pr_number": 150,
            "review_loop": review_loop,
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 150, "original_task_id": str(orig.id)}],
        context={"review_loop": dict(review_loop)},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)
    orig_id = orig.id

    # A PEER cap handler lands the terminal transition WHILE we are on the network.
    # Issued as a bulk UPDATE with synchronize_session=False so it lands in the DB
    # WITHOUT touching our session's identity map — which is the whole point: our
    # in-memory ``orig`` must stay the pre-race snapshot, exactly as in production
    # (``expire_on_commit=False`` means the commit inside _pr_is_preservable_on_cap
    # does not refresh it). Mutating our own instance here would hide the bug.
    from sqlalchemy import update as _sa_update

    peer_committed_ctx = {
        "_is_rollup_review": True,
        "rollup_pr_number": 150,
        "review_loop": review_loop,
        "failure_detail": "review_cap_exhausted",
    }

    async def _rate_limited_after_peer_goes_terminal(*_a, **_kw):
        await db_session.execute(
            _sa_update(Task)
            .where(Task.id == orig_id)
            .values(status=TaskStatus.FAILED, context=peer_committed_ctx)
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        raise HTTPException(
            status_code=503, detail="GitHub rate limit exhausted fetching PR #150"
        )

    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=_rate_limited_after_peer_goes_terminal,
    )

    await _handle_cap_exhaustion(
        source_task, str(orig_id), 150, 16, db_session,
        scope="global", verified_disposition="reject"
    )

    # Re-read from the DB, bypassing the identity map — it would hide the clobber.
    db_session.expire_all()
    fresh = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.id == orig_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    fresh_ctx = fresh.context or {}
    assert fresh.status == TaskStatus.FAILED
    assert fresh_ctx.get("failure_detail") == "review_cap_exhausted", (
        "the defer-path counter RMW ERASED the peer's failure_detail from the "
        "context blob (stale full-blob overwrite). _cap_already_handled requires "
        "BOTH a terminal status AND a failure_detail, so the double-apply guard is "
        "now defeated: the next re-entry re-closes the PR and re-fires every cap "
        "side effect"
    )
    assert _cap_already_handled(fresh) is True, (
        "the cap double-apply guard no longer recognises the peer's terminal "
        "transition — cap side effects will run a SECOND time"
    )
    # Nothing to defer once a peer went terminal: no counter should be written.
    assert _CAP_DEFER_COUNT_KEY not in fresh_ctx
    # The peer's other context keys must survive too.
    assert fresh_ctx.get("review_loop") is not None

    close_mock.assert_not_called()
