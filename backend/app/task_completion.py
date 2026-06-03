"""Task completion handlers and their supporting functions.

Extracted from app.main to keep main.py focused on routing and orchestration.
These handlers are called from _process_post_commit() in main.py based on task type.

Anomaly detection, research completion, and strategic completion handlers have
been further extracted into their own modules (anomaly_detection.py,
research_completion.py, strategic_completion.py).
"""

import asyncio
import json
import logging
import random
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.github import (
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,  # noqa: F401  (re-exported for tests / downstream)
    build_blueprint_pr_merge_summary_comment,  # noqa: F401  (re-exported for tests / downstream)
    create_branch,
    create_pr_comment,
    get_branch_sha,
    get_open_pull_requests,
)
from app.models import (
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.task_capabilities import WRITE_TASK_TYPES
from app.plan_completion import (
    # Re-exports — task_completion is the canonical completion-API import surface
    # for downstream modules (task_lifecycle, research_completion, tests). The
    # implementations live in plan_completion.py, but a 2026-05-03 ruff --fix
    # quietly removed these as "unused" within task_completion.py and silently
    # broke every downstream importer. noqa pins them so a future --fix sweep
    # can't repeat the regression.
    _handle_plan_completion,  # noqa: F401
    _inject_file_overlap_deps,  # noqa: F401
    _log_file_overlaps,  # noqa: F401
    _parse_plan_output,  # noqa: F401
    _validate_plan_dependencies,  # noqa: F401
)
from app.routers.agent import get_github_token_for_task
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.payload_schemas import validate_internal_payload
from app.broadcast import broadcast_to_project
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
from app.time_utils import _utcnow
from app.task_locking import with_design_doc_locked, with_directions_locked
from app.vm_cost_rates import get_vm_rate_per_second

logger = logging.getLogger(__name__)

# Shared marker used by both the TypeScript agent handler (review-fix.ts
# buildBlueprintPrFooter) and this Python helper so either side can update
# the same block without leaving duplicates.
BLUEPRINT_PR_META_MARKER = "<!-- blueprint:meta -->"


async def _persist_direction_field_update(
    session: AsyncSession,
    project: Project,
    direction: Dict[str, Any],
    *,
    deleted_keys: frozenset = frozenset(),
) -> None:
    """Serialize one mutated direction dict back into ``project.directions``.

    The caller has already applied every field mutation to ``direction`` — an
    element of a directions list that was loaded *outside* any lock. Writing the
    whole stale list back (the legacy ``project.directions = directions``
    pattern) loses a concurrent same-project completion's write to a *different*
    direction. This helper instead re-acquires the project row lock via
    ``with_directions_locked``, re-reads the directions list fresh under that
    lock, and merges only the field delta onto the matching entry — every other
    direction, and every field not touched by the caller, is left exactly as the
    fresh read found it.

    ``deleted_keys`` must enumerate any keys that the caller explicitly removed
    from ``direction`` (via ``pop``). Because we no longer clear the fresh entry
    before merging, those keys would otherwise survive in the DB from a previous
    write. Callers that only add/update fields may omit this parameter.

    ``session.expire`` is required because the app runs ``expire_on_commit=False``:
    without it, ``with_directions_locked``'s ``SELECT ... FOR UPDATE`` returns the
    already-cached (stale) Project instance rather than the freshly-committed row
    data, defeating the lock. Expiring the attribute forces a fresh load inside
    the lock.
    """
    dir_id = direction.get("id")
    session.expire(project, ["directions"])

    def _apply(fresh_dirs: List[Dict[str, Any]]):
        target = next((d for d in fresh_dirs if d.get("id") == dir_id), None)
        if target is not None:
            target.update(direction)
            for k in deleted_keys:
                target.pop(k, None)
        return None

    await with_directions_locked(session, project.id, _apply)


def _estimated_task_cost_usd(task: Task) -> Optional[float]:
    """Return the VM cost for a task, or None when timing data is incomplete."""
    if not task.vm_provisioned_at or not task.vm_stopped_at:
        return None
    rate = get_vm_rate_per_second(spot=task.vm_spot)
    return (task.vm_stopped_at - task.vm_provisioned_at).total_seconds() * rate


def build_blueprint_pr_footer(
    task: Task,
    *,
    iteration_count: Optional[int] = None,
    direction_title: Optional[str] = None,
) -> str:
    """Build a badge-style footer string for a Blueprint PR body.

    Omits any field whose data is absent so callers need not pre-filter.
    Uses BLUEPRINT_PR_META_MARKER so the block can be replaced idempotently.
    """
    ctx = task.context or {}
    parts: list[str] = []
    review_loop = ctx.get("review_loop") or {}
    if iteration_count is None and isinstance(review_loop.get("iteration"), int):
        iteration_count = int(review_loop["iteration"])
    if not direction_title and isinstance(ctx.get("direction_title"), str):
        direction_title = ctx.get("direction_title")

    cost = _estimated_task_cost_usd(task)
    if cost is not None:
        parts.append(f"${cost:.2f}")

    if iteration_count and iteration_count > 1:
        parts.append(f"{iteration_count} iterations")

    direction_id = ctx.get("direction_id")
    if direction_id:
        label = (
            f"{direction_title[:60]} ({direction_id})"
            if direction_title
            else str(direction_id)
        )
        parts.append(f"direction: {label}")

    if not parts:
        return ""
    return f"{BLUEPRINT_PR_META_MARKER}\n*{' · '.join(parts)}*"


async def patch_pr_body_with_blueprint_meta(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    footer: str,
) -> None:
    """Best-effort: append or replace the blueprint:meta footer in a GitHub PR body."""
    import httpx  # noqa: PLC0415

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        if not resp.is_success:
            return
        current = resp.json().get("body") or ""
        idx = current.find(BLUEPRINT_PR_META_MARKER)
        updated = (current[:idx] + footer) if idx >= 0 else f"{current}\n\n{footer}"
        await client.patch(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={**headers, "Content-Type": "application/json"},
            json={"body": updated},
        )


def _compute_first_attempt_succeeded(attempt_count: int, status: "TaskStatus") -> bool:
    """Return True if the task completed successfully on its first attempt (attempt_count <= 1)."""
    return attempt_count <= 1 and status == TaskStatus.COMPLETED


def _stamp_orphan_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    direction_id: str,
) -> None:
    """Record an orphan remediation outcome on the task itself when the linked
    direction no longer exists. Writes to ``task.context['orphan_remediation_outcome']``
    so diagnosis/operator tooling can still locate the result."""
    ctx = dict(task.context or {})
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
    outcome: Dict[str, Any] = {
        "result": "merged" if merged else "failed",
        "timestamp": _utcnow().isoformat(),
        "missing_direction_id": direction_id,
    }
    if fingerprint:
        outcome["fingerprint"] = str(fingerprint)
    if not merged:
        from app.failure_classification import classify_failure  # noqa: PLC0415

        outcome["failure_class"] = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )
        outcome["error_summary"] = (task.latest_status_message or "")[:200]
    ctx["orphan_remediation_outcome"] = outcome
    task.context = ctx
    flag_modified(task, "context")


