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

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.migration_path import is_migration_file
from app.models import Task, TaskStatus
from app.shared_state import is_direction_held, DEFAULT_TASK_SCOPE
from app.task_capabilities import READ_ONLY_TASK_TYPES

logger = logging.getLogger("blueprint")

# Number of consecutive dispatch cycles a candidate must stay deferred on the
# SAME precondition blocker before we raise one operator-visible alert. Kept
# low so a genuinely stuck task surfaces quickly, but >1 so a blocker that
# clears within a cycle or two never spams the Alerts banner.
_PRECONDITION_ESCALATE_THRESHOLD = 3


async def _record_defer_and_maybe_escalate(
    candidate: Task,
    session,
    *,
    blocker_kind: str,
    detail: str,
    threshold: int = _PRECONDITION_ESCALATE_THRESHOLD,
) -> None:
    """Track repeated deferral of *candidate* on the same precondition blocker.

    Persists a per-blocker defer counter in
    ``candidate.context['_precondition_defer']`` keyed by ``blocker_kind``
    (the blocker identity — overlapping files or the blocking rollup title).
    Once the count reaches *threshold* and the blocker has not yet been
    escalated, raise ONE deduplicated operator alert and stamp
    ``_precondition_escalated_key`` so it does not re-fire on every subsequent
    dispatch cycle. The counter (and escalation stamp) reset when the blocker
    identity changes, so a task that moves from one blocker to a different one
    starts a fresh persistence count.

    Alerting only — the candidate stays QUEUED and is NOT dispatched; we never
    dispatch work known to be blocked, we just make the blocker visible.
    """
    ctx = candidate.context or {}
    defer = ctx.get("_precondition_defer") or {}
    if defer.get("key") == blocker_kind:
        count = int(defer.get("count", 0)) + 1
    else:
        # New (or first) blocker identity — reset the persistence count and any
        # stale escalation stamp from a previous, now-cleared blocker.
        count = 1
        ctx.pop("_precondition_escalated_key", None)
    ctx["_precondition_defer"] = {"key": blocker_kind, "count": count}

    already_escalated = ctx.get("_precondition_escalated_key") == blocker_kind
    should_escalate = count >= threshold and not already_escalated
    if should_escalate:
        ctx["_precondition_escalated_key"] = blocker_kind

    candidate.context = ctx
    flag_modified(candidate, "context")
    session.add(candidate)

    if should_escalate:
        # Deferred import keeps app.alert_dispatch a leaf and lets tests patch
        # app.orchestrator_filters._maybe_create_precondition_blocker_alert.
        from app.alert_dispatch import (  # noqa: PLC0415
            _maybe_create_precondition_blocker_alert,
        )

        logger.warning(
            f"[DISPATCH] Escalating persistent precondition blocker on task "
            f"{candidate.id} after {count} deferrals: {blocker_kind}"
        )
        await _maybe_create_precondition_blocker_alert(
            candidate,
            session,
            blocker_kind=blocker_kind,
            detail=detail,
        )


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


def rollup_overlap_defers(
    candidate_files: set[str], rollup_files
) -> tuple[bool, set[str]]:
    """Decide whether a candidate should be deferred for pending-rollup overlap.

    The deferral threshold (shared single source of truth for
    ``filter_rollup_conflicts``): defer only on *significant* overlap — 2+
    files AND >= 50% of the candidate's files overlap the pending-rollup set.

    Args:
        candidate_files: the candidate task's production files (test files
            already excluded via ``_prod_files``).
        rollup_files: the pending-rollup file set (any iterable of paths; a
            dict's keys are accepted directly).

    Returns:
        ``(should_defer, stale_overlap)`` — the boolean decision and the set of
        overlapping files (for logging / status messages).
    """
    rollup_keys = rollup_files.keys() if hasattr(rollup_files, "keys") else rollup_files
    stale_overlap = candidate_files & set(rollup_keys)
    should_defer = (
        len(stale_overlap) >= 2 and len(stale_overlap) >= len(candidate_files) * 0.5
    )
    return should_defer, stale_overlap


