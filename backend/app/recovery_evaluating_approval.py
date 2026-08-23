"""Approval-gate hard-timeout recovery handler, extracted from
``app.recovery_evaluating``.

A single self-contained per-row recovery branch lifted out of the
``_check_evaluating_timeouts`` Phase-B loop. It terminalizes EVALUATING parents
flagged ``requires_approval=True`` that otherwise stall indefinitely waiting for
human approval — the ``evaluating_fail_hours`` backstop can be deferred by an
active pipeline chain, so approval-gated parents need their own companion
timeout.

TWO disposition paths, keyed on ``context.approval_hold_reason``:

  * DELIBERATE spend/budget hold (== ``SPEND_BUDGET_APPROVAL_HOLD_REASON``): a
    longer, operator-configurable window resolving to a CLEAN NON-FAILURE
    ``CANCELLED`` terminal on expiry — a cost decision an operator simply has
    not reached is not a pipeline failure. This path NEVER records a failed
    task result, failure category, or failure alert.
  * Everything else (no marker / review-escalation churn): the existing
    2h → ``FAILED`` behavior, with classification, failure alert, and a bounded
    defer while the review/fix chain is still actively working the PR.

Inside that second path the disposition forks AGAIN on whether the hold has a PR
behind it. Every non-destructive branch below — the merged-PR reconcile, the
churn-halt respawn, the preserve-for-handmerge latch — derives a PR number first
and no-ops without one, so an approval hold carrying NO PR at all used to fall
straight through to ``FAILED``. For that shape the task row IS the work (nothing
is durable on GitHub), which makes FAILing it the one disposition that actually
destroys something. See ``app.recovery_evaluating_prless_hold`` (split out
only to respect this module's size cap) — incident: research task ``70c5ec8f``.

The preserve-for-handmerge latch is a BOUNDED park, not a one-way door. See
``app.recovery_evaluating_handmerge_latch``: after a second configurable window
the engine re-reads the PR and DECIDES — reconcile an already-merged PR, MERGE
one that is still provably green-and-clean, or re-arm a bounded number of times
before falling through to the terminalization below.

``_handle_approval_gate_timeout`` takes the per-row locals it needs (``task``,
``ctx``, ``session``, ``now``) plus the runtime-config-derived spend-window
values and returns ``True`` when it handled the row (the caller ``continue``s)
or ``False`` to fall through to the remaining recovery branches unchanged
(``requires_approval`` is False, or a non-spend hold still inside its 2h
window). The move is purely structural — every commit / notification /
classification semantic is preserved byte-for-byte. ``services.*`` refs and the
function-local ``app.failure_classification`` / ``app.routers.agent_status``
imports are kept unchanged so test patches against ``app.services.<name>``,
``app.routers.agent_status.record_task_result``, and
``app.alert_dispatch._maybe_create_failure_alert`` still take effect.
"""

import contextlib
import logging
from datetime import datetime

from app import services
from app.admin_approval_authority import (
    approval_hold_provenance,
    consume_approval_hold_metadata,
    revoke_operator_approval_followup,
)
from app.closed_pr_reconciliation import revoke_review_wait_approval_hold
from app.models import TaskStatus
from app.recovery_evaluating_handmerge_latch import (
    HANDMERGE_LATCH_STAMPED_AT_KEY,
    HANDMERGE_REDECIDE_MAX_REARMS,
    HANDMERGE_REDECIDE_WINDOW_HOURS,
    reconcile_merged_approval_hold,
    redecide_preserved_handmerge_latch,
)
from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON
from app.recovery_evaluating_prless_hold import (
    _approval_hold_pr_number,
    _hold_pr_less_approval_gate_timeout,
)
from app.recovery_evaluating_helpers import (
    _approval_gate_chain_busy,
    _dispatch_spend_hold_notification,
    _emit_spend_hold_created_signals,
)

logger = logging.getLogger(__name__)

