"""EVALUATING-parent convergence: the infra-resurrection hold must be releasable.

Regression cover for a permanent-wedge class -- a parent frozen in EVALUATING
with every child terminal, holding forever on::

    "Recovering: infrastructure-failed review/fix task is eligible for
     auto-resurrection"

``rollup_management._check_and_trigger_rollup`` writes that hold under one
predicate, while the only code that can release it -- the reaper's Step 5
infra-recovery sweep in ``reaper_infra_recovery`` -- refused under a *disjoint*
one: the sweep skips every review/fix task carrying a ``pr_number`` before it
ever looks at the failure class, because such a task must be re-driven through
canonical review admission rather than a generic FAILED -> QUEUED flip.
Pipeline review/fix children always carry a ``pr_number``, so the hold was true
in exactly the case where the release was impossible, and the operator-facing
message promised a recovery that was structurally forbidden.

These tests pin BOTH sides of the equivalence so the predicates cannot drift
apart again:

  * the sweep really does refuse a PR-pipeline review child (independent
    evidence -- the real sweep runs, nothing about the decision is mocked), and
  * the rollup must not hold for a child the sweep refuses, while it must
    STILL hold for a child the sweep genuinely would resurrect.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.models import Task, TaskStatus
from app.time_utils import _utcnow

from testing.test_rollup_cleanup import _make_project, _make_task

pytest_plugins = ["testing.test_rollup_cleanup"]

HOLD_MARKER = "eligible for auto-resurrection"


def _mock_rollup_externals(mocker):
    """Neutralise GitHub / broadcast I/O so the rollup decision logic runs."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "additions": 3,
            "deletions": 1,
            "merged": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {"sha": "base-sha", "ref": "research/base"},
            "head": {"sha": "head-sha", "ref": "research/stg"},
        },
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "blocked"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock)


async def _build_stranded_shape(
    session,
    *,
    failed_payload,
    failed_context=None,
    failed_age_hours=0.1,
):
    """The live wedge shape: EVALUATING rollup parent, every child terminal.

    Children: one COMPLETED ``create`` (the rollup trigger), one FAILED
    review/fix carrying ``failed_payload``, one CANCELLED ``fix``.  The parent
    carries ``recheck_after=None``, exactly as the stranded production row did.
    """
    project = await _make_project(session)
    fix_id = str(uuid.uuid4())
    parent = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/stg",
            "parent_base": "research/base",
            "rollup_pr_number": 57,
            "rollup_pr_url": "https://github.com/owner/repo/pull/57",
            "_is_rollup_review": True,
            "review_loop": {
                "pr_number": 57,
                "branch": "research/stg",
                "base_branch": "main",
                "ci_passed": False,
                "ci_waiting": False,
                "ci_fix_spawned": fix_id,
                "review_approved": False,
                "original_task_id": None,
            },
        },
        plan_output="plan",
    )
    parent.recheck_after = None
    session.add(parent)
    done = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    failed = await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        payloads=[failed_payload],
        context=failed_context
        or {
            "failure_category": "EXTERNAL_API",
            "failure_class": "EXTERNAL_API",
        },
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        payloads=[{"type": "fix", "pr_number": 57}],
        context={},
    )
    # Inside the reaper's 2h infra-resurrection lookback window.
    failed.updated_at = _utcnow() - timedelta(hours=failed_age_hours)
    session.add(failed)
    await session.commit()
    return parent, done, failed


# -- the predicate itself ----------------------------------------------------


@pytest.mark.parametrize(
    "payload,context,expected",
    [
        # A pipeline review/fix child on a PR: the sweep refuses outright.
        (
            {"type": "review", "pr_number": 57},
            {"failure_category": "EXTERNAL_API"},
            "pr_pipeline_requires_review_admission",
        ),
        (
            {"type": "fix", "pr_number": 57},
            {"failure_category": "EXTERNAL_API"},
            "pr_pipeline_requires_review_admission",
        ),
        # Not an infra class -- nothing to wait for.
        (
            {"type": "fix"},
            {"failure_category": "CODE_ERROR"},
            "failure_class_not_infra",
        ),
        # One resurrection per task, ever.
        (
            {"type": "fix"},
            {"failure_category": "EXTERNAL_API", "_resurrection_count": 1},
            "already_resurrected",
        ),
        # Terminal PR closures are not infrastructure failures.
        (
            {"type": "fix"},
            {"failure_category": "EXTERNAL_API", "pr_rejected": True},
            "pr_terminal_closure",
        ),
        (
            {"type": "fix"},
            {"failure_category": "EXTERNAL_API", "pr_closed_unmerged": True},
            "pr_terminal_closure",
        ),
        (
            {"type": "fix"},
            {"failure_category": "EXTERNAL_API", "pr_closed_empty": True},
            "pr_terminal_closure",
        ),
        # OOM will just OOM again on the same VM type.
        (
            {"type": "fix"},
            {"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "oom"},
            "oom_not_transient",
        ),
        # Genuinely resurrectable: no block reason.
        ({"type": "fix"}, {"failure_category": "EXTERNAL_API"}, None),
        ({"type": "create"}, {"failure_category": "PROVISION_FAILURE"}, None),
    ],
)
def test_infra_resurrection_block_reason_table(payload, context, expected):
    """The single predicate both the sweep and the rollup hold must consult.

    Every entry is a *reason code*, never a bare bool: a caller that declines
    to act has to be able to say why.
    """
    from app.failure_classification import infra_resurrection_block_reason

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[payload],
        status=TaskStatus.FAILED,
        context=context,
    )
    assert infra_resurrection_block_reason(task) == expected