def _stamp_remediation_spawn_on_direction(
    direction: Dict[str, Any], task: Task
) -> None:
    """Record that a remediation task is in flight without declaring its outcome."""
    ctx = task.context or {}
    direction["last_remediation_at"] = _utcnow().isoformat()
    direction["last_remediation_task_id"] = str(task.id)
    direction["remediation_task_id"] = str(task.id)
    diagnosis_task_id = ctx.get("diagnosis_task_id")
    if diagnosis_task_id:
        direction["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
    if fingerprint:
        direction["last_remediation_fingerprint"] = str(fingerprint)


# Cap on the per-direction remediation-outcome history list length.
_REMEDIATION_HISTORY_MAX = 5


async def _record_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    session: AsyncSession,
) -> bool:
    """Record remediation outcome on the direction the remediation targeted.

    A remediation task is spawned by the autonomous diagnosis pipeline with
    `diagnosis_task_id` in its context. When such a task reaches a terminal
    outcome (merged or failed), stamp the linked direction so effectiveness
    monitoring and escalation logic (direction_lifecycle.py, anomaly_detection.py)
    can observe attempts, last result, timestamp, and the diagnosis fingerprint.

    Returns True if direction state was updated, False otherwise.
    """
    ctx = task.context or {}
    diagnosis_task_id = ctx.get("diagnosis_task_id")
    direction_id = ctx.get("direction_id")
    # diagnosis_task_id is optional — CI auto-remediation fixes may target a
    # direction (by fingerprint match) without a prior investigation linkage.
    # direction_id alone is sufficient to stamp the outcome.
    if not direction_id:
        return False
    # Serialize the read-modify-write of project.directions through the
    # canonical row lock: the mutate_fn re-locates the target direction inside
    # the lock against the freshly-loaded list and applies all mutations there,
    # so a concurrent same-project completion mutating a *different* direction is
    # not clobbered. Sibling propagation also runs inside the lock against the
    # fresh list (``find_directions_sharing_fingerprint`` only reads
    # ``project.directions``, so a lightweight shim suffices).
    recorded = {"v": False}

    def _mutate(directions: List[Dict[str, Any]]):
        target = next((d for d in directions if d.get("id") == direction_id), None)
        if target is None:
            # Direction was retired or removed before the remediation task
            # reached a terminal state. Stamp the outcome on the task itself so
            # the fix outcome is not silently discarded — operators and the
            # diagnosis pipeline can still find what happened via task.context.
            _stamp_orphan_remediation_outcome(
                task,
                merged=merged,
                direction_id=str(direction_id),
            )
            return None
        proj_shim = SimpleNamespace(directions=directions)
        fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
        prior_fingerprint = target.get("last_remediation_fingerprint")
        prior_attempts = int(target.get("remediation_attempts") or 0)
        if (
            fingerprint
            and prior_fingerprint
            and str(fingerprint) != str(prior_fingerprint)
        ):
            target["remediation_attempts"] = 1
        else:
            target["remediation_attempts"] = prior_attempts + 1
        now_iso = _utcnow().isoformat()
        target["last_remediation_result"] = "merged" if merged else "failed"
        target["last_remediation_at"] = now_iso
        if diagnosis_task_id:
            target["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
        target["last_remediation_task_id"] = str(task.id)
        if fingerprint:
            target["last_remediation_fingerprint"] = str(fingerprint)
        history_entry: Dict[str, Any] = {
            "task_id": str(task.id),
            "result": "merged" if merged else "failed",
            "timestamp": now_iso,
            "prompt_snippet": ((task.first_payload or {}).get("prompt", "") or "")[
                :200
            ],
        }
        if fingerprint:
            history_entry["fingerprint"] = str(fingerprint)
        trigger_failure_class = ctx.get("trigger_failure_class")
        if trigger_failure_class:
            history_entry["trigger_failure_class"] = trigger_failure_class
        ci_failure_type = ctx.get("ci_failure_type")
        if ci_failure_type:
            history_entry["ci_failure_type"] = ci_failure_type
        if merged:
            target.pop("last_remediation_failure_class", None)
            target.pop("last_remediation_error_summary", None)
            if fingerprint:
                try:
                    _propagate_remediation_success_to_siblings(
                        project=proj_shim,
                        directions=directions,
                        fingerprint=str(fingerprint),
                        successful_direction_id=str(direction_id),
                        task=task,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[DIAGNOSIS] Failed to propagate remediation success to "
                        f"siblings for direction {direction_id}: {exc!r}"
                    )
        else:
            from app.failure_classification import classify_failure  # noqa: PLC0415

            target["last_remediation_failure_class"] = classify_failure(
                task, at_time=task.last_heartbeat or task.started_at
            )
            target["last_remediation_error_summary"] = (
                task.latest_status_message or ""
            )[:200]
            history_entry["failure_class"] = target["last_remediation_failure_class"]
            history_entry["error_summary"] = target["last_remediation_error_summary"]
            if fingerprint:
                try:
                    _propagate_remediation_failure_to_siblings(
                        project=proj_shim,
                        directions=directions,
                        fingerprint=str(fingerprint),
                        failed_direction_id=str(direction_id),
                        task=task,
                        failure_class=target["last_remediation_failure_class"],
                        error_summary=target["last_remediation_error_summary"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[DIAGNOSIS] Failed to propagate remediation failure to "
                        f"siblings for direction {direction_id}: {exc!r}"
                    )
        history_list = list(target.get("remediation_history") or [])
        history_list.append(history_entry)
        target["remediation_history"] = history_list[-_REMEDIATION_HISTORY_MAX:]
        recorded["v"] = True
        return None

    # expire_on_commit=False leaves a stale directions snapshot in the identity
    # map; drop it so with_directions_locked re-reads the row fresh under the
    # lock (see _persist_direction_field_update docstring).
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["directions"])
    await with_directions_locked(session, task.project_id, _mutate)
    return recorded["v"]


def _propagate_remediation_success_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    successful_direction_id: Optional[str],
    task: Task,
) -> None:
    """Un-pause sibling directions sharing ``fingerprint`` and stamp prior-art context.

    When a remediation merges, other auto-paused directions holding on the same
    exact error fingerprint almost certainly share the root cause that was just
    fixed. Rather than making each one wait out its own 48h cooldown and run an
    independent diagnosis, clear their pause state, stamp the successful task as
    prior art (so the diagnosis pipeline can reference what worked if they fail
    again), and append an audit note. The match relies on
    ``find_directions_sharing_fingerprint`` which enforces exact equality plus
    auto-paused + recency filters, guarding against false positives.

    Mutates ``directions`` in place; the caller persists via ``flag_modified``.
    """
    from app.anomaly_detection import find_directions_sharing_fingerprint  # noqa: PLC0415

    siblings = find_directions_sharing_fingerprint(
        project,
        fingerprint,
        exclude_direction_id=successful_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    if successful_direction_id:
        note_text = (
            f"Sibling remediation merged ({now_iso[:10]}) for direction "
            f"{successful_direction_id} sharing the same error fingerprint. "
            f"Task: {task.id}."
        )
    else:
        note_text = (
            f"Unlinked CI remediation merged ({now_iso[:10]}) sharing the "
            f"same error fingerprint. Task: {task.id}."
        )
    if prompt_snippet:
        note_text += f" Approach: {prompt_snippet}"
    note_text = note_text[:STRATEGIC_RATIONALE_MAX]
    sibling_ids = {s.get("id") for s in siblings if s.get("id")}
    diagnosis_task_id = (task.context or {}).get("diagnosis_task_id")
    for direction in directions:
        if direction.get("id") not in sibling_ids:
            continue
        # Clear the auto-pause so the direction can resume on the next lifecycle
        # sweep without waiting out its own cooldown.
        direction["status"] = "active"
        for field in (
            "auto_paused",
            "paused_reason",
            "paused_at",
            "needs_manual_intervention",
            "escalation_notification_pending",
        ):
            direction.pop(field, None)
        # Stamp prior art so autonomous_research_diagnosis can surface what
        # worked if the same fingerprint resurfaces on this direction.
        direction["last_remediation_task_id"] = str(task.id)
        if diagnosis_task_id:
            direction["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
        direction["last_remediation_fingerprint"] = str(fingerprint)
        direction["last_remediation_result"] = "merged"
        direction["last_remediation_at"] = now_iso
        if successful_direction_id:
            direction["cross_direction_remediation_source"] = str(
                successful_direction_id
            )
        notes = list(direction.get("notes") or [])
        notes.append(
            {
                "text": note_text,
                "added_at": now_iso,
                "added_by_task": str(task.id),
                "source": "sibling_remediation_success",
            }
        )
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


def _propagate_remediation_failure_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    failed_direction_id: Optional[str],
    task: Task,
    failure_class: Optional[str],
    error_summary: Optional[str],
) -> None:
    """Inject a 'do not repeat' note into sibling directions sharing ``fingerprint``.

    When a remediation fails, other directions holding on the same error
    fingerprint should not re-attempt the same approach. Surface the failed
    prompt, failure class, and error summary so sibling investigations can
    propose a different fix.

    Mutates ``directions`` in place; the caller persists via ``flag_modified``.
    """
    from app.anomaly_detection import find_directions_sharing_fingerprint  # noqa: PLC0415

    siblings = find_directions_sharing_fingerprint(
        project,
        fingerprint,
        exclude_direction_id=failed_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    if failed_direction_id:
        note_text = (
            f"Sibling remediation FAILED ({now_iso[:10]}) for direction "
            f"{failed_direction_id} sharing the same error fingerprint. "
            f"Task: {task.id}. Do not repeat this approach."
        )
    else:
        note_text = (
            f"Unlinked CI remediation FAILED ({now_iso[:10]}) sharing the "
            f"same error fingerprint. Task: {task.id}. Do not repeat this approach."
        )
    if failure_class:
        note_text += f" Failure: {failure_class}."
    if error_summary:
        note_text += f" Error: {error_summary}"
    if prompt_snippet:
        note_text += f" Failed approach: {prompt_snippet}"
    note_text = note_text[:STRATEGIC_RATIONALE_MAX]
    sibling_ids = {s.get("id") for s in siblings if s.get("id")}
    for direction in directions:
        if direction.get("id") not in sibling_ids:
            continue
        notes = list(direction.get("notes") or [])
        notes.append(
            {
                "text": note_text,
                "added_at": now_iso,
                "added_by_task": str(task.id),
                "source": "sibling_remediation_failure",
            }
        )
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


_PROJECT_REMEDIATION_HISTORY_MAX = 20


async def _record_project_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    session: AsyncSession,
) -> bool:
    """Record a CI auto-remediation outcome at the project level when no
    direction_id is linked, so unlinked-fix effectiveness is still trackable
    via Project.remediation_tracking."""
    project = await session.get(Project, task.project_id)
    if not project:
        return False
    ctx = task.context or {}
    # Audit W1: previous_error_fingerprint is now a column.
    fingerprint = (
        ctx.get("diagnosis_fingerprint")
        or ctx.get("error_fingerprint")
        or task.previous_error_fingerprint
    )
    state = dict(project.remediation_tracking or {})
    now_iso = _utcnow().isoformat()
    entry: Dict[str, Any] = {
        "task_id": str(task.id),
        "result": "merged" if merged else "failed",
        "timestamp": now_iso,
    }
    if fingerprint:
        entry["fingerprint"] = str(fingerprint)
    failure_class: Optional[str] = None
    error_summary: Optional[str] = None
    if not merged:
        from app.failure_classification import classify_failure  # noqa: PLC0415

        failure_class = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )
        error_summary = (task.latest_status_message or "")[:200]
        entry["failure_class"] = failure_class
        entry["error_summary"] = error_summary
    history = list(state.get("history") or [])
    history.append(entry)
    state["history"] = history[-_PROJECT_REMEDIATION_HISTORY_MAX:]
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_result"] = entry["result"]
    state["last_at"] = now_iso
    if fingerprint:
        state["last_fingerprint"] = str(fingerprint)
    project.remediation_tracking = state
    flag_modified(project, "remediation_tracking")

    # Fingerprint-based sibling propagation: although this remediation is
    # unlinked (no direction_id), other auto-paused directions may be holding
    # on the same exact error fingerprint. Apply the same sibling notes /
    # un-pause logic as the linked path so unlinked CI fixes don't leave
    # siblings stuck.
    if fingerprint:
        # Serialize the sibling-directions write through the canonical row lock
        # so a concurrent same-project completion mutating a different direction
        # is not clobbered. Drop the cached directions snapshot first
        # (expire_on_commit=False) so the helper reads the row fresh under the
        # lock; the remediation_tracking write above is left pending and flushed
        # within the same transaction. remediation_tracking itself has no
        # canonical helper and is left as a plain write (out of scope here).
        session.expire(project, ["directions"])

        def _propagate(directions: List[Dict[str, Any]]):
            proj_shim = SimpleNamespace(directions=directions)
            if merged:
                _propagate_remediation_success_to_siblings(
                    project=proj_shim,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    successful_direction_id=None,
                    task=task,
                )
            else:
                _propagate_remediation_failure_to_siblings(
                    project=proj_shim,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    failed_direction_id=None,
                    task=task,
                    failure_class=failure_class,
                    error_summary=error_summary,
                )
            return None

        try:
            await with_directions_locked(session, task.project_id, _propagate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[DIAGNOSIS] Failed to propagate unlinked remediation outcome "
                f"to siblings for project {project.id}: {exc!r}"
            )
    session.add(project)
    return True


