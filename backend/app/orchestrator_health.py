"""Health / dependency classification helpers extracted from ``app.orchestrator``.

This sibling module hosts the pure-ish helpers that classify candidate
tasks for the dispatch loop — cycle detection, dead-dep / cycle
diagnosis, the small dispatch-priority boost for health-aware
reordering, and the dependency-satisfaction check.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import _find_cycle_members``, etc.) working.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime
from typing import Callable, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

import app.shared_state as _shared_state
from app.landed_signal import (
    NON_MERGE_UNBLOCK_REASON_KEY,
    has_landed_signal,
    has_merge_unblock_signal,
)
from app.rollup_management import _FORCE_DROP_REASONS
from app.models import Project, Task, TaskStatus
from app.orchestrator_filters import _prod_files
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# ---------------------------------------------------------------------------
# Bounded base-re-cut accounting
# ---------------------------------------------------------------------------

# A held cross-base dependent whose re-cut keeps CONFLICTING never self-resolves
# (a genuine merge conflict between the dependent's staging base and integration
# requires a human / fix task). After this many CONSECUTIVE genuine-conflict
# re-cut attempts the dependent is escalated to the operator exactly once instead
# of being retried — and re-issuing the GitHub merge call — every cycle forever.
# A TRANSIENT GitHub/credential error never counts toward this bound; it resets
# the consecutive-conflict streak and keeps retrying with backoff.
_RECUT_CONFLICT_ESCALATION_THRESHOLD = 3

# blocker_kind encoded into the deduplicated escalation alert title so the
# operator-facing message reads clearly and dedups on (task_id, title).
_RECUT_CONFLICT_BLOCKER_KIND = "cross-base re-cut conflict"

# A deleted/absent base (or head) branch (HTTP 404) is a PERMANENT condition, not
# a transient error. Unlike a genuine conflict (which is retried up to the
# consecutive-conflict bound on the chance it is later resolved), a missing base
# can NEVER recover by retrying the merge, so it escalates IMMEDIATELY on the
# first occurrence under its own blocker_kind rather than being held silently and
# retried on a backoff that never terminates.
_RECUT_MISSING_BASE_BLOCKER_KIND = "cross-base re-cut base missing"


# ---------------------------------------------------------------------------
# Genuine-code vs ordering-only dependency policy
# ---------------------------------------------------------------------------

# Task types that PRODUCE a code change, and therefore can carry a real code
# relationship to a dependent. Pure-sequencing types (research / plan / review /
# investigate / strategic) only order work — a dependency of that kind never
# propagates code into the dependent's base, so it is ordering-only.
_CODE_PRODUCING_TASK_TYPES = frozenset(
    {"create", "fix", "refactor", "migrate", "docs", "deps", "qa", ""}
)

# Dependent task types that exist specifically to TRANSFORM another task's
# output: a fix / refactor that ``depends_on`` a code-producing task is,
# by virtue of that edge, changing the dependency's code.
_OUTPUT_TRANSFORMING_TASK_TYPES = frozenset({"fix", "refactor"})


def _task_type(task: Task) -> str:
    """The task's payload type, normalized ("" == default create)."""
    return (task.first_payload or {}).get("type", "create") or ""


def _is_code_producing_task(task: Task) -> bool:
    """True when *task* emits a code change (vs. a pure-sequencing task type)."""
    return _task_type(task) in _CODE_PRODUCING_TASK_TYPES