def _coerce_uuid(raw) -> "uuid.UUID | None":
    """Parse *raw* into a UUID, returning None on any malformed value.

    Kept as a standalone helper (rather than an inline ``try`` in the
    ``collect_dep_uuids`` loop) so the exception handling lives outside the
    hot loop body — both readable and PERF-clean.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        # ValueError: malformed hex string. AttributeError: non-str without a
        # .replace (e.g. an int). TypeError: None or a bytes-like value passed
        # positionally (uuid.UUID(None) / uuid.UUID(b"...")). Any of these is a
        # bad dep id that must be SKIPPED, not allowed to crash the shared
        # batched dep-fetch (collect_dep_uuids) for every other candidate.
        return None


def collect_dep_uuids(candidate_tasks: list[Task]) -> set:
    """Collect every valid ``depends_on`` UUID across all candidates.

    Helper for the dispatch loop's single batched dependency fetch (the N+1
    fix): one ``IN (...)`` query over this set replaces the old per-candidate
    ``select(Task).where(Task.id.in_(dep_uuids))``. Malformed dep ids are
    silently skipped here — per-candidate validation (with error logging) is
    still re-run inside the dispatch loop, so one bad row can't poison the
    shared batch nor swallow the diagnostic.
    """
    out: set = set()
    for cand in candidate_tasks:
        for d in cand.depends_on or []:
            du = _coerce_uuid(d)
            if du is not None:
                out.add(du)
    return out


def apply_pipeline_cap(
    eligible_ids: list,
    types_for_cap: dict,
    pipeline_active: int,
    max_pipeline_slots: int,
) -> tuple[list, int]:
    """Apply the review/fix pipeline-slot cap to an eligible-id list.

    Pure single source of truth for the cap selection in ``cap_pipeline_slots``
    (Step 2.9): non-pipeline tasks always pass; review/fix tasks are kept only
    while the remaining budget (``max_pipeline_slots`` minus already-active
    review/fix tasks) is not yet exhausted, otherwise deferred.

    Args:
        eligible_ids: candidate ids in dispatch order.
        types_for_cap: mapping ``id -> task type string`` (missing/None types
            are treated as non-pipeline and always kept).
        pipeline_active: count of currently active review/fix tasks.
        max_pipeline_slots: the configured pipeline-slot ceiling.

    Returns:
        ``(kept, deferred_count)`` — the retained ids (order preserved) and the
        number of review/fix tasks deferred for budget exhaustion.
    """
    budget = max(0, max_pipeline_slots - pipeline_active)
    kept: list = []
    deferred = 0
    pipeline_kept = 0
    for tid in eligible_ids:
        if types_for_cap.get(tid) in ("review", "fix"):
            if pipeline_kept < budget:
                pipeline_kept += 1
                kept.append(tid)
            else:
                deferred += 1
        else:
            kept.append(tid)
    return kept, deferred


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
        # Read-only task types (review, plan, research, the audits, memo, ...)
        # never push a branch or open a PR — they produce a verdict / plan /
        # findings, not a diff — so they CANNOT cause a merge conflict. Exempt
        # them from the write-conflict gate entirely. Gating them just serialized
        # the pipeline against hot files (orchestrator.py / task_lifecycle.py /
        # models.py) that nearly every change touches, and needlessly held
        # read-only work (e.g. a code review) behind unrelated in-flight writes.
        # Real write-write conflicts are still caught on the WRITE-type tasks
        # (create/fix/refactor/migrate/docs/deps/qa).
        task_type = (candidate.first_payload).get("type", "")
        if task_type in READ_ONLY_TASK_TYPES:
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
        # Large scope tasks defer on any overlap; standard scope uses the 2-file
        # / 50% threshold (refactors are expected to touch decomposition targets —
        # overlap is intentional). NB: plan (and every other read-only type) is
        # exempted above, so the old ``or task_type == "plan"`` clause is gone.
        scope = (candidate.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        is_large = scope == "large"
        # Otherwise: defer if at least 2 files overlap AND >= 50% of candidate's files
        should_defer = is_large or (
            len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5
        )
        if should_defer:
            overlapping_blockers = [
                t
                for t in active_tasks
                if t.status == TaskStatus.IN_PROGRESS
                and t.project_id == candidate.project_id
                and _prod_files((t.context or {}).get("related_files", [])) & overlap
            ]
            # Cross-parent looseness: two WRITE tasks under DIFFERENT parents are
            # different features whose PRs target different rollup branches — they
            # share no immediate merge target, so overlapping a hot file
            # (orchestrator.py / models.py / task_lifecycle.py) is rarely a real
            # conflict; the eventual rollup/automain merge + the back-merge
            # resolver catch the rare one. Only HOLD when an overlapping in-flight
            # task is a SAME-PARENT sibling (the plan's coordinated decomposition
            # merges to the same rollup branch). ALWAYS hold large-scope tasks
            # (wide rewrites genuinely conflict across the board), and stay
            # conservative on missing data — a null parent on either side keeps
            # the hold.
            cand_parent = candidate.parent_task_id
            if (
                not is_large
                and cand_parent is not None
                and overlapping_blockers
                and all(
                    t.parent_task_id is not None and t.parent_task_id != cand_parent
                    for t in overlapping_blockers
                )
            ):
                non_conflicting.append(eid)
                logger.info(
                    "[DISPATCH] Not deferring task %s — file overlap is cross-parent "
                    "(different rollups, %d file(s): %s); real conflicts, if any, "
                    "resolve at merge",
                    eid,
                    len(overlap),
                    ", ".join(sorted(overlap))[:80],
                )
                continue
            overlapping_files = ", ".join(sorted(overlap))[:120]
            blocking_task = overlapping_blockers[0] if overlapping_blockers else None
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
            # Track persistence — if this candidate keeps deferring on the same
            # overlapping files across dispatch cycles, raise one operator alert.
            await _record_defer_and_maybe_escalate(
                candidate,
                session,
                blocker_kind=f"file conflict on {overlapping_files}",
                detail=defer_msg,
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
        # Only defer on significant overlap: 2+ files AND >= 50% of candidate's
        # files. Shared with TestRollupDeferral via rollup_overlap_defers().
        should_defer, stale_overlap = rollup_overlap_defers(
            cfiles, project_rollup_files
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
            # Track persistence — if this candidate keeps deferring on the same
            # pending rollup across dispatch cycles, raise one operator alert.
            await _record_defer_and_maybe_escalate(
                candidate,
                session,
                blocker_kind=f"pending rollup '{blocking_title[:60]}'",
                detail=defer_msg,
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
        if is_migration_file(normalized):
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


def _decomposition_cluster_key(task: Task) -> "tuple[uuid.UUID, int] | None":
    """Identity of the auto-decomposition cluster group a task belongs to.

    Clusters of ONE finding share ``(parent_task_id, finding_index)``. The
    ``parent_task_id`` alone is the *research task*, which fans a single
    research pass out across multiple findings (13 tasks across 5 findings
    is typical) — so two tasks sharing only ``parent_task_id`` are not
    necessarily decomposition siblings.

    Returns ``None`` when the task cannot be placed in a cluster group (no
    parent, or no ``finding_index``); such a task must not be SCOPE_EMPTY
    pre-empted, because coincidental file overlap with an unrelated finding
    is not proof of redundant work (2026-05-21 incident: a finding-#1 task
    touching ``admin_digest.py`` wrongly pre-empted a finding-#2 cluster).
    """
    finding_index = (task.context or {}).get("finding_index")
    if task.parent_task_id is None or finding_index is None:
        return None
    return (task.parent_task_id, finding_index)


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
    Transition the candidate directly to CANCELLED with SCOPE_EMPTY so it
    does NOT consume the retry budget and does NOT penalize direction health
    — a no-op exit (work already covered) is not a failure.

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

    # Group coverage by decomposition-cluster identity — (parent_task_id,
    # finding_index) — NOT by parent_task_id alone. A completed task from a
    # *different finding* under the same research parent that happens to
    # touch an overlapping file does not make this cluster's work redundant.
    covered_by_cluster: dict[tuple[uuid.UUID, int], set[str]] = {}
    sibling_title_by_cluster: dict[tuple[uuid.UUID, int], dict[str, str]] = {}
    for sib in completed_siblings:
        cluster_key = _decomposition_cluster_key(sib)
        if cluster_key is None:
            continue
        files = _prod_files((sib.context or {}).get("related_files", []))
        if files:
            covered_by_cluster.setdefault(cluster_key, set()).update(files)
            titles = sibling_title_by_cluster.setdefault(cluster_key, {})
            for f in files:
                titles.setdefault(f, sib.title or str(sib.id))

    from app.failure_classification import SCOPE_EMPTY, persist_failure_category  # noqa: PLC0415

    pre_empted: set[uuid.UUID] = set()
    for candidate in auto_decomp:
        cluster_key = _decomposition_cluster_key(candidate)
        if cluster_key is None:
            # Cannot place this task in a cluster group — never pre-empt.
            continue
        cfiles = _prod_files((candidate.context or {}).get("related_files", []))
        if not cfiles:
            continue
        covered = covered_by_cluster.get(cluster_key, set())
        if not cfiles.issubset(covered):
            continue
        # All declared files already covered by a completed sibling cluster
        # of the same finding — pre-empt.
        titles = sibling_title_by_cluster.get(cluster_key, {})
        sibling_title = next((titles[f] for f in cfiles if f in titles), None)
        msg = "SCOPE_EMPTY: all declared files already covered by completed sibling" + (
            f" '{sibling_title[:60]}'" if sibling_title else ""
        )
        candidate.status = TaskStatus.CANCELLED
        candidate.latest_status_message = msg
        persist_failure_category(candidate, SCOPE_EMPTY)
        session.add(candidate)
        pre_empted.add(candidate.id)
        # Same root cause as the dispatch dedup branches below — see
        # c56fa118 / PR #5983 (2026-05-26). SCOPE_EMPTY here fires
        # pre-dispatch so the candidate hasn't opened a PR yet in the
        # common case, but call the helper anyway: it's idempotent if
        # there's no _pr_url in context.
        from app.task_lifecycle import close_orphan_pr_if_any

        await close_orphan_pr_if_any(
            candidate, session, reason="dispatch_scope_empty_preempt"
        )
        try:
            await broadcast_fn(
                str(candidate.project_id),
                "task_updated",
                {"task_id": str(candidate.id), "status": "CANCELLED", "message": msg},
            )
        except Exception:
            logger.debug(
                f"Failed to broadcast SCOPE_EMPTY for {candidate.id}", exc_info=True
            )
        logger.info(
            f"[DISPATCH] SCOPE_EMPTY: pre-empting task {candidate.id} — "
            f"all {len(cfiles)} file(s) covered by a completed sibling cluster "
            f"of finding {cluster_key[1]} under research parent {cluster_key[0]}"
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
            # If the dedup loser already opened a PR (race: candidate had run
            # past the spawn-time dedup window before this filter caught it),
            # close that PR so it doesn't hang the parent rollup gate.
            # Regression for c56fa118 / PR #5983 (2026-05-26).
            from app.task_lifecycle import close_orphan_pr_if_any

            await close_orphan_pr_if_any(
                candidate, session, reason="dispatch_dedup_running_sibling"
            )
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
                # See comment above re: c56fa118 / PR #5983.
                from app.task_lifecycle import close_orphan_pr_if_any

                await close_orphan_pr_if_any(
                    candidate, session, reason="dispatch_dedup_queued_sibling"
                )
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


async def cap_pipeline_slots(
    eligible_ids: list,
    candidate_tasks: list[Task],
    runtime_config: dict,
    session,
) -> list:
    """Cap self-perpetuating review/fix chains to a configured slot budget.

    Step 2.9 of the dispatch loop. review/fix chains are self-perpetuating
    (review spawns fix spawns review, up to 16 iterations per PR), so without a
    ceiling they can fill every VM slot and starve create/research tasks. Defer
    (keep QUEUED) review/fix tasks once the configured pipeline budget — the
    cap minus the count of already-active review/fix tasks — is exhausted.

    The budget defaults to ``max(1, max_concurrent_vms // 2)`` when
    ``runtime_config['max_pipeline_slots']`` is unset or non-numeric. Returns the
    filtered ``eligible_ids`` with deferred review/fix ids removed in place.
    """
    max_pipeline_slots = runtime_config.get("max_pipeline_slots")
    if max_pipeline_slots is None:
        max_pipeline_slots = max(1, runtime_config["max_concurrent_vms"] // 2)
    try:
        max_pipeline_slots = int(max_pipeline_slots)
    except (TypeError, ValueError):
        max_pipeline_slots = max(1, runtime_config["max_concurrent_vms"] // 2)
    if not (eligible_ids and max_pipeline_slots > 0):
        return eligible_ids

    pipeline_active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status.in_(
                    [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                ),
                Task.payloads[0]["type"].as_string().in_(("review", "fix")),
            )
        )
    ).scalar() or 0
    types_for_cap = {
        t.id: ((t.first_payload or {}).get("type") or "") for t in candidate_tasks
    }
    # Shared cap selection (single source of truth, also exercised by
    # TestPipelineSlotCap): keep review/fix only while budget remains.
    kept, deferred_pipeline = apply_pipeline_cap(
        eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots
    )
    if deferred_pipeline:
        logger.info(
            f"[ORCHESTRATOR] Pipeline-slot cap: deferred {deferred_pipeline} "
            f"review/fix task(s) (active={pipeline_active}, cap={max_pipeline_slots})"
        )
    return kept


def reserve_slots_for_types(
    eligible_ids: list,
    candidate_tasks: list[Task],
    runtime_config: dict,
    slots_available: int,
) -> list:
    """Promote reserved task types into the dispatch window under queue pressure.

    Ensures high-priority task types (e.g. create, research) get a minimum share
    of slots when they're queued behind self-perpetuating review/fix chains. Only
    activates when queue pressure exists (more eligible tasks than open slots) and
    there are reserved-type tasks waiting beyond the dispatch cutoff. The reserved
    minimums come from ``runtime_config['reserved_slots_for_types']`` (defaults to
    ``{"create": 1, "research": 1}``).

    Returns the reordered ``eligible_ids`` (promoted reserved tasks moved into the
    first ``slots_available`` positions, displacing non-reserved tasks).
    """
    reserved_config = (
        runtime_config.get(
            "reserved_slots_for_types", {"create": 1, "research": 1}
        )
        or {}
    )
    if not (reserved_config and len(eligible_ids) > slots_available):
        return eligible_ids

    types_by_id = {
        t.id: ((t.first_payload or {}).get("type") or "") for t in candidate_tasks
    }
    protected = set(reserved_config.keys())
    initial = list(eligible_ids[:slots_available])
    waiting = list(eligible_ids[slots_available:])
    for rtype, rn in reserved_config.items():
        try:
            rn = int(rn)
        except (TypeError, ValueError):
            continue
        if rn <= 0:
            continue
        current = sum(1 for tid in initial if types_by_id.get(tid) == rtype)
        needed = rn - current
        if needed <= 0:
            continue
        promote = [tid for tid in waiting if types_by_id.get(tid) == rtype][:needed]
        if not promote:
            continue
        displaced = 0
        for i in range(len(initial) - 1, -1, -1):
            if displaced >= len(promote):
                break
            if types_by_id.get(initial[i]) not in protected:
                initial.pop(i)
                displaced += 1
        if displaced == 0:
            continue
        promote = promote[:displaced]
        promoted_set = set(promote)
        waiting = [tid for tid in waiting if tid not in promoted_set]
        initial.extend(promote)
        logger.info(
            f"[ORCHESTRATOR] Slot reservation: promoted {len(promote)} '{rtype}' "
            f"task(s) (reserved={rn}, displaced {displaced} non-reserved)"
        )
    return initial + waiting


def _project_fairness_weight(priority) -> int:
    """Map a task priority to a round-robin weight (turns per round).

    Higher-priority work earns proportionally more turns per round so a
    project whose queued tasks are urgent is serviced faster than one whose
    tasks are routine — the "weighted by tier" knob. With no dedicated tier
    column, ``Task.priority`` is the available signal (already loaded on the
    candidate batch). The mapping is intentionally coarse and bounded so a
    single high-priority project still cannot starve the others: weight grows
    by ~1 turn per priority point, clamped to ``[1, 4]``. NULL/unknown
    priority normalizes to the baseline weight of 1.
    """
    try:
        p = int(priority)
    except (TypeError, ValueError):
        p = 0
    return max(1, min(4, 1 + p))


def apply_project_fairness(
    eligible_ids: list,
    project_by_id: dict,
    weight_by_id: dict | None = None,
) -> list:
    """Round-robin ``eligible_ids`` across projects so no one project starves.

    Pure single source of truth for the per-project fairness pass (Step 2.95,
    applied BEFORE the slot cap at ``eligible_ids[:slots_available]``). Without
    it, the candidate scan's global ``priority desc, created_at asc`` ordering
    lets a single autonomous project that fanned out dozens of tasks occupy the
    entire dispatch window, starving every other project for multiple cycles
    (violates the multi-project fairness promise).

    The reorder interleaves projects in a weighted round-robin while preserving
    each project's *internal* order (so the per-project priority/age ordering
    from the candidate scan is untouched). Behavior-preserving for valid
    dispatch: this is a permutation of ``eligible_ids`` — no id is dropped or
    added, every eligible task remains dispatchable, and the downstream slot
    cap still truncates to ``slots_available``.

    Args:
        eligible_ids: candidate ids in current dispatch order.
        project_by_id: mapping ``id -> project_id`` (a None project_id is
            treated as its own bucket, keyed by None).
        weight_by_id: optional mapping ``id -> int`` round-robin weight; the
            first-seen id per project sets that project's weight. Defaults to
            weight 1 for every project (plain round-robin).

    Returns:
        The reordered ``eligible_ids`` (a permutation; same length / membership).
    """
    if len(eligible_ids) <= 1:
        return eligible_ids

    # Group ids per project, preserving each project's internal order. Project
    # buckets are kept in first-appearance order so the highest-priority
    # project (which sorts first in the candidate scan) still leads round 1.
    buckets: dict = {}
    project_weight: dict = {}
    for tid in eligible_ids:
        pid = project_by_id.get(tid)
        if pid not in buckets:
            buckets[pid] = []
            w = (weight_by_id or {}).get(tid, 1)
            try:
                w = int(w)
            except (TypeError, ValueError):
                w = 1
            project_weight[pid] = max(1, w)
        buckets[pid].append(tid)

    if len(buckets) <= 1:
        return eligible_ids  # single project — nothing to interleave

    # Weighted round-robin: each round, draw up to ``weight`` ids from every
    # non-empty project bucket (in first-appearance order). A project with a
    # higher weight is serviced more per round but never to the exclusion of
    # the others, so wide fan-out from one project no longer monopolizes the
    # dispatch window.
    ordered: list = []
    cursors = dict.fromkeys(buckets, 0)
    remaining = len(eligible_ids)
    while remaining > 0:
        progressed = False
        for pid, ids in buckets.items():
            take = project_weight[pid]
            c = cursors[pid]
            drawn = 0
            while drawn < take and c < len(ids):
                ordered.append(ids[c])
                c += 1
                drawn += 1
                remaining -= 1
                progressed = True
            cursors[pid] = c
        if not progressed:  # safety: all buckets drained
            break
    return ordered
