"""Health / dependency classification helpers extracted from ``app.orchestrator``.

This sibling module hosts the pure-ish helpers that classify candidate
tasks for the dispatch loop — cycle detection, dead-dep / cycle
diagnosis, the small dispatch-priority boost for health-aware
reordering, and the dependency-satisfaction check.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import _find_cycle_members``, etc.) working.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus

logger = logging.getLogger("blueprint")


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


async def _classify_stuck_tasks(
    stuck, dep_map, dep_tasks_by_id, stuck_ids, session, verify_pr_merged
) -> list:
    """Return [(task_id, reason)] for stuck QUEUED tasks needing intervention.

    ``reason`` is one of:
      - ``'cycle'``          — a true dependency cycle; clear depends_on and dispatch.
      - ``'cascade_cancel'`` — a real prerequisite genuinely FAILED (or was deleted /
                               permanently cancelled). The dependent is impossible
                               work and must be cancelled, NOT dispatched.
      - ``'dead_deps'``      — every dependency is terminal and satisfied/non-blocking
                               (e.g. SCOPE_EMPTY or a merged completion). Safe to clear
                               depends_on and dispatch.

    Dependency-death semantics are delegated to :func:`_check_candidate_task_deps`
    so this deadlock-breaker and the main dispatch loop share ONE source of truth:
    SCOPE_EMPTY-cancelled deps count as satisfied, requeue-cancelled deps remap to
    their live replacement clone (and the dependent keeps waiting), and a genuinely
    failed dep cascade-cancels rather than being cleared-and-dispatched.

    ``dep_tasks_by_id`` maps dependency UUID -> the fully-loaded ``Task`` row (every
    column present, so the attribute reads inside ``_check_candidate_task_deps``
    cannot trip an async lazy-load). A dependency absent from the map is a deleted
    row — ``_check_candidate_task_deps`` treats it as failed.
    """
    adj = {str(t.id): [str(d) for d in dep_map[t.id] if d in stuck_ids] for t in stuck}
    cycle_m = _find_cycle_members(adj)
    _terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    out = []
    for t in stuck:
        deps = dep_map[t.id]
        if not deps:
            continue
        if str(t.id) in cycle_m:
            out.append((t.id, "cycle"))
            continue
        # Still waiting on a resolvable (stuck) or in-flight (non-terminal)
        # dependency — not a deadlock. Leave it for a later cycle, exactly as the
        # main path waits rather than forcing the issue.
        if any(
            d in stuck_ids
            or (d in dep_tasks_by_id and dep_tasks_by_id[d].status not in _terminal)
            for d in deps
        ):
            continue
        # Every dependency is terminal (or deleted). Resolve it through the main
        # path's policy so the death-reason actually drives the outcome.
        dep_tasks = [dep_tasks_by_id[d] for d in deps if d in dep_tasks_by_id]
        result, _ = await _check_candidate_task_deps(
            t, dep_tasks, list(deps), session, verify_pr_merged
        )
        if result == "cascade_cancel":
            out.append((t.id, "cascade_cancel"))
        elif result == "eligible":
            out.append((t.id, "dead_deps"))
        # result == "waiting" (e.g. a requeue clone still in flight) → keep waiting.
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


async def reorder_eligible_by_health(
    eligible_ids: list,
    candidate_tasks: list,
    recent_results: list,
    slots_available: int,
    runtime_config: dict,
    zone_summary: dict,
    session: AsyncSession,
    predict_fn: Callable[..., float],
) -> list:
    """Health-aware reordering of the eligible-task list under degradation.

    Step 2.8 of the dispatch loop. Only activates under degradation so normal
    priority ordering is preserved. Degradation signals: any zone in cooldown,
    failure rate > 30% over the last 10 results, or available slots < 50% of the
    max VM cap.

    Under degradation the list is sorted by predicted P(success)
    (``predict_fn``, injected — normally ``spawn_calibration.predict_spawn_success``)
    with ``_health_score_task`` as the tiebreak, and a configurable confidence
    floor (``runtime_config['dispatch_confidence_floor']``) drops tasks whose
    P(success) is below the floor so scarce slots go to higher-confidence work.
    Skipped tasks remain QUEUED and become eligible again next cycle.

    Returns the (possibly reordered / filtered) ``eligible_ids`` list. A no-op
    when there are <= 1 eligible tasks or no degradation signal is present.
    """
    if len(eligible_ids) <= 1:
        return eligible_ids

    _degradation_signals = []

    if zone_summary["available"] < zone_summary["total_zones"]:
        _degradation_signals.append(
            f"zones {zone_summary['available']}/{zone_summary['total_zones']}"
        )

    if recent_results:
        _hw_window = recent_results[-10:]
        _hw_fail_count = sum(1 for r in _hw_window if r[1] == "failure")
        _hw_fail_rate = _hw_fail_count / len(_hw_window)
        if _hw_fail_rate > 0.30:
            _degradation_signals.append(
                f"failure rate {_hw_fail_count}/{len(_hw_window)}"
            )

    _max_vms = runtime_config["max_concurrent_vms"]
    if slots_available < _max_vms * 0.5:
        _degradation_signals.append(f"slots {slots_available}/{_max_vms}")

    if not _degradation_signals:
        return eligible_ids

    _task_by_id = {t.id: t for t in candidate_tasks}
    _original_order = list(eligible_ids)

    # Load projects for candidate tasks (once per cycle) so we can compute real
    # P(success) via predict_fn instead of relying on the static small/low-risk
    # bonus alone. Cache the score per (project_id, direction_id, scope) since
    # many tasks may share the same direction within a dispatch cycle.
    _project_ids = {
        _task_by_id[tid].project_id
        for tid in eligible_ids
        if tid in _task_by_id and _task_by_id[tid].project_id is not None
    }
    _projects_by_id: dict = {}
    if _project_ids:
        _proj_rows = (
            (
                await session.execute(
                    select(Project).where(Project.id.in_(_project_ids))
                )
            )
            .scalars()
            .all()
        )
        _projects_by_id = {p.id: p for p in _proj_rows}
    _confidence_cache: dict = {}

    def _confidence_score(tid) -> float:
        t = _task_by_id.get(tid)
        if t is None:
            return 0.5
        ctx = t.context or {}
        scope = ctx.get("scope") or "medium"
        dir_id = str(ctx.get("direction_id") or "")
        cache_key = (t.project_id, dir_id, scope)
        if cache_key in _confidence_cache:
            return _confidence_cache[cache_key]
        proj = _projects_by_id.get(t.project_id)
        direction: dict = {}
        if proj and dir_id:
            for d in proj.directions or []:
                if str(d.get("id") or "") == dir_id:
                    direction = d
                    break
        try:
            score = predict_fn(scope, direction, proj)
        except Exception:
            score = 0.5
        _confidence_cache[cache_key] = score
        return score

    eligible_ids = sorted(
        eligible_ids,
        key=lambda tid: (
            _confidence_score(tid),
            _health_score_task(_task_by_id[tid]) if tid in _task_by_id else 0,
        ),
        reverse=True,
    )
    if eligible_ids != _original_order:
        logger.info(
            "[ORCHESTRATOR] Health-aware reordering applied (%s): "
            "%d eligible tasks reordered by P(success) confidence",
            ", ".join(_degradation_signals),
            len(eligible_ids),
        )

    # Confidence floor: during degraded operation, skip tasks whose P(success)
    # is below the configured floor so scarce slots go to higher-confidence
    # work. Skipped tasks remain QUEUED and become eligible again next cycle.
    _conf_floor = float(runtime_config.get("dispatch_confidence_floor", 0.10) or 0.0)
    if _conf_floor > 0:
        _before_floor = list(eligible_ids)
        eligible_ids = [
            tid for tid in eligible_ids if _confidence_score(tid) >= _conf_floor
        ]
        _skipped = len(_before_floor) - len(eligible_ids)
        if _skipped > 0:
            logger.info(
                "[ORCHESTRATOR] Confidence floor (%.2f) skipped "
                "%d task(s) during degraded operation (%s)",
                _conf_floor,
                _skipped,
                ", ".join(_degradation_signals),
            )

    return eligible_ids


_CRITICAL_EXTERNAL_DEPS = ("github_api", "dispatch_api", "gcp")


def get_confirmed_down_critical_deps() -> list:
    """Return critical dependency names confirmed down for 2+ consecutive health checks.

    DB is excluded — if DB is down the orchestrator itself cannot function, so
    there's no meaningful gate to apply.  A dep needs >=2 consecutive failures to
    avoid false-positives from transient probe errors.
    """
    consec = _shared_state.external_health.get("consecutive_failures", {})
    return [d for d in _CRITICAL_EXTERNAL_DEPS if consec.get(d, 0) >= 2]


def _same_base_branch(a: Task, b: Task) -> bool:
    """True when two tasks share a base branch.

    A dependency only propagates a *result* when the dep's PR merges into the
    *dependent's* base branch. Same-rollup / same-default tasks share a base, so
    the merge lands where the dependent will see it. Tasks in different rollups
    carry different ``base_branch`` values (the plan/rollup branch); a
    ``depends_on`` across that boundary is timing-only — the dep merges into its
    own rollup branch while the dependent cuts from a different one, so waiting
    buys no propagation, only serialization. None/"" both normalize to the
    project integration branch, so two default-base tasks count as same-base.
    """
    a_base = (a.first_payload or {}).get("base_branch") or None
    b_base = (b.first_payload or {}).get("base_branch") or None
    return a_base == b_base


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
        # Cross-rollup deps are timing-only: the dependent's base branch never
        # inherits this dep's result (the dep merges into its own rollup branch
        # while the dependent cuts from a different one), so waiting on an
        # unfinished cross-base dep buys no propagation — only serialization.
        # Scope the *wait* to same-base deps, where the dep's merge actually
        # lands in the dependent's base. A FAILED dep still cascade-cancels above
        # regardless of base; only the wait is loosened here. File-overlap
        # conflict avoidance is handled separately by the dispatch filters
        # (filter_file_conflicts / filter_rollup_conflicts), so dropping the
        # cross-base wait does not remove conflict protection.
        same_base = _same_base_branch(task, effective_dt)

        if effective_dt.status != TaskStatus.COMPLETED:
            if same_base:
                all_met = False
            continue
        # For create tasks, also require PR to be merged — but only when the dep
        # shares this task's base (else the merge lands where the dependent can't
        # see it, so the merge requirement is meaningless).
        dt_type = (effective_dt.first_payload).get("type", "create")
        if (
            dt_type in ("create", "")
            and same_base
            and not await verify_pr_merged(effective_dt, session)
        ):
            all_met = False

    if any_failed:
        return "cascade_cancel", deps_remapped
    elif all_met:
        return "eligible", deps_remapped
    else:
        return "waiting", deps_remapped
