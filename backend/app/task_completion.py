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
import re
import uuid
from datetime import datetime, timedelta, timezone
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
    DIAGNOSIS_IN_PROGRESS_KEY,
    DIAGNOSIS_STARTED_AT_KEY,
    DIAGNOSIS_TASK_ID_KEY,
    Project,
    RECOMMENDED_MODE_META_KEY,
    RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
    RECOMMENDED_MODE_SOURCE_STRATEGIC,
    SystemMaturity,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.plan_completion import (
    TASK_PRIORITY_MAP,
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
from app.research_context import _build_research_context
from app.routers.agent import get_github_token_for_task
from app.shared_state import (
    clear_direction_hold,
    get_project_integration_branch,
    runtime_config as _runtime_config,
    scope_to_effort,
)
from app.payload_schemas import validate_internal_payload
from app.broadcast import broadcast_to_project
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
from app.time_utils import _utcnow
from app.self_audit_dispatch import _record_auto_clear_note
from app.task_locking import with_design_doc_locked, with_directions_locked

logger = logging.getLogger(__name__)

# VM cost rates ($/sec) — mirror admin.py to avoid cross-module import
_SPOT_RATE_PER_SEC = 0.020 / 3600
_ON_DEMAND_RATE_PER_SEC = 0.067 / 3600

# Shared marker used by both the TypeScript agent handler (review-fix.ts
# buildBlueprintPrFooter) and this Python helper so either side can update
# the same block without leaving duplicates.
BLUEPRINT_PR_META_MARKER = "<!-- blueprint:meta -->"


async def _persist_direction_field_update(
    session: AsyncSession,
    project: Project,
    direction: Dict[str, Any],
) -> None:
    """Serialize one mutated direction dict back into ``project.directions``.

    The caller has already applied every field mutation to ``direction`` — an
    element of a directions list that was loaded *outside* any lock. Writing the
    whole stale list back (the legacy ``project.directions = directions``
    pattern) loses a concurrent same-project completion's write to a *different*
    direction. This helper instead re-acquires the project row lock via
    ``with_directions_locked``, re-reads the directions list fresh under that
    lock, and overwrites only the entry whose ``id`` matches — every other
    direction is left exactly as the fresh read found it.

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
            target.clear()
            target.update(direction)
        return None

    await with_directions_locked(session, project.id, _apply)


def _estimated_task_cost_usd(task: Task) -> Optional[float]:
    """Return the VM cost for a task, or None when timing data is incomplete."""
    if not task.vm_provisioned_at or not task.vm_stopped_at:
        return None
    rate = _SPOT_RATE_PER_SEC if task.vm_spot else _ON_DEMAND_RATE_PER_SEC
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


async def _record_remediation_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Stamp remediation outcome on the linked direction when a diagnosis-spawned task terminates.

    On a failed/cancelled remediation, additionally attempt to spawn a
    follow-up investigation so the pipeline does not have to wait for the
    48h auto-recovery cycle to revisit the root cause.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
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
    _REVIEW_PIPELINE_TYPES = {
        "create",
        "refactor",
        "docs",
        "qa",
        "deps",
        "migrate",
        "fix",
        "review",
    }
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
        if recorded and status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            await _maybe_spawn_followup_investigation(task, session)
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


async def _record_regression_signal_if_matching(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Stamp remediation_regression_signal when a non-remediation failure during
    the monitoring window matches the last-remediated fingerprint."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase  # noqa: PLC0415

    ctx = task.context or {}
    if status == TaskStatus.COMPLETED and (task.first_payload or {}).get("type") in {
        "create",
        "refactor",
        "docs",
        "qa",
        "deps",
        "migrate",
        "fix",
        "review",
    }:
        return
    regression_class = ctx.get("regression_class")
    if regression_class:
        result = await session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == task.project_id,
                RegressionRemediationCase.regression_class == regression_class,
            )
        )
        case = result.scalar_one_or_none()
        if case is not None and case.status == RegressionCaseStatus.MERGED:
            snapshot = ctx.get("signal_snapshot") or {}
            value = snapshot.get("value")
            threshold = snapshot.get("threshold")
            recovered = (
                isinstance(value, (int, float))
                and isinstance(threshold, (int, float))
                and value < threshold
            )
            hit_count = snapshot.get("hit_count")
            if not recovered and isinstance(hit_count, int):
                recovered = hit_count <= int(case.hit_count or 0)
            if recovered:
                case.mark_closed()
            else:
                case.re_arm(fast_track=False)
            session.add(case)
    if status != TaskStatus.FAILED:
        return
    direction_id = ctx.get("direction_id")
    if not direction_id or ctx.get("diagnosis_task_id"):
        return
    if not (task.latest_status_message or "").strip():
        return
    # Must mirror the stage-aware fingerprint that production paths persist
    # (auto_remediation, _record_remediation_outcome) — a stage-less probe
    # would never match.
    from app.anomaly_detection import _task_error_fingerprint  # noqa: PLC0415

    try:
        task_fp = _task_error_fingerprint(task)
    except Exception:
        return
    if not task_fp:
        return
    from app.direction_lifecycle import _REMEDIATION_MONITOR_EXPIRY_DAYS  # noqa: PLC0415

    # Serialize the regression-signal stamp through the canonical row lock so a
    # concurrent same-project completion mutating a different direction is not
    # clobbered. All target-dependent guards run inside the lock against the
    # freshly-loaded directions list; the task-only guards above stay outside.
    applied = {"count": None}

    def _mutate(directions: List[Dict[str, Any]]):
        target = next((d for d in directions if d.get("id") == direction_id), None)
        if target is None:
            return None
        monitor_started_at = target.get("remediation_monitor_started_at")
        last_rem_fp = target.get("last_remediation_fingerprint")
        if not monitor_started_at or not last_rem_fp:
            return None
        try:
            age_days = (
                _utcnow() - datetime.fromisoformat(monitor_started_at)
            ).total_seconds() / 86400
            if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                return None
        except (TypeError, ValueError):
            return None
        if str(task_fp) != str(last_rem_fp):
            return None
        target["remediation_regression_signal"] = True
        target["remediation_regression_count"] = (
            int(target.get("remediation_regression_count") or 0) + 1
        )
        target["remediation_regression_last_task_id"] = str(task.id)
        target["remediation_regression_last_at"] = _utcnow().isoformat()
        applied["count"] = target["remediation_regression_count"]
        return None

    # expire_on_commit=False leaves a stale directions snapshot in the identity
    # map; drop it so with_directions_locked re-reads the row fresh under the lock.
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["directions"])
    await with_directions_locked(session, task.project_id, _mutate)
    if applied["count"] is not None:
        logger.info(
            f"[DIAGNOSIS] Regression signal on direction {direction_id}: "
            f"task {task.id} failed with fingerprint matching last remediation "
            f"(count={applied['count']})"
        )


# Maximum follow-up investigations the failure-driven trigger will spawn
# per (direction, fingerprint) pair before falling back to the 48h
# auto-recovery cycle. Prevents a never-ending reinvestigate loop when the
# root cause genuinely can't be diagnosed from code signals alone.
_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT = 2

# Retain this many recent remediation attempts per direction so follow-up
# investigations can summarize what was already tried.
_REMEDIATION_HISTORY_MAX = 5

# Attempts older than this window are treated as expired and do not count
# against the cap, letting directions that hit the cap recover naturally
# as the codebase evolves.
_FOLLOWUP_INVESTIGATION_DECAY_DAYS = 14


def _active_followup_timestamps(
    entry: Any,
    *,
    now: Optional[datetime] = None,
) -> List[str]:
    """Return non-expired ISO timestamps from a fingerprint entry.

    Accepts the current list-of-timestamps format and the legacy integer
    count format (treated as fully expired so old directions can recover
    once this decay logic rolls out).
    """
    if not isinstance(entry, list):
        return []
    cutoff = (now or _utcnow()) - timedelta(days=_FOLLOWUP_INVESTIGATION_DECAY_DAYS)
    active: List[str] = []
    for ts in entry:
        try:
            if datetime.fromisoformat(str(ts)) >= cutoff:
                active.append(str(ts))
        except (TypeError, ValueError):
            continue
    return active


def _project_active_followup_count(
    project: Project,
    fp_key: str,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Sum non-expired follow-up timestamps for ``fp_key`` across all directions.

    Used to enforce a project-wide cap so a shared root cause (e.g. a broken
    external API) does not trigger N directions × cap follow-ups. ``no_fingerprint``
    is excluded from cross-direction aggregation because it conflates unrelated
    errors that happen to lack a real fingerprint.
    """
    if not fp_key or fp_key == "no_fingerprint":
        return 0
    total = 0
    for d in project.directions or []:
        attempts = (d.get("followup_investigation_attempts_by_fingerprint") or {}).get(
            fp_key
        )
        total += len(_active_followup_timestamps(attempts, now=now))
    return total


async def _recent_followup_investigation_exists(
    project_id,
    direction_id,
    cutoff,
    fingerprint,
    session,
    exclude_task_id=None,
) -> bool:
    """Return True if a recent investigate task for this direction blocks a follow-up.

    Mirrors the fingerprint-aware dedup used by the initial-spawn path in
    ``autonomous_research._recent_investigation_exists``: when ``fingerprint`` is
    provided, only investigations whose ``diagnosis_fingerprint`` matches block
    the new follow-up — distinct failure patterns on the same direction can each
    proceed. Per-direction concurrent capping continues to be enforced by the
    ``diagnosis_in_progress`` flag at the caller.
    """
    q = select(Task.id).where(
        Task.project_id == project_id,
        Task.created_at >= cutoff,
        Task.payloads[0]["type"].as_string() == "investigate",
        Task.context["direction_id"].as_string() == str(direction_id),
    )
    if exclude_task_id is not None:
        q = q.where(Task.id != exclude_task_id)
    if fingerprint:
        q = q.where(
            Task.context["diagnosis_fingerprint"].as_string() == str(fingerprint)
        )
    row = (await session.execute(q.limit(1))).first()
    return row is not None


async def _find_sibling_fingerprint_investigation(
    project_id,
    fingerprint,
    cutoff,
    session,
    exclude_task_id=None,
) -> Optional[str]:
    """Return ID of a recent investigate task sharing ``fingerprint`` (any direction).

    Mirrors the cross-direction dedup used by direction_lifecycle._spawn_direction_diagnosis:
    if a sibling direction already has an open investigation for the same root cause,
    a second direction's follow-up would re-analyze the same fingerprint.
    """
    if not fingerprint:
        return None
    q = select(Task.id).where(
        Task.project_id == project_id,
        Task.created_at >= cutoff,
        Task.payloads[0]["type"].as_string() == "investigate",
        Task.context["diagnosis_fingerprint"].as_string() == str(fingerprint),
    )
    if exclude_task_id is not None:
        q = q.where(Task.id != exclude_task_id)
    row = (await session.execute(q.limit(1))).first()
    return str(row[0]) if row else None


def _append_followup_dedup_note(
    direction: Dict[str, Any], fingerprint, existing_task_id: str
) -> None:
    note_text = (
        f"Skipped duplicate follow-up investigation: shares error fingerprint "
        f"{str(fingerprint)[:80]} with existing investigate task {existing_task_id}."
    )[:STRATEGIC_RATIONALE_MAX]
    notes = list(direction.get("notes") or [])
    notes.append(
        {
            "text": note_text,
            "added_at": _utcnow().isoformat(),
            "source": "followup_dedup",
            "shared_investigation_task_id": existing_task_id,
        }
    )
    direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


async def _maybe_spawn_followup_investigation(
    task: Task,
    session: AsyncSession,
) -> Optional[Task]:
    """Spawn a follow-up investigate task after a diagnosis-spawned remediation fails.

    Intended to be called only when ``_record_remediation_outcome`` has just
    recorded a failed remediation for this task. Builds a new investigate
    task whose prompt carries the prior diagnosis summary, the approach that
    failed, the failure class, and the error summary — giving the agent a
    materially different starting point than the original diagnosis.

    Guards (all must pass to spawn):
      * task has ``diagnosis_task_id`` and ``direction_id`` in context
      * direction is not already flagged ``diagnosis_in_progress``
      * no investigate task for this direction created in the last 24h
      * fewer than ``_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT`` follow-ups
        have been spawned for the current fingerprint on this direction
    """
    ctx = task.context or {}
    direction_id = ctx.get("direction_id")
    diagnosis_task_id = ctx.get("diagnosis_task_id")
    if not direction_id or not diagnosis_task_id:
        return None
    project = await session.get(Project, task.project_id)
    if project is None:
        return None
    directions = list(project.directions or [])
    target = next((d for d in directions if d.get("id") == direction_id), None)
    if target is None:
        return None
    if target.get(DIAGNOSIS_IN_PROGRESS_KEY):
        logger.debug(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: "
            f"diagnosis_in_progress already set"
        )
        return None
    cutoff = _utcnow() - timedelta(hours=24)
    fingerprint = (
        ctx.get("diagnosis_fingerprint")
        or ctx.get("error_fingerprint")
        or target.get("last_remediation_fingerprint")
    )
    if await _recent_followup_investigation_exists(
        project.id,
        direction_id,
        cutoff,
        fingerprint,
        session,
    ):
        logger.debug(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: "
            f"investigate task already created in last 24h "
            f"(fingerprint={str(fingerprint)[:40] if fingerprint else 'none'})"
        )
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id,
        fingerprint,
        cutoff,
        session,
    )
    if shared_id is not None:
        _append_followup_dedup_note(target, fingerprint, shared_id)
        await _persist_direction_field_update(session, project, target)
        logger.info(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: shares "
            f"fingerprint {str(fingerprint)[:40]} with investigate task {shared_id}"
        )
        return None
    fp_key = str(fingerprint) if fingerprint else "no_fingerprint"
    attempts_by_fp = dict(
        target.get("followup_investigation_attempts_by_fingerprint") or {}
    )
    now = _utcnow()
    active_ts = _active_followup_timestamps(attempts_by_fp.get(fp_key), now=now)
    project_active = _project_active_followup_count(project, fp_key, now=now)
    if (
        len(active_ts) >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
        or project_active >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
    ):
        logger.info(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: "
            f"cap reached for fingerprint {fp_key[:40]} "
            f"(direction={len(active_ts)}, project={project_active}, "
            f"cap={_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT}, "
            f"decay={_FOLLOWUP_INVESTIGATION_DECAY_DAYS}d)"
        )
        await _maybe_escalate_exhausted_followups(
            project=project,
            direction=target,
            fp_key=fp_key,
            session=session,
        )
        return None

    prior_summary = (target.get("diagnosis_summary") or "").strip()
    failure_class = target.get("last_remediation_failure_class") or "(unknown)"
    diagnosis_confidence = ctx.get("diagnosis_confidence")
    error_summary = target.get("last_remediation_error_summary") or "(no error message)"
    failed_prompt = (task.first_payload or {}).get("prompt", "") or ""
    prior_history = list(target.get("remediation_history") or [])
    # Exclude the current failed attempt (just appended by _record_remediation_outcome)
    # since it is already surfaced separately as "approach that failed".
    earlier_attempts = [h for h in prior_history if h.get("task_id") != str(task.id)]
    regression_case = None
    regression_class = ctx.get("regression_class")
    if regression_class:
        from app.models import RegressionRemediationCase  # noqa: PLC0415

        rr_result = await session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == project.id,
                RegressionRemediationCase.regression_class == regression_class,
            )
        )
        regression_case = rr_result.scalar_one_or_none()
    prompt_parts = [
        "A remediation task spawned from a prior diagnosis on this direction "
        "has failed. Re-investigate the root cause — the earlier recommended "
        "fix was insufficient or targeted the wrong layer.",
        "",
        f"Direction: {(target.get('text') or '')[:200]}",
        f"Direction id: {direction_id}",
        "",
        f"Prior diagnosis summary:\n{prior_summary[:1500] or '(none recorded)'}",
        "",
        f"Remediation approach that failed (agent prompt, truncated):\n"
        f"{failed_prompt[:1500] or '(no prompt recorded)'}",
        "",
        f"Failure class: {failure_class}",
        f"Error summary: {str(error_summary)[:400]}",
    ]
    if target.get("remediation_regression_signal"):
        regression_count = int(target.get("remediation_regression_count") or 0)
        prompt_parts.extend(
            [
                "",
                f"REGRESSION SIGNAL: {regression_count} task failure(s) during the "
                f"post-merge monitoring window have fingerprints matching the prior "
                f"remediated error. The previous fix was merged but did not hold. "
                f"Do not propose the same approach — treat the root cause as "
                f"unresolved.",
            ]
        )
    if diagnosis_confidence:
        guidance = {
            "low": (
                "Prior diagnosis confidence was LOW — strongly consider "
                "re-diagnosing the root cause from scratch rather than "
                "iterating on the prior hypothesis."
            ),
            "medium": (
                "Prior diagnosis confidence was MEDIUM — re-examine the root "
                "cause hypothesis; a different layer or interacting cause may "
                "be at play."
            ),
            "high": (
                "Prior diagnosis confidence was HIGH — the root cause is "
                "likely correct; focus on a materially different "
                "implementation approach for the same root cause."
            ),
        }.get(str(diagnosis_confidence).lower(), "")
        prompt_parts.extend(
            [
                "",
                f"Prior diagnosis confidence: {diagnosis_confidence}",
            ]
        )
        if guidance:
            prompt_parts.append(guidance)
    if earlier_attempts:
        lines = ["", "Earlier remediation attempts on this direction (oldest first):"]
        for i, entry in enumerate(earlier_attempts, start=1):
            ts = str(entry.get("timestamp") or "")[:19]
            result = entry.get("result") or "?"
            parts = [f"  {i}. [{ts}] result={result}"]
            if entry.get("failure_class"):
                parts.append(f"class={entry['failure_class']}")
            snippet = str(entry.get("prompt_snippet") or "").strip()
            if snippet:
                parts.append(f"approach={snippet[:200]!r}")
            err = str(entry.get("error_summary") or "").strip()
            if err:
                parts.append(f"error={err[:200]!r}")
            lines.append("; ".join(parts))
        prompt_parts.extend(lines)
    if regression_case is not None and (regression_case.attempts or 0) >= 2:
        rc_lines: list[str] = [
            "",
            "PRIOR ATTEMPT CONTEXT (pipeline regression)",
            f"This regression has been investigated {regression_case.attempts} time(s) without recovering the signal.",
        ]
        pr = regression_case.last_remediation_pr_number
        if pr is not None:
            rc_lines.append(
                f"Prior remediation PR: #{pr}. Read its diff to understand what was already tried "
                f"before forming a new hypothesis."
            )
        snap = regression_case.signal_snapshot_at_merge or {}
        post_val = snap.get("current_rate")
        if post_val is not None:
            rc_lines.append(f"Signal value after prior fix merged: {post_val:.1%}.")
        rc_lines.append(
            "Do NOT converge on the same diagnosis as prior attempts. "
            "Approach from a different angle — look for a deeper root cause or a "
            "different contributing factor that the prior fix missed."
        )
        prompt_parts.extend(rc_lines)
    prompt_parts.extend(
        [
            "",
            "Produce a recommended_fix that is materially different from all prior "
            "attempts above. Consider whether the direction scope is wrong, prior "
            "fixes targeted a symptom rather than the cause, or a systemic/"
            "cross-cutting issue (shared infrastructure, framework bug, test "
            "harness) is at play.",
        ]
    )
    prompt = "\n".join(prompt_parts)[:20000]
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": get_project_integration_branch(project),
        },
        "task_completion:remediation_failure_followup",
    )
    task_context = {
        "direction_id": str(direction_id),
        "diagnosis_trigger": "remediation_failure_followup",
        "failed_remediation_task_id": str(task.id),
    }
    original_diagnosis_task_id = ctx.get("original_diagnosis_task_id") or ctx.get(
        "diagnosis_task_id"
    )
    if original_diagnosis_task_id:
        task_context["original_diagnosis_task_id"] = str(original_diagnosis_task_id)
    if fingerprint:
        task_context["diagnosis_fingerprint"] = str(fingerprint)
        task_context["failure_fingerprints"] = [str(fingerprint)]
    if diagnosis_confidence:
        task_context["diagnosis_confidence"] = str(diagnosis_confidence)
    inherited_failure_class = ctx.get("trigger_failure_class")
    if inherited_failure_class:
        task_context["last_failure_class"] = inherited_failure_class
    inherited_failure_taxonomy = ctx.get("trigger_failure_taxonomy")
    if inherited_failure_taxonomy:
        task_context["failure_taxonomy"] = inherited_failure_taxonomy
    new_task = Task(
        project_id=project.id,
        title=f"Investigate remediation failure: {(target.get('text') or '')[:60]}",
        description=(
            f"Follow-up investigation after remediation task {str(task.id)[:8]} failed"
        )[:500],
        payloads=[payload],
        context=task_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=70,
    )
    session.add(new_task)
    active_ts.append(now.isoformat())
    attempts_by_fp[fp_key] = active_ts
    target["followup_investigation_attempts_by_fingerprint"] = attempts_by_fp
    target[DIAGNOSIS_IN_PROGRESS_KEY] = True
    target[DIAGNOSIS_STARTED_AT_KEY] = now.isoformat()
    await _persist_direction_field_update(session, project, target)
    logger.info(
        f"[DIAGNOSIS] Spawned follow-up investigation {new_task.id} after "
        f"remediation task {task.id} failed (direction={direction_id}, "
        f"fingerprint={fp_key[:40]}, "
        f"attempt {len(active_ts)}/"
        f"{_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT})"
    )
    return new_task