# Approval-gate companion timeout: parents flagged requires_approval can
# otherwise stall indefinitely when _chain_has_active_pipeline_work defers
# the EVALUATING_FAIL backstop. 2h gives operators a reasonable window
# to approve/deny independent of the configured wall-clock.
APPROVAL_GATE_FAIL_SECONDS = 2 * 3600
# Bounded defer window for the approval-gate timeout while the chain is
# STILL actively working the PR (live review/fix children, or a fix parked
# in CI-wait). A stuck/standoff escalation flags the parent but does NOT
# stop the loop — FAILing the parent at the bare 2h mark while its own fix
# chain is mid-flight orphans that work and strands the rollup (research
# 6506582b / PR #9179, 2026-06-09). Mirrors CI_BACKSTOP_DEFER_WINDOW_HOURS;
# beyond 2h + this window the original unconditional timeout applies, so a
# genuinely wedged chain still terminates.
APPROVAL_GATE_DEFER_WINDOW_HOURS = 6

# Bounded automatic-respawn cap for supersession_churn_halt holds (see
# _respawn_review_after_churn_halt below). Persisted forever on
# ``_churn_halt_respawn_count`` — deliberately never reset — so a task that
# keeps re-triggering the fence over its lifetime gets at most this many
# free automatic second chances before landing on the existing, explicit,
# admin-visible preserve-for-handmerge/FAILED disposition. That disposition
# is itself the "operator-only terminal tier": requires_approval plus the
# review_loop_terminal_outcome breadcrumb are both queryable, so exhausting
# the cap is never a silently-vanishing state, just a human-owned one.
_CHURN_HALT_AUTO_RESPAWN_MAX = 2


