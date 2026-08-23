"""Bounded re-decision for the approval-timeout hand-merge preserve latch.

``recovery_evaluating_approval._handle_approval_gate_timeout`` preserves an
approval-gate-timed-out parent whose PR is still worth keeping by stamping
``context.approval_timeout_preserved_for_handmerge = True``.

That flag USED TO BE A ONE-WAY DOOR. The handler's idempotent re-entry check
returned early on every later sweep, so the row sat ``EVALUATING`` with
``requires_approval=True`` FOREVER, waiting for a human to press Merge on a PR
the engine had ALREADY PROVEN landable — live instance 2026-08-23: task
``129fb995`` / PR #17720, held since 09:22 UTC, ``MERGEABLE`` /
``mergeStateStatus: CLEAN``, every required check green, no conflict. The
engine measured the answer and then handed the question back.

This module gives the latch a SECOND, BOUNDED window and a real disposition
when it expires:

  1. **PR already merged** -> reconcile to the normal merged terminal via
     :func:`reconcile_merged_approval_hold` — the SAME
     ``_apply_rollup_merge_classification`` sequence the handler's merged-PR
     branch runs (that branch now calls this helper too, so the two paths
     cannot drift).
  2. **PR open and provably green-and-clean** — ``mergeable is True`` AND
     ``mergeable_state == "clean"``. GitHub's ``clean`` means mergeable, every
     required check passing and no blocking review. MERGE IT, through the
     repo's one merge primitive and behind the shared Alembic pre-merge
     combined-chain guard, then reconcile exactly as (1). Deliberately strict:
     ``unstable`` (a check is red), ``blocked``, ``behind``, ``dirty``,
     ``unknown``, ``''`` and any lookup failure are NOT this case.
  3. **Anything else** — open-but-not-clean, closed-unmerged, or an unreadable
     PR. Destroy nothing on this pass: re-arm the clock for one more window and
     record WHY. After :data:`HANDMERGE_REDECIDE_MAX_REARMS` re-arms the caller
     falls through to the pre-existing terminalization path so the chain
     reaches a terminal instead of sitting forever — the "cull the dud" leg.

FAIL-SAFE SPLIT (the one thing that must not regress): a GitHub OUTAGE must
never cull in bulk. A lookup/merge FAILURE re-arms WITHOUT consuming a re-arm
budget slot; only a SUCCESSFUL read of a genuinely-not-landable PR consumes
one. An install-token-exhaustion window therefore costs latency, never PRs —
the same invariant ``_PreservabilityUnavailable`` exists to protect upstream.

LEGACY ROWS: a latch written before this module existed carries no
``approval_timeout_preserved_at`` stamp. A missing stamp must NOT read as
"never expires" (that is the wedge again), so the first sweep that sees one
BACKFILLS the stamp and starts the clock from there. It delays the decision by
one window rather than deciding on evidence we do not have.

Every branch records WHY on ``context.approval_timeout_handmerge_decision``
(and logs it) so the disposition is inspectable without re-deriving it.
"""

import logging
from datetime import datetime

# NOTE: every ``app.*`` import in this module is deliberately LAZY (inside the
# functions that need it). ``app.services`` imports the recovery sweeps, which
# import ``recovery_evaluating`` -> ``recovery_evaluating_approval`` -> this
# module, so a module-level ``from app import services`` here makes importing
# this module FIRST a partially-initialized circular import. Keeping the
# top-level imports stdlib-only makes this module safe to import from anywhere.

logger = logging.getLogger(__name__)

# The preserve latch itself (written by the approval-gate handler).
HANDMERGE_LATCH_KEY = "approval_timeout_preserved_for_handmerge"
# When the latch was (re-)armed. Elapsed time is unmeasurable without it, which
# is exactly why a missing value is BACKFILLED rather than treated as eternal.
HANDMERGE_LATCH_STAMPED_AT_KEY = "approval_timeout_preserved_at"
# How many bounded re-arms this row has consumed. Only a SUCCESSFUL read of a
# not-landable PR increments it.
HANDMERGE_LATCH_REARM_KEY = "approval_timeout_handmerge_rearms"
# Last disposition + reason, for operators and for the next sweep.
HANDMERGE_LATCH_DECISION_KEY = "approval_timeout_handmerge_decision"