async def _release_pool_member_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> Optional[str]:
    """Return or retire the warm-pool member that served a now-terminal task.

    A leased :class:`~app.models.PoolVm` row stays ``LEASED`` — counting against
    the per-project pool target and the fleet budget (blocking replenishment) —
    until the reaper's orphan-lease sweep eventually reclaims and DELETEs it,
    making every member single-use. Reconcile the registry promptly instead,
    matching what the pool agent does next:

      * ``COMPLETED`` — the agent loops back to ``/next-role``
        (``bootstrapPoolMode`` -> ``pollForNextRole``), so return the member to
        IDLE via :func:`~app.warm_pool.release_pool_vm` (a fresh idle window
        starts; the idle-TTL reaper / stale-image recycling / cost guard still
        decide when it is ultimately retired).
      * ``FAILED`` / ``CANCELLED`` — the agent re-throws and exits without
        polling, so it cannot be reused: mark it ``RETIRING`` via
        :func:`~app.warm_pool.mark_pool_vm_retiring`.

    No-op (returns ``None``) for non-terminal states, when the pool is disabled,
    or when no LEASED member is bound to this task (the common cold-provisioned
    case + idempotency guard). Returns the outcome label when it acted; the
    caller owns the commit.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return None
    # Deferred imports: keep the module-level surface stable and avoid importing
    # the warm-pool registry on the hot completion path when the pool is dark.
    from app.models import PoolVmState  # noqa: PLC0415
    from app.warm_pool import (  # noqa: PLC0415
        get_pool_vm,
        is_warm_pool_enabled,
        mark_pool_vm_retiring,
        release_pool_vm,
    )

    if not is_warm_pool_enabled():
        return None
    vm_id = task.vm_instance_id
    if not vm_id:
        return None
    member = await get_pool_vm(session, vm_id)
    if member is None or member.state != PoolVmState.LEASED:
        # Not a pool member, or already reconciled / retiring — nothing to do.
        return None
    # Only reclaim the member actually bound to THIS task: a vm_instance_id
    # reused across tasks must not let one task's terminal state pull back a
    # member leased to a different task.
    if member.leased_task_id is not None and member.leased_task_id != task.id:
        return None
    if status == TaskStatus.COMPLETED:
        await release_pool_vm(session, vm_id)
        outcome = "idle"
    else:
        await mark_pool_vm_retiring(session, vm_id)
        outcome = "retiring"
    logger.info(
        "[WARM-POOL] task %s terminal (%s) — pool member %s -> %s",
        task.id,
        getattr(status, "value", status),
        vm_id,
        outcome,
    )
    return outcome


async def _record_remediation_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Stamp remediation outcome on the linked direction when a diagnosis-spawned task terminates.

    Records the merged/failed outcome on the targeted direction (and falls back
    to project-level tracking when no direction linkage survives) so the
    direction-result ladder sees terminal remediation outcomes.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
    # Reconcile the warm-pool member that served this task BEFORE the
    # remediation-specific early returns below, so every terminal task — not
    # just diagnosis-linked ones — promptly returns/retires its pool member
    # instead of stranding it LEASED against the pool budget. Best-effort: a
    # failure here must not block remediation recording (the orphan-lease reaper
    # remains the backstop).
    try:
        await _release_pool_member_if_terminal(task, status, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[WARM-POOL] Failed to reconcile pool member for terminal task {task.id}: {exc!r}"
        )
    ctx = task.context or {}
    has_direction_linkage = bool(
        ctx.get("diagnosis_task_id") and ctx.get("direction_id")
    )
    # Audit W1: auto_remediation is now a column.
    is_auto_remediation = bool(getattr(task, "auto_remediation", False))
    if not has_direction_linkage and not is_auto_remediation:
        return
    # Tasks that produce a PR enter the review pipeline — their true outcome
    # (merged vs. rejected) is only known after the review task completes or the
    # pipeline exhausts its iteration cap. Recording on the PR-creating task's
    # COMPLETED would prematurely stamp merged=True before review/CI has run.
    # FAILED/CANCELLED still record immediately since no viable PR exists.
    # Every write task type opens a PR and thus enters the review pipeline; the
    # `review` task itself also operates on that PR. Derive from the canonical
    # registry so a newly-added write type is honored automatically — `review` is
    # the only non-write member and is listed explicitly. See
    # app/task_capabilities.py:WRITE_TASK_TYPES.
    _REVIEW_PIPELINE_TYPES = WRITE_TASK_TYPES | {"review"}
    if status == TaskStatus.COMPLETED:
        task_type = (task.first_payload or {}).get("type")
        if task_type in _REVIEW_PIPELINE_TYPES:
            return
    try:
        recorded = await _record_remediation_outcome(
            task,
            merged=(status == TaskStatus.COMPLETED),
            session=session,
        )
        if not recorded:
            # Fall back to project-level tracking whenever the direction lookup
            # failed (either no direction_id at all on an auto_remediation, or
            # the linked direction was retired before terminal state). This
            # guarantees remediation outcomes are never silently discarded.
            had_direction_linkage = bool(ctx.get("direction_id"))
            if is_auto_remediation or had_direction_linkage:
                await _record_project_remediation_outcome(
                    task,
                    merged=(status == TaskStatus.COMPLETED),
                    session=session,
                )
    except Exception as exc:
        logger.warning(
            f"[DIAGNOSIS] Failed to record remediation outcome for task {task.id}: {exc!r}"
        )


# ---------------------------------------------------------------------------
# Parser / Validator helpers
# ---------------------------------------------------------------------------


def _extract_json_array(text: str, *required_keys: str) -> Optional[str]:
    """Find the first JSON array in text containing objects with the required keys.

    Uses bracket-depth counting (O(n)) instead of regex with greedy groups,
    which can cause O(n*k) backtracking on malformed input and block the
    async event loop for seconds.
    """
    # Cap input to prevent scanning megabytes of raw output
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    # Find outermost [ ... ] containing the required keys
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        return candidate
                    break  # This bracket pair didn't match, try next [
        start = text.find("[", start + 1)
    return None


# Round 4 W1 (audit C3): content snapshot used by ``_ensure_branch`` for
# lock-window-break validation. Heartbeat-driven ``updated_at`` advances
# every 30s and would false-defer under load. Validate against the fields
# the branch-creation window protects.
def _capture_branch_creation_snapshot(task: Task) -> dict:
    """Snapshot the fields the branch-creation lock-window-break protects.

    The window between commit-release and re-acquire is meant to detect
    peer mutation of the post-plan / post-research status flow. The
    relevant fields are:
      - status: must remain EVALUATING (the value the caller advanced to
        before calling _ensure_branch); a peer transition would invalidate
        the branch-creation outcome
      - context.plan_branch / context.research_branch: a peer may have
        already created the staging branch; we must not overwrite
      - auto_remediation: snapshotted at line 1615 and read on the
        failure path at line 1710 (record_task_result). If a peer flips
        this between snapshot and re-acquire, the failure record would
        carry a stale flag (R5 audit MEDIUM-C).
      - requires_approval: read post-window in places that consume the
        relocked task; included so peer flips trip the validator.
    Heartbeat-only updates don't touch any of these.
    """
    ctx = task.context or {}
    return {
        "status": task.status,
        "plan_branch": ctx.get("plan_branch"),
        "research_branch": ctx.get("research_branch"),
        "auto_remediation": task.auto_remediation,
        "requires_approval": task.requires_approval,
    }


async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str, label: str, session: AsyncSession
) -> Task | None:
    """Create a branch, or verify it already exists.

    Returns:
        - The freshly-locked Task on success (caller continues mutating
          under the new lock).
        - None on failure (task set to FAILED) OR when a peer mutated the
          row during the unlocked window and we deferred. In both cases
          the caller should ``return`` without further mutation.

    Round 3 H5 (lock-window break): callers (_handle_plan_completion,
    _handle_research_completion) run inside _process_post_commit while
    the task is FOR UPDATE-locked. The retry loop below sleeps 10-40s
    (with jitter, up to ~150s combined across 5 attempts) and makes
    GitHub API calls that can hang for tens of seconds during rate-limit.
    Holding the task lock across that window starves concurrent
    heartbeats and false-orphans the row. We mirror the
    rollup_management.py ``_release_parent_lock`` / ``_relock_parent``
    pattern: capture a content snapshot, commit-release before the loop,
    run GitHub calls + sleeps without the lock, then re-acquire with
    snapshot validation.

    Round 4 W1 (audit C1, C2, C3):
      - C1 (cache): ``populate_existing=True`` on the re-SELECT bypasses
        the identity-map cache that previously made the staleness
        validation a no-op under ``expire_on_commit=False``.
      - C2 (success path): the prior implementation returned True on
        success WITHOUT re-acquiring the lock. Callers
        (_handle_plan_completion, _handle_research_completion) then
        mutated ``task.context`` and committed *as if* locked — but
        they were not. Fix: success path now also re-acquires FOR UPDATE
        and returns the fresh Task. Caller swaps its local ``task``
        binding to the returned instance and proceeds under the new
        lock. If re-acquire fails (peer holds), return None and let the
        caller defer (the branch was created, but we couldn't re-lock —
        the next sweep observes the branch artifact and proceeds).
      - C3 (heartbeat): the prior ``updated_at`` validation tripped on
        every overlapping heartbeat. Fix: validate against
        ``_capture_branch_creation_snapshot`` — content fields only.
    """
    # Deferred import to avoid circular dependency
    from app.alert_dispatch import _maybe_create_failure_alert  # noqa: PLC0415

    # Round 4 W1: capture snapshot BEFORE the release-commit so the
    # snapshot reflects the pre-commit content, not the post-commit
    # ``updated_at`` (which the prior implementation needed but the new
    # content-based validator does not). ``parent_id_local`` mirrors the
    # snapshot-stale convention: between release and re-acquire, the
    # in-memory ``task`` is detached and only stable ids + the captured
    # snapshot are safe to read.
    parent_id_local = task.id
    parent_task_type = task.first_payload.get("type", "")
    parent_auto_remediation = task.auto_remediation
    parent_context_snapshot = dict(task.context or {})
    pre_branch_snapshot = _capture_branch_creation_snapshot(task)
    session.add(task)
    await session.commit()  # release task FOR UPDATE lock; SQLA returns
    # the connection to the pool (audit H6).

    max_retries = 5
    gh_token = owner = repo = None
    last_err = None
    branch_created = False
    for _attempt in range(max_retries):
        try:
            gh_token, owner, repo = await get_github_token_for_task(
                parent_id_local, session, force_refresh=(_attempt > 0)
            )
            sha = await get_branch_sha(gh_token, owner, repo, source_branch)
            await create_branch(gh_token, owner, repo, branch_name, sha)
            logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
            branch_created = True
            break
        except Exception as e:
            last_err = e
            if gh_token:
                try:
                    await get_branch_sha(gh_token, owner, repo, branch_name)
                    logger.info(
                        f"[{label}] Branch {branch_name} already exists — reusing"
                    )
                    branch_created = True
                    break
                except HTTPException as verify_exc:
                    if verify_exc.status_code == 404:
                        # Branch genuinely doesn't exist — count against attempt budget
                        pass
                    else:
                        # Transient server error during verification (e.g. 502) — don't
                        # conclude the branch is missing; continue retrying.
                        logger.warning(
                            f"[{label}] Transient error verifying branch {branch_name} ({verify_exc.status_code}), will retry"
                        )
                except Exception:
                    logger.exception(
                        f"[{label}] Non-HTTP error verifying branch {branch_name}"
                    )
            if _attempt < max_retries - 1:
                backoff = (
                    10 * (2**_attempt) * (0.75 + 0.5 * random.random())
                )  # jitter: 10-20s, 20-40s, …
                logger.warning(
                    f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {backoff:.1f}s: {e!r}"
                )
                await asyncio.sleep(backoff)
            else:
                if gh_token:
                    logger.exception(
                        f"[{label}] Branch {branch_name} creation failed after {max_retries} attempts"
                    )
                else:
                    logger.exception(
                        f"[{label}] Cannot create branch — GitHub token unavailable"
                    )

    # Round 4 W1 (C1, C2, C3): re-acquire with snapshot validation on
    # BOTH the success path (C2 — caller will mutate task.context) AND
    # the failure path (mark FAILED). Content snapshot replaces
    # updated_at. To bypass the identity-map cache, ``session.expire``
    # the in-memory instance AND set ``populate_existing=True`` on the
    # SELECT (the canonical reference is now
    # ``rollup_management._relock_parent``, ~line 155 — populate_existing
    # alone is unreliable with ``with_for_update(skip_locked=True)`` on
    # async sessions).
    #
    # Incident 2026-05-04 (b9456e05 / PRs #3606/3607/3612/3613): a
    # one-shot ``skip_locked=True`` silently stranded the parent when
    # the row happened to be locked at the wrong instant — the prior
    # comment claimed "next sweep observes the branch artifact and
    # proceeds" but no such sweep exists. Bounded retry (~9s) makes
    # now-this-instant contention recoverable; on exhaustion we raise
    # so the caller's exception path can persist post_commit_pending
    # for the existing sweep to retry, instead of silently returning
    # None.
    fresh = None
    _max_relock_attempts = 6
    for _relock_attempt in range(_max_relock_attempts):
        session.expire(task)
        fresh_stmt = (
            select(Task)
            .where(Task.id == parent_id_local)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        fresh = (await session.execute(fresh_stmt)).scalars().first()
        if fresh is not None:
            break
        if _relock_attempt < _max_relock_attempts - 1:
            # 1.0, 1.4, 1.8, 2.2, 2.6 → ~9s total before final attempt
            await asyncio.sleep(1.0 + _relock_attempt * 0.4)
    _defer_kind = "post-success context" if branch_created else "failure"
    if fresh is None:
        # All retries exhausted. Word "timeout" in the message routes
        # the caller's transient-error heuristic at task_lifecycle.py
        # (~line 2710) to requeue the task instead of failing it
        # permanently. A fresh agent run regenerates the plan output;
        # post-commit then re-runs and the relock typically succeeds
        # once the contending peer is gone.
        raise RuntimeError(
            f"[{label}] Task {parent_id_local} relock timeout after "
            f"{_max_relock_attempts} retries (~9s) — could not "
            f"persist {_defer_kind} mutation. Branch {branch_name} "
            f"{'was created on GitHub' if branch_created else 'creation also failed'}."
        )
    fresh_snapshot = _capture_branch_creation_snapshot(fresh)
    if fresh_snapshot != pre_branch_snapshot:
        # Genuine peer-content mutation — a different writer modified
        # status/payloads/context during our unlock window. Re-running
        # the agent would just race again. Raise with a non-transient
        # message so the caller marks the task FAILED for operator
        # review rather than silently stranding it.
        raise RuntimeError(
            f"[{label}] Task {parent_id_local} mutated during branch-creation "
            f"window (expected snapshot={pre_branch_snapshot}, found "
            f"{fresh_snapshot}) — could not persist {_defer_kind} mutation. "
            f"Branch {branch_name} "
            f"{'was created on GitHub' if branch_created else 'creation also failed'}. "
            f"Manual investigation required: identify the peer writer."
        )

    if branch_created:
        # C2: success — return the relocked task so caller mutates under
        # the new lock. Caller MUST swap its ``task`` reference to the
        # returned instance.
        return fresh

    # Failure path
    e = last_err
    task = fresh
    task.status = TaskStatus.FAILED
    task.latest_status_message = (
        f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    )
    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415

    _fc = classify_failure(task, at_time=task.last_heartbeat or task.started_at)
    persist_failure_category(task, _fc)
    record_task_result(
        success=False,
        failure_class=_fc,
        task_id=str(task.id),
        task_type=parent_task_type,
        task_context=parent_context_snapshot,
        auto_remediation=parent_auto_remediation,
    )
    session.add(task)
    await _maybe_create_failure_alert(
        task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200]
    )
    await session.commit()
    return None


# ---------------------------------------------------------------------------
# Completion handlers
# ---------------------------------------------------------------------------


async def _maybe_release_safeguard_gate(
    safeguard_task: Task, findings: list | None, session: AsyncSession
) -> None:
    """Release or maintain the cross-cutting merge gate when a safeguard audit terminates.

    No-ops immediately if the task is not a safeguard (no safeguard_for_task_id in context).
    Releases the gate (clears orig.requires_approval) when no critical/high findings exist.
    Called on every TERMINAL safeguard path — COMPLETED (with parsed findings) AND
    FAILED/reaped (findings=None) — so the gate cannot be permanently stranded (cluster C5).

    Re-arm behaviour after clearing the flag:
      - orig EVALUATING (review approved while safeguard was pending): set
        review_loop.ci_waiting=True so the orchestrator's CI loop attempts the merge.
      - orig already COMPLETED (the safeguard outlived orig's lifecycle): clearing
        the flag alone leaves the open PR stranded forever — nothing re-attempts the
        merge. Fire the merge directly here, guarded by an idempotency flag
        (_safeguard_merge_fired) so a re-run / concurrent path cannot double-merge.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    safeguard_for_id = (safeguard_task.context or {}).get("safeguard_for_task_id")
    if not safeguard_for_id:
        return
    has_blocker = bool(
        findings and any(f.get("severity") in ("critical", "high") for f in findings)
    )
    try:
        orig = await session.get(Task, uuid.UUID(safeguard_for_id))
    except Exception:
        orig = None
    if orig is None:
        logger.warning(
            f"[SAFEGUARD] orig {safeguard_for_id[:8]} not found — gate not updated"
        )
        return
    if has_blocker:
        logger.info(
            f"[SAFEGUARD] Gate maintained on {safeguard_for_id[:8]} — critical/high findings"
        )
        return
    orig.requires_approval = False
    pr_num = (safeguard_task.context or {}).get("safeguard_pr_number")
    orig_ctx = dict(orig.context or {})
    _fire_completed_orig_merge = False
    if orig.status == TaskStatus.EVALUATING and pr_num:
        # Review already approved while safeguard was pending; hand off to CI polling loop.
        orig_ctx["review_loop"] = {
            **(orig_ctx.get("review_loop") or {}),
            "ci_waiting": True,
            "pr_number": pr_num,
        }
        orig.context = orig_ctx
        flag_modified(orig, "context")
    elif (
        orig.status == TaskStatus.COMPLETED
        and pr_num
        and not orig_ctx.get("_safeguard_merge_fired")
    ):
        # orig already terminalized — clearing the flag re-arms nothing, so the
        # open PR would strand forever. Mark the idempotency flag now (persisted
        # with the gate-clear commit) and fire the merge AFTER the commit, below.
        orig_ctx["_safeguard_merge_fired"] = True
        orig.context = orig_ctx
        flag_modified(orig, "context")
        _fire_completed_orig_merge = True
    orig.latest_status_message = (
        f"Cross-cutting safeguard passed — merge gate released (PR #{pr_num})"
    )
    session.add(orig)
    await session.commit()
    logger.info(f"[SAFEGUARD] Gate released on {safeguard_for_id[:8]} for PR #{pr_num}")

    if _fire_completed_orig_merge:
        # Best-effort: fire the held merge for the already-COMPLETED orig. The
        # idempotency flag was committed above, so even if this raises the merge
        # is never re-attempted from here (avoids a double-merge loop); a manual
        # re-arm is then the operator's call. Merge only if the PR is still open.
        from app.github import (  # noqa: PLC0415
            get_pull_request,
            merge_pull_request,
        )

        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            _pr = await get_pull_request(gh_token, owner, repo, int(pr_num))
            if _pr.get("merged"):
                logger.info(
                    f"[SAFEGUARD] PR #{pr_num} already merged — no action (orig "
                    f"{safeguard_for_id[:8]})"
                )
            elif (_pr.get("state") or "open") != "open":
                logger.info(
                    f"[SAFEGUARD] PR #{pr_num} not open (state={_pr.get('state')}) "
                    f"— skipping post-gate merge (orig {safeguard_for_id[:8]})"
                )
            else:
                # Pre-merge Alembic dual-head guard (parity with the CI-completion
                # and inline task-completion merge paths). The held merge can target
                # the integration or promotion branch; without this check a PR that
                # added 'the next' migration while the safeguard ran could land a
                # duplicate revision / dual head. Only guard integration/promotion
                # targets; on a block-or-defer verdict skip the merge and leave the
                # PR open so the guarded retry/resolver flow can handle it.
                from app.shared_state import (  # noqa: PLC0415
                    get_project_promotion_target,
                )

                _sg_project = (
                    await session.get(Project, orig.project_id)
                    if orig.project_id
                    else None
                )
                _sg_base = (_pr.get("base") or {}).get("ref", "")
                _sg_gated = _sg_base in (
                    get_project_integration_branch(_sg_project),
                    get_project_promotion_target(_sg_project),
                )
                _sg_ok = True
                if _sg_gated:
                    from app.rollup_migration_validation import (  # noqa: PLC0415
                        enforce_premerge_migration_guard,
                    )

                    _sg_ok, _sg_reason = await enforce_premerge_migration_guard(
                        gh_token, owner, repo, int(pr_num), _sg_base
                    )
                if not _sg_ok:
                    logger.warning(
                        f"[SAFEGUARD] Post-gate merge of PR #{pr_num} held — "
                        f"Alembic dual-head guard: {_sg_reason} (orig "
                        f"{safeguard_for_id[:8]}) — leaving PR open for the "
                        f"guarded retry/resolver flow"
                    )
                else:
                    _res = await merge_pull_request(gh_token, owner, repo, int(pr_num))
                    if _res.get("merged"):
                        logger.info(
                            f"[SAFEGUARD] Fired held merge for COMPLETED orig "
                            f"{safeguard_for_id[:8]} PR #{pr_num} after gate release"
                        )
                    else:
                        logger.warning(
                            f"[SAFEGUARD] Post-gate merge of PR #{pr_num} (orig "
                            f"{safeguard_for_id[:8]}) did not merge: "
                            f"{_res.get('message', 'unknown')}"
                        )
        except Exception as _merge_exc:  # noqa: BLE001
            logger.warning(
                f"[SAFEGUARD] Post-gate merge attempt for PR #{pr_num} (orig "
                f"{safeguard_for_id[:8]}) failed: {_merge_exc}",
                exc_info=True,
            )


async def _handle_security_audit_completion(
    task: Task, logs: str, session: AsyncSession
):
    """Parse security audit findings and spawn create tasks for critical/high severity issues."""
    # Parse the JSON findings array from logs
    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = (
            "Security audit completed with no parseable findings"
        )
        task.plan_output = {"findings": []}
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, findings, session)
        return

    # Store all findings for display — wrapped in a dict so query_findings_index
    # (which only processes list plan_output) does not treat security audit
    # findings as research findings.
    task.plan_output = {"findings": findings}
    session.add(task)
    await session.commit()

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload).get(
        "base_branch", get_project_integration_branch(project)
    )

    # Spawn create tasks for critical and high severity findings
    actionable = [f for f in findings if f.get("severity") in ("critical", "high")]
    if len(actionable) > 10:
        actionable = actionable[:10]
    if not actionable:
        task.latest_status_message = f"Security audit found {len(findings)} finding(s), none critical/high — no tasks spawned"
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, findings, session)
        return

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Security fix #{i + 1}")
        severity = finding.get("severity", "high")

        new_task = Task(
            project_id=task.project_id,
            title=f"[{severity.upper()}] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[
                validate_internal_payload(
                    {
                        "type": "create",
                        "prompt": fix_prompt,
                        "base_branch": base_branch,
                        **({"prompt_suffix": create_suffix} if create_suffix else {}),
                    },
                    "task_completion:security_finding",
                )
            ],
            context={
                "security_audit_task_id": str(task.id),
                "finding_index": i,
                "severity": severity,
                "file_path": finding.get("file_path", ""),
                "line_number": finding.get("line_number", 0),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)
        history = TaskHistory(
            task_id=ct.id,
            action="SPAWNED",
            details={"spawned_by": "SECURITY_AUDIT", "parent_audit_id": str(task.id)},
        )
        session.add(history)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
        )

    await session.commit()

    # Post PR comment for medium/low findings that don't get auto-fixed
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    if informational:
        rows = "\n".join(
            "| {severity} | {title} | {loc} | {desc} |".format(
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                loc=(
                    f.get("file_path", "")
                    + (f":{f['line_number']}" if f.get("line_number") else "")
                ),
                desc=f.get("description", ""),
            )
            for f in informational
        )
        comment_body = (
            "## Security Audit Findings (Informational)\n\n"
            "The following medium/low severity findings were identified. "
            "They have not been auto-fixed and require manual review:\n\n"
            "| Severity | Title | File | Description |\n"
            "|----------|-------|------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(
                    f"[SECURITY AUDIT] Posted informational findings comment on PR #{pr_number}"
                )
        except Exception as e:
            logger.warning(
                f"[SECURITY AUDIT] Failed to post PR comment for informational findings: {e}",
                exc_info=True,
            )

    if child_tasks:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = f"{len(child_tasks)} critical/high fixed, {len(informational)} medium/low surfaced as PR comment"
    else:
        # No critical/high fix tasks spawned. Like the perf handler below, a
        # security_audit opens no PR of its own, so a childless EVALUATING task
        # has no lifecycle path to COMPLETED (no rollup trigger; reaper Step 4
        # skips childless parents) and would hang forever, pinning any parent.
        # Mark it terminal directly.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = f"Security audit complete: no critical/high findings; {len(informational)} medium/low surfaced as PR comment"
    session.add(task)
    await session.commit()

    await broadcast_to_project(
        str(task.project_id),
        "autonomous_activity",
        {"security_audit_task_id": str(task.id), "tasks_created": len(child_tasks)},
    )
    logger.info(
        f"[SECURITY AUDIT] Created {len(child_tasks)} fix tasks from audit {task.id}"
    )
    await _maybe_release_safeguard_gate(task, findings, session)


