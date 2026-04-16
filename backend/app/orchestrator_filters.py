"""Dispatch conflict filters for the orchestrator.

Extracted from orchestrator.py to reduce module size.  Contains the async
filter passes that run between dependency/deadlock resolution and final
slot-capping:

  - filter_file_conflicts: defer tasks that overlap with IN_PROGRESS work
  - filter_rollup_conflicts: defer tasks that overlap with pending-rollup files
  - filter_held_directions: defer tasks belonging to directions that are
    currently held due to consecutive code-quality failures
"""

import logging
import uuid
from typing import Callable, Coroutine, Any

from sqlmodel import select

from app.models import Task, TaskStatus
from app.shared_state import is_direction_held, DEFAULT_TASK_SCOPE

logger = logging.getLogger("blueprint")


async def filter_file_conflicts(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    active_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Defer candidates that would conflict with currently IN_PROGRESS tasks.

    Builds per-project sets of files being modified by IN_PROGRESS tasks, then
    removes from *eligible_ids* any candidate whose related_files overlap
    significantly (large-scope = any overlap; standard = 2+ files AND >= 50%).

    Updates the candidate's latest_status_message and broadcasts a deferral
    event when a task is deferred.  Returns the filtered list of eligible_ids.
    """
    # Build per-project sets of files being modified by IN_PROGRESS tasks
    in_progress_files_by_project: dict[uuid.UUID, set[str]] = {}
    for t in active_tasks:
        if t.status == TaskStatus.IN_PROGRESS:
            files = (t.context or {}).get("related_files", [])
            if files:
                in_progress_files_by_project.setdefault(t.project_id, set()).update(files)

    if not in_progress_files_by_project:
        return eligible_ids

    non_conflicting = []
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if candidate is None:
            non_conflicting.append(eid)
            continue
        # Only check conflicts within the same project
        project_files = in_progress_files_by_project.get(candidate.project_id, set())
        if not project_files:
            non_conflicting.append(eid)
            continue
        candidate_files = set((candidate.context or {}).get("related_files", []))
        if not candidate_files:
            non_conflicting.append(eid)
            continue
        overlap = candidate_files & project_files
        if not overlap:
            non_conflicting.append(eid)
            continue
        # Large scope tasks defer on any overlap; refactors use standard threshold
        # (refactors are expected to touch decomposition targets — overlap is intentional)
        scope = (candidate.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        task_type = (candidate.first_payload).get("type", "")
        is_large = scope == "large" or task_type == "plan"  # refactors use standard threshold
        # Otherwise: defer if at least 2 files overlap AND >= 50% of candidate's files
        should_defer = is_large or (len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5)
        if should_defer:
            overlapping_files = ', '.join(sorted(overlap))[:120]
            blocking_task = next((t for t in active_tasks if t.status == TaskStatus.IN_PROGRESS
                                  and t.project_id == candidate.project_id
                                  and set((t.context or {}).get("related_files", [])) & overlap), None)
            blocking_title = f" (blocked by: {blocking_task.title[:60]})" if blocking_task else ""
            defer_msg = f"Waiting: file conflict on {overlapping_files}{blocking_title}"
            if candidate.latest_status_message != defer_msg:
                candidate.latest_status_message = defer_msg
                session.add(candidate)
                try:
                    await broadcast_fn(str(candidate.project_id), "task_updated",
                        {"task_id": str(eid), "status": "QUEUED", "message": defer_msg})
                except Exception:
                    logger.debug(f"Failed to broadcast deferral for {eid}", exc_info=True)
            logger.info(
                f"[DISPATCH] Deferring task {eid} — file conflict "
                f"({len(overlap)}/{len(candidate_files)} files, scope={scope}): {overlapping_files}"
            )
        else:
            non_conflicting.append(eid)
    return non_conflicting


async def filter_rollup_conflicts(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Defer candidates that overlap with files belonging to a pending rollup.

    EVALUATING parents represent rollup PRs not yet merged to main; their
    children's files are stale.  Tasks that significantly overlap those files
    are deferred until the rollup merges.

    Updates the candidate's latest_status_message and broadcasts a deferral
    event when a task is deferred.  Returns the filtered list of eligible_ids.
    """
    if not eligible_ids:
        return eligible_ids

    eval_parent_ids = (await session.execute(
        select(Task.id).where(Task.status == TaskStatus.EVALUATING)
    )).scalars().all()
    if not eval_parent_ids:
        return eligible_ids

    pending_children = (await session.execute(
        select(Task).where(
            Task.parent_task_id.in_(eval_parent_ids),
            Task.status == TaskStatus.COMPLETED,
        )
    )).scalars().all()

    # Build per-project rollup file sets (different repos can't conflict)
    pending_rollup_by_project: dict[uuid.UUID, dict[str, str]] = {}
    for pc in pending_children:
        pc_scope = (pc.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        pc_type = (pc.first_payload).get("type", "")
        if pc_scope == "large" or pc_type in ("refactor", "plan"):
            rollup_files = pending_rollup_by_project.setdefault(pc.project_id, {})
            for f in (pc.context or {}).get("related_files", []):
                rollup_files[f] = pc.title or str(pc.id)

    if not pending_rollup_by_project:
        return eligible_ids

    non_rollup_conflicting = []
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if not candidate:
            non_rollup_conflicting.append(eid)
            continue
        # Only check rollup conflicts within the same project
        project_rollup_files = pending_rollup_by_project.get(candidate.project_id, {})
        if not project_rollup_files:
            non_rollup_conflicting.append(eid)
            continue
        cfiles = set((candidate.context or {}).get("related_files", []))
        if not cfiles:
            non_rollup_conflicting.append(eid)
            continue
        stale_overlap = cfiles & project_rollup_files.keys()
        # Only defer on significant overlap: 2+ files AND >= 50% of candidate's files
        should_defer = len(stale_overlap) >= 2 and len(stale_overlap) >= len(cfiles) * 0.5
        if should_defer:
            blocking_title = project_rollup_files[next(iter(stale_overlap))]
            logger.warning(
                f"[DISPATCH] Near-miss: deferring task {eid} — overlaps {len(stale_overlap)} file(s) "
                f"with pending-rollup refactor '{blocking_title[:60]}': "
                f"{', '.join(sorted(stale_overlap)[:3])}"
            )
            defer_msg = f"Waiting: overlaps pending rollup '{blocking_title[:60]}'"
            if candidate.latest_status_message != defer_msg:
                candidate.latest_status_message = defer_msg
                session.add(candidate)
                try:
                    await broadcast_fn(str(candidate.project_id), "task_updated",
                        {"task_id": str(eid), "status": "QUEUED", "message": defer_msg})
                except Exception:
                    logger.debug(f"Failed to broadcast rollup deferral for {eid}", exc_info=True)
        else:
            if stale_overlap:
                blocking_title = project_rollup_files[next(iter(stale_overlap))]
                logger.warning(
                    f"[DISPATCH] Near-miss: task {eid} overlaps {len(stale_overlap)} file(s) "
                    f"with pending-rollup refactor '{blocking_title[:60]}': "
                    f"{', '.join(sorted(stale_overlap)[:3])}"
                )
            non_rollup_conflicting.append(eid)
    return non_rollup_conflicting


async def filter_held_directions(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Defer tasks whose direction is currently held due to consecutive code failures.

    Tasks without a direction_id pass through unaffected.  When a direction is
    held, the task's latest_status_message is updated and a deferral broadcast
    is emitted so the UI reflects the hold reason.
    """
    if not eligible_ids:
        return eligible_ids

    non_held = []
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if candidate is None:
            non_held.append(eid)
            continue
        direction_id = (candidate.context or {}).get("direction_id")
        if not direction_id:
            non_held.append(eid)
            continue
        held, reason = is_direction_held(str(direction_id))
        if not held:
            non_held.append(eid)
            continue
        # Defer the task with the hold reason
        if candidate.latest_status_message != reason:
            candidate.latest_status_message = reason
            session.add(candidate)
            try:
                await broadcast_fn(
                    str(candidate.project_id),
                    "task_updated",
                    {"task_id": str(eid), "status": "QUEUED", "message": reason},
                )
            except Exception:
                logger.debug(f"Failed to broadcast direction-hold deferral for {eid}", exc_info=True)
        logger.info(
            f"[DISPATCH] Deferring task {eid} — direction {direction_id} is held: {reason}"
        )
    return non_held