def _followup_history_all_failed(
    direction: Dict[str, Any],
    fp_key: str,
) -> bool:
    """True when remediation_history for this fingerprint exists and every entry failed.

    Any merged entry — even an older one — indicates partial success on this
    fingerprint, so the direction is not a candidate for cap-exhaustion escalation.
    """
    history = list(direction.get("remediation_history") or [])
    relevant = [
        h
        for h in history
        if (str(h.get("fingerprint") or "") or "no_fingerprint") == fp_key
    ]
    if not relevant:
        return False
    return all(h.get("result") != "merged" for h in relevant)


async def _maybe_escalate_exhausted_followups(
    *,
    project: Project,
    direction: Dict[str, Any],
    fp_key: str,
    session: AsyncSession,
) -> None:
    """Escalate to operator when the follow-up cap is hit with no merged remediation.

    Without this, the direction sits in a dead end: automated remediation is
    exhausted, the 48h auto-recovery cycle re-triggers the same failure pattern,
    and nobody is told. Mirror the auto-pause escalation path in direction_lifecycle
    so operators get a BLOCKER notification the moment the full diagnosis→remediation
    →follow-up chain has been exhausted.

    All mutations target the single ``direction`` dict; the write is serialized
    once at the end through ``_persist_direction_field_update`` so a concurrent
    same-project completion mutating a different direction is not clobbered.
    """
    if direction.get("needs_manual_intervention"):
        return
    if not _followup_history_all_failed(direction, fp_key):
        return
    direction["needs_manual_intervention"] = True
    logger.warning(
        f"[DIAGNOSIS] Direction {direction.get('id')} flagged needs_manual_intervention: "
        f"follow-up cap exhausted for fingerprint {fp_key[:40]} with all prior "
        f"remediations failed"
    )
    try:
        from app.notification_dispatch import post_remediation_escalation_message  # noqa: PLC0415
        from app.direction_lifecycle import _build_escalation_args  # noqa: PLC0415

        await post_remediation_escalation_message(
            session,
            project.id,
            **_build_escalation_args(direction),
        )
        direction.pop("escalation_notification_pending", None)
        direction.pop("escalation_notification_retries", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[NOTIFY] Failed to post follow-up cap escalation for direction "
            f"{direction.get('id')}: {exc!r}"
        )
        direction["escalation_notification_pending"] = True
        direction["escalation_notification_retries"] = int(
            direction.get("escalation_notification_retries", 0)
        )
        from app.models import Alert, AlertSeverity  # noqa: PLC0415

        session.add(
            Alert(
                project_id=project.id,
                severity=AlertSeverity.ERROR,
                title="Follow-up escalation notification failed",
                message=(
                    f"Direction {direction.get('id')} ('{(direction.get('text') or '')[:80]}') "
                    f"hit the follow-up investigation cap with no merged remediation, but the "
                    f"BLOCKER notification failed: {exc!r}. Fingerprint={fp_key[:80]}, "
                    f"remediation_attempts={int(direction.get('remediation_attempts', 0))}."
                ),
            )
        )
    await _persist_direction_field_update(session, project, direction)


