"""EVALUATING-parent rescue sweeps extracted from reaper.reaper_service Steps 3+4.

Step 3: Sweep stale EVALUATING parents with unmerged children and re-trigger
rollup review spawns that were silently dropped.

Step 4: Rescue EVALUATING parents whose children ALL terminated without any
completing successfully, by re-queuing them via _requeue_parent_task.

Entry point: sweep_evaluating_parents().
"""

import contextlib
import logging
import os
import uuid
from datetime import timedelta
from typing import Callable, Dict, Optional

from sqlalchemy import inspect, or_
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Direction, Task, TaskStatus

logger = logging.getLogger(__name__)

# Per-cycle cap on the Step 3/4 stale-EVALUATING sweep. Each parent costs
# either serial GitHub I/O (_check_and_trigger_rollup, Step 3) or a DB-heavy
# requeue (_requeue_parent_task, Step 4). Generous default; deferred parents
# re-qualify next cycle (the sweep runs every reaper cycle and selects
# oldest-stale first).
_REAPER_MAX_EVALUATING_PER_CYCLE = int(
    os.getenv("REAPER_MAX_EVALUATING_PER_CYCLE", "100")
)

_SNAPSHOT_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def _snap_all_terminal(snap: list[tuple]) -> bool:
    """Return True iff every child in the snapshot tuple list has a terminal status."""
    return all(status in _SNAPSHOT_TERMINAL for _, status, _ in snap)


def _snap_any_completed(snap: list[tuple]) -> bool:
    """Return True iff at least one child in the snapshot tuple list is COMPLETED."""
    return any(status == TaskStatus.COMPLETED for _, status, _ in snap)


