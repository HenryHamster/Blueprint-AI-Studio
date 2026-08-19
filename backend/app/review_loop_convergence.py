"""Review<->fix outer-loop convergence machinery.

Extracted from review_pipeline.py to keep that module below its size cap.
Holds the scope-pressure / narrow-focus threshold constants and prompt
prefixes, the pipeline-task staleness check, and the terminal
cap-exhaustion handler.

These names are re-exported from app.review_pipeline so every existing
``from app.review_pipeline import X`` caller and test keeps working
unchanged. This module imports nothing from review_pipeline, so the
re-export is acyclic.

``CROSS_EPISODE_ABANDON_THRESHOLD`` is intentionally an intra-PR-only census.
Project-lifetime recurrence across distinct merged PRs is tracked by
``ModuleFindingChain`` and escalated from the REQUEST_CHANGES path.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.failure_classification import INFRA_ANALYTICS_CLASSES
from app.models import Task, TaskStatus
from app.review_heuristics import actioned_review_cap_tasks

# MAJOR 5: the DEFEAT for the shared-VmContact liveness override (see
# _is_pipeline_task_stale). reaper_orphan_classify depends only on
# failure_classification / models / schemas / shared_state / time_utils — it does
# not import review_pipeline or this module, so this import stays acyclic.
from app.reaper_orphan_classify import _runner_report_excludes_task
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Keep the PR-level census aligned with the canonical rollup review budget.
# A smaller value preempts the rollup's 16-cycle cap and leaves no recovery
# path for work that is still within the budget.
CROSS_EPISODE_ABANDON_THRESHOLD = 16
REVIEW_LOOP_ABANDON_CANDIDATE_KEY = "review_loop_abandon_candidate"


def build_cross_episode_abandon_candidate(
    tasks: Iterable[Task],
    *,
    threshold: int = CROSS_EPISODE_ABANDON_THRESHOLD,
    actionable_tasks: Iterable[Task] | None = None,
) -> dict[str, object] | None:
    """Build the durable PR-level abandon signal from consumed pipeline tasks.

    The caller supplies rows for one project+PR, without filtering by chain or
    episode.  A cancelled review/fix is an attempt even though it never appears
    in the ordinary cap query; archived rows remain evidence here too. Raw rows
    remain visible in the attempt/cancellation totals, but only exact
    actioned-or-discarded patch/open-set generations control the stop:

    * an actioned generation (an exact REQUEST_CHANGES review + its fix, or a
      mechanical conflict/CI fix) collapses via ``actioned_review_cap_tasks``,
      same as the ordinary per-chain/lane cap; and
    * a supersede/retarget-discarded review or fix (``_review_stale_head`` /
      ``_review_stale_target``) still ran a real agent that produced a verdict
      against an exact (base, head) pair before a supersede/retarget moved the
      live target out from under it. The ordinary per-chain/lane cap correctly
      excludes it (a chain must not be penalised for a verdict it couldn't
      act on), but that exclusion is exactly the gap a supersede/retarget
      cycle could otherwise launder its history through here: this
      PR-lifetime census collapses those discarded rows into generations the
      same way, so they add to — not reset — the accumulated burden.

    Carried iteration numbers and parallel observation fan-out are not work
    receipts and cannot trigger this signal by themselves. A *manual* cap
    reset is a separate, deliberate path: the caller filters rows by
    ``iteration_reset_at`` before ever calling in here, so an explicit
    operator reset legitimately starts a fresh census window — this function
    has no opinion on, and does not defeat, that filtering.
    """
    if threshold < 1:
        raise ValueError("abandon threshold must be positive")

    attempts = [
        task for task in tasks if task.first_payload.get("type") in ("review", "fix")
    ]
    if not attempts:
        return None

    generation_source = attempts if actionable_tasks is None else list(actionable_tasks)
    actioned = actioned_review_cap_tasks(generation_source)

    # Supersede/retarget-discarded rows never reach ``generation_source``
    # above (``task_counts_toward_review_cap`` — the default filter behind
    # ``actionable_tasks`` — excludes any ``_review_stale_head`` /
    # ``_review_stale_target`` row), so nothing here can double-count a row
    # already present in ``actioned``. Collapse them into generations the
    # same way, using the raw, unfiltered ``attempts`` as the source so a
    # caller that passes a pre-filtered ``actionable_tasks`` still sees its
    # discards counted.
    #
    # ADMIN_CANCEL (and the rest of INFRA_ANALYTICS_CLASSES) is excluded here
    # exactly as it is for the ordinary cap: an admin-cancel supersede tears
    # down a task before/without letting its agent produce a verdict, so it
    # never ran review/fix work to discard in the first place. Counting it
    # would charge the PR for an operator's housekeeping cancel, not for
    # burned agent work — the one thing this census exists to measure.
    discarded_candidates = [
        task
        for task in attempts
        if (
            (task.context or {}).get("_review_stale_head")
            or (task.context or {}).get("_review_stale_target")
        )
        and not (
            task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and (task.context or {}).get("failure_category") in INFRA_ANALYTICS_CLASSES
        )
    ]
    discarded = actioned_review_cap_tasks(
        discarded_candidates, count_stale_discards=True
    )
    cycles = len(actioned) + len(discarded)
    burden = max(len(attempts), cycles)
    if cycles < threshold:
        return None

    episode_ids: set[str] = set()
    for task in attempts:
        loop = (task.context or {}).get("review_loop")
        loop = loop if isinstance(loop, dict) else {}
        episode_ids.add(
            str(
                loop.get("original_task_id")
                or task.first_payload.get("original_task_id")
                or task.id
            )
        )
    cancelled = sum(1 for task in attempts if task.status == TaskStatus.CANCELLED)
    return {
        "status": "abandon_candidate",
        "threshold": threshold,
        "attempts": len(attempts),
        "cycles": cycles,
        "burden": burden,
        "cancelled_attempts": cancelled,
        "episodes": len(episode_ids),
    }


def build_review_cap_abandon_candidate(
    tasks: Iterable[Task],
    *,
    actioned_generations: int,
    threshold: int,
    scope: str,
    lane_counts: dict[str, int] | None = None,
    reason: str = "review_cap",
    detail: str = "",
) -> dict[str, object]:
    """Build the existing operator-disposition marker for a bounded cap stop.

    Unlike :func:`build_cross_episode_abandon_candidate`, the caller has
    already collapsed raw worker rows into exact actioned patch/open-set
    generations.  Preserve the raw attempt count for burn visibility while
    making the controlling ``cycles``/``burden`` fields reflect the unit that
    actually exhausted policy.
    """

    attempts = [
        task for task in tasks if task.first_payload.get("type") in ("review", "fix")
    ]
    episode_ids: set[str] = set()
    for task in attempts:
        loop = (task.context or {}).get("review_loop")
        loop = loop if isinstance(loop, dict) else {}
        episode_ids.add(
            str(
                loop.get("original_task_id")
                or task.first_payload.get("original_task_id")
                or task.id
            )
        )
    cancelled = sum(1 for task in attempts if task.status == TaskStatus.CANCELLED)
    return {
        "status": "abandon_candidate",
        "reason": reason,
        "threshold": int(threshold),
        "attempts": len(attempts),
        "cycles": int(actioned_generations),
        "actioned_generations": int(actioned_generations),
        "burden": max(len(attempts), int(actioned_generations)),
        "cancelled_attempts": cancelled,
        "episodes": len(episode_ids),
        "scope": scope,
        "lane_counts": dict(lane_counts or {}),
        **({"detail": detail} if detail else {}),
    }

# Liveness backstop on the cap-exhaustion preservability GET — a bound on a
# WEDGED coroutine, NOT a competitor to the GitHub client's own retry budget.
#
# ⚠ READ THIS BEFORE "TIGHTENING" THE NUMBER. A 45s value (round 2) was wrong,
# and the arithmetic first used to justify 420s was ALSO wrong — right answer,
# wrong reason (round 3). The corrected derivation:
#
#   max_retries = 3 (github.py:408), httpx timeout = 15s (github.py:66).
#   A RATE LIMIT DOES NOT SLEEP HERE. ``_github_request`` FAST-FAILS it:
#     • a 429/403 whose reset is > RATE_LIMIT_FAST_FAIL_THRESHOLD_S (5s) out
#       returns at attempt 0 (github.py ~:536-540), and
#     • a known-empty bucket returns a SYNTHETIC 403 with ZERO network
#       (github.py ~:471-486).
#   So the retry sleeps are NOT reachable for a hard 429/403, and the old
#   "3*(15+120) + 2*5 = 415s" figure was unreachable for the very case it was
#   meant to describe. The 120s proactive ``sleep(min(wait, 120))`` is only
#   reachable on a 5xx that happens to carry X-RateLimit-Remaining < 10.
#   True worst case ≈ 3 * (15 + 120) + jittered 5xx backoffs ≈ 408s.
#
# 420s is therefore still ABOVE the client's own worst case (so this can only
# fire on a genuinely wedged socket, never on a retry the client would have won)
# — but it is emphatically NOT the thing that protects a rate-limit window.
# THAT is done by classification, not by the deadline: a rate-limited lookup is
# raised as ``_PreservabilityUnavailable`` and DEFERS (see
# ``_pr_is_preservable_on_cap``). A timeout must never close a PR or fail a task,
# and neither must a 429.
#
# There is no pressure to make this tight: the caller has ALREADY COMMITTED and
# released both the per-PR advisory lock and the ``orig`` row lock before this
# GET, so the 60s idle_in_transaction_session_timeout does not apply to it.
#
# NOTE (outer bound, not ours): on the agent-status path this whole handler runs
# inside ``asyncio.wait_for(..., timeout=240)`` (routers/agent_status.py ~:4065).
# That cancels with CancelledError — a BaseException, caught by neither except
# below — so it propagates and sets the post-commit-retry flag (non-destructive,
# and the cap path is idempotent). It does mean the effective ceiling on THAT
# path is 240s, not 420s. Kept deliberately: shrinking this constant to 240 would
# buy nothing (the outer bound already fires first) while re-introducing a tight
# deadline on every OTHER caller, which is what made 45s dangerous.
_PRESERVABILITY_GET_TIMEOUT_S = 420.0

# A DEFER is a PARK, and an uncounted park is a silent wedge. Re-entry into the
# cap handler is EVENT-DRIVEN (``_spawn_pipeline_task`` is driven by CI /
# review-verdict completions), NOT timer-swept — so a transient preservability
# failure does NOT self-heal on a quiet PR: with no further event the cap
# transition simply never happens and the task sits EVALUATING until the
# hours-later stall reaper. Count the defers on ``orig`` and raise an operator
# alert past the threshold. The transition stays deferred either way — alerting is
# the escalation, NOT closing the PR. Never trade a silent park for a destructive
# one.
#
# The count is LIFETIME, not a consecutive streak: the key is deliberately in
# NEITHER ``rollup_state._ROLLUP_TRANSIENT_CTX_KEYS`` nor
# ``FRESH_ATTEMPT_CONTEXT_STRIP_KEYS``, and no success path clears it. A counter
# that reset on every good lookup would hide a FLAPPING GitHub budget — which is
# exactly the condition worth alerting on. The log line and the alert body both say
# "lifetime" so it is never misread as a streak.
_CAP_DEFER_COUNT_KEY = "_cap_defer_count"
_CAP_DEFER_ALERT_AFTER = 3


class _PreservabilityUnavailable(Exception):
    """We could not determine the PR's state, for a reason that will pass.

    Raised for the two conditions that say nothing whatsoever about the PR:

      • DEADLINE BREACH — we stopped waiting. That is a statement about US.
      • RATE LIMIT (429 / 403 + rate-limit headers, including the client's
        synthetic 403 for a known-empty bucket) — we are out of GitHub budget
        until the reset. That is a statement about our QUOTA.

    Both are recoverable and both are TRANSIENT. Neither is evidence that the PR
    is unsalvageable, so neither may take the destructive path.

    Contrast a genuine, *informative* failure — a 404/410 (PR gone), a permission
    403, a persistent 5xx — where the GitHub client really did exhaust its budget
    and we honestly know no more. Those keep the pre-existing fail-safe semantics
    (rollup → close+FAIL, back-merge resolver → preserve).

    WHY THE RATE-LIMIT CASE IS THE WHOLE POINT: ``_github_request`` does NOT sleep
    through a hard rate limit — it FAST-FAILS (github.py ~:536-540) and, for a
    known-empty bucket, short-circuits to a synthetic 403 with zero network
    (~:471-486). So during an install-token-budget exhaustion window — the failure
    that has taken production down twice — ``get_pull_request`` returns in ~0-15s,
    every cap-exhausted rollup lands in the lookup-failure branch at once, and the
    old code closed EVERY ONE of their PRs and FAILED their tasks with CODE_ERROR.
    No deadline can protect against that, because nothing ever hangs: the fix has
    to be CLASSIFICATION. Raising this instead makes the caller defer the whole
    terminal transition — PR stays open, task stays non-terminal, and the
    idempotent cap path retries once the budget resets.
    """


_REVIEW_LOOP_TERMINAL_OUTCOME_KEY = "review_loop_terminal_outcome"


def _approval_hold_episode_identity(task: Task) -> dict[str, object] | None:
    """Return the non-recursive identity of the current approval episode."""

    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
    )

    context = dict(task.context or {})
    provenance = context.get(APPROVAL_HOLD_PROVENANCE_KEY)
    episode = context.get(APPROVAL_HOLD_EPISODE_KEY)
    set_at = context.get("_approval_set_at")
    if not episode or not set_at or not isinstance(provenance, dict):
        return None
    return {
        "episode": episode,
        "set_at": set_at,
        "reason": context.get("approval_hold_reason"),
        "provenance": dict(provenance),
    }


def _set_review_loop_terminal_outcome(
    task: Task,
    *,
    kind: str,
    reason: str,
    pr_number: int,
    iterations: int,
    scope: str | None = None,
    detail: str | None = None,
    head_sha: str | None = None,
) -> None:
    """Persist the chain-original terminal outcome in task.context.

    This is purely a structured breadcrumb for downstream aggregation. It does
    not change the existing alerting, gating, or close-comment behavior.

    ``head_sha`` (stuck/standoff escalations only) records the exact PR head
    the stuck-cycle detector compared when it parked the chain. Without it, a
    park recorded before a later commit lands (e.g. a fix that pushed real
    work but was cancelled by an unrelated infra event before its own review
    ran) is indistinguishable from a park against the still-current head, and
    every subsequent respawn attempt keeps discarding into the same stale
    park forever. See :func:`review_loop_park_stale_head`.
    """
    from sqlalchemy.orm.attributes import flag_modified

    ctx = dict(task.context or {})
    terminal_outcome: dict[str, object] = {
        "kind": kind,
        "reason": reason,
        "pr_number": pr_number,
        "iterations": iterations,
    }
    if scope is not None:
        terminal_outcome["scope"] = scope
    if detail is not None:
        terminal_outcome["detail"] = detail
    if head_sha:
        terminal_outcome["head_sha"] = head_sha
    terminal_outcome["recorded_at"] = _utcnow().isoformat()
    if task.requires_approval:
        # Bind a resolvable escalation breadcrumb to the exact approval episode
        # it created.  The boolean latch can remain TRUE across a later manual
        # or policy hold, so the breadcrumb alone is never release authority.
        hold_episode = _approval_hold_episode_identity(task)
        hold_owner = (
            (hold_episode.get("provenance") or {}).get("owner")
            if isinstance(hold_episode, dict)
            else None
        )
        hold_reason = (
            (hold_episode.get("provenance") or {}).get("reason")
            if isinstance(hold_episode, dict)
            else None
        )
        if (
            hold_episode is not None
            and hold_owner == "review_pipeline"
            and hold_reason == kind
        ):
            terminal_outcome["approval_hold_episode"] = hold_episode
    ctx[_REVIEW_LOOP_TERMINAL_OUTCOME_KEY] = terminal_outcome
    task.context = ctx
    flag_modified(task, "context")


async def _create_handmerge_attention_message(
    task: Task,
    pr_number: int,
    iterations: int,
    cap_label: str,
    session: AsyncSession,
) -> None:
    """Create a durable operator-attention ProjectMessage for a preserved hand-merge PR.

    Idempotent: a message keyed to the task is only inserted if one does not
    already exist (safe on retry).  The message is committed independently so
    it persists even if the caller's surrounding try/except swallows errors.
    The sweep ``sweep_closed_handmerge_prs`` resolves this message automatically
    when it detects the PR has been merged or closed.
    """
    from app.message_audience import stamp_message_audience
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message
    from sqlmodel import select as _sqlm_select

    ikey = f"handmerge:{task.id}"
    existing = (
        await session.execute(
            _sqlm_select(ProjectMessage).where(
                ProjectMessage.project_id == task.project_id,
                ProjectMessage.idempotency_key == ikey,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        await build_project_message(
            session,
            project_id=task.project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            from_task_id=task.id,
            content=(
                f"PR #{pr_number} preserved for hand-merge — review-fix cap reached "
                f"({iterations} iterations, {cap_label}); PR left open with mergeable "
                f"work awaiting manual merge"
            ),
            context=stamp_message_audience(
                {
                    "pr_number": pr_number,
                    "task_id": str(task.id),
                },
                "handmerge_pr_preserved",
            ),
            idempotency_key=ikey,
        )
    )
    await session.commit()


# Escalation kinds that flag the chain original ``requires_approval`` while the
# review-fix loop KEEPS RUNNING (stuck cycle / standoff / repeat-fingerprint
# halt). These holds exist to point an operator at a loop that looks like it is
# not converging — but the loop is not stopped by them, so when it demonstrably
# converges anyway (a later review APPROVEs, or the PR merges) the hold is
# stale and must be released. Left in place, the recovery sweep's 2h
# approval-gate timeout FAILs a healthy parent out from under its own in-flight
# fix chain (research 6506582b / PR #9179, 2026-06-09). ``cap_exhausted`` is
# deliberately NOT in this set: that path closes the PR and terminalizes the
# chain, so nothing can legitimately converge afterwards.
_RESOLVABLE_ESCALATION_GATE_KINDS = frozenset(
    {"stuck", "standoff", "repeat_fingerprint_halt", "supersession_churn_halt"}
)


def clear_review_escalation_approval_gate(orig: Task, *, resolved_by: str) -> bool:
    """Release a stale stuck/standoff/repeat-fingerprint/supersession hold.

    Pure mutation (mirrors :func:`_set_review_loop_terminal_outcome`): the
    caller owns the row lock and the commit. Only touches holds whose recorded
    terminal-outcome breadcrumb is one of the resolvable escalation kinds —
    spend/budget holds, branch-safety holds, and any hold without a matching
    breadcrumb are left untouched. Returns ``True`` iff the gate was released.
    """
    from sqlalchemy.orm.attributes import flag_modified

    if not orig.requires_approval:
        return False
    ctx = dict(orig.context or {})
    from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON

    if ctx.get("approval_hold_reason") == SPEND_BUDGET_APPROVAL_HOLD_REASON:
        return False
    outcome = ctx.get(_REVIEW_LOOP_TERMINAL_OUTCOME_KEY) or {}
    if outcome.get("kind") not in _RESOLVABLE_ESCALATION_GATE_KINDS:
        return False
    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        revoke_operator_approval_followup,
    )

    expected_hold = outcome.get("approval_hold_episode")
    current_hold = _approval_hold_episode_identity(orig)
    if isinstance(expected_hold, dict):
        if current_hold != expected_hold:
            return False
    else:
        # Rolling compatibility for old escalation rows: they remain clearable
        # only while no newer-code approval episode exists and no independent
        # reason displaced the old breadcrumb.  Once provenance exists, fail
        # closed because an unbound legacy breadcrumb cannot name that episode.
        if current_hold is not None or ctx.get(APPROVAL_HOLD_PROVENANCE_KEY):
            return False
        legacy_reason = ctx.get("approval_hold_reason")
        if legacy_reason not in (
            None,
            outcome.get("kind"),
            outcome.get("reason"),
        ):
            return False
    orig.requires_approval = False
    from app.closed_pr_reconciliation import revoke_review_wait_approval_hold

    revoke_review_wait_approval_hold(orig)
    revoke_operator_approval_followup(orig)
    ctx = dict(orig.context or {})
    # Consume every piece of metadata owned by this exact episode. A later,
    # unrelated hold must neither inherit the elapsed timer nor be mistaken for
    # this escalation on a subsequent convergence pass.
    ctx.pop("_approval_set_at", None)
    ctx.pop("approval_hold_reason", None)
    ctx.pop(APPROVAL_HOLD_EPISODE_KEY, None)
    ctx.pop(APPROVAL_HOLD_PROVENANCE_KEY, None)
    ctx[_REVIEW_LOOP_TERMINAL_OUTCOME_KEY] = {
        **outcome,
        "resolved": True,
        "resolved_by": resolved_by,
        "resolved_at": _utcnow().isoformat(),
    }
    orig.context = ctx
    flag_modified(orig, "context")
    logger.info(
        "[PIPELINE] Released %s approval hold on %s (%s)",
        outcome.get("kind"),
        str(orig.id)[:8],
        resolved_by,
    )
    return True


def review_loop_parked_for_operator(orig: Task | None) -> str | None:
    """Return the operator-park reason for this chain root, or ``None``.

    A chain root is "parked" when the stuck-cycle/standoff detector recorded a
    review-loop terminal outcome of kind ``stuck``/``standoff`` via
    :func:`_set_review_loop_terminal_outcome` and no later convergence released
    the hold (:func:`clear_review_escalation_approval_gate` stamps
    ``resolved=True`` when a later APPROVE or merge proves the loop healthy).
    Parked means an operator owns the next move: sweeps must NOT respawn
    reviews for the chain's PR, and cap exhaustion must NOT close the PR the
    escalation deliberately preserved.

    Incident this exists for (lineage d3c12745 / PR #16759, 2026-08-06): after
    the stuck escalation parked the chain, every rollup sweep kept respawning a
    review (the prior one had COMPLETED, freeing its idempotency key), each
    respawn re-hit the stuck gate, the recount ratcheted pr_iterations to the
    global cap, and the cap handler auto-closed the PR the park had deliberately
    preserved — destroying a real landed fix across 19 burned review tasks.

    Deliberately narrow: only ``stuck``/``standoff`` park. ``cap_exhausted``
    terminalizes the chain through its own path, and the remaining resolvable
    escalation kinds (repeat_fingerprint_halt / supersession_churn_halt) gate
    their own respawn sites. The breadcrumb — not ``requires_approval`` — is
    the signal: the flag is discretionary (require_approval_gates) and can
    belong to unrelated holds (spend budget, branch safety), while the
    breadcrumb is written on every stuck/standoff escalation regardless of
    gate configuration.
    """
    if orig is None:
        return None
    context = orig.context or {}
    abandon_candidate = context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY)
    if isinstance(abandon_candidate, dict):
        # The existing reset-iteration-cap operation removes this marker and
        # starts a new fenced census window. Until that explicit disposition,
        # the PR remains operator-owned and no cap/stale-review path may resume
        # or close it.
        return str(abandon_candidate.get("reason") or "abandon_candidate")
    outcome = context.get(_REVIEW_LOOP_TERMINAL_OUTCOME_KEY)
    if not isinstance(outcome, dict) or outcome.get("resolved"):
        return None
    kind = outcome.get("kind")
    if kind in ("stuck", "standoff"):
        return str(kind)
    return None


def review_loop_park_stale_head(orig: Task | None, live_head_sha: str) -> bool:
    """Return whether an active stuck/standoff park predates ``live_head_sha``.

    Defect this exists for: a fix task pushed a real commit and passed its
    local publication gate, then was CANCELLED by an unrelated infra event
    (e.g. a heartbeat 4xx) before its own review ever ran. The commit landed
    on the branch, but the park the stuck-cycle detector recorded compared
    only reviews of the OLD head — it never saw the new one. Left alone, every
    later spawn/respawn attempt (including an admin "all children terminal,
    re-triggering rollup" repair pass) keeps hitting
    :func:`review_loop_parked_for_operator` and discarding without ever
    looking at the commits that already landed.

    Pure / read-only, mirroring :func:`review_loop_parked_for_operator`: it
    does not release the hold itself (see
    :func:`clear_review_escalation_approval_gate` for that), it only answers
    whether the currently active park is stale relative to a freshly-fetched
    live PR head. Returns ``False`` (never stale) when there is no active
    stuck/standoff park, or when the park predates ``head_sha`` tracking (a
    legacy row with no recorded ``head_sha``) — a park without provenance
    cannot be proven stale, so it fails closed and stays parked.
    """
    if orig is None or not live_head_sha:
        return False
    context = orig.context or {}
    if isinstance(context.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY), dict):
        # A cross-episode abandon candidate is a distinct, operator-scoped
        # marker (cleared only by the explicit reset-iteration-cap
        # operation) — not the stuck/standoff head-comparison this helper is
        # about. Treat it as not-a-stale-stuck-park; it fails closed via its
        # own dedicated release path.
        return False
    outcome = context.get(_REVIEW_LOOP_TERMINAL_OUTCOME_KEY)
    if not isinstance(outcome, dict) or outcome.get("resolved"):
        return False
    if outcome.get("kind") not in ("stuck", "standoff"):
        return False
    parked_head_sha = str(outcome.get("head_sha") or "")
    return bool(parked_head_sha) and parked_head_sha != live_head_sha


_CHURN_NARROW_FOCUS_PREFIX = (
    "⚠️ ISSUE CHURN DETECTED: the prior {window} fix iterations resolved some "
    "issues but introduced new ones (CRITICAL+MAJOR counts: {counts}). "
    "Prefer addressing only the listed issues; avoid incidental refactoring "
    "or cleanup. If the same issue keeps recurring, the fix you've been "
    "applying is probably wrong — don't apply it harder, investigate. Two "
    "common patterns: (1) the planned file set was wrong and the fix "
    "genuinely needs files outside scope (a sibling cluster's dep) — expand "
    "scope, explain why in the PR description, stop rewriting the same file "
    "differently each iteration; (2) review asks for a literal revert — run "
    "`git checkout origin/<base_branch> -- <file>` rather than rewriting "
    "from memory, which drifts each iteration and never converges.\n\n"
)

# Softer proactive directive injected when a fix iteration has consumed at
# least half the per-chain iteration cap but churn has not (yet) been
# detected. Unlike the churn prefix, this does NOT reduce the cap — it is
# guidance only, aimed at preventing churn before it begins.
MID_BUDGET_NARROW_FOCUS_THRESHOLD = 0.5
_MID_BUDGET_NARROW_FOCUS_PREFIX = (
    "⚠️ You have used over half of the fix iteration budget "
    "({iteration}/{cap}). Focus on the specific issues listed in the review "
    "feedback. Avoid modifying files not referenced by the issues. Defer "
    "cleanups and refactors to follow-up tasks.\n\n"
)

# Light-touch informational prefix injected from iteration 2 onward whenever
# repeating CRITICAL/MAJOR issues are observed across prior review iterations.
# No cap reduction, no narrow-focus urgency — just surfaces the pattern so fix
# agents have early visibility before churn or mid-budget thresholds trigger.
_EARLY_REPEATING_ISSUES_PREFIX = (
    "Note: some issues have appeared in multiple prior review iterations. "
    "Consider prioritizing a durable fix for these recurring patterns.\n\n"
)

# Utilization at/above this fraction of the scope file budget signals the fix
# agent to prefer minimal fixes over expansive refactoring — gives headroom
# before a fix iteration would tip the PR into scope-overflow territory.
SCOPE_PRESSURE_WARN_THRESHOLD = 0.7

# Graduated iteration cap: once a medium-scope PR crosses
# SCOPE_PRESSURE_WARN_THRESHOLD (70%) of its production-file budget, the
# remaining review-fix iterations are scaled down by how close the PR is to
# full budget. At 70% utilization the fix agent keeps ~30% of its remaining
# budget; at 90% only ~10% (floor of 2); at 95%+ the floor of 2 dominates.
# The no-new-files prompt stays gated on the stricter
# SCOPE_PRESSURE_HARD_CAP_PERCENTAGE (90%) level.
SCOPE_PRESSURE_HARD_CAP_PERCENTAGE = 90
SCOPE_PRESSURE_HARD_CAP_REMAINING = 2
# For large-scope tasks, allow slightly more remaining iterations (floor of 3)
# so we don't slam to 2 remaining on a near-finished large refactor.
SCOPE_PRESSURE_HARD_CAP_REMAINING_LARGE = 3

_SCOPE_PRESSURE_HARD_CAP_PREFIX = (
    "🚫 SCOPE BUDGET EXHAUSTED: this PR has consumed {count}/{budget} "
    "({pct}%) of its medium-scope production file budget. You MUST NOT add "
    "any new production files — only modify the existing changed files. "
    "Defer any new-file work to follow-up tasks.\n\n"
)


# A pipeline task is considered 'stale' (effectively dead, safe to supersede)
# when it has not heartbeated for this long. Matches the orchestrator's own
# reap threshold (see orchestrator.py stale-PROVISIONING recovery) so we don't
# fight with it — whichever sweep reaches the task first cancels it.
PIPELINE_TASK_STALE_MINUTES = 15


def _is_pipeline_task_stale(
    task: Task,
    *,
    vm_last_contact: datetime | None = None,
    runner_report_lookup: dict | None = None,
) -> bool:
    """True if a review/fix task has gone silent and can be superseded.

    Only applies to tasks that have actually started running. A task still
    waiting in QUEUED (never claimed) is NOT considered stale — long queue
    waits typically mean rate-limit backoff, not a dead task, and the
    orchestrator's reap loop owns that state. Once a task has either
    heartbeated or reached ``started_at``, we measure silence from the
    freshest of those timestamps.

    R3 (2026-07-12 concurrency audit) — ``vm_last_contact`` OVERRIDE.
    ``Task.last_heartbeat`` is NOT a trustworthy liveness signal on its own:
    the heartbeat handler updates the Task row ``with_for_update(skip_locked=
    True)`` and is 409-skipped under contention (routers/agent.py:4525-4545), so
    a LIVE agent whose row is lock-starved (classically by the rollup, which
    locks all sibling children FOR UPDATE) stops advancing ``last_heartbeat``
    while still calling in. Superseding on that signal alone CANCELS a live
    reviewer and deletes its VM. The reaper hit exactly this and grew a
    ``VmContact`` override for it (reaper.py:481); this predicate never did.

    ``VmContact.last_contact_at`` is written on its OWN uncontended row on every
    backend contact, so it cannot be lock-starved. When the caller supplies it
    and it is within the SAME cutoff, the agent is provably alive and the task
    is NOT stale — regardless of the on-row heartbeat. A genuinely dead agent
    (crashed / preempted / OOM) sends no requests, leaves this stale or absent,
    and is still superseded on the existing schedule.

    ``runner_report_lookup`` — the DEFEAT for that override (review round 2,
    MAJOR 5). The VmContact override alone re-opens the shared-VmContact zombie
    mask that #14413 closed in the reaper: every task on a fleet/local runner
    shares ``vm_instance_id='runner:<id>'`` (orchestrator_runner_dispatch.py:626),
    and ``VmContact`` is keyed on ``vm_instance_id``, so ANY live sibling agent on
    a busy runner keeps ``last_contact_at`` permanently fresh — a hung or dead
    reviewer would then NEVER be superseded. The runner already self-reports its
    ground-truth ``active_task_ids`` on each heartbeat; a fresh report that OMITS
    this task is proof its agent process is gone, and defeats the shared-contact
    suppression. This mirrors ``reaper.py`` exactly — the VmContact override at
    :481 AND ``_runner_report_excludes_task`` 47 lines below it, which the first
    version of this predicate copied without. Fail-safe: no lookup, a non-runner
    vm id, or a stale/ambiguous report all leave the override intact.
    """
    threshold = _utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES)
    if (
        vm_last_contact is not None
        and vm_last_contact >= threshold
        and not _runner_report_excludes_task(task, threshold, runner_report_lookup)
    ):
        return False
    if task.last_heartbeat is not None:
        return task.last_heartbeat < threshold
    if task.started_at is None:
        return False
    return task.started_at < threshold


def _task_is_actively_ci_waiting(task: Task) -> bool:
    """True if ``task`` is parked waiting on CI (``review_loop.ci_waiting``).

    A CI-waiting task's silence is EXPECTED, not the signature of a dying VM:
    the agent VM has already finished and pushed, and from here the ORCHESTRATOR
    — not the VM — polls GitHub CI, so ``last_heartbeat`` deliberately goes
    stale. A multi-shard CI run routinely outlasts ``PIPELINE_TASK_STALE_MINUTES``
    (15), so :func:`_is_pipeline_task_stale` reports such a task as stale even
    though it is making authoritative progress. Callers must therefore exempt a
    CI-waiting task from stale-supersession / stale-skip exactly as they exempt a
    BLOCKED precondition park — a genuinely-stuck CI-waiting task is bounded
    instead by ci_completion's in-progress reset cap and the EVALUATING CI
    backstop (which FAIL it, dropping it from the active pipeline). Without this
    exemption, three false stale-supersessions of one CI-waiting fix trip the
    supersession-churn halt and terminalize an otherwise-healthy chain
    (research b2231dd0 / rollup PR #10389, 2026-06-16).
    """
    return bool(((task.context or {}).get("review_loop") or {}).get("ci_waiting"))


def _review_loop_has_authoritative_approval(
    review_loop: dict | None,
    *,
    project_id: uuid.UUID,
    pr_number: int,
) -> bool:
    """Return whether ``review_loop`` proves a clean exact-target approval.

    Approval flags and top-level verdict mirrors are lifecycle hints, not
    authority.  The durable proof is the final review-history entry, stamped
    with both immutable SHAs, matching the pinned review target, with no binding
    blocker left in either the persisted or rebuilt issue ledger.  Missing or
    contradictory proof fails closed.
    """
    if not isinstance(review_loop, dict):
        return False

    history = review_loop.get("review_history")
    if not isinstance(history, list) or not history:
        return False
    latest = history[-1]
    if not isinstance(latest, dict):
        return False
    if str(latest.get("verdict") or "").upper() != "APPROVE":
        return False

    approved_head = str(
        latest.get("reviewed_head_sha") or latest.get("head_sha") or ""
    ).strip()
    approved_base = str(
        latest.get("reviewed_base_sha") or latest.get("base_sha") or ""
    ).strip()
    if not approved_head or not approved_base:
        return False

    target = review_loop.get("review_target")
    if not isinstance(target, dict):
        return False
    try:
        target_pr = int(target.get("pr_number"))
    except (TypeError, ValueError):
        return False
    if target_pr != pr_number:
        return False
    if str(target.get("project_id") or "") != str(project_id):
        return False
    if str(target.get("head_sha") or "").strip() != approved_head:
        return False
    if str(target.get("base_sha") or "").strip() != approved_base:
        return False

    persisted_ledger = review_loop.get("issue_ledger")
    if isinstance(persisted_ledger, dict) and persisted_ledger.get("open"):
        return False

    from app.issue_ledger import build_issue_ledger

    return not bool(build_issue_ledger(history).get("open"))


def _cap_exhausted_after_approve(
    orig: Task,
    source_task: Task,
    pr_number: int,
) -> bool:
    """Classify cap exhaustion as post-approval only from durable proof.

    The parent handoff is preferred, while an exact proof on the source task is
    accepted for non-rollup chains.  This intentionally performs no broad task
    scan: a stale review row or legacy verdict mirror cannot authorize a
    lifecycle outcome for the current PR target.
    """
    return any(
        _review_loop_has_authoritative_approval(
            (task.context or {}).get("review_loop"),
            project_id=orig.project_id,
            pr_number=pr_number,
        )
        for task in (orig, source_task)
    )


def _is_backmerge_resolver(orig: Task) -> bool:
    """True if ``orig`` is a back-merge conflict-resolver task.

    Back-merge resolvers carry ``context['backmerge_conflict'] == True`` and
    open a PR (base=automain) that re-applies main→automain sync work after a
    conflict. They are ``create`` tasks, NOT rollups, but — like a rollup — the
    PR aggregates already-completed work (the sync) that is expensive to redo;
    auto-closing it on review-loop non-convergence destroys that work and stalls
    main→automain sync. So a mergeable, conflict-free back-merge resolver PR is
    preservable on cap exhaustion exactly like a rollup.
    """
    return (orig.context or {}).get("backmerge_conflict") is True


async def _pr_is_preservable_on_cap(
    orig: Task,
    pr_number: int,
    session: AsyncSession,
    *,
    after_approve: bool = False,
) -> bool:
    """FIX #3b: decide whether a capped PR is worth PRESERVING (left open for
    hand-merge) rather than destroyed (closed + FAILED + CODE_ERROR).

    A PR is preservable when it carries real, mergeable work that is expensive
    to redo and safe to hand-merge:
      • it is EITHER a rollup PR (rollup chains aggregate already-completed
        subtask work — discarding the whole PR over review-loop non-convergence
        throws away merged-elsewhere siblings) OR a back-merge conflict-resolver
        PR (``context['backmerge_conflict'] is True`` — re-applied main→automain
        sync work; auto-closing it stalls promotion sync), AND
      • it is NON-EMPTY (``changed_files > 0`` — a branch rebased flat has
        nothing to hand-merge), AND
      • it is not a genuine merge conflict (``mergeable_state != 'dirty'``).

    GUARDRAIL — rollups: a broken/empty rollup PR returns False so the caller
    keeps today's close+FAIL path; ``''``/``'unknown'`` (not-yet-computed) and
    any GitHub lookup failure all return False — when in doubt, fall back to the
    safe destructive path.  ``'dirty'`` (conflict) is NOT preservable when
    called from the non-approved path (default ``after_approve=False``) because a
    non-approved conflicted PR is a genuine non-convergence.  When
    ``after_approve=True`` (the PR was signed off before the cap was hit),
    ``'dirty'`` IS preservable — the conflict is a mechanical blocker, not a
    rejection, and a human can resolve it for hand-merge.  Every other state
    (``clean`` / ``unstable`` / ``blocked`` / ``behind``) is treated as
    hand-mergeable regardless of ``after_approve``.

    Back-merge resolvers are stricter on CI but more conservative on ambiguity:
      • CI-RED: ``mergeable_state == 'unstable'`` (mergeable but a status check
        is failing) is NOT preservable — the task requires the resolver PR be
        CI-green, and a red back-merge shouldn't be hand-merged blind.
      • AMBIGUOUS: closing a back-merge resolver is the worse outcome (it can
        always be hand-merged and otherwise stalls sync), so when its live PR
        state is UNKNOWN or the GitHub lookup FAILS we PRESERVE.
      • The only hard non-preserve for a back-merge resolver is a confirmed
        empty PR or a confirmed ``dirty`` / ``unstable`` state.
    """
    is_rollup = (orig.context or {}).get("_is_rollup_review") is True or (
        orig.context or {}
    ).get("_rollup_retries") is not None
    if not is_rollup:
        rollup_pr = (orig.context or {}).get("rollup_pr_number")
        if rollup_pr and rollup_pr == pr_number:
            is_rollup = True
    is_backmerge = _is_backmerge_resolver(orig)
    if not is_rollup and not is_backmerge:
        return False
    from app.github import get_pull_request
    from app.routers.agent import get_github_token_for_task

    # ── STEP 1: resolve the token. NOT part of the rate-limit classification. ──
    #
    # This try is DELIBERATELY NARROW. Folding it into the GET's handler (as an
    # earlier draft did) is a WEDGE: ``get_github_token_for_task`` →
    # ``github_token_resolver`` raises
    #     HTTPException(403, "Project does not have an authorized GitHub repository")
    # for a project whose github_owner/github_repo was cleared or de-authorized.
    # That is a PERMANENT CONFIG failure, not a quota statement — GitHub is never
    # even reached — but a 403 classified as "transient" becomes
    # _PreservabilityUnavailable and DEFERS **forever**, on a condition that never
    # clears. Every re-entry re-raises and re-defers; the task never terminalizes.
    # That is the "deadline-less park = permanent wedge" class this campaign exists
    # to remove, re-created by the fix for it.
    #
    # A token/config failure is INFORMATIVE: we genuinely cannot act on this PR and
    # no amount of waiting changes that. Take the pre-existing fail-safe branch.
    #
    # ``get_github_token_for_task`` does NOT mint a token (in-process 50-min TTL
    # cache — github_token_resolver.py:63-72; zero HTTP in steady state) but it DOES
    # read the DB, which re-opens a transaction on this session. The commit ends
    # that read-only txn so the connection is NOT left idle-in-transaction across
    # the HTTP call below — Postgres would kill it at
    # idle_in_transaction_session_timeout (60s; database.py:405-418). The caller
    # (_handle_cap_exhaustion) has already committed and released the per-PR
    # advisory lock, so this commit is a no-op write-wise; it exists purely to close
    # the read txn.
    # The try covers ONLY the resolve, NOT the commit. A commit failure (connection
    # death, the idle-in-transaction kill this very change is about, a deadlock) is
    # not a config failure, and mislabelling it as one would (a) log a lie and
    # (b) swallow it and `return` WITHOUT rolling back — leaving the session in
    # pending-rollback so the caller's next statement (the FOR UPDATE re-lock) dies
    # with PendingRollbackError. Let a commit failure propagate to the caller, which
    # already has the rollback discipline for it.
    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: could not resolve a GitHub token for PR "
            f"#{pr_number} ({type(e).__name__}: {e}) — this is a CONFIG failure, not "
            f"a quota one, so it will not clear on retry. Taking the fail-safe "
            f"branch: "
            f"{'PRESERVING (back-merge resolver)' if is_backmerge else 'close+FAIL'}"
        )
        return is_backmerge
    await session.commit()

    # ── STEP 2: the GET. ONLY this is rate-limit/deadline classified. ──
    try:
        pr = await asyncio.wait_for(
            get_pull_request(
                gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
            ),
            timeout=_PRESERVABILITY_GET_TIMEOUT_S,
        )
    except Exception as e:
        # ── CLASSIFY THE FAILURE. Not every failure is an ANSWER. ──
        #
        # TRANSIENT (says nothing about the PR) -> raise _PreservabilityUnavailable
        # -> the caller DEFERS the whole terminal transition. Two cases:
        #
        #   1. DEADLINE BREACH. We stopped waiting. A statement about US.
        #
        #   2. RATE LIMIT — a statement about our QUOTA. This is the case the
        #      deadline could never have covered, because NOTHING HANGS:
        #      ``_github_request`` FAST-FAILS a hard 429/403 (github.py ~:536-540)
        #      and short-circuits a known-empty bucket with zero network
        #      (~:471-486), so the call returns in ~0-15s. Left in the fail-safe
        #      branch below it reads as "PR not salvageable" -> close+FAIL+
        #      CODE_ERROR, in BULK, for every cap-exhausted rollup in an
        #      install-token-exhaustion window — verbatim the incident this change
        #      exists to prevent.
        #
        #      Keyed on 503, which is what ``github_pulls.get_pull_request`` now
        #      raises via the CANONICAL ``github_http.raise_rate_limited`` (503 +
        #      Retry-After — the repo's one retryable-GitHub surface, added by
        #      #14546). 403/429 are kept as a belt-and-braces alias in case a
        #      helper still surfaces the raw upstream code; they cost nothing and
        #      cannot fire for a PERMISSION 403, which reaches us as a 500.
        #
        # INFORMATIVE (a real answer) -> unchanged fail-safe semantics: a 404/410
        # (PR gone), a PERMISSION 403 (GitHub sends the quota headers on it but with
        # a NONZERO remaining, so it is not a rate limit and github_pulls raises it
        # as a 500), a persistent 5xx. Rollups fall back to the safe destructive
        # path; back-merge resolvers prefer preserve-over-destroy (closing a
        # resolver is the worse outcome — it can always be hand-merged).
        #
        # NOTE: a token/CONFIG failure can no longer reach this handler at all —
        # it is caught by the narrow try above. See the wedge note there.
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: preservability GET for PR #{pr_number} "
                f"exceeded the {_PRESERVABILITY_GET_TIMEOUT_S:.0f}s deadline — "
                f"DEFERRING the terminal cap transition to the next sweep (PR left "
                f"OPEN, task left non-terminal). A timeout is not evidence the PR is "
                f"unsalvageable."
            )
            raise _PreservabilityUnavailable(
                f"preservability GET for PR #{pr_number} timed out"
            ) from e
        if isinstance(e, HTTPException) and e.status_code in (403, 429, 503):
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: preservability GET for PR #{pr_number} "
                f"was RATE LIMITED ({e.status_code}: {e.detail}) — DEFERRING the "
                f"terminal cap transition to the next sweep (PR left OPEN, task left "
                f"non-terminal). Being out of GitHub budget is not evidence the PR is "
                f"unsalvageable."
            )
            raise _PreservabilityUnavailable(
                f"preservability GET for PR #{pr_number} rate limited "
                f"({e.status_code})"
            ) from e
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: preservability lookup failed for PR "
            f"#{pr_number}: {type(e).__name__}: {e} — "
            f"{'PRESERVING (back-merge resolver)' if is_backmerge else 'falling back to close+FAIL'}"
        )
        return is_backmerge
    if (pr.get("state") or "").lower() != "open":
        return False
    if (pr.get("changed_files") or 0) <= 0:
        # A confirmed-empty PR has nothing to hand-merge — never preservable,
        # not even for a back-merge resolver.
        return False
    mergeable_state = (pr.get("mergeable_state") or "").lower()
    if is_backmerge:
        # Back-merge resolver: require CI-green ('unstable' = a status check is
        # failing → NOT preservable) and conflict-free ('dirty'). Any other
        # state — INCLUDING the unknown/not-yet-computed case — is preservable;
        # prefer preserve-over-destroy for sync-critical resolver work.
        return mergeable_state not in ("dirty", "unstable")
    # Rollup: 'unknown'/'' means GitHub hasn't computed the state yet — treat
    # conservatively as NOT preservable regardless of after_approve.  'dirty'
    # (genuine merge conflict): non-approved PRs close as before; approved PRs
    # preserve — the conflict is mechanical and a human can resolve it for
    # hand-merge.  All other states (clean, unstable, blocked, behind) are
    # hand-mergeable in both modes.
    if mergeable_state in ("", "unknown"):
        return False
    if mergeable_state == "dirty":
        return after_approve
    return True


def _cap_already_handled(orig: Task) -> bool:
    """True when ``orig`` already carries a terminal cap-exhaustion outcome.

    Checked twice in ``_handle_cap_exhaustion``: once on entry, and again under
    the ``orig`` row lock after the (unlocked) preservability GitHub GET, so a
    peer cap handler that landed the transition while we were on the network
    cannot be double-applied.
    """
    return orig.status in (
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and (orig.context or {}).get("failure_detail") in (
        "review_cap_exhausted",
        "cap_exhausted_after_approve",
        "review_cap_preserved_for_handmerge",
    )


async def _handle_cap_exhaustion(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    iterations: int,
    session: AsyncSession,
    *,
    scope: str,
    verified_disposition: str,
    lane_counts: dict | None = None,
) -> None:
    """Apply an explicitly verified destructive cap disposition.

    Ordinary numerical cap stops never call this function: they enter the
    existing abandon-candidate/operator park instead. This handler is retained
    for an explicit authority that has independently verified a ``reject`` or
    ``empty`` disposition. It transitions the create task (``orig``) to FAILED with
    failure_detail=review_cap_exhausted, closes the GitHub PR, and fires an
    approval alert. Idempotent — safe to re-enter on the same orig without
    duplicating side effects.

    Why FAILED instead of COMPLETED+requires_approval:
    The prior behaviour left the create task COMPLETED and relied on
    requires_approval=True as a terminal signal, but rollup_management's
    stale-unmerged respawn loop doesn't check that flag — it kept re-entering
    the cap path every 10 min indefinitely. Marking FAILED routes the task
    through the standard failure → partial-rollup path.

    ``scope`` is either "global" (cross-chain) or "per-chain" — used only for
    human-readable messages.
    """
    if verified_disposition not in {"reject", "empty"}:
        raise ValueError(
            "cap closure requires verified_disposition='reject' or 'empty'"
        )
    from app.failure_classification import (
        APPROVED_CAP_PR_CLOSED,
        CODE_ERROR,
        persist_failure_category,
    )
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    cap_label = scope
    # ── ROLLBACK-EXPIRY CONTRACT (review round 2, BLOCKER 1) ──
    # ``session.rollback()`` expires EVERY loaded instance in the session — even
    # under ``expire_on_commit=False`` — including the CALLER's ``source_task``.
    # All three callers (review_pipeline.py ~:2901/:3068/:3096) immediately do
    # ``_set_spawn_reason(source_task, SPAWN_REASON_CAP)``, whose first statement
    # is ``dict(task.context or {})`` — an expired-attribute read that issues a
    # lazy refresh OUTSIDE greenlet context and raises MissingGreenlet, poisoning
    # the agent-status post-commit chain with a 500. That is the SAME failure mode
    # this change fixes at the acquire side, so re-introducing it here would be
    # self-defeating.
    #
    # Rules for this function, enforced by
    # test_cap_exhaustion_peer_locked_orig_does_not_expire_caller_source_task:
    #   1. NEVER rollback when nothing is pending — ``commit()`` ends the txn and
    #      releases row locks WITHOUT expiring (expire_on_commit=False).
    #   2. When a rollback IS mandatory (a failed commit leaves the session in
    #      pending-rollback), re-fetch ``source_task`` by id afterwards. The id is
    #      snapshotted HERE, before any rollback can happen, because reading
    #      ``source_task.id`` on an already-expired instance is itself the very
    #      lazy-load we are avoiding.
    _source_task_id = source_task.id
    _lane_detail = (
        "; ".join(f"{k}={v}" for k, v in sorted(lane_counts.items()))
        if lane_counts
        else ""
    )
    source_task.latest_status_message = (
        f"Review-fix cap reached for PR #{pr_number} ({iterations} iterations, {cap_label})"
        + (f" [{_lane_detail}]" if _lane_detail else "")
    )
    session.add(source_task)

    # ── K8 (2026-07-12 concurrency audit): RELEASE THE PER-PR ADVISORY LOCK ──
    #
    # This handler is only ever reached from inside ``_spawn_pipeline_task``,
    # which takes ``pg_advisory_xact_lock(pr_number)`` at its top
    # (review_pipeline.py ~:1764) and holds it until the transaction ends. The
    # four commits between that acquire and the calls into this function all sit
    # on branches that RETURN, so on the cap-exhaustion path the lock was STILL
    # HELD across the preservability GitHub GET below (and, on the back-merge /
    # rollup paths, that GET is the only reason the lock mattered at all). That
    # left the connection idle-in-transaction across an unbounded HTTP call —
    # racing the 60s idle_in_transaction_session_timeout — while blocking every
    # other spawner for this PR.
    #
    # Committing here releases it. That is SAFE precisely because this handler
    # never INSERTs: the advisory lock exists to serialize the
    # SELECT-decide-INSERT dedup window in ``_spawn_pipeline_task``
    # (task_locking.py ~:228-244), and the cap path returns ``(None, False)``
    # without ever reaching that INSERT. The mutual exclusion this function
    # actually needs — two concurrent cap handlers racing the same ``orig`` —
    # is re-established below with a real row lock on ``orig``.
    await session.commit()

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to load orig {resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} not found"
        )
        return

    if _cap_already_handled(orig):
        logger.info(
            f"[PIPELINE] Cap re-triggered for PR #{pr_number} — orig {resolved_original_id[:8]} "
            f"already terminal with {(orig.context or {}).get('failure_detail')}, "
            f"skipping side effects"
        )
        await session.commit()
        return

    # ── OPERATOR-PARK GUARD (lineage d3c12745 / PR #16759). ──
    # ``_cap_already_handled`` cannot cover this: a parked orig is NOT terminal
    # (the stuck/standoff escalation leaves it non-terminal on purpose so an
    # operator can adjudicate), so a separate guard is needed. When the
    # stuck-cycle/standoff detector already escalated this chain to the
    # operator, cap exhaustion is a LATER, weaker terminal policy: it must not
    # override the park by closing the PR the escalation deliberately preserved
    # and FAILing the root out from under the operator. Idempotent — log and
    # return, leaving the park (status, requires_approval, breadcrumb) exactly
    # as the escalation recorded it.
    _park_reason = review_loop_parked_for_operator(orig)
    if _park_reason is not None:
        logger.warning(
            f"[PIPELINE] Cap exhaustion for PR #{pr_number} ({iterations} iterations, "
            f"{cap_label}) suppressed — orig {resolved_original_id[:8]} is parked for "
            f"operator ({_park_reason}); NOT closing the PR or overriding the park"
        )
        await session.commit()
        return

    # ── Decide FIRST, mutate SECOND. ──
    # Both the after-approve classification (a pure durable-context read) and
    # the preservability check (a real GitHub GET) now run here, with NO
    # advisory lock held and no lock on ``orig``. `_pr_is_preservable_on_cap`
    # commits its own read txn before the GET so nothing is left
    # idle-in-transaction across it.
    #
    # Hoisting the two preservability calls into one is behaviour-preserving:
    # the after-approve branch called it with after_approve=True and returned;
    # the fall-through branch called it with after_approve=False. Exactly one of
    # those ran before, and exactly one set of semantics runs now.
    after_approve = _cap_exhausted_after_approve(orig, source_task, pr_number)
    try:
        preservable = await _pr_is_preservable_on_cap(
            orig, pr_number, session, after_approve=after_approve
        )
    except _PreservabilityUnavailable as _unavail:
        # TRANSIENT (deadline breach / GitHub rate limit): take NO terminal action —
        # do not close the PR, do not FAIL the task, do not record CODE_ERROR. We
        # simply do not know the PR's state, and neither a timeout nor a drained
        # quota bucket is evidence that it is unsalvageable.
        #
        # BUT A DEFER IS A PARK, AND AN UNCOUNTED PARK IS A SILENT WEDGE.
        # Re-entry here is EVENT-DRIVEN, not swept: ``_spawn_pipeline_task`` is
        # driven by CI / review-verdict completions, so with no further event the
        # cap transition simply never happens and the task sits EVALUATING until the
        # hours-later stall reaper notices. Deferring forever on a `logger.warning`
        # is exactly the deadline-less-park class this campaign exists to remove.
        # So COUNT the defers on ``orig`` and ALERT once past the threshold: a
        # GitHub outage long enough to burn _CAP_DEFER_ALERT_AFTER consecutive
        # attempts is an operator problem, not something to keep silently retrying.
        # (The transition itself stays deferred — alerting is the escalation, not
        # closing the PR. Never trade a silent park for a destructive one.)
        #
        # ── RELOCK BEFORE THE COUNTER WRITE. ──
        #
        # The counter is a ``context`` RMW, and ``orig`` here is a STALE snapshot:
        # it was loaded at the ``session.get`` above, and since then we have been on
        # the network for 0-15s (rate limit) or up to 420s (deadline). K8
        # deliberately COMMITTED at the top of this function to release the per-PR
        # advisory lock, so a peer cap handler may legitimately be running on this
        # same ``orig`` — that is the whole point of the release — and
        # ``expire_on_commit=False`` means the commit inside
        # ``_pr_is_preservable_on_cap`` did NOT refresh our copy.
        #
        # Writing ``orig.context = dict(stale_snapshot)`` unlocked would therefore
        # blind-overwrite the WHOLE JSONB blob from a pre-race snapshot — the exact
        # R3 defect this change fixes at the supersede sites ("the write replaced the
        # whole context blob from the stale snapshot, so the just-committed verdict
        # was ERASED"). Two real collisions:
        #   • a peer cap handler that landed the terminal transition during our GET:
        #     its ``status`` column survives (status isn't dirty in OUR session) but
        #     its ``failure_detail`` is ERASED — which defeats ``_cap_already_handled``
        #     (it requires BOTH a terminal status AND a failure_detail), so a
        #     re-entry re-runs every cap side effect: a SECOND close_pull_request, a
        #     second alert, a second record_direction_result.
        #   • ``orchestrator_ci_monitor`` writing ``context["review_loop"]`` on this
        #     same EVALUATING task, on an exponential backoff — i.e. exactly during a
        #     GitHub-degraded window, which is exactly when this defer path fires. It
        #     does it correctly, under FOR UPDATE. We would clobber it.
        #
        # So take the SAME row lock the terminal path takes below, and re-read the
        # context from the FRESH row. ``skip_locked``: if a peer holds it, simply
        # forfeit the increment and stay deferred — losing a counter tick is always
        # safe, erasing a peer's terminal write is not. (Same reasoning as the CAS
        # un-stake in rollup_management.)
        _defer_locked = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id == orig.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if _defer_locked is None:
            logger.warning(
                f"[PIPELINE] Cap-exhaustion DEFERRED for orig {resolved_original_id[:8]} "
                f"/ PR #{pr_number}: {_unavail}. (Defer counter not incremented — a "
                f"peer holds the orig row; forfeiting the tick beats clobbering it.)"
            )
            # COMMIT, not rollback: nothing is pending and a rollback would expire the
            # caller's ``source_task`` (rollback-expiry contract, rule 1).
            await session.commit()
            return
        orig = _defer_locked
        if _cap_already_handled(orig):
            # A peer cap handler landed the terminal transition while we were on the
            # network. Do NOT write the counter — that write is what would erase its
            # ``failure_detail`` and defeat this very guard on the next re-entry.
            logger.info(
                f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} went "
                f"terminal ({(orig.context or {}).get('failure_detail')}) during the "
                f"preservability lookup for PR #{pr_number} — nothing to defer"
            )
            await session.commit()
            return

        from sqlalchemy.orm.attributes import flag_modified

        # LIFETIME, not consecutive: this key is in neither ``_ROLLUP_TRANSIENT_
        # CTX_KEYS`` nor ``FRESH_ATTEMPT_CONTEXT_STRIP_KEYS``, and no success path
        # clears it. That is deliberate — a counter that reset on every good lookup
        # would hide a FLAPPING GitHub budget, which is precisely the condition worth
        # alerting on. The log and the alert say "lifetime" so nobody reads it as a
        # consecutive-failure streak.
        _defer_ctx = dict(orig.context or {})  # FRESH row, taken under the lock
        _defer_n = int(_defer_ctx.get(_CAP_DEFER_COUNT_KEY) or 0) + 1
        _defer_ctx[_CAP_DEFER_COUNT_KEY] = _defer_n
        orig.context = _defer_ctx
        flag_modified(orig, "context")
        session.add(orig)
        logger.warning(
            f"[PIPELINE] Cap-exhaustion DEFERRED for orig {resolved_original_id[:8]} / "
            f"PR #{pr_number} ({_defer_n} lifetime defers): {_unavail}. The PR stays "
            f"OPEN and the task stays non-terminal. Re-entry is event-driven (a CI or "
            f"review-verdict completion), NOT a timer — so this does not self-heal on "
            f"a quiet PR."
        )
        try:
            await session.commit()
        except Exception as _defer_commit_err:  # noqa: BLE001
            # Losing the counter must never turn a non-destructive defer into an
            # error path. The rollback is MANDATORY (a failed commit leaves the
            # session in pending-rollback; without it the caller's next statement
            # raises PendingRollbackError), and it expires everything — so re-fetch
            # the caller's source_task per rule 2 of the contract above.
            await session.rollback()
            await session.get(Task, _source_task_id)
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: failed to persist the defer counter for "
                f"PR #{pr_number}: {_defer_commit_err}"
            )
            return
        if _defer_n >= _CAP_DEFER_ALERT_AFTER:
            try:
                await _maybe_create_failure_alert(
                    orig,
                    session,
                    (
                        f"Review-fix cap exhaustion for PR #{pr_number} has been "
                        f"DEFERRED {_defer_n} times (lifetime) — the preservability "
                        f"lookup keeps coming back transient ({_unavail}). The PR is "
                        f"still OPEN and the task is still non-terminal, but it is NOT "
                        f"converging: re-entry is event-driven, so this will not "
                        f"self-heal without a new CI/review event. Check the GitHub "
                        f"install-token budget."
                    ),
                )
                await session.commit()
            except Exception as _alert_err:  # noqa: BLE001
                # Same pending-rollback discipline as above: an alert failure must not
                # poison the session for the caller. Rollback (mandatory) + re-fetch.
                await session.rollback()
                await session.get(Task, _source_task_id)
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion: defer alert failed for PR "
                    f"#{pr_number}: {_alert_err}"
                )
        return

    # ── Now take a REAL row lock on ``orig`` for the terminal transition. ──
    # This restores the mutual exclusion the advisory lock used to provide (two
    # cap handlers for the same PR can no longer both write ``orig``), and it is
    # released by the first ``session.commit()`` in every branch below — i.e.
    # BEFORE any of the GitHub side effects (comment / close / alert), all of
    # which already run post-commit. ``skip_locked``: a peer holding this row is
    # either the other cap handler doing exactly this work or the agent's own
    # completion write; in both cases the fail-safe is to not fight for it and
    # let the (idempotent, re-entrant) cap path retry on the next sweep.
    _locked_orig = (
        await session.execute(
            select(Task)
            .where(Task.id == orig.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if _locked_orig is None:
        logger.info(
            f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} row held by a "
            f"peer — deferring the terminal transition for PR #{pr_number} to the next sweep"
        )
        # COMMIT, not rollback (rule 1 of the rollback-expiry contract above).
        # Nothing is pending on this branch — the last write was committed at the
        # top of the function and the only statement since is the SELECT ... FOR
        # UPDATE that returned no row. ``commit()`` ends that read transaction and
        # releases the lock exactly as a rollback would, but does NOT expire the
        # caller's ``source_task`` (expire_on_commit=False), so the caller's
        # immediate ``_set_spawn_reason(source_task, ...)`` stays a pure in-memory
        # read. A rollback here raised MissingGreenlet at review_pipeline.py:751
        # on precisely the branch this PR's concurrency targets (a peer holding
        # the ``orig`` row).
        await session.commit()
        return
    orig = _locked_orig
    # Re-assert idempotency under the lock: a peer cap handler may have landed
    # the terminal transition while we were doing the GitHub GET above.
    if _cap_already_handled(orig):
        logger.info(
            f"[PIPELINE] Cap-exhaustion: orig {resolved_original_id[:8]} went terminal "
            f"({(orig.context or {}).get('failure_detail')}) during the preservability "
            f"lookup for PR #{pr_number} — skipping duplicate side effects"
        )
        await session.commit()
        return
    # Re-assert the operator-park guard on the FRESH row too: the stuck/standoff
    # escalation may have parked this chain while we were on the network for the
    # preservability GET. Same rule as the entry guard — the park wins.
    _park_reason = review_loop_parked_for_operator(orig)
    if _park_reason is not None:
        logger.warning(
            f"[PIPELINE] Cap exhaustion for PR #{pr_number} suppressed under lock — "
            f"orig {resolved_original_id[:8]} was parked for operator ({_park_reason}) "
            f"during the preservability lookup; NOT closing the PR or overriding the park"
        )
        await session.commit()
        return

    # FIX D: if the cap was exhausted on an already-APPROVED PR (benign merge-
    # mechanics churn after sign-off, not an unresolved review rejection), land
    # CANCELLED with failure_detail='cap_exhausted_after_approve' and record the
    # distinct APPROVED_CAP_PR_CLOSED outcome instead of CODE_ERROR. GUARDRAIL:
    # _cap_exhausted_after_approve returns False without exact target proof or
    # while the authoritative ledger has any open blocker, preserving today's
    # FAILED + CODE_ERROR path for genuine non-convergence.
    if after_approve:
        from sqlalchemy.orm.attributes import flag_modified

        # APPROVED + PRESERVABLE: an approved rollup/back-merge PR is the
        # strongest candidate for preservation — close+CANCEL destroys
        # salvageable, signed-off work. Leave it open for hand-merge and
            # keep the APPROVED_CAP_PR_CLOSED labeling. Non-preservable approved
            # PRs (empty, non-rollup) keep the close+CANCEL path.  Conflicted
        # (dirty) rollup/back-merge PRs are also preserved here: the conflict
        # is a mechanical, recoverable blocker on an APPROVED PR — a human can
        # resolve it for hand-merge (after_approve=True loosens the dirty guard).
        if preservable:
            _preserve_kind = (
                "back-merge resolver" if _is_backmerge_resolver(orig) else "rollup"
            )
            logger.info(
                f"[PIPELINE] Cap reached for PR #{pr_number} on an APPROVED, "
                f"non-empty {_preserve_kind} — PRESERVING: leaving "
                f"PR open + flagging orig {resolved_original_id[:8]} "
                f"requires_approval for hand-merge "
                f"(APPROVED_CAP_PR_CLOSED, no CODE_ERROR, no PR close)"
            )
            orig.status = TaskStatus.FAILED
            from app.admin_approval_authority import (
                establish_independent_approval_hold,
            )

            establish_independent_approval_hold(
                orig,
                reason="approved_cap_pr_preserved_for_handmerge",
                owner="review_loop_convergence",
            )
            orig.latest_status_message = (
                f"Review-fix cap reached ({iterations} iterations, {cap_label}) "
                f"after approval — PR #{pr_number} LEFT OPEN with mergeable work; "
                f"manual hand-merge required (approved cap, not auto-closed)"
            )
            persist_failure_category(orig, APPROVED_CAP_PR_CLOSED)
            ctx = dict(orig.context or {})
            ctx["failure_detail"] = "cap_exhausted_after_approve"
            ctx["review_cap_iterations"] = iterations
            ctx["pr_preserved_for_handmerge"] = True
            ctx["handmerge_pr_number"] = pr_number
            # NB: deliberately NOT setting pr_rejected — this was an approved PR.
            orig.context = ctx
            flag_modified(orig, "context")
            session.add(orig)
            try:
                await session.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): failed to "
                    f"commit orig {resolved_original_id[:8]}: {e}",
                    exc_info=True,
                )
                return
            record_task_result(
                success=False,
                failure_class=APPROVED_CAP_PR_CLOSED,
                task_id=str(orig.id),
                task_type=(orig.first_payload or {}).get("type", ""),
            )
            _direction_id = str((orig.context or {}).get("direction_id") or "")
            if _direction_id:
                record_direction_result(
                    _direction_id,
                    success=False,
                    failure_class=APPROVED_CAP_PR_CLOSED,
                    project_id=str(orig.project_id) if orig.project_id else "",
                )
            try:
                from app.task_completion import _record_remediation_if_terminal

                await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
                await session.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): remediation "
                    f"record failed for PR #{pr_number}: {e}"
                )
            try:
                gh_token, owner, repo = await get_github_token_for_task(
                    orig.id, session
                )
                await create_pr_comment(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    f"Review-fix cap reached ({iterations} iterations, {cap_label}) "
                    f"after approval. This PR has mergeable work and was LEFT OPEN "
                    f"for manual hand-merge rather than auto-closed. "
                    f"Operator review required.",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): comment "
                    f"failed on PR #{pr_number}: {e}"
                )
            try:
                await _maybe_create_failure_alert(
                    orig,
                    session,
                    f"PR #{pr_number} hit review-fix cap ({iterations} iterations, "
                    f"{cap_label}) after approval — left open with approved mergeable "
                    f"work, hand-merge required",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): alert "
                    f"failed for PR #{pr_number}: {e}"
                )
            try:
                await _create_handmerge_attention_message(
                    orig, pr_number, iterations, cap_label, session
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve/preserve): attention "
                    f"message failed for PR #{pr_number}: {e}"
                )
            return

        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} AFTER approval — landing "
            f"orig {resolved_original_id[:8]} CANCELLED (cap_exhausted_after_approve), "
            f"recording {APPROVED_CAP_PR_CLOSED}"
        )
        orig.status = TaskStatus.CANCELLED
        orig.latest_status_message = (
            f"Review-fix cap reached ({iterations} iterations, {cap_label}) after "
            f"approval — PR #{pr_number} auto-closed because merge mechanics exhausted "
            f"the cap (no rejection)"
        )
        persist_failure_category(orig, APPROVED_CAP_PR_CLOSED)
        ctx = dict(orig.context or {})
        ctx["failure_detail"] = "cap_exhausted_after_approve"
        ctx["review_cap_iterations"] = iterations
        # NB: deliberately NOT setting pr_rejected — this was an approved PR.
        orig.context = ctx
        flag_modified(orig, "context")
        session.add(orig)
        try:
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): failed to commit orig "
                f"{resolved_original_id[:8]}: {e}",
                exc_info=True,
            )
            return
        # Best-effort: still close the GitHub PR and record the terminal
        # outcome as a distinct, budget-exempt failure class so analytics and
        # digests can list approved PR closures without CODE_ERROR/PR_REJECTED.
        record_task_result(
            success=False,
            failure_class=APPROVED_CAP_PR_CLOSED,
            task_id=str(orig.id),
            task_type=(orig.first_payload or {}).get("type", ""),
        )
        _direction_id = str((orig.context or {}).get("direction_id") or "")
        if _direction_id:
            record_direction_result(
                _direction_id,
                success=False,
                failure_class=APPROVED_CAP_PR_CLOSED,
                project_id=str(orig.project_id) if orig.project_id else "",
            )
        try:
            from app.task_completion import _record_remediation_if_terminal

            await _record_remediation_if_terminal(orig, TaskStatus.CANCELLED, session)
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): remediation record "
                f"failed for PR #{pr_number}: {e}"
            )
        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            try:
                await create_pr_comment(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    f"Auto-closed: review-fix cap reached ({iterations} iterations, "
                    f"{cap_label}) after approval — no outstanding changes requested. "
                    f"Work was signed off, but merge mechanics exhausted the cap; "
                    f"closing as superseded.",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[PIPELINE] Cap-exhaustion(after-approve): comment failed on PR "
                    f"#{pr_number}: {e}"
                )
            closed = await close_pull_request(
                gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
            )
            if closed:
                logger.info(
                    f"[PIPELINE] Closed PR #{pr_number} after approved-cap exhaustion "
                    f"({iterations} iterations)"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(after-approve): PR close failed for "
                f"#{pr_number}: {e}"
            )
        return

    # FIX #3b: PRESERVE WORK ON CAP. When the cap is hit on REQUEST_CHANGES for a
    # NON-EMPTY, conflict-free PR that aggregates expensive-to-redo work — a
    # ROLLUP PR (merged-elsewhere siblings) or a BACK-MERGE conflict-resolver PR
    # (re-applied main→automain sync) — the prior behaviour (close PR + FAIL +
    # CODE_ERROR) destroyed salvageable work over review-loop non-convergence,
    # and for a back-merge resolver additionally stalled main→automain sync.
    # Instead, leave the PR OPEN and flag the orig requires_approval so an
    # operator can hand-merge it. The orig still lands terminal (FAILED routes
    # through the partial-rollup path and stops the stale-unmerged respawn loop,
    # per the docstring above) but with a distinct failure_detail, NO CODE_ERROR,
    # NO pr_rejected, and NO PR close.
    # GUARDRAIL: _pr_is_preservable_on_cap returns False for empty / conflicted /
    # CI-red PRs and for non-rollup non-backmerge tasks → those keep today's
    # close+FAIL+CODE_ERROR path below. (Rollups also fall back to close on an
    # unknown mergeable state or GitHub lookup failure; back-merge resolvers
    # prefer preserve in those ambiguous cases since closing them is worse.)
    if preservable:
        from sqlalchemy.orm.attributes import flag_modified

        _preserve_kind = (
            "back-merge resolver" if _is_backmerge_resolver(orig) else "rollup"
        )
        logger.info(
            f"[PIPELINE] Cap reached for PR #{pr_number} on a non-empty, "
            f"conflict-free {_preserve_kind} — PRESERVING work: leaving PR open "
            f"+ flagging orig {resolved_original_id[:8]} requires_approval for "
            f"hand-merge (no CODE_ERROR, no PR close)"
        )
        orig.status = TaskStatus.FAILED
        # Always flag requires_approval here regardless of the global gate — the
        # whole point of preservation is to route this to a human; without the
        # flag the open PR would dangle with no owner.
        from app.admin_approval_authority import establish_independent_approval_hold

        establish_independent_approval_hold(
            orig,
            reason="review_cap_preserved_for_handmerge",
            owner="review_loop_convergence",
        )
        orig.latest_status_message = (
            f"Review-fix cap reached ({iterations} iterations, {cap_label}) — "
            f"PR #{pr_number} LEFT OPEN with mergeable work; manual hand-merge "
            f"required (not auto-closed)"
        )
        ctx = dict(orig.context or {})
        ctx["failure_detail"] = "review_cap_preserved_for_handmerge"
        ctx["review_cap_iterations"] = iterations
        ctx["pr_preserved_for_handmerge"] = True
        ctx["handmerge_pr_number"] = pr_number
        # NB: deliberately NOT setting pr_rejected / CODE_ERROR — the work is
        # salvageable, not a code failure, and the PR is NOT closed.
        orig.context = ctx
        flag_modified(orig, "context")
        session.add(orig)
        try:
            await session.commit()
        except Exception as e:
            # SURFACE, don't swallow: this commit IS the terminal cap transition.
            # A connection death here (e.g. the idle-in-transaction kill this PR
            # is fixing) silently dropped it at WARNING level, leaving the orig
            # non-terminal and the PR open with no owner — the stale-unmerged
            # respawn loop then re-enters this handler every ~10 min forever with
            # nothing in the error budget to show for it. ERROR + exc_info so it
            # is visible; rollback so the caller's session is not left in
            # pending-rollback; the re-entry is idempotent (_cap_already_handled)
            # so the next sweep genuinely retries.
            #
            # The rollback is MANDATORY here (the failed commit left the session
            # in pending-rollback), so rule 2 of the rollback-expiry contract at
            # the top of this function applies: it expired every loaded instance,
            # INCLUDING the caller's ``source_task``. Re-fetch it by the id
            # snapshotted before the rollback so the caller's immediate
            # ``_set_spawn_reason(source_task, ...)`` — a ``task.context`` read —
            # does not lazy-load outside greenlet context and raise
            # MissingGreenlet. ``session.get`` returns the identity-mapped
            # instance, i.e. the caller's OWN object, un-expired in place.
            await session.rollback()
            await session.get(Task, _source_task_id)
            logger.error(
                f"[PIPELINE] Cap-exhaustion(preserve): TERMINAL CAP TRANSITION "
                f"DROPPED for orig {resolved_original_id[:8]} / PR #{pr_number} — "
                f"commit failed ({type(e).__name__}: {e}). PR left OPEN and orig "
                f"left non-terminal; will retry on the next sweep.",
                exc_info=True,
            )
            return
        try:
            from app.task_completion import _record_remediation_if_terminal

            await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
            await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): remediation record failed "
                f"for PR #{pr_number}: {e}"
            )
        # Leave a PR comment so the operator knows it was left open on purpose.
        # Do NOT close the PR.
        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"This PR has mergeable work and no genuine conflict, so it was "
                f"LEFT OPEN for manual hand-merge rather than auto-closed. "
                f"Operator review required.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): comment failed on PR "
                f"#{pr_number}: {e}"
            )
        # Surface for the operator via the normal failure-alert channel — this
        # IS a manual-intervention event even though the work is salvageable.
        try:
            await _maybe_create_failure_alert(
                orig,
                session,
                f"PR #{pr_number} hit review-fix cap ({iterations} iterations, "
                f"{cap_label}) — left open with mergeable work, hand-merge required",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): alert failed for PR "
                f"#{pr_number}: {e}"
            )
        try:
            await _create_handmerge_attention_message(
                orig, pr_number, iterations, cap_label, session
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion(preserve): attention message failed "
                f"for PR #{pr_number}: {e}"
            )
        return

    orig.status = TaskStatus.FAILED
    # The approval gate is discretionary: only raise it when require_approval_gates
    # is enabled. With gates off the operator has opted out of manual gating, so the
    # capped child still FAILs (terminal → routes through the normal partial-rollup
    # path) but we must NOT set requires_approval. Otherwise rollup_management
    # escalates this child's flag onto the parent (rollup_management.py ~1086) and
    # services.py's 2h approval-gate timeout then FAILs the otherwise-healthy parent,
    # discarding its already-merged siblings.
    if _runtime_config.get("require_approval_gates", True):
        from app.admin_approval_authority import establish_independent_approval_hold

        establish_independent_approval_hold(
            orig,
            reason="review_cap_exhausted",
            owner="review_loop_convergence",
        )
    orig.latest_status_message = (
        f"Review-fix cycle reached limit ({iterations} iterations, {cap_label}) — "
        f"PR #{pr_number} auto-closed, manual review required"
        + (f" [{_lane_detail}]" if _lane_detail else "")
    )
    persist_failure_category(orig, CODE_ERROR)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "review_cap_exhausted"
    ctx["review_cap_iterations"] = iterations
    ctx["pr_rejected"] = True
    ctx["pr_rejection_note"] = (
        f"Auto-closed by Blueprint: review-fix cap exhausted after {iterations} iterations"
    )
    _terminal_detail = cap_label + (f"; {_lane_detail}" if _lane_detail else "")
    ctx["review_loop_terminal_outcome"] = {
        "kind": "cap_exhausted",
        "reason": "review_cap_exhausted",
        "pr_number": pr_number,
        "iterations": iterations,
        "scope": scope,
        "detail": _terminal_detail,
        "lane_counts": lane_counts or {},
        "recorded_at": _utcnow().isoformat(),
    }
    orig.context = ctx
    _set_review_loop_terminal_outcome(
        orig,
        kind="cap_exhausted",
        reason="review_cap_exhausted",
        pr_number=pr_number,
        iterations=iterations,
        scope=scope,
        detail=_terminal_detail,
    )
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to commit orig {resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

    # Surface cap-exhaustion to deploy_monitoring + direction health tracking.
    # Without this, cap failures bypass per-type failure gates and anomaly detection.
    record_task_result(
        success=False,
        failure_class=CODE_ERROR,
        task_id=str(orig.id),
        task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id,
            success=False,
            failure_class=CODE_ERROR,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    # Cap exhaustion bypasses the normal status-update handler; call
    # _record_remediation_if_terminal directly to keep direction state consistent.
    try:
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(orig, TaskStatus.FAILED, session)
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to record remediation outcome for PR #{pr_number}: {e}"
        )
    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            fingerprint = ((orig.context or {}).get("failure_fingerprints") or [None])[
                0
            ]
            fingerprint_note = (
                f"\n\nFailure fingerprint: `{fingerprint}`" if fingerprint else ""
            )
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                f"Auto-closed: review-fix cap reached ({iterations} iterations, {cap_label}). "
                f"Manual review required.{fingerprint_note}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Cap-exhaustion: failed to comment on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed PR #{pr_number} after cap exhaustion ({iterations} iterations)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )

    try:
        await _maybe_create_failure_alert(
            orig,
            session,
            f"PR #{pr_number} hit review-fix cap ({iterations} iterations, {cap_label}) — "
            f"auto-closed, manual review required",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Cap-exhaustion: failed to create alert for PR #{pr_number}: {e}"
        )


async def _handle_empty_effective_diff_at_merge(
    source_task: Task,
    resolved_original_id: str,
    pr_number: int,
    session: AsyncSession,
) -> None:
    """Terminal handler when a task PR reaches the merge gate with an EMPTY
    effective diff (``get_pr_files`` returned ``[]`` vs base).

    A code-fix/create PR whose effective diff went empty by merge time (its
    commits already landed on base via a sibling merge / rebase-flat) MUST NOT
    auto-merge and MUST NOT report success — merging it would mark the task
    COMPLETED ('CI passed + review approved, PR merged') while zero user-visible
    change landed (incident 2026-07-12, PR #14500 / commit 683abce38: an empty
    ``git show --stat`` merged 'green' with a title claiming a real fix).

    Instead: close the empty PR and land the chain original terminal with the
    benign, budget-exempt ``SCOPE_EMPTY`` failure class and
    ``failure_detail="empty_effective_diff_at_merge"``. ``pr_closed_empty=True``
    marks the close as an intentional empty no-op (not a review rejection or a
    merge failure) so downstream verification (rollup_pr_verification) does not
    mis-downgrade the outcome.

    Idempotent — re-entering on an already-terminal orig skips side effects.
    """
    from app.failure_classification import SCOPE_EMPTY, persist_failure_category
    from app.github import close_pull_request, create_pr_comment
    from app.routers.agent import get_github_token_for_task
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    source_task.latest_status_message = (
        f"Empty effective diff at merge for PR #{pr_number} — nothing to land; "
        f"not merging (no-op)"
    )
    session.add(source_task)

    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to load orig "
            f"{resolved_original_id[:8]}: {e}"
        )
        return
    if orig is None:
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: orig {resolved_original_id[:8]} not found"
        )
        return

    already_handled = orig.status in (
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ) and (orig.context or {}).get("failure_detail") == "empty_effective_diff_at_merge"
    if already_handled:
        logger.info(
            f"[PIPELINE] Empty-diff-at-merge re-triggered for PR #{pr_number} — "
            f"orig {resolved_original_id[:8]} already terminal, skipping side effects"
        )
        await session.commit()
        return

    logger.info(
        f"[PIPELINE] PR #{pr_number} reached the merge gate with an EMPTY effective "
        f"diff — landing orig {resolved_original_id[:8]} CANCELLED "
        f"(empty_effective_diff_at_merge / {SCOPE_EMPTY}), closing PR, NOT merging"
    )
    orig.status = TaskStatus.CANCELLED
    orig.latest_status_message = (
        f"PR #{pr_number} had an empty effective diff at merge — no change to land; "
        f"closed as a no-op instead of reporting a false success"
    )
    persist_failure_category(orig, SCOPE_EMPTY)
    ctx = dict(orig.context or {})
    ctx["failure_detail"] = "empty_effective_diff_at_merge"
    ctx["pr_closed_empty"] = True
    # NB: deliberately NOT setting pr_rejected — this is a benign no-op, not a
    # review rejection or a code failure.
    orig.context = ctx
    _set_review_loop_terminal_outcome(
        orig,
        kind="empty_effective_diff",
        reason="empty_effective_diff_at_merge",
        pr_number=pr_number,
        iterations=int((ctx.get("review_loop") or {}).get("iteration") or 0),
        detail="empty_effective_diff_at_merge",
    )
    session.add(orig)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to commit orig "
            f"{resolved_original_id[:8]}: {e}",
            exc_info=True,
        )
        return

    record_task_result(
        success=False,
        failure_class=SCOPE_EMPTY,
        task_id=str(orig.id),
        task_type=(orig.first_payload or {}).get("type", ""),
    )
    _direction_id = str((orig.context or {}).get("direction_id") or "")
    if _direction_id:
        record_direction_result(
            _direction_id,
            success=False,
            failure_class=SCOPE_EMPTY,
            project_id=str(orig.project_id) if orig.project_id else "",
        )

    try:
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(orig, TaskStatus.CANCELLED, session)
        await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to record remediation for "
            f"PR #{pr_number}: {e}"
        )

    try:
        gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
        try:
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                pr_number,
                "Auto-closed: this PR had an empty effective diff at merge time "
                "(its changes already landed on the base branch). Closing as a "
                "no-op rather than reporting a false success.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[PIPELINE] Empty-diff-at-merge: comment failed on PR #{pr_number}: {e}"
            )
        closed = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(orig.project_id)
        )
        if closed:
            logger.info(
                f"[PIPELINE] Closed empty-effective-diff PR #{pr_number} (no-op)"
            )
        else:
            logger.warning(
                f"[PIPELINE] close_pull_request returned False for empty PR #{pr_number}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PIPELINE] Empty-diff-at-merge: failed to close PR #{pr_number}: {e}",
            exc_info=True,
        )