def is_genuine_code_dependency(dependent: Task, dependency: Task) -> bool:
    """True when *dependency* has a real CODE relationship with *dependent*.

    This is the policy that separates a GENUINE code-dependency (whose landed
    code the dependent must build on) from an ORDERING-ONLY dependency (pure
    sequencing, no code relationship). It composes with — does not duplicate —
    the dispatch file-overlap logic in :mod:`app.orchestrator_filters` by reusing
    its :func:`_prod_files` test-file-excluding overlap helper.

    A dependency is a genuine code-dependency when BOTH hold:

      1. it is a code-producing task (``create`` / ``fix`` / ``refactor`` /
         ``migrate`` / … — not research / plan / review / strategic), AND
      2. it has a real code relationship with the dependent, established by
         EITHER

         (a) file-overlap between the two tasks' production target files
             (``related_files`` with test files excluded), OR
         (b) the dependent being a fix / refactor that exists to change
             the dependency's output — the ``depends_on`` edge from such a task
             to a code-producing task IS that relationship. This holds UNLESS
             both sides declare files that provably do NOT overlap (in which case
             there is no code relationship and the dependency is ordering-only).

    Everything else — a code-producing dependency a non-transforming dependent
    merely sequences after with no file overlap, or any dependency on a
    non-code-producing (research/plan/…) task — is ordering-only.
    """
    if not _is_code_producing_task(dependency):
        return False

    dependent_files = _prod_files((dependent.context or {}).get("related_files") or [])
    dependency_files = _prod_files(
        (dependency.context or {}).get("related_files") or []
    )
    # (a) overlapping production target files == a real code relationship.
    if dependent_files & dependency_files:
        return True
    # (b) a fix/refactor dependent transforms the dependency's output.
    if _task_type(dependent) in _OUTPUT_TRANSFORMING_TASK_TYPES:
        # When both sides declare files that provably do not overlap, there is no
        # code relationship — treat as ordering-only. When either side omits its
        # file metadata we cannot disprove overlap, so the transforming-dependent
        # edge stands (hold rather than risk a missing-code dispatch).
        return not (dependent_files and dependency_files)
    return False


def is_ordering_only_dependency(dependent: Task, dependency: Task) -> bool:
    """True when *dependency* is pure sequencing — no code relationship.

    The exact complement of :func:`is_genuine_code_dependency`; named explicitly
    so dispatch call sites read in the affirmative ("this is ordering-only, do
    not hold").
    """
    return not is_genuine_code_dependency(dependent, dependency)


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


def is_parked_blocked_foundation(task: Task) -> bool:
    """True when *task* is a BLOCKED prerequisite that automatic recovery has
    GIVEN UP on (escalated-and-parked), not one still being actively retried.

    The reaper's blocked-escalation path stamps ``context['_blocked_escalated']``
    when it exhausts recovery and parks the row (recheck pushed far out). Only
    such a genuinely parked foundation strands its dependents — a foundation that
    is merely BLOCKED but still being rechecked/resumed must NOT trip this, so a
    foundation that later resumes never leaves its dependents wrongly escalated.
    """
    if task.status != TaskStatus.BLOCKED:
        return False
    return bool((task.context or {}).get("_blocked_escalated"))


