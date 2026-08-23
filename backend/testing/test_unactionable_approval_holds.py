"""Engine escalations must never be stamped onto a row nobody can act on.

``Task.requires_approval`` is a DECISION gate: it means "an operator still has
an answer that changes what happens next".  Four pipeline refusal paths used it
as a NOTIFICATION instead, stamping the latch onto the very review/fix row whose
COMPLETED write they were processing.  Measured on the Blueprint project
2026-08-23: 182 of the 184 rows in the operator approval queue were COMPLETED
corpses, reaching back to PR #15342, and the two live decisions were invisible
among them.

These tests pin the release predicate from both directions.  The NEGATIVE cases
matter most: an operator-owned hold, a security hold, a live merge/follow-up
claim, and any non-COMPLETED row must survive every path here untouched.
"""

import uuid

import pytest

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    APPROVAL_HOLD_EPISODE_KEY,
    APPROVAL_HOLD_PROVENANCE_KEY,
    APPROVAL_HOLD_RELEASED_KEY,
    APPROVAL_MERGE_LEDGER_KEY,
    PIPELINE_ESCALATION_HOLD_OWNERS,
    PIPELINE_ESCALATION_HOLD_REASONS,
    PIPELINE_ESCALATION_HOLD_TASK_TYPES,
    _legacy_pipeline_escalation_reason,
    approval_decision_can_change_anything,
    establish_independent_approval_hold,
    pipeline_escalation_hold_reason,
    pipeline_escalation_hold_was_released,
    settle_unactionable_pipeline_escalation,
)
from app.closed_pr_reconciliation import REVIEW_WAIT_APPROVAL_HOLD_KEY
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)


async def _seed(db_session, *, name, status=TaskStatus.COMPLETED, context=None):
    project = Project(
        name=name,
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Review of PR #77",
        description="Review the exact attempt",
        status=status,
        payloads=[{"type": "review", "branch": "blueprint/x", "prompt": "review"}],
        context=dict(context or {}),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


def _engine_hold(task, *, reason="review_fix_spawn_refused", owner=None):
    """Mint a real provenance-bearing engine escalation through the seam."""

    return establish_independent_approval_hold(
        task,
        reason=reason,
        owner=owner or "review_request_changes",
    )


# ---------------------------------------------------------------------------
# SECURITY/POLICY PINS -- literal, not derived from the constants under test
# ---------------------------------------------------------------------------


def test_releasable_owner_allowlist_is_exactly_the_two_engine_paths():
    """A third owner must never be added silently: only these two are engine."""

    assert set(PIPELINE_ESCALATION_HOLD_OWNERS) == {
        "review_request_changes",
        "task_lifecycle_fix_completion",
    }


def test_releasable_reason_allowlist_excludes_every_security_reason():
    """``security_scan_*`` holds are operator business however dead the row is."""

    assert set(PIPELINE_ESCALATION_HOLD_REASONS) == {
        "review_fix_spawn_refused",
        "review_spawn_failed",
        "review_target_unavailable",
        "originating_task_missing",
    }
    assert "security_scan_blocked" not in PIPELINE_ESCALATION_HOLD_REASONS
    assert "security_scan_failed" not in PIPELINE_ESCALATION_HOLD_REASONS
    assert "spend_budget" not in PIPELINE_ESCALATION_HOLD_REASONS


def test_releasable_row_identity_is_only_the_review_and_fix_lanes():
    """Widening this re-opens every reader that reads the CHAIN ORIGINAL.

    ``app.task_lifecycle._publish_exact_review_refusal``, the critical-approve
    ``orig_task`` branch, ``app.review_approval_gate_publication`` and
    ``app.ci_completion``'s merge gate all read ``requires_approval`` on the
    chain original / rollup parent and treat "no hold" as licence to act.  A
    chain original is a PR-OPENING write row, so it is never a review or a fix
    row -- which is exactly why those readers cannot see a released hold.
    """

    assert set(PIPELINE_ESCALATION_HOLD_TASK_TYPES) == {"review", "fix"}
    assert "create" not in PIPELINE_ESCALATION_HOLD_TASK_TYPES
    assert "" not in PIPELINE_ESCALATION_HOLD_TASK_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_type",
    ["create", "plan", "refactor", "debug", "research", "verify_tests", ""],
)
async def test_hold_on_a_non_review_lane_row_is_never_released(
    db_session, payload_type
):
    """A chain original wearing the same reason is still off limits.

    This cannot happen through the two writers, and the pin is what keeps it
    that way: the chain original's latch is load-bearing for review-publication
    authority, so releasing it would hand a stale approval a live target.
    """

    _project, task = await _seed(db_session, name=f"lane-{payload_type or 'blank'}")
    task.payloads = [{"type": payload_type, "prompt": "x"}]
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_legacy_latch_on_a_non_review_lane_row_is_never_released(db_session):
    """The legacy message recogniser is gated by row identity too."""

    _project, task = await _seed(db_session, name="lane-legacy-create")
    task.payloads = [{"type": "create", "prompt": "x"}]
    task.requires_approval = True
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_fix_lane_row_is_releasable(db_session):
    """The other half of the pinned set is genuinely reachable."""

    _project, task = await _seed(db_session, name="lane-fix")
    task.payloads = [{"type": "fix", "prompt": "x"}]
    task.latest_status_message = "Review blocked PR #77, but review spawn failed (x)"
    _engine_hold(
        task, reason="review_spawn_failed", owner="task_lifecycle_fix_completion"
    )
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) == "review_spawn_failed"


