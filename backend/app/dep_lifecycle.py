"""Dependency-lifecycle helpers extracted from ``app.orchestrator``.

This module hosts the cluster of functions that manage the full lifecycle
of task dependencies: stamping the deps-satisfied readiness marker, cascade-
cancelling dependents of failed prerequisites, escalating stranded dependents
behind parked BLOCKED foundations, breaking deadlock cycles, and proactively
marking unblocked dependents warm-claimable at completion time.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import _mark_unblocked_dependents``, etc.) working.
"""

import logging
import uuid
from datetime import timedelta

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.depends_on_provenance import remap_structural_depends_on
from app.models import Task, TaskStatus
from app.orchestrator_health import (
    _check_candidate_task_deps,
    _classify_stuck_tasks,
    _effective_dep_task,
)
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


async def _persist_deps_satisfied(task: Task) -> None:
    """Durably stamp the sticky ``_deps_satisfied`` readiness marker on a
    dependency-bearing task whose dependencies the orchestrator has just
    resolved as fully satisfied.

    This marker is the contract between the orchestrator's authoritative,
    GitHub-aware dependency / PR-merge resolution (``_check_candidate_task_deps``
    returning ``"eligible"``) and the two warm-reuse hot paths — the
    ``/next-role`` warm-backlog scan and the dispatch first-refusal hold. Both
    treat a task carrying this marker as warm-claimable WITHOUT re-running dep
    resolution at high frequency. It is stamped only after deps resolve eligible
    (a failed dep cascade-cancels the dependent instead of stamping), so the
    marker can never outlive an unsatisfied dependency.

    Persisted in its own session (mirroring the dep-remap write) so the Step 3
    dispatch re-read and the next-role scan both observe it, and mirrored onto
    the in-memory ``task`` so the same-cycle dispatch sees it without a re-read.
    Idempotent: a no-op when the marker is already present.
    """
    if (task.context or {}).get("_deps_satisfied"):
        return
    async with AsyncSessionLocal() as deps_session:
        deps_task = await deps_session.get(Task, task.id)
        if deps_task is not None:
            deps_task.context = {
                **(deps_task.context or {}),
                "_deps_satisfied": True,
            }
            deps_session.add(deps_task)
            await deps_session.commit()
    task.context = {**(task.context or {}), "_deps_satisfied": True}