async def _maybe_spawn_low_confidence_followup(
    *,
    investigation_task: Task,
    project: Project,
    direction: Dict[str, Any],
    session: AsyncSession,
) -> Optional[Task]:
    """Spawn a re-investigation when a diagnosis completed at LOW confidence.

    Mirrors the remediation-failure follow-up guards (24h dedup excluding the
    current investigation, per-fingerprint cap, diagnosis_in_progress flag),
    but issues a different prompt: ask the agent to narrow scope, reproduce
    the failure, or examine signals the first pass may have missed — rather
    than re-running the same root-cause analysis that just produced a weak
    answer.
    """
    direction_id = direction.get("id")
    if not direction_id or direction.get(DIAGNOSIS_IN_PROGRESS_KEY):
        return None
    cutoff = _utcnow() - timedelta(hours=24)
    fingerprint = (investigation_task.context or {}).get(
        "diagnosis_fingerprint"
    ) or direction.get("error_fingerprint")
    if await _recent_followup_investigation_exists(
        project.id,
        direction_id,
        cutoff,
        fingerprint,
        session,
        exclude_task_id=investigation_task.id,
    ):
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id,
        fingerprint,
        cutoff,
        session,
        exclude_task_id=investigation_task.id,
    )
    if shared_id is not None:
        _append_followup_dedup_note(direction, fingerprint, shared_id)
        logger.info(
            f"[DIAGNOSIS] Skip low-confidence follow-up for direction "
            f"{direction_id}: shares fingerprint {str(fingerprint)[:40]} "
            f"with investigate task {shared_id}"
        )
        return None
    fp_key = str(fingerprint) if fingerprint else "no_fingerprint"
    attempts_by_fp = dict(
        direction.get("followup_investigation_attempts_by_fingerprint") or {}
    )
    now = _utcnow()
    active_ts = _active_followup_timestamps(attempts_by_fp.get(fp_key), now=now)
    if (
        len(active_ts) >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
        or _project_active_followup_count(project, fp_key, now=now)
        >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
    ):
        await _maybe_escalate_exhausted_followups(
            project=project,
            direction=direction,
            fp_key=fp_key,
            session=session,
        )
        return None
    prior_summary = (direction.get("diagnosis_summary") or "").strip()
    prompt = "\n".join(
        [
            "A prior investigation on this direction produced a LOW-confidence "
            "diagnosis. Re-investigate with a different approach: narrow the scope "
            "to one concrete failing task, try to reproduce the failure, and examine "
            "signals the prior pass may have missed (raw logs, stack traces, recent "
            "diffs, CI output) rather than re-running the same root-cause analysis.",
            "",
            f"Direction: {(direction.get('text') or '')[:200]}",
            f"Direction id: {direction_id}",
            "",
            f"Prior (low-confidence) summary:\n{prior_summary[:1500] or '(none recorded)'}",
            "",
            "Return a diagnosis with HIGH or MEDIUM confidence, or explicitly "
            "declare that code signals alone are insufficient and manual "
            "intervention is required.",
        ]
    )[:20000]
    inv_ctx = investigation_task.context or {}
    original_diagnosis_task_id = inv_ctx.get("original_diagnosis_task_id") or str(
        investigation_task.id
    )
    task_ctx: Dict[str, Any] = {
        "direction_id": str(direction_id),
        "diagnosis_trigger": "low_confidence_followup",
        "previous_investigation_task_id": str(investigation_task.id),
        "original_diagnosis_task_id": str(original_diagnosis_task_id),
    }
    if fingerprint:
        task_ctx["diagnosis_fingerprint"] = str(fingerprint)
        task_ctx["failure_fingerprints"] = [str(fingerprint)]
    inherited_failure_class = inv_ctx.get("last_failure_class")
    if inherited_failure_class:
        task_ctx["last_failure_class"] = inherited_failure_class
    inherited_failure_taxonomy = inv_ctx.get("failure_taxonomy")
    if inherited_failure_taxonomy:
        task_ctx["failure_taxonomy"] = inherited_failure_taxonomy
    new_task = Task(
        project_id=project.id,
        title=f"Re-investigate low-confidence diagnosis: {(direction.get('text') or '')[:50]}",
        description=(
            f"Follow-up after low-confidence investigation "
            f"{str(investigation_task.id)[:8]}"
        )[:500],
        payloads=[
            validate_internal_payload(
                {
                    "type": "investigate",
                    "prompt": prompt,
                    "base_branch": get_project_integration_branch(project),
                },
                "task_completion:low_confidence_followup",
            )
        ],
        context=task_ctx,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=70,
    )
    session.add(new_task)
    active_ts.append(now.isoformat())
    attempts_by_fp[fp_key] = active_ts
    direction["followup_investigation_attempts_by_fingerprint"] = attempts_by_fp
    direction[DIAGNOSIS_IN_PROGRESS_KEY] = True
    direction[DIAGNOSIS_STARTED_AT_KEY] = now.isoformat()
    logger.info(
        f"[DIAGNOSIS] Spawned low-confidence follow-up {new_task.id} for "
        f"direction {direction_id} (fingerprint={fp_key[:40]}, "
        f"attempt {len(active_ts)}/{_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT})"
    )
    return new_task


def _extract_partial_diagnosis(task: Task) -> Optional[Dict[str, Any]]:
    """Return salvageable diagnosis fields from a failed investigate task.

    Inspects ``task.plan_output`` for the standard report keys (``summary``,
    ``root_cause``, ``affected_files``). Returns ``None`` if none are present.
    """
    raw = task.plan_output
    if not isinstance(raw, dict):
        return None
    summary = str(raw.get("summary") or "").strip()
    root_cause = str(raw.get("root_cause") or "").strip()
    affected_paths: List[str] = []
    for entry in raw.get("affected_files") or []:
        p = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(p, str) and p.strip():
            affected_paths.append(p.strip())
    seen: set = set()
    affected_paths = [p for p in affected_paths if not (p in seen or seen.add(p))]
    if not (summary or root_cause or affected_paths):
        return None
    return {
        "summary": summary[:500],
        "root_cause": root_cause[:2000],
        "affected_files": affected_paths[:20],
    }


async def _maybe_spawn_reinvestigation_after_failure(
    *,
    investigation_task: Task,
    project: Project,
    direction: Dict[str, Any],
    session: AsyncSession,
) -> Optional[Task]:
    """Spawn a re-investigation after an investigate task terminates FAILED on infra.

    Mirrors the 24h-dedup / per-fingerprint-cap / diagnosis_in_progress guards
    used by remediation-failure and low-confidence follow-ups. Caller must have
    already cleared ``diagnosis_in_progress`` on ``direction`` — an infra crash
    leaves the flag pinned unless something clears it.
    """
    direction_id = direction.get("id")
    if not direction_id or direction.get(DIAGNOSIS_IN_PROGRESS_KEY):
        return None
    cutoff = _utcnow() - timedelta(hours=24)
    fingerprint = (investigation_task.context or {}).get(
        "diagnosis_fingerprint"
    ) or direction.get("error_fingerprint")
    if await _recent_followup_investigation_exists(
        project.id,
        direction_id,
        cutoff,
        fingerprint,
        session,
        exclude_task_id=investigation_task.id,
    ):
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id,
        fingerprint,
        cutoff,
        session,
        exclude_task_id=investigation_task.id,
    )
    if shared_id is not None:
        _append_followup_dedup_note(direction, fingerprint, shared_id)
        await _persist_direction_field_update(session, project, direction)
        logger.info(
            f"[DIAGNOSIS] Skip reinvestigation for direction {direction_id}: "
            f"shares fingerprint {str(fingerprint)[:40]} with investigate "
            f"task {shared_id}"
        )
        return None
    fp_key = str(fingerprint) if fingerprint else "no_fingerprint"
    attempts_by_fp = dict(
        direction.get("followup_investigation_attempts_by_fingerprint") or {}
    )
    now = _utcnow()
    active_ts = _active_followup_timestamps(attempts_by_fp.get(fp_key), now=now)
    if (
        len(active_ts) >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
        or _project_active_followup_count(project, fp_key, now=now)
        >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
    ):
        logger.info(
            f"[DIAGNOSIS] Skip reinvestigation for direction {direction_id}: "
            f"cap reached for fingerprint {fp_key[:40]} "
            f"(direction={len(active_ts)}, "
            f"cap={_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT})"
        )
        await _maybe_escalate_exhausted_followups(
            project=project,
            direction=direction,
            fp_key=fp_key,
            session=session,
        )
        return None
    prior_summary = (direction.get("diagnosis_summary") or "").strip()
    failure_summary = investigation_task.latest_status_message or "(no error message)"
    from app.failure_classification import classify_failure  # noqa: PLC0415

    failure_class = classify_failure(
        investigation_task,
        at_time=investigation_task.last_heartbeat or investigation_task.started_at,
    )
    partial_summary = (direction.get("partial_diagnosis_summary") or "").strip()
    partial_root_cause = (direction.get("partial_diagnosis_root_cause") or "").strip()
    partial_files = direction.get("partial_diagnosis_files") or []
    partial_block = ""
    if partial_summary or partial_root_cause or partial_files:
        partial_block = "\n".join(
            [
                "",
                "Partial findings from the prior (incomplete) investigation:",
                f"- summary: {partial_summary[:500] or '(none)'}",
                f"- root_cause: {partial_root_cause[:1000] or '(none)'}",
                f"- affected_files: {', '.join(partial_files[:20]) or '(none)'}",
                "Build on these findings rather than re-investigating from scratch.",
            ]
        )
    prompt = "\n".join(
        [
            "A prior investigation on this direction did not complete — the "
            "investigate task terminated before producing a final diagnosis. "
            "Re-run the investigation, leveraging any partial findings below.",
            "",
            f"Direction: {(direction.get('text') or '')[:200]}",
            f"Direction id: {direction_id}",
            "",
            f"Prior diagnosis summary (if any):\n{prior_summary[:1500] or '(none recorded)'}",
            partial_block,
            "",
            f"Previous investigation failure class: {failure_class}",
            f"Previous investigation error summary: {str(failure_summary)[:400]}",
            "",
            "Produce a diagnosis (root cause, affected files, recommended fix) with "
            "HIGH or MEDIUM confidence, or explicitly declare that code signals "
            "alone are insufficient and manual intervention is required.",
        ]
    )[:20000]
    inv_ctx = investigation_task.context or {}
    original_diagnosis_task_id = inv_ctx.get("original_diagnosis_task_id") or str(
        investigation_task.id
    )
    task_ctx: Dict[str, Any] = {
        "direction_id": str(direction_id),
        "diagnosis_trigger": "investigation_failure_followup",
        "previous_investigation_task_id": str(investigation_task.id),
        "original_diagnosis_task_id": str(original_diagnosis_task_id),
    }
    if fingerprint:
        task_ctx["diagnosis_fingerprint"] = str(fingerprint)
        task_ctx["failure_fingerprints"] = [str(fingerprint)]
    inherited_failure_class = inv_ctx.get("last_failure_class")
    if inherited_failure_class:
        task_ctx["last_failure_class"] = inherited_failure_class
    inherited_failure_taxonomy = inv_ctx.get("failure_taxonomy")
    if inherited_failure_taxonomy:
        task_ctx["failure_taxonomy"] = inherited_failure_taxonomy
    new_task = Task(
        project_id=project.id,
        title=f"Re-investigate after failure: {(direction.get('text') or '')[:50]}",
        description=(
            f"Follow-up after investigation task "
            f"{str(investigation_task.id)[:8]} failed ({failure_class})"
        )[:500],
        payloads=[
            validate_internal_payload(
                {
                    "type": "investigate",
                    "prompt": prompt,
                    "base_branch": get_project_integration_branch(project),
                },
                "task_completion:investigation_failure_followup",
            )
        ],
        context=task_ctx,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=70,
    )
    session.add(new_task)
    active_ts.append(now.isoformat())
    attempts_by_fp[fp_key] = active_ts
    direction["followup_investigation_attempts_by_fingerprint"] = attempts_by_fp
    direction[DIAGNOSIS_IN_PROGRESS_KEY] = True
    direction[DIAGNOSIS_STARTED_AT_KEY] = now.isoformat()
    logger.info(
        f"[DIAGNOSIS] Spawned investigation-failure follow-up {new_task.id} for "
        f"direction {direction_id} (fingerprint={fp_key[:40]}, "
        f"class={failure_class}, "
        f"attempt {len(active_ts)}/{_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT})"
    )
    return new_task