# ---------------------------------------------------------------------------
# Positive: an inert engine escalation is retracted, with a receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_engine_escalation_is_released_with_receipt(db_session):
    _project, task = await _seed(db_session, name="release-positive")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()
    assert task.requires_approval is True

    released = await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    await db_session.commit()

    assert released == "review_fix_spawn_refused"
    assert task.requires_approval is False
    assert pipeline_escalation_hold_was_released(task) is True
    receipt = task.context[APPROVAL_HOLD_RELEASED_KEY]
    assert receipt["reason"] == "review_fix_spawn_refused"
    assert receipt["owner"] == "review_request_changes"
    assert receipt["released_by"] == "unit-test"
    assert receipt["task_status"] == "COMPLETED"
    assert receipt["released_at"]
    # The episode metadata is consumed through the provenance seam, not left
    # behind to make a later hold look already-established.
    assert APPROVAL_HOLD_EPISODE_KEY not in task.context
    assert APPROVAL_HOLD_PROVENANCE_KEY not in task.context
    assert "approval_hold_reason" not in task.context
    # Gates must emit WHY they unblocked.
    assert "already COMPLETED" in task.latest_status_message
    assert "Fix not spawned for PR #77" in task.latest_status_message


@pytest.mark.asyncio
async def test_release_is_idempotent_across_replay(db_session):
    _project, task = await _seed(db_session, name="release-idempotent")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    first = task.latest_status_message

    # Replay: the post-commit path re-establishes and re-settles.
    _engine_hold(task)
    await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    db_session.add(task)
    await db_session.commit()

    assert task.requires_approval is False
    assert task.latest_status_message == first
    assert task.latest_status_message.count("already COMPLETED") == 1


@pytest.mark.asyncio
async def test_every_allowlisted_reason_is_released(db_session):
    for index, reason in enumerate(sorted(PIPELINE_ESCALATION_HOLD_REASONS)):
        owner = (
            "task_lifecycle_fix_completion"
            if reason == "review_spawn_failed"
            else "review_request_changes"
        )
        _project, task = await _seed(db_session, name=f"reason-{index}-{reason}")
        task.latest_status_message = f"escalation for {reason}"
        _engine_hold(task, reason=reason, owner=owner)
        db_session.add(task)
        await db_session.commit()
        released = await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        await db_session.commit()
        assert released == reason, reason
        assert task.requires_approval is False, reason