async def _handle_perf_completion(task: Task, logs: str, session: AsyncSession):
    """Parse perf findings and spawn a bundled create task for all high-severity issues.

    Bundles all high-severity findings into a single child task to eliminate the fan-out
    (which previously spawned one task per finding, each with its own VM, PR, and review).
    Medium/low findings are surfaced as an informational PR comment.
    """
    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = (
            "Perf analysis completed with no parseable findings"
        )
        task.plan_output = {"findings": []}
        session.add(task)
        await session.commit()
        return

    # Store all findings for display — wrapped in a dict so query_findings_index
    # (which only processes list plan_output) does not treat perf audit findings
    # as research findings.
    task.plan_output = {"findings": findings}
    session.add(task)
    await session.commit()

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload).get(
        "base_branch", get_project_integration_branch(project)
    )

    # Collect high-severity findings (capped at 10 as before)
    actionable = [f for f in findings if f.get("severity") == "high"]
    if len(actionable) > 10:
        actionable = actionable[:10]

    child_tasks = []

    # Bundle all high-severity findings into a single child task
    if actionable:
        # Enumerate findings in the prompt so the agent addresses them all
        findings_text = "\n".join(
            f"{i + 1}. [{finding.get('area', 'general')}] {finding.get('title', f'Finding {i + 1}')}\n"
            f"   Description: {finding.get('description', '')}\n"
            f"   Suggested approach: {finding.get('fix_prompt', '')}"
            for i, finding in enumerate(actionable)
        )

        bundled_prompt = (
            f"Performance audit identified {len(actionable)} high-severity findings:\n\n"
            f"{findings_text}\n\n"
            f"Address each finding independently in a single PR. You may skip individual "
            f"findings that prove risky or out of scope rather than failing the entire task. "
            f"Aim to fix as many as feasible without coupling unrelated changes."
        )

        new_task = Task(
            project_id=task.project_id,
            title=f"[PERF] Fix {len(actionable)} high-severity performance findings",
            description=f"Bundle of {len(actionable)} performance findings from audit",
            parent_task_id=task.id,
            payloads=[
                validate_internal_payload(
                    {
                        "type": "create",
                        "prompt": bundled_prompt,
                        "base_branch": base_branch,
                    },
                    "task_completion:perf_findings_bundle",
                )
            ],
            context={
                "perf_task_id": str(task.id),
                "finding_count": len(actionable),
                "severity": "high",
                "findings_areas": list(
                    set(f.get("area", "") for f in actionable if f.get("area"))
                ),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.flush()
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id,
            action="SPAWNED",
            details={"spawned_by": "PERF_AUDIT", "parent_perf_id": str(task.id)},
        )
        session.add(history)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
        )

    # Post a summary comment on open PRs for medium/low findings
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    all_surfaced = informational + [
        f for f in findings if f.get("severity") == "high" and f not in actionable
    ]
    if all_surfaced or findings:
        rows = "\n".join(
            "| {area} | {severity} | {title} | {desc} |".format(
                area=f.get("area", ""),
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                desc=f.get("description", "")[:120],
            )
            for f in findings
        )
        comment_body = (
            "## Performance Analysis Findings\n\n"
            f"Analyzed codebase for performance bottlenecks. "
            f"{len(child_tasks)} high-severity fix task(s) spawned automatically.\n\n"
            "| Area | Severity | Title | Description |\n"
            "|------|----------|-------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(
                    f"[PERF] Posted findings summary comment on PR #{pr_number}"
                )
        except Exception as e:
            logger.warning(f"[PERF] Failed to post PR comment: {e}", exc_info=True)

    if child_tasks:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = (
            f"1 high-severity fix task spawned for {len(actionable)} findings, "
            f"{len(informational)} medium/low finding(s) surfaced as PR comment"
        )
    else:
        # No high-severity fix tasks spawned. A perf task opens no PR of its
        # own, so with zero children there is no COMPLETED child to trigger
        # _check_and_trigger_rollup and no children for the reaper's EVALUATING
        # sweep to act on (reaper Step 4 ``if not children_snap: continue``).
        # An EVALUATING perf task here would hang forever — and pin its
        # research/strategic parent in EVALUATING too. Mark it terminal
        # directly, the same workaround the memo handler documents for its
        # childless, PR-less case.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            "Perf audit complete: no high-severity findings; "
            f"{len(informational)} medium/low finding(s) surfaced as PR comment"
        )
    session.add(task)
    await session.commit()

    await broadcast_to_project(
        str(task.project_id),
        "autonomous_activity",
        {"perf_task_id": str(task.id), "tasks_created": len(child_tasks)},
    )
    logger.info(
        f"[PERF] Created 1 bundled fix task from perf analysis {task.id} "
        f"(findings={len(actionable)})"
    )


