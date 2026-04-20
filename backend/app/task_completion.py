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
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.github import create_branch, create_pr_comment, get_branch_sha, get_open_pull_requests
from app.models import (
    DIAGNOSIS_IN_PROGRESS_KEY,
    DIAGNOSIS_STARTED_AT_KEY,
    DIAGNOSIS_TASK_ID_KEY,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.plan_completion import (
    TASK_PRIORITY_MAP,
    _handle_plan_completion,
    _inject_file_overlap_deps,
    _log_file_overlaps,
    _parse_plan_output,
    _validate_plan_dependencies,
)
from app.research_context import _build_research_context
from app.routers.agent import get_github_token_for_task
from app.shared_state import (
    clear_direction_hold,
    runtime_config as _runtime_config,
    scope_to_effort,
)
from app.payload_schemas import validate_internal_payload
from app.broadcast import broadcast_to_project
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

def _compute_first_attempt_succeeded(attempt_count: int, status: "TaskStatus") -> bool:
    """Return True if the task completed successfully on its first attempt (attempt_count <= 1)."""
    return attempt_count <= 1 and status == TaskStatus.COMPLETED


async def _record_remediation_outcome(
    task: Task, *, merged: bool, session: AsyncSession,
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
    project = await session.get(Project, task.project_id)
    if not project:
        return False
    directions = list(project.directions or [])
    target = next((d for d in directions if d.get("id") == direction_id), None)
    if target is None:
        return False
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
    prior_fingerprint = target.get("last_remediation_fingerprint")
    prior_attempts = int(target.get("remediation_attempts") or 0)
    if fingerprint and prior_fingerprint and str(fingerprint) != str(prior_fingerprint):
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
        "prompt_snippet": ((task.first_payload or {}).get("prompt", "") or "")[:200],
    }
    if fingerprint:
        history_entry["fingerprint"] = str(fingerprint)
    trigger_failure_class = ctx.get("trigger_failure_class")
    if trigger_failure_class:
        history_entry["trigger_failure_class"] = trigger_failure_class
    if merged:
        target.pop("last_remediation_failure_class", None)
        target.pop("last_remediation_error_summary", None)
        if fingerprint:
            try:
                _propagate_remediation_success_to_siblings(
                    project=project,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    successful_direction_id=str(direction_id),
                    task=task,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[DIAGNOSIS] Failed to propagate remediation success to siblings "
                    f"for direction {direction_id}: {exc!r}"
                )
    else:
        from app.failure_classification import classify_failure  # noqa: PLC0415
        target["last_remediation_failure_class"] = classify_failure(task)
        target["last_remediation_error_summary"] = (task.latest_status_message or "")[:200]
        history_entry["failure_class"] = target["last_remediation_failure_class"]
        history_entry["error_summary"] = target["last_remediation_error_summary"]
        if fingerprint:
            try:
                _propagate_remediation_failure_to_siblings(
                    project=project,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    failed_direction_id=str(direction_id),
                    task=task,
                    failure_class=target["last_remediation_failure_class"],
                    error_summary=target["last_remediation_error_summary"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[DIAGNOSIS] Failed to propagate remediation failure to siblings "
                    f"for direction {direction_id}: {exc!r}"
                )
    history_list = list(target.get("remediation_history") or [])
    history_list.append(history_entry)
    target["remediation_history"] = history_list[-_REMEDIATION_HISTORY_MAX:]
    project.directions = directions
    flag_modified(project, "directions")
    session.add(project)
    return True


def _propagate_remediation_success_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    successful_direction_id: str,
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
        project, fingerprint, exclude_direction_id=successful_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    note_text = (
        f"Sibling remediation merged ({now_iso[:10]}) for direction "
        f"{successful_direction_id} sharing the same error fingerprint. "
        f"Task: {task.id}."
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
        direction["cross_direction_remediation_source"] = str(successful_direction_id)
        notes = list(direction.get("notes") or [])
        notes.append({
            "text": note_text,
            "added_at": now_iso,
            "added_by_task": str(task.id),
            "source": "sibling_remediation_success",
        })
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


def _propagate_remediation_failure_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    failed_direction_id: str,
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
        project, fingerprint, exclude_direction_id=failed_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    note_text = (
        f"Sibling remediation FAILED ({now_iso[:10]}) for direction "
        f"{failed_direction_id} sharing the same error fingerprint. "
        f"Task: {task.id}. Do not repeat this approach."
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
        notes.append({
            "text": note_text,
            "added_at": now_iso,
            "added_by_task": str(task.id),
            "source": "sibling_remediation_failure",
        })
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


async def _record_remediation_if_terminal(
    task: Task, status: "TaskStatus", session: AsyncSession,
) -> None:
    """Stamp remediation outcome on the linked direction when a diagnosis-spawned task terminates.

    On a failed/cancelled remediation, additionally attempt to spawn a
    follow-up investigation so the pipeline does not have to wait for the
    48h auto-recovery cycle to revisit the root cause.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
    ctx = task.context or {}
    if not ctx.get("diagnosis_task_id") or not ctx.get("direction_id"):
        return
    # Tasks that produce a PR enter the review pipeline — their true outcome
    # (merged vs. rejected) is only known after the review task completes or the
    # pipeline exhausts its iteration cap. Recording on the PR-creating task's
    # COMPLETED would prematurely stamp merged=True before review/CI has run.
    # FAILED/CANCELLED still record immediately since no viable PR exists.
    _REVIEW_PIPELINE_TYPES = {"create", "refactor", "docs", "qa", "deps", "migrate", "fix", "review"}
    if status == TaskStatus.COMPLETED:
        task_type = (task.first_payload or {}).get("type")
        if task_type in _REVIEW_PIPELINE_TYPES:
            return
    try:
        recorded = await _record_remediation_outcome(
            task, merged=(status == TaskStatus.COMPLETED), session=session,
        )
        if recorded and status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            await _maybe_spawn_followup_investigation(task, session)
    except Exception as exc:
        logger.warning(f"[DIAGNOSIS] Failed to record remediation outcome for task {task.id}: {exc!r}")


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
    entry: Any, *, now: Optional[datetime] = None,
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
    project: Project, fp_key: str, *, now: Optional[datetime] = None,
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
    for d in (project.directions or []):
        attempts = (d.get("followup_investigation_attempts_by_fingerprint") or {}).get(fp_key)
        total += len(_active_followup_timestamps(attempts, now=now))
    return total


async def _recent_followup_investigation_exists(
    project_id, direction_id, cutoff, fingerprint, session, exclude_task_id=None,
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
    project_id, fingerprint, cutoff, session, exclude_task_id=None,
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


def _append_followup_dedup_note(direction: Dict[str, Any], fingerprint, existing_task_id: str) -> None:
    note_text = (
        f"Skipped duplicate follow-up investigation: shares error fingerprint "
        f"{str(fingerprint)[:80]} with existing investigate task {existing_task_id}."
    )[:STRATEGIC_RATIONALE_MAX]
    notes = list(direction.get("notes") or [])
    notes.append({
        "text": note_text,
        "added_at": _utcnow().isoformat(),
        "source": "followup_dedup",
        "shared_investigation_task_id": existing_task_id,
    })
    direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


async def _maybe_spawn_followup_investigation(
    task: Task, session: AsyncSession,
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
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint") \
        or target.get("last_remediation_fingerprint")
    if await _recent_followup_investigation_exists(
        project.id, direction_id, cutoff, fingerprint, session,
    ):
        logger.debug(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: "
            f"investigate task already created in last 24h "
            f"(fingerprint={str(fingerprint)[:40] if fingerprint else 'none'})"
        )
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id, fingerprint, cutoff, session,
    )
    if shared_id is not None:
        _append_followup_dedup_note(target, fingerprint, shared_id)
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        logger.info(
            f"[DIAGNOSIS] Skip follow-up for direction {direction_id}: shares "
            f"fingerprint {str(fingerprint)[:40]} with investigate task {shared_id}"
        )
        return None
    fp_key = str(fingerprint) if fingerprint else "no_fingerprint"
    attempts_by_fp = dict(target.get("followup_investigation_attempts_by_fingerprint") or {})
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
            project=project, direction=target, directions=directions,
            fp_key=fp_key, session=session,
        )
        return None

    prior_summary = (target.get("diagnosis_summary") or "").strip()
    failure_class = target.get("last_remediation_failure_class") or "(unknown)"
    error_summary = (target.get("last_remediation_error_summary") or "(no error message)")
    failed_prompt = (task.first_payload or {}).get("prompt", "") or ""
    prior_history = list(target.get("remediation_history") or [])
    # Exclude the current failed attempt (just appended by _record_remediation_outcome)
    # since it is already surfaced separately as "approach that failed".
    earlier_attempts = [
        h for h in prior_history if h.get("task_id") != str(task.id)
    ]
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
    prompt_parts.extend([
        "",
        "Produce a recommended_fix that is materially different from all prior "
        "attempts above. Consider whether the direction scope is wrong, prior "
        "fixes targeted a symptom rather than the cause, or a systemic/"
        "cross-cutting issue (shared infrastructure, framework bug, test "
        "harness) is at play.",
    ])
    prompt = "\n".join(prompt_parts)[:20000]
    payload = validate_internal_payload(
        {"type": "investigate", "prompt": prompt, "base_branch": "main"},
        "task_completion:remediation_failure_followup",
    )
    task_context = {
        "direction_id": str(direction_id),
        "diagnosis_trigger": "remediation_failure_followup",
        "failed_remediation_task_id": str(task.id),
    }
    original_diagnosis_task_id = (
        ctx.get("original_diagnosis_task_id") or ctx.get("diagnosis_task_id")
    )
    if original_diagnosis_task_id:
        task_context["original_diagnosis_task_id"] = str(original_diagnosis_task_id)
    if fingerprint:
        task_context["diagnosis_fingerprint"] = str(fingerprint)
        task_context["failure_fingerprints"] = [str(fingerprint)]
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
            f"Follow-up investigation after remediation task "
            f"{str(task.id)[:8]} failed"
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
    project.directions = directions
    flag_modified(project, "directions")
    session.add(project)
    logger.info(
        f"[DIAGNOSIS] Spawned follow-up investigation {new_task.id} after "
        f"remediation task {task.id} failed (direction={direction_id}, "
        f"fingerprint={fp_key[:40]}, "
        f"attempt {len(active_ts)}/"
        f"{_FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT})"
    )
    return new_task


def _followup_history_all_failed(
    direction: Dict[str, Any], fp_key: str,
) -> bool:
    """True when remediation_history for this fingerprint exists and every entry failed.

    Any merged entry — even an older one — indicates partial success on this
    fingerprint, so the direction is not a candidate for cap-exhaustion escalation.
    """
    history = list(direction.get("remediation_history") or [])
    relevant = [
        h for h in history
        if (str(h.get("fingerprint") or "") or "no_fingerprint") == fp_key
    ]
    if not relevant:
        return False
    return all(h.get("result") != "merged" for h in relevant)


async def _maybe_escalate_exhausted_followups(
    *,
    project: Project,
    direction: Dict[str, Any],
    directions: List[Dict[str, Any]],
    fp_key: str,
    session: AsyncSession,
) -> None:
    """Escalate to operator when the follow-up cap is hit with no merged remediation.

    Without this, the direction sits in a dead end: automated remediation is
    exhausted, the 48h auto-recovery cycle re-triggers the same failure pattern,
    and nobody is told. Mirror the auto-pause escalation path in direction_lifecycle
    so operators get a BLOCKER notification the moment the full diagnosis→remediation
    →follow-up chain has been exhausted.
    """
    if direction.get("needs_manual_intervention"):
        return
    if not _followup_history_all_failed(direction, fp_key):
        return
    direction["needs_manual_intervention"] = True
    project.directions = directions
    flag_modified(project, "directions")
    session.add(project)
    logger.warning(
        f"[DIAGNOSIS] Direction {direction.get('id')} flagged needs_manual_intervention: "
        f"follow-up cap exhausted for fingerprint {fp_key[:40]} with all prior "
        f"remediations failed"
    )
    try:
        from app.notification_dispatch import post_remediation_escalation_message  # noqa: PLC0415
        from app.direction_lifecycle import _build_escalation_args  # noqa: PLC0415
        await post_remediation_escalation_message(
            session, project.id, **_build_escalation_args(direction),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[NOTIFY] Failed to post follow-up cap escalation for direction "
            f"{direction.get('id')}: {exc!r}"
        )


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
    fingerprint = (
        (investigation_task.context or {}).get("diagnosis_fingerprint")
        or direction.get("error_fingerprint")
    )
    if await _recent_followup_investigation_exists(
        project.id, direction_id, cutoff, fingerprint, session,
        exclude_task_id=investigation_task.id,
    ):
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id, fingerprint, cutoff, session,
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
    attempts_by_fp = dict(direction.get("followup_investigation_attempts_by_fingerprint") or {})
    now = _utcnow()
    active_ts = _active_followup_timestamps(attempts_by_fp.get(fp_key), now=now)
    if (
        len(active_ts) >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
        or _project_active_followup_count(project, fp_key, now=now)
        >= _FOLLOWUP_INVESTIGATION_CAP_PER_FINGERPRINT
    ):
        directions = list(project.directions or [])
        await _maybe_escalate_exhausted_followups(
            project=project, direction=direction, directions=directions,
            fp_key=fp_key, session=session,
        )
        return None
    prior_summary = (direction.get("diagnosis_summary") or "").strip()
    prompt = "\n".join([
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
    ])[:20000]
    inv_ctx = investigation_task.context or {}
    original_diagnosis_task_id = (
        inv_ctx.get("original_diagnosis_task_id") or str(investigation_task.id)
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
        payloads=[validate_internal_payload(
            {"type": "investigate", "prompt": prompt, "base_branch": "main"},
            "task_completion:low_confidence_followup",
        )],
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
    fingerprint = (
        (investigation_task.context or {}).get("diagnosis_fingerprint")
        or direction.get("error_fingerprint")
    )
    if await _recent_followup_investigation_exists(
        project.id, direction_id, cutoff, fingerprint, session,
        exclude_task_id=investigation_task.id,
    ):
        return None
    shared_id = await _find_sibling_fingerprint_investigation(
        project.id, fingerprint, cutoff, session,
        exclude_task_id=investigation_task.id,
    )
    if shared_id is not None:
        _append_followup_dedup_note(direction, fingerprint, shared_id)
        logger.info(
            f"[DIAGNOSIS] Skip reinvestigation for direction {direction_id}: "
            f"shares fingerprint {str(fingerprint)[:40]} with investigate "
            f"task {shared_id}"
        )
        return None
    fp_key = str(fingerprint) if fingerprint else "no_fingerprint"
    attempts_by_fp = dict(direction.get("followup_investigation_attempts_by_fingerprint") or {})
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
        directions = list(project.directions or [])
        await _maybe_escalate_exhausted_followups(
            project=project, direction=direction, directions=directions,
            fp_key=fp_key, session=session,
        )
        return None
    prior_summary = (direction.get("diagnosis_summary") or "").strip()
    failure_summary = (investigation_task.latest_status_message or "(no error message)")
    from app.failure_classification import classify_failure  # noqa: PLC0415
    failure_class = classify_failure(investigation_task)
    partial_summary = (direction.get("partial_diagnosis_summary") or "").strip()
    partial_root_cause = (direction.get("partial_diagnosis_root_cause") or "").strip()
    partial_files = direction.get("partial_diagnosis_files") or []
    partial_block = ""
    if partial_summary or partial_root_cause or partial_files:
        partial_block = "\n".join([
            "",
            "Partial findings from the prior (incomplete) investigation:",
            f"- summary: {partial_summary[:500] or '(none)'}",
            f"- root_cause: {partial_root_cause[:1000] or '(none)'}",
            f"- affected_files: {', '.join(partial_files[:20]) or '(none)'}",
            "Build on these findings rather than re-investigating from scratch.",
        ])
    prompt = "\n".join([
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
    ])[:20000]
    inv_ctx = investigation_task.context or {}
    original_diagnosis_task_id = (
        inv_ctx.get("original_diagnosis_task_id") or str(investigation_task.id)
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
        payloads=[validate_internal_payload(
            {"type": "investigate", "prompt": prompt, "base_branch": "main"},
            "task_completion:investigation_failure_followup",
        )],
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
    task: Task, status: "TaskStatus", session: AsyncSession,
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
        project.directions = directions_list
        flag_modified(project, "directions")
        session.add(project)
        if had_flag:
            logger.info(
                f"[DIAGNOSIS] Cleared diagnosis_in_progress on direction "
                f"{direction_id} after investigate task {task.id} terminated "
                f"{status}"
            )
        if status != TaskStatus.FAILED:
            return
        from app.failure_classification import classify_failure, INFRA_CLASSES  # noqa: PLC0415
        failure_class = classify_failure(task)
        has_salvageable_root_cause = bool(partial and partial.get("root_cause"))
        if failure_class not in INFRA_CLASSES and not has_salvageable_root_cause:
            return
        spawned = await _maybe_spawn_reinvestigation_after_failure(
            investigation_task=task,
            project=project,
            direction=target,
            session=session,
        )
        if spawned is not None:
            flag_modified(project, "directions")
            session.add(project)
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
                    candidate = text[start:i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        return candidate
                    break  # This bracket pair didn't match, try next [
        start = text.find("[", start + 1)
    return None

async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str,
    label: str, session: AsyncSession
) -> bool:
    """Create a branch, or verify it already exists.

    Returns True on success, False on failure (task set to FAILED).
    """
    # Deferred import to avoid circular dependency
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    max_retries = 5
    gh_token = owner = repo = None
    last_err = None
    for _attempt in range(max_retries):
        try:
            gh_token, owner, repo = await get_github_token_for_task(
                task.id, session, force_refresh=(_attempt > 0))
            sha = await get_branch_sha(gh_token, owner, repo, source_branch)
            await create_branch(gh_token, owner, repo, branch_name, sha)
            logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
            return True
        except Exception as e:
            last_err = e
            if gh_token:
                try:
                    await get_branch_sha(gh_token, owner, repo, branch_name)
                    logger.info(f"[{label}] Branch {branch_name} already exists — reusing")
                    return True
                except HTTPException as verify_exc:
                    if verify_exc.status_code == 404:
                        # Branch genuinely doesn't exist — count against attempt budget
                        pass
                    else:
                        # Transient server error during verification (e.g. 502) — don't
                        # conclude the branch is missing; continue retrying.
                        logger.warning(f"[{label}] Transient error verifying branch {branch_name} ({verify_exc.status_code}), will retry")
                except Exception:
                    logger.exception(f"[{label}] Non-HTTP error verifying branch {branch_name}")
            if _attempt < max_retries - 1:
                backoff = 10 * (2 ** _attempt) * (0.75 + 0.5 * random.random())  # jitter: 10-20s, 20-40s, …
                logger.warning(f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {backoff:.1f}s: {e!r}")
                await asyncio.sleep(backoff)
            else:
                if gh_token:
                    logger.exception(f"[{label}] Branch {branch_name} creation failed after {max_retries} attempts")
                else:
                    logger.exception(f"[{label}] Cannot create branch — GitHub token unavailable")
    e = last_err
    task.status = TaskStatus.FAILED
    task.latest_status_message = f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    _fc = classify_failure(task)
    persist_failure_category(task, _fc)
    record_task_result(success=False, failure_class=_fc, task_id=str(task.id), task_type=task.first_payload.get("type", ""))
    session.add(task)
    await _maybe_create_failure_alert(task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200])
    await session.commit()
    return False

# ---------------------------------------------------------------------------
# Completion handlers
# ---------------------------------------------------------------------------

async def _handle_security_audit_completion(task: Task, logs: str, session: AsyncSession):
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
        task.latest_status_message = "Security audit completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
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
        return

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Security fix #{i+1}")
        severity = finding.get("severity", "high")

        new_task = Task(
            project_id=task.project_id,
            title=f"[{severity.upper()}] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[validate_internal_payload({
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }, "task_completion:security_finding")],
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
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "SECURITY_AUDIT", "parent_audit_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()

    # Post PR comment for medium/low findings that don't get auto-fixed
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    if informational:
        rows = "\n".join(
            "| {severity} | {title} | {loc} | {desc} |".format(
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                loc=(f.get("file_path", "") + (f":{f['line_number']}" if f.get("line_number") else "")),
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
                logger.info(f"[SECURITY AUDIT] Posted informational findings comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[SECURITY AUDIT] Failed to post PR comment for informational findings: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} critical/high fixed, {len(informational)} medium/low surfaced as PR comment"
    )
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"security_audit_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[SECURITY AUDIT] Created {len(child_tasks)} fix tasks from audit {task.id}")

async def _handle_perf_completion(task: Task, logs: str, session: AsyncSession):
    """Parse perf findings and spawn create tasks for high-severity issues."""
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
        task.latest_status_message = "Perf analysis completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)

    base_branch = (task.first_payload).get("base_branch", "main")

    # Spawn create tasks for high-severity findings
    actionable = [f for f in findings if f.get("severity") == "high"]
    if len(actionable) > 10:
        actionable = actionable[:10]

    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Perf fix #{i+1}")
        area = finding.get("area", "")

        new_task = Task(
            project_id=task.project_id,
            title=f"[PERF] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[validate_internal_payload({
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
            }, "task_completion:perf_finding")],
            context={
                "perf_task_id": str(task.id),
                "finding_index": i,
                "severity": "high",
                "area": area,
                "related_files": finding.get("related_files", []),
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
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PERF_AUDIT", "parent_perf_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    # Post a summary comment on open PRs for medium/low findings
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    all_surfaced = informational + [f for f in findings if f.get("severity") == "high" and f not in actionable]
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
                logger.info(f"[PERF] Posted findings summary comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[PERF] Failed to post PR comment: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} high-severity fix task(s) spawned, "
        f"{len(informational)} medium/low finding(s) surfaced as PR comment"
    )
    session.add(task)

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"perf_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[PERF] Created {len(child_tasks)} fix tasks from perf analysis {task.id}")

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


async def _handle_investigation_completion(task: Task, payload, session: AsyncSession) -> None:
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
        fence_stripped = re.sub(r"^\s*```(?:json)?\s*\n?|\n?```\s*$", "", logs.strip(), flags=re.IGNORECASE)
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
            f"(raw_prefix={logs[:120]!r})" if logs else
            "Investigation produced no output — no diagnosis report available"
        )[:500]
        session.add(task)
        # Still clear diagnosis_in_progress so a malformed report doesn't wedge
        # the direction out of future auto-pause diagnosis. FAILED status lets
        # the retry budget and _maybe_spawn_reinvestigation_after_failure path
        # engage instead of silently swallowing the crash as a success.
        await _clear_direction_diagnosis_flag(
            task, session, summary=None, outcome="report_unparseable",
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
        direction = next((d for d in directions_list if d.get("id") == direction_id), None)

    structural = _is_structural_diagnosis(root_cause, recommended_fix)
    spawn_remediation = (
        not structural
        and confidence in ("high", "medium")
        and bool(recommended_fix)
    )

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

    # Update direction state
    if direction is not None and project is not None:
        now_iso = _utcnow().isoformat()
        direction["diagnosis_completed_at"] = now_iso
        direction["last_diagnosis_task_id"] = str(task.id)
        if summary:
            direction["diagnosis_summary"] = summary[:STRATEGIC_RATIONALE_MAX]
        if affected_paths:
            direction["diagnosed_affected_files"] = affected_paths[:25]
        direction.pop(DIAGNOSIS_IN_PROGRESS_KEY, None)
        direction.pop(DIAGNOSIS_STARTED_AT_KEY, None)
        direction.pop(DIAGNOSIS_TASK_ID_KEY, None)
        if spawned_child is not None:
            direction["remediation_task_id"] = str(spawned_child.id)
        if structural:
            note_text = (
                f"Diagnosis ({now_iso[:10]}) flagged this direction as structurally "
                f"misaligned. Root cause: {root_cause[:400]}"
            )
            if recommended_fix:
                note_text += f" Recommendation: {recommended_fix[:400]}"
            note_text = note_text[:STRATEGIC_RATIONALE_MAX]
            notes = list(direction.get("notes") or [])
            notes.append({
                "text": note_text,
                "added_at": now_iso,
                "added_by_task": str(task.id),
                "source": "investigation_diagnosis",
            })
            direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]

        # Append diagnosis insight to research_context via canonical builder
        diag_line = (
            f"Last diagnosis ({now_iso[:10]}): {(root_cause or summary)[:200]}"
        )
        insights = [diag_line]
        if affected_paths:
            insights.append(
                "Diagnosed affected files: "
                + ", ".join(affected_paths[:5])
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

        project.directions = directions_list
        flag_modified(project, "directions")
        session.add(project)

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
            flag_modified(project, "directions")
            session.add(project)

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
        flag_modified(project, "directions")
        session.add(project)

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
            "structural" if structural else
            ("no_actionable_fix" if not recommended_fix else
             ("low_confidence" if confidence not in ("high", "medium") else "no_direction"))
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
    project = await session.get(Project, task.project_id)
    if project is None:
        return
    directions_list = list(project.directions or [])
    target = next((d for d in directions_list if d.get("id") == direction_id), None)
    if target is None:
        return
    target.pop(DIAGNOSIS_IN_PROGRESS_KEY, None)
    target.pop(DIAGNOSIS_STARTED_AT_KEY, None)
    target.pop(DIAGNOSIS_TASK_ID_KEY, None)
    target["diagnosis_completed_at"] = _utcnow().isoformat()
    target["last_diagnosis_task_id"] = str(task.id)
    if outcome:
        target["last_diagnosis_outcome"] = outcome
    if summary:
        target["diagnosis_summary"] = summary[:STRATEGIC_RATIONALE_MAX]
    project.directions = directions_list
    flag_modified(project, "directions")
    session.add(project)


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
    direction_id: str,
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
    prompt_parts = [recommended_fix.strip()]
    if root_cause:
        prompt_parts.append(f"\nRoot cause (from diagnosis):\n{root_cause[:1500]}")
    if affected_paths:
        prompt_parts.append(
            "\nKey files to review or modify:\n"
            + "\n".join(f"- {p}" for p in affected_paths[:10])
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
    severity_tag = severity if severity in ("critical", "high", "medium", "low") else "medium"
    scope = _calculate_remediation_scope(affected_paths, severity_tag, remediation_attempts)

    payload_dict: Dict[str, Any] = {
        "type": "create",
        "prompt": full_prompt,
        "base_branch": "main",
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
        "diagnosed_direction_id": direction_id,
        "direction_id": direction_id,
        "severity": severity_tag,
        "diagnosis_confidence": confidence if confidence in ("high", "medium") else "high",
        "related_files": affected_paths[:25],
        "diagnosis_trigger": "investigation_completion",
        "diagnosis_root_cause": root_cause[:2000],
        "diagnosis_recommended_fix": recommended_fix[:2000],
        "scope": scope,
    }
    error_fingerprint = parent_ctx.get("error_fingerprint")
    if not error_fingerprint:
        fps = parent_ctx.get("failure_fingerprints") or []
        if isinstance(fps, list) and fps:
            error_fingerprint = fps[0]
    diagnosis_fingerprint = (
        parent_ctx.get("diagnosis_fingerprint") or error_fingerprint
    )
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

    new_task = Task(
        project_id=task.project_id,
        title=title,
        description=(recommended_fix or summary or "")[:500],
        parent_task_id=task.id,
        payloads=[validate_internal_payload(
            payload_dict, "task_completion:investigation_fix"
        )],
        context=remediation_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        depends_on=[],
        priority=TASK_PRIORITY_MAP.get("high" if severity_tag in ("critical", "high") else "medium", 50),
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

async def _handle_design_doc_completion(task: Task, logs: str, session: AsyncSession):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.
    """
    # Deferred imports to avoid circular dependency
    from app.strategic_spawning import _spawn_strategic_review  # noqa: PLC0415
    from app.main import _create_strategic_alert  # noqa: PLC0415

    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        logger.error(f"[DESIGN DOC] Task {task.id}: project {task.project_id} not found")
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update failed: project not found (task {str(task.id)[:8]})")
        return

    doc = project.design_doc or {}
    rev = doc.get("_meta", {}).get("revision", 0)
    page_count = sum(1 for k in doc if not k.startswith("_"))
    logger.info(f"[DESIGN DOC] Task {task.id} completed: {page_count} pages, rev {rev}")

    if page_count == 0:
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update produced 0 pages (task {str(task.id)[:8]})")

    # Chain: spawn Phase 2 (strategic review)
    # Do this BEFORE committing task status so a failed spawn doesn't leave
    # the task as COMPLETED with no Phase 2 follow-up.
    cycle_id = (task.context or {}).get("strategic_cycle_id")
    spawned = None
    try:
        await session.refresh(project)
        spawned = await _spawn_strategic_review(project, session, cycle_id=cycle_id)
        if spawned:
            task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
        else:
            # Spawn silently skipped (active strategic task) — create retry marker
            logger.warning(f"[DESIGN DOC] Phase 2 spawn skipped for task {task.id} (active strategic task exists)")
            task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review deferred (active task exists)"
            project.vision = {**(project.vision or {}), '_pending_strategic_review': {
                'design_doc_task_id': str(task.id),
                'failed_at': _utcnow().isoformat(),
                'error': 'skipped: active strategic task exists',
                'cycle_id': cycle_id,
            }}
            flag_modified(project, 'vision')
            session.add(project)
    except Exception as chain_exc:
        logger.error(f"[DESIGN DOC] Failed to chain Phase 2 for task {task.id}: {chain_exc}", exc_info=True)
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — FAILED to spawn strategic review: {chain_exc!r}"[:500]
        await _create_strategic_alert(task.project_id, session,
            f"Strategic review failed to spawn after design doc update (task {str(task.id)[:8]}): {chain_exc!r}")
        # Save a retry marker so the next research spawn can retry the strategic review
        project.vision = {**(project.vision or {}), '_pending_strategic_review': {
            'design_doc_task_id': str(task.id),
            'failed_at': _utcnow().isoformat(),
            'error': str(chain_exc)[:200],
            'cycle_id': cycle_id,
        }}
        if cycle_id:
            from app.research_completion import _update_cycle_history  # noqa: PLC0415
            _update_cycle_history(project, cycle_id, {
                "phase1_completed_at": _utcnow().isoformat(),
                "outcome": "failed_phase2_spawn",
                "error": str(chain_exc)[:200],
            })
        flag_modified(project, 'vision')
        session.add(project)
    if spawned:
        phase1_msg = ProjectMessage(
            project_id=task.project_id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            from_task_id=task.id,
            content=f"Architecture analysis completed ({page_count} pages, rev {rev}). Strategic review starting.",
            context={
                "event_type": "cycle_phase1_complete",
                "cycle_id": cycle_id,
                "page_count": page_count,
                "revision": rev,
            },
        )
        session.add(phase1_msg)
    session.add(task)
    await session.commit()