# ---------------------------------------------------------------------------
# NEGATIVE: holds an operator genuinely owns are never released
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ],
)
async def test_non_completed_row_is_never_released(db_session, status):
    """Only COMPLETED is provably inert.

    FAILED especially: ``app.task_lifecycle._review_publication_identity_is_current``
    keeps review-publication authority on a FAILED original precisely BECAUSE it
    still carries an independent hold, so clearing it there loses a real signal.
    """

    _project, task = await _seed(
        db_session, name=f"non-terminal-{status.value}", status=status
    )
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()

    assert await approval_decision_can_change_anything(task, db_session) is True
    released = await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    assert released is None
    assert task.requires_approval is True
    assert APPROVAL_HOLD_RELEASED_KEY not in task.context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["security_scan_blocked", "security_scan_failed", "spend_budget"]
)
async def test_security_and_policy_reasons_are_never_released(db_session, reason):
    _project, task = await _seed(db_session, name=f"security-{reason}")
    task.latest_status_message = "Blocked by security scan"
    _engine_hold(task, reason=reason, owner="task_lifecycle_fix_completion")
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    released = await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    assert released is None
    assert task.requires_approval is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner",
    [
        "operator",
        "review_pipeline",
        "review_loop_convergence",
        "rollup_lineage_fence",
        "admin_branch_safety",
        "ci_completion",
    ],
)
async def test_non_engine_owner_is_never_released(db_session, owner):
    """A reason inside the allowlist is not enough -- the owner must match too."""

    _project, task = await _seed(db_session, name=f"owner-{owner}")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task, reason="review_fix_spawn_refused", owner=owner)
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    released = await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    assert released is None
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_live_operator_followup_claim_blocks_release(db_session):
    _project, task = await _seed(db_session, name="followup-claim")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    task.context = {
        **task.context,
        APPROVAL_FOLLOWUP_CLAIM_KEY: {
            "state": "pending",
            "token": str(uuid.uuid4()),
            "followup_kind": "review_spawn",
        },
    }
    db_session.add(task)
    await db_session.commit()

    assert await approval_decision_can_change_anything(task, db_session) is True
    assert pipeline_escalation_hold_reason(task) is None
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_in_flight_merge_intent_blocks_release(db_session):
    _project, task = await _seed(db_session, name="merge-in-flight")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    task.context = {
        **task.context,
        APPROVAL_MERGE_LEDGER_KEY: {"state": "ambiguous", "pr_number": 77},
    }
    db_session.add(task)
    await db_session.commit()

    assert await approval_decision_can_change_anything(task, db_session) is True
    assert pipeline_escalation_hold_reason(task) is None
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_review_wait_hold_blocks_release(db_session):
    _project, task = await _seed(db_session, name="review-wait")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    task.context = {
        **task.context,
        "review_loop": {
            **(task.context.get("review_loop") or {}),
            REVIEW_WAIT_APPROVAL_HOLD_KEY: {"owner": "review_wait", "pr_number": 77},
        },
    }
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_completed_row_with_pr_artifact_stays_actionable(db_session):
    """A COMPLETED row that can still publish a follow-up is NOT a corpse."""

    _project, task = await _seed(db_session, name="actionable-artifact")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/77",
        )
    )
    await db_session.commit()

    assert await approval_decision_can_change_anything(task, db_session) is True
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_completed_rollup_row_with_pr_number_stays_actionable(db_session):
    _project, task = await _seed(
        db_session, name="actionable-rollup", context={"rollup_pr_number": 91}
    )
    task.latest_status_message = "Fix not spawned for PR #91 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()

    assert await approval_decision_can_change_anything(task, db_session) is True
    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_episode_provenance_mismatch_blocks_release(db_session):
    """A provenance dict whose episode disagrees with the latch fails closed."""

    _project, task = await _seed(db_session, name="episode-mismatch")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    provenance = dict(task.context[APPROVAL_HOLD_PROVENANCE_KEY])
    provenance["episode"] = str(uuid.uuid4())
    task.context = {**task.context, APPROVAL_HOLD_PROVENANCE_KEY: provenance}
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_latch_false_is_never_released(db_session):
    _project, task = await _seed(db_session, name="latch-false")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    task.requires_approval = False
    db_session.add(task)
    await db_session.commit()

    assert pipeline_escalation_hold_reason(task) is None