# ---------------------------------------------------------------------------
# Memo completion — persist a terminal reasoning artifact, no PR, no children
# ---------------------------------------------------------------------------


_MEMO_REQUIRED_KEYS = ("summary", "reasoning", "citations", "tags")


def _parse_memo_output(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extract the structured memo object from agent ``logs``.

    The agent's contract is to emit a JSON object with the keys
    ``summary``, ``reasoning``, ``citations`` (list[str]), ``tags``
    (list[str]). We accept either a bare JSON object as the entire
    payload, or an object embedded in surrounding text.
    """
    if not text:
        return None
    snippet = text[:500_000]
    candidates: List[str] = []
    # Direct decode — common case where the agent passes a clean JSON
    # string up as the artifact body. Only accept the bare decode when
    # the required schema keys are present; otherwise fall through to
    # the scan so we don't surface unrelated objects as memos.
    try:
        parsed = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "summary" in parsed and "reasoning" in parsed:
        candidates.append(snippet)

    # Fallback: scan for a balanced JSON object containing "summary"
    # and "reasoning" keys.
    if not candidates and '"summary"' in snippet and '"reasoning"' in snippet:
        start = snippet.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escape_next = False
            for i in range(start, len(snippet)):
                c = snippet[i]
                if escape_next:
                    escape_next = False
                    continue
                if c == "\\" and in_string:
                    escape_next = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = snippet[start : i + 1]
                        if all(f'"{k}"' in candidate for k in ("summary", "reasoning")):
                            candidates.append(candidate)
                        break
            start = snippet.find("{", start + 1)
            if candidates:
                break

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _normalize_memo(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce raw memo fields into a stable shape for persistence."""
    summary = obj.get("summary")
    reasoning = obj.get("reasoning")
    citations = obj.get("citations") or []
    tags = obj.get("tags") or []
    if not isinstance(citations, list):
        citations = []
    if not isinstance(tags, list):
        tags = []
    return {
        "summary": str(summary) if summary is not None else "",
        "reasoning": str(reasoning) if reasoning is not None else "",
        "citations": [str(c) for c in citations[:50]],
        "tags": [str(t) for t in tags[:20]],
    }


def _synthesize_memo_from_source_finding(
    reasoning_kind: str,
    sf: Dict[str, Any],
    source_task_id: str,
) -> Optional[Dict[str, Any]]:
    """Synthesize a memo dict from dispatcher-provided source_finding without a model call.

    Mirrors the agent-side _synthesizeDispatcherMemo logic. Returns None if
    there is insufficient data to synthesize (falls back to VM path).
    """
    cite = [f"task:{source_task_id}"] if source_task_id else []
    if reasoning_kind == "regression_false_positive":
        vr = str(sf.get("verdict_reason") or "")
        if not vr:
            return None
        parts = [f"Verdict reason: {vr}"]
        inv = str(sf.get("investigation_summary") or "")
        if inv:
            parts.append(f"Investigation summary: {inv}")
        rc = str(sf.get("regression_class") or "")
        return {
            "summary": vr[:200],
            "reasoning": "\n\n".join(parts),
            "citations": cite,
            "tags": ["regression-false-positive"] + ([rc] if rc else []),
        }
    if reasoning_kind == "investigation_unactionable":
        rc = str(sf.get("root_cause") or "")
        sm = str(sf.get("summary") or "")
        if not rc and not sm:
            return None
        parts = [f"Root cause: {rc}"] if rc else []
        fx = str(sf.get("recommended_fix") or "")
        if fx:
            parts.append(f"Recommended fix: {fx}")
        oc = str(sf.get("outcome") or "")
        if oc:
            parts.append(f"Outcome: {oc}")
        return {
            "summary": sm[:200] or f"Investigation concluded: {oc}",
            "reasoning": "\n\n".join(parts),
            "citations": cite,
            "tags": ["investigation-unactionable"] + ([oc] if oc else []),
        }
    return None


def _apply_synthesized_memo(
    memo_task: Task,
    memo: Dict[str, Any],
    session: AsyncSession,
) -> None:
    """Persist a synthesized memo artifact and mark the task COMPLETED immediately.

    Called at spawn time for dispatcher memos whose full reasoning is already
    known — avoids provisioning a VM solely to reformat known text.
    """
    from app.models import Artifact, ArtifactType  # noqa: PLC0415

    normalized = _normalize_memo(memo)
    session.add(
        Artifact(
            task_id=memo_task.id,
            artifact_type=ArtifactType.MEMO,
            logs=json.dumps(normalized),
        )
    )
    po = dict(memo_task.plan_output) if isinstance(memo_task.plan_output, dict) else {}
    po["memo"] = normalized
    memo_task.plan_output = po
    flag_modified(memo_task, "plan_output")
    memo_task.status = TaskStatus.COMPLETED
    memo_task.latest_status_message = (
        f"Memo recorded ({len(normalized['citations'])} citation(s), "
        f"{len(normalized['tags'])} tag(s))"
    )


async def _handle_memo_completion(task: Task, logs: str, session: AsyncSession):
    """Persist a memo task's reasoning artifact.

    Memo tasks are terminal — they produce a single ``Artifact`` row of
    type ``MEMO`` and do NOT spawn child tasks or open a PR. Strategic
    review and downstream reasoning consumers can cite the artifact by id.

    Behaviour:
      * On parseable output: create one ``Artifact`` (artifact_type=MEMO,
        task_id=task.id, logs=serialized JSON), mirror the parsed object
        into ``task.plan_output['memo']``, and mark the task ``COMPLETED``.
        Memo tasks spawn no children and open no PR, so no lifecycle path
        promotes a childless, PR-less ``EVALUATING`` memo to ``COMPLETED`` —
        it must be marked terminal here directly, like the other terminal
        read-only handlers. ``query_recent_memo_citations`` reads only
        ``COMPLETED`` memos, so this is also the reader/writer contract.
      * On unparseable output: leave the task without an artifact, set
        ``latest_status_message`` so the operator sees why, and mark it
        ``COMPLETED`` (terminal, uncitable) rather than raising — failing
        here would re-queue an inherently invalid agent output.
    """
    # Local import keeps the module-level import surface stable and
    # avoids pulling Artifact into the strategic-completion re-export
    # chain (other handlers use the same lazy-import pattern).
    from app.models import Artifact, ArtifactType  # noqa: PLC0415

    parsed = _parse_memo_output(logs)
    if parsed is None:
        task.latest_status_message = (
            "Memo task completed with no parseable reasoning JSON"
        )
        task.status = TaskStatus.COMPLETED
        session.add(task)
        await session.commit()
        return

    memo = _normalize_memo(parsed)
    serialized = json.dumps(memo)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.MEMO,
        logs=serialized,
    )
    session.add(artifact)

    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    plan_output["memo"] = memo
    task.plan_output = plan_output
    flag_modified(task, "plan_output")
    task.status = TaskStatus.COMPLETED
    task.latest_status_message = (
        f"Memo recorded ({len(memo['citations'])} citation(s), "
        f"{len(memo['tags'])} tag(s))"
    )
    session.add(task)
    await session.commit()
    await session.refresh(artifact)

    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.COMPLETED},
    )
    logger.info(f"[MEMO] Persisted memo artifact {artifact.id} for task {task.id}")