async def _handle_investigation_terminal_failure(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Clear diagnosis state + optionally re-spawn when an investigate task terminates FAILED/CANCELLED.

    ``_handle_investigation_completion`` only runs on COMPLETED status, so a
    crashed or cancelled investigate task leaves the linked direction pinned
    with ``diagnosis_in_progress=True`` until the 4h stale-diagnosis timer in
    ``direction_lifecycle`` runs. This handler short-circuits that wait:

      1. Clear ``diagnosis_in_progress`` / ``diagnosis_started_at`` /
         ``diagnosis_task_id`` on the linked direction and stamp
         ``diagnosis_completed_at`` so future auto-pause diagnoses can fire.
      2. On FAILED status only, when the failure class is an infra class
         (VM crash, timeout, rate limit, etc.) — not a CODE_ERROR from a
         broken investigate agent — spawn a re-investigation subject to the
         same cap + dedup guards as the other follow-up helpers.
    """
    if status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
    payload = task.first_payload or {}
    if payload.get("type") != "investigate":
        return
    ctx = task.context or {}
    direction_id = ctx.get("direction_id")
    if not direction_id:
        return
    try:
        project = await session.get(Project, task.project_id)
        if project is None:
            return
        directions_list = list(project.directions or [])
        target = next((d for d in directions_list if d.get("id") == direction_id), None)
        if target is None:
            return
        had_flag = bool(target.get(DIAGNOSIS_IN_PROGRESS_KEY))
        partial = _extract_partial_diagnosis(task)
        if partial:
            target["partial_diagnosis_summary"] = partial["summary"]
            target["partial_diagnosis_root_cause"] = partial["root_cause"]
            target["partial_diagnosis_files"] = partial["affected_files"]
            target["partial_diagnosis_task_id"] = str(task.id)
            target["partial_diagnosis_recorded_at"] = _utcnow().isoformat()
            logger.info(
                f"[DIAGNOSIS] Salvaged partial diagnosis from failed investigate "
                f"task {task.id} on direction {direction_id} "
                f"(files={len(partial['affected_files'])}, "
                f"has_root_cause={bool(partial['root_cause'])})"
            )
        target.pop(DIAGNOSIS_IN_PROGRESS_KEY, None)
        target.pop(DIAGNOSIS_STARTED_AT_KEY, None)
        target.pop(DIAGNOSIS_TASK_ID_KEY, None)
        target["diagnosis_completed_at"] = _utcnow().isoformat()
        await _persist_direction_field_update(session, project, target)
        if had_flag:
            logger.info(
                f"[DIAGNOSIS] Cleared diagnosis_in_progress on direction "
                f"{direction_id} after investigate task {task.id} terminated "
                f"{status}"
            )
        if status != TaskStatus.FAILED:
            return
        from app.failure_classification import classify_failure, INFRA_CLASSES  # noqa: PLC0415

        failure_class = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )
        has_salvageable_root_cause = bool(partial and partial.get("root_cause"))
        if failure_class not in INFRA_CLASSES and not has_salvageable_root_cause:
            return

        # If the partial diagnosis is actionable (root_cause + files) and the
        # failure was infra (not a broken agent producing garbage), try to
        # spawn a remediation directly from the partial findings rather than
        # waiting for a re-investigation to complete. Confidence is capped at
        # "medium" so _calculate_remediation_scope keeps the change small.
        partial_spawn: Optional[Task] = None
        partial_actionable = (
            failure_class in INFRA_CLASSES
            and has_salvageable_root_cause
            and bool(partial.get("affected_files"))
        )
        if partial_actionable:
            partial_spawn = await _spawn_investigation_remediation(
                task=task,
                direction_id=str(direction_id),
                summary=partial.get("summary") or "",
                root_cause=partial["root_cause"],
                recommended_fix=partial["root_cause"],
                severity="medium",
                confidence="medium",
                affected_paths=list(partial.get("affected_files") or []),
                remediation_attempts=int(target.get("remediation_attempts") or 0),
                session=session,
            )
            if partial_spawn is not None:
                spawn_ctx = dict(partial_spawn.context or {})
                spawn_ctx["diagnosis_source"] = "partial"
                spawn_ctx["diagnosis_trigger"] = "partial_diagnosis_remediation"
                partial_spawn.context = spawn_ctx
                flag_modified(partial_spawn, "context")
                session.add(partial_spawn)
                _stamp_remediation_spawn_on_direction(target, partial_spawn)
                target["last_diagnosis_outcome"] = "partial_remediation_spawned"
                clear_direction_hold(str(direction_id))
                await _persist_direction_field_update(session, project, target)
                logger.info(
                    f"[DIAGNOSIS] Spawned partial-diagnosis remediation "
                    f"{partial_spawn.id} for direction {direction_id} from "
                    f"failed investigate task {task.id} "
                    f"(files={len(partial['affected_files'])})"
                )
                return

        spawned = await _maybe_spawn_reinvestigation_after_failure(
            investigation_task=task,
            project=project,
            direction=target,
            session=session,
        )
        if spawned is not None:
            await _persist_direction_field_update(session, project, target)
    except Exception as exc:
        logger.warning(
            f"[DIAGNOSIS] Failed to handle investigation terminal failure for "
            f"task {task.id}: {exc!r}"
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
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

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
    """Release or maintain the cross-cutting merge gate when a safeguard audit completes.

    No-ops immediately if the task is not a safeguard (no safeguard_for_task_id in context).
    Releases the gate (clears orig.requires_approval) when no critical/high findings exist.
    If the original task is already EVALUATING (review approved while safeguard was pending),
    sets review_loop.ci_waiting=True so the orchestrator's CI loop attempts the merge.
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
    if orig.status == TaskStatus.EVALUATING and pr_num:
        # Review already approved while safeguard was pending; hand off to CI polling loop.
        orig_ctx["review_loop"] = {
            **(orig_ctx.get("review_loop") or {}),
            "ci_waiting": True,
            "pr_number": pr_num,
        }
        orig.context = orig_ctx
        flag_modified(orig, "context")
    orig.latest_status_message = (
        f"Cross-cutting safeguard passed — merge gate released (PR #{pr_num})"
    )
    session.add(orig)
    await session.commit()
    logger.info(f"[SAFEGUARD] Gate released on {safeguard_for_id[:8]} for PR #{pr_num}")


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
        task.plan_output = []
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, findings, session)
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)
    await session.commit()

    base_branch = (task.first_payload).get("base_branch", "main")

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
        task.plan_output = []
        session.add(task)
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)

    base_branch = (task.first_payload).get("base_branch", "main")

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


# ---------------------------------------------------------------------------
# Self audit completion — ingest SELF_AUDIT_REPORT into SystemMaturity rows
# ---------------------------------------------------------------------------

# Allowed values for SystemMaturity.recommended_mode. Rows with a value outside
# this set are dropped at ingest with a logged warning rather than persisted.
_VALID_SELF_AUDIT_MODES = {"audit", "fundamentals", "ship", "pause"}
_SELF_AUDIT_PROPAGATED_MODES = {"audit", "fundamentals", "pause"}
_MODE_STRICTNESS = {"ship": 0, "fundamentals": 1, "audit": 2, "pause": 3}
_SHIP_RECOVERY_THRESHOLD = (
    2  # consecutive ship assessments before auto-clearing audit/fundamentals
)
_AUDIT_CLEARABLE_MODES = {"audit", "fundamentals"}

# Per-component scores must be 0-100 integers; we don't enforce a DB check
# constraint, so we clamp here to keep downstream consumers safe.
_SELF_AUDIT_COMPONENT_KEYS = (
    "test_coverage",
    "friction_density",
    "claim_consistency",
    "failure_mix",
)