# ---------------------------------------------------------------------------
# Legacy pre-provenance latches (the ~182 measured corpses)
# ---------------------------------------------------------------------------


def _legacy_call_site_messages():
    """The exact runtime strings the four refusal sites write.

    Reconstructed from the literal f-strings so a message edit at a call site
    that breaks the legacy recogniser fails HERE instead of silently shrinking
    the sweep's reach.
    """

    pr_number = 77
    label = "Iteration Cap Reached"
    return {
        # app.review_request_changes, review_fix_spawn_refused
        "review_fix_spawn_refused": f"Fix not spawned for PR #{pr_number} — {label}",
        # app.review_request_changes, review_target_unavailable
        "review_target_unavailable": (
            f"Review changes for PR #{pr_number} withheld: could not "
            "prove the live immutable review target"
        ),
        # app.review_request_changes, originating_task_missing
        "originating_task_missing": (
            f"Review blocked PR #{pr_number}, but its originating "
            "task is missing; fix withheld for manual recovery"
        ),
        # app.task_lifecycle_fix_completion, review_spawn_failed
        "review_spawn_failed": (
            f"Fix applied for PR #{pr_number} but review spawn failed ({label})"
        ),
    }


@pytest.mark.parametrize(
    "reason,message", sorted(_legacy_call_site_messages().items())
)
def test_legacy_marker_recognises_each_real_call_site_message(reason, message):
    # The lane each message is written from: ``review_spawn_failed`` is the
    # ``app.task_lifecycle_fix_completion`` (fix) site, the other three are
    # ``app.review_request_changes`` (review).
    lane = "fix" if reason == "review_spawn_failed" else "review"
    task = Task(
        project_id=uuid.uuid4(),
        title="legacy",
        description="legacy",
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        latest_status_message=message,
        payloads=[{"type": lane, "prompt": "legacy"}],
        context={},
    )
    assert _legacy_pipeline_escalation_reason(task) == reason
    assert pipeline_escalation_hold_reason(task) == reason


def test_legacy_marker_ignores_the_transient_review_spawn_message():
    """The transient sibling never set a hold, so it must not be recognised."""

    task = Task(
        project_id=uuid.uuid4(),
        title="legacy",
        description="legacy",
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        latest_status_message=(
            "Fix applied for PR #77 but review spawn deferred (concurrent spawn)"
        ),
        context={},
    )
    assert _legacy_pipeline_escalation_reason(task) is None
    assert pipeline_escalation_hold_reason(task) is None


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Waiting for operator disposition",
        "PR #77 is parked for operator disposition after a cross-episode abandon",
        "requires_approval for hand-merge",
        "Blocked by security scan",
    ],
)
def test_legacy_marker_ignores_unrelated_messages(message):
    task = Task(
        project_id=uuid.uuid4(),
        title="legacy",
        description="legacy",
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        latest_status_message=message,
        context={},
    )
    assert _legacy_pipeline_escalation_reason(task) is None
    assert pipeline_escalation_hold_reason(task) is None


def test_legacy_marker_is_ignored_once_any_seam_metadata_exists():
    """A latch minted through the seam is judged on owner/reason, never message."""

    base = {
        "project_id": uuid.uuid4(),
        "title": "legacy",
        "description": "legacy",
        "status": TaskStatus.COMPLETED,
        "requires_approval": True,
        "latest_status_message": "Fix not spawned for PR #77 — Iteration Cap Reached",
    }
    for context in (
        {"approval_hold_reason": "cross_episode_abandon_candidate"},
        {APPROVAL_HOLD_EPISODE_KEY: str(uuid.uuid4())},
        {APPROVAL_HOLD_PROVENANCE_KEY: {"owner": "review_pipeline"}},
    ):
        task = Task(context=dict(context), **base)
        assert pipeline_escalation_hold_reason(task) is None, context