# Second bounded window before the engine RE-DECIDES a preserved latch.
# Defaulted here IN CODE (not behind a host env var, not default-off): the
# runtime-config key ``handmerge_redecide_window_hours`` merely lets an
# operator retune the live value, and its default IS this constant.
HANDMERGE_REDECIDE_WINDOW_HOURS = 6
HANDMERGE_REDECIDE_WINDOW_CONFIG_KEY = "handmerge_redecide_window_hours"
# Bounded number of re-arms before the caller terminalizes. Small on purpose:
# a PR that is still not landable after this many full windows is a dud, and
# nursing it is the behaviour this module exists to end.
HANDMERGE_REDECIDE_MAX_REARMS = 3


def resolve_handmerge_redecide_seconds(runtime_config) -> int:
    """Re-decision window in seconds, defaulting IN CODE to the active value.

    Mirrors the defensive read the sibling spend-approval window uses: a
    corrupted ``runtime_config_json`` must retune nothing and must never abort
    the sweep cycle.
    """
    try:
        raw = (runtime_config or {}).get(
            HANDMERGE_REDECIDE_WINDOW_CONFIG_KEY, HANDMERGE_REDECIDE_WINDOW_HOURS
        )
        hours = int(raw)
        if not (1 <= hours <= 168):
            hours = HANDMERGE_REDECIDE_WINDOW_HOURS
    except (ValueError, TypeError, AttributeError):
        hours = HANDMERGE_REDECIDE_WINDOW_HOURS
    return hours * 3600


def _write_decision(task, ctx: dict, *, outcome: str, why: str, now) -> dict:
    """Stage the WHY breadcrumb on ``task.context`` and return the new ctx."""
    from sqlalchemy.orm.attributes import flag_modified

    ctx[HANDMERGE_LATCH_DECISION_KEY] = {
        "at": now.isoformat(),
        "outcome": outcome,
        "why": why,
        "rearms": int(ctx.get(HANDMERGE_LATCH_REARM_KEY, 0) or 0),
        "max_rearms": HANDMERGE_REDECIDE_MAX_REARMS,
    }
    task.context = ctx
    flag_modified(task, "context")
    return ctx