def _parse_self_audit_findings(logs: str) -> List[Dict[str, Any]]:
    """Pull a list of self-audit findings out of a task's status logs.

    The agent writes either ``{"findings": [...]}`` or the bare array. Tolerate
    both, plus surrounding text, by trying a JSON object decode first, then
    falling back to bracket-depth array extraction keyed on ``system_id``.
    Returns ``[]`` when nothing parseable is found.
    """
    if not logs:
        return []
    text = logs[:500_000]

    # Direct JSON object decode (the common case — agent stringifies a dict).
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        findings = parsed.get("findings")
        if isinstance(findings, list):
            return findings
    if isinstance(parsed, list):
        return parsed

    # Fallback: extract a JSON array of objects containing "system_id".
    arr_str = _extract_json_array(text, "system_id")
    if arr_str:
        try:
            arr = json.loads(arr_str)
            if isinstance(arr, list):
                return arr
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _normalize_self_audit_finding(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate and normalize one self-audit finding.

    Returns a clean dict or ``None`` when the finding is malformed (missing
    system_id, invalid recommended_mode, non-integer score). Callers should log
    a warning when this returns ``None``.
    """
    if not isinstance(raw, dict):
        return None
    system_id = raw.get("system_id")
    if not isinstance(system_id, str) or not system_id.strip():
        return None
    mode = raw.get("recommended_mode")
    if mode is not None and mode not in _VALID_SELF_AUDIT_MODES:
        return None
    try:
        score = int(raw.get("score", 0))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))
    components_raw = raw.get("components") or {}
    components: Dict[str, int] = {}
    if isinstance(components_raw, dict):
        for key in _SELF_AUDIT_COMPONENT_KEYS:
            try:
                components[key] = max(0, min(100, int(components_raw.get(key, 0))))
            except (TypeError, ValueError):
                components[key] = 0
    top_issues = raw.get("top_issues") or []
    if not isinstance(top_issues, list):
        top_issues = []
    # Normalize each issue to the {issue: str} shape so downstream consumers
    # don't have to handle plain-string vs dict variations.
    top_issues_norm: List[Dict[str, Any]] = []
    for it in top_issues[:10]:
        if isinstance(it, str):
            top_issues_norm.append({"issue": it})
        elif isinstance(it, dict):
            top_issues_norm.append(it)
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    evidence_norm: List[Dict[str, Any]] = [
        e for e in evidence[:20] if isinstance(e, dict)
    ]
    return {
        "system_id": system_id.strip()[:200],
        "score": score,
        "components": components,
        "recommended_mode": mode,
        "top_issues": top_issues_norm,
        "evidence": evidence_norm,
    }


def _maybe_auto_clear_direction_mode(
    directions: List[Dict[str, Any]],
    system_id: str,
    consecutive_ship_count: int,
    source_task_id: str,
    now: datetime,
) -> bool:
    """Auto-clear audit/fundamentals direction mode when maturity fully recovers.

    Only clears modes that were auto-set via self_audit — strategic-set and
    operator-set modes (any other source) are left untouched.  Returns True if
    any direction was changed.
    """
    if consecutive_ship_count < _SHIP_RECOVERY_THRESHOLD:
        return False
    changed = False
    for d in directions:
        if d.get("status") != "active" or d.get("system_id") != system_id:
            continue
        current_mode = str(d.get("recommended_mode") or "").strip().lower()
        if current_mode not in _AUDIT_CLEARABLE_MODES:
            continue
        meta = d.get(RECOMMENDED_MODE_META_KEY) or {}
        if (
            not isinstance(meta, dict)
            or meta.get("source") != RECOMMENDED_MODE_SOURCE_SELF_AUDIT
        ):
            continue
        d["recommended_mode"] = None
        d[RECOMMENDED_MODE_META_KEY] = {
            "mode": None,
            "source": RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
            "source_task_id": source_task_id,
            "set_at": now.isoformat(),
            "cleared_reason": (
                f"Auto-cleared after {consecutive_ship_count} consecutive 'ship' "
                "maturity assessments — system health recovered"
            ),
        }
        _record_auto_clear_note(d, current_mode, consecutive_ship_count)
        changed = True
    return changed


def _is_recent_strategic_mode_lock(
    direction: Dict[str, Any], incoming_mode: str, now: datetime
) -> bool:
    meta = direction.get(RECOMMENDED_MODE_META_KEY) or {}
    if (
        not isinstance(meta, dict)
        or meta.get("source") != RECOMMENDED_MODE_SOURCE_STRATEGIC
    ):
        return False
    existing = str(direction.get("recommended_mode") or "").strip().lower()
    incoming_rank = _MODE_STRICTNESS.get(incoming_mode, -1)
    existing_rank = _MODE_STRICTNESS.get(existing, -1)
    if existing_rank <= incoming_rank:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        set_at = datetime.fromisoformat(
            str(meta.get("set_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if set_at.tzinfo is None:
        # Backward-compatible guard for older strategic metadata that stored
        # naive timestamps: keep the stricter strategic recommendation.
        return True
    if now - set_at > timedelta(hours=6):
        return False
    return True


async def _handle_self_audit_completion(task: Task, logs: str, session: AsyncSession):
    """Ingest a SELF_AUDIT_REPORT into SystemMaturity rows.

    Idempotent: re-running an audit for the same (project_id, system_id) updates
    the existing row in place (overwriting score/components/issues/evidence and
    bumping last_audited_at + source_task_id). Findings with invalid
    recommended_mode or missing system_id are dropped with a warning so a single
    bad entry doesn't sink the whole batch.
    """
    raw_findings = _parse_self_audit_findings(logs)
    if not raw_findings:
        task.latest_status_message = "Self audit completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    normalized: List[Dict[str, Any]] = []
    dropped = 0
    for raw in raw_findings:
        norm = _normalize_self_audit_finding(raw if isinstance(raw, dict) else {})
        if norm is None:
            dropped += 1
            logger.warning(
                f"[SELF_AUDIT] Task {task.id}: dropping malformed finding "
                f"(invalid system_id/mode/score): {str(raw)[:200]}"
            )
            continue
        normalized.append(norm)

    # Persist what we parsed for the admin UI even if everything else fails.
    task.plan_output = normalized
    session.add(task)

    upserted = 0
    project = await session.get(Project, task.project_id)
    # Collect (mode, system_id, consecutive_ship_count) for findings that may
    # propagate to directions; the actual directions read-modify-write is
    # serialized once, after the loop, through the canonical row lock so a
    # concurrent same-project completion is not clobbered.
    propagation_inputs: List[tuple] = []
    for finding in normalized:
        system_id = finding["system_id"]
        mode = finding["recommended_mode"]
        existing = (
            (
                await session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == task.project_id,
                        SystemMaturity.system_id == system_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        now = _utcnow()
        # Track consecutive ship assessments so we can auto-clear recovered directions.
        if existing is not None:
            prev_mode = existing.recommended_mode
            prev_ship_count = int(
                (existing.components or {}).get("_consecutive_ship_count", 0)
            )
        else:
            prev_mode = None
            prev_ship_count = 0
        if mode == "ship":
            consecutive_ship_count = prev_ship_count + 1 if prev_mode == "ship" else 1
        else:
            consecutive_ship_count = 0
        new_components = dict(finding["components"])
        new_components["_consecutive_ship_count"] = consecutive_ship_count
        if existing is not None:
            existing.score = finding["score"]
            existing.components = new_components
            existing.recommended_mode = finding["recommended_mode"]
            existing.top_issues = finding["top_issues"]
            existing.evidence = finding["evidence"]
            existing.last_audited_at = now
            existing.source_task_id = task.id
            flag_modified(existing, "components")
            flag_modified(existing, "top_issues")
            flag_modified(existing, "evidence")
            session.add(existing)
        else:
            session.add(
                SystemMaturity(
                    project_id=task.project_id,
                    system_id=system_id,
                    score=finding["score"],
                    components=new_components,
                    recommended_mode=finding["recommended_mode"],
                    top_issues=finding["top_issues"],
                    evidence=finding["evidence"],
                    last_audited_at=now,
                    source_task_id=task.id,
                )
            )
        upserted += 1
        if mode in _SELF_AUDIT_PROPAGATED_MODES or mode == "ship":
            propagation_inputs.append((mode, system_id, consecutive_ship_count))

    if project and propagation_inputs:

        def _propagate_modes(directions: List[Dict[str, Any]]):
            for mode_, system_id_, csc_ in propagation_inputs:
                now_ = _utcnow()
                if mode_ in _SELF_AUDIT_PROPAGATED_MODES:
                    for d in directions:
                        if (
                            d.get("status") != "active"
                            or d.get("system_id") != system_id_
                        ):
                            continue
                        if _is_recent_strategic_mode_lock(d, mode_, now_):
                            continue
                        d["recommended_mode"] = mode_
                        d[RECOMMENDED_MODE_META_KEY] = {
                            "mode": mode_,
                            "source": RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
                            "source_task_id": str(task.id),
                            "set_at": now_.isoformat(),
                        }
                elif mode_ == "ship":
                    _maybe_auto_clear_direction_mode(
                        directions, system_id_, csc_, str(task.id), now_
                    )
            return None

        # Drop the cached directions snapshot (expire_on_commit=False) so the
        # helper reads the row fresh under the lock.
        session.expire(project, ["directions"])
        await with_directions_locked(session, task.project_id, _propagate_modes)

    summary = f"Self audit ingested {upserted} system(s)"
    if dropped:
        summary += f", dropped {dropped} malformed"
    task.latest_status_message = summary
    session.add(task)
    await session.commit()

    await broadcast_to_project(
        str(task.project_id),
        "autonomous_activity",
        {"self_audit_task_id": str(task.id), "systems_ingested": upserted},
    )
    logger.info(
        f"[SELF_AUDIT] Task {task.id}: upserted {upserted} maturity row(s), dropped {dropped}"
    )


# Phrases that unambiguously indicate a structural (direction-level) diagnosis
# regardless of surrounding context.
_STRUCTURAL_STRONG_KEYWORDS = (
    "fundamentally misaligned",
    "direction scope",
    "direction is too broad",
    "direction is too wide",
    "narrow the direction",
    "split the direction",
    "rewrite the direction",
)

# Weaker keywords — scope-related language that only signals a structural
# diagnosis when paired with a direction/goal-level context word. Without the
# context gate, phrases like "validation scope too narrow" or "out of scope for
# this module" get misclassified as structural.
_STRUCTURAL_WEAK_KEYWORDS = (
    "too broad",
    "too wide",
    "overly broad",
    "scope too",
    "misaligned",
    "out of scope",
    "narrow the scope",
)

_STRUCTURAL_CONTEXT_WORDS = ("direction", "goal", "vision")


def _is_structural_diagnosis(root_cause: str, recommended_fix: str) -> bool:
    blob = f"{root_cause}\n{recommended_fix}".lower()
    if any(kw in blob for kw in _STRUCTURAL_STRONG_KEYWORDS):
        return True
    if not any(w in blob for w in _STRUCTURAL_CONTEXT_WORDS):
        return False
    return any(kw in blob for kw in _STRUCTURAL_WEAK_KEYWORDS)


# Stopwords trimmed from root-cause divergence comparison so generic English
# scaffolding ("the", "this", "test", etc.) doesn't inflate the apparent
# overlap between diagnoses that actually disagree on substance.
_DIVERGENCE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "into",
        "have",
        "been",
        "will",
        "their",
        "there",
        "these",
        "those",
        "what",
        "when",
        "which",
        "while",
        "after",
        "because",
        "could",
        "should",
        "would",
        "shall",
        "might",
        "test",
        "tests",
        "code",
        "fail",
        "fails",
        "failed",
        "failing",
        "issue",
        "issues",
        "error",
        "errors",
        "task",
        "tasks",
        "task_id",
        "regression",
        "investigation",
        "diagnosis",
    }
)


def _normalize_path_for_divergence(path: str) -> str:
    """Strip whitespace/quotes and lowercase a file path for set comparison."""
    return path.strip().strip('"').strip("'").lstrip("./").lower()


def _tokenize_for_divergence(text: str) -> set[str]:
    """Return lowercased alphanumeric tokens of length >= 4 minus stopwords."""
    if not text:
        return set()
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) >= 4 and t not in _DIVERGENCE_STOPWORDS}


def _jaccard(a: set, b: set) -> Optional[float]:
    """Jaccard similarity, or None when both sets are empty (undefined)."""
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _compute_diagnosis_divergence(
    *,
    prior_root: str,
    prior_paths: List[str],
    new_root: str,
    new_paths: List[str],
) -> tuple[bool, str]:
    """Pure helper: does the new diagnosis diverge from the prior one?

    Returns (diverged, detail). Diverged when EITHER affected-file Jaccard
    < 0.30 OR root-cause token-set Jaccard < 0.15. ``detail`` is a short
    human-readable string for the operator alert.
    """
    prior_path_set = {_normalize_path_for_divergence(p) for p in prior_paths if p}
    new_path_set = {_normalize_path_for_divergence(p) for p in new_paths if p}
    path_jaccard = _jaccard(prior_path_set, new_path_set)
    root_jaccard = _jaccard(
        _tokenize_for_divergence(prior_root),
        _tokenize_for_divergence(new_root),
    )
    if path_jaccard is not None and path_jaccard < 0.30:
        return True, f"affected-file overlap {path_jaccard:.2f} < 0.30."
    if root_jaccard is not None and root_jaccard < 0.15:
        return True, f"root-cause token overlap {root_jaccard:.2f} < 0.15."
    return False, ""


async def _maybe_escalate_diagnosis_divergence(
    *,
    task: Task,
    new_root_cause: str,
    new_affected_paths: List[str],
    session: AsyncSession,
) -> bool:
    """Detect divergence between this and the prior pipeline-regression
    investigation; escalate the case to NEEDS_MANUAL_INTERVENTION if so.

    Returns True when the case was escalated (caller must skip remediation
    spawning) and False otherwise. Best-effort: any missing linkage / empty
    prior diagnosis returns False so the existing spawn path proceeds.
    """
    parent_ctx = task.context or {}
    if parent_ctx.get("spawned_by") != "pipeline_regression_dispatcher":
        return False
    regression_class = parent_ctx.get("regression_class")
    if not regression_class:
        return False

    from app.models import (  # noqa: PLC0415
        Alert,
        AlertSeverity,
        RegressionCaseStatus,
        RegressionRemediationCase,
    )

    case_result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == task.project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = case_result.scalar_one_or_none()
    if case is None:
        return False
    if (case.attempts or 0) < 2:
        return False
    if case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
        return False

    prior_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status == TaskStatus.COMPLETED,
            Task.context["regression_class"].as_string() == str(regression_class),
        )
        .order_by(Task.updated_at.desc())
        .limit(1)
    )
    prior_task = prior_result.scalar_one_or_none()
    if prior_task is None:
        return False

    prior_output = prior_task.plan_output or {}
    if not isinstance(prior_output, dict) or not prior_output:
        return False
    prior_root = str(prior_output.get("root_cause") or "")
    prior_affected_raw = prior_output.get("affected_files") or []
    prior_paths: List[str] = []
    for entry in prior_affected_raw if isinstance(prior_affected_raw, list) else []:
        p = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(p, str) and p.strip():
            prior_paths.append(p.strip())
    if not (prior_root or prior_paths):
        return False

    diverged, detail = _compute_diagnosis_divergence(
        prior_root=prior_root,
        prior_paths=prior_paths,
        new_root=new_root_cause,
        new_paths=new_affected_paths,
    )
    if not diverged:
        return False

    case.escalate_for_divergence()
    session.add(case)
    session.add(
        Alert(
            project_id=task.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Regression case escalated for diagnosis divergence: {regression_class}"[
                :200
            ],
            message=(
                "investigations diverged: structural/environmental cause likely. "
                f"{detail} Prior investigation {prior_task.id} and current investigation "
                f"{task.id} produced incompatible diagnoses for {regression_class} "
                f"after {case.attempts} attempts. Manual review required."
            )[:1000],
        )
    )
    logger.warning(
        "[INVESTIGATION_DIVERGENCE] project=%s class=%s task=%s prior_task=%s "
        "attempts=%d — case escalated to NEEDS_MANUAL_INTERVENTION (%s)",
        task.project_id,
        regression_class,
        task.id,
        prior_task.id,
        case.attempts,
        detail,
    )
    return True


# Dedup window for regression false-positive memos. Avoids creating
# multiple memos for the same regression_class if it re-arms after the TTL.
_REGRESSION_FALSE_POSITIVE_MEMO_DEDUP_DAYS = 30


async def _spawn_regression_false_positive_memo(
    *,
    task: Task,
    regression_class: str,
    verdict_reason: str,
    case_id,
    session: AsyncSession,
) -> Optional[Task]:
    """Spawn a memo task documenting the regression false-positive reasoning.

    Preserves the verdict_reason and investigation summary as citable artifacts
    so strategic cycles can reference the reasoning. Dedup guard: skips if a
    memo for this regression_class was created in the last 30 days.

    Returns the spawned memo task on success, or None if deduped/failed.
    """
    thirty_days_ago = _utcnow() - timedelta(
        days=_REGRESSION_FALSE_POSITIVE_MEMO_DEDUP_DAYS
    )
    existing = (
        await session.execute(
            select(Task).where(
                Task.project_id == task.project_id,
                Task.created_at >= thirty_days_ago,
                Task.created_by == "ORCHESTRATOR",
                Task.context["memo_reasoning_kind"].as_string()
                == "regression_false_positive",
                Task.context["memo_regression_class"].as_string() == regression_class,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.debug(
            "[INVESTIGATION_FALSE_POSITIVE] Skipping duplicate memo for class %s; "
            "existing memo %s created within %d days",
            regression_class,
            existing.id,
            _REGRESSION_FALSE_POSITIVE_MEMO_DEDUP_DAYS,
        )
        return None

    # Prepare memo payload with reasoning
    investigation_summary = (task.first_payload or {}).get("prompt", "")[
        :500
    ] or "(no summary)"
    direction_id = (task.context or {}).get("direction_id")
    memo_topic = f"Regression {regression_class[:80]}: not a real regression"
    memo_payload = validate_internal_payload(
        {
            "type": "memo",
            "topic": memo_topic,
            "reasoning_kind": "regression_false_positive",
            "source_task_id": str(task.id),
            **({"direction_id": direction_id} if direction_id else {}),
            "source_finding": {
                "regression_class": regression_class,
                "verdict_reason": verdict_reason[:1000] or "(no reason provided)",
                "investigation_summary": investigation_summary,
                "case_id": str(case_id),
            },
        },
        "task_completion:memo:regression_false_positive",
    )
    memo_task = Task(
        project_id=task.project_id,
        title=f"Memo: {memo_topic[:100]}",
        description="Reasoning memo for false-positive regression determination",
        parent_task_id=task.id,
        payloads=[memo_payload],
        context={
            "memo_reasoning_kind": "regression_false_positive",
            "memo_regression_class": regression_class,
            "memo_case_id": str(case_id),
            "memo_source_task_id": str(task.id),
            **({"direction_id": direction_id} if direction_id else {}),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=TASK_PRIORITY_MAP.get("low", 0),
    )
    session.add(memo_task)
    _synth = _synthesize_memo_from_source_finding(
        "regression_false_positive",
        memo_payload.get("source_finding") or {},
        str(task.id),
    )
    if _synth:
        _apply_synthesized_memo(memo_task, _synth, session)
    logger.info(
        "[INVESTIGATION_FALSE_POSITIVE] Spawned memo task %s for regression class %s "
        "(case_id=%s, investigation_task=%s, synthesized=%s)",
        memo_task.id,
        regression_class,
        case_id,
        task.id,
        _synth is not None,
    )
    return memo_task


async def _mark_regression_case_false_positive(
    *,
    task: Task,
    verdict_reason: str,
    session: AsyncSession,
) -> None:
    """Transition the linked RegressionRemediationCase to FALSE_POSITIVE.

    Triggered from ``_handle_investigation_completion`` when an investigate
    task spawned by the pipeline-regression dispatcher reports
    ``verdict == "not_a_regression"``. Stamps the suppression clock so the
    dispatcher silently skips re-spawning the same regression class for the
    TTL window (see ``_REGRESSION_FALSE_POSITIVE_TTL`` in
    ``pipeline_regression_dispatcher``). After the TTL lapses the dispatcher
    re-arms the case to ACTIVE so a genuine recurrence is still caught.

    Also spawns a memo task to preserve the verdict reasoning as a citable artifact.

    No-ops (without raising) when the matching case row cannot be found —
    the dispatcher rebuilds case rows on next spawn anyway, so a missing row
    just means the suppression won't survive past the next true trip.
    """
    from app.models import (  # noqa: PLC0415
        Alert,
        AlertSeverity,
        RegressionCaseStatus,
        RegressionRemediationCase,
    )
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        _REGRESSION_FALSE_POSITIVE_TTL,
    )

    ctx = task.context or {}
    regression_class = ctx.get("regression_class")
    if not regression_class:
        logger.warning(
            "[INVESTIGATION_FALSE_POSITIVE] task=%s missing regression_class in "
            "context — cannot mark case suppressed",
            task.id,
        )
        return
    case_row = (
        await session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == task.project_id,
                RegressionRemediationCase.regression_class == regression_class,
            )
        )
    ).scalar_one_or_none()
    if case_row is None:
        logger.warning(
            "[INVESTIGATION_FALSE_POSITIVE] task=%s class=%s no matching case "
            "row — verdict recorded on task only",
            task.id,
            regression_class,
        )
        return

    now = _utcnow()
    suppressed_until = now + _REGRESSION_FALSE_POSITIVE_TTL
    reason = (verdict_reason or "")[:500]

    # Spawn memo task BEFORE updating case state. If memo spawn fails, the Alert
    # will still fire and operator will know the case was marked FALSE_POSITIVE.
    try:
        await _spawn_regression_false_positive_memo(
            task=task,
            regression_class=regression_class,
            verdict_reason=reason,
            case_id=case_row.id,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[INVESTIGATION_FALSE_POSITIVE] Failed to spawn memo for class %s: %r; "
            "continuing with case update",
            regression_class,
            exc,
        )

    case_row.status = RegressionCaseStatus.FALSE_POSITIVE
    case_row.false_positive_until_at = suppressed_until
    case_row.false_positive_reason = reason
    case_row.last_seen_at = now
    case_row.last_remediation_result = "false_positive"
    # Clear any in-flight signal-recovery clock — the case is no longer
    # awaiting a fix, so the watchdog must not auto-close it via the
    # recovered-unattended path.
    case_row.signal_healthy_since_at = None
    session.add(case_row)
    session.add(
        Alert(
            project_id=task.project_id,
            severity=AlertSeverity.INFO,
            title=(f"Regression case marked FALSE_POSITIVE: {regression_class}")[:200],
            message=(
                f"Investigation {task.id} concluded the signal is not a real "
                f"regression. Suppressing dispatcher spawns for class "
                f"{regression_class} until {suppressed_until.isoformat()}. "
                f"Reason: {reason or '(none provided)'}"
            )[:1000],
        )
    )
    logger.info(
        "[INVESTIGATION_FALSE_POSITIVE] project=%s class=%s task=%s case_id=%s "
        "suppressed_until=%s reason=%r",
        task.project_id,
        regression_class,
        task.id,
        case_row.id,
        suppressed_until.isoformat(),
        reason[:200],
    )


async def _handle_investigation_completion(
    task: Task, payload, session: AsyncSession
) -> None:
    """Process investigation report: update diagnosis state and spawn remediation.

    The investigate agent writes a JSON report with keys:
      summary, root_cause, affected_files (list of {path, issue}),
      recommended_fix, severity, confidence.

    Steps:
      1. Parse the JSON (fall back to {"raw": logs} on error and stop).
      2. Locate the linked direction via task.context["direction_id"] and stamp
         diagnosis_completed_at / diagnosis_summary / diagnosed_affected_files,
         clearing the diagnosis_in_progress flag.
      3. Append a diagnosis insight to the direction's research_context.
      4. If the diagnosis is structural (scope/alignment), append a direction
         note instead of spawning a fix task.
      5. Otherwise, when confidence is high and recommended_fix is actionable,
         spawn a targeted `create` task carrying diagnosis_task_id /
         diagnosed_direction_id / direction_id so _record_remediation_outcome
         can stamp effectiveness after the task terminates.
    """
    logs = payload.logs or ""
    report: Optional[Dict[str, Any]] = None
    if logs:
        candidates: List[str] = [logs]
        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        fence_stripped = re.sub(
            r"^\s*```(?:json)?\s*\n?|\n?```\s*$", "", logs.strip(), flags=re.IGNORECASE
        )
        if fence_stripped and fence_stripped != logs:
            candidates.append(fence_stripped)
        used_fallback = False
        for idx, candidate in enumerate(candidates):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    report = parsed
                    if idx > 0:
                        used_fallback = True
                    break
            except (json.JSONDecodeError, ValueError):
                continue
        if report is None:
            # Last resort: extract first balanced-looking JSON object via regex
            match = re.search(r"\{.*\}", logs, flags=re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, dict):
                        report = parsed
                        used_fallback = True
                except (json.JSONDecodeError, ValueError):
                    report = None
        if used_fallback:
            logger.warning(
                "investigation_report_fallback_parse task_id=%s raw_prefix=%r",
                task.id,
                logs[:200],
            )

    if report is None:
        logger.error(
            "[INVESTIGATION] Task %s completed but report is unparseable — "
            "no remediation spawned. Output prefix: %r",
            task.id,
            logs[:200],
        )
        task.plan_output = {"raw": logs} if logs else {}
        task.status = TaskStatus.FAILED
        ctx = dict(task.context or {})
        ctx["failure_detail"] = "investigation_report_unparseable"
        task.context = ctx
        flag_modified(task, "context")
        task.latest_status_message = (
            "Investigation report unparseable — no JSON diagnosis produced "
            f"(raw_prefix={logs[:120]!r})"
            if logs
            else "Investigation produced no output — no diagnosis report available"
        )[:500]
        session.add(task)
        # Still clear diagnosis_in_progress so a malformed report doesn't wedge
        # the direction out of future auto-pause diagnosis. FAILED status lets
        # the retry budget and _maybe_spawn_reinvestigation_after_failure path
        # engage instead of silently swallowing the crash as a success.
        await _clear_direction_diagnosis_flag(
            task,
            session,
            summary=None,
            outcome="report_unparseable",
        )
        return

    task.plan_output = report
    task.status = TaskStatus.COMPLETED

    summary = str(report.get("summary") or "")[:500]
    root_cause = str(report.get("root_cause") or "")
    recommended_fix = str(report.get("recommended_fix") or "").strip()
    confidence = str(report.get("confidence") or "").lower()
    if confidence not in ("high", "medium", "low") and recommended_fix:
        logger.warning(
            "[DIAGNOSIS] Investigation %s missing/invalid confidence=%r but has "
            "recommended_fix; defaulting to 'medium'. Improve investigation prompt.",
            task.id,
            report.get("confidence"),
        )
        confidence = "medium"
    severity = str(report.get("severity") or "").lower()
    verdict = str(report.get("verdict") or "real_regression").strip().lower()
    verdict_reason = str(report.get("verdict_reason") or "").strip()
    raw_affected = report.get("affected_files") or []
    affected_paths: List[str] = []
    for entry in raw_affected if isinstance(raw_affected, list) else []:
        if isinstance(entry, dict):
            p = entry.get("path")
        else:
            p = entry
        if isinstance(p, str) and p.strip():
            affected_paths.append(p.strip())
    # De-duplicate while preserving order
    seen: set = set()
    affected_paths = [p for p in affected_paths if not (p in seen or seen.add(p))]

    direction_id = (task.context or {}).get("direction_id")
    project = await session.get(Project, task.project_id) if direction_id else None
    direction: Optional[Dict[str, Any]] = None
    directions_list: List[Dict[str, Any]] = []
    if project is not None:
        directions_list = list(project.directions or [])
        direction = next(
            (d for d in directions_list if d.get("id") == direction_id), None
        )

    # Pipeline-regression false-positive short-circuit. When the agent
    # concluded the tripped signal isn't actually a regression (intended
    # behavior, statistical noise, known-incomplete pipeline step), mark the
    # RegressionRemediationCase as FALSE_POSITIVE so the dispatcher
    # suppresses re-spawning the same class for the TTL window. We MUST run
    # this before the remediation-spawn branches below, otherwise a verdict
    # disagreement would still spawn a fix and undo the suppression.
    spawned_by = (task.context or {}).get("spawned_by")
    if verdict == "not_a_regression" and spawned_by == "pipeline_regression_dispatcher":
        await _mark_regression_case_false_positive(
            task=task,
            verdict_reason=verdict_reason or summary or "(no reason provided)",
            session=session,
        )
        session.add(task)
        return

    structural = _is_structural_diagnosis(root_cause, recommended_fix)
    spawn_remediation = (
        not structural and confidence in ("high", "medium") and bool(recommended_fix)
    )

    # Diverged-diagnoses early escalation. Only fires for pipeline-regression
    # investigations at attempts>=2; on hit, skips the remediation spawn so the
    # case waits for manual review instead of burning a 3rd attempt on a
    # diagnosis the next investigator will likely also disagree with.
    if spawn_remediation:
        diverged_escalated = await _maybe_escalate_diagnosis_divergence(
            task=task,
            new_root_cause=root_cause,
            new_affected_paths=affected_paths,
            session=session,
        )
        if diverged_escalated:
            session.add(task)
            logger.info(
                "[INVESTIGATION] Task %s skipping remediation spawn — case "
                "escalated for diagnosis divergence",
                task.id,
            )
            return

    spawned_child: Optional[Task] = None
    if spawn_remediation and direction is not None:
        spawned_child = await _spawn_investigation_remediation(
            task=task,
            direction_id=str(direction_id),
            summary=summary,
            root_cause=root_cause,
            recommended_fix=recommended_fix,
            severity=severity,
            confidence=confidence,
            affected_paths=affected_paths,
            remediation_attempts=int(direction.get("remediation_attempts") or 0),
            session=session,
        )
        if spawned_child is not None:
            clear_direction_hold(str(direction_id))
            logger.info(
                "[DIRECTION HEALTH] Cleared hold on direction %s after "
                "investigation %s spawned remediation task %s",
                direction_id,
                task.id,
                spawned_child.id,
            )
    elif (
        spawn_remediation
        and (task.context or {}).get("spawned_by") == "crash_investigation"
    ):
        # Crash investigations have no linked direction — spawn remediation directly.
        spawned_child = await _spawn_investigation_remediation(
            task=task,
            direction_id=None,
            summary=summary,
            root_cause=root_cause,
            recommended_fix=recommended_fix,
            severity=severity,
            confidence=confidence,
            affected_paths=affected_paths,
            remediation_attempts=0,
            session=session,
        )
    elif (
        spawn_remediation
        and direction is None
        and (task.context or {}).get("spawned_by") == "pipeline_regression_dispatcher"
    ):
        # Pipeline-regression investigations are not direction-anchored — the
        # regression is system-wide. We still want the remediation chain to
        # ship a fix; pass a synthetic direction_id keyed off the regression
        # class so _record_remediation_outcome can stamp something traceable
        # without requiring a real Direction row.
        synthetic_direction = f"pipeline_regression:{(task.context or {}).get('regression_class') or 'unknown'}"
        spawned_child = await _spawn_investigation_remediation(
            task=task,
            direction_id=synthetic_direction,
            summary=summary,
            root_cause=root_cause,
            recommended_fix=recommended_fix,
            severity=severity,
            confidence=confidence,
            affected_paths=affected_paths,
            remediation_attempts=0,
            session=session,
        )
        if spawned_child is not None:
            logger.info(
                "[REGRESSION_DISPATCHER] Spawned remediation %s from pipeline "
                "regression investigation %s (class=%s)",
                spawned_child.id,
                task.id,
                synthetic_direction,
            )

    # Update direction state
    if direction is not None and project is not None:
        now_iso = _utcnow().isoformat()
        direction["diagnosis_completed_at"] = now_iso
        direction["last_diagnosis_task_id"] = str(task.id)
        if summary:
            direction["diagnosis_summary"] = summary[:STRATEGIC_RATIONALE_MAX]
        if root_cause:
            direction["diagnosis_root_cause"] = root_cause[:2000]
        if recommended_fix:
            direction["diagnosis_recommended_fix"] = recommended_fix[:2000]
        if affected_paths:
            direction["diagnosed_affected_files"] = affected_paths[:25]
        direction.pop(DIAGNOSIS_IN_PROGRESS_KEY, None)
        direction.pop(DIAGNOSIS_STARTED_AT_KEY, None)
        direction.pop(DIAGNOSIS_TASK_ID_KEY, None)
        if spawned_child is not None:
            _stamp_remediation_spawn_on_direction(direction, spawned_child)
        if structural:
            note_text = (
                f"Diagnosis ({now_iso[:10]}) flagged this direction as structurally "
                f"misaligned. Root cause: {root_cause[:400]}"
            )
            if recommended_fix:
                note_text += f" Recommendation: {recommended_fix[:400]}"
            note_text = note_text[:STRATEGIC_RATIONALE_MAX]
            notes = list(direction.get("notes") or [])
            notes.append(
                {
                    "text": note_text,
                    "added_at": now_iso,
                    "added_by_task": str(task.id),
                    "source": "investigation_diagnosis",
                }
            )
            direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]

        # Append diagnosis insight to research_context via canonical builder
        diag_line = f"Last diagnosis ({now_iso[:10]}): {(root_cause or summary)[:200]}"
        insights = [diag_line]
        if affected_paths:
            insights.append(
                "Diagnosed affected files: " + ", ".join(affected_paths[:5])
            )
        try:
            direction["research_context"] = _build_research_context(
                prior_context=direction.get("research_context"),
                finding_titles=[],
                key_finding_descriptions=[],
                cycle_insights=insights,
                history_entry={},
            )
        except Exception as ctx_exc:  # noqa: BLE001
            logger.warning(
                f"[INVESTIGATION] Failed to rebuild research_context for direction "
                f"{direction_id}: {ctx_exc!r}"
            )

        await _persist_direction_field_update(session, project, direction)

    spawned_followup: Optional[Task] = None
    if (
        spawned_child is None
        and not structural
        and confidence == "low"
        and direction is not None
        and project is not None
    ):
        spawned_followup = await _maybe_spawn_low_confidence_followup(
            investigation_task=task,
            project=project,
            direction=direction,
            session=session,
        )
        if spawned_followup is not None:
            await _persist_direction_field_update(session, project, direction)

    if direction is not None and project is not None:
        if spawned_child is not None:
            outcome = "remediation_spawned"
        elif spawned_followup is not None:
            outcome = "low_confidence_followup_spawned"
        elif structural:
            outcome = "structural"
        elif not recommended_fix:
            outcome = "no_actionable_fix"
        elif confidence not in ("high", "medium"):
            outcome = "low_confidence"
        else:
            outcome = "no_direction"
        direction["last_diagnosis_outcome"] = outcome
        await _persist_direction_field_update(session, project, direction)

    # Spawn a terminal memo for unactionable outcomes so the investigator's
    # reasoning becomes a citable artifact. Only fires when neither a
    # remediation task nor a low-confidence followup was spawned.
    # Guardrail: not_a_regression returns early above, so it never reaches here.
    if spawned_child is None and spawned_followup is None:
        _outcome_label = (
            "structural"
            if structural
            else (
                "no_actionable_fix"
                if not recommended_fix
                else (
                    "low_confidence"
                    if confidence not in ("high", "medium")
                    else "no_direction"
                )
            )
        )
        # Dedup: skip if a memo for this investigation task already exists.
        _memo_window = _utcnow() - timedelta(days=30)
        _already_has_memo = (
            await session.execute(
                select(Task.id)
                .where(
                    Task.project_id == task.project_id,
                    Task.created_at >= _memo_window,
                    Task.context["memo_source_task_id"].as_string() == str(task.id),
                    Task.context["memo_reasoning_kind"].as_string()
                    == "investigation_unactionable",
                )
                .limit(1)
            )
        ).first() is not None
        if not _already_has_memo:
            _memo_topic = (summary[:120] or f"Investigation: {_outcome_label}")[:200]
            _inv_source_finding: Dict[str, Any] = {
                "outcome": _outcome_label,
                "summary": summary[:500],
                "root_cause": root_cause[:2000],
            }
            if recommended_fix:
                _inv_source_finding["recommended_fix"] = recommended_fix[:500]
            if affected_paths:
                _inv_source_finding["affected_paths"] = affected_paths[:10]
            _inv_memo_payload = validate_internal_payload(
                {
                    "type": "memo",
                    "topic": _memo_topic,
                    "reasoning_kind": "investigation_unactionable",
                    "source_task_id": str(task.id),
                    "source_finding": _inv_source_finding,
                    **({"direction_id": direction_id} if direction_id else {}),
                },
                "task_completion:memo:investigation_unactionable",
            )
            _inv_memo_context: Dict[str, Any] = {
                "memo_source_task_id": str(task.id),
                "memo_reasoning_kind": "investigation_unactionable",
                "investigation_outcome": _outcome_label,
                "investigation_summary": summary[:500],
                "investigation_root_cause": root_cause[:500],
            }
            if recommended_fix:
                _inv_memo_context["investigation_recommended_fix"] = recommended_fix[
                    :500
                ]
            if affected_paths:
                _inv_memo_context["investigation_affected_paths"] = affected_paths[:10]
            if direction_id:
                _inv_memo_context["direction_id"] = direction_id
            _inv_memo_task = Task(
                project_id=task.project_id,
                title=f"Memo: {_memo_topic[:120]}",
                description=f"Reasoning memo for unactionable investigation ({_outcome_label})",
                parent_task_id=task.id,
                payloads=[_inv_memo_payload],
                context=_inv_memo_context,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                depends_on=[],
                priority=TASK_PRIORITY_MAP.get("low", 0),
            )
            session.add(_inv_memo_task)
            _inv_synth = _synthesize_memo_from_source_finding(
                "investigation_unactionable", _inv_source_finding, str(task.id)
            )
            if _inv_synth:
                _apply_synthesized_memo(_inv_memo_task, _inv_synth, session)
            logger.info(
                "[INVESTIGATION] Spawned memo %s for unactionable investigation %s "
                "(outcome=%s, synthesized=%s)",
                _inv_memo_task.id,
                task.id,
                _outcome_label,
                _inv_synth is not None,
            )

        # Persist verdict on the regression case so the next investigation
        # receives it as context. Only runs for dispatcher-spawned tasks.
        _spawned_by = (task.context or {}).get("spawned_by")
        if _spawned_by == "pipeline_regression_dispatcher":
            from app.models import RegressionRemediationCase  # noqa: PLC0415

            _unactionable_regression_class = (task.context or {}).get(
                "regression_class"
            )
            if _unactionable_regression_class:
                _case_result = await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == task.project_id,
                        RegressionRemediationCase.regression_class
                        == _unactionable_regression_class,
                    )
                )
                _case_row = _case_result.scalar_one_or_none()
                if _case_row is not None:
                    _case_row.last_unactionable_verdict = _outcome_label
                    _case_row.last_unactionable_summary = summary[:500] or None
                    session.add(_case_row)

    session.add(task)

    if spawned_child is not None:
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(spawned_child.id), "status": TaskStatus.QUEUED},
        )
        await broadcast_to_project(
            str(task.project_id),
            "autonomous_activity",
            {
                "investigation_task_id": str(task.id),
                "remediation_task_id": str(spawned_child.id),
                "direction_id": direction_id,
            },
        )
        logger.info(
            f"[INVESTIGATION] Spawned remediation task {spawned_child.id} from "
            f"investigation {task.id} (direction={direction_id}, "
            f"confidence={confidence}, files={len(affected_paths)})"
        )
    elif spawned_followup is not None:
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(spawned_followup.id), "status": TaskStatus.QUEUED},
        )
    else:
        reason = (
            "structural"
            if structural
            else (
                "no_actionable_fix"
                if not recommended_fix
                else (
                    "low_confidence"
                    if confidence not in ("high", "medium")
                    else "no_direction"
                )
            )
        )
        logger.info(
            f"[INVESTIGATION] Task {task.id} completed without spawning remediation "
            f"(reason={reason}, confidence={confidence}, direction={direction_id})"
        )


async def _clear_direction_diagnosis_flag(
    task: Task,
    session: AsyncSession,
    summary: Optional[str],
    outcome: Optional[str] = None,
) -> None:
    """Clear diagnosis_in_progress on the linked direction without full processing.

    Used when the investigation report is unparseable — the direction must not
    be left pinned as "diagnosis in progress" forever.
    """
    direction_id = (task.context or {}).get("direction_id")
    if not direction_id:
        return

    # Serialize the single-direction write through the canonical row lock so a
    # concurrent same-project completion mutating a different direction is not
    # clobbered. The find-and-mutate runs inside the lock against the freshly
    # loaded directions list.
    def _mutate(directions: List[Dict[str, Any]]):
        target = next((d for d in directions if d.get("id") == direction_id), None)
        if target is None:
            return None
        target.pop(DIAGNOSIS_IN_PROGRESS_KEY, None)
        target.pop(DIAGNOSIS_STARTED_AT_KEY, None)
        target.pop(DIAGNOSIS_TASK_ID_KEY, None)
        target["diagnosis_completed_at"] = _utcnow().isoformat()
        target["last_diagnosis_task_id"] = str(task.id)
        if outcome:
            target["last_diagnosis_outcome"] = outcome
        if summary:
            target["diagnosis_summary"] = summary[:STRATEGIC_RATIONALE_MAX]
        return None

    # expire_on_commit=False leaves a stale directions snapshot in the identity
    # map; drop it so with_directions_locked re-reads the row fresh under the lock.
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["directions"])
    await with_directions_locked(session, task.project_id, _mutate)


def _calculate_remediation_scope(
    affected_paths: List[str],
    severity: str,
    remediation_attempts: int,
) -> str:
    """Calibrate the remediation task's scope based on diagnosis complexity.

    Returns "small" or "large". Pipeline data shows the medium tier is a dead
    zone (near-zero merge rate) — complex enough to fail but not decomposed by
    the plan agent. Route <=5 affected files to small and >5 to large so large
    remediations get plan-based decomposition. On the second retry and beyond
    (remediation_attempts >= 2), escalate small to large so the plan agent can
    decompose fixes that proved too complex for a single create task.
    """
    del severity  # retained for signature compatibility
    if len(affected_paths) > 5:
        return "large"
    if remediation_attempts >= 2:
        return "large"
    return "small"


async def _spawn_investigation_remediation(
    *,
    task: Task,
    direction_id: Optional[str],
    summary: str,
    root_cause: str,
    recommended_fix: str,
    severity: str,
    confidence: str,
    affected_paths: List[str],
    remediation_attempts: int,
    session: AsyncSession,
) -> Optional[Task]:
    """Build and persist a targeted `create` task from a high/medium-confidence diagnosis."""
    parent_ctx_early = task.context or {}
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    is_cross_layer = (
        parent_ctx_early.get("escalation") == "cross_layer"
        or parent_ctx_early.get("cross_layer") is True
        or plan_output.get("escalation") == "cross_layer"
    )
    layer_context = parent_ctx_early.get("layer_context")
    if not isinstance(layer_context, dict):
        layer_context = None

    prompt_parts = [recommended_fix.strip()]
    if root_cause:
        prompt_parts.append(f"\nRoot cause (from diagnosis):\n{root_cause[:1500]}")
    if affected_paths:
        prompt_parts.append(
            "\nKey files to review or modify:\n"
            + "\n".join(f"- {p}" for p in affected_paths[:10])
        )
    if is_cross_layer:
        layer_names = []
        if layer_context and isinstance(layer_context.get("layers"), list):
            layer_names = [
                str(entry.get("layer"))
                for entry in layer_context["layers"]
                if isinstance(entry, dict) and entry.get("layer")
            ]
        layers_phrase = (
            ", ".join(layer_names) if layer_names else "multiple infrastructure layers"
        )
        prompt_parts.append(
            "\nCROSS-LAYER REMEDIATION: this regression class surfaced across "
            f"{layers_phrase}. Plan a coordinated change that touches all affected "
            "layers in a single cluster rather than serializing one PR per layer. "
            "Note: .github/workflows/ files cannot be edited by the agent. If a "
            "defense would target workflow YAML or another unmodifiable layer, declare "
            "it by setting 'recommended_unmodifiable_layers': ['CI_WORKFLOW'] (or the "
            "appropriate layer name) in the task context — do NOT attempt a direct "
            "edit. The compliance checker credits documented unmodifiable layers toward "
            "the >=2-layer mandate."
        )
    prompt_parts.append(
        f"\nThis task was spawned from investigation {task.id} to remediate a "
        f"diagnosed failure pattern. Keep the change minimal and scoped to the "
        f"root cause above."
    )
    full_prompt = "\n".join(prompt_parts)[:20000]

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    title_hint = (summary or recommended_fix or "diagnosed fix")[:80]
    title = f"fix (diagnosed): {title_hint}"
    severity_tag = (
        severity if severity in ("critical", "high", "medium", "low") else "medium"
    )
    scope = _calculate_remediation_scope(
        affected_paths, severity_tag, remediation_attempts
    )
    if is_cross_layer:
        # Cross-layer remediations get plan-based decomposition so the agent can
        # bundle multi-layer changes into a single cluster plan instead of
        # serializing one PR per layer.
        scope = "large"

    project = await session.get(Project, task.project_id)
    payload_dict: Dict[str, Any] = {
        "type": "create",
        "prompt": full_prompt,
        "base_branch": get_project_integration_branch(project),
        "effort": scope_to_effort(scope),
    }
    if create_suffix:
        payload_dict["prompt_suffix"] = create_suffix

    parent_ctx = task.context or {}
    remediation_context: Dict[str, Any] = {
        "diagnosis_task_id": str(task.id),
        "original_diagnosis_task_id": str(
            parent_ctx.get("original_diagnosis_task_id") or task.id
        ),
        "severity": severity_tag,
        "diagnosis_confidence": confidence
        if confidence in ("high", "medium")
        else "high",
        "related_files": affected_paths[:25],
        "diagnosis_trigger": "investigation_completion",
        "diagnosis_root_cause": root_cause[:2000],
        "diagnosis_recommended_fix": recommended_fix[:2000],
        "scope": scope,
    }
    if direction_id:
        remediation_context["diagnosed_direction_id"] = direction_id
        remediation_context["direction_id"] = direction_id
    error_fingerprint = parent_ctx.get("error_fingerprint")
    if not error_fingerprint:
        fps = parent_ctx.get("failure_fingerprints") or []
        if isinstance(fps, list) and fps:
            error_fingerprint = fps[0]
    diagnosis_fingerprint = parent_ctx.get("diagnosis_fingerprint") or error_fingerprint
    if diagnosis_fingerprint:
        remediation_context["diagnosis_fingerprint"] = diagnosis_fingerprint
    if error_fingerprint:
        remediation_context["error_fingerprint"] = error_fingerprint
    trigger_failure_class = parent_ctx.get("last_failure_class")
    if trigger_failure_class:
        remediation_context["trigger_failure_class"] = trigger_failure_class
    trigger_failure_taxonomy = parent_ctx.get("failure_taxonomy")
    if trigger_failure_taxonomy:
        remediation_context["trigger_failure_taxonomy"] = trigger_failure_taxonomy
    if parent_ctx.get("proactive_pattern_fix"):
        remediation_context["proactive_pattern_fix"] = True
    if is_cross_layer:
        remediation_context["escalation"] = "cross_layer"
        remediation_context["escalation_reason"] = "cross_layer"
        remediation_context["cross_layer"] = True
        if layer_context is not None:
            remediation_context["layer_context"] = layer_context
    regression_class = parent_ctx.get("regression_class")
    if regression_class:
        remediation_context["regression_class"] = regression_class
        from app.models import RegressionRemediationCase  # noqa: PLC0415

        case_result = await session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == task.project_id,
                RegressionRemediationCase.regression_class == regression_class,
            )
        )
        case_row = case_result.scalar_one_or_none()
        if case_row is not None:
            snap = dict(case_row.signal_snapshot_at_spawn or {})
            snap["captured_at"] = _utcnow().isoformat()
            remediation_context["signal_snapshot"] = snap

    new_task = Task(
        project_id=task.project_id,
        title=title,
        description=(recommended_fix or summary or "")[:500],
        parent_task_id=task.id,
        payloads=[
            validate_internal_payload(payload_dict, "task_completion:investigation_fix")
        ],
        context=remediation_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        depends_on=[],
        priority=TASK_PRIORITY_MAP.get(
            "high" if severity_tag in ("critical", "high") else "medium", 50
        ),
    )
    session.add(new_task)
    await session.flush()
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "INVESTIGATION_DIAGNOSIS",
            "parent_investigation_id": str(task.id),
            "direction_id": direction_id,
        },
    )
    session.add(history)
    return new_task


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
    from app.main import _create_strategic_alert  # noqa: PLC0415
    from app.task_locking import lock_project_for_strategic_transition  # noqa: PLC0415

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
    spawned = None
    try:
        # Wave F4 H9: removed `await session.refresh(project)`. The caller
        # (post-commit chain via task_lifecycle._process_post_commit) already
        # holds the project lock acquired via lock_project_for_strategic_transition;
        # the row is fresh-read at lock time, so a refresh here is a redundant
        # round-trip that defeats the lock-share optimization between
        # strategic-completion handlers.
        spawned = await _spawn_strategic_review(project, session, cycle_id=cycle_id)
        if spawned:
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