@pytest.mark.asyncio
async def test_legacy_bare_latch_on_completed_row_is_released(db_session):
    _project, task = await _seed(db_session, name="legacy-release")
    task.requires_approval = True
    task.latest_status_message = "Fix not spawned for PR #77 — Iteration Cap Reached"
    db_session.add(task)
    await db_session.commit()

    released = await settle_unactionable_pipeline_escalation(
        task, db_session, released_by="unit-test"
    )
    await db_session.commit()

    assert released == "review_fix_spawn_refused"
    assert task.requires_approval is False
    receipt = task.context[APPROVAL_HOLD_RELEASED_KEY]
    assert receipt["owner"] is None
    assert receipt["reason"] == "review_fix_spawn_refused"


@pytest.mark.asyncio
async def test_legacy_bare_latch_on_non_completed_row_is_kept(db_session):
    _project, task = await _seed(
        db_session, name="legacy-evaluating", status=TaskStatus.EVALUATING
    )
    task.requires_approval = True
    task.latest_status_message = "Fix not spawned for PR #77 — Iteration Cap Reached"
    db_session.add(task)
    await db_session.commit()

    assert (
        await settle_unactionable_pipeline_escalation(
            task, db_session, released_by="unit-test"
        )
        is None
    )
    assert task.requires_approval is True


# ---------------------------------------------------------------------------
# Call-site wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_changes_hold_settles_inline(db_session):
    from app.review_request_changes import _establish_request_changes_hold

    _project, task = await _seed(db_session, name="rrc-inline")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    await _establish_request_changes_hold(
        task, db_session, reason="review_fix_spawn_refused"
    )
    db_session.add(task)
    await db_session.commit()

    assert task.requires_approval is False
    assert pipeline_escalation_hold_was_released(task) is True
    assert "already COMPLETED" in task.latest_status_message


@pytest.mark.asyncio
async def test_request_changes_hold_survives_on_a_live_row(db_session):
    from app.review_request_changes import _establish_request_changes_hold

    _project, task = await _seed(
        db_session, name="rrc-live", status=TaskStatus.EVALUATING
    )
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    await _establish_request_changes_hold(
        task, db_session, reason="review_fix_spawn_refused"
    )
    db_session.add(task)
    await db_session.commit()

    assert task.requires_approval is True
    assert pipeline_escalation_hold_was_released(task) is False


@pytest.mark.asyncio
async def test_fix_completion_security_hold_survives_on_a_dead_row(db_session):
    """The sibling wiring must NOT release a security escalation."""

    from app.task_lifecycle_fix_completion import _establish_fix_completion_hold

    _project, task = await _seed(db_session, name="fix-security")
    task.latest_status_message = "Prompt injection detected in fix output"
    await _establish_fix_completion_hold(
        task, db_session, reason="security_scan_blocked"
    )
    db_session.add(task)
    await db_session.commit()

    assert task.requires_approval is True
    assert pipeline_escalation_hold_was_released(task) is False
    assert task.context["approval_hold_reason"] == "security_scan_blocked"


@pytest.mark.asyncio
async def test_fix_completion_spawn_failure_settles_inline(db_session):
    from app.task_lifecycle_fix_completion import _establish_fix_completion_hold

    _project, task = await _seed(db_session, name="fix-spawn-failed")
    task.latest_status_message = (
        "Fix applied for PR #77 but review spawn failed (Iteration Cap)"
    )
    await _establish_fix_completion_hold(
        task, db_session, reason="review_spawn_failed"
    )
    db_session.add(task)
    await db_session.commit()

    assert task.requires_approval is False
    assert pipeline_escalation_hold_was_released(task) is True