async def sweep_evaluating_parents(
    session,
    now,
    progress_cb: Callable[[], None],
    *,
    landed_cb: Optional[Callable] = None,
) -> None:
    """Run Steps 3 and 4 of the reaper service loop.

    ``landed_cb`` is ``_mark_landed_parent_dependents`` from reaper.py, passed
    as a callback to avoid a circular import.
    """
    from app.rollup_management import _check_and_trigger_rollup
    from app.task_lifecycle import _requeue_parent_task

    _children_snapshot_by_parent: Dict[uuid.UUID, list[tuple]] = {}
    _parent_ids: list = []

    # Step 3: Sweep EVALUATING parents with stale unmerged children.
    # When the initial review spawn after a child completes fails silently,
    # no event re-triggers _check_and_trigger_rollup.  This sweep catches
    # those stuck parents and lets the stale-PR recovery path respawn reviews.
    #
    # Phase B4 (H-P1): batch-fetch all children for ALL evaluating parents
    # in a single ``IN (...)`` query (mirrors the Step 4 pattern at the
    # batched fetch below). The previous code fired N queries — one per
    # parent — to find a single COMPLETED child for the rollup trigger.
    # ``load_only`` keeps each row narrow but MUST include ``status_log``
    # because the ``Task.latest_status_message``/``Task.status`` event
    # listeners (models.py ~360) read status_log on every set; under
    # async sessions a deferred-column fetch raises MissingGreenlet.
    # Plain-tuple snapshot of (id, status, context) per child, captured
    # at SELECT time. Steps 3 AND 4 iterate this rather than touching
    # live ORM rows: ``_check_and_trigger_rollup`` in Step 3 may commit
    # or trigger a session rollback (via inner DB error), which under
    # SQLAlchemy expires every loaded instance — a subsequent
    # ``parent.id`` / ``child.status`` read fires a sync lazy-load that
    # can't acquire a connection if the underlying asyncpg socket is
    # dead, crashing the entire reaper cycle. Snapshotting + re-fetching
    # via ``session.get`` decouples both steps from session state.
    # (Production 2026-05-08: 26x crashes at reaper.py:2407; recurred
    # 2026-05-21 at reaper.py:2385/2420 — Step 3/4 still read parent.id
    # and child.status off the now-stale batch.)
    try:
        stale_cutoff = now - timedelta(minutes=15)
        evaluating_parents = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.updated_at,
                            Task.context,
                            Task.payloads,
                            Task.project_id,
                            Task.parent_task_id,
                            Task.latest_status_message,
                            Task.status_log,
                            Task.attempt_count,
                            Task.created_at,
                            raiseload=True,
                        )
                    )
                    .where(
                        Task.status == TaskStatus.EVALUATING,
                        or_(
                            Task.updated_at < stale_cutoff,
                            Task.updated_at.is_(None),
                        ),
                    )
                    # C3: per-cycle row cap. Step 3 calls
                    # _check_and_trigger_rollup (serial GitHub I/O)
                    # per parent and Step 4 calls _requeue_parent_task
                    # (DB-heavy) per parent. Without a cap a large
                    # stale-EVALUATING backlog grows the reaper cycle
                    # past REAPER_HANG_SECONDS mid-I/O — the same O(N)
                    # saturation the orphan cap bounds. Oldest-stale
                    # first (most-stuck parents); deferred parents
                    # re-qualify next cycle (status+age scan, no
                    # deferral queue needed).
                    .order_by(Task.updated_at.asc().nullsfirst())
                    .limit(_REAPER_MAX_EVALUATING_PER_CYCLE)
                )
            )
            .scalars()
            .all()
        )
        if len(evaluating_parents) >= _REAPER_MAX_EVALUATING_PER_CYCLE:
            logger.warning(
                "[REAPER] Hit per-cycle cap of %d stale EVALUATING "
                "parents — remainder deferred to next cycle",
                _REAPER_MAX_EVALUATING_PER_CYCLE,
            )
        if evaluating_parents:
            _parent_ids = [p.id for p in evaluating_parents]
            _all_children = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.parent_task_id,
                                Task.context,
                                Task.status_log,
                                Task.latest_status_message,
                                Task.payloads,
                                raiseload=True,
                            )
                        )
                        .where(Task.parent_task_id.in_(_parent_ids))
                    )
                )
                .scalars()
                .all()
            )
            for _c in _all_children:
                _children_snapshot_by_parent.setdefault(
                    _c.parent_task_id, []
                ).append((_c.id, _c.status, _c.context or {}))

        # ── Step 3 batch-fetch children ──
        # Collect all completed and nudge-pending child IDs from the snapshot
        # and batch-fetch them in a single query to avoid N+1 session.get
        # calls inside the per-parent loop. After a commit/rollback inside
        # _check_and_trigger_rollup, cached instances may be expired — fall
        # back to session.get for the affected row (covers all parents in a
        # fresh batch when no commit happens between iterations).
        _all_trigger_child_ids: set[uuid.UUID] = set()
        _all_nudge_child_ids: set[uuid.UUID] = set()
        for _pid in _parent_ids:
            for _cid, _c_status, _c_ctx in _children_snapshot_by_parent.get(_pid, []):
                if _c_status == TaskStatus.COMPLETED:
                    _all_trigger_child_ids.add(_cid)
                if (_c_ctx or {}).get("_rollup_nudge_pending"):
                    _all_nudge_child_ids.add(_cid)
        _all_child_ids = _all_trigger_child_ids | _all_nudge_child_ids
        _child_map: dict[uuid.UUID, Task] = {}
        if _all_child_ids:
            _child_rows = (
                (
                    await session.execute(
                        select(Task).where(Task.id.in_(_all_child_ids))
                    )
                )
                .scalars()
                .all()
            )
            _child_map = {c.id: c for c in _child_rows}

        for parent_id in _parent_ids:
            # Heartbeat: Step 3 calls _check_and_trigger_rollup per
            # parent (serial GitHub I/O) — the other dominant per-cycle
            # cost besides the orphan loop. Mark progress every parent.
            progress_cb()
            # Find a triggering child id from the plain-tuple
            # snapshot, then re-fetch it fresh. An earlier iteration's
            # _check_and_trigger_rollup may have committed or rolled
            # back — reading parent.id / child.status off the now-stale
            # ORM batch fires a sync lazy-load that crashes the cycle
            # on an unhealthy connection.
            #
            # Prefer a COMPLETED child (the normal stale-spawn case).
            # C6 rework (B): ALSO accept any child carrying the
            # ``_rollup_nudge_pending`` write-ahead marker, regardless
            # of status. ``_propagate_pipeline_failure_to_original``
            # sets that marker on a child it is CANCELLING just before
            # nudging the rollup; a death between its terminal commit
            # and the nudge drops the trigger, and a parent whose only
            # un-rolled child is that CANCELLED one has NO COMPLETED
            # child for the path above to find — it would sit
            # EVALUATING until the backstop. ``_check_and_trigger_rollup``
            # is keyed only on ``child.parent_task_id`` and re-evaluates
            # ALL siblings, so re-running it for the marker-carrying
            # child is correct and idempotent.
            _nudge_child_id = next(
                (
                    cid
                    for cid, _c_status, c_ctx in _children_snapshot_by_parent.get(
                        parent_id, []
                    )
                    if (c_ctx or {}).get("_rollup_nudge_pending")
                ),
                None,
            )
            completed_child_id = next(
                (
                    cid
                    for cid, c_status, _ in _children_snapshot_by_parent.get(
                        parent_id, []
                    )
                    if c_status == TaskStatus.COMPLETED
                ),
                None,
            )
            trigger_child_id = completed_child_id or _nudge_child_id
            if trigger_child_id is not None:
                try:
                    # Use batch-cached child if available; re-fetch only
                    # when the cached instance was expired by a prior
                    # commit/rollback inside _check_and_trigger_rollup.
                    child = _child_map.get(trigger_child_id)
                    if child is not None and inspect(child).expired:
                        child = await session.get(Task, trigger_child_id)
                    if child is not None:
                        # C3: pass the heartbeat so _check_and_trigger_rollup's
                        # per-child PR-verify loop keeps the reaper
                        # liveness fresh on a fan-heavy parent.
                        await _check_and_trigger_rollup(
                            child,
                            session,
                            progress_cb=progress_cb,
                        )
                        # C6 rework (B): the nudge ran without raising —
                        # drop the write-ahead recovery marker on the
                        # child that carried it (the rollup has now been
                        # re-evaluated). Best-effort: a leftover marker
                        # is self-healing (next sweep re-runs an
                        # idempotent check).
                        if _nudge_child_id is not None:
                            _nudged = _child_map.get(_nudge_child_id)
                            if _nudged is not None and inspect(_nudged).expired:
                                _nudged = await session.get(
                                    Task, _nudge_child_id
                                )
                            if _nudged is not None and (
                                _nudged.context or {}
                            ).get("_rollup_nudge_pending"):
                                _nc = dict(_nudged.context or {})
                                _nc.pop("_rollup_nudge_pending", None)
                                _nudged.context = _nc
                                flag_modified(_nudged, "context")
                                session.add(_nudged)
                                await session.commit()
                        # Completion-side warm-reuse: if the sweep's
                        # rollup trigger landed this parent, stamp the
                        # _deps_satisfied marker on the dependents it
                        # just unblocked (see helper docstring) so a
                        # polling warm VM claims them before cold
                        # teardown rather than waiting for a later
                        # window-bounded dispatch scan.
                        if landed_cb is not None:
                            await landed_cb(parent_id, session)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[REAPER] Evaluating sweep rollup failed for parent {parent_id}: {e}",
                        exc_info=True,
                    )
        if evaluating_parents:
            logger.info(
                f"[REAPER] Swept {len(evaluating_parents)} stale EVALUATING parents"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[REAPER] Evaluating parent sweep error: {e}", exc_info=True
        )
        # C1: a sub-helper (e.g. _check_and_trigger_rollup) that
        # raised may have left the shared ``session`` in
        # PendingRollback — roll back here so Steps 4-10 run on a
        # clean session rather than cascading silent failures /
        # rollback-expiry MissingGreenlet. Mirrors Step 6/7/8/10.
        with contextlib.suppress(Exception):
            await session.rollback()

    # Step 4: Rescue EVALUATING parents whose children ALL terminated without
    # any completing successfully.  These parents have no COMPLETED child to
    # trigger _check_and_trigger_rollup, so they stay EVALUATING forever.
    # Iterates the plain-tuple snapshot from Step 3 — see the comment
    # at ``_children_snapshot_by_parent`` for why we don't touch
    # live ORM children here.
    try:
        rescued = 0

        # Batch-fetch all parents for Step 4 rescue loop to avoid N+1
        # session.get calls. Step 3 may have committed/rolled back, expiring
        # the live evaluating_parents batch — fall back to session.get if
        # the cached instance is expired.
        _parent_map: dict[uuid.UUID, Task] = {}
        if _parent_ids:
            _parent_rows = (
                (
                    await session.execute(
                        select(Task).where(Task.id.in_(_parent_ids))
                    )
                )
                .scalars()
                .all()
            )
            _parent_map = {p.id: p for p in _parent_rows}

        for parent_id in _parent_ids:
            # Heartbeat: this rescue loop calls _requeue_parent_task
            # (child clone + dep remap, multiple queries) per parent —
            # another per-cycle cost that can extend wall time under a
            # large EVALUATING backlog. Mark progress every parent.
            progress_cb()
            children_snap = _children_snapshot_by_parent.get(parent_id, [])
            if not children_snap:
                continue
            if not _snap_all_terminal(children_snap):
                # At least one child still running — not safe to rescue yet.
                continue
            if _snap_any_completed(children_snap):
                # Step 3 handles this; skip.
                continue
            # All children are terminal and none completed — attempt re-evaluation
            # via _requeue_parent_task which clones failed children with retry
            # guidance (up to MAX_PARENT_REQUEUES attempts).  If attempts are
            # exhausted it marks the parent FAILED and fires a failure alert.
            # Re-fetch the parent fresh — Step 3 may have committed or
            # rolled back, expiring the live ``evaluating_parents`` batch.
            # Use batch-cached parent; re-fetch only when expired by a prior
            # commit/rollback inside Step 3's _check_and_trigger_rollup.
            parent = _parent_map.get(parent_id)
            if parent is not None and inspect(parent).expired:
                parent = await session.get(Task, parent_id)
            if parent is None:
                continue
            failed_children = [
                (cid, ctx)
                for cid, status, ctx in children_snap
                if status == TaskStatus.FAILED
            ]
            cancelled_children = [
                cid
                for cid, status, _ in children_snap
                if status == TaskStatus.CANCELLED
            ]
            # Build a diagnostic message including child failure classes.
            parts = []
            if failed_children:
                classes = [
                    (ctx or {}).get("failure_category") or "unknown"
                    for _, ctx in failed_children
                ]
                class_summary = ", ".join(f"{cls}" for cls in classes)
                parts.append(
                    f"{len(failed_children)} failed ({class_summary})"
                )
            if cancelled_children:
                parts.append(f"{len(cancelled_children)} cancelled")
            diag = (
                "; ".join(parts)
                if parts
                else f"{len(children_snap)} terminal"
            )
            parent.latest_status_message = (
                f"All child tasks terminated without success: {diag}"
            )
            session.add(parent)
            await _requeue_parent_task(parent, session)
            rescued += 1
            logger.warning(
                f"[REAPER] Rescued stuck EVALUATING parent {parent.id}: {diag}"
            )
        if rescued:
            await session.commit()
            logger.info(
                f"[REAPER] Rescued {rescued} all-children-failed EVALUATING parents"
            )
    except Exception as e:
        logger.error(
            f"[REAPER] All-children-failed rescue error: {e}", exc_info=True
        )
        # C1: _requeue_parent_task commits internally and an
        # IntegrityError (e.g. idempotency-key collision on the
        # cloned retry child) leaves the shared ``session`` aborted.
        # Roll back so Steps 5-10 don't inherit the poison. Mirrors
        # the Step 6/7/8/10 handlers and the Step 3 guard above.
        with contextlib.suppress(Exception):
            await session.rollback()


