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


def _is_test_file(path: str) -> bool:
    """Return True if *path* looks like a test file.

    Test files are additive — concurrent edits rarely cause merge conflicts —
    so the dispatch filters exclude them from overlap calculations.
    """
    if not path:
        return False
    p = path.replace("\\", "/")
    if "/testing/" in p or "/__tests__/" in p or "/tests/" in p:
        return True
    name = p.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    if "_spec." in name:
        return True
    return False


def _prod_files(files) -> set[str]:
    return {f for f in (files or []) if not _is_test_file(f)}


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
            files = _prod_files((t.context or {}).get("related_files", []))
            if files:
                in_progress_files_by_project.setdefault(t.project_id, set()).update(
                    files
                )

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
        candidate_files = _prod_files(
            (candidate.context or {}).get("related_files", [])
        )
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
        is_large = (
            scope == "large" or task_type == "plan"
        )  # refactors use standard threshold
        # Otherwise: defer if at least 2 files overlap AND >= 50% of candidate's files
        should_defer = is_large or (
            len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5
        )
        if should_defer:
            overlapping_files = ", ".join(sorted(overlap))[:120]
            blocking_task = next(
                (
                    t
                    for t in active_tasks
                    if t.status == TaskStatus.IN_PROGRESS
                    and t.project_id == candidate.project_id
                    and set((t.context or {}).get("related_files", [])) & overlap
                ),
                None,
            )
            blocking_title = (
                f" (blocked by: {blocking_task.title[:60]})" if blocking_task else ""
            )
            defer_msg = f"Waiting: file conflict on {overlapping_files}{blocking_title}"
            if candidate.latest_status_message != defer_msg:
                candidate.latest_status_message = defer_msg
                session.add(candidate)
                try:
                    await broadcast_fn(
                        str(candidate.project_id),
                        "task_updated",
                        {"task_id": str(eid), "status": "QUEUED", "message": defer_msg},
                    )
                except Exception:
                    logger.debug(
                        f"Failed to broadcast deferral for {eid}", exc_info=True
                    )
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

    eval_parent_ids = (
        (
            await session.execute(
                select(Task.id).where(Task.status == TaskStatus.EVALUATING)
            )
        )
        .scalars()
        .all()
    )
    if not eval_parent_ids:
        return eligible_ids
    eval_parent_id_set = set(eval_parent_ids)

    pending_children = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id.in_(eval_parent_ids),
                    Task.status == TaskStatus.COMPLETED,
                )
            )
        )
        .scalars()
        .all()
    )

    # Build per-project rollup file sets (different repos can't conflict)
    pending_rollup_by_project: dict[uuid.UUID, dict[str, str]] = {}
    for pc in pending_children:
        pc_scope = (pc.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        pc_type = (pc.first_payload).get("type", "")
        if pc_scope == "large" or pc_type in ("refactor", "plan"):
            rollup_files = pending_rollup_by_project.setdefault(pc.project_id, {})
            for f in _prod_files((pc.context or {}).get("related_files", [])):
                rollup_files[f] = pc.title or str(pc.id)

    if not pending_rollup_by_project:
        return eligible_ids

    non_rollup_conflicting = []
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if not candidate:
            non_rollup_conflicting.append(eid)
            continue
        # Siblings of the rollup parent (review/fix/ci tasks for sibling PRs, or
        # intra-tree refactors) live on the same rollup branch and are part of
        # the rollup itself — not external work blocked by it. Deferring them
        # would deadlock the rollup: e.g. a review task for a child's PR shares
        # related_files with that child by construction, so blocking it would
        # prevent the review that's required to land the child into the rollup.
        if candidate.parent_task_id in eval_parent_id_set:
            non_rollup_conflicting.append(eid)
            continue
        # Only check rollup conflicts within the same project
        project_rollup_files = pending_rollup_by_project.get(candidate.project_id, {})
        if not project_rollup_files:
            non_rollup_conflicting.append(eid)
            continue
        cfiles = _prod_files((candidate.context or {}).get("related_files", []))
        if not cfiles:
            non_rollup_conflicting.append(eid)
            continue
        stale_overlap = cfiles & project_rollup_files.keys()
        # Only defer on significant overlap: 2+ files AND >= 50% of candidate's files
        should_defer = (
            len(stale_overlap) >= 2 and len(stale_overlap) >= len(cfiles) * 0.5
        )
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
                    await broadcast_fn(
                        str(candidate.project_id),
                        "task_updated",
                        {"task_id": str(eid), "status": "QUEUED", "message": defer_msg},
                    )
                except Exception:
                    logger.debug(
                        f"Failed to broadcast rollup deferral for {eid}", exc_info=True
                    )
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
        # Investigation tasks are the escape hatch for held directions —
        # blocking them would strand the hold until the 2-hour timeout.
        if (candidate.first_payload).get("type") == "investigate":
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
                logger.debug(
                    f"Failed to broadcast direction-hold deferral for {eid}",
                    exc_info=True,
                )
        logger.info(
            f"[DISPATCH] Deferring task {eid} — direction {direction_id} is held: {reason}"
        )
    return non_held


def _is_migration_task(task: Task) -> bool:
    """Return True if *task* produces an alembic migration file.

    Three cases qualify: explicit ``migrate``-type tasks; tasks whose
    ``related_files`` includes a path under ``backend/alembic/versions/``;
    and tasks whose ``related_files`` includes ``backend/app/models.py``
    (the agent's standard handler auto-generates a migration when models.py
    changes without a corresponding alembic file — see needsMigrationGeneration
    in agent/src/handlers/standard.ts).
    """
    fp = task.first_payload
    if fp.get("type") == "migrate":
        return True
    files = (task.context or {}).get("related_files") or []
    for f in files:
        if not f:
            continue
        normalized = f.replace("\\", "/")
        if "backend/alembic/versions/" in normalized:
            return True
        if normalized == "backend/app/models.py" or normalized.endswith(
            "/backend/app/models.py"
        ):
            return True
    return False


async def filter_migration_serialization(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    active_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Defer migration-producing candidates while another runs in the same project.

    Two concurrent migrate tasks (or create tasks that auto-generate alembic
    revisions) read the same latest revision and each generates the next one,
    producing duplicate revisions with the same down_revision. Serializing
    dispatch per project prevents that broken-chain scenario.
    """
    if not eligible_ids:
        return eligible_ids

    in_progress_by_project: dict[uuid.UUID, Task] = {}
    for t in active_tasks:
        if t.status != TaskStatus.IN_PROGRESS:
            continue
        if not _is_migration_task(t):
            continue
        in_progress_by_project.setdefault(t.project_id, t)

    if not in_progress_by_project:
        return eligible_ids

    non_blocked = []
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if candidate is None or not _is_migration_task(candidate):
            non_blocked.append(eid)
            continue
        blocker = in_progress_by_project.get(candidate.project_id)
        if blocker is None or blocker.id == candidate.id:
            non_blocked.append(eid)
            continue
        defer_msg = (
            f"Waiting: serializing migration — task {str(blocker.id)[:8]} "
            f"is already producing a migration in this project"
        )
        if candidate.latest_status_message != defer_msg:
            candidate.latest_status_message = defer_msg
            session.add(candidate)
            try:
                await broadcast_fn(
                    str(candidate.project_id),
                    "task_updated",
                    {"task_id": str(eid), "status": "QUEUED", "message": defer_msg},
                )
            except Exception:
                logger.debug(
                    f"Failed to broadcast migration deferral for {eid}", exc_info=True
                )
        logger.info(
            f"[DISPATCH] Deferring migration task {eid} — blocked by {blocker.id} in project {candidate.project_id}"
        )
    return non_blocked


# Task types exempt from SCOPE_EMPTY dispatch pre-emption. A review task
# reviews a concrete PR, a fix task addresses review feedback on one, and a
# plan task produces a plan — all three inherit ``auto_decomposed`` and
# ``related_files`` from the create-task they descend from, but a sibling
# cluster merging the same files does not make their work redundant. Pre-empting
# them strands the PR and triggers an unbounded review-respawn loop.
_SCOPE_EMPTY_EXEMPT_TASK_TYPES = {"review", "fix", "plan"}


async def filter_scope_empty(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Pre-empt auto-decomposed cluster siblings whose scope is fully covered.

    When a QUEUED task carries ``auto_decomposed=True`` and a sibling task
    (same parent_task_id) has already reached COMPLETED status with overlapping
    related_files, the candidate's work was already done by the sibling.
    Transition the candidate directly to FAILED with SCOPE_EMPTY so it does
    NOT consume the retry budget and does NOT penalize direction health.

    Only the *original implementation task* of a cluster is pre-emptible.
    Review and fix tasks inherit ``auto_decomposed`` from the create-task they
    descend from, but they operate on a concrete PR — a sibling cluster
    covering the same files does not make a review or fix redundant. Pre-empting
    them strands the PR with no review and spawns an unbounded review-respawn
    loop (2026-05-09 incident: 795 SCOPE_EMPTY review tasks in 9.5h), so the
    task types in ``_SCOPE_EMPTY_EXEMPT_TASK_TYPES`` are exempt.
    """
    if not eligible_ids:
        return eligible_ids

    auto_decomp = [
        t
        for t in candidate_tasks
        if t.id in eligible_ids
        and (t.context or {}).get("auto_decomposed")
        and t.parent_task_id is not None
        # Exempt review/fix/plan — they inherit auto_decomposed but are not
        # decomposition clusters; pre-empting them loops (see docstring).
        and t.first_payload.get("type") not in _SCOPE_EMPTY_EXEMPT_TASK_TYPES
    ]
    if not auto_decomp:
        return eligible_ids

    parent_ids = list({t.parent_task_id for t in auto_decomp})
    completed_siblings = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id.in_(parent_ids),
                    Task.status == TaskStatus.COMPLETED,
                )
            )
        )
        .scalars()
        .all()
    )
    if not completed_siblings:
        return eligible_ids

    covered_by_parent: dict[uuid.UUID, set[str]] = {}
    sibling_title_by_parent: dict[uuid.UUID, dict[str, str]] = {}
    for sib in completed_siblings:
        files = _prod_files((sib.context or {}).get("related_files", []))
        if files:
            covered_by_parent.setdefault(sib.parent_task_id, set()).update(files)
            titles = sibling_title_by_parent.setdefault(sib.parent_task_id, {})
            for f in files:
                titles.setdefault(f, sib.title or str(sib.id))

    from app.failure_classification import SCOPE_EMPTY, persist_failure_category  # noqa: PLC0415

    pre_empted: set[uuid.UUID] = set()
    for candidate in auto_decomp:
        cfiles = _prod_files((candidate.context or {}).get("related_files", []))
        if not cfiles:
            continue
        covered = covered_by_parent.get(candidate.parent_task_id, set())
        if not cfiles.issubset(covered):
            continue
        # All declared files already covered by a completed sibling — pre-empt.
        titles = sibling_title_by_parent.get(candidate.parent_task_id, {})
        sibling_title = next((titles[f] for f in cfiles if f in titles), None)
        msg = "SCOPE_EMPTY: all declared files already covered by completed sibling" + (
            f" '{sibling_title[:60]}'" if sibling_title else ""
        )
        candidate.status = TaskStatus.FAILED
        candidate.latest_status_message = msg
        persist_failure_category(candidate, SCOPE_EMPTY)
        session.add(candidate)
        pre_empted.add(candidate.id)
        try:
            await broadcast_fn(
                str(candidate.project_id),
                "task_updated",
                {"task_id": str(candidate.id), "status": "FAILED", "message": msg},
            )
        except Exception:
            logger.debug(
                f"Failed to broadcast SCOPE_EMPTY for {candidate.id}", exc_info=True
            )
        logger.info(
            f"[DISPATCH] SCOPE_EMPTY: pre-empting task {candidate.id} — "
            f"all {len(cfiles)} file(s) covered by completed sibling under parent {candidate.parent_task_id}"
        )

    return [eid for eid in eligible_ids if eid not in pre_empted]


