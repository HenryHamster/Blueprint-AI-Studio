"""Terminal tail of ``_process_post_commit``, extracted from task_lifecycle.py.

S2 of the fdd07b88 decomposition plan (knowledge doc
``fdd07b88-8bc5-4db3-89e0-016075fb7563``). This is stage **T** in that plan:
the parent-rollup / direction-outcome / remediation / release-qualifier /
strategic-backstop / final-commit / broadcast sequence that always runs after
the ``task_type`` dispatch (create / review / fix) completes, regardless of
which branch ran or whether it ran at all.

Free-variable set (per the design doc's live-in analysis): ``task``,
``payload``, ``task_id``, ``session``, ``_session_factory``, and the
``_rollback_and_refetch_task`` closure (passed in as ``refetch`` — it closes
over ``session``/``task_id`` only, so it is trivially passable). Nothing from
the COMPLETED/review/fix branches above is read here.

This is the *last* stage: whatever it returns (or raises) is exactly what
``_process_post_commit`` returns (or raises), so the caller is a single
``return await run_post_commit_terminal(...)`` — no ``StageResult`` indirection
needed, unlike stages with more code after them.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.models import Task, TaskStatus
from app.post_commit_errors import PostCommitFinalCommitFailed
from app.schemas import AgentStatusPayload

logger = logging.getLogger(__name__)


async def run_post_commit_terminal(
    task: Task,
    *,
    payload: AgentStatusPayload,
    task_id: uuid.UUID,
    session: AsyncSession,
    _session_factory,
    refetch: Callable[[], Awaitable[Task | None]],
) -> dict:
    """Run the post-commit terminal sequence and return the final response dict.

    May raise ``PostCommitFinalCommitFailed`` — callers translate that into a
    clean 200 while preserving the ``post_commit_pending`` marker for sweep
    retry (see the comment above the final-commit guard below).
    """
    # Deferred import so test patches on app.task_lifecycle._check_and_trigger_rollup
    # keep intercepting the call (23 call sites in test_task_lifecycle.py patch it
    # there, not on app.rollup_management — the canonical home).
    from app.task_lifecycle import _check_and_trigger_rollup

    # 6. Parent task rollup — delegated to shared function
    if task.parent_task_id and payload.status in [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ]:
        try:
            await _check_and_trigger_rollup(task, session)
            if task.discovery_cycle_id is not None:
                from app.discovery_review import (
                    maybe_close_and_spawn_discovery_review,
                )

                await maybe_close_and_spawn_discovery_review(
                    session, task.discovery_cycle_id
                )
        except Exception as e:
            # NB: ``task_id`` (plain argument), never ``task.id`` — on a
            # poisoned session with an expired instance the attribute read
            # re-raised PendingRollbackError out of this except block
            # (prod burst 2026-06-07 01:05: lock_timeout inside
            # _spawn_pipeline_task).
            logger.error(
                f"[ROLLUP] Rollup check failed for task {task_id}: {e!r}",
                exc_info=True,
            )
            # C1: _check_and_trigger_rollup does FOR UPDATE selects +
            # internal commits that can raise on a DB error, leaving this
            # post-commit ``session`` in PendingRollback. Without this
            # rollback every subsequent post-terminal helper (direction
            # outcomes, remediation/regression, strategic backstop) and the
            # final commit silently fail or 500 the agent with
            # PendingRollbackError. Roll back so the chain continues clean,
            # and re-fetch ``task`` — the sections below keep reading
            # task.context / task.first_payload on it.
            task = await refetch()
            if task is None:
                return {"status": "accepted"}

    # 6b. Update direction research history outcomes for child tasks of a research cycle
    _task_ctx = task.context or {}
    if (
        _task_ctx.get("direction_id")
        and _task_ctx.get("research_task_id")
        and payload.status
        in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
    ):
        try:
            from app.direction_outcomes import _update_direction_research_outcomes

            await _update_direction_research_outcomes(
                task.project_id,
                _task_ctx["direction_id"],
                _task_ctx["research_task_id"],
                session,
            )
        except Exception as _outcome_exc:
            logger.warning(
                f"[RESEARCH] Failed to update direction research outcomes for task {task_id}: {_outcome_exc!r}"
            )
            # C1: _update_direction_research_outcomes takes the directions
            # FOR NO KEY UPDATE lock and commits; an IntegrityError /
            # serialization failure there leaves this session aborted. Roll
            # back so the terminal-record helpers below + the final commit
            # don't inherit the PendingRollback, and re-fetch ``task`` (the
            # rollback expired it; the helpers below read its attributes).
            task = await refetch()
            if task is None:
                return {"status": "accepted"}

    # C1: the terminal-record helper does its own session writes (it calls
    # _record_remediation_outcome which commits). It is an independent
    # operation on the shared post-commit session: a raise must not leave the
    # session in PendingRollback for the final commit below. Run it behind a
    # rollback-on-failure guard so the chain is poison-free.
    try:
        # Deferred import so test patches on app.task_completion.
        # _record_remediation_if_terminal keep intercepting the call (matches
        # the original function-local import in _process_post_commit).
        from app.task_completion import _record_remediation_if_terminal

        await _record_remediation_if_terminal(task, payload.status, session)
    except Exception as _helper_exc:
        logger.error(
            f"[LIFECYCLE] Terminal-record helper _record_remediation_if_terminal "
            f"failed for task {task_id}: {_helper_exc!r}",
            exc_info=True,
        )
        task = await refetch()
        if task is None:
            return {"status": "accepted"}
    try:
        from app.task_completion import (
            _expire_migration_reservation_if_terminal,
        )

        await _expire_migration_reservation_if_terminal(
            task, payload.status, session
        )
    except Exception as _mr_exc:
        logger.error(
            f"[LIFECYCLE] Migration-reservation terminal cleanup failed for "
            f"task {task_id}: {_mr_exc!r}",
            exc_info=True,
        )
        task = await refetch()
        if task is None:
            return {"status": "accepted"}

    # Release-qualifier finalization — fires from EVERY terminal path
    # (COMPLETED / FAILED / CANCELLED / BLOCKED) for BOTH a release_audit
    # task and other current qualifier children. This deliberately lives in the
    # terminal section (not the COMPLETED-only review-pipeline block above):
    # a qualifier child that fails/cancels/blocks must still advance
    # qualification,
    # or the ReleaseVersion stays {status:"pending"} forever and the
    # version is wedged.
    #
    # Why here and not in agent_status.py: this runs after the status
    # endpoint's early commit, on a fresh post-commit session. The task's
    # terminal status AND any SMOKETEST_RESULT / RELEASE_AUDIT_REPORT
    # Artifact the agent posted are therefore already committed and visible
    # to the finalize SELECT (_latest_artifact_logs). The old
    # agent_status.py hook ran pre-flush with autoflush disabled and could
    # not see its own just-added Artifact.
    #
    # maybe_finalize_qualification opens its OWN dedicated DB session
    # (AsyncSessionLocal) for the entire finalize→promote chain — it does
    # NOT use this post-commit ``session``. That decoupling is deliberate:
    # promote() merges to production main and issues an intermediate
    # commit() to release its row lock before 30-120s of GitHub I/O; on a
    # caller's session that commit would prematurely commit this handler's
    # unrelated dirty work (the completed task's terminal status,
    # TaskHistory, Artifacts, remediation/regression records). Because it
    # owns its session, this call also cannot self-deadlock against the
    # Task-row lock this handler holds (it locks the ReleaseVersion row, a
    # different row). It is idempotent (it FOR UPDATE-locks the version and
    # no-ops unless last_qualification.status == "pending" and the required
    # children are terminal) and a cheap O(1) no-op — no session opened, no
    # I/O — for the overwhelming majority of tasks (those with no
    # release_version_id in context). Keyed on context["release_version_id"]
    # and/or task_type == "release_audit".
    if payload.status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    ):
        _qual_ctx = task.context or {}
        _qual_first_payload = task.first_payload or {}
        _is_qualifier_child = bool(
            _qual_ctx.get("release_version_id")
            or _qual_first_payload.get("type") == "release_audit"
        )
        if _is_qualifier_child:
            try:
                from app import release_qualifier

                # maybe_finalize_qualification opens its OWN dedicated
                # session (see the comment above) — never this post-commit
                # ``session``. ``_session_factory`` is threaded through so
                # the dedicated session is opened from the SAME factory
                # this handler runs on: in production that is None →
                # AsyncSessionLocal (a genuine independent connection,
                # preserving the transaction-decoupling); under the test
                # harness it is the test-engine factory, so the finalize
                # sees the committed seed data and its commit is visible to
                # the test's assertions instead of opening an unrelated
                # connection on a different event loop.
                await release_qualifier.maybe_finalize_qualification(
                    task, session_factory=_session_factory
                )
            except Exception as _qual_exc:
                logger.error(
                    f"[RELEASE_QUALIFIER] maybe_finalize_qualification failed "
                    f"for terminal task {task_id} (status={payload.status}): "
                    f"{_qual_exc!r}",
                    exc_info=True,
                )
                # C1 (defensive): maybe_finalize_qualification owns its own
                # dedicated session, so it should not poison this shared
                # ``session`` — but the pre-call accessors (task.context /
                # task.first_payload) can trigger a lazy load that aborts the
                # transaction, and a future refactor might touch the shared
                # session. Roll back to match the sibling terminal-record /
                # backstop / final-commit guards so the chain below stays
                # poison-free.
                task = await refetch()
                if task is None:
                    return {"status": "accepted"}

    # Backstop for terminal strategic phases.  The canonical domain helper
    # derives the actual phase from the cycle/task linkage, so delayed
    # phase-1 delivery cannot regress a cycle already running phase 2.
    _reconciled_completion_event: dict | None = None
    try:
        # The completion handler recovery above can terminalize a task as
        # FAILED even when the agent payload reported COMPLETED. Reconcile
        # from the current persisted task status so that handler failures
        # close their linked strategic phase immediately instead of waiting
        # for the stalled-cycle reaper.
        if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            from app import strategic_cycle

            reconciled_cycle = await strategic_cycle.reconcile_terminal_task(
                session, task, source="agent_status_lifecycle"
            )
            if reconciled_cycle is not None:
                logger.warning(
                    "[STRATEGIC] Reconciled terminal task %s (%s) to cycle %s",
                    task.id,
                    payload.status,
                    reconciled_cycle.id,
                )
                # Snapshot BEFORE the commit expires the rows; the emit is
                # post-commit below. Driving the strategist-indicator event
                # from the reconciled cycle (not payload.status) is what
                # keeps durable cycle state and the connected UI in sync
                # when the agent payload said COMPLETED but the completion
                # handler recovery terminalized the task as FAILED.
                _reconciled_completion_event = (
                    strategic_cycle.terminal_completion_event(
                        reconciled_cycle, task
                    )
                )
                await session.commit()
    except Exception as _bk_exc:
        logger.error(
            f"[STRATEGIC] Lifecycle backstop failed for task {task_id}: {_bk_exc!r}",
            exc_info=True,
        )
        # The reconciliation (and/or its commit) did not land — drop the
        # snapshot so we never broadcast a completion for a cycle whose
        # terminalization was rolled back. The payload-keyed fallback below
        # still runs, and the stalled-cycle reaper converges the residue.
        _reconciled_completion_event = None
        # C1: strategic_cycle.mark_failure + the commit inside the backstop
        # can raise (lock contention / DB error), leaving the session
        # aborted. Roll back so the final commit below does not raise
        # PendingRollbackError and return a 500 to the agent, and re-fetch
        # ``task`` (the final broadcast/backstop below read its attributes).
        task = await refetch()
        if task is None:
            return {"status": "accepted"}

    # Final commit for any deferred changes (status, artifacts, spawned tasks).
    # C1: guard the commit. The task's own terminal status was already
    # committed on the early-commit (status-endpoint) session before this
    # post-commit handler ran, so a failure here is NOT fatal to the task.
    # BUT the deferred side effects it would flush (spawned follow-up /
    # remediation tasks, rollup nudges, artifacts) are now DROPPED — they
    # were never persisted. _process_post_commit runs behind the
    # write-ahead ``post_commit_pending`` marker + sweep-retry machinery;
    # both the inline path (_clear_post_commit_pending, agent_status.py
    # ~2946) and the sweep (services.py ~1954) clear that marker on a CLEAN
    # return, so swallowing this failure and returning {status:"received"}
    # would clear the marker and the sweep would NEVER retry the dropped
    # side effects (incident eefbed05: dependent chain orphaned).
    #
    # So roll back the poisoned transaction (cleanly closing the session)
    # and raise PostCommitFinalCommitFailed: a distinct signal the callers
    # treat as "PRESERVE the marker for the sweep" — while still NOT 500ing
    # the agent (the inline/retry callers translate it to a clean 200).
    if session.dirty or session.new:
        try:
            await session.commit()
        except Exception as _commit_exc:
            logger.error(
                f"[LIFECYCLE] Final post-commit commit failed for task "
                f"{task_id}: {_commit_exc!r} — preserving post_commit_pending "
                f"marker for sweep retry (deferred side effects not persisted)",
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
            raise PostCommitFinalCommitFailed(
                f"final post-commit commit failed for task {task_id}: "
                f"{_commit_exc!r}"
            ) from _commit_exc

    # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": payload.status},
    )

    # Strategist-indicator backstop: a strategic task (design_doc_update /
    # strategic_review) that terminates FAILED/CANCELLED (VM crash, timeout)
    # never reaches the COMPLETED completion handlers that emit
    # strategic_cycle_completed, so the frontend "Strategist working…"
    # indicator would stick on. Pair the spawn-time strategic_cycle_started
    # here. COMPLETED is deliberately excluded — the completion handlers own
    # that emit, and a COMPLETED design_doc_update that chained to Phase 2
    # must keep the cycle active.
    #
    # When the backstop above actually terminalized a cycle, that reconciled
    # result is authoritative: it carries the real phase (from the cycle/task
    # linkage, so a late phase-1 delivery cannot mislabel a phase-2 cycle)
    # and the resulting terminal outcome, and it fires even when the agent
    # payload reported COMPLETED (handler-recovery failures). The
    # payload-keyed branch stays as the fallback for tasks with no
    # reconcilable cycle row (already-terminal or legacy cycles), so a
    # strategic task that fails still clears the indicator.
    if _reconciled_completion_event is not None:
        from app import strategic_cycle as _strategic_cycle_emit

        await _strategic_cycle_emit.emit_terminal_completion_events(
            [_reconciled_completion_event]
        )
    else:
        _peek_strategic_type = task.first_payload.get("type", "")
        if (
            task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and _peek_strategic_type in (
                "design_doc_update",
                "strategic_review",
            )
        ):
            from app.strategic_events import emit_strategic_cycle_completed

            await emit_strategic_cycle_completed(
                task.project_id,
                task.id,
                1 if _peek_strategic_type == "design_doc_update" else 2,
                cycle_id=task.strategic_cycle_id
                or (task.context or {}).get("strategic_cycle_id"),
                phase_state="failed",
                completion={
                    "status": getattr(task.status, "value", str(task.status)),
                    "status_message": task.latest_status_message,
                },
            )

    return {
        "status": "received",
        "message": f"Status update for task {payload.task_id} recorded.",
    }