# Terminal direction statuses — duplicated from research_direction_state to
# avoid a cross-module import that would create a cycle between the reaper
# and the research completion layer.
_RETIRED_DIRECTION_STATUSES = frozenset(
    {"expired", "retired", "superseded", "completed"}
)

_ORPHANED_PROVIDER_SWEEP_BATCH = 200


async def sweep_orphaned_direction_provider_tasks(session) -> int:
    """Detach (never cancel) EVALUATING tasks whose parent direction is retired.

    A retired direction (status in _RETIRED_DIRECTION_STATUSES) must NEVER cancel
    in-flight or completed work.  ``context["direction_id"]`` is PROVENANCE — it
    records which autonomous-improvement direction prompted the task — not an
    ownership or liveness tether.  Cancelling here was actively destructive: an
    audit/research task that already spawned downstream fix tasks (but holds no PR)
    would be cancelled, which cascaded to FAIL its parent and tear down the shared
    staging branch, stranding the spawned fix tasks.

    So for every EVALUATING task whose direction row is now retired, we simply
    detach it: clear ``direction_id`` from context while keeping the task
    EVALUATING.  Nothing hangs forever — the generic ``sweep_evaluating_parents``
    sweep (which also covers ``Task.updated_at IS NULL``) resolves the task via its
    own children/PR once it is no longer pinned to a dead direction.

    A direction that does not exist in the Direction table (e.g. never synced) is
    left alone — missing != retired.

    Returns the number of tasks detached.
    """
    detached = 0
    try:
        # Step A: find EVALUATING tasks with a direction_id in context.
        # The context["direction_id"] key is set by research/refactor flows
        # when the task is tied to a specific autonomous-improvement direction.
        candidates = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.project_id,
                            Task.context,
                            Task.payloads,
                            Task.latest_status_message,
                            Task.status_log,
                            raiseload=True,
                        )
                    )
                    .where(
                        Task.status == TaskStatus.EVALUATING,
                        Task.archived.is_(False),
                        Task.context["direction_id"].is_not(None),
                    )
                    .limit(_ORPHANED_PROVIDER_SWEEP_BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return 0

        # Step B: batch-fetch Direction rows grouped by project to minimise
        # round-trips.  A direction that does not exist in the Direction table
        # (e.g. it was never synced) is left alone — missing ≠ retired.
        from collections import defaultdict

        tasks_by_proj: dict = defaultdict(list)
        for task in candidates:
            tasks_by_proj[task.project_id].append(task)

        for proj_id, proj_tasks in tasks_by_proj.items():
            direction_keys = {
                (t.context or {}).get("direction_id")
                for t in proj_tasks
                if (t.context or {}).get("direction_id")
            }
            if not direction_keys:
                continue

            direction_rows = (
                (
                    await session.execute(
                        select(Direction)
                        .options(
                            load_only(
                                Direction.direction_key,
                                Direction.status,
                                raiseload=True,
                            )
                        )
                        .where(
                            Direction.project_id == proj_id,
                            Direction.direction_key.in_(list(direction_keys)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            dir_status_by_key = {d.direction_key: d.status for d in direction_rows}

            for task in proj_tasks:
                did = (task.context or {}).get("direction_id")
                if not did:
                    continue
                dir_status = dir_status_by_key.get(did)
                if dir_status not in _RETIRED_DIRECTION_STATUSES:
                    continue

                # direction_id is provenance, not a kill-tether: a retired
                # direction must never cancel in-flight/completed work. Always
                # detach (clear direction_id) and keep the task EVALUATING; the
                # generic EVALUATING sweep resolves it via its own children/PR.
                logger.warning(
                    "[REAPER] Detaching EVALUATING task %s from retired direction "
                    "%s (%s) — provenance only, not cancelling",
                    str(task.id)[:8],
                    did,
                    dir_status,
                )
                ctx = dict(task.context or {})
                ctx.pop("direction_id", None)
                task.context = ctx
                flag_modified(task, "context")
                session.add(task)
                detached += 1

        if detached:
            await session.commit()
            logger.info(
                "[REAPER] Detached %d EVALUATING tasks from retired directions",
                detached,
            )
    except Exception as e:
        logger.error(
            "[REAPER] Orphaned direction provider sweep error: %s", e, exc_info=True
        )
        with contextlib.suppress(Exception):
            await session.rollback()
    return detached
