"""Health / dependency classification helpers extracted from ``app.orchestrator``.

This sibling module hosts the pure-ish helpers that classify candidate
tasks for the dispatch loop — cycle detection, dead-dep / cycle
diagnosis, the small dispatch-priority boost for health-aware
reordering, and the dependency-satisfaction check.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import _find_cycle_members``, etc.) working.
"""

from __future__ import annotations

import uuid
from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession

import app.shared_state as _shared_state
from app.models import Task, TaskStatus


# ---------------------------------------------------------------------------
# Cycle detection for deadlock resolution
# ---------------------------------------------------------------------------


def _find_cycle_members(adj: Dict[str, list]) -> set:
    """Return node IDs participating in cycles (DFS white/gray/black coloring)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)
    members: set = set()

    def dfs(n, stack):
        color[n] = GRAY
        stack.append(n)
        for d in adj.get(n, []):
            if color.get(d) == GRAY:
                members.update(stack[stack.index(d) :])
            elif color.get(d) == WHITE:
                dfs(d, stack)
        stack.pop()
        color[n] = BLACK

    for n in list(adj):
        if color[n] == WHITE:
            dfs(n, [])
    return members


def _classify_stuck_tasks(stuck, dep_map, dep_status, stuck_ids) -> list:
    """Return [(task_id, reason)] for tasks whose depends_on should be cleared.

    reason is 'cycle' (true dependency cycle) or 'dead_deps' (all deps are
    FAILED/CANCELLED/deleted with none stuck or active).
    """
    adj = {str(t.id): [str(d) for d in dep_map[t.id] if d in stuck_ids] for t in stuck}
    cycle_m = _find_cycle_members(adj)
    out = []
    for t in stuck:
        deps = dep_map[t.id]
        if not deps:
            continue
        if str(t.id) in cycle_m:
            out.append((t.id, "cycle"))
            continue
        # Dead-dep: no dep is stuck (resolvable) or active
        if not any(
            d in stuck_ids
            or dep_status.get(d) not in (None, TaskStatus.FAILED, TaskStatus.CANCELLED)
            for d in deps
        ):
            out.append((t.id, "dead_deps"))
    return out


def _health_score_task(task: Task) -> int:
    """Return a dispatch-priority boost for health-aware reordering during degradation.

    Small scope and low risk are preferred because they are less likely to consume
    a scarce VM slot and fail.  Higher score = dispatched first.

    Scoring:
        +2  scope == "small"
        +1  risk  == "low"
    """
    ctx = task.context or {}
    score = 0
    if ctx.get("scope") == "small":
        score += 2
    if ctx.get("risk") == "low":
        score += 1
    return score


_CRITICAL_EXTERNAL_DEPS = ("github_api", "dispatch_api", "gcp")


def get_confirmed_down_critical_deps() -> list:
    """Return critical dependency names confirmed down for 2+ consecutive health checks.

    DB is excluded — if DB is down the orchestrator itself cannot function, so
    there's no meaningful gate to apply.  A dep needs >=2 consecutive failures to
    avoid false-positives from transient probe errors.
    """
    consec = _shared_state.external_health.get("consecutive_failures", {})
    return [d for d in _CRITICAL_EXTERNAL_DEPS if consec.get(d, 0) >= 2]


async def _check_candidate_task_deps(
    task: Task,
    dep_tasks: list,
    dep_uuids: list,
    session: AsyncSession,
    verify_pr_merged,
) -> tuple[str, dict]:
    """Check whether a candidate task's dependencies are satisfied.

    Handles requeue-cancelled dependencies: if a dep task is CANCELLED but has
    ``_replaced_by_task_id`` in its context (set by ``_requeue_parent_task``),
    the replacement clone's status is used instead of treating the dependency as
    permanently failed.

    Returns:
        (result, deps_remapped) where:
        - result is ``'cascade_cancel'``, ``'eligible'``, or ``'waiting'``
        - deps_remapped maps old cancelled dep IDs to replacement IDs (for
          persisting remap so future cycles skip the lookup)
    """
    all_met = True
    any_failed = len(dep_tasks) < len(dep_uuids)  # Missing deps treated as failed
    deps_remapped: dict[str, str] = {}

    # A SCOPE_EMPTY dependency is a deliberate "scope already covered" exit,
    # not a defect — the branch is intact and dependent clusters (different
    # files) must still run. Treat it as a satisfied, non-blocking dep
    # wherever it surfaces, whether it landed CANCELLED (current) or FAILED
    # (legacy rows pre-dating the CANCELLED relabel).
    from app.failure_classification import SCOPE_EMPTY

    for dt in dep_tasks:
        effective_dt = dt
        if dt.status == TaskStatus.CANCELLED:
            if (dt.context or {}).get("failure_category") == SCOPE_EMPTY:
                continue  # clean no-op exit — satisfied, do not cascade-cancel
            replacement_id_str = (dt.context or {}).get("_replaced_by_task_id")
            if replacement_id_str:
                try:
                    replacement = await session.get(Task, uuid.UUID(replacement_id_str))
                except (ValueError, AttributeError):
                    replacement = None
                if replacement:
                    deps_remapped[str(dt.id)] = str(replacement.id)
                    effective_dt = replacement
                else:
                    # Replacement task is gone — treat as permanently cancelled
                    any_failed = True
                    break
            else:
                # Permanently cancelled (no replacement recorded)
                any_failed = True
                break

        if effective_dt.status == TaskStatus.FAILED:
            if (effective_dt.context or {}).get("failure_category") == SCOPE_EMPTY:
                continue  # legacy FAILED+SCOPE_EMPTY rows — satisfied
            any_failed = True
            break
        if effective_dt.status == TaskStatus.CANCELLED:
            if (effective_dt.context or {}).get("failure_category") == SCOPE_EMPTY:
                continue
            any_failed = True
            break
        if effective_dt.status != TaskStatus.COMPLETED:
            all_met = False
            continue
        # For create tasks, also require PR to be merged
        dt_type = (effective_dt.first_payload).get("type", "create")
        if dt_type in ("create", "") and not await verify_pr_merged(
            effective_dt, session
        ):
            all_met = False

    if any_failed:
        return "cascade_cancel", deps_remapped
    elif all_met:
        return "eligible", deps_remapped
    else:
        return "waiting", deps_remapped