async def _handle_approval_gate_timeout(
    task,
    ctx,
    session,
    now,
    *,
    spend_approval_fail_seconds,
    spend_hours,
    maybe_create_failure_alert,
    handmerge_redecide_seconds: int = HANDMERGE_REDECIDE_WINDOW_HOURS * 3600,
) -> bool:
    """Approval-gate hard timeout for EVALUATING parents.

    Returns ``True`` when the row was handled (caller ``continue``s) and
    ``False`` to fall through to the remaining recovery branches — namely
    when ``task.requires_approval`` is False, or a non-spend approval hold is
    still within its 2h window.
    """
    if not task.requires_approval:
        return False

    from sqlalchemy.orm.attributes import flag_modified

    is_spend_hold = ctx.get("approval_hold_reason") == SPEND_BUDGET_APPROVAL_HOLD_REASON

    approval_set_at = ctx.get("_approval_set_at")
    if not approval_set_at:
        new_ctx = {**ctx, "_approval_set_at": now.isoformat()}
        # MAKE THE HOLD LOUD: when a spend/budget hold is first
        # seen, fire the created notification + a non-failure
        # WARNING alert, exactly once (guarded by the flag).
        if is_spend_hold and not ctx.get("_spend_hold_created_notified"):
            await _emit_spend_hold_created_signals(
                task, session, window_hours=spend_hours
            )
            new_ctx["_spend_hold_created_notified"] = True
        task.context = new_ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return True
    try:
        approval_since = datetime.fromisoformat(approval_set_at)
        approval_elapsed = (now - approval_since).total_seconds()
    except (ValueError, TypeError):
        approval_elapsed = 0.0

    if is_spend_hold:
        # Fire the EXPIRING warning once, within the lead window
        # (last 20% of the configured window, min 1h) before the
        # hold expires — so the operator gets an active nudge.
        warn_lead = max(spend_approval_fail_seconds * 0.2, 3600.0)
        if (
            not ctx.get("_spend_hold_expiring_notified")
            and spend_approval_fail_seconds - warn_lead
            <= approval_elapsed
            < spend_approval_fail_seconds
        ):
            hours_remaining = round(
                (spend_approval_fail_seconds - approval_elapsed) / 3600,
                1,
            )
            _dispatch_spend_hold_notification(
                task,
                "SPEND_HOLD_EXPIRING",
                spend_hours,
                hours_remaining=hours_remaining,
            )
            task.context = {
                **ctx,
                "_spend_hold_expiring_notified": True,
            }
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return True
        if approval_elapsed > spend_approval_fail_seconds:
            hours = round(approval_elapsed / 3600, 1)
            logger.info(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} "
                f"spend/budget approval not granted in {hours}h — "
                f"declining (CANCELLED, not a failure)"
            )
            task.status = TaskStatus.CANCELLED
            task.latest_status_message = (
                "Spend/budget approval not granted in time — declined (not a failure)"
            )
            # Clear the hold so it leaves the digest needs-
            # attention list; the task is terminal now. This path
            # deliberately does NOT call record_task_result /
            # classify_failure / _maybe_create_failure_alert — it
            # must not be counted as a task failure.
            task.requires_approval = False
            revoke_review_wait_approval_hold(task)
            revoke_operator_approval_followup(task)
            consume_approval_hold_metadata(
                task,
                expected_episode=approval_hold_provenance(task),
            )
            task.context = {
                **(task.context or {}),
                "spend_approval_declined": True,
            }
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            await services.broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": TaskStatus.CANCELLED,
                },
            )
            return True
        # Within window, not yet at the warning lead — leave
        # EVALUATING for a later sweep. A spend/budget hold never
        # falls through to the review-escalation FAILED path.
        return True

    if approval_elapsed > APPROVAL_GATE_FAIL_SECONDS:
        # Defer (bounded) while the chain is still actively
        # working the PR: live review/fix children, or a fix
        # parked in CI-wait. The two other backstops in this
        # sweep already consult the chain before terminalizing;
        # the approval gate was the one that did not.
        if (
            approval_elapsed
            <= APPROVAL_GATE_FAIL_SECONDS + APPROVAL_GATE_DEFER_WINDOW_HOURS * 3600
        ):
            _approval_chain_busy = False
            try:
                _approval_chain_busy = await services._chain_has_active_pipeline_work(
                    task, session
                ) or await _approval_gate_chain_busy(task, session)
            except Exception:
                logger.debug(
                    f"[EVAL TIMEOUT] Approval-gate chain check "
                    f"failed for {str(task.id)[:8]}",
                    exc_info=True,
                )
            if _approval_chain_busy:
                logger.info(
                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                    f"the approval-gate window but its review/fix "
                    f"chain is still working the PR — deferring "
                    f"(hard ceiling "
                    f"{round(APPROVAL_GATE_FAIL_SECONDS / 3600 + APPROVAL_GATE_DEFER_WINDOW_HOURS, 1)}h)"
                )
                return True
        hours = round(approval_elapsed / 3600, 1)

        # CREDIT-BLINDNESS GUARD: before FAILing, check whether the parent's
        # rollup PR has ALREADY MERGED. The preserve block below only handles
        # an OPEN mergeable PR — an already-merged PR is not "preservable", so
        # without this guard control falls through to FAILED and marks landed
        # work as a failure, costing the direction its merge credit (real
        # incident: research be331963 / PR #14342 merged, then FAILED 36s
        # later). A merged rollup is a SUCCESS: reconcile to COMPLETED via the
        # canonical rollup-merge path (never classify_failure/record_task_result/
        # failure alert).
        # Derived through the WIDE accessor, same as the PR-less predicate
        # below. These preserve/reconcile paths used to read only
        # ``rollup_pr_number or first_payload.pr_number``, so a task whose PR
        # identity lived only at ``context.review_loop.pr_number`` (every
        # ``arm_ci_wait`` co-stamps it there; see ci_merge_helpers.
        # effective_pr_number for the identity split-brain this mirrors) was
        # invisible to all three of them. Worse than "no preserve": the wide
        # predicate at the PR-less branch DID see that number, so the PR-less
        # hold declined too, and the row fell through BOTH safety nets to
        # FAILED with no hold at all. One derivation, so they agree.
        _merged_pr_num = _approval_hold_pr_number(task, ctx)
        if _merged_pr_num:
            _merged_pr_data = None
            try:
                from app.routers.agent import get_github_token_for_task
                from app.github import get_pull_request

                _gh_tok, _gh_owner, _gh_repo = await services._bounded(
                    get_github_token_for_task(task.id, session),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"approval-gate merged-check token {str(task.id)[:8]}",
                )
                _merged_pr_data = await services._bounded(
                    get_pull_request(
                        _gh_tok, _gh_owner, _gh_repo, int(_merged_pr_num)
                    ),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"approval-gate get_pull_request #{_merged_pr_num}",
                )
            except Exception:
                logger.debug(
                    f"[EVAL TIMEOUT] Approval-gate merged-PR check failed for "
                    f"{str(task.id)[:8]} PR #{_merged_pr_num} — falling through "
                    f"to preserve/FAIL",
                    exc_info=True,
                )
            if _merged_pr_data is not None and bool(_merged_pr_data.get("merged")):
                # Shared with the hand-merge latch's re-decision path so the
                # two merged-PR reconciles cannot drift apart.
                await reconcile_merged_approval_hold(
                    task,
                    session,
                    pr_number=int(_merged_pr_num),
                    pr_data=_merged_pr_data,
                    source="recovery_evaluating.approval_gate_merged",
                    msg_terminal=(
                        f"✓ rollup PR #{_merged_pr_num} merged; reconciled "
                        f"from approval-gate timeout (landed to {{landed}})"
                    ),
                    msg_non_terminal=(
                        f"Approval-gate reconcile: rollup PR #{_merged_pr_num} "
                        f"merged into {{landed}}; awaiting downstream landing"
                    ),
                    log_detail=(
                        f"approval-gate timeout after {hours}h — rollup PR "
                        f"#{_merged_pr_num} ALREADY MERGED"
                    ),
                )
                return True

        # BOUNDED AUTO-RESPAWN for supersession_churn_halt holds specifically.
        # Unlike stuck/standoff/repeat_fingerprint_halt (which flag the parent
        # but leave the review/fix chain RUNNING — see
        # review_loop_convergence._RESOLVABLE_ESCALATION_GATE_KINDS), a
        # supersession_churn_halt CANCELS every in-flight review/fix task on
        # the PR (review_pipeline.py churn-halt block). Nothing is left
        # running to ever produce the APPROVE or merge that
        # clear_review_escalation_approval_gate waits for, so without this
        # branch the loop fell through to the one-way
        # approval_timeout_preserved_for_handmerge latch (or FAILED) and sat
        # dead until an operator manually respawned a review (incident: PR
        # #17419, 2026-08-18 — days of silent stranding). Give the loop a
        # bounded number of automatic fresh attempts first; the existing
        # preserve/FAILED dispositions below still apply once the cap is
        # exhausted, so a genuine infinite-churn PR still terminates — this
        # only recovers a loop whose actual blocker (a gate bug, a missing
        # runner capability, ...) was fixed after the halt tripped.
        if ctx.get("approval_hold_reason") == "supersession_churn_halt":
            _churn_respawn_count = int(ctx.get("_churn_halt_respawn_count", 0))
            if _churn_respawn_count < _CHURN_HALT_AUTO_RESPAWN_MAX:
                if await _respawn_review_after_churn_halt(task, session):
                    return True
                # Respawn declined (PR merged/closed meanwhile, cap reached,
                # branch/PR number unrecoverable, ...) — fall through to the
                # existing preserve/FAILED disposition below unchanged.

        # A rollup_lineage_fence descendant-block hold whose flagged blockers
        # have ALL resolved out-of-band (subject PR merged/closed, or the
        # PR-less descendant completed) is RELEASED here instead of killed:
        # the fence's only other release path is an operator requeue of the
        # flagged descendant, so a self-resolved blocker left the hold in
        # place forever. Incident 2026-08-19: PR #17419 merged at 02:15Z, yet
        # this timeout FAILED ancestor 289809a3 at 02:18Z and the requeued
        # clone re-inherited the stale hold. Conservative: any unknown PR
        # state keeps the hold and falls through to preserve/FAILED below.
        try:
            from app.rollup_lineage_fence import (
                release_resolved_descendant_operator_blocks,
            )

            _fence_released = await release_resolved_descendant_operator_blocks(
                task, session
            )
        except Exception:  # noqa: BLE001 — release is best-effort recovery
            _fence_released = False
            logger.debug(
                f"[EVAL TIMEOUT] Fence-block release check failed for "
                f"{str(task.id)[:8]}",
                exc_info=True,
            )
        if _fence_released:
            logger.info(
                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} approval-gate "
                f"timeout after {hours}h — descendant operator block(s) "
                f"resolved out-of-band, hold RELEASED instead of failing"
            )
            await session.commit()
            await services.broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {"task_id": str(task.id), "status": task.status},
            )
            return True

        # A fence hold is a MACHINE-set dependency, never a human question.
        # ``rollup_lineage_fence.mark_ancestor_rollups_awaiting_operator``
        # establishes it autonomously to encode "a descendant of mine needs
        # intervention" — a blocker with its own lifecycle, on its own
        # clock. Running the generic 2h *human*-approval timeout against it
        # FAILs the ancestor while the blocker is still moving: it resolves
        # nothing, and it destroys the rollup. Incident 2026-08-23: research
        # 70c5ec8f carried
        # ``_approval_hold_provenance.owner == "rollup_lineage_fence"`` set at
        # 01:42Z, this branch FAILED it at ~03:42Z, and the blocking
        # descendant's PR #17693 merged at 12:11Z — 8.5h after the
        # ancestor was already dead. The release check above is also only
        # reachable from inside THIS timeout branch, so a blocker resolving at
        # any other moment was never noticed at all (the reaper sweep
        # ``sweep_release_resolved_descendant_fence_blocks`` now runs the same
        # adjudication on its own cadence).
        #
        # So: adjudicate instead of terminalizing — WAIT while a blocker
        # is non-terminal, RELEASE when they all resolved, DECIDE (release +
        # record the unresolved descendants) once every blocker is terminal
        # and the block aged out. Ownership is proven STRUCTURALLY (non-empty
        # keyed blocks + the exact hold receipt + the fence's own provenance
        # owner), never by matching a reason string, and it is proven BEFORE
        # any I/O: if ownership does not hold, this whole branch is skipped
        # and every disposition below applies exactly as it did before.
        if not _fence_released:
            _fence_owned = False
            try:
                from app.rollup_lineage_fence import (
                    adjudicate_descendant_operator_blocks,
                    descendant_operator_block_hold_is_owned,
                )

                _fence_owned = descendant_operator_block_hold_is_owned(task)
            except Exception:
                _fence_owned = False
                logger.debug(
                    f"[EVAL TIMEOUT] Fence-hold ownership check failed for "
                    f"{str(task.id)[:8]} — falling through unchanged",
                    exc_info=True,
                )
            if _fence_owned:
                _fence_state = "waiting"
                # Snapshot identity BEFORE the adjudication attempt. Its except
                # clause rolls back, which expires every instance in the session,
                # so a later ``task.id`` / ``task.project_id`` is a lazy refresh —
                # MissingGreenlet under asyncio. The log line and the broadcast
                # below are exactly the two signals that tell an operator the
                # hold was kept, so raising there would erase the reason for the
                # one path whose whole job is to be observable.
                _fence_task_id = str(task.id)
                _fence_project_id = str(task.project_id)
                _fence_status = task.status
                try:
                    _fence_state = await adjudicate_descendant_operator_blocks(
                        task, session, now=now
                    )
                    _fence_status = task.status
                    await session.commit()
                except Exception:
                    # Ownership was PROVEN above, so a transient adjudication
                    # error must NOT let this row fall through to the FAILED
                    # terminalization: hold it and retry on the next sweep.
                    _fence_state = "waiting"
                    logger.warning(
                        f"[EVAL TIMEOUT] Fence-hold adjudication failed for "
                        f"{_fence_task_id[:8]} — keeping the hold rather "
                        f"than failing a machine-held rollup",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()
                logger.info(
                    f"[EVAL TIMEOUT] Task {_fence_task_id[:8]} carries a "
                    f"MACHINE-set rollup_lineage_fence descendant-block hold "
                    f"({hours}h old) — adjudicated {_fence_state!r}; the "
                    f"human-approval timeout does not apply to it"
                )
                await services.broadcast_to_project(
                    _fence_project_id,
                    "task_updated",
                    {"task_id": _fence_task_id, "status": _fence_status},
                )
                return True

        # If the PR is still clean/mergeable and not yet preserved, leave it
        # open for hand-merge instead of silently abandoning it. Guard with
        # the preserve flag so re-entering sweeps are idempotent (no repeat
        # alerts). Broken/conflicted/non-rollup PRs fall through to FAILED.
        if not ctx.get("approval_timeout_preserved_for_handmerge"):
            _pr_num = _approval_hold_pr_number(task, ctx)
            if _pr_num:
                from app.review_loop_convergence import (
                    _PreservabilityUnavailable,
                    _pr_is_preservable_on_cap,
                )

                _is_preservable = False
                try:
                    _is_preservable = await _pr_is_preservable_on_cap(
                        task, int(_pr_num), session
                    )
                except _PreservabilityUnavailable as _unavail:
                    # TRANSIENT (deadline breach / GitHub rate limit) — we do NOT
                    # know this PR's state, so we must not abandon it. Without this
                    # clause the generic ``except Exception`` below swallows it,
                    # leaves _is_preservable=False, and the task lands FAILED with a
                    # perfectly mergeable PR abandoned — in BULK during an
                    # install-token-exhaustion window, which is exactly the incident
                    # the raising code exists to prevent.
                    #
                    # PRESERVE instead. The preserve branch below persists
                    # ``approval_timeout_preserved_for_handmerge=True``, which USED
                    # TO BE a one-way door to human hand-merge: the early-return
                    # under it skipped the row forever, so a transient blip latched
                    # preserve permanently and the engine handed a question back to
                    # a human that it had already answered.
                    #
                    # It is now a BOUNDED park. ``recovery_evaluating_handmerge_latch``
                    # re-decides the latch after a second window: reconcile if the PR
                    # merged, MERGE it if it is still provably green-and-clean, or
                    # re-arm (a bounded number of times) and finally terminalize. The
                    # outcome here stays non-destructive and observable — the PR is
                    # left OPEN and an alert fires — but it no longer waits forever
                    # on a human because our quota ran out for one sweep.
                    logger.warning(
                        f"[EVAL TIMEOUT] Preservability UNAVAILABLE for "
                        f"{str(task.id)[:8]} PR #{_pr_num} ({_unavail}) — PRESERVING "
                        f"for hand-merge rather than abandoning a PR we could not "
                        f"read. This park is BOUNDED: the latch is re-decided "
                        f"automatically once its second window elapses."
                    )
                    _is_preservable = True
                except Exception:
                    logger.debug(
                        f"[EVAL TIMEOUT] Preservability check failed for "
                        f"{str(task.id)[:8]} PR #{_pr_num}",
                        exc_info=True,
                    )
                if _is_preservable:
                    logger.info(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} approval-gate "
                        f"timeout after {hours}h — PR #{_pr_num} is clean/mergeable, "
                        f"PRESERVING for hand-merge instead of abandoning"
                    )
                    _redecide_hours = round(handmerge_redecide_seconds / 3600, 1)
                    task.context = {
                        **ctx,
                        "approval_timeout_preserved_for_handmerge": True,
                        "failure_detail": "approval_timeout_preserved_for_handmerge",
                        # Stamp WHEN the latch was armed so the second window is
                        # measurable. Without it the re-decision path cannot tell
                        # a fresh park from a week-old one.
                        HANDMERGE_LATCH_STAMPED_AT_KEY: now.isoformat(),
                    }
                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    await maybe_create_failure_alert(
                        task,
                        session,
                        f"Approval-gate timeout after {hours}h — PR #{_pr_num} "
                        f"is clean/mergeable and PRESERVED (not abandoned). This "
                        f"park is BOUNDED, not a permanent hand-merge request: in "
                        f"{_redecide_hours}h the engine re-decides it — merging the "
                        f"PR if it is still green-and-clean, reconciling if it "
                        f"already merged, otherwise re-arming up to "
                        f"{HANDMERGE_REDECIDE_MAX_REARMS} times before "
                        f"terminalizing.",
                    )
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    return True

        # BOUNDED RE-ENTRY: already preserved on a prior sweep. This used to be a
        # bare ``return True`` — a ONE-WAY DOOR. Nothing ever re-decided, so the
        # row sat EVALUATING with requires_approval=True forever waiting for a
        # human to press Merge on a PR the engine had already PROVEN landable
        # (task 129fb995 / PR #17720, MERGEABLE + CLEAN, 2026-08-23). The latch
        # now gets a second bounded window and a real disposition; only an
        # exhausted re-arm budget falls through to the terminalization below.
        if ctx.get("approval_timeout_preserved_for_handmerge"):
            if await redecide_preserved_handmerge_latch(
                task,
                ctx,
                session,
                now,
                window_seconds=handmerge_redecide_seconds,
            ):
                return True
            # Budget spent and the PR is provably not landable — cull the dud
            # through the existing terminalization path. Re-read the context the
            # re-decision just committed so the branches below see it.
            ctx = task.context or {}

        # PR-LESS APPROVAL HOLD: the same non-destructive latch, for work that
        # has no PR to hand-merge. Everything above this line needs a PR number
        # and no-ops without one, so a hold carrying none fell straight through
        # to the FAILED terminalization below — no alert-and-hold, no second
        # sweep, no operator window.
        if not _approval_hold_pr_number(task, ctx):
            await _hold_pr_less_approval_gate_timeout(
                task,
                ctx,
                session,
                now,
                approval_elapsed=approval_elapsed,
                hours=hours,
            )
            return True

        logger.error(
            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been "
            f"requires_approval for {hours}h without resolution — "
            f"approval-gate timeout, marking FAILED"
        )
        task.status = TaskStatus.FAILED
        task.latest_status_message = (
            f"Approval-gate timeout after {hours}h — no manual "
            f"approval received, escalating to FAILED"
        )
        from app.failure_classification import (
            classify_failure,
            persist_failure_category,
        )
        from app.routers.agent_status import record_task_result

        _fc = classify_failure(task)
        persist_failure_category(task, _fc)
        record_task_result(
            success=False,
            failure_class=_fc,
            task_id=str(task.id),
            task_type=task.first_payload.get("type", ""),
        )
        session.add(task)
        await maybe_create_failure_alert(
            task,
            session,
            f"Approval-gate timeout after {hours}h — parent failed "
            f"without manual resolution",
        )
        await session.commit()
        # task.status was set to FAILED above; _maybe_create_failure_alert
        # can poison/roll back the session (expiring `task`), so use the
        # literal rather than a post-commit lazy-load (MissingGreenlet).
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        return True

    # Non-spend approval hold still within its 2h window — fall through
    # to the remaining recovery branches unchanged.
    return False