async def filter_pipeline_duplicates(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    session,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
) -> list[uuid.UUID]:
    """Cancel review/fix candidates that would duplicate an already-active task.

    Belt-and-suspenders check against _spawn_pipeline_task's spawn-time dedup:
    two concurrent spawns can race past the TOCTOU window on the active-task
    query, or can resolve different ``original_task_id`` values for what is
    effectively the same chain. This filter runs right before provisioning
    and catches both cases by keying only on (pr_number, task_type).

    Safety invariants — never break the pipeline:
      * Only a review or fix candidate can be filtered here. Other task
        types pass through unaffected.
      * A candidate is cancelled only if SOMEONE ELSE on the same
        (pr_number, task_type) is either (a) already claimed (PROVISIONING
        / IN_PROGRESS / EVALUATING) or (b) an older QUEUED sibling still
        in this eligible batch. The sole survivor — or the oldest of a set
        of QUEUED tie — is always preserved.
      * Cancellation marks the loser CANCELLED with a clear message so
        operators can see it happened; broadcasts the event.
    """
    if not eligible_ids:
        return eligible_ids

    # Limit the DB fetch to review/fix candidates — non-pipeline tasks pass
    # through, so we don't need to check them.
    pipeline_candidates = [
        t
        for t in candidate_tasks
        if t.id in eligible_ids
        and (t.first_payload).get("type") in ("review", "fix")
        and (t.first_payload).get("pr_number") is not None
    ]
    if not pipeline_candidates:
        return eligible_ids

    project_ids = {t.project_id for t in pipeline_candidates}
    # Fetch ALL active (non-terminal, non-cancelled) review/fix tasks in these
    # projects, fully loaded. We need payloads for pr_number/task_type lookup.
    active_pipeline = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id.in_(project_ids),
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    # Group by (project_id, pr_number, task_type). Using project_id in the
    # key guards against the unlikely case of two projects reusing PR numbers.
    groups: dict[tuple[uuid.UUID, int, str], list[Task]] = {}
    for t in active_pipeline:
        fp = t.first_payload
        ttype = fp.get("type")
        pr = fp.get("pr_number")
        if ttype not in ("review", "fix") or pr is None:
            continue
        groups.setdefault((t.project_id, pr, ttype), []).append(t)

    RUNNING = {TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING}
    survivors: list[uuid.UUID] = []
    for eid in eligible_ids:
        candidate = next((t for t in pipeline_candidates if t.id == eid), None)
        if candidate is None:
            # Not a pipeline candidate — pass through unchanged.
            survivors.append(eid)
            continue

        fp = candidate.first_payload
        key = (candidate.project_id, fp.get("pr_number"), fp.get("type"))
        group = groups.get(key, [])
        # Drop self from the group for the comparison below.
        others = [t for t in group if t.id != candidate.id]
        if not others:
            survivors.append(eid)
            continue

        # (a) Any other task is already claimed → cancel this one.
        running_other = next((t for t in others if t.status in RUNNING), None)
        if running_other:
            from app.failure_classification import (
                AGENT_TIMEOUT,
                persist_failure_category,
            )  # noqa: PLC0415

            candidate.status = TaskStatus.CANCELLED
            candidate.latest_status_message = (
                f"Deduplicated: {fp.get('type')} task {str(running_other.id)[:8]} "
                f"is already {running_other.status.value if hasattr(running_other.status, 'value') else running_other.status} "
                f"on PR #{fp.get('pr_number')}"
            )
            persist_failure_category(candidate, AGENT_TIMEOUT)
            session.add(candidate)
            try:
                await broadcast_fn(
                    str(candidate.project_id),
                    "task_updated",
                    {
                        "task_id": str(eid),
                        "status": "CANCELLED",
                        "message": candidate.latest_status_message,
                    },
                )
            except Exception:
                logger.debug(
                    f"Failed to broadcast dedup cancel for {eid}", exc_info=True
                )
            logger.warning(
                f"[DISPATCH] Cancelling duplicate {fp.get('type')} {eid} — "
                f"{running_other.status} sibling {running_other.id} on PR #{fp.get('pr_number')}"
            )
            continue

        # (b) Only QUEUED siblings → keep the oldest, cancel newer duplicates.
        # Ties broken by created_at then id (stable). Only cancel if THIS
        # candidate is strictly newer than some QUEUED sibling.
        queued_others = [t for t in others if t.status == TaskStatus.QUEUED]
        if queued_others:
            oldest_queued = min(queued_others, key=lambda t: (t.created_at, t.id))
            if (candidate.created_at, candidate.id) > (
                oldest_queued.created_at,
                oldest_queued.id,
            ):
                from app.failure_classification import (
                    AGENT_TIMEOUT,
                    persist_failure_category,
                )  # noqa: PLC0415

                candidate.status = TaskStatus.CANCELLED
                candidate.latest_status_message = (
                    f"Deduplicated: older QUEUED {fp.get('type')} task "
                    f"{str(oldest_queued.id)[:8]} already exists on PR #{fp.get('pr_number')}"
                )
                persist_failure_category(candidate, AGENT_TIMEOUT)
                session.add(candidate)
                try:
                    await broadcast_fn(
                        str(candidate.project_id),
                        "task_updated",
                        {
                            "task_id": str(eid),
                            "status": "CANCELLED",
                            "message": candidate.latest_status_message,
                        },
                    )
                except Exception:
                    logger.debug(
                        f"Failed to broadcast dedup cancel for {eid}", exc_info=True
                    )
                logger.warning(
                    f"[DISPATCH] Cancelling duplicate {fp.get('type')} {eid} — "
                    f"older QUEUED sibling {oldest_queued.id} on PR #{fp.get('pr_number')}"
                )
                continue

        survivors.append(eid)
    return survivors
