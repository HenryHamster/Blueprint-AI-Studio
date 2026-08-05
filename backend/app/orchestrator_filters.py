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
from collections import Counter
from datetime import datetime, timedelta
from typing import Callable, Coroutine, Any

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.migration_path import is_migration_file
from app.models import Task, TaskStatus
from app.shared_state import is_direction_held, DEFAULT_TASK_SCOPE
from app.time_utils import _utcnow
from app.task_capabilities import (
    DIRECT_PACKER_GCE_EXECUTION_SITE,
    READ_ONLY_TASK_TYPES,
    execution_site_for_task_type,
)

logger = logging.getLogger("blueprint")

# Number of consecutive dispatch cycles a candidate must stay deferred on the
# SAME precondition blocker before we raise one operator-visible alert. Kept
# low so a genuinely stuck task surfaces quickly, but >1 so a blocker that
# clears within a cycle or two never spams the Alerts banner.
_PRECONDITION_ESCALATE_THRESHOLD = 3

# Once a candidate has been deferred on the same precondition blocker for
# ``_PRECONDITION_ESCALATE_THRESHOLD`` consecutive cycles we stop re-judging it
# on every fast dispatch cycle and instead park it in the first-class BLOCKED
# state, scheduling the periodic unblock sweep to re-evaluate it no sooner than
# ``recheck_after``.  This replaces the old "stay QUEUED forever, re-judge every
# 0.5–5s" churn with one cheap recheck per interval (see
# ``reaper_sweeps.sweep_unblock_blocked_tasks``).
_BLOCKED_RECHECK_INTERVAL = timedelta(minutes=5)