async def _cascade_cancel_dependent(task_id, project_id, broadcast_to_project) -> int:
    """Cancel a dependent whose prerequisite genuinely failed, cascading to its
    non-terminal children, releasing any held account slot, and tearing down its VM.

    Single source of truth shared by the main dispatch dependency check and the
    deadlock-breaker sweep so both apply identical dependency-death policy — a task
    whose real prerequisite failed is recognized once and cancelled, never
    dispatched as impossible work.

    Returns the number of tasks cancelled (the dependent itself plus its cascaded
    children) so callers can accumulate a single deduplicated operator alert per
    failed prerequisite. Returns 0 if the dependent row no longer exists.
    """
    from app.max_plan_pool import release_max_plan_account, get_task_llm_account_id
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.alert_dispatch import _maybe_create_failure_alert

    vm_id = None
    child_vm_ids = []
    cancelled_count = 0
    async with AsyncSessionLocal() as cancel_session:
        cancel_task = await cancel_session.get(Task, task_id)
        if cancel_task is None:
            return 0
        vm_id = cancel_task.vm_instance_id
        cancel_task.status = TaskStatus.CANCELLED
        if cancel_task.vm_provisioned_at:
            cancel_task.vm_stopped_at = _utcnow()
        cancel_task.latest_status_message = "Cancelled: dependency task failed"
        # Release account slot if held
        cancel_acct = get_task_llm_account_id(cancel_task)
        if cancel_acct and not (cancel_task.context or {}).get("_slot_released"):
            await release_max_plan_account(
                cancel_acct,
                project_id=str(cancel_task.project_id) if cancel_task.project_id else None,
            )
            cancel_task.context = {
                **(cancel_task.context or {}),
                "_slot_released": True,
            }
        cancel_session.add(cancel_task)
        # Cascade cancel to children in non-terminal states
        children = (
            (
                await cancel_session.execute(
                    select(Task).where(
                        Task.parent_task_id == task_id,
                        Task.status.not_in(
                            [
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                                TaskStatus.CANCELLED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for child in children:
            child.status = TaskStatus.CANCELLED
            if child.vm_provisioned_at:
                child.vm_stopped_at = _utcnow()
            child.latest_status_message = "Cancelled: parent dependency failed"
            child_acct = get_task_llm_account_id(child)
            if child_acct and not (child.context or {}).get("_slot_released"):
                await release_max_plan_account(
                    child_acct,
                    project_id=str(child.project_id) if child.project_id else None,
                )
                child.context = {**(child.context or {}), "_slot_released": True}
            cancel_session.add(child)
            # Track child VM IDs for post-commit deletion
            if child.vm_instance_id:
                child_vm_ids.append(child.vm_instance_id)
        await _maybe_create_failure_alert(
            cancel_task,
            cancel_session,
            "Cascade cancelled: dependency task failed",
        )
        cancelled_count = 1 + len(children)
        await cancel_session.commit()
    await broadcast_to_project(
        str(project_id),
        "task_updated",
        {"task_id": str(task_id), "status": TaskStatus.CANCELLED},
    )
    await _ensure_vm_deleted(vm_id, reason="dependency_cancelled")
    # Tear down provisioned VMs for any children that have them
    for child_vm_id in child_vm_ids:
        await _ensure_vm_deleted(child_vm_id, reason="dependency_cancelled")
    logger.error(f"[ORCHESTRATOR] Cancelled task {task_id} — dependency failed")
    return cancelled_count


async def _escalate_stranded_dependent(
    dependent, session, broadcast_to_project, scan_label=""
) -> bool:
    """Surface a dependent stranded behind an escalated-and-parked BLOCKED
    foundation.

    The dependent is gated on a foundation that automatic recovery has given up
    on (parked in BLOCKED). BLOCKED is non-terminal, so the normal dependency
    gate would keep the dependent QUEUED forever with no operator signal. Move it
    into BLOCKED+escalated, naming the parked foundation, and raise ONE
    deduplicated operator alert so the whole stuck chain becomes a single
    actionable item instead of waiting silently. Returns ``True`` when escalated.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.alert_dispatch import _maybe_create_blocked_escalation_alert
    from app.orchestrator_health import find_parked_blocked_foundation
    from app.reaper_sweeps import _BLOCKED_ESCALATED_PARK
    from app.task_blocked import build_need, enter_blocked_state

    foundation = await find_parked_blocked_foundation(dependent, session)
    if foundation is None:
        return False
    label = foundation.title or str(foundation.id)
    detail = (
        f"Stranded behind parked foundation '{label}' ({foundation.id}): automatic "
        f"recovery has given up on that blocked prerequisite, so this dependent can "
        f"never dispatch until an operator intervenes (requeue / re-point / cancel "
        f"the foundation){scan_label}."
    )
    enter_blocked_state(
        dependent,
        waiting_on=f"parked foundation {label}",
        reason=detail,
        source="system",
        blocker_kind="upstream_dependency",
        recheck_in_seconds=None,
        escalated=True,
        need=build_need("task", str(foundation.id), label=foundation.title),
    )
    ctx = dict(dependent.context or {})
    ctx["_blocked_escalated"] = True
    dependent.context = ctx
    dependent.recheck_after = _utcnow() + _BLOCKED_ESCALATED_PARK
    flag_modified(dependent, "context")
    session.add(dependent)
    logger.error(
        f"[ORCHESTRATOR] Stranded dependent {dependent.id} escalated — parked "
        f"foundation {foundation.id}{scan_label}"
    )
    # Fires (and commits) one deduplicated ERROR alert per stranded dependent.
    await _maybe_create_blocked_escalation_alert(
        dependent, session, blocker_kind="upstream_dependency", detail=detail
    )
    await broadcast_to_project(
        str(dependent.project_id),
        "task_updated",
        {
            "task_id": str(dependent.id),
            "status": TaskStatus.BLOCKED,
            "message": detail,
        },
    )
    return True


async def _escalate_unlandable_dependent(
    dependent, foundation_info, session, broadcast_to_project, scan_label=""
) -> bool:
    """Surface a dependent stranded behind a COMPLETED dependency whose PR can
    never merge on its own.

    The same-base dependency gate releases a code-producing dependency only once
    its PR is MERGED. When the review loop that owns that PR has been parked for
    operator disposition, automatic fix spawning is deliberately stopped — and
    the dependency is already terminal — so the PR will never merge and the gate
    holds this dependent QUEUED forever behind a bare "Waiting on N deps"
    message, with no reason an operator can act on.

    Deliberately does NOT clear ``depends_on`` and dispatch: the dependent's base
    genuinely lacks the prerequisite's code, which is the very invariant the
    merge requirement exists to protect. It also does not re-drive the PR —
    re-driving is exactly what the operator park stopped. Instead move the
    dependent into BLOCKED+escalated NAMING the unmerged PR and the park reason,
    and raise ONE deduplicated operator alert. Returns ``True`` when escalated.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.alert_dispatch import _maybe_create_blocked_escalation_alert
    from app.reaper_sweeps import _BLOCKED_ESCALATED_PARK
    from app.task_blocked import build_need, enter_blocked_state

    if not isinstance(foundation_info, dict):
        return False
    foundation_id = str(foundation_info.get("id") or "")
    if not foundation_id:
        return False
    label = str(foundation_info.get("label") or foundation_id)
    park_reason = str(foundation_info.get("reason") or "parked_for_operator")
    pr_number = foundation_info.get("pr_number")
    pr_text = f"PR #{pr_number}" if pr_number else "its pull request"
    detail = (
        f"Stranded behind unlanded prerequisite '{label}' ({foundation_id}): that "
        f"dependency COMPLETED but {pr_text} is still open and its review loop is "
        f"parked for operator disposition ({park_reason}), so automatic fix "
        f"spawning has stopped and the PR will never merge on its own. This "
        f"dependent needs the prerequisite's merged code, so it can never "
        f"dispatch until an operator disposes of that PR (merge it, reset the "
        f"iteration cap to resume the loop, or cancel this dependent)"
        f"{scan_label}."
    )
    enter_blocked_state(
        dependent,
        waiting_on=f"unlanded prerequisite {label}",
        reason=detail,
        source="system",
        blocker_kind="upstream_dependency",
        recheck_in_seconds=None,
        escalated=True,
        need=build_need("task", foundation_id, label=foundation_info.get("label")),
    )
    ctx = dict(dependent.context or {})
    ctx["_blocked_escalated"] = True
    dependent.context = ctx
    dependent.recheck_after = _utcnow() + _BLOCKED_ESCALATED_PARK
    # Overwrite the stale "Waiting on N deps: <prefix>" line. That bare message is
    # the whole visibility defect: it is what an operator reads on the task list,
    # and it names neither the unmerged PR nor the park that made the wait
    # permanent. enter_blocked_state records the reason on blocked_reason /
    # context['blocker'], but not here.
    dependent.latest_status_message = detail
    flag_modified(dependent, "context")
    session.add(dependent)
    logger.error(
        f"[ORCHESTRATOR] Stranded dependent {dependent.id} escalated — unlanded "
        f"prerequisite {foundation_id} ({park_reason}){scan_label}"
    )
    # Fires (and commits) one deduplicated ERROR alert per stranded dependent.
    await _maybe_create_blocked_escalation_alert(
        dependent, session, blocker_kind="upstream_dependency", detail=detail
    )
    await broadcast_to_project(
        str(dependent.project_id),
        "task_updated",
        {
            "task_id": str(dependent.id),
            "status": TaskStatus.BLOCKED,
            "message": detail,
        },
    )
    return True


async def _apply_deadlock_classification(
    classified, broadcast_to_project, eligible_ids, scan_label=""
) -> int:
    """Apply a :func:`_classify_stuck_tasks` result.

    True cycles and satisfied-dead deps get ``depends_on`` cleared and become
    dispatchable; a dependent whose real prerequisite genuinely failed is
    cascade-cancelled (NOT dispatched) via the same policy the main check uses.
    Emits a deduplicated operator alert per distinct failed prerequisite (same
    contract as the main dispatch loop). Returns the number of tasks acted on.
    """
    if not classified:
        return 0
    # Each item is (task_id, reason) or (task_id, reason, failed_prereq).
    cancels: list[tuple] = []
    acted = 0
    all_ids = [item[0] for item in classified]
    async with AsyncSessionLocal() as fix_session:
        rows = await fix_session.execute(select(Task).where(Task.id.in_(all_ids)))
        tasks_by_id = {t.id: t for t in rows.scalars().all()}
        for item in classified:
            task_id, reason = item[0], item[1]
            failed_prereq = item[2] if len(item) > 2 else None
            fix_task = tasks_by_id.get(task_id)
            if reason == "cascade_cancel":
                if fix_task:
                    cancels.append((task_id, fix_task.project_id, failed_prereq))
                    acted += 1
                continue
            if reason == "stranded_unlanded":
                # Stranded behind a COMPLETED dependency whose PR can never
                # merge on its own (its review loop is parked for operator
                # disposition). Escalate the dependent WITH the reason; never
                # clear deps and dispatch — its base genuinely lacks the
                # prerequisite's code. ``item[2]`` carries the foundation
                # descriptor dict for this classification.
                if fix_task and await _escalate_unlandable_dependent(
                    fix_task,
                    failed_prereq,
                    fix_session,
                    broadcast_to_project,
                    scan_label,
                ):
                    acted += 1
                continue
            if reason == "stranded_parked":
                # Stranded behind an escalated-and-parked BLOCKED foundation:
                # escalate the dependent (do NOT clear deps / dispatch it).
                if fix_task and await _escalate_stranded_dependent(
                    fix_task, fix_session, broadcast_to_project, scan_label
                ):
                    acted += 1
                continue
            if fix_task:
                if reason == "cycle":
                    msg = f"Dependencies cleared: dependency cycle detected{scan_label}"
                    tag = "cycle broken"
                else:
                    msg = (
                        "Dependencies cleared: all deps satisfied/cancelled"
                        f"{scan_label}"
                    )
                    tag = "dead deps cleared"
                logger.warning(
                    f"[ORCHESTRATOR] Breaking deadlock ({tag}{scan_label}): "
                    f"clearing depends_on for task {task_id}"
                )
                fix_task.depends_on = []
                fix_task.latest_status_message = msg
                fix_session.add(fix_task)
                await broadcast_to_project(
                    str(fix_task.project_id),
                    "task_updated",
                    {"task_id": str(task_id), "status": "QUEUED", "message": msg},
                )
            eligible_ids.append(task_id)
            acted += 1
        await fix_session.commit()
    # Cascade-cancel after the clear-commit so each cancel manages its own session.
    # Accumulate counts per prereq to emit ONE deduplicated operator alert each.
    from app.alert_dispatch import _maybe_create_cascade_cancel_alert

    _cancel_by_prereq: dict[uuid.UUID, dict] = {}
    for task_id, project_id, failed_prereq in cancels:
        _count = await _cascade_cancel_dependent(
            task_id, project_id, broadcast_to_project
        )
        if failed_prereq is not None and _count > 0:
            _entry = _cancel_by_prereq.setdefault(
                failed_prereq.id,
                {"prereq_task": failed_prereq, "count": 0},
            )
            _entry["count"] += _count
    if _cancel_by_prereq:
        async with AsyncSessionLocal() as _alert_session:
            for _entry in _cancel_by_prereq.values():
                try:
                    await _maybe_create_cascade_cancel_alert(
                        _alert_session,
                        _entry["prereq_task"].project_id,
                        _entry["prereq_task"],
                        _entry["count"],
                    )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Failed to raise cascade-cancel alert "
                        "for prereq %s (deadlock-breaker path)",
                        _entry["prereq_task"].id,
                    )
    return acted


async def _recover_stale_dependency_deadlocks(
    session,
    *,
    exclude_task_ids: set[uuid.UUID] | None = None,
    eligible_ids: list[uuid.UUID] | None = None,
    scan_label: str = " (broad scan)",
) -> int:
    """Slot-independent dependency recovery over stale QUEUED tasks.

    Scans QUEUED tasks older than 10 min that carry a ``depends_on`` edge and
    applies the shared dependency-death policy: cascade-cancel the dependents
    of a foundation that genuinely FAILED/CANCELLED, and break/release
    ``depends_on`` cycles or satisfied-dead deps. None of this consumes a VM
    slot, so it can run even when the fleet is saturated or auto-paused.

    Returns the number of tasks acted on.
    """
    from app.rollup_management import _verify_pr_merged
    from app.broadcast import broadcast_to_project

    exclude_task_ids = exclude_task_ids or set()
    eligible_ids = eligible_ids if eligible_ids is not None else []

    ten_min_ago = _utcnow() - timedelta(minutes=10)
    # Full rows (no load_only): the shared dependency-death policy reads
    # first_payload / context on both the stuck task and its deps, so a
    # partial projection would trip an async lazy-load.
    broad_stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.QUEUED,
            Task.created_at < ten_min_ago,
            Task.requires_approval.is_(False),
            Task.depends_on.isnot(None),
        )
        .limit(100)
    )
    broad_stuck = (await session.execute(broad_stmt)).scalars().all()
    broad_stuck = [
        t
        for t in broad_stuck
        if (t.depends_on or [])
        and t.id not in eligible_ids
        and t.id not in exclude_task_ids
    ]
    if not broad_stuck:
        return 0

    broad_stuck_ids = {t.id for t in broad_stuck}
    # Batch all dependency UUIDs for the scan into one query
    all_dep_ids_broad: set[uuid.UUID] = set()
    broad_task_dep_uuids: dict = {}
    for stask in broad_stuck:
        dep_uuids_b: set[uuid.UUID] = set()
        for d in stask.depends_on or []:
            try:
                dep_uuids_b.add(uuid.UUID(d))
            except (ValueError, AttributeError):
                continue
        broad_task_dep_uuids[stask.id] = dep_uuids_b
        all_dep_ids_broad.update(dep_uuids_b)

    broad_dep_tasks_by_id: dict = {}
    if all_dep_ids_broad:
        broad_dep_rows = await session.execute(
            select(Task).where(Task.id.in_(all_dep_ids_broad))
        )
        broad_dep_tasks_by_id = {dt.id: dt for dt in broad_dep_rows.scalars().all()}

    broad_classified = await _classify_stuck_tasks(
        broad_stuck,
        broad_task_dep_uuids,
        broad_dep_tasks_by_id,
        broad_stuck_ids,
        session,
        _verify_pr_merged,
    )
    return await _apply_deadlock_classification(
        broad_classified,
        broadcast_to_project,
        eligible_ids,
        scan_label=scan_label,
    )


async def _mark_unblocked_dependents(completed_task: Task, verify_pr_merged) -> int:
    """Completion-side trigger: when a task finishes and its PR has landed on the
    integration branch, proactively stamp the sticky ``_deps_satisfied`` readiness
    marker on any QUEUED dependent whose dependencies now authoritatively resolve
    as satisfied.

    Without this, a just-unblocked dependent stays unmarked until a later
    capacity-and-window-bounded dispatch scan happens to reach it, so the
    just-completed task's warm VM — polling for follow-on work inside its
    teardown-defer grace window — skips it and tears down cold, and the
    orchestrator then cold-provisions a fresh VM for the very dependent the
    completion just unblocked. Marking it here makes the obvious sibling-to-sibling
    fan-out reusable in the saturated-fleet regime where reuse matters most.

    The readiness signal is NOT loosened: this reuses the same authoritative,
    GitHub-aware ``_check_candidate_task_deps`` resolution the dispatch scan uses,
    and stamps only the ``"eligible"`` result — a dependent with any still-unresolved
    or failed dependency continues to carry no marker and continue to cold-dispatch
    and cascade-cancel exactly as before. Adds no cold provisioning.

    Async hot-loop safety: dependents and their dependency rows are loaded with
    full ``select(Task)`` (every column present, no partial projection) so the
    downstream attribute reads inside ``_check_candidate_task_deps`` cannot trip a
    MissingGreenlet. The eligibility pass performs no commits, so the attached rows
    are never expired mid-iteration; the marker write happens afterwards in its own
    session.
    """
    completed_id_str = str(completed_task.id)
    project_id = completed_task.project_id
    eligible_ids: list[uuid.UUID] = []
    # Bound the candidate scan. ``depends_on`` is a JSON column filtered in Python
    # below, so the cap is on the raw QUEUED-dependent pool, not the matched subset.
    # Order deterministically (oldest first) so the bounded window is stable rather
    # than nondeterministic, and surface a warning when it is saturated so the
    # silent-drop of surplus dependents for a >cap fan-out is observable.
    _DEPENDENT_SCAN_CAP = 100
    async with AsyncSessionLocal() as dep_session:
        dependents = (
            (
                await dep_session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.QUEUED,
                        Task.requires_approval.is_(False),
                        Task.project_id == project_id,
                        Task.depends_on.isnot(None),
                    )
                    .order_by(Task.created_at)
                    .limit(_DEPENDENT_SCAN_CAP)
                )
            )
            .scalars()
            .all()
        )
        if len(dependents) >= _DEPENDENT_SCAN_CAP:
            logger.warning(
                "[warm-reuse] _mark_unblocked_dependents hit the %d-dependent scan "
                "cap for project %s after task %s; surplus QUEUED dependents are not "
                "marked this pass and will fall back to the dispatch scan",
                _DEPENDENT_SCAN_CAP,
                project_id,
                completed_id_str,
            )
        dependents = [
            d
            for d in dependents
            if completed_id_str in (d.depends_on or [])
            and not (d.context or {}).get("_deps_satisfied")
        ]
        if not dependents:
            return 0
        # Batch every dependency row across all dependents into one query.
        per_dep_uuids: dict[uuid.UUID, list[uuid.UUID]] = {}
        all_dep_uuids: set[uuid.UUID] = set()
        for d in dependents:
            du: list[uuid.UUID] = []
            for raw in d.depends_on or []:
                try:
                    du.append(uuid.UUID(raw))
                except (ValueError, AttributeError):
                    continue
            per_dep_uuids[d.id] = du
            all_dep_uuids.update(du)
        dep_by_id: dict[uuid.UUID, Task] = {}
        if all_dep_uuids:
            dep_by_id = {
                dt.id: dt
                for dt in (
                    await dep_session.execute(
                        select(Task).where(Task.id.in_(all_dep_uuids))
                    )
                )
                .scalars()
                .all()
            }
        for d in dependents:
            du = per_dep_uuids[d.id]
            dep_tasks = [dep_by_id[u] for u in du if u in dep_by_id]
            result_str, _ = await _check_candidate_task_deps(
                d, dep_tasks, du, dep_session, verify_pr_merged
            )
            if result_str == "eligible":
                # A dependent unblocked by a cross-rollup code-dependency landing
                # must have its base re-cut from current integration BEFORE it is
                # marked warm-claimable — otherwise a warm VM could claim it via
                # the _deps_satisfied marker and run against a base missing the
                # landed code, bypassing the dispatch-loop re-cut. Reuse the same
                # re-cut; skip the marker (keep holding) if it can't complete.
                # Resolve effective (remap-following) dep rows first so a
                # requeue-cancelled-and-replaced cross-base code-dependency is
                # recognized as landed — otherwise the CANCELLED original is
                # skipped by _has_landed_cross_base_code_dep and no re-cut fires,
                # leaving the dependent stamped against a base missing the
                # replacement's landed code (mirrors the dispatch loop and
                # recut_landed_cross_base_dependents).
                from app.orchestrator_health import ensure_dependent_base_recut

                effective_dep_tasks = [
                    await _effective_dep_task(dt, dep_session) for dt in dep_tasks
                ]
                if not await ensure_dependent_base_recut(
                    d, effective_dep_tasks, dep_session
                ):
                    continue
                eligible_ids.append(d.id)
    if not eligible_ids:
        return 0
    async with AsyncSessionLocal() as stamp_session:
        for r in (
            (await stamp_session.execute(select(Task).where(Task.id.in_(eligible_ids))))
            .scalars()
            .all()
        ):
            if not (r.context or {}).get("_deps_satisfied"):
                r.context = {**(r.context or {}), "_deps_satisfied": True}
                stamp_session.add(r)
        await stamp_session.commit()
    # Surface the completion-side trigger's lift on the reuse funnel so operators
    # can confirm the widening change is converting just-unblocked dependents into
    # warm-claimable work. Best-effort: telemetry must never abort the completion
    # cycle. Recorder no-ops on a non-positive count, but the early ``return 0``
    # paths above carry no marked dependents, so this single call at the marked-some
    # landing is the authoritative record for all three completion callers.
    from app.warm_backlog_reuse import record_completion_dependents_marked

    try:
        record_completion_dependents_marked(len(eligible_ids))
    except Exception:
        logger.exception("[warm-reuse] completion_dependents_marked telemetry failed")
    return len(eligible_ids)


async def _persist_requeue_dep_remap(task: Task, deps_remapped: dict[str, str]) -> bool:
    """Rewrite a candidate's requeue-cancelled dep edges to their replacements.

    Updates BOTH ``depends_on`` and the per-edge ``_structural_depends_on_ids``
    provenance so a surviving structural prerequisite still resolves through
    ``_has_edge_prerequisite_provenance`` after the remap — otherwise
    clear-wave-deps would mistake the live edge for legacy sequencing and strip
    it. Persists via its own session and mirrors the change onto the in-memory
    ``task``. Returns True when an edge was actually rewritten.
    """
    new_deps = [deps_remapped.get(d, d) for d in (task.depends_on or [])]
    if new_deps == list(task.depends_on or []):
        return False
    async with AsyncSessionLocal() as remap_session:
        remap_task = await remap_session.get(Task, task.id)
        if remap_task:
            remap_task.depends_on = new_deps
            remap_structural_depends_on(remap_task, deps_remapped)
            remap_session.add(remap_task)
            await remap_session.commit()
    task.depends_on = new_deps
    remap_structural_depends_on(task, deps_remapped)
    return True