_RECOVERED_AGENT_MESSAGE_MARKER = "--- recovered-agent-message-text ---"


async def _try_recover_memo_from_parse_failure(
    task: Task, logs: str, session: AsyncSession
) -> bool:
    """Scan a JSONL error log for a valid memo JSON buried in an agent_message envelope.

    When a memo task fails with failure_category='parse_failed' the Codex agent
    may have already emitted a schema-valid memo JSON inside an item.completed /
    item.updated event that the deployed agent handler failed to extract.  Tries
    three strategies in order:

    1. ``--- recovered-agent-message-text ---`` marker blocks appended by the
       agent handler's failure path — each block contains the full raw text of a
       candidate agent_message, which may be longer than what fits in the truncated
       JSONL tail.
    2. Line-by-line JSONL scan for item.completed / item.updated agent_message
       envelopes (original strategy, handles the intact-line case).
    3. Escaped-JSON fallback: if the logs contain ``\\"summary\\"`` and
       ``\\"reasoning\\"`` (the escaped forms that appear when a JSONL line is
       only partially captured), unescape the substring following the nearest
       ``"text":"`` and call ``_parse_memo_output`` on the result.

    Returns True on successful recovery, False if no valid memo is found (leaving
    the task status unchanged so the caller can decide how to proceed).
    """
    if not logs:
        return False

    # Strategy 1: recovered-agent-message-text marker blocks.
    if _RECOVERED_AGENT_MESSAGE_MARKER in logs:
        parts = logs.split(_RECOVERED_AGENT_MESSAGE_MARKER)
        for part in parts[1:]:  # skip the content before the first marker
            # Text up to the next '---' marker or end of string
            next_sep = part.find("\n---")
            candidate_text = part[:next_sep].strip() if next_sep != -1 else part.strip()
            if not candidate_text:
                continue
            parsed = _parse_memo_output(candidate_text)
            if parsed is not None and all(k in parsed for k in _MEMO_REQUIRED_KEYS):
                logger.info(
                    "[MEMO] Recovery: found valid memo JSON in recovered-agent-message-text "
                    "block for task %s",
                    task.id,
                )
                await _handle_memo_completion(task, candidate_text, session)
                return True

    # Strategy 2: line-by-line JSONL scan (original path).
    for raw_line in logs.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(event, dict):
            continue
        if event.get("type") not in ("item.completed", "item.updated"):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue

        # text may be a plain string or a list of content blocks
        raw_text = item.get("text") or item.get("content")
        if isinstance(raw_text, list):
            text = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw_text
            )
        elif isinstance(raw_text, str):
            text = raw_text
        else:
            continue

        parsed = _parse_memo_output(text)
        if parsed is None:
            continue
        if not all(k in parsed for k in _MEMO_REQUIRED_KEYS):
            continue

        logger.info(
            "[MEMO] Recovery: found valid memo JSON in agent_message envelope for task %s",
            task.id,
        )
        await _handle_memo_completion(task, text, session)
        return True

    # Strategy 3: escaped-JSON fallback for truncated JSONL tails.
    # The escaped forms \"summary\" and \"reasoning\" appear when the JSONL line
    # was cut and only the interior of the JSON-escaped text value is visible.
    if '\\"summary\\"' in logs and '\\"reasoning\\"' in logs:
        text_key = '"text":"'
        idx = logs.rfind(text_key)
        if idx != -1:
            raw_escaped = logs[idx + len(text_key) :]
            # Unescape JSON string escapes so _parse_memo_output can find the object.
            unescaped = (
                raw_escaped.replace('\\"', '"')
                .replace("\\\\", "\\")
                .replace("\\n", "\n")
            )
            parsed = _parse_memo_output(unescaped)
            if parsed is not None and all(k in parsed for k in _MEMO_REQUIRED_KEYS):
                logger.info(
                    "[MEMO] Recovery: found valid memo JSON via escaped-tail fallback "
                    "for task %s",
                    task.id,
                )
                await _handle_memo_completion(task, unescaped, session)
                return True

    return False