async def _record_defer_and_maybe_escalate(
    candidate: Task,
    session,
    *,
    blocker_kind: str,
    detail: str,
    block_code: str,
    threshold: int = _PRECONDITION_ESCALATE_THRESHOLD,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
    reserved_files: list[str] | None = None,
) -> None:
    """Track repeated deferral of *candidate* on the same precondition blocker.

    Persists a per-blocker defer counter in
    ``candidate.context['_precondition_defer']`` keyed by ``blocker_kind``
    (the blocker identity — overlapping files or the blocking rollup title).
    Once the count reaches *threshold* the candidate is parked in the
    first-class ``BLOCKED`` state (durable ``blocker_kind`` / ``blocked_reason``
    / ``blocked_at`` / ``recheck_after`` metadata) so it stops burning fast
    dispatch cycles — the periodic unblock sweep takes over rechecking it. On
    the cycle that first crosses the threshold (and only then) we also raise ONE
    deduplicated operator alert, stamping ``_precondition_escalated_key`` so it
    does not re-fire every cycle. The counter (and escalation stamp) reset when
    the blocker identity changes, so a task that moves from one blocker to a
    different one starts a fresh persistence count.

    ``block_code`` is the short categorical code persisted as ``Task.blocker_kind``
    (the column is ``VARCHAR(64)``) — e.g. ``"file_conflict"`` or
    ``"pending_rollup"`` — while ``blocker_kind`` is the human-readable blocker
    identity used for the defer-counter key and the alert title.

    We never dispatch work known to be blocked: below the threshold the
    candidate stays QUEUED (deferred this cycle); at/above it the candidate is
    BLOCKED so the normal queue path skips it entirely.
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
        # Drop a stale first-blocked onset stamp only if it belongs to a
        # *different* blocker identity. A same-identity stamp must survive
        # optimistic-requeue episodes: the unblock sweep wipes
        # ``_precondition_defer`` when it requeues a conflict/rollup blocker, but
        # the blocker (and therefore its escalation deadline onset) persists.
        fb = ctx.get("_blocked_first_at") or {}
        if fb.get("key") != blocker_kind:
            ctx.pop("_blocked_first_at", None)
    defer_record = {"key": blocker_kind, "count": count}
    if reserved_files:
        # Persist the full contested-file set (the blocker key is truncated to
        # 120 chars and is lossy) so the seniority pass can reserve those files
        # for this task across cycles. Lifecycle note:
        # reaper_sweeps._clear_blocked_fields preserves _precondition_defer on
        # optimistic (conflict/rollup) requeues so the counter accumulates across
        # episodes and can escalate to FAILED. On a genuine dependency clear, the
        # record is wiped. The reservation holds across the parked-BLOCKED window
        # — _precondition_defer['files'] is intact and the blocked_conflict_tasks
        # query feeds it to _file_conflict_reserved_files.
        defer_record["files"] = sorted(set(reserved_files))
    ctx["_precondition_defer"] = defer_record

    already_escalated = ctx.get("_precondition_escalated_key") == blocker_kind
    should_escalate = count >= threshold and not already_escalated
    if should_escalate:
        ctx["_precondition_escalated_key"] = blocker_kind

    # At/above the threshold, park the task in BLOCKED so it leaves the QUEUED
    # candidate pool. The unblock sweep re-evaluates it at/after recheck_after,
    # moving it back to QUEUED when the precondition clears.
    if count >= threshold:
        now = _utcnow()
        # Preserve the ONSET of this blocker identity across optimistic-requeue
        # episodes so the deadline escalation (reaper_sweeps,
        # ``_BLOCKED_ESCALATE_AFTER``) measures from the TRUE first-blocked time.
        # Conflict/rollup blockers have no dependency edge, so the unblock sweep
        # requeues them every interval and dispatch re-blocks them here. A fresh
        # ``blocked_at`` per episode would mean ``now - blocked_at`` never
        # reaches the deadline, so a permanently-stuck conflict (e.g. a pending
        # rollup that never merges) would loop indefinitely without the ERROR
        # escalation. The sweep only wipes ``_blocked_first_at`` on a genuine
        # dependency clear, so for conflict blockers it survives the requeue.
        first_blocked = ctx.get("_blocked_first_at") or {}
        blocked_onset = None
        if first_blocked.get("key") == blocker_kind and first_blocked.get("at"):
            try:
                blocked_onset = datetime.fromisoformat(first_blocked["at"])
            except (ValueError, TypeError):
                blocked_onset = None
        if blocked_onset is None:
            blocked_onset = now
            ctx["_blocked_first_at"] = {"key": blocker_kind, "at": now.isoformat()}

        candidate.status = TaskStatus.BLOCKED
        candidate.blocker_kind = block_code[:64]
        candidate.blocked_reason = detail
        candidate.blocked_at = blocked_onset
        candidate.recheck_after = now + _BLOCKED_RECHECK_INTERVAL
        candidate.latest_status_message = f"Blocked: {detail}"[:500]

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
            f"[DISPATCH] Parking task {candidate.id} in BLOCKED after {count} "
            f"deferrals on persistent precondition blocker: {blocker_kind}"
        )
        # Durability contract: the CALLER owns the task commit, not the alert
        # helper. Commit the BLOCKED parking FIRST so the transition is durable
        # before we raise the operator alert or broadcast it to the UI. This
        # mirrors the other two caller families (task_lifecycle_requeue.py:272
        # and agent_status.py:1452 — session.add -> commit -> alert -> broadcast)
        # and removes the old implicit "who committed first" dependency on the
        # alert helper's side-effect commit. If this commit RAISES, the
        # transition did not persist; the exception propagates and the BLOCKED
        # broadcast below is never reached, so the UI never sees a task_updated
        # BLOCKED event for a state the database rolled back.
        await session.commit()
        # The alert helper now owns ONLY its own alert insert/commit; its return
        # value is informational. Whether the operator alert is newly created
        # (returns True) or already exists (returns False) no longer decides who
        # committed the task — it is already durable above — so all three cases
        # (new alert, existing alert, failed commit) converge on the single
        # contract: broadcast BLOCKED iff the transition persisted.
        await _maybe_create_precondition_blocker_alert(
            candidate,
            session,
            blocker_kind=blocker_kind,
            detail=detail,
        )
        if broadcast_fn:
            try:
                await broadcast_fn(
                    str(candidate.project_id),
                    "task_updated",
                    {
                        "task_id": str(candidate.id),
                        "status": "BLOCKED",
                        "message": candidate.latest_status_message,
                    },
                )
            except Exception:
                logger.debug(
                    f"Failed to broadcast BLOCKED for {candidate.id}", exc_info=True
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


def _is_read_only_task(task: Task) -> bool:
    """True if *task* runs the agent in read-only mode (review/plan/research/...).

    Read-only types produce a verdict / plan / findings, never a diff — they push
    no branch and open no PR, so they cannot cause a merge conflict and must be
    excluded from BOTH sides of the file-conflict gate: they are never deferred
    as a candidate, and never contribute their related_files to the IN_PROGRESS
    claim set (a review inherits the PR's full file list via propagated_ctx, so
    leaving it in the claim set starved real write work on a phantom conflict —
    prod 2026-06-07, PR #8742).
    """
    return task.first_payload.get("type", "") in READ_ONLY_TASK_TYPES


# A candidate earns *seniority* over its contested files once it has been
# deferred on the SAME file-conflict blocker for this many dispatch cycles.
# Past this point, strictly-newer candidates that would step on those files
# defer BEHIND it — per-resource FIFO fairness that stops a stream of freshly
# spawned same-file tasks from starving a long-waiting task (observed
# 2026-06-06: a fix task sat blocked on models.py for 2h+ while the lock
# rotated through two newer tasks). Set to one below the BLOCKED-escalation
# threshold so a task earns the reservation just before it parks, and the
# reservation then persists through the BLOCKED/requeue cycle.
_FILE_CONFLICT_SENIORITY_THRESHOLD = 2


def _file_conflict_reserved_files(task: Task) -> set[str]:
    """Production files a file-conflict waiter has earned seniority over.

    Read from the persisted defer bookkeeping written by
    ``_record_defer_and_maybe_escalate`` (``_precondition_defer['files']``);
    falls back to parsing the lossy blocker-identity string when the explicit
    list is absent (older persisted state / the onset stamp).
    """
    ctx = task.context or {}
    defer = ctx.get("_precondition_defer") or {}
    files = defer.get("files")
    if isinstance(files, list) and files:
        return _prod_files(files)
    # Fallback: parse "file conflict on a.py, b.py, c.py" from the blocker key
    # (or the preserved onset stamp, which survives the BLOCKED requeue).
    for src in (defer.get("key"), (ctx.get("_blocked_first_at") or {}).get("key")):
        if isinstance(src, str) and src.startswith("file conflict on "):
            raw = src[len("file conflict on ") :]
            return _prod_files([p.strip() for p in raw.split(",") if p.strip()])
    return set()


def _is_senior_file_conflict_waiter(task: Task) -> bool:
    """True if *task* has waited long enough on a file conflict to earn a
    seniority reservation over its contested files.

    Qualifies when the task is parked BLOCKED on a file conflict (already past
    the escalation threshold) OR is still QUEUED but has been deferred on the
    same file-conflict blocker for ``_FILE_CONFLICT_SENIORITY_THRESHOLD``+
    cycles.
    """
    if task.status == TaskStatus.BLOCKED and task.blocker_kind == "file_conflict":
        return True
    defer = (task.context or {}).get("_precondition_defer") or {}
    key = defer.get("key")
    return (
        isinstance(key, str)
        and key.startswith("file conflict on ")
        and int(defer.get("count", 0)) >= _FILE_CONFLICT_SENIORITY_THRESHOLD
    )


def _is_more_senior(holder: Task, candidate: Task) -> bool:
    """True if *holder* outranks *candidate* for contested-resource FIFO order.

    Older ``created_at`` wins; ties (and missing timestamps) break on the
    stringified id so the ordering is total and deterministic — a reservation
    chain must be a strict order or two tasks could mutually defer forever.
    """
    h, c = holder.created_at, candidate.created_at
    if h is not None and c is not None and h != c:
        return h < c
    return str(holder.id) < str(candidate.id)


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


# A file referenced by this many concurrent tasks (candidates + in-flight) in
# the same dispatch cycle is considered a "hotspot" god-file. Cross-parent
# tasks whose ONLY overlap is hotspot files are not held — additive edits to
# such files (e.g. schemas.py, models.py) rebase cleanly between independent
# feature branches and the occasional real conflict surfaces at rollup merge.
HOTSPOT_FILE_THRESHOLD = 3


def file_conflict_defers(
    candidate_files: set,
    blocker_files: set,
    *,
    scope: str,
    cand_parent: "uuid.UUID | None",
    blocker_parents: set,
    hotspot_files: "frozenset | set" = frozenset(),
) -> bool:
    """Decide whether a file conflict should defer a candidate task.

    Shared single source of truth for both the cold dispatch path
    (filter_file_conflicts) and the warm backlog reuse path.

    Cross-parent looseness with hotspot exemption:
    - If the candidate and ALL overlapping blockers are under DIFFERENT
      non-null parents, evaluate against only the NON-hotspot portion of the
      overlap.  A hotspot-only overlap never defers, even at large scope.
    - When parent information is missing or parents are the same, falls back
      to the strict rule (any overlap defers large scope; 2+ files AND >= 50%
      defers standard scope).

    Args:
        candidate_files: production files the candidate task will edit.
        blocker_files: union of production files held by overlapping blockers.
        scope: the candidate task's scope ("large" or anything else).
        cand_parent: the candidate's parent_task_id (None if orphan).
        blocker_parents: set of parent_task_id values for overlapping blockers
            (None entries mean the blocker has no parent — stays conservative).
        hotspot_files: files currently "hot" (referenced by many concurrent
            tasks). Cross-parent overlaps limited to hotspot files skip deferral.

    Returns:
        True if the candidate should be deferred; False if it can proceed.
    """
    overlap = candidate_files & blocker_files
    if not overlap:
        return False
    is_large = scope == "large"
    confidently_cross_parent = (
        cand_parent is not None
        and bool(blocker_parents)
        and all(p is not None and p != cand_parent for p in blocker_parents)
    )
    if confidently_cross_parent:
        # Standard scope: the cross-parent exemption already existed and is preserved
        # (different rollups target different branches; the threshold isn't meaningful
        # for independent features). No change to this path.
        if not is_large:
            return False
        # Large scope: previously always deferred regardless of parent relationship.
        # Now only defer when the overlap includes at least one NON-hotspot file.
        # A hotspot-only overlap (e.g. schemas.py shared by many independent tasks)
        # is treated as safe: additive edits to god-files rebase cleanly.
        non_hotspot_overlap = overlap - hotspot_files
        return bool(non_hotspot_overlap)
    return is_large or (
        len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5
    )


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
    blocked_conflict_tasks: list[Task] | None = None,
) -> list[uuid.UUID]:
    """Defer candidates that would conflict with currently IN_PROGRESS tasks.

    Builds per-project sets of files being modified by IN_PROGRESS tasks, then
    removes from *eligible_ids* any candidate whose related_files overlap
    significantly (large-scope = any overlap; standard = 2+ files AND >= 50%).

    Seniority (contested-resource FIFO): a candidate that has already waited on
    a file conflict long enough to earn a reservation (see
    ``_is_senior_file_conflict_waiter``) reserves its contested files. A
    strictly-NEWER candidate that would step on those files defers behind the
    senior even when no IN_PROGRESS task currently holds them, so a stream of
    freshly-spawned same-file tasks cannot keep re-claiming the lock ahead of a
    long-waiting task. ``blocked_conflict_tasks`` carries the already-parked
    BLOCKED file-conflict tasks (absent from the QUEUED candidate scan) so their
    reservations still hold while they are parked.

    Updates the candidate's latest_status_message and broadcasts a deferral
    event when a task is deferred.  Returns the filtered list of eligible_ids.
    """
    # Build per-project sets of files being modified by IN_PROGRESS tasks.
    # Read-only tasks (review, plan, research, the audits, memo, ...) never push
    # a branch or open a PR — they produce a verdict / plan / findings, not a
    # diff — so they can NEVER cause a merge conflict and must not contribute to
    # the claim set. This mirrors the candidate-side exemption below: a review
    # inheriting a PR's full related_files (review_pipeline propagated_ctx) was
    # otherwise IN_PROGRESS at nearly every recheck and starved the PR's own
    # fix-task on a phantom file conflict (prod 2026-06-07, PR #8742, ~7h hold).
    in_progress_files_by_project: dict[uuid.UUID, set[str]] = {}
    for t in active_tasks:
        if t.status == TaskStatus.IN_PROGRESS and not _is_read_only_task(t):
            files = _prod_files((t.context or {}).get("related_files", []))
            if files:
                in_progress_files_by_project.setdefault(t.project_id, set()).update(
                    files
                )

    # Build per-project seniority reservations from tasks already waiting on a
    # file conflict (parked BLOCKED, or QUEUED past the seniority threshold).
    # Read-only tasks are exempt from the gate (below) so they can never accrue a
    # file-conflict reservation, but filter here too so the set can never name a
    # read-only blocker even if upstream state is inconsistent.
    senior_holders_by_project: dict[uuid.UUID, list[Task]] = {}
    for t in list(candidate_tasks) + list(blocked_conflict_tasks or ()):
        if (
            not _is_read_only_task(t)
            and _is_senior_file_conflict_waiter(t)
            and _file_conflict_reserved_files(t)
        ):
            senior_holders_by_project.setdefault(t.project_id, []).append(t)

    # Nothing in flight and no reservations → no candidate can conflict.
    if not in_progress_files_by_project and not senior_holders_by_project:
        return eligible_ids

    # Compute per-project hotspot files: files referenced by HOTSPOT_FILE_THRESHOLD
    # or more concurrent tasks (candidates + in-flight). Cross-parent tasks whose
    # only overlap is hotspot files are not held — see file_conflict_defers().
    hotspot_files_by_project: dict[uuid.UUID, frozenset] = {}
    file_counts_by_project: dict[uuid.UUID, Counter] = {}
    for t in list(candidate_tasks) + list(active_tasks):
        if _is_read_only_task(t):
            continue
        files = _prod_files((t.context or {}).get("related_files", []))
        for f in files:
            file_counts_by_project.setdefault(t.project_id, Counter())[f] += 1
    for pid, counts in file_counts_by_project.items():
        hotspot_files_by_project[pid] = frozenset(
            f for f, n in counts.items() if n >= HOTSPOT_FILE_THRESHOLD
        )

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
        if _is_read_only_task(candidate):
            non_conflicting.append(eid)
            continue
        candidate_files = _prod_files(
            (candidate.context or {}).get("related_files", [])
        )
        if not candidate_files:
            non_conflicting.append(eid)
            continue
        # Effective blocker file set for THIS candidate (same project only):
        # files held by IN_PROGRESS work, PLUS files reserved by a MORE-SENIOR
        # file-conflict waiter (older created_at). The seniority reservation is
        # what gives a long-deferred task contested-resource FIFO priority over
        # freshly-spawned same-file tasks. A holder never reserves against
        # itself, and only strictly-older holders outrank the candidate, so the
        # reservation chain is a strict order (no mutual deferral).
        ip_files = in_progress_files_by_project.get(candidate.project_id, set())
        senior_blockers = [
            h
            for h in senior_holders_by_project.get(candidate.project_id, [])
            if h.id != candidate.id and _is_more_senior(h, candidate)
        ]
        reserved_files: set[str] = set()
        for h in senior_blockers:
            reserved_files |= _file_conflict_reserved_files(h)
        project_files = ip_files | reserved_files
        if not project_files:
            non_conflicting.append(eid)
            continue
        overlap = candidate_files & project_files
        if not overlap:
            non_conflicting.append(eid)
            continue
        # Compute overlapping blockers for attribution and cross-parent evaluation.
        # Read-only tasks are excluded from in_progress_files_by_project above so
        # they can never be in overlapping_blockers — but filter here too so
        # blocker attribution ("blocked by: <title>") can never name a read-only
        # task even if the two scans ever diverge.
        overlapping_blockers = [
            t
            for t in active_tasks
            if t.status == TaskStatus.IN_PROGRESS
            and t.project_id == candidate.project_id
            and not _is_read_only_task(t)
            and _prod_files((t.context or {}).get("related_files", [])) & overlap
        ]
        # Senior reservation holders that overlap also count as blockers so
        # the cross-parent looseness evaluates the candidate's parent relationship
        # against them too (a senior in a different rollup gets the SAME
        # cross-parent treatment as an in-flight blocker).
        overlapping_blockers += [
            h for h in senior_blockers if _file_conflict_reserved_files(h) & overlap
        ]
        scope = (candidate.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        blocker_parents = {t.parent_task_id for t in overlapping_blockers}
        hotspot_files = hotspot_files_by_project.get(candidate.project_id, frozenset())
        if not file_conflict_defers(
            candidate_files,
            project_files,
            scope=scope,
            cand_parent=candidate.parent_task_id,
            blocker_parents=blocker_parents,
            hotspot_files=hotspot_files,
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
                logger.debug(f"Failed to broadcast deferral for {eid}", exc_info=True)
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
            block_code="file_conflict",
            broadcast_fn=broadcast_fn,
            reserved_files=sorted(overlap),
        )
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
                block_code="pending_rollup",
                broadcast_fn=broadcast_fn,
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


async def reserve_migration_revisions(
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    session,
) -> list[uuid.UUID]:
    """Reserve a provisional revision id for each migration-producing candidate.

    Replaces the old project-wide IN_PROGRESS serialization. Previously two
    concurrent migrate tasks (or create tasks that auto-generate alembic
    revisions) would read the same latest revision and each generate the next
    one, producing duplicate revisions on the same down_revision — so dispatch
    held every sibling schema task behind the holder's ENTIRE agent run
    (authoring + the internal test-fix loop + full Docker test gates), which can
    last hours.

    Instead, allocate (or idempotently reuse) a persisted
    :class:`app.models.MigrationReservation` for every dispatchable
    migration-producing candidate the moment it is selected. The allocator hands
    out distinct monotonic ids under a per-project lock, so once a candidate's
    revision number is locked, concurrent siblings can each author a migration in
    parallel and be linearized at merge time. The slot therefore RELEASES
    immediately after the number is reserved. The reserved
    revision/down_revision rides the agent claim response additively and is
    consumed verbatim.

    Allocation is idempotent: an existing ACTIVE reservation is returned as-is
    (re-running every cycle never burns a fresh id). Unknown remote-head failures
    fail closed because local numbering may be stale; unrelated allocator errors
    retain the legacy best-effort fallback.
    """
    if not eligible_ids:
        return eligible_ids

    # Deferred import keeps app.migration_reservations (which pulls in
    # migration_chain + glob) off the orchestrator import path at module load
    # and mirrors the leaf-import discipline used elsewhere in this module.
    from app.migration_reservations import (  # noqa: PLC0415
        MigrationHeadUnavailableError,
        allocate_migration_reservation,
    )

    reservable_ids = list(eligible_ids)
    for eid in eligible_ids:
        candidate = next((t for t in candidate_tasks if t.id == eid), None)
        if candidate is None or not _is_migration_task(candidate):
            continue
        try:
            await allocate_migration_reservation(session, candidate)
        except MigrationHeadUnavailableError:
            reservable_ids.remove(eid)
            logger.warning(
                "[DISPATCH] Deferring migration task %s because its task-base "
                "migration head cannot be determined safely",
                eid,
                exc_info=True,
            )
        except Exception:
            # Best-effort: a transient allocation failure must not hold a
            # migration task out of dispatch. The agent derives max+1 locally
            # when the claim carries no reservation, so correctness degrades
            # gracefully back to the pre-reservation behavior for this task.
            logger.warning(
                "[DISPATCH] Failed to reserve a migration revision for task %s; "
                "agent will fall back to local numbering",
                eid,
                exc_info=True,
            )
    return reservable_ids


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
        # Commit the terminal CANCELLED transition BEFORE broadcasting so the
        # websocket event never announces a status the API has not yet
        # persisted (stale-UI defect) and is never emitted for a change that a
        # later rollback discards (lost-transition defect). Same commit-then-
        # broadcast discipline as PR #7721 (unblock sweep). expire_on_commit is
        # False (database.py), so committing here does NOT expire candidate or
        # the other in-memory candidate_tasks the downstream dispatch steps
        # still read. Snapshot the broadcast literals before committing.
        _project_id = str(candidate.project_id)
        _task_id = str(candidate.id)
        await session.commit()
        try:
            await broadcast_fn(
                _project_id,
                "task_updated",
                {"task_id": _task_id, "status": "CANCELLED", "message": msg},
            )
        except Exception:
            logger.debug(
                f"Failed to broadcast SCOPE_EMPTY for {_task_id}", exc_info=True
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
                ADMIN_CANCEL,
                persist_failure_category,
            )  # noqa: PLC0415

            candidate.status = TaskStatus.CANCELLED
            candidate.latest_status_message = (
                f"Deduplicated: {fp.get('type')} task {str(running_other.id)[:8]} "
                f"is already {running_other.status.value if hasattr(running_other.status, 'value') else running_other.status} "
                f"on PR #{fp.get('pr_number')}"
            )
            persist_failure_category(candidate, ADMIN_CANCEL)
            session.add(candidate)
            # If the dedup loser already opened a PR (race: candidate had run
            # past the spawn-time dedup window before this filter caught it),
            # close that PR so it doesn't hang the parent rollup gate.
            # Regression for c56fa118 / PR #5983 (2026-05-26).
            from app.task_lifecycle import close_orphan_pr_if_any

            await close_orphan_pr_if_any(
                candidate, session, reason="dispatch_dedup_running_sibling"
            )
            # Commit the terminal CANCELLED transition BEFORE broadcasting so
            # the event is durable and never rolled back out from under it
            # (see filter_scope_empty for the full rationale). Snapshot the
            # broadcast literals before the commit.
            _project_id = str(candidate.project_id)
            _task_id = str(eid)
            _dedup_msg = candidate.latest_status_message
            await session.commit()
            try:
                await broadcast_fn(
                    _project_id,
                    "task_updated",
                    {
                        "task_id": _task_id,
                        "status": "CANCELLED",
                        "message": _dedup_msg,
                    },
                )
            except Exception:
                logger.debug(
                    f"Failed to broadcast dedup cancel for {_task_id}", exc_info=True
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
                    ADMIN_CANCEL,
                    persist_failure_category,
                )  # noqa: PLC0415

                candidate.status = TaskStatus.CANCELLED
                candidate.latest_status_message = (
                    f"Deduplicated: older QUEUED {fp.get('type')} task "
                    f"{str(oldest_queued.id)[:8]} already exists on PR #{fp.get('pr_number')}"
                )
                persist_failure_category(candidate, ADMIN_CANCEL)
                session.add(candidate)
                # See comment above re: c56fa118 / PR #5983.
                from app.task_lifecycle import close_orphan_pr_if_any

                await close_orphan_pr_if_any(
                    candidate, session, reason="dispatch_dedup_queued_sibling"
                )
                # Commit the terminal CANCELLED transition BEFORE broadcasting
                # so the event is durable and never rolled back out from under
                # it (see filter_scope_empty for the full rationale). Snapshot
                # the broadcast literals before the commit.
                _project_id = str(candidate.project_id)
                _task_id = str(eid)
                _dedup_msg = candidate.latest_status_message
                await session.commit()
                try:
                    await broadcast_fn(
                        _project_id,
                        "task_updated",
                        {
                            "task_id": _task_id,
                            "status": "CANCELLED",
                            "message": _dedup_msg,
                        },
                    )
                except Exception:
                    logger.debug(
                        f"Failed to broadcast dedup cancel for {_task_id}",
                        exc_info=True,
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
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
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
        runtime_config.get("reserved_slots_for_types", {"create": 1, "research": 1})
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


# Task types that are part of the review/fix pipeline (tree-finishing candidates).
_PIPELINE_TASK_TYPES = frozenset({"review", "fix"})
# Task types that represent brand-new top-level work (lowest completion priority).
_NEW_WORK_TASK_TYPES = frozenset({"research", "web_research"})


def apply_completion_priority(
    eligible_ids: list,
    types_by_id: dict,
    parent_id_by_id: dict,
    active_task_ids: set,
    rollup_parent_ids: set,
) -> list:
    """Reorder eligible tasks so tree-completing work outranks new work.

    Assigns completion tiers and stably sorts so that tasks closer to finishing
    an already-started tree appear before brand-new top-level work:

      1. review/fix tasks whose parent plan already issued a rollup PR
         (``parent_id`` in ``rollup_parent_ids``) — these close whole plan trees.
      2. review/fix tasks on ordinary in-flight PRs (parent not a rollup).
      3. Children of already-running plans (``parent_id`` in ``active_task_ids``).
      4. Brand-new top-level work: research, web_research, and unparented tasks.

    This is a pure permutation — no tasks are added or removed. The existing
    order from per-project fairness is preserved within each tier (stable sort).
    The downstream ``reserve_slots_for_types`` pass still provides the
    anti-starvation floor so new research/create tasks are never fully starved.

    Args:
        eligible_ids: candidate ids in current dispatch order.
        types_by_id: mapping ``id -> task type string``.
        parent_id_by_id: mapping ``id -> parent_task_id`` (None if top-level).
        active_task_ids: set of task IDs currently PROVISIONING/IN_PROGRESS.
        rollup_parent_ids: set of plan-task IDs whose context contains
            ``rollup_pr_number`` (indicating the tree has rolled up).

    Returns:
        The reordered ``eligible_ids`` (same length / membership, stable within tier).
    """
    if len(eligible_ids) <= 1:
        return eligible_ids

    def _tier(tid) -> int:
        task_type = types_by_id.get(tid) or ""
        parent_id = parent_id_by_id.get(tid)
        if task_type in _PIPELINE_TASK_TYPES:
            if parent_id and parent_id in rollup_parent_ids:
                return 1  # rollup tree-completing review/fix — highest priority
            return 2  # ordinary review/fix on in-flight PR
        if parent_id and parent_id in active_task_ids:
            return 3  # child of already-running plan
        return 4  # new research / new top-level work — lowest priority

    tiers = [(_tier(tid), i, tid) for i, tid in enumerate(eligible_ids)]
    tiers.sort(key=lambda x: (x[0], x[1]))
    result = [x[2] for x in tiers]

    # Log when the reorder actually changes the order (scarcity signal).
    if result != eligible_ids:
        tier_counts: dict[int, int] = {}
        for tier, _, _ in tiers:
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        logger.info(
            "[ORCHESTRATOR] Finish-before-start: tier distribution "
            f"{tier_counts} (1=rollup-review, 2=review/fix, 3=plan-child, 4=new-work)"
        )
    return result


def apply_project_concurrency_cap(
    eligible_ids: list,
    project_by_id: dict,
    active_by_project_id: dict,
    cap_by_project_id: dict,
) -> list:
    """Defer candidates from projects that are at-or-over their per-project cap.

    Pure single source of truth for the per-project concurrency filter (Step 2.92,
    applied AFTER cap_pipeline_slots and BEFORE apply_project_fairness). Projects
    with no configured cap are NEVER filtered — behavior is byte-identical for them.

    Semantics are skip-not-block: filtered candidates stay QUEUED and dispatch
    normally on a later cycle once the project's active count drops. This filter
    never fails a task and never blocks another project's candidates.

    The admitted-this-cycle counter tracks how many candidates from the same
    project have already been admitted in this pass, so two candidates from the
    same capped project cannot both slip past the cap in a single dispatch cycle.

    Args:
        eligible_ids: candidate ids in current dispatch order.
        project_by_id: mapping ``id -> project_id``.
        active_by_project_id: mapping ``project_id -> int`` count of currently
            PROVISIONING/IN_PROGRESS tasks (computed from the already-fetched
            active set — no extra DB query).
        cap_by_project_id: mapping ``project_id -> int`` cap; absent or 0
            means no cap for that project.

    Returns:
        The filtered ``eligible_ids`` with over-cap projects' candidates removed.
    """
    if not cap_by_project_id or not eligible_ids:
        return eligible_ids

    admitted_this_cycle: dict = {}
    kept: list = []
    for tid in eligible_ids:
        pid = project_by_id.get(tid)
        cap = cap_by_project_id.get(pid) if pid is not None else None
        if not cap or cap <= 0:
            kept.append(tid)
            continue
        active = int(active_by_project_id.get(pid, 0))
        already_admitted = int(admitted_this_cycle.get(pid, 0))
        if active + already_admitted >= cap:
            continue
        admitted_this_cycle[pid] = already_admitted + 1
        kept.append(tid)
    return kept


async def reorder_by_completion_priority(
    eligible_ids: list,
    candidate_tasks: list[Task],
    active_task_ids: set,
    session,
) -> list:
    """Step 2.97: reorder eligible tasks so tree-completing work wins scarce slots.

    Fetches parent-task contexts for review/fix candidates in a single batched
    query to identify rollup parents, then delegates to the pure
    ``apply_completion_priority`` for the stable-sort reorder.  Only issues a
    DB query when review/fix candidates with a parent_task_id are present.
    """
    if len(eligible_ids) <= 1:
        return eligible_ids

    types_by_id = {
        t.id: ((t.first_payload or {}).get("type") or "") for t in candidate_tasks
    }
    parent_id_by_id = {t.id: t.parent_task_id for t in candidate_tasks}

    # Collect parent IDs for review/fix candidates — only these need rollup check.
    parent_ids_to_fetch = set()
    for tid in eligible_ids:
        task_type = types_by_id.get(tid) or ""
        parent_id = parent_id_by_id.get(tid)
        if task_type in _PIPELINE_TASK_TYPES and parent_id:
            parent_ids_to_fetch.add(parent_id)

    # Query parent contexts in one batch to find rollup parents.
    rollup_parent_ids: set = set()
    if parent_ids_to_fetch:
        rows = (
            await session.execute(
                select(Task.id, Task.context).where(Task.id.in_(parent_ids_to_fetch))
            )
        ).all()
        for row_id, row_context in rows:
            if row_context and row_context.get("rollup_pr_number"):
                rollup_parent_ids.add(row_id)

    return apply_completion_priority(
        eligible_ids,
        types_by_id,
        parent_id_by_id,
        active_task_ids,
        rollup_parent_ids,
    )


# ── Dispatch-window sizing + failure-gate withholding ──────────────────────────
# Both transforms exist for one reason: a dispatch-window position must only ever
# be spent on a task that can ACTUALLY dispatch this cycle. The window used to be
# cut at exactly ``slots_available``, but several outcomes inside the dispatch loop
# consume a position and dispatch nothing (a failure-gated type, a compute_experiment
# held for a runner, a recoverable BLOCKED launch, a transient cap deferral, a
# per-project resource deferral, a lost lock race). Those slots were never
# backfilled, so healthy tasks ranked just below the cutoff starved for hours while
# the fleet sat idle.

#: Scan this many times ``slots_available`` (with the additive floor below) so
#: non-dispatchable candidates are backfilled rather than each burning a slot.
#: Bounded so a queue whose head is entirely non-dispatchable cannot turn a single
#: cycle into an unbounded scan.
DISPATCH_WINDOW_BACKFILL_MULTIPLIER = 3
DISPATCH_WINDOW_BACKFILL_MIN_EXTRA = 20


def dispatch_window_size(slots_available: int) -> int:
    """How many candidates to SCAN for a cycle offering ``slots_available`` slots.

    Strictly >= ``slots_available``, so widening never drops a task that the old
    ``eligible_ids[:slots_available]`` cut would have included — it only appends
    backfill candidates behind them, preserving every upstream ordering/reservation
    permutation. The dispatch loop still enforces the real limit by stopping once
    ``slots_available`` ACTUAL dispatches have occurred.
    """
    if slots_available <= 0:
        return 0
    return max(
        slots_available * DISPATCH_WINDOW_BACKFILL_MULTIPLIER,
        slots_available + DISPATCH_WINDOW_BACKFILL_MIN_EXTRA,
    )


def filter_failing_types(
    eligible_ids: list,
    candidate_tasks: list[Task],
    failing_types: set,
) -> tuple[list, set]:
    """Withhold candidates whose task type is currently failure-gated.

    The per-type failure gate is enforced inside the dispatch loop, but that loop
    runs AFTER the window has been cut — so a failure-gated task ranked above the
    cutoff consumed a slot and then skipped, dispatching nothing. With a failing type
    at the head of the queue that silently zeroed out an entire cycle's dispatch while
    capacity sat idle. These tasks provably cannot dispatch this cycle (``failing_types``
    is computed once per cycle and the loop does not mutate it), so withholding them
    here is behaviour-preserving for every task that *can* dispatch and purely frees
    the slots they were wasting.

    Filtering is a subsequence operation: the relative order of the survivors — and
    therefore every fairness / completion-priority / slot-reservation permutation
    applied downstream — is untouched.

    Returns ``(kept_ids, withheld_types)``. ``withheld_types`` feeds the
    all-types-blocked stall detector, which must still see the types it just
    withheld or it would never fire again.
    """
    if not failing_types or not eligible_ids:
        return eligible_ids, set()
    types_by_id = {
        t.id: ((t.first_payload or {}).get("type") or "") for t in candidate_tasks
    }
    withheld_ids = {
        task_id
        for task_id in eligible_ids
        if types_by_id.get(task_id, "") in failing_types
    }
    if not withheld_ids:
        return eligible_ids, set()
    withheld_types = {types_by_id.get(task_id, "") for task_id in withheld_ids}
    kept = [task_id for task_id in eligible_ids if task_id not in withheld_ids]
    return kept, withheld_types


def unplaceable_execution_site_reason(task_type: str) -> str | None:
    """Return a legible terminal-failure reason when a QUEUED task of this type
    can never be placed by any dispatch path, or None when the site is
    generically placeable.

    Generic dispatch routes only the ``default`` site (and unknown types, which
    conservatively resolve to the default matrix).  The direct-Packer-GCE site is
    also placeable by the generic cold-GCE path.  Every OTHER declared site
    (today only the qualifier's live-local-runner ``walkthrough_e2e`` site) is a
    hard routing contract generic dispatch cannot honour: such a task is pinned
    at birth by its site owner (the qualifier resolves a site-proven runner
    BEFORE creating the child), so any QUEUED instance of it is a stranded
    requeue that no path will ever re-place.  Returning a reason lets the
    dispatch loop fail it closed instead of holding it QUEUED (warning every
    cycle) forever.
    """
    site = execution_site_for_task_type(task_type)
    if (
        site is None
        or site == "default"
        or site == DIRECT_PACKER_GCE_EXECUTION_SITE
    ):
        return None
    return (
        f"declared execution site {site!r} for task type {task_type!r} has no "
        "eligible runner: generic dispatch serves only the 'default' and "
        "'direct_packer_gce' sites, and a QUEUED task cannot be re-pinned to a "
        "site-proven runner. Terminating instead of holding QUEUED forever"
    )


async def fail_unplaceable_non_generic_site_task(
    session,
    task: Task,
    *,
    task_type: str,
    broadcast_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> bool:
    """Terminalize a QUEUED task whose registry execution site no dispatch path
    can place, carrying a readable reason that names the execution site.

    The task is stamped UNPLACEABLE_EXECUTION_SITE — a TERMINAL, non-resurrectable
    category (deliberately outside INFRA_CLASSES, so the reaper's Step 5
    resurrection sweep never flips it back to QUEUED, where generic dispatch
    would fail it closed again every cycle). The task never provisioned, so
    PROVISION_FAILURE (5-min infra backoff + resurrection eligibility) would be
    a lie about a permanent routing-contract violation.

    Returns True when the task was marked FAILED; False when the site is
    actually generically placeable (no reason to fail) or the terminal commit
    failed and was rolled back (the task stays QUEUED and retries next cycle).
    """
    reason = unplaceable_execution_site_reason(task_type)
    if reason is None:
        return False
    task.status = TaskStatus.FAILED
    if getattr(task, "vm_provisioned_at", None) is not None:
        task.vm_stopped_at = _utcnow()
    task.latest_status_message = reason
    from app.failure_classification import (
        UNPLACEABLE_EXECUTION_SITE,
        persist_failure_category,
    )

    persist_failure_category(task, UNPLACEABLE_EXECUTION_SITE)
    session.add(task)
    try:
        await session.commit()
    except Exception:
        logger.exception(
            "[DISPATCH] failed to commit terminal FAILED for unplaceable "
            "non-generic-site task %s",
            task.id,
        )
        await session.rollback()
        return False
    if broadcast_fn is not None:
        try:
            await broadcast_fn(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": "FAILED",
                    "message": reason,
                },
            )
        except Exception:
            logger.debug(
                "[DISPATCH] task_updated broadcast failed for unplaceable "
                "non-generic-site task %s",
                task.id,
                exc_info=True,
            )
    return True