async def find_parked_blocked_foundation(
    dependent: Task, session: AsyncSession
) -> Task | None:
    """Return the first escalated-and-parked BLOCKED dependency of *dependent*.

    Used by the dispatch deadlock-breaker to name the parked foundation in the
    operator-facing escalation it raises for a stranded dependent. Returns
    ``None`` when no dependency is currently parked.
    """
    dep_uuids = []
    for d in dependent.depends_on or []:
        try:
            dep_uuids.append(uuid.UUID(str(d)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not dep_uuids:
        return None
    rows = (
        (await session.execute(select(Task).where(Task.id.in_(dep_uuids))))
        .scalars()
        .all()
    )
    for r in rows:
        if is_parked_blocked_foundation(r):
            return r
    return None


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
      - ``'stranded_parked'`` — a dependency has been escalated-and-parked in BLOCKED
                               (automatic recovery exhausted). BLOCKED is non-terminal,
                               so the in-flight wait below would strand this dependent
                               forever and the dead-deps breaker (terminal-only) never
                               fires. Surface it so the caller escalates the dependent.

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
        # A dependency that has been escalated-and-parked in BLOCKED (automatic
        # recovery has given up on it) is non-terminal, so the in-flight skip
        # below would leave this dependent waiting forever and the dead-deps
        # breaker — which only fires when EVERY dep is terminal — would never
        # act either. Surface it so the caller escalates the stranded dependent.
        # Conservative: only a genuinely parked foundation trips this; a still-
        # recovering BLOCKED dep falls through to the normal in-flight wait.
        if any(
            d in dep_tasks_by_id and is_parked_blocked_foundation(dep_tasks_by_id[d])
            for d in deps
        ):
            out.append((t.id, "stranded_parked"))
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
            from app.failure_classification import is_non_death_cancellation

            failed_prereq = next(
                (
                    dt
                    for dt in dep_tasks
                    if dt.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                    and not is_non_death_cancellation(dt)
                ),
                None,
            )
            out.append((t.id, "cascade_cancel", failed_prereq))
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
            (await session.execute(select(Project).where(Project.id.in_(_project_ids))))
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


async def _effective_dep_task(dt: Task, session: AsyncSession) -> Task:
    """Follow a requeue-cancelled dependency to its replacement clone.

    Mirrors the ``_replaced_by_task_id`` remap in :func:`_check_candidate_task_deps`
    so the re-cut path resolves the row that actually carries the landed code (the
    replacement), not the superseded CANCELLED original — otherwise a remapped
    cross-base code-dependency would resolve to a CANCELLED row, ``status !=
    COMPLETED`` would short-circuit :func:`_has_landed_cross_base_code_dep`, and
    the re-cut would be silently skipped. Returns *dt* unchanged when there is no
    replacement to follow (or it cannot be loaded)."""
    if dt.status != TaskStatus.CANCELLED:
        return dt
    replacement_id_str = (dt.context or {}).get("_replaced_by_task_id")
    if not replacement_id_str:
        return dt
    try:
        replacement = await session.get(Task, uuid.UUID(replacement_id_str))
    except (ValueError, AttributeError):
        replacement = None
    return replacement or dt


def _has_landed_cross_base_code_dep(dependent: Task, dep_tasks: list) -> bool:
    """True when *dependent* has a CROSS-BASE GENUINE code-dependency that has
    LANDED on integration — i.e. its base must be re-cut before it can safely
    run. Pure helper shared by both re-cut call sites (dispatch + completion)."""
    return any(
        (not _same_base_branch(dependent, dep))
        and is_genuine_code_dependency(dependent, dep)
        and dep.status == TaskStatus.COMPLETED
        and has_landed_signal(dep)
        for dep in dep_tasks
    )


def _recut_backoff_active(ctx: dict) -> bool:
    """True while a failed dependent re-cut is inside its backed-off recheck window.

    Once a re-cut has failed for ANY reason (transient ``"error"``, permanent
    ``"missing"``, or a genuine ``"conflict"``), we stop re-issuing the GitHub
    merge call every dispatch/reaper cycle and only re-attempt the re-cut after the
    backed-off ``_recut_recheck_after`` window elapses — so a held dependent is not
    hammering the (non-idempotent, uncoalescable) merge API ~720x/hour against a
    shared 5000/hr token budget, yet can still recover if the condition later
    clears (a successful re-cut clears the accounting)."""
    raw = ctx.get("_recut_recheck_after")
    if not raw:
        return False
    try:
        due = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return False
    return _utcnow() < due


async def _clear_recut_accounting(dependent: Task, session: AsyncSession) -> None:
    """Clear all ``_recut_*`` accounting after a successful (or no-longer-needed)
    re-cut, and reconcile any open escalation alert.

    Mirrors the resume path's alert reconciliation: when a previously-escalated
    dependent finally re-cuts cleanly, the ``Task permanently blocked`` ERROR
    alert that declared it stuck no longer describes a real condition and is
    dismissed (best-effort, exactly as ``reaper_sweeps`` does on resume)."""
    ctx = dict(dependent.context or {})
    recut_keys = [k for k in ctx if str(k).startswith("_recut_")]
    if not recut_keys:
        return
    had_escalation = bool(ctx.get("_recut_escalated"))
    for k in recut_keys:
        ctx.pop(k, None)
    dependent.context = ctx
    flag_modified(dependent, "context")
    session.add(dependent)
    await session.commit()
    if had_escalation:
        from app.alert_dispatch import _resolve_blocked_escalation_alert

        with contextlib.suppress(Exception):
            await _resolve_blocked_escalation_alert(dependent, session)


async def _record_recut_failure(
    dependent: Task, outcome: str, session: AsyncSession
) -> None:
    """Persist a failed re-cut attempt and escalate a PERSISTENT / PERMANENT one.

    Records, in a distinct ``_recut_*`` context namespace (never colliding with
    the reaper's ``_blocked_recheck_attempts`` counter):

      * ``_recut_recheck_attempts`` — consecutive failures, driving the backed-off
        recheck schedule (reusing ``_next_recheck_delay`` / ``recheck_after``).
      * ``_recut_conflict_attempts`` — consecutive GENUINE conflicts. A transient
        ``"error"`` (and a permanent ``"missing"``) reset this to 0, so only a
        persistent true conflict ever reaches the consecutive-conflict bound.
      * ``_recut_last_outcome`` — ``"conflict"`` / ``"error"`` / ``"missing"`` for
        observability.

    Escalation policy by outcome:

      * ``"conflict"`` — on reaching ``_RECUT_CONFLICT_ESCALATION_THRESHOLD``
        consecutive conflicts, escalate EXACTLY ONCE (the conflict may yet be
        resolved, so it is retried up to the bound first).
      * ``"missing"``  — a deleted/absent base or head branch (HTTP 404) is a
        PERMANENT condition that can NEVER recover by retrying the merge, so it
        escalates IMMEDIATELY on the first occurrence (no bound) under its own
        blocker_kind and stamps a durable ``_recut_base_missing`` marker.
      * ``"error"``    — a transient class; NEVER escalates, just retries.

    Escalation goes through the same deduplicated machinery the blocked-task
    escalation uses (``_maybe_create_blocked_escalation_alert``, deduped on
    ``(task_id, title)``), and an observable ``latest_status_message`` plus a
    ``_recut_escalated`` context marker are stamped so the stuck state is no
    longer invisible.
    """
    from app.reaper_sweeps import _next_recheck_delay

    ctx = dict(dependent.context or {})
    recheck_attempts = int(ctx.get("_recut_recheck_attempts", 0) or 0) + 1
    ctx["_recut_recheck_attempts"] = recheck_attempts
    ctx["_recut_last_outcome"] = outcome

    conflict_attempts = 0
    if outcome == "conflict":
        conflict_attempts = int(ctx.get("_recut_conflict_attempts", 0) or 0) + 1
        ctx["_recut_conflict_attempts"] = conflict_attempts
    else:
        # A transient error (or a permanent missing-branch) breaks the
        # consecutive-conflict streak: it is classified separately, never on the
        # conflict bound.
        ctx["_recut_conflict_attempts"] = 0

    delay = _next_recheck_delay(recheck_attempts)
    due = _utcnow() + delay
    ctx["_recut_recheck_after"] = due.isoformat()
    # Reuse the existing recheck_after backoff column so the reaper resume sweep
    # backs off too (the BLOCKED-path caller may overwrite it with its own
    # blocked-recheck backoff — both are the same backoff family).
    dependent.recheck_after = due

    base_label = (dependent.first_payload or {}).get("base_branch") or "(default)"
    escalate_conflict = (
        outcome == "conflict"
        and conflict_attempts >= _RECUT_CONFLICT_ESCALATION_THRESHOLD
        and not ctx.get("_recut_escalated")
    )
    # A missing base is permanent — escalate on the FIRST occurrence, not after a
    # retry bound, so the dependent never parks silently and indefinitely.
    escalate_missing = outcome == "missing" and not ctx.get("_recut_escalated")

    detail = ""
    blocker_kind = _RECUT_CONFLICT_BLOCKER_KIND
    if escalate_missing:
        ctx["_recut_escalated"] = True
        ctx["_recut_base_missing"] = True
        blocker_kind = _RECUT_MISSING_BASE_BLOCKER_KIND
        detail = (
            f"Cross-base base re-cut cannot proceed: the dependent's base branch "
            f"{base_label!r} (or the integration head it merges from) no longer "
            "exists on GitHub (HTTP 404). A deleted base is a PERMANENT "
            "condition — typically the staging branch was cleaned up after its "
            "rollup merged. Retrying can never succeed; the dependent must be "
            "re-homed onto integration or cancelled by an operator."
        )
        dependent.latest_status_message = (
            f"Held: cross-base base re-cut base {base_label!r} is missing "
            "(deleted/404, permanent) — escalated to operator"
        )
    elif escalate_conflict:
        ctx["_recut_escalated"] = True
        detail = (
            f"Cross-base base re-cut has conflicted on {conflict_attempts} "
            f"consecutive attempts merging integration into base "
            f"{base_label}. "
            "The dependent cannot dispatch against a base missing its landed "
            "dependency code; a conflict resolution / fix task or operator "
            "action is required."
        )
        dependent.latest_status_message = (
            "Held: cross-base base re-cut blocked by a persistent merge conflict "
            f"after {conflict_attempts} attempts — escalated to operator"
        )

    dependent.context = ctx
    flag_modified(dependent, "context")
    session.add(dependent)
    await session.commit()

    if escalate_missing or escalate_conflict:
        from app.alert_dispatch import _maybe_create_blocked_escalation_alert

        await _maybe_create_blocked_escalation_alert(
            dependent,
            session,
            blocker_kind=blocker_kind,
            detail=detail,
        )


async def ensure_dependent_base_recut(
    dependent: Task, dep_tasks: list, session: AsyncSession
) -> bool:
    """Ensure *dependent*'s base provably contains a landed cross-base code-dep.

    If *dependent* has a CROSS-BASE GENUINE code-dependency that has landed on
    integration, merge current integration into the dependent's staging/rollup
    base (without re-homing onto integration — the rollup model is preserved).

    Returns True when the base is safe to dispatch against — either no re-cut was
    needed, or the re-cut succeeded — and False when a re-cut is required but
    could not complete, so the caller must keep HOLDING the dependent rather than
    dispatch it against a base missing the landed code.

    The chokepoint distinguishes the underlying ``"ok"`` / ``"conflict"`` /
    ``"error"`` outcome and BOUNDS a persistently-failing re-cut so all four call
    sites inherit the behavior without duplicating the accounting:

      * ``"ok"``       — clear any ``_recut_*`` accounting (recovery), reconcile a
                         stale escalation alert, and return True.
      * ``"error"``    — a TRANSIENT GitHub/credential error: record a backed-off
                         recheck, reset the consecutive-conflict streak, NEVER
                         escalate, and return False (re-attempt only after the
                         backed-off recheck window — NOT on the next cycle).
      * ``"missing"``  — a PERMANENT deleted/absent base or head branch (404):
                         escalate IMMEDIATELY (first occurrence, no retry bound)
                         under the missing-base blocker_kind with an observable
                         status message + durable ``_recut_base_missing`` marker,
                         so the dependent never parks silently forever. Returns
                         False.
      * ``"conflict"`` — a GENUINE merge conflict: record it; after
                         ``_RECUT_CONFLICT_ESCALATION_THRESHOLD`` consecutive
                         conflicts escalate exactly once (deduplicated) with an
                         observable status message + context marker, and stop
                         hammering the merge API every cycle (re-attempt only
                         after the backed-off recheck window). Returns False.

    EVERY failing outcome arms the backed-off recheck window, and the merge call
    is gated on that window ALONE (see the gate below). The schedule is
    ``_next_recheck_delay(_recut_recheck_attempts)`` =
    ``_BLOCKED_RECHECK_BASE`` (5 min) x 2**attempts, capped at
    ``_BLOCKED_RECHECK_MAX`` (1 h) — so the FIRST failure (attempts=1) holds for
    10 min, the second for 20 min, and so on. No failing outcome re-issues the
    merge POST on the next ~5s dispatch cycle. A ``"conflict"`` therefore reaches
    its 3-attempt escalation bound in ~30 min of wall-clock (10 min + 20 min)
    rather than ~10 s. This is intentional: the dependent is HELD either way, and
    the independent permanent-blocked escalation (``_BLOCKED_ESCALATE_AFTER``,
    6 h, measured on wall-clock from ``blocked_at``) still fires regardless, so a
    stuck dependent is never starved of an operator signal.

    Single source of truth for the re-cut, shared by the dispatch driver
    (:func:`recut_landed_cross_base_dependents`), the completion-side warm-claim
    trigger (``orchestrator._mark_unblocked_dependents``), and the reaper resume
    sweep (``reaper_sweeps._partition_completed_needs_base_safe``).
    """
    if not _has_landed_cross_base_code_dep(dependent, dep_tasks):
        # No re-cut needed. If this dependent previously accrued re-cut failures
        # (e.g. it was held, the dep later changed), clear that stale accounting.
        await _clear_recut_accounting(dependent, session)
        return True
    project = await session.get(Project, dependent.project_id)
    if project is None:
        # Cannot resolve the integration branch — hold to be safe.
        return False
    # Once a re-cut has FAILED, do not re-issue the GitHub merge call every cycle:
    # only re-attempt after the backed-off recheck window so a held dependent stops
    # hammering the merge API, while still allowing eventual recovery.
    #
    # The gate is the backoff window ALONE. It used to additionally require
    # ``_recut_escalated``, which made the backoff DEAD CODE for the entire
    # transient ``"error"`` class: ``_recut_escalated`` is only ever set by the
    # missing/conflict escalation branches in ``_record_recut_failure``, and
    # ``"error"`` NEVER escalates by design. A transiently-failing dependent
    # therefore re-issued a real (uncoalesced, non-idempotent) POST
    # /repos/{o}/{r}/merges on EVERY dispatch cycle — ~720/hr per held dependent,
    # forever — draining the shared 5000/hr installation-token budget, in direct
    # contradiction of this function's own contract ("a transient error keeps
    # retrying with backoff"). ``_recut_recheck_after`` is already written
    # unconditionally by ``_record_recut_failure`` for every failing outcome, so
    # reading it alone is what makes the documented backoff real.
    #
    # Escalation semantics are unaffected: the window is only ever written AFTER an
    # attempt was made and classified, so "missing" still escalates on its FIRST
    # occurrence and consecutive conflicts still accrue toward (and trip) the
    # one-shot conflict escalation — merely spaced by the backoff instead of
    # hammered every ~5s. An "ok" outcome still clears the window via
    # ``_clear_recut_accounting``, so recovery is never delayed.
    if _recut_backoff_active(dependent.context or {}):
        return False
    from app.branch_resolution import recut_dependent_base_from_integration

    outcome = await recut_dependent_base_from_integration(dependent, project, session)
    if outcome == "ok":
        await _clear_recut_accounting(dependent, session)
        return True
    await _record_recut_failure(dependent, outcome, session)
    return False


async def recut_landed_cross_base_dependents(
    eligible_ids: list,
    candidate_tasks: list,
    dep_by_id: dict,
    session: AsyncSession,
) -> list:
    """Re-cut held cross-rollup code-dependents' bases from current integration.

    Dispatch step run AFTER dependency resolution and BEFORE provisioning. For
    each eligible candidate that has a CROSS-BASE GENUINE code-dependency which
    has now LANDED on integration, merge current integration into the
    candidate's staging / rollup base so it provably contains the landed code —
    WITHOUT re-homing the dependent onto integration (the rollup and the
    qualify->promote gate are preserved). A candidate whose re-cut cannot
    complete (merge conflict or transient GitHub error) is DROPPED from this
    cycle's eligible set so it never dispatches against a base missing its
    dependency's code; it stays QUEUED and is retried next cycle. The retry is
    BOUNDED inside :func:`ensure_dependent_base_recut`: a transient error keeps
    retrying with backoff, while a persistent genuine conflict is escalated to
    the operator exactly once (a deduplicated ERROR alert + an observable status
    message / context marker) after a few consecutive conflicts, instead of
    silently re-issuing the merge call forever.

    Idempotent: when a candidate's base already bases on the integration trunk,
    or already contains integration's HEAD, the re-cut is a cheap no-op.

    Defense-in-depth: a dropped candidate that was already stamped
    ``_deps_satisfied`` (the warm-claim marker) this cycle has that marker
    durably CLEARED here, so the cheap ``/next-role`` backlog scan
    (``warm_backlog_reuse``) cannot warm-claim it and dispatch it against a base
    missing the landed code. The dispatch loop now re-cuts BEFORE stamping, so
    this clear is belt-and-suspenders for any other stamp path.
    """
    if not eligible_ids:
        return eligible_ids

    task_by_id = {t.id: t for t in candidate_tasks}
    dropped: set = set()

    # Batch-fetch replacement tasks for CANCELLED dependencies to avoid N+1
    # session.get calls inside the loop below. Collect all replacement_id_str
    # values across eligible candidates, then fetch them in one query.
    replacement_ids: set[uuid.UUID] = set()
    for eid in eligible_ids:
        cand = task_by_id.get(eid)
        if cand is None or not (cand.depends_on or []):
            continue
        for raw in cand.depends_on or []:
            try:
                du = uuid.UUID(raw)
            except (ValueError, AttributeError):
                continue
            dep = dep_by_id.get(du)
            if dep is not None and dep.status == TaskStatus.CANCELLED:
                rid_str = (dep.context or {}).get("_replaced_by_task_id")
                if rid_str:
                    try:
                        replacement_ids.add(uuid.UUID(rid_str))
                    except (ValueError, AttributeError):
                        pass

    replacement_by_id: dict[uuid.UUID, Task] = {}
    if replacement_ids:
        repl_result = await session.execute(
            select(Task).where(Task.id.in_(replacement_ids))
        )
        for t in repl_result.scalars().all():
            replacement_by_id[t.id] = t

    for eid in eligible_ids:
        cand = task_by_id.get(eid)
        if cand is None or not (cand.depends_on or []):
            continue
        dep_tasks = []
        for raw in cand.depends_on or []:
            try:
                du = uuid.UUID(raw)
            except (ValueError, AttributeError):
                continue
            dep = dep_by_id.get(du)
            if dep is not None:
                # Follow the requeue-cancelled → replacement remap so a remapped
                # cross-base code-dependency's landed code still triggers re-cut.
                # Resolution uses the batch-fetched replacement_by_id dict
                # instead of a per-dep session.get.
                if dep.status == TaskStatus.CANCELLED:
                    rid_str = (dep.context or {}).get("_replaced_by_task_id")
                    if rid_str:
                        try:
                            replacement = replacement_by_id.get(uuid.UUID(rid_str))
                            if replacement is not None:
                                dep_tasks.append(replacement)
                                continue
                        except (ValueError, AttributeError):
                            pass
                dep_tasks.append(dep)
        if not dep_tasks:
            continue
        if not await ensure_dependent_base_recut(cand, dep_tasks, session):
            dropped.add(eid)
            await _clear_deps_satisfied_marker(cand.id, session)
            logger.warning(
                "[CROSS-BASE] Holding dependent %s this cycle — base re-cut incomplete",
                eid,
            )
    if not dropped:
        return eligible_ids
    return [eid for eid in eligible_ids if eid not in dropped]


async def _dep_parent_lineage_has_landed(dep: Task, session: AsyncSession) -> bool:
    """True when *dep*'s immediate lineage parent has landed on integration AND
    *dep* itself genuinely merged into that lineage.

    Defense-in-depth for cross-base code-deps that completed before the
    recursive propagation swept them: a staging-merged code task whose own
    ``landed_on_integration`` flag was not yet flipped is still provably on
    the integration trunk when its parent research/rollup task has landed
    (the parent's rollup merge carried the child's code to integration).
    Checking one level up is sufficient because
    ``_propagate_landed_to_rollup_children`` stamps the immediate parent when
    the ancestor rollup lands; the parent's landed signal is durable.

    Guards against false positives: a force-dropped dep (review_respawn_cap /
    malformed_pr_url / rollup_no_pr_artifact) or a no-merge COMPLETED dep
    (has_merge_unblock_signal False) must NOT be released — their code was
    discarded, so the parent landing on integration does NOT carry their code
    to the trunk. Mirrors the sweep filter in
    ``rollup_management._propagate_landed_to_rollup_children``.
    """
    ctx = dep.context or {}
    if ctx.get(NON_MERGE_UNBLOCK_REASON_KEY) in _FORCE_DROP_REASONS:
        return False
    if not has_merge_unblock_signal(dep):
        return False
    parent_id = dep.parent_task_id
    if not parent_id:
        return False
    parent = await session.get(Task, parent_id)
    if parent is None:
        return False
    return has_landed_signal(parent)


async def _clear_deps_satisfied_marker(task_id, session: AsyncSession) -> None:
    """Durably clear the ``_deps_satisfied`` warm-claim marker on *task_id*.

    Used when a held cross-base code-dependent is dropped from this cycle's
    eligible set: the marker must not outlive the eligibility, or a warm VM could
    claim the task via the backlog scan and dispatch it against a base that does
    not yet contain its dependency's landed code."""
    row = await session.get(Task, task_id)
    if row is None or not (row.context or {}).get("_deps_satisfied"):
        return
    new_ctx = dict(row.context or {})
    new_ctx.pop("_deps_satisfied", None)
    row.context = new_ctx
    session.add(row)
    await session.commit()


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

    # Non-death cancellations are satisfied, non-blocking deps wherever they
    # surface, whether CANCELLED (current) or FAILED (legacy SCOPE_EMPTY rows
    # pre-dating the CANCELLED relabel). A SCOPE_EMPTY dep is a deliberate "scope
    # already covered" exit (branch intact); a co-deferred dep is recoverable
    # shelving that re-surfaces with its prerequisite. Both are recognized by the
    # shared predicate is_non_death_cancellation — the SAME source of truth the
    # resume system's dead-foundation detector uses, so the two cannot drift. A
    # requeue-replaced dep keeps its distinct remap mechanics (the dependent waits
    # on the live replacement clone, not a satisfied no-op).
    from app.failure_classification import is_non_death_cancellation

    # Batch-fetch all replacement tasks up front to avoid N+1 queries.
    _replacement_uuids: list[uuid.UUID] = []
    for _dt in dep_tasks:
        if _dt.status == TaskStatus.CANCELLED:
            _rid = (_dt.context or {}).get("_replaced_by_task_id")
            if _rid:
                with contextlib.suppress(ValueError, AttributeError):
                    _replacement_uuids.append(uuid.UUID(_rid))
    _replacement_cache: dict[uuid.UUID, Task] = {}
    if _replacement_uuids:
        _rows = await session.execute(
            select(Task).where(Task.id.in_(_replacement_uuids))
        )
        for _t in _rows.scalars().all():
            _replacement_cache[_t.id] = _t

    for dt in dep_tasks:
        effective_dt = dt
        if dt.status == TaskStatus.CANCELLED:
            replacement_id_str = (dt.context or {}).get("_replaced_by_task_id")
            if replacement_id_str:
                try:
                    replacement = _replacement_cache.get(uuid.UUID(replacement_id_str))
                except (ValueError, AttributeError):
                    replacement = None
                if replacement:
                    deps_remapped[str(dt.id)] = str(replacement.id)
                    effective_dt = replacement
                else:
                    # Replacement task is gone — treat as permanently cancelled
                    any_failed = True
                    break
            elif is_non_death_cancellation(dt):
                # Clean no-op (SCOPE_EMPTY) or recoverable co-deferred shelving —
                # satisfied; do not cascade-cancel the dependent.
                continue
            else:
                # Permanently cancelled (no non-death marker recorded)
                any_failed = True
                break

        if effective_dt.status == TaskStatus.FAILED:
            if is_non_death_cancellation(effective_dt):
                continue  # legacy FAILED+SCOPE_EMPTY rows — satisfied
            any_failed = True
            break
        if effective_dt.status == TaskStatus.CANCELLED:
            if is_non_death_cancellation(effective_dt):
                continue
            any_failed = True
            break
        # A cross-base dependency only reaches the dependent's base when it lands
        # on the SHARED integration trunk. Distinguish a GENUINE code-dependency
        # (whose landed code the dependent must build on — HOLD until it lands,
        # then re-cut the base) from an ORDERING-ONLY one (pure sequencing — its
        # result never reaches the dependent's base, so waiting buys only
        # serialization). Same-base deps are unchanged: the dep's merge lands
        # directly in the dependent's base, so the existing all_met / PR-merge
        # wait applies. A FAILED dep still cascade-cancels above regardless of
        # base; only the WAIT policy is decided here. File-overlap conflict
        # avoidance for in-flight work is still handled by the dispatch filters
        # (filter_file_conflicts / filter_rollup_conflicts).
        same_base = _same_base_branch(task, effective_dt)
        cross_base_code_dep = (not same_base) and is_genuine_code_dependency(
            task, effective_dt
        )

        if effective_dt.status != TaskStatus.COMPLETED:
            # Hold while the dep is in flight whenever its result must reach the
            # dependent: same-base (merge lands in the dependent's base) OR a
            # cross-base genuine code-dependency (held until it lands on
            # integration, after which the dependent's base is re-cut). An
            # ordering-only cross-base dep never propagates, so it stays
            # non-blocking exactly as before.
            if same_base or cross_base_code_dep:
                all_met = False
            continue

        if cross_base_code_dep:
            # COMPLETED cross-rollup genuine code-dependency: its code is only
            # reachable from the dependent's integration trunk once it has LANDED
            # on integration (has_landed_signal). A merge to its own staging /
            # rollup branch (pr_merged-to-staging) is NOT enough — keep holding
            # until it lands, after which the dependent becomes eligible and its
            # base is re-cut from current integration before dispatch.
            #
            # Defense-in-depth: when the dep's own signal was not yet propagated
            # (e.g. it is a grandchild of a landed rollup whose recursive sweep
            # hadn't run yet), check whether its immediate lineage parent has
            # landed — the parent's rollup merge provably carried this dep's code
            # to integration, so holding further wedges the dependent forever.
            if not has_landed_signal(effective_dt):
                if not await _dep_parent_lineage_has_landed(effective_dt, session):
                    all_met = False
            continue

        # Same-base (or ordering-only cross-base) COMPLETED dependency. For
        # create tasks, also require PR to be merged — but only when the dep
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