# ---------------------------------------------------------------------------
# Self audit completion — moved to task_completion_self_audit.
# Importers should keep using `from app.task_completion import ...`; the
# re-export block at the bottom of this module preserves the facade.
# ---------------------------------------------------------------------------
from app.task_completion_self_audit import (  # noqa: E402,F401
    _AUDIT_CLEARABLE_MODES,
    _MODE_STRICTNESS,
    _SELF_AUDIT_COMPONENT_KEYS,
    _SELF_AUDIT_PROPAGATED_MODES,
    _SHIP_RECOVERY_THRESHOLD,
    _VALID_SELF_AUDIT_MODES,
    _handle_self_audit_completion,
    _is_recent_strategic_mode_lock,
    _maybe_auto_clear_direction_mode,
    _normalize_self_audit_finding,
    _parse_self_audit_findings,
)


async def _handle_design_doc_completion(
    task: Task,
    logs: str,
    session: AsyncSession,
    *,
    project: Project | None = None,
):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.

    Phase C3 / Round 3 C3 + C4: callers in the post-commit chain pre-acquire
    the project lock at the top of ``_process_post_commit`` via the
    canonical pair-locker ``lock_project_then_task`` (Project before Task)
    and pass the locked Project as ``project=`` so this handler does NOT
    re-acquire. When ``project=None`` (legacy / direct callers), we fall
    back to acquiring the lock here ourselves via
    ``lock_project_for_strategic_transition`` with the same bounded-retry
    semantics. The canonical order is Project before Task — see
    ``docs/concurrency.md``.
    """
    # Deferred imports to avoid circular dependency
    from app.strategic_spawning import _spawn_strategic_review  # noqa: PLC0415
    from app.alert_dispatch import _create_strategic_alert  # noqa: PLC0415
    from app.task_locking import lock_project_for_strategic_transition  # noqa: PLC0415
    from app.strategic_events import emit_strategic_cycle_completed  # noqa: PLC0415

    # Phase 1 emitted strategic_cycle_started at spawn time. Every path out of
    # this handler that does NOT chain to Phase 2 must pair it with a
    # strategic_cycle_completed so the frontend "Strategist working…" indicator
    # never sticks on. Only a successful _spawn_strategic_review (which emits its
    # own Phase-2 started) is exempt — that keeps the cycle visibly active.
    phase2_spawned = False

    if project is None:
        # Legacy / direct-caller path: acquire the lock here. Post-commit
        # chain callers (task_lifecycle._process_post_commit) are expected
        # to pass ``project=`` to share the single acquisition across the
        # strategic-completion chain — see Phase C3 in the audit plan.
        project = await lock_project_for_strategic_transition(session, task.project_id)
    if project is None:
        # Distinguish "missing" from "held past retries" — the helper
        # returns None for both. We re-probe non-lockingly to choose the
        # right alert message; a missing project is a hard error, while
        # contention defers to the next cycle.
        exists = (
            await session.execute(
                select(Project.id).where(Project.id == task.project_id)
            )
        ).first()
        if exists is None:
            logger.error(
                f"[DESIGN DOC] Task {task.id}: project {task.project_id} not found"
            )
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Design doc update failed: project not found (task {str(task.id)[:8]})",
            )
            # Phase 1 ended without chaining — close the cycle for the UI.
            await emit_strategic_cycle_completed(task.project_id, task.id, 1)
            return
        logger.warning(
            f"[DESIGN DOC] Task {task.id}: project {task.project_id} held under contention "
            f"after retries; deferring strategic chain"
        )
        await _create_strategic_alert(
            task.project_id,
            session,
            f"Design doc update: project row contended (task {str(task.id)[:8]}) — strategic chain deferred",
        )
        # Phase 1 ended without chaining — close the cycle for the UI.
        await emit_strategic_cycle_completed(task.project_id, task.id, 1)
        return

    doc = project.design_doc or {}
    rev = doc.get("_meta", {}).get("revision", 0)
    page_count = sum(1 for k in doc if not k.startswith("_"))
    logger.info(f"[DESIGN DOC] Task {task.id} completed: {page_count} pages, rev {rev}")

    if page_count == 0:
        await _create_strategic_alert(
            task.project_id,
            session,
            f"Design doc update produced 0 pages (task {str(task.id)[:8]})",
        )

    # Chain: spawn Phase 2 (strategic review)
    # Do this BEFORE committing task status so a failed spawn doesn't leave
    # the task as COMPLETED with no Phase 2 follow-up.
    cycle_id = (task.context or {}).get("strategic_cycle_id")
    # Architecture-cycle operator steering: the pending refresh marker was
    # consumed when Phase 1 (this design_doc task) was spawned, so it no longer
    # reads as operator-origin in vision. The flag was stamped on the task
    # context at spawn time — thread it into Phase 2 so the operator steering
    # directive still reaches the strategist.
    operator_steered = bool((task.context or {}).get("operator_steered"))
    # The operator steering text was persisted on the Phase 1 task context (the
    # refresh marker — and its excerpt — was consumed at Phase 1 spawn). Thread
    # it into Phase 2 so the steering directive survives even when the operator
    # message has aged out of the live conversation-thread snapshot.
    steering_excerpt = (task.context or {}).get("steering_excerpt")
    spawned = None
    try:
        # Wave F4 H9: removed `await session.refresh(project)`. The caller
        # (post-commit chain via task_lifecycle._process_post_commit) already
        # holds the project lock acquired via lock_project_for_strategic_transition;
        # the row is fresh-read at lock time, so a refresh here is a redundant
        # round-trip that defeats the lock-share optimization between
        # strategic-completion handlers.
        spawned = await _spawn_strategic_review(
            project,
            session,
            cycle_id=cycle_id,
            operator_steered=operator_steered,
            steering_excerpt=steering_excerpt,
        )
        if spawned:
            phase2_spawned = True
            task.latest_status_message = (
                f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
            )
        else:
            # Spawn silently skipped (active strategic task) — create retry marker
            logger.warning(
                f"[DESIGN DOC] Phase 2 spawn skipped for task {task.id} (active strategic task exists)"
            )
            task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review deferred (active task exists)"
            project.vision = {
                **(project.vision or {}),
                "_pending_strategic_review": {
                    "design_doc_task_id": str(task.id),
                    "failed_at": _utcnow().isoformat(),
                    "error": "skipped: active strategic task exists",
                    "cycle_id": cycle_id,
                    "operator_steered": operator_steered,
                    "steering_excerpt": steering_excerpt,
                },
            }
            flag_modified(project, "vision")
            session.add(project)
    except Exception as chain_exc:
        logger.error(
            f"[DESIGN DOC] Failed to chain Phase 2 for task {task.id}: {chain_exc}",
            exc_info=True,
        )
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — FAILED to spawn strategic review: {chain_exc!r}"[
            :500
        ]
        await _create_strategic_alert(
            task.project_id,
            session,
            f"Strategic review failed to spawn after design doc update (task {str(task.id)[:8]}): {chain_exc!r}",
        )
        # Save a retry marker so the next research spawn can retry the strategic review
        project.vision = {
            **(project.vision or {}),
            "_pending_strategic_review": {
                "design_doc_task_id": str(task.id),
                "failed_at": _utcnow().isoformat(),
                "error": str(chain_exc)[:200],
                "cycle_id": cycle_id,
                "operator_steered": operator_steered,
                "steering_excerpt": steering_excerpt,
            },
        }
        if cycle_id:
            from app.research_completion import _update_cycle_history  # noqa: PLC0415

            _update_cycle_history(
                project,
                cycle_id,
                {
                    "phase1_completed_at": _utcnow().isoformat(),
                    "outcome": "failed_phase2_spawn",
                    "error": str(chain_exc)[:200],
                },
            )
        flag_modified(project, "vision")
        session.add(project)
    # Previously emitted a "Architecture analysis completed, strategic review
    # starting" phase-transition message. Removed as part of the auto-status
    # cleanup — the phase transition is visible in the task timeline and the
    # eventual strategic cycle_summary carries the user-facing narrative.
    session.add(task)
    await session.commit()
    # Post-commit: when Phase 2 was NOT spawned (deferred because an active
    # strategic task exists, or the spawn raised), the Phase-1 started event is
    # otherwise never paired. Emit completed so the indicator clears. A
    # successful spawn already emitted a Phase-2 started, so it is skipped.
    if not phase2_spawned:
        await emit_strategic_cycle_completed(task.project_id, task.id, 1)


async def confirm_applied_playbook_entries(task: Task, session: AsyncSession) -> None:
    """Increment confirmed_count for playbook entries that guided this task's prompt.

    Called after a create/fix/refactor task merges. Reads playbook_applied_entry_ids
    from task.context (stamped at spawn time by research_completion) and bumps
    confirmed_count on each live entry in project.design_doc so the archival ranker
    can distinguish actively-effective patterns from stale ones.
    """
    entry_ids = (task.context or {}).get("playbook_applied_entry_ids") or []
    if not entry_ids:
        return
    now_iso = _utcnow().isoformat()

    # Serialize the design_doc read-modify-write through the canonical row lock
    # so a concurrent same-project completion bumping a different design_doc key
    # is not clobbered. The mutate_fn bumps confirmed_count on live playbook
    # entries inside the lock against the freshly-loaded doc.
    confirmed = {"v": 0}

    def _mutate(doc: Dict[str, Any]) -> None:
        for entry_id in entry_ids:
            full_key = (
                entry_id if entry_id.startswith("playbook/") else f"playbook/{entry_id}"
            )
            entry = doc.get(full_key)
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1
            entry["last_confirmed_at"] = now_iso
            doc[full_key] = entry
            confirmed["v"] += 1

    # expire_on_commit=False means a Project already identity-mapped by the
    # surrounding completion flow carries a stale design_doc; expire it so the
    # lock's SELECT FOR UPDATE re-reads the freshly-committed row (canonical
    # pattern documented at the top of this module, ~L103-110).
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["design_doc"])
    await with_design_doc_locked(session, task.project_id, _mutate)
    if confirmed["v"]:
        logger.info(
            "[PLAYBOOK] Confirmed %d entries for task %s", confirmed["v"], task.id
        )