# -- release side: independent evidence from the real Step 5 sweep -----------


@pytest.mark.asyncio
async def test_step5_sweep_never_resurrects_pr_pipeline_review_child(
    cleanup_db, mocker
):
    """Independent evidence for the hold side: the real sweep leaves it FAILED.

    Nothing about the resurrection decision is mocked here -- the actual
    ``run_infra_recovery_and_hold_maintenance`` runs against the same row shape
    the rollup holds for.  If this ever starts resurrecting PR-pipeline
    review/fix tasks, the rollup's hold becomes legitimate again and the
    convergence test below must be revisited rather than this one relaxed.
    """
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    mocker.patch(
        "app.reaper_infra_recovery._sweep_orphan_direction_holds",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.reaper_infra_recovery._sweep_stale_residual_cases",
        new_callable=AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.reaper_infra_recovery.any_zone_available", return_value=True)
    mocker.patch.dict(
        "app.shared_state.external_health",
        {"github_api": True, "dispatch_api": True},
    )

    _parent, _done, failed = await _build_stranded_shape(
        cleanup_db, failed_payload={"type": "review", "pr_number": 57}
    )

    await run_infra_recovery_and_hold_maintenance(
        cleanup_db, _utcnow(), just_reaped_ids=set()
    )

    await cleanup_db.refresh(failed)
    assert failed.status == TaskStatus.FAILED
    assert (failed.context or {}).get("_resurrection_count", 0) == 0


@pytest.mark.asyncio
async def test_step5_sweep_does_resurrect_a_non_pr_infra_failure(cleanup_db, mocker):
    """The paired positive: the sweep is not simply inert.

    Without this, the test above would also pass if resurrection were deleted
    wholesale, and the rollup hold could then be removed for the wrong reason.
    """
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    mocker.patch(
        "app.reaper_infra_recovery._sweep_orphan_direction_holds",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.reaper_infra_recovery._sweep_stale_residual_cases",
        new_callable=AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.reaper_infra_recovery.any_zone_available", return_value=True)
    mocker.patch.dict(
        "app.shared_state.external_health",
        {"github_api": True, "dispatch_api": True},
    )

    _parent, _done, failed = await _build_stranded_shape(
        cleanup_db, failed_payload={"type": "fix"}
    )

    await run_infra_recovery_and_hold_maintenance(
        cleanup_db, _utcnow(), just_reaped_ids=set()
    )

    await cleanup_db.refresh(failed)
    assert failed.status == TaskStatus.QUEUED


# -- hold side: the parent must converge -------------------------------------


@pytest.mark.asyncio
async def test_evaluating_parent_converges_when_child_is_unresurrectable(
    cleanup_db, mocker
):
    """A non-terminal parent whose children are all terminal must converge.

    Pre-fix this parked forever on the auto-resurrection hold: the rollup
    returned early every cycle waiting for a Step 5 flip that Step 5 refuses to
    perform for a PR-pipeline review child.
    """
    from app.rollup_management import _check_and_trigger_rollup

    _mock_rollup_externals(mocker)
    parent, done, _failed = await _build_stranded_shape(
        cleanup_db, failed_payload={"type": "review", "pr_number": 57}
    )

    await _check_and_trigger_rollup(done, cleanup_db)

    await cleanup_db.refresh(parent)
    assert HOLD_MARKER not in (parent.latest_status_message or ""), (
        "parent held for a resurrection the reaper will never perform: "
        f"{parent.latest_status_message!r}"
    )


@pytest.mark.asyncio
async def test_evaluating_parent_still_holds_for_a_resurrectable_child(
    cleanup_db, mocker
):
    """...and the hold is NOT simply deleted.

    A review/fix child the sweep genuinely will resurrect must still hold the
    parent, otherwise the rollup would race an in-flight recovery.  This is the
    assertion that makes the fix a shared predicate rather than a deletion.
    """
    from app.rollup_management import _check_and_trigger_rollup

    _mock_rollup_externals(mocker)
    parent, done, _failed = await _build_stranded_shape(
        cleanup_db, failed_payload={"type": "fix"}
    )

    await _check_and_trigger_rollup(done, cleanup_db)

    await cleanup_db.refresh(parent)
    assert HOLD_MARKER in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_hold_message_names_the_child_and_its_failure_class(cleanup_db, mocker):
    """Surface reasons, not states: the hold says which child and which class."""
    from app.rollup_management import _check_and_trigger_rollup

    _mock_rollup_externals(mocker)
    parent, done, failed = await _build_stranded_shape(
        cleanup_db, failed_payload={"type": "fix"}
    )

    await _check_and_trigger_rollup(done, cleanup_db)

    await cleanup_db.refresh(parent)
    msg = parent.latest_status_message or ""
    assert str(failed.id)[:8] in msg
    assert "EXTERNAL_API" in msg