# ---------------------------------------------------------------------------
# Reaper sweep (the legacy backlog)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_releases_corpses_and_leaves_owned_holds_alone(db_session):
    import app.reaper_sweeps as rsw

    project = Project(
        name="sweep-project",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _mk(title, *, status, message, context=None):
        task = Task(
            project_id=project.id,
            title=title,
            description=title,
            status=status,
            payloads=[{"type": "review", "prompt": "review"}],
            latest_status_message=message,
            context=dict(context or {}),
        )
        db_session.add(task)
        return task

    corpse = _mk(
        "corpse",
        status=TaskStatus.COMPLETED,
        message="Fix not spawned for PR #77 - Iteration Cap Reached",
    )
    legacy = _mk(
        "legacy",
        status=TaskStatus.COMPLETED,
        message="Fix not spawned for PR #78 — Iteration Cap Reached",
    )
    security = _mk(
        "security",
        status=TaskStatus.COMPLETED,
        message="Prompt injection detected",
    )
    live = _mk(
        "live",
        status=TaskStatus.EVALUATING,
        message="Fix not spawned for PR #79 - Iteration Cap Reached",
    )
    other_owner = _mk(
        "other-owner",
        status=TaskStatus.COMPLETED,
        message="Fix not spawned for PR #80 - Iteration Cap Reached",
    )
    await db_session.commit()

    _engine_hold(corpse)
    legacy.requires_approval = True
    _engine_hold(
        security, reason="security_scan_blocked", owner="task_lifecycle_fix_completion"
    )
    _engine_hold(live)
    _engine_hold(other_owner, owner="review_pipeline")
    for task in (corpse, legacy, security, live, other_owner):
        db_session.add(task)
    await db_session.commit()

    saved = rsw._last_unactionable_approval_hold_sweep
    try:
        rsw._last_unactionable_approval_hold_sweep = None
        released = await rsw.sweep_release_unactionable_approval_holds(db_session)
    finally:
        rsw._last_unactionable_approval_hold_sweep = saved

    assert released == 2
    for task in (corpse, legacy, security, live, other_owner):
        await db_session.refresh(task)
    assert corpse.requires_approval is False
    assert legacy.requires_approval is False
    assert security.requires_approval is True
    assert live.requires_approval is True
    assert other_owner.requires_approval is True
    assert "already COMPLETED" in corpse.latest_status_message
    assert corpse.context[APPROVAL_HOLD_RELEASED_KEY]["released_by"] == (
        "reaper_unactionable_approval_hold_sweep"
    )


@pytest.mark.asyncio
async def test_sweep_is_cadence_gated(db_session):
    import app.reaper_sweeps as rsw
    from app.time_utils import _utcnow

    _project, task = await _seed(db_session, name="sweep-cadence")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()

    saved = rsw._last_unactionable_approval_hold_sweep
    try:
        rsw._last_unactionable_approval_hold_sweep = _utcnow()
        released = await rsw.sweep_release_unactionable_approval_holds(db_session)
    finally:
        rsw._last_unactionable_approval_hold_sweep = saved

    assert released == 0
    await db_session.refresh(task)
    assert task.requires_approval is True


@pytest.mark.asyncio
async def test_sweep_skips_archived_rows(db_session):
    import app.reaper_sweeps as rsw

    _project, task = await _seed(db_session, name="sweep-archived")
    task.latest_status_message = "Fix not spawned for PR #77 - Iteration Cap Reached"
    task.archived = True
    _engine_hold(task)
    db_session.add(task)
    await db_session.commit()

    saved = rsw._last_unactionable_approval_hold_sweep
    try:
        rsw._last_unactionable_approval_hold_sweep = None
        released = await rsw.sweep_release_unactionable_approval_holds(db_session)
    finally:
        rsw._last_unactionable_approval_hold_sweep = saved

    assert released == 0
    await db_session.refresh(task)
    assert task.requires_approval is True


def test_sweep_is_registered_in_the_reaper_cycle():
    import inspect

    import app.reaper as reaper

    source = inspect.getsource(reaper.reaper_service)
    assert "sweep_release_unactionable_approval_holds(" in source
    assert reaper.sweep_release_unactionable_approval_holds is not None