async def _respawn_review_after_churn_halt(task, session) -> bool:
    """Bounded automatic respawn for a supersession_churn_halt hold.

    Mirrors the exact ``_spawn_pipeline_task`` call shape already used by
    the analogous ``recovery_evaluating_pr_check._handle_pr_state_check_pending``
    open-PR branch: derive branch/base_branch/original_prompt straight off
    the chain-root task's own payload/context (no live GitHub call needed
    here — the caller already ran the merged-PR check above this branch, and
    ``_spawn_pipeline_task`` itself no-ops on a ``pr_merged``/
    ``pr_closed_unmerged`` context flag), then spawn a fresh review chained
    to ``task`` via ``original_task_id=str(task.id)``.

    Returns ``True`` when a fresh review was spawned and the hold released
    (caller treats the row as handled this pass) or ``False`` when respawn
    was declined/failed (caller falls through to the existing preserve/
    FAILED disposition unchanged).
    """
    import app.shared_state as _shared_state
    from app import services
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import Project
    from app.review_pipeline import (
        SPAWN_REASON_KEY,
        _REVIEW_SUPERSEDE_CHURN_COUNT_KEY,
        _REVIEW_SUPERSEDE_CHURN_LAST_KEY,
        _spawn_pipeline_task,
    )
    from app.review_loop_convergence import clear_review_escalation_approval_gate

    ctx = dict(task.context or {})
    pr_number = _approval_hold_pr_number(task, ctx)
    if not pr_number:
        logger.info(
            f"[EVAL TIMEOUT approval-gate] Task {str(task.id)[:8]}: no "
            f"recoverable PR number for churn-halt respawn — declining"
        )
        return False

    task_type_str = (task.first_payload or {}).get("type", "") or ""
    proj = await session.get(Project, task.project_id)
    branch = services._derive_task_branch(
        task.id,
        task_type_str,
        _shared_state.get_project_task_branch_namespace(proj),
    )
    base_branch = (task.first_payload or {}).get(
        "base_branch"
    ) or _shared_state.get_project_integration_branch(proj)
    original_prompt = (task.first_payload or {}).get("prompt") or (
        task.description or ""
    )

    try:
        review_task, _was_created = await _spawn_pipeline_task(
            task,
            "review",
            session,
            int(pr_number),
            branch,
            base_branch,
            original_prompt=original_prompt,
            original_task_id=str(task.id),
        )
    except Exception:
        logger.warning(
            f"[EVAL TIMEOUT approval-gate] Churn-halt respawn failed for "
            f"task {str(task.id)[:8]} PR #{pr_number}",
            exc_info=True,
        )
        return False
    if review_task is None:
        # Spawn suppressed (PR merged/closed meanwhile, iteration cap
        # reached, a different-type task already in flight, ...) — nothing
        # to respawn. Let the caller fall through to the existing terminal
        # disposition rather than looping here.
        logger.info(
            f"[EVAL TIMEOUT approval-gate] Task {str(task.id)[:8]}: "
            f"churn-halt respawn suppressed for PR #{pr_number} "
            f"(spawn_reason={(task.context or {}).get(SPAWN_REASON_KEY)!r})"
        )
        return False

    released = clear_review_escalation_approval_gate(
        task, resolved_by="churn_halt_bounded_respawn"
    )
    ctx = dict(task.context or {})
    respawn_count = int(ctx.get("_churn_halt_respawn_count", 0)) + 1
    ctx["_churn_halt_respawn_count"] = respawn_count
    # Give the fresh review a clean churn-detection window instead of
    # inheriting the count that just tripped the halt.
    ctx.pop(_REVIEW_SUPERSEDE_CHURN_COUNT_KEY, None)
    ctx.pop(_REVIEW_SUPERSEDE_CHURN_LAST_KEY, None)
    task.context = ctx
    flag_modified(task, "context")
    task.latest_status_message = (
        f"Churn-halt bounded auto-respawn: spawned review {review_task.id} "
        f"for PR #{pr_number} (attempt {respawn_count}/"
        f"{_CHURN_HALT_AUTO_RESPAWN_MAX})"
    )
    session.add(task)
    await session.commit()
    logger.info(
        f"[EVAL TIMEOUT approval-gate] Task {str(task.id)[:8]}: churn-halt "
        f"bounded auto-respawn spawned review {review_task.id} for PR "
        f"#{pr_number} (attempt {respawn_count}/{_CHURN_HALT_AUTO_RESPAWN_MAX}, "
        f"hold_released={released})"
    )
    await services.broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": task.status},
    )
    return True
