"""Follow-up / investigation completion helpers extracted from task_completion.

This module owns the follow-up spawning, exhaustion escalation, and
diagnosis-terminal-failure helpers. task_completion re-exports these names so
existing imports and monkeypatch targets keep working.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    DIAGNOSIS_IN_PROGRESS_KEY,
    DIAGNOSIS_STARTED_AT_KEY,
    DIAGNOSIS_TASK_ID_KEY,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.plan_completion import TASK_PRIORITY_MAP
from app.research_context import _build_research_context
from app.shared_state import (
    clear_direction_hold,
    get_project_integration_branch,
    runtime_config as _runtime_config,
    scope_to_effort,
)
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
from app.task_completion import (
    _persist_direction_field_update,
    _stamp_remediation_spawn_on_direction,
    _synthesize_memo_from_source_finding,
    _apply_synthesized_memo,
)
from app.broadcast import broadcast_to_project
from app.task_locking import with_directions_locked
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


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

# Keys removed from a direction when a diagnosis completes or fails — passed
# as deleted_keys to _persist_direction_field_update so the delta-merge helper
# removes them from the freshly-loaded DB entry rather than relying on the old
# target.clear() whole-snapshot replacement.
_DIAGNOSIS_FLAGS_CLEARED_KEYS: frozenset = frozenset(
    {DIAGNOSIS_IN_PROGRESS_KEY, DIAGNOSIS_STARTED_AT_KEY, DIAGNOSIS_TASK_ID_KEY}
)

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
    _notify_keys_cleared: set = set()
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
        _notify_keys_cleared = {
            "escalation_notification_pending",
            "escalation_notification_retries",
        }
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
    await _persist_direction_field_update(
        session, project, direction, deleted_keys=frozenset(_notify_keys_cleared)
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
        await _persist_direction_field_update(
            session, project, target, deleted_keys=_DIAGNOSIS_FLAGS_CLEARED_KEYS
        )
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
                await _persist_direction_field_update(
                    session,
                    project,
                    target,
                    deleted_keys=_DIAGNOSIS_FLAGS_CLEARED_KEYS,
                )
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
# Investigation-completion cluster — moved from task_completion.py.
# task_completion re-exports these names so existing callers keep working.
# ---------------------------------------------------------------------------

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

        await _persist_direction_field_update(
            session, project, direction, deleted_keys=_DIAGNOSIS_FLAGS_CLEARED_KEYS
        )

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
        await _persist_direction_field_update(
            session,
            project,
            direction,
            deleted_keys=(
                frozenset()
                if spawned_followup is not None
                else _DIAGNOSIS_FLAGS_CLEARED_KEYS
            ),
        )

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
