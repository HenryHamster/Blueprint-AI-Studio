"""Binding issue-ledger and exact-review proof gate for PR publication."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.issue_ledger import (
    build_issue_ledger,
    issue_ledger_complete_for_history,
    issue_ledger_lineage_proof_unsatisfiable,
)
from app.models import Task, _utcnow
from app.review_loop_convergence import REVIEW_LOOP_ABANDON_CANDIDATE_KEY

logger = logging.getLogger(__name__)

# The synthetic blocker raised when the durable ledger cannot prove it covers
# every review row.  Unlike every other entry in the open set it describes the
# bookkeeping, not the diff, so no commit fixes it and no reviewer refutes it.
LEDGER_PROOF_INCOMPLETE_ISSUE_ID = "BP-ledger-proof-incomplete"

# How many exact-target reviews this gate may spawn for ONE immutable
# ``(base_sha, head_sha)`` pair before it stops asking and hands the PR to an
# operator.
#
# Re-reviewing the same target is legitimate once or twice: the binding
# blocker set is accumulated across review rows, so a later reviewer can
# refute an earlier reviewer's finding and move it out of ``open``.  What is
# never legitimate is doing it without end.  With the head pinned, every spawn
# reads the identical diff; if this many independent adjudications of that
# diff have not cleared the ledger, the next one will not either, and the only
# thing still changing is the runner slot it occupies.
#
# The measured defect this bounds: PR #17693 took 441 exact-target review
# spawns over 9.5 hours -- one every ~66 seconds, continuously occupying one
# of three fleet runners -- and no cap fired.  The per-chain iteration cap
# counts actioned *generations*, and ``actioned_review_cap_tasks`` collapses
# every same-target review into a single generation, so ``chain_iterations``
# stayed at 1 forever.  That collapsing is correct for what the cap measures;
# it simply means the cap is not, and cannot be, the counter that bounds
# same-target respawn.  This is.
_EXACT_TARGET_REVIEW_SPAWN_BUDGET = 5

# Both live on ``review_loop``.  The target key is stored alongside the count
# so the budget is scoped to one immutable target rather than to the task: a
# fix commit or a base refresh mints a new key and restores the full budget,
# which is why a healthy loop can never reach the cap.
_SPAWN_BUDGET_TARGET_KEY = "authoritative_review_spawn_target"
_SPAWN_BUDGET_COUNT_KEY = "authoritative_review_spawns"


def review_target_matches_merge_identity(
    review_target: dict[str, str | int] | None,
    *,
    project_id: str,
    pr_number: int,
) -> bool:
    """Return whether a purported immutable target belongs to this merge."""
    if not isinstance(review_target, dict):
        return False
    try:
        target_pr_number = int(review_target.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        project_id
        and str(review_target.get("project_id") or "") == project_id
        and target_pr_number == int(pr_number)
        and str(review_target.get("base_sha") or "")
        and str(review_target.get("head_sha") or "")
    )


async def _enforce_authoritative_review_merge_gate(
    task: Task,
    session: AsyncSession,
    *,
    ci_loop: dict,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    live_review_target: dict[str, str | int] | None,
) -> bool:
    """Require empty binding blockers and latest exact-target APPROVE."""
    raw_history = ci_loop.get("review_history") or []
    history = raw_history if isinstance(raw_history, list) else []
    persisted_ledger = ci_loop.get("issue_ledger")
    try:
        ledger = build_issue_ledger(
            history,
            **(
                {"seed_ledger": persisted_ledger}
                if persisted_ledger is not None
                else {}
            ),
            stamp_complete=True,
        )
    except ValueError:
        ledger = build_issue_ledger(history, stamp_complete=True)
    ledger_proof_complete = issue_ledger_complete_for_history(ledger, history)
    if not ledger_proof_complete and len(history) >= REVIEW_HISTORY_MAX_ENTRIES:
        ledger["open"].append(
            {
                "issue_id": LEDGER_PROOF_INCOMPLETE_ISSUE_ID,
                "severity": "major",
                "description": "durable blocker ledger missing after history compaction",
            }
        )
    open_blockers = list(ledger.get("open") or [])

    # Every dictionary here is review evidence.  A trailing COMMENT-only,
    # feedback-only, or malformed row invalidates an older APPROVE.
    latest_review = next(
        (entry for entry in reversed(history) if isinstance(entry, dict)),
        {},
    )
    latest_is_approval = (
        str(latest_review.get("verdict") or "").upper() == "APPROVE"
    )
    approved_head = str(
        latest_review.get("reviewed_head_sha")
        or latest_review.get("head_sha")
        or ""
    )
    approved_base = str(
        latest_review.get("reviewed_base_sha")
        or latest_review.get("base_sha")
        or ""
    )
    live_head = str((live_review_target or {}).get("head_sha") or "")
    live_base = str((live_review_target or {}).get("base_sha") or "")
    project_id = str(task.project_id) if task.project_id else ""
    target_identity_is_exact = review_target_matches_merge_identity(
        live_review_target,
        project_id=project_id,
        pr_number=pr_number,
    )
    approval_is_exact = bool(
        latest_is_approval
        and latest_review.get("target_verified_by_backend") is True
        and str(latest_review.get("review_task_id") or "").strip()
        and ledger_proof_complete
        and target_identity_is_exact
        and approved_head
        and approved_base
        and approved_head == live_head
        and approved_base == live_base
    )
    if not open_blockers and approval_is_exact:
        for transient_key in (
            "authoritative_review_required",
            "authoritative_review_target_unresolved_at",
            "authoritative_review_task_id",
            "review_not_required",
            "review_policy_reason",
        ):
            ci_loop.pop(transient_key, None)
        ci_loop["review_approved"] = True
        ci_loop["review_target"] = dict(live_review_target or {})
        ci_loop["head_sha"] = live_head
        ci_loop["issue_ledger"] = ledger
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        return True

    from app.review_pipeline import _spawn_pipeline_task

    # Spawn helpers may commit/rollback and expire ORM instances, so snapshot
    # everything needed to reconstruct the fail-closed gate first.
    source_task_id = task.id
    source_context = dict(task.context or {})
    source_prompt = task.first_payload.get("prompt", "") or (task.description or "")
    gated_loop = dict(ci_loop)
    for stale_key in (
        "review_not_required",
        "review_policy_reason",
        "verdict",
        "authoritative_review_task_id",
    ):
        gated_loop.pop(stale_key, None)
    gated_loop["review_approved"] = False
    gated_loop["authoritative_review_required"] = True
    gated_loop["ci_waiting"] = True
    gated_loop["issue_ledger"] = ledger
    if not gated_loop.get("review_wait_started"):
        gated_loop["review_wait_started"] = _utcnow().isoformat()
    if target_identity_is_exact:
        gated_loop["authoritative_review_target"] = dict(live_review_target or {})
        gated_loop.pop("authoritative_review_target_unresolved_at", None)
    else:
        gated_loop.pop("authoritative_review_target", None)
        gated_loop["authoritative_review_target_unresolved_at"] = (
            _utcnow().isoformat()
        )
    task.context = {**source_context, "review_loop": gated_loop}
    flag_modified(task, "context")

    # A latched ledger cannot be argued with.  Once the durable proof is
    # unsatisfiable (see ``issue_ledger_lineage_proof_unsatisfiable``) the
    # synthetic ``BP-ledger-proof-incomplete`` blocker is re-minted on every
    # single pass, so spawning another exact-target review buys nothing: the
    # reviewer verifies the same clean delta, returns the same verdict, and the
    # gate refuses it again.  Hand the PR to an operator instead of burning
    # review budget against a gate that no review can satisfy, and record the
    # true reason so the stop is not later misread as the fix agent failing to
    # converge.  Fail-closed is unchanged -- nothing merges here either way.
    lineage_unsatisfiable = issue_ledger_lineage_proof_unsatisfiable(
        ledger, history
    )
    if lineage_unsatisfiable:
        gated_loop["ledger_lineage_unsatisfiable"] = True
        task.context = {
            **source_context,
            "review_loop": gated_loop,
            REVIEW_LOOP_ABANDON_CANDIDATE_KEY: {
                "status": "abandon_candidate",
                "reason": "ledger_lineage_unsatisfiable",
                "scope": "ledger-proof",
                "pr_number": pr_number,
                "detail": (
                    "The durable issue ledger is stamped complete=false with a "
                    "lineage marker while the retained review history is at its "
                    "cap, so build_issue_ledger can never restore the proof and "
                    f"{LEDGER_PROOF_INCOMPLETE_ISSUE_ID} is re-raised on every "
                    "merge-gate pass. No commit fixes it and no review refutes "
                    "it; an operator must adjudicate the ledger."
                ),
                "open_issue_ids": [
                    str(blocker.get("issue_id") or "")
                    for blocker in open_blockers
                    if isinstance(blocker, dict)
                ],
                "review_history_entries": len(history),
                "recorded_at": _utcnow().isoformat(),
            },
        }
        flag_modified(task, "context")
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: the durable issue ledger can no "
            "longer prove it covers every review row, so no review can clear "
            "the gate. Operator adjudication required."
        )
        session.add(task)
        logger.error(
            "[CI] Merge permanently held for PR #%s: issue-ledger lineage proof "
            "is unsatisfiable (%d retained review rows, %d open blocker(s)); "
            "suppressed the exact-target review respawn and escalated to "
            "operator adjudication",
            pr_number,
            len(history),
            len(open_blockers),
        )
        return False

    # Bound the exact-target respawn.  Keyed on the immutable target, so the
    # only thing this can ever stop is a loop in which nothing is moving:
    # any real forward motion mints a new key and restores the whole budget.
    spawn_target_key = f"{live_base}:{live_head}"
    try:
        prior_spawns = int(gated_loop.get(_SPAWN_BUDGET_COUNT_KEY) or 0)
    except (TypeError, ValueError):
        prior_spawns = 0
    if str(gated_loop.get(_SPAWN_BUDGET_TARGET_KEY) or "") != spawn_target_key:
        prior_spawns = 0
    if (
        target_identity_is_exact
        and prior_spawns >= _EXACT_TARGET_REVIEW_SPAWN_BUDGET
    ):
        gated_loop[_SPAWN_BUDGET_TARGET_KEY] = spawn_target_key
        gated_loop[_SPAWN_BUDGET_COUNT_KEY] = prior_spawns
        task.context = {
            **source_context,
            "review_loop": gated_loop,
            REVIEW_LOOP_ABANDON_CANDIDATE_KEY: {
                "status": "abandon_candidate",
                "reason": "exact_target_review_budget_exhausted",
                "scope": "exact-target-review",
                "pr_number": pr_number,
                "detail": (
                    f"The merge gate has already spawned {prior_spawns} "
                    "exact-target review(s) for the immutable target "
                    f"{live_base[:8]}..{live_head[:8]} and the binding blocker "
                    "ledger is still not clear. The head has not moved, so "
                    "every further review reads the identical diff and can "
                    "only re-derive the same verdict. An operator must "
                    "adjudicate the open blockers, or a fix must land a "
                    "commit -- which mints a new target and restores the "
                    "budget on its own."
                ),
                "open_issue_ids": [
                    str(blocker.get("issue_id") or "")
                    for blocker in open_blockers
                    if isinstance(blocker, dict)
                ],
                "review_history_entries": len(history),
                "spawned_reviews": prior_spawns,
                "review_target": {
                    "base_sha": live_base,
                    "head_sha": live_head,
                },
                "recorded_at": _utcnow().isoformat(),
            },
        }
        flag_modified(task, "context")
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: {prior_spawns} exact-target "
            "reviews of an unchanged head have not cleared "
            f"{len(open_blockers)} binding blocker(s); no further review can "
            "read a different diff. Operator adjudication required."
        )
        session.add(task)
        logger.error(
            "[CI] Merge held for PR #%s: exact-target review budget exhausted "
            "(%d spawns at %s..%s, %d open blocker(s)); suppressed the "
            "respawn and escalated to operator adjudication",
            pr_number,
            prior_spawns,
            live_base[:8],
            live_head[:8],
            len(open_blockers),
        )
        return False

    review_task_id = ""
    if target_identity_is_exact:
        review_task, _created = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=source_prompt,
            original_task_id=original_task_id,
            review_target=live_review_target,
        )
        if review_task is not None:
            review_task_id = str(review_task.id)
        if _created:
            # Only a genuinely NEW task spends budget. While one spawned
            # review is still in flight the gate keeps calling this on every
            # CI tick and ``_spawn_pipeline_task`` hands back the same active
            # row; charging those would burn the whole budget in five minutes
            # against a single review that had not even reported yet.
            gated_loop[_SPAWN_BUDGET_TARGET_KEY] = spawn_target_key
            gated_loop[_SPAWN_BUDGET_COUNT_KEY] = prior_spawns + 1

    post_spawn_context = task.__dict__.get("context")
    refreshed_task = (
        task
        if isinstance(post_spawn_context, dict)
        else await session.get(Task, source_task_id, populate_existing=True)
    )
    if refreshed_task is None:
        logger.error(
            "[CI] Merge held for PR #%s but source task %s disappeared after "
            "the authoritative review spawn attempt",
            pr_number,
            source_task_id,
        )
        return False

    refreshed_context = dict(refreshed_task.context or {})
    refreshed_loop = dict(refreshed_context.get("review_loop") or {})
    refreshed_loop.update(gated_loop)
    for stale_key in ("verdict", "review_not_required", "review_policy_reason"):
        refreshed_loop.pop(stale_key, None)
    if review_task_id:
        refreshed_loop["authoritative_review_task_id"] = review_task_id
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            f"require exact-target review {review_task_id}"
        )
    elif not target_identity_is_exact:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_loop.pop("authoritative_review_target", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: could not resolve the live "
            "review target; retrying before merge"
        )
    else:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            "require an exact-target review"
        )
    refreshed_task.context = {
        **refreshed_context,
        "review_loop": refreshed_loop,
    }
    flag_modified(refreshed_task, "context")
    session.add(refreshed_task)
    logger.warning(
        "[CI] Merge held for PR #%s: %d authoritative blocker(s), "
        "exact approval=%s; exact-target review required",
        pr_number,
        len(open_blockers),
        approval_is_exact,
    )
    return False
