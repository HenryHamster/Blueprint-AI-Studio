"""Fresh, attempt-scoped authority for operator approval decisions.

The local approval decision is committed under the Task row lock before any
Project, PR, or GitHub work. Downstream work receives an immutable receipt and
must revalidate it after the canonical Project/PR locks are acquired.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Artifact,
    ArtifactType,
    Task,
    TaskHistory,
    TaskStatus,
    _utcnow,
)
from app.task_locking import task_lock

APPROVAL_FOLLOWUP_CLAIM_KEY = "_operator_approval_followup_claim"
APPROVAL_HOLD_EPISODE_KEY = "_approval_hold_episode"
APPROVAL_HOLD_PROVENANCE_KEY = "_approval_hold_provenance"
APPROVAL_MERGE_LEDGER_KEY = "_operator_approval_merge_ledger"
APPROVAL_MERGE_FACT_KEY = "_operator_approval_merge_fact"

APPROVAL_MERGE_HISTORY_ACTIONS = frozenset(
    {
        "APPROVAL_MERGE_INTENT_CREATED",
        "APPROVAL_MERGE_UNSENT_ABORTED",
        "APPROVAL_MERGE_SUBMISSION_TERMINAL",
        "APPROVAL_MERGE_SUBMISSION_STARTED",
        "APPROVAL_MERGE_OBSERVED_MERGED",
        "APPROVAL_MERGE_SUBMITTED",
        "APPROVAL_MERGE_FINALIZED",
        "APPROVAL_MERGE_POST_EFFECT",
    }
)


@dataclass(frozen=True)
class ApprovalDecision:
    """Durable operator decision and the exact follow-up authority it created."""

    task_id: uuid.UUID
    project_id: uuid.UUID
    desired: bool
    was_blocked: bool
    status: TaskStatus | str
    modified_by: str
    should_follow_up: bool
    followup_kind: str | None
    claim: Dict[str, Any] | None
    pr_number: int | None
    rollup: bool
    artifact_id: uuid.UUID | None
    artifact_pr_url: str | None
    merge_intent_id: str | None = None
    post_effects_only: bool = False
    merge_recovery: bool = False
    merge_ledger: Dict[str, Any] | None = None


def _status_value(status: TaskStatus | str) -> str:
    return status.value if isinstance(status, TaskStatus) else str(status)


def _normalize_approval_review_target(
    *,
    project_id: object,
    pr_number: object,
    head_sha: object,
    base_sha: object,
) -> Dict[str, Any] | None:
    """Return one complete immutable review target, never an empty-SHA shell."""

    try:
        normalized_pr = int(pr_number)
    except (TypeError, ValueError):
        return None
    normalized_project = str(project_id or "")
    if (
        not normalized_project
        or normalized_pr <= 0
        or not isinstance(head_sha, str)
        or not head_sha
        or not isinstance(base_sha, str)
        or not base_sha
    ):
        return None
    return {
        "project_id": normalized_project,
        "pr_number": normalized_pr,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def approval_review_spawn_target(
    task: Task,
    *,
    pr_number: object,
) -> Dict[str, Any] | None:
    """Resolve the source row's exact review target with legacy compatibility."""

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    nested = review_loop.get("review_target")
    nested_target = dict(nested) if isinstance(nested, dict) else {}
    loop_pr = review_loop.get("pr_number")
    try:
        expected_pr = int(pr_number)
        current_pr = int(
            loop_pr if loop_pr not in (None, "") else nested_target.get("pr_number")
        )
    except (TypeError, ValueError):
        return None
    if current_pr != expected_pr:
        return None
    head_sha = (
        review_loop.get("ci_checked_head_sha")
        or review_loop.get("head_sha")
        or nested_target.get("head_sha")
    )
    base_sha = (
        review_loop.get("ci_checked_base_sha")
        or review_loop.get("base_sha")
        or nested_target.get("base_sha")
    )
    target = _normalize_approval_review_target(
        project_id=task.project_id,
        pr_number=expected_pr,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    if target is None:
        return None
    if nested_target:
        nested_normalized = _normalize_approval_review_target(
            project_id=nested_target.get("project_id"),
            pr_number=nested_target.get("pr_number"),
            head_sha=nested_target.get("head_sha"),
            base_sha=nested_target.get("base_sha"),
        )
        if nested_normalized != target:
            return None
    return target


def approval_review_spawn_claim_target(
    decision: ApprovalDecision,
    claim: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Return the exact target carried by a review-spawn decision claim."""

    claim = dict(claim if claim is not None else (decision.claim or {}))
    target = _normalize_approval_review_target(
        project_id=claim.get("project_id"),
        pr_number=claim.get("pr_number"),
        head_sha=claim.get("head_sha"),
        base_sha=claim.get("base_sha"),
    )
    if target is None or target["project_id"] != str(decision.project_id):
        return None
    try:
        return target if target["pr_number"] == int(decision.pr_number) else None
    except (TypeError, ValueError):
        return None


def approval_hold_provenance(task: Task) -> Dict[str, Any]:
    """Return the exact approval episode that a disposition may consume.

    ``requires_approval`` is only a boolean latch.  The timer, explicit reason,
    terminal-review breadcrumb, and review-wait owner distinguish one hold from
    a later independent hold that happens to leave the latch TRUE.
    """

    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    return {
        "episode": context.get(APPROVAL_HOLD_EPISODE_KEY),
        "set_at": context.get("_approval_set_at"),
        "reason": copy.deepcopy(context.get("approval_hold_reason")),
        "touched_critical": copy.deepcopy(context.get("touched_critical")),
        "terminal_outcome": copy.deepcopy(context.get("review_loop_terminal_outcome")),
        "review_wait_hold": copy.deepcopy(review_loop.get("review_wait_approval_hold")),
    }


def consume_approval_hold_metadata(
    task: Task,
    *,
    expected_episode: Dict[str, Any] | None,
) -> bool:
    """Clear only metadata proven to belong to one released hold episode.

    ``touched_critical`` is durable evidence, not hold ownership, and is never
    removed here.  A later independent episode changes the provenance snapshot
    and therefore makes this operation a no-op.
    """

    if not isinstance(expected_episode, dict):
        return False
    token = expected_episode.get("episode")
    context = dict(task.context or {})
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    if not (
        token
        and context.get(APPROVAL_HOLD_EPISODE_KEY) == token
        and isinstance(provenance, dict)
        and provenance.get("episode") == token
        and approval_hold_provenance(task) == expected_episode
    ):
        return False
    context.pop(APPROVAL_HOLD_EPISODE_KEY, None)
    context.pop(APPROVAL_HOLD_PROVENANCE_KEY, None)
    if context.get("_approval_set_at") == expected_episode.get("set_at"):
        context.pop("_approval_set_at", None)
    if context.get("approval_hold_reason") == expected_episode.get("reason"):
        context.pop("approval_hold_reason", None)
    if context.get("review_loop_terminal_outcome") == expected_episode.get(
        "terminal_outcome"
    ):
        context.pop("review_loop_terminal_outcome", None)
    task.context = context
    return True


def establish_independent_approval_hold(
    task: Task,
    *,
    reason: str,
    owner: str,
) -> str:
    """Install a new TRUE hold episode, even when the latch was already TRUE.

    Callers own the Task lock and commit.  This is the single provenance seam
    for a manual/policy hold that must outrank an older review-wait timer or
    operator follow-up claim.
    """

    from app.closed_pr_reconciliation import revoke_review_wait_approval_hold

    revoke_review_wait_approval_hold(task)
    revoke_operator_approval_followup(task)
    consume_approval_hold_metadata(
        task,
        expected_episode=approval_hold_provenance(task),
    )
    episode = str(uuid.uuid4())
    set_at = _utcnow().isoformat()
    context = dict(task.context or {})
    context["_approval_set_at"] = set_at
    context["approval_hold_reason"] = str(reason)
    context[APPROVAL_HOLD_EPISODE_KEY] = episode
    context[APPROVAL_HOLD_PROVENANCE_KEY] = {
        "episode": episode,
        "owner": str(owner),
        "reason": str(reason),
        "set_at": set_at,
    }
    task.context = context
    task.requires_approval = True
    return episode


def approval_merge_not_sent_is_current(task: Task, ledger: Dict[str, Any]) -> bool:
    """Whether a known-unsent intent still owns the exact live approval claim."""

    context = dict(task.context or {})
    claim = context.get(APPROVAL_FOLLOWUP_CLAIM_KEY)
    episode = ledger.get("approval_episode")
    return bool(
        ledger.get("state") == "not_sent"
        and isinstance(claim, dict)
        and task.requires_approval is True
        and task.archived is not True
        and str(task.id) == str(ledger.get("task_id") or "")
        and str(task.project_id) == str(ledger.get("project_id") or "")
        and _status_value(task.status) == str(ledger.get("source_status") or "")
        and claim.get("state") == "merge_intent"
        and claim.get("token") == ledger.get("approval_decision_token")
        and claim.get("merge_intent_id") == ledger.get("intent_id")
        and claim.get("approval_episode") == episode
        and _claim_approval_episode_matches(task, claim)
        and str(claim.get("task_id") or "") == str(ledger.get("task_id") or "")
        and str(claim.get("project_id") or "") == str(ledger.get("project_id") or "")
        and str(claim.get("pr_number") or "") == str(ledger.get("pr_number") or "")
        and claim.get("followup_kind") == ledger.get("followup_kind")
        and str(claim.get("head_sha") or "") == str(ledger.get("head_sha") or "")
        and str(claim.get("base_sha") or "") == str(ledger.get("base_sha") or "")
    )


def approval_merge_is_in_flight(task: Task) -> bool:
    """Whether request-changes/fix admission must fence a possible merge PUT."""

    ledger = (task.context or {}).get(APPROVAL_MERGE_LEDGER_KEY)
    return bool(
        isinstance(ledger, dict)
        and (
            approval_merge_not_sent_is_current(task, ledger)
            or ledger.get("state") == "ambiguous"
            or (
                ledger.get("state") == "merged"
                and ledger.get("semantic_state")
                not in {"finalized", "fact_only_finalized"}
            )
        )
    )


def _claim_approval_episode_matches(task: Task, claim: Dict[str, Any]) -> bool:
    expected = claim.get("approval_episode")
    if isinstance(expected, dict):
        return approval_hold_provenance(task) == expected
    # Rolling compatibility for claims created before episode provenance was
    # added.  They remain usable only while no newer-code hold episode exists;
    # once an independent helper stamps an episode the legacy claim loses.
    context = dict(task.context or {})
    return not context.get(APPROVAL_HOLD_EPISODE_KEY) and not context.get(
        APPROVAL_HOLD_PROVENANCE_KEY
    )


def _parse_pr_number(pr_url: str | None) -> int | None:
    try:
        return int(str(pr_url or "").rstrip("/").split("/")[-1])
    except (TypeError, ValueError):
        return None


async def _regular_pr_artifact(
    session: AsyncSession, task_id: uuid.UUID
) -> Artifact | None:
    return (
        (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# Unactionable engine-escalation holds
# ---------------------------------------------------------------------------
#
# ``requires_approval`` is a DECISION gate: it means "a human still has an
# answer that changes what happens next".  Several pipeline refusal paths used
# it as a NOTIFICATION instead, stamping the latch onto the very review/fix row
# whose completion they were processing.  That row is already COMPLETED, so the
# operator queue (``GET /api/admin/tasks?approval_needed=true``, i.e.
# ``blueprint tasks list --approval-needed``) filled with rows on which
# approving and denying produce the same outcome: nothing.  Measured on the
# Blueprint project 2026-08-23, 182 of 184 approval-needed rows were COMPLETED,
# reaching back to PR #15342 -- the two live decisions were invisible inside
# the corpses.  Notification already has its own lane: every one of these sites
# also raises a WARNING ``review.escalated`` alert and writes an explicit
# ``latest_status_message``.

APPROVAL_HOLD_RELEASED_KEY = "_approval_hold_released_because"

# SECURITY/POLICY PIN.  Only an ENGINE-owned escalation may ever be released by
# machine.  Every owner listed here mints its hold from a pipeline spawn
# refusal, never from a human decision.  Owners that must stay OUT of this set,
# because a person or the merge transport owns their holds:
# ``operator``/``<operator email>`` (the explicit hold in
# ``decide_operator_approval``), ``operator_approval_followup``,
# ``approval_followup_refused``, ``admin_branch_safety``,
# ``approval_merge_precheck_refused``, ``approval_merge_unsent_abort``,
# ``approval_merge_submission_terminal``, ``approval_merge_fact_only``,
# ``rollup_lineage_fence`` (its holds land on a live EVALUATING/BLOCKED
# ancestor precisely so they ARE actionable), ``review_loop_convergence``
# (``*_preserved_for_handmerge`` literally asks for a hand merge),
# ``review_pipeline``, ``task_lifecycle``, ``ci_completion``,
# ``orchestrator_ci_monitor``, ``migration_guard_recovery``,
# ``rollup_management``.
PIPELINE_ESCALATION_HOLD_OWNERS = frozenset(
    {
        "review_request_changes",
        "task_lifecycle_fix_completion",
    }
)

# SECURITY/POLICY PIN.  The reason must match TOO, not just the owner: both
# owners above also mint SECURITY holds -- ``security_scan_blocked`` and
# ``security_scan_failed`` in ``app.task_lifecycle_fix_completion`` -- and
# those stay operator business no matter how dead the row is.  They are
# deliberately absent from this set and must not be added without an
# independent security review.
PIPELINE_ESCALATION_HOLD_REASONS = frozenset(
    {
        "review_fix_spawn_refused",
        "review_spawn_failed",
        "review_target_unavailable",
        "originating_task_missing",
    }
)

# SECURITY/POLICY PIN -- row identity.  Both owners above are COMPLETED-write
# handlers for exactly two lanes: ``app.review_request_changes`` runs under
# ``app.task_lifecycle``'s ``elif task_type == "review"`` and
# ``app.task_lifecycle_fix_completion`` under its ``elif task_type == "fix"``,
# so the four reasons can only ever be stamped on a review or a fix row.
# Pinning that here is not redundant with the owner/reason pins: it is what
# makes the audited readers that treat "no hold" as licence to act provably
# unreachable.  Every one of them reads a DIFFERENT row --
#   ``app.task_lifecycle._publish_exact_review_refusal``   (chain original)
#   ``app.task_lifecycle`` critical-approve ``orig_task``  (chain original)
#   ``app.review_approval_gate_publication``               (chain original)
#   ``app.ci_completion`` merge gate                       (rollup parent)
# -- and a chain original is by construction a PR-OPENING write row:
# ``_PR_OPENING_COMPLETION_TYPES`` is ``WRITE_TASK_TYPES - {"fix"}`` and
# "review" is not a write type, so ``review_loop["original_task_id"]`` can
# never name a review or fix row.  Widening this set therefore re-opens those
# readers and needs the reachability argument re-run, not just a rename.
PIPELINE_ESCALATION_HOLD_TASK_TYPES = frozenset({"review", "fix"})

_HOLD_RELEASE_NOTE = (
    "approval hold released: this row is already COMPLETED, so no operator "
    "approval decision can change its outcome"
)


async def approval_decision_can_change_anything(
    task: Task, session: AsyncSession
) -> bool:
    """Whether flipping ``requires_approval`` on this row still has an effect.

    This mirrors the ``should_follow_up`` computation in
    :func:`decide_operator_approval` exactly, because publishing a follow-up is
    the only thing a disposition on a finished row can still cause.  A
    COMPLETED row with a resolvable PR number publishes a review spawn /
    critical merge / rollup merge and is therefore genuinely actionable; a
    COMPLETED row without one only flips a boolean nobody acts on.

    Fails OPEN (returns ``True``) for every state this module cannot prove
    inert, including FAILED and CANCELLED: ``app.task_lifecycle``'s
    ``_review_publication_identity_is_current`` keeps review-publication
    authority on a FAILED original *because* it still carries an independent
    hold, so a FAILED row's latch is load-bearing.
    """

    if task.status != TaskStatus.COMPLETED:
        return True
    context = dict(task.context or {})
    if context.get(APPROVAL_FOLLOWUP_CLAIM_KEY):
        # An operator already decided; the claim is the durable receipt of it.
        return True
    if approval_merge_is_in_flight(task):
        return True
    rollup_pr_number = context.get("rollup_pr_number")
    if rollup_pr_number is not None:
        return _parse_pr_number(str(rollup_pr_number)) is not None
    artifact = await _regular_pr_artifact(session, task.id)
    return _parse_pr_number(artifact.pr_url if artifact else None) is not None


# SECURITY/POLICY PIN, ``(status-message marker, reason)``.  Episode provenance
# landed in PR #17399 (2026-08-17); before it, all four sites below were a bare
# ``task.requires_approval = True`` with no reason, no owner and no episode, and
# the measured corpses reach back to PR #15342.  A bare latch cannot name its
# owner, so the only provenance those rows carry is the status message the SAME
# transaction wrote next to the latch.  Each marker is a verbatim substring of
# exactly one call site and of nothing else in the tree:
#   ``app.review_request_changes``       -- the first three
#   ``app.task_lifecycle_fix_completion`` -- the fourth
# The transient sibling of the fourth reads "review spawn deferred (concurrent
# spawn)" and never set a hold, so matching on "failed (" keeps it out.  This is
# provenance, NOT an age heuristic: a legacy row is recognised only when it
# carries no reason, no episode and no provenance of its own, so any hold minted
# through the seam -- by any owner -- is judged on its owner/reason instead.
_LEGACY_ESCALATION_MESSAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("Fix not spawned for PR #", "review_fix_spawn_refused"),
    ("but review spawn failed (", "review_spawn_failed"),
    (
        "withheld: could not prove the live immutable review target",
        "review_target_unavailable",
    ),
    (
        "its originating task is missing; fix withheld for manual recovery",
        "originating_task_missing",
    ),
)


def _legacy_pipeline_escalation_reason(task: Task) -> str | None:
    """Recognise a pre-provenance engine escalation by its co-written message."""

    context = dict(task.context or {})
    if (
        context.get(APPROVAL_MERGE_LEDGER_KEY) is not None
        or context.get(APPROVAL_MERGE_FACT_KEY) is not None
    ):
        return None
    message = str(task.latest_status_message or "")
    for marker, reason in _LEGACY_ESCALATION_MESSAGE_MARKERS:
        if marker in message:
            return reason
    return None


def pipeline_escalation_hold_reason(task: Task) -> str | None:
    """Return the releasable engine-escalation reason this row's hold carries.

    Fails CLOSED (``None``) for every hold this module cannot prove it owns: a
    manual or policy hold, a live operator follow-up claim, an in-flight merge
    intent, a review-wait timer, a row outside the two lanes that can mint
    these reasons, an owner or reason outside the pinned allowlists above, or
    an episode whose provenance disagrees with the context it was minted into.
    """

    if task.requires_approval is not True:
        return None
    task_type = (task.first_payload or {}).get("type")
    if task_type not in PIPELINE_ESCALATION_HOLD_TASK_TYPES:
        return None
    context = dict(task.context or {})
    if context.get(APPROVAL_FOLLOWUP_CLAIM_KEY) or approval_merge_is_in_flight(task):
        return None
    if approval_hold_provenance(task).get("review_wait_hold") is not None:
        return None
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    episode = context.get(APPROVAL_HOLD_EPISODE_KEY)
    reason = context.get("approval_hold_reason")
    if provenance is None and not episode and reason is None:
        return _legacy_pipeline_escalation_reason(task)
    if reason not in PIPELINE_ESCALATION_HOLD_REASONS:
        return None
    if not (
        isinstance(provenance, dict)
        and provenance.get("owner") in PIPELINE_ESCALATION_HOLD_OWNERS
        and provenance.get("reason") == reason
        and provenance.get("episode")
        and provenance.get("episode") == episode
    ):
        return None
    return str(reason)


def release_pipeline_escalation_approval_hold(
    task: Task,
    *,
    released_by: str,
) -> str | None:
    """Retract one engine escalation, leaving a durable receipt behind.

    Returns the released reason, or ``None`` when nothing was released.  The
    caller owns the lock and the commit.

    History is preserved, never destroyed: ``APPROVAL_HOLD_RELEASED_KEY``
    records the exact reason, owner, releaser, row status and note, so the
    escalation stays auditable after the latch is gone.  The tree-wide reader
    audit behind this change found no reader that needs the latch itself on a
    released row -- the two that come closest both fail to reach one:

    * ``app.rollup_management``'s cap-exhaustion escalation only inspects
      ``child.requires_approval`` for children it just tried to respawn, and a
      respawn candidate must own a parseable PULL_REQUEST artifact -- which is
      exactly the condition under which
      :func:`approval_decision_can_change_anything` refuses to release.
    * its ``unmerged`` filter does read ``not c.requires_approval``, and a
      released child does re-enter that list -- deliberately.  With no PR
      artifact it takes ``mark_unblocked_non_merge(reason=
      "rollup_no_pr_artifact")``, the same path every normally-completed
      review/fix child already takes, so the parent can finalize instead of
      waiting out ``evaluating_fail_hours`` behind a corpse.
    """

    reason = pipeline_escalation_hold_reason(task)
    if reason is None:
        return None
    provenance = (task.context or {}).get(APPROVAL_HOLD_PROVENANCE_KEY)
    owner = (
        str(provenance.get("owner"))
        if isinstance(provenance, dict) and provenance.get("owner")
        else None
    )
    consumed = consume_approval_hold_metadata(
        task,
        expected_episode=approval_hold_provenance(task),
    )
    context = dict(task.context or {})
    if not consumed:
        # Pre-provenance latch: there is no episode token to consume, so remove
        # exactly the keys an episode would have owned and nothing else.
        for key in (
            "_approval_set_at",
            "approval_hold_reason",
            APPROVAL_HOLD_EPISODE_KEY,
            APPROVAL_HOLD_PROVENANCE_KEY,
        ):
            context.pop(key, None)
    context[APPROVAL_HOLD_RELEASED_KEY] = {
        "reason": reason,
        "owner": owner,
        "released_by": str(released_by),
        "released_at": _utcnow().isoformat(),
        "task_status": _status_value(task.status),
        "note": _HOLD_RELEASE_NOTE,
    }
    task.context = context
    task.requires_approval = False
    return reason


def pipeline_escalation_hold_was_released(task: Task) -> bool:
    """Whether an engine escalation on this row was retracted as unactionable.

    The canonical reader for ``APPROVAL_HOLD_RELEASED_KEY``.  Nothing in
    ``backend/app`` consumes it today -- that is the audited conclusion above,
    not an oversight -- so any future reader that wants to distinguish "never
    held" from "held and retracted" should come through here rather than
    reaching into ``task.context``.
    """

    return isinstance((task.context or {}).get(APPROVAL_HOLD_RELEASED_KEY), dict)


async def settle_unactionable_pipeline_escalation(
    task: Task,
    session: AsyncSession,
    *,
    released_by: str,
) -> str | None:
    """Retract an engine escalation that landed where nobody can act on it.

    Returns the released reason, or ``None``.  Surfaces WHY on
    ``latest_status_message`` per the standing "gates must emit why they
    blocked/unblocked" rule; the caller owns the lock and the commit.
    """

    if await approval_decision_can_change_anything(task, session):
        return None
    reason = release_pipeline_escalation_approval_hold(
        task,
        released_by=released_by,
    )
    if reason is None:
        return None
    existing = str(task.latest_status_message or "").strip()
    if _HOLD_RELEASE_NOTE not in existing:
        suffix = f"{_HOLD_RELEASE_NOTE} ({reason})"
        task.latest_status_message = (f"{existing} - {suffix}" if existing else suffix)[
            :500
        ]
    return reason


def _claim_matches_task(task: Task, decision: ApprovalDecision) -> bool:
    """Cheap row-local half of exact approval follow-up authority."""

    claim = decision.claim
    current_claim = dict((task.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {})
    if claim is None or not current_claim:
        return False
    if decision.followup_kind == "review_spawn":
        claimed_target = approval_review_spawn_claim_target(decision)
        current_claim_target = approval_review_spawn_claim_target(
            decision, current_claim
        )
        source_target = approval_review_spawn_target(
            task,
            pr_number=decision.pr_number,
        )
        if (
            claimed_target is None
            or current_claim_target != claimed_target
            or source_target != claimed_target
        ):
            return False
    context = dict(task.context or {})
    review_loop = dict(context.get("review_loop") or {})
    try:
        current_review_pr = int(review_loop.get("pr_number"))
    except (TypeError, ValueError):
        current_review_pr = None
    return bool(
        task.id == decision.task_id
        and task.project_id == decision.project_id
        and task.requires_approval is True
        and task.archived is not True
        and _status_value(task.status) == _status_value(decision.status)
        and _status_value(task.status) == str(claim.get("status") or "")
        and current_claim.get("token") == claim.get("token")
        and current_claim.get("state") in {"pending", "merge_intent", "merge_submitted"}
        and current_claim.get("followup_kind") == decision.followup_kind
        and current_claim.get("pr_number") == claim.get("pr_number")
        and current_claim.get("artifact_id") == claim.get("artifact_id")
        and current_claim.get("artifact_pr_url") == claim.get("artifact_pr_url")
        and context.get("approval_hold_reason") == claim.get("approval_hold_reason")
        and context.get("touched_critical") == claim.get("touched_critical")
        and context.get("review_loop_terminal_outcome")
        == claim.get("review_loop_terminal_outcome")
        and context.get("rollup_pr_number") == claim.get("rollup_pr_number")
        and current_review_pr == claim.get("review_pr_number")
        and str(
            review_loop.get("ci_checked_head_sha") or review_loop.get("head_sha") or ""
        )
        == str(claim.get("head_sha") or "")
        and str(
            review_loop.get("ci_checked_base_sha") or review_loop.get("base_sha") or ""
        )
        == str(claim.get("base_sha") or "")
        and _claim_approval_episode_matches(task, claim)
    )


def _decision_from_pending_claim(
    task: Task, claim: Dict[str, Any], *, modified_by: str
) -> ApprovalDecision | None:
    """Rebuild the immutable receipt for an interrupted FALSE decision."""

    try:
        task_id = uuid.UUID(str(claim.get("task_id")))
        project_id = uuid.UUID(str(claim.get("project_id")))
        artifact_id = (
            uuid.UUID(str(claim.get("artifact_id")))
            if claim.get("artifact_id")
            else None
        )
        pr_number = int(claim.get("pr_number"))
    except (TypeError, ValueError, AttributeError):
        return None
    if claim.get("state") not in {"pending", "merge_intent", "merge_submitted"}:
        return None
    try:
        claim_status: TaskStatus | str = TaskStatus(str(claim.get("status") or ""))
    except ValueError:
        claim_status = str(claim.get("status") or "")
    decision = ApprovalDecision(
        task_id=task_id,
        project_id=project_id,
        desired=False,
        was_blocked=True,
        status=claim_status,
        modified_by=str(claim.get("modified_by") or modified_by),
        should_follow_up=True,
        followup_kind=str(claim.get("followup_kind") or "") or None,
        claim=dict(claim),
        pr_number=pr_number,
        rollup=claim.get("followup_kind") == "rollup_merge",
        artifact_id=artifact_id,
        artifact_pr_url=claim.get("artifact_pr_url"),
    )
    return decision if _claim_matches_task(task, decision) else None


async def _claim_matches_authoritative_task(
    task: Task,
    decision: ApprovalDecision,
    session: AsyncSession,
    *,
    allowed_states: frozenset[str] = frozenset(
        {"pending", "merge_intent", "merge_submitted"}
    ),
) -> bool:
    if not _claim_matches_task(task, decision):
        return False
    current_claim = dict((task.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {})
    if current_claim.get("state") not in allowed_states:
        return False
    if decision.rollup or decision.artifact_id is None:
        return True
    artifact = await _regular_pr_artifact(session, task.id)
    return bool(
        artifact is not None
        and artifact.id == decision.artifact_id
        and artifact.pr_url == decision.artifact_pr_url
        and _parse_pr_number(artifact.pr_url) == decision.pr_number
    )


def approval_followup_authority(
    decision: ApprovalDecision,
    *,
    allowed_states: frozenset[str] = frozenset({"pending"}),
) -> Callable[[AsyncSession], Awaitable[Task | None]]:
    """Build the Task-lock callback run only after canonical Project/PR locks."""

    async def _acquire(authority_session: AsyncSession) -> Task | None:
        authoritative_task = (
            (
                await authority_session.execute(
                    select(Task)
                    .where(Task.id == decision.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if authoritative_task is None or not await _claim_matches_authoritative_task(
            authoritative_task,
            decision,
            authority_session,
            allowed_states=allowed_states,
        ):
            return None
        return authoritative_task

    return _acquire


async def decide_operator_approval(
    task_id: uuid.UUID,
    desired: bool,
    session: AsyncSession,
    *,
    modified_by: str,
) -> tuple[Task, ApprovalDecision, Dict[str, Any]]:
    """Commit a fresh approval decision before any Project or GitHub I/O."""

    async with task_lock(session, task_id, skip_locked=False) as task:
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        desired = bool(desired)
        existing_claim = dict(
            (task.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {}
        )
        if not desired:
            recovered_merge = await recover_approval_merge_decision(task, session)
            if recovered_merge is not None:
                # Objective merge transport outlives its revocable hold.  A
                # duplicate operator request is the recovery entry even after
                # request-changes/replacement removed the old claim.
                await session.commit()
                return task, recovered_merge, {}
        if not desired and existing_claim:
            resumed = _decision_from_pending_claim(
                task, existing_claim, modified_by=modified_by
            )
            if resumed is not None and await _claim_matches_authoritative_task(
                task, resumed, session
            ):
                # A process may have died after the durable local decision but
                # before child/merge publication. Preserve and resume that same
                # exact claim instead of silently consuming operator approval.
                await session.commit()
                return task, resumed, {}

        was_blocked = task.requires_approval is True
        context = dict(task.context or {})
        rollup_pr_number = context.get("rollup_pr_number")
        rollup = rollup_pr_number is not None
        artifact = None if rollup else await _regular_pr_artifact(session, task.id)
        pr_number = (
            _parse_pr_number(str(rollup_pr_number))
            if rollup
            else _parse_pr_number(artifact.pr_url if artifact else None)
        )
        should_follow_up = bool(
            was_blocked
            and not desired
            and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
            and pr_number is not None
        )
        followup_kind = (
            "rollup_merge"
            if should_follow_up and rollup
            else (
                "critical_merge"
                if should_follow_up and context.get("touched_critical")
                else ("review_spawn" if should_follow_up else None)
            )
        )
        review_spawn_target = (
            approval_review_spawn_target(task, pr_number=pr_number)
            if followup_kind == "review_spawn"
            else None
        )
        if followup_kind == "review_spawn" and review_spawn_target is None:
            # An approval disposition is not authority over a mutable PR name.
            # Keep the exact existing hold byte-for-byte until a complete
            # project/PR/base/head target can be published by the lifecycle.
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot release approval: the review target has no complete "
                    "immutable project/PR/base/head identity"
                ),
            )

        revoke_operator_approval_followup(task)

        from app.closed_pr_reconciliation import revoke_review_wait_approval_hold

        provenance_revoked = revoke_review_wait_approval_hold(task)
        if was_blocked and not desired and task.spend_approval_cleared_at is None:
            task.spend_approval_cleared_at = _utcnow()

        context = dict(task.context or {})
        # A follow-up-required FALSE decision remains visibly approval-gated
        # until the child or merge result is durably published. That makes the
        # decision safe across process loss and scheduler retries.
        if should_follow_up:
            task.requires_approval = True  # approval-provenance: operator-followup-claim  # fmt: skip
        elif desired:
            establish_independent_approval_hold(
                task,
                reason="explicit operator approval hold",
                owner=str(modified_by or "operator"),
            )
            context = dict(task.context or {})
        else:
            consume_approval_hold_metadata(
                task,
                expected_episode=approval_hold_provenance(task),
            )
            task.requires_approval = False
        claim: Dict[str, Any] | None = None
        if should_follow_up:
            review_loop = dict(context.get("review_loop") or {})
            status_value = _status_value(task.status)
            try:
                review_pr_number = int(review_loop.get("pr_number"))
            except (TypeError, ValueError):
                review_pr_number = None
            token = str(uuid.uuid4())
            episode_set_at = _utcnow().isoformat()
            context["_approval_set_at"] = episode_set_at
            context[APPROVAL_HOLD_EPISODE_KEY] = token
            context[APPROVAL_HOLD_PROVENANCE_KEY] = {
                "episode": token,
                "owner": "operator_approval_followup",
                "reason": str(context.get("approval_hold_reason") or followup_kind),
                "set_at": episode_set_at,
            }
            task.context = context
            claim = {
                "token": token,
                "state": "pending",
                "task_id": str(task.id),
                "project_id": str(task.project_id),
                "modified_by": modified_by,
                "followup_kind": followup_kind,
                "status": status_value,
                "pr_number": pr_number,
                "rollup_pr_number": rollup_pr_number,
                "review_pr_number": review_pr_number,
                "head_sha": str(
                    (review_spawn_target or {}).get("head_sha")
                    or review_loop.get("ci_checked_head_sha")
                    or review_loop.get("head_sha")
                    or ""
                ),
                "base_sha": str(
                    (review_spawn_target or {}).get("base_sha")
                    or review_loop.get("ci_checked_base_sha")
                    or review_loop.get("base_sha")
                    or ""
                ),
                "artifact_id": str(artifact.id) if artifact else None,
                "artifact_pr_url": artifact.pr_url if artifact else None,
                "approval_hold_reason": context.get("approval_hold_reason"),
                "touched_critical": context.get("touched_critical"),
                "review_loop_terminal_outcome": context.get(
                    "review_loop_terminal_outcome"
                ),
                "approval_episode": approval_hold_provenance(task),
            }
            task.context = {**context, APPROVAL_FOLLOWUP_CLAIM_KEY: claim}

        changes: Dict[str, Any] = (
            {"approval_followup_pending": True}
            if should_follow_up
            else {"requires_approval": desired}
        )
        history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={
                "changes": changes,
                "modified_by": modified_by,
                "approval_decision": {
                    "token": claim.get("token") if claim else None,
                    "was_blocked": was_blocked,
                    "follow_up": should_follow_up,
                    "review_wait_provenance_revoked": provenance_revoked,
                },
            },
        )
        session.add(task)
        session.add(history)
        await session.commit()
        await session.refresh(task)

        decision = ApprovalDecision(
            task_id=task.id,
            project_id=task.project_id,
            desired=desired,
            was_blocked=was_blocked,
            status=task.status,
            modified_by=modified_by,
            should_follow_up=should_follow_up,
            followup_kind=followup_kind,
            claim=claim,
            pr_number=pr_number,
            rollup=rollup,
            artifact_id=artifact.id if artifact else None,
            artifact_pr_url=artifact.pr_url if artifact else None,
        )
        return task, decision, changes


async def _restore_refused_approval(
    decision: ApprovalDecision,
    session: AsyncSession,
    *,
    detail: str,
) -> None:
    """Re-arm only the still-current decision after a synchronous refusal."""

    async with task_lock(session, decision.task_id, skip_locked=False) as task:
        if task is None or not await _claim_matches_authoritative_task(
            task,
            decision,
            session,
            allowed_states=frozenset({"pending"}),
        ):
            await session.rollback()
            return
        establish_independent_approval_hold(
            task,
            reason=detail,
            owner="approval_followup_refused",
        )
        task.latest_status_message = detail[:500]
        session.add(task)
        session.add(
            TaskHistory(
                task_id=task.id,
                action="MODIFIED",
                details={
                    "changes": {"approval_followup_pending": False},
                    "modified_by": "APPROVAL_FOLLOWUP_REFUSED",
                    "approval_decision_token": (decision.claim or {}).get("token"),
                },
            )
        )
        await session.commit()


def revoke_operator_approval_followup(task: Task) -> bool:
    """Revoke an old operator disposition before an independent transition."""

    context = dict(task.context or {})
    claim = context.get(APPROVAL_FOLLOWUP_CLAIM_KEY)
    if not isinstance(claim, dict):
        return False
    context.pop(APPROVAL_FOLLOWUP_CLAIM_KEY, None)
    task.context = context
    consume_approval_hold_metadata(
        task,
        expected_episode=claim.get("approval_episode"),
    )
    return True


def approval_claim_state(task: Task, decision: ApprovalDecision) -> str | None:
    if not _claim_matches_task(task, decision):
        return None
    claim = dict((task.context or {}).get(APPROVAL_FOLLOWUP_CLAIM_KEY) or {})
    return str(claim.get("state") or "") or None


def approval_child_spawn_publication(
    decision: ApprovalDecision,
) -> Callable[[Task, AsyncSession], Awaitable[None]]:
    """Consume a pending decision in the atomic child-publication transaction."""

    async def _publish(task: Task, authority_session: AsyncSession) -> None:
        if (
            not _claim_matches_task(task, decision)
            or approval_claim_state(task, decision) != "pending"
        ):
            raise RuntimeError("approval claim changed before child publication")
        history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={
                "changes": {
                    "requires_approval": False,
                    "approval_followup_pending": False,
                },
                "modified_by": decision.modified_by,
                "approval_decision_token": (decision.claim or {}).get("token"),
            },
        )
        revoke_operator_approval_followup(task)
        task.requires_approval = False
        authority_session.add(task)
        authority_session.add(history)

    return _publish


async def consume_approval_followup_after_dedup(
    decision: ApprovalDecision,
    review_task_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """Consume only while a canonical exact-target DEDUP remains active."""

    from app.admin_approval_dedup_authority import (
        consume_approval_followup_after_dedup as _consume,
    )

    return await _consume(decision, review_task_id, session)


from app.admin_approval_merge_protocol import (  # noqa: E402,F401
    approval_merge_intent_writer,
    approval_merge_ledger,
    approval_merge_receipt,
    approval_merge_receipt_writer,
    approval_merge_submission_starter,
)
from app.admin_approval_merge_recovery import (  # noqa: E402,F401
    approval_merge_in_flight,
    reconcile_approval_merge_ledger,
    recover_approval_merge_decision,
)
