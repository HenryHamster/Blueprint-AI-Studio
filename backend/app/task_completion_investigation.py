"""Follow-up / investigation completion helpers extracted from task_completion.

This module owns the follow-up spawning, exhaustion escalation, and
diagnosis-terminal-failure helpers. task_completion re-exports these names so
existing imports and monkeypatch targets keep working.
"""

from __future__ import annotations

import logging
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
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    clear_direction_hold,
    get_project_integration_branch,
)
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
import app.task_completion as _task_completion
from app.task_completion import (
    _persist_direction_field_update,
    _stamp_remediation_spawn_on_direction,
)
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
        await _task_completion._maybe_escalate_exhausted_followups(
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
        await _task_completion._maybe_escalate_exhausted_followups(
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
        await _task_completion._maybe_escalate_exhausted_followups(
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
            partial_spawn = await _task_completion._spawn_investigation_remediation(
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

        spawned = await _task_completion._maybe_spawn_reinvestigation_after_failure(
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