async def reconcile_merged_approval_hold(
    task,
    session,
    *,
    pr_number: int,
    pr_data: dict,
    source: str,
    msg_terminal: str,
    msg_non_terminal: str,
    log_detail: str,
    merge_commit_sha: str | None = None,
) -> None:
    """Reconcile an approval-held parent whose PR is MERGED to its normal
    merged/completed terminal.

    Extracted verbatim from the approval-gate handler's merged-PR branch so the
    latch re-decision path lands on the exact same terminal instead of
    inventing a second one. A merged rollup is a SUCCESS: never
    ``classify_failure`` / ``record_task_result`` / failure alert.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app import services
    from app.admin_approval_authority import (
        approval_hold_provenance,
        consume_approval_hold_metadata,
        revoke_operator_approval_followup,
    )
    from app.closed_pr_reconciliation import revoke_review_wait_approval_hold
    from app.models import Project
    from app.rollup_state import _clear_rollup_transient_state
    from app.recovery_evaluating_helpers import _apply_rollup_merge_classification

    proj = await session.get(Project, task.project_id)
    # Clear the approval hold — the work landed; this is terminal.
    task.requires_approval = False
    revoke_review_wait_approval_hold(task)
    revoke_operator_approval_followup(task)
    consume_approval_hold_metadata(
        task,
        expected_episode=approval_hold_provenance(task),
    )
    merged_ctx = _clear_rollup_transient_state({**(task.context or {})})
    merged_ctx["rollup_pr_number"] = int(pr_number)
    task.context = merged_ctx
    await _apply_rollup_merge_classification(
        task,
        proj,
        session,
        pr_number=int(pr_number),
        merged_base_branch=((pr_data.get("base") or {}).get("ref")),
        source=source,
        msg_terminal=msg_terminal,
        msg_non_terminal=msg_non_terminal,
        hotfix_lane=None,
        merge_commit_sha=(merge_commit_sha or pr_data.get("merge_commit_sha")),
    )
    flag_modified(task, "context")
    session.add(task)
    await session.commit()
    logger.info(
        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} {log_detail} — reconciled to "
        f"{task.status} (not FAILED)"
    )
    await services.broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": task.status},
    )


async def _merge_proven_clean_pr(task, session, pr_number: int, pr_data: dict):
    """Merge a PR this sweep has just PROVEN green-and-clean.

    Returns ``(merge_result | None, why, consumes_rearm_budget)``.

    Raises on transport/credential trouble — the caller treats an exception as
    a NON-budget-consuming re-arm, so a GitHub outage cannot cull anything.
    A *definite* refusal (the Alembic guard says no, or GitHub answers
    ``merged: false``) is a real answer about this PR and DOES consume a slot.
    """
    from app import services
    from app.github import merge_pull_request
    from app.models import Project
    from app.routers.agent import get_github_token_for_task
    from app.rollup_migration_validation import enforce_premerge_migration_guard
    from app.shared_state import get_project_promotion_target

    base_branch = (pr_data.get("base") or {}).get("ref")
    head_sha = (pr_data.get("head") or {}).get("sha")
    gh_token, owner, repo = await services._bounded(
        get_github_token_for_task(task.id, session),
        services._GH_CALL_TIMEOUT_S,
        what=f"handmerge-latch merge token {str(task.id)[:8]}",
    )
    project = await session.get(Project, task.project_id)
    _promotion_target = (
        get_project_promotion_target(project) if project is not None else None
    )
    # NOT THIS PATH'S CALL. A PR based on the promotion target is a PROMOTION,
    # and promotion authority does not live in GitHub's mergeability answer --
    # it lives in the release qualifier (composite score, audit, test_erosion).
    # ``mergeStateStatus: CLEAN`` only says branch protection is satisfied, so
    # merging here would land unqualified work on the deploy branch while
    # reporting that a green check authorized it. The re-arm below consumes
    # budget deliberately: this refusal is a permanent fact about the PR, not a
    # transient one, so the row must reach a terminal instead of re-deciding
    # the same thing forever. Terminalizing leaves the PR OPEN for the release
    # lane, which is the surface that owns it.
    if base_branch and _promotion_target and base_branch == _promotion_target:
        return (
            None,
            f"PR #{pr_number} targets the promotion branch "
            f"{base_branch!r}; landing it is a promotion and belongs to the "
            f"release qualifier, not to an approval-timeout sweep",
            True,
        )
    # Same combined-chain Alembic guard every other Blueprint-owned merge site
    # runs: an autonomous merge must never be the one that puts a duplicate
    # revision / dual head onto the integration trunk.
    _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
        gh_token,
        owner,
        repo,
        int(pr_number),
        base_branch,
        promotion_target=_promotion_target,
    )
    if not _ok_to_merge:
        return (
            None,
            f"the Alembic pre-merge combined-chain guard refused PR "
            f"#{pr_number}: {_hold_reason}",
            True,
        )
    merge_result = await services._bounded(
        merge_pull_request(
            gh_token,
            owner,
            repo,
            int(pr_number),
            project_id=str(task.project_id),
            expected_head_sha=head_sha,
        ),
        services._GH_CALL_TIMEOUT_S,
        what=f"handmerge-latch merge PR #{pr_number}",
    )
    if not (merge_result or {}).get("merged"):
        return (
            None,
            f"GitHub declined the merge of PR #{pr_number}: "
            f"{(merge_result or {}).get('message') or merge_result!r}",
            True,
        )
    return (merge_result, f"merged PR #{pr_number}", False)


async def _rearm_or_cull(
    task,
    session,
    now,
    *,
    outcome: str,
    why: str,
    consumes_budget: bool,
) -> bool:
    """Re-arm the latch clock for one more window, or give up and terminalize.

    Returns ``True`` when the row was re-armed (caller keeps it non-terminal)
    and ``False`` when the re-arm budget is spent and the caller must fall
    through to its existing terminalization path.
    """
    ctx = dict(task.context or {})
    rearms = int(ctx.get(HANDMERGE_LATCH_REARM_KEY, 0) or 0)

    if consumes_budget and rearms >= HANDMERGE_REDECIDE_MAX_REARMS:
        # Replace the preserve breadcrumb: this row is no longer "left open for
        # hand-merge", it is being terminalized, and the terminal must say so.
        ctx["failure_detail"] = "approval_timeout_handmerge_rearm_budget_exhausted"
        _write_decision(
            task,
            ctx,
            outcome="rearm_budget_exhausted",
            why=(
                f"{why}; the re-decision budget of {HANDMERGE_REDECIDE_MAX_REARMS} "
                f"re-arms is spent, so this hold terminalizes instead of being "
                f"nursed further"
            ),
            now=now,
        )
        session.add(task)
        await session.commit()
        logger.warning(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch: "
            f"{HANDMERGE_REDECIDE_MAX_REARMS}/{HANDMERGE_REDECIDE_MAX_REARMS} "
            f"re-arms spent and the PR is still not landable ({why}) — "
            f"terminalizing"
        )
        return False

    if consumes_budget:
        rearms += 1
        ctx[HANDMERGE_LATCH_REARM_KEY] = rearms
    ctx[HANDMERGE_LATCH_STAMPED_AT_KEY] = now.isoformat()
    _write_decision(task, ctx, outcome=outcome, why=why, now=now)
    session.add(task)
    await session.commit()
    logger.info(
        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch re-armed "
        f"({outcome}, budget {rearms}/{HANDMERGE_REDECIDE_MAX_REARMS}"
        f"{'' if consumes_budget else ', NOT consumed — unreadable, not unlandable'}"
        f"): {why}"
    )
    return True


async def redecide_preserved_handmerge_latch(
    task,
    ctx,
    session,
    now,
    *,
    window_seconds: int,
) -> bool:
    """Second-window re-decision for a preserved hand-merge latch.

    Returns ``True`` when the row is handled for this sweep (still inside the
    window, freshly stamped, reconciled, merged, or re-armed) and ``False``
    only when the re-arm budget is exhausted and the caller must fall through
    to its existing terminalization path.
    """
    ctx = dict(task.context or ctx or {})
    stamped_at = ctx.get(HANDMERGE_LATCH_STAMPED_AT_KEY)

    if not stamped_at:
        # LEGACY ROW (or a corrupted stamp, below): start the clock now. A
        # missing stamp must not mean "never expires" — that is the wedge this
        # module removes — but nor may it mean "expired long ago", because we
        # have no evidence of when the latch was set. Fails safe: one extra
        # window of delay, never a decision on absent evidence.
        ctx[HANDMERGE_LATCH_STAMPED_AT_KEY] = now.isoformat()
        _write_decision(
            task,
            ctx,
            outcome="stamp_backfilled",
            why=(
                "pre-existing preserve latch carried no timestamp; the "
                "re-decision clock starts now rather than treating the latch "
                "as permanent"
            ),
            now=now,
        )
        session.add(task)
        await session.commit()
        logger.info(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch had no "
            f"timestamp — backfilled; it will be re-decided in "
            f"{round(window_seconds / 3600, 1)}h"
        )
        return True

    try:
        held_seconds = (now - datetime.fromisoformat(stamped_at)).total_seconds()
    except (ValueError, TypeError):
        ctx[HANDMERGE_LATCH_STAMPED_AT_KEY] = now.isoformat()
        _write_decision(
            task,
            ctx,
            outcome="stamp_repaired",
            why=(
                f"preserve-latch timestamp {stamped_at!r} is unparseable; "
                f"re-stamping instead of deciding on an unreadable clock"
            ),
            now=now,
        )
        session.add(task)
        await session.commit()
        return True

    if held_seconds <= window_seconds:
        # Inside the second window — the operator still owns it. Nothing to
        # decide, and deliberately no repeat alert.
        return True

    from app.recovery_evaluating_prless_hold import _approval_hold_pr_number

    held_hours = round(held_seconds / 3600, 1)
    pr_number = _approval_hold_pr_number(task, ctx)
    if not pr_number:
        return await _rearm_or_cull(
            task,
            session,
            now,
            outcome="no_pr_number",
            why=(
                "the preserved hold no longer resolves to any PR number, so "
                "there is nothing to re-read or merge"
            ),
            consumes_budget=True,
        )

    pr_data = None
    read_error: str | None = None
    try:
        from app import services
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task

        gh_token, owner, repo = await services._bounded(
            get_github_token_for_task(task.id, session),
            services._GH_CALL_TIMEOUT_S,
            what=f"handmerge-latch token {str(task.id)[:8]}",
        )
        pr_data = await services._bounded(
            get_pull_request(gh_token, owner, repo, int(pr_number)),
            services._GH_CALL_TIMEOUT_S,
            what=f"handmerge-latch get_pull_request #{pr_number}",
        )
    except Exception as exc:  # noqa: BLE001 — classified below, never fatal
        read_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch could not "
            f"read PR #{pr_number} ({read_error}) — re-arming WITHOUT spending "
            f"re-arm budget; an unreadable PR is not an unlandable one",
            exc_info=True,
        )

    if pr_data is None:
        # A FAILURE TO READ IS NOT AN ANSWER. Re-arm without spending budget so
        # a GitHub outage / drained install-token bucket can never cull a fleet
        # of perfectly landable PRs.
        return await _rearm_or_cull(
            task,
            session,
            now,
            outcome="pr_read_failed",
            why=(
                f"live read of PR #{pr_number} failed "
                f"({read_error or 'no data returned'}); re-arming without "
                f"spending re-arm budget"
            ),
            consumes_budget=False,
        )

    if bool(pr_data.get("merged")):
        await reconcile_merged_approval_hold(
            task,
            session,
            pr_number=int(pr_number),
            pr_data=pr_data,
            source="recovery_evaluating.handmerge_latch_already_merged",
            msg_terminal=(
                f"✓ PR #{pr_number} merged; reconciled from the preserved "
                f"hand-merge latch after {held_hours}h (landed to {{landed}})"
            ),
            msg_non_terminal=(
                f"Hand-merge latch reconcile: PR #{pr_number} merged into "
                f"{{landed}}; awaiting downstream landing"
            ),
            log_detail=(
                f"hand-merge latch re-decision after {held_hours}h — PR "
                f"#{pr_number} was ALREADY MERGED"
            ),
        )
        return True

    state = (pr_data.get("state") or "").lower()
    mergeable = pr_data.get("mergeable")
    mergeable_state = (pr_data.get("mergeable_state") or "").lower()
    provably_landable = (
        state == "open" and mergeable is True and mergeable_state == "clean"
    )

    if provably_landable:
        try:
            merge_result, merge_why, merge_consumes = await _merge_proven_clean_pr(
                task, session, int(pr_number), pr_data
            )
        except Exception as exc:  # noqa: BLE001 — transport trouble, not a verdict
            logger.warning(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch merge "
                f"of PR #{pr_number} errored ({type(exc).__name__}: {exc}) — "
                f"re-arming WITHOUT spending re-arm budget",
                exc_info=True,
            )
            return await _rearm_or_cull(
                task,
                session,
                now,
                outcome="auto_merge_errored",
                why=(
                    f"merging the proven-clean PR #{pr_number} raised "
                    f"{type(exc).__name__}: {exc}; a failed attempt is not "
                    f"evidence the PR is unlandable"
                ),
                consumes_budget=False,
            )
        if merge_result is not None:
            logger.info(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} hand-merge latch: PR "
                f"#{pr_number} was still green-and-clean after {held_hours}h — "
                f"MERGED IT instead of waiting for a human to press the button"
            )
            await reconcile_merged_approval_hold(
                task,
                session,
                pr_number=int(pr_number),
                pr_data=pr_data,
                source="recovery_evaluating.handmerge_latch_auto_merged",
                msg_terminal=(
                    f"✓ PR #{pr_number} auto-merged by the hand-merge latch "
                    f"re-decision after {held_hours}h (landed to {{landed}})"
                ),
                msg_non_terminal=(
                    f"Hand-merge latch auto-merge: PR #{pr_number} merged into "
                    f"{{landed}}; awaiting downstream landing"
                ),
                log_detail=(
                    f"hand-merge latch AUTO-MERGED proven-clean PR #{pr_number} "
                    f"after {held_hours}h"
                ),
                merge_commit_sha=merge_result.get("sha"),
            )
            return True
        return await _rearm_or_cull(
            task,
            session,
            now,
            outcome="auto_merge_declined",
            why=merge_why,
            consumes_budget=merge_consumes,
        )

    return await _rearm_or_cull(
        task,
        session,
        now,
        outcome="not_landable",
        why=(
            f"PR #{pr_number} read cleanly but is not provably landable "
            f"(state={state!r}, mergeable={mergeable!r}, "
            f"mergeable_state={mergeable_state!r}); only mergeable+'clean' is "
            f"merged automatically"
        ),
        consumes_budget=True,
    )
