"""Direction lifecycle management for autonomous research.

Handles dead direction pruning, auto-recovery from auto-pause, and active
direction filtering (cooldown enforcement, auto-pause, completion detection).
Extracted from autonomous_research.py to create headroom in that module.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.failure_classification import classify_failure, classify_failure_taxonomy
from app.models import Project, Task, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import get_circuit_breaker, runtime_config as _runtime_config
from app.strategic_completion import _is_code_failure_pause_reason
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_MAX_AUTO_PAUSE_CYCLES = 3

# Remediation monitor window: if a merged remediation happened longer ago than this,
# a new failure is presumed unrelated and the stale window is cleared instead of
# triggering escalation.
_REMEDIATION_MONITOR_EXPIRY_DAYS = 7

# Max time an investigation task may hold diagnosis_in_progress before flags are considered stale.
# Investigations typically finish in minutes; 4h is a safety ceiling to recover from reaped/timed-out tasks.
_DIAGNOSIS_STALE_HOURS = 4

# Recent failed tasks to scan when computing the dominant error fingerprint
_FINGERPRINT_SAMPLE_SIZE = 10
# Min matching failures required for a fingerprint to count as "dominant" for the direction
_FINGERPRINT_MIN_RECURRENCE = 2
# Only consider failures from within this window so stale patterns don't dominate
# after the current failure mode has shifted. Falls back to the unfiltered query
# when fewer than _FINGERPRINT_MIN_RECURRENCE recent failures exist.
_FINGERPRINT_RECENCY_DAYS = 7

# Max retries for a BLOCKER escalation notification whose initial delivery failed.
# After this many failed retries, the pending flag is cleared and an ERROR is logged
# with structured fields so monitoring can catch a silently-stuck direction.
_MAX_ESCALATION_NOTIFICATION_RETRIES = 3

# Minimum fractional drop in CODE_ERROR failure rate (pre- vs post-remediation)
# that counts as partial success rather than a failed fix. 0.5 == 50% reduction.
_PARTIAL_SUCCESS_RATE_DROP = 0.5


def _detect_partial_remediation_success(direction: dict) -> bool:
    """Return True if post-remediation code-failure rate is materially lower than pre-remediation.

    A remediation that reduces failures from 80% to 20% still leaves the same
    dominant error fingerprint visible, so fingerprint-based escalation treats
    it as total failure. Compares code-failure rate across research_history
    entries straddling ``last_remediation_at`` and returns True when the
    post-remediation rate dropped by at least ``_PARTIAL_SUCCESS_RATE_DROP``.
    """
    last_rem_at = direction.get("last_remediation_at")
    if not last_rem_at:
        return False
    try:
        boundary = datetime.fromisoformat(last_rem_at)
    except (ValueError, TypeError):
        return False
    pre: list[dict] = []
    post: list[dict] = []
    for entry in (direction.get("research_history") or []):
        ts = entry.get("timestamp")
        if not ts:
            continue
        try:
            entry_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        (post if entry_at >= boundary else pre).append(entry)
    if not pre or not post:
        return False

    def _code_failure_rate(entries: list[dict]) -> float:
        created = sum(e.get("tasks_created", 0) for e in entries)
        if created <= 0:
            return 0.0
        failed = sum(e.get("tasks_failed", 0) for e in entries)
        infra = sum(e.get("tasks_failed_infra", 0) for e in entries)
        return max(0, failed - infra) / created

    pre_rate = _code_failure_rate(pre[-3:])
    post_rate = _code_failure_rate(post)
    if pre_rate <= 0:
        return False
    return post_rate <= pre_rate * (1.0 - _PARTIAL_SUCCESS_RATE_DROP)


def _build_escalation_args(direction: dict) -> dict:
    """Assemble kwargs for post_remediation_escalation_message from direction state.

    Shared by the initial auto-pause escalation path and the retry loop so both
    produce identical message bodies when re-derived from the direction.
    """
    from app.strategic_completion import _build_direction_failure_summary  # noqa: PLC0415
    summary = _build_direction_failure_summary(direction)
    rem_summary = (
        f"cycles_analyzed={summary['cycles_analyzed']} "
        f"tasks_created={summary['tasks_created']} "
        f"code_failed={summary['tasks_failed_code']} "
        f"merged={summary['prs_merged']} "
        f"code_failure_rate={summary['code_failure_rate']}"
    )
    did = direction.get("id")
    return {
        "direction_text": direction.get("text") or (str(did) if did else ""),
        "failure_pattern": (
            direction.get("error_fingerprint")
            or direction.get("paused_reason")
            or "(unknown)"
        ),
        "attempts": int(direction.get("remediation_attempts", 0)),
        "remediation_summary": rem_summary,
    }


async def _compute_direction_error_fingerprint(
    project_id: uuid.UUID, direction_id: str, session: AsyncSession,
) -> str | None:
    """Return the dominant error fingerprint across this direction's recent failed tasks.

    Pulls the most recent ``_FINGERPRINT_SAMPLE_SIZE`` failures for ``direction_id``
    within the last ``_FINGERPRINT_RECENCY_DAYS`` days and uses
    ``count_matching_fingerprints`` to find the fingerprint that recurs most,
    returning it only if it matches at least ``_FINGERPRINT_MIN_RECURRENCE``
    failures. Falls back to the unfiltered query when too few failures exist
    inside the window so low-volume directions still get a fingerprint. The
    result is stored on the direction so operators see the specific error
    pattern and the escalation path can compare it against the last
    remediation's fingerprint.
    """
    from app.anomaly_detection import _error_fingerprint, count_matching_fingerprints  # noqa: PLC0415

    base_query = select(Task).where(
        Task.project_id == project_id,
        Task.status == TaskStatus.FAILED,
        Task.context["direction_id"].as_string() == direction_id,
    ).order_by(Task.created_at.desc()).limit(_FINGERPRINT_SAMPLE_SIZE)
    window_start = _utcnow() - timedelta(days=_FINGERPRINT_RECENCY_DAYS)
    failed_tasks = (await session.execute(
        base_query.where(Task.created_at >= window_start)
    )).scalars().all()
    if len(failed_tasks) < _FINGERPRINT_MIN_RECURRENCE:
        failed_tasks = (await session.execute(base_query)).scalars().all()
    best_fp: str | None = None
    best_count = 0
    seen: set[str] = set()
    for t in failed_tasks:
        msg = t.latest_status_message or ""
        if not msg:
            continue
        fp = _error_fingerprint(msg)
        if fp in seen:
            continue
        seen.add(fp)
        count = count_matching_fingerprints(fp, failed_tasks)
        if count > best_count:
            best_fp, best_count = fp, count
    return best_fp if best_count >= _FINGERPRINT_MIN_RECURRENCE else None


async def _spawn_direction_diagnosis(
    project: Project, direction: dict, pause_reason: str, session: AsyncSession,
) -> bool:
    """Spawn an investigate task for a code-failure auto-pause.

    Guards: pause-reason type (Rule 3/4 only), auto-retire threshold, circuit
    breaker state, per-direction 24h dedup, cross-direction 24h fingerprint
    dedup. On spawn, marks the direction with diagnosis_in_progress=True and
    returns True.
    """
    if not _is_code_failure_pause_reason(pause_reason):
        return False
    if direction.get("code_failure_pause_count", 0) >= _MAX_AUTO_PAUSE_CYCLES:
        return False
    breaker_state = get_circuit_breaker(str(project.id)).get("state")
    if breaker_state == "OPEN":
        logger.warning(
            f"[DIAGNOSIS] Skipping diagnosis spawn for project={project.id} "
            f"direction={direction.get('id')} — circuit breaker state={breaker_state}"
        )
        return False
    direction_id = direction.get("id")
    if not direction_id:
        return False
    from app.autonomous_research import _recent_investigation_exists  # noqa: PLC0415
    if await _recent_investigation_exists(project.id, str(direction_id), session):
        return False
    cutoff = _utcnow() - timedelta(hours=24)
    # Compute fingerprint up front so it can be (1) used to dedup against
    # recent investigations on sibling directions sharing the same root cause
    # and (2) stamped on the new task's context so future dedup queries match.
    fingerprint = direction.get("error_fingerprint")
    try:
        if not fingerprint:
            fingerprint = await _compute_direction_error_fingerprint(
                project.id, str(direction_id), session,
            )
    except Exception as _fp_exc:
        logger.warning(f"[DIAGNOSIS] Failed to compute error fingerprint for {direction_id}: {_fp_exc!r}")
        fingerprint = None
    if fingerprint:
        shared = (await session.execute(
            select(Task.id).where(
                Task.project_id == project.id,
                Task.created_at >= cutoff,
                Task.payloads[0]["type"].as_string() == "investigate",
                Task.context["diagnosis_fingerprint"].as_string() == str(fingerprint),
            ).limit(1)
        )).first()
        if shared is not None:
            existing_task_id = str(shared[0])
            from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX  # noqa: PLC0415
            note_text = (
                f"Skipped duplicate investigation: shares error fingerprint "
                f"{str(fingerprint)[:80]} with existing investigate task {existing_task_id}."
            )[:STRATEGIC_RATIONALE_MAX]
            notes = list(direction.get("notes") or [])
            notes.append({
                "text": note_text,
                "added_at": _utcnow().isoformat(),
                "source": "diagnosis_dedup",
                "shared_investigation_task_id": existing_task_id,
            })
            direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
            logger.info(
                f"[DIAGNOSIS] Skipping spawn for {direction_id}: shares fingerprint "
                f"{str(fingerprint)[:40]} with investigation {existing_task_id}"
            )
            return False
    history = direction.get("research_history") or []
    recent_lines = "\n".join(
        f"- created={e.get('tasks_created', 0)} failed={e.get('tasks_failed', 0)} "
        f"infra_failed={e.get('tasks_failed_infra', 0)} merged={e.get('prs_merged', 0)}"
        for e in history[-5:]
    ) or "- (no research history)"
    recent_failures = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.FAILED,
            Task.context["direction_id"].as_string() == str(direction_id),
        ).order_by(Task.created_at.desc()).limit(3)
    )).scalars().all()
    failure_lines = "\n".join(
        f"- {(t.latest_status_message or '(no message)').strip()[:200]}"
        for t in recent_failures
    ) or "- (no recent failed tasks)"
    prior_diagnosis = (direction.get("diagnosis_summary") or "").strip()
    # Cross-direction shared-fingerprint detection. If the same fingerprint
    # already appears on other auto-paused directions, annotate the diagnosis
    # so the agent investigates a systemic root cause rather than a
    # direction-specific scope/code issue.
    shared_directions: list[dict] = []
    if fingerprint:
        for other in (project.directions or []):
            if other.get("id") == direction_id:
                continue
            if other.get("status") != "paused" or not other.get("auto_paused"):
                continue
            if other.get("error_fingerprint") == fingerprint:
                shared_directions.append({
                    "id": other.get("id"),
                    "text": (other.get("text") or "")[:80],
                })
    extras = ""
    if fingerprint:
        extras += f"Dominant error fingerprint: {fingerprint[:200]}\n"
    if shared_directions:
        sibling_lines = "\n".join(
            f"  - {d['id']}: {d['text']}" if d.get("text") else f"  - {d['id']}"
            for d in shared_directions
        )
        extras += (
            f"Shared error pattern: same fingerprint also appears on "
            f"{len(shared_directions)} other auto-paused direction(s):\n"
            f"{sibling_lines}\n"
            f"This suggests a SYSTEMIC root cause rather than a direction-specific "
            f"issue — investigate cross-cutting infrastructure or framework code, "
            f"not just files specific to this direction.\n"
        )
        logger.info(
            f"[DIAGNOSIS] Cross-direction systemic pattern for {direction_id}: "
            f"fingerprint {str(fingerprint)[:40]} shared with "
            f"{len(shared_directions)} other direction(s)"
        )
    if prior_diagnosis:
        extras += f"Prior diagnosis summary: {prior_diagnosis[:400]}\n"
    rem_attempts = int(direction.get("remediation_attempts") or 0)
    if rem_attempts:
        last_rem_result = direction.get("last_remediation_result") or "(unknown)"
        last_rem_at = direction.get("last_remediation_at") or "(unknown)"
        last_rem_task = direction.get("last_remediation_task_id") or "(unknown)"
        last_rem_fp = direction.get("last_remediation_fingerprint") or "(unknown)"
        extras += (
            f"Prior remediation attempts: {rem_attempts} "
            f"(last result: {last_rem_result}, at: {last_rem_at}, "
            f"task: {last_rem_task}, fingerprint: {str(last_rem_fp)[:200]})\n"
        )
        if last_rem_result == "failed":
            last_rem_class = direction.get("last_remediation_failure_class")
            last_rem_error = direction.get("last_remediation_error_summary")
            if last_rem_class or last_rem_error:
                extras += (
                    f"Last remediation failed with "
                    f"{last_rem_class or '(unknown class)'}: "
                    f"{(last_rem_error or '(no error summary)')[:200]}\n"
                )
            # Load the prior remediation task so the investigation sees what
            # approach was tried (title/description), not just what it errored on.
            rem_task = None
            if direction.get("last_remediation_task_id"):
                try:
                    rem_task = await session.get(
                        Task, uuid.UUID(str(direction["last_remediation_task_id"])),
                    )
                except (ValueError, TypeError):
                    rem_task = None
            if rem_task:
                extras += (
                    f"Prior remediation approach:\n"
                    f"  title: {(rem_task.title or '')[:200]}\n"
                    f"  description: {(rem_task.description or '')[:500]}\n"
                )
            extras += (
                "The previous remediation attempt failed. Your investigation "
                "should determine whether the prior diagnosis was incorrect, "
                "incomplete, or whether the fix approach was wrong. Do not "
                "recommend the same approach unless you have evidence it was "
                "implemented incorrectly.\n"
            )
        extras += (
            f"Avoid proposing a fix targeting the same fingerprint that already "
            f"failed; prior attempts did not resolve the recurring pattern.\n"
        )
    prompt = (
        f"A research direction was auto-paused for repeated code failures.\n\n"
        f"Direction: {direction.get('text', '')[:200]}\n"
        f"Pause reason: {pause_reason}\n"
        f"Auto-pause count: {direction.get('auto_pause_count', 0)}\n"
        f"{extras}\n"
        f"Recent cycles (tasks_created / failed / infra_failed / merged):\n{recent_lines}\n\n"
        f"Recent failed task messages (truncated):\n{failure_lines}\n\n"
        f"Investigate why tasks spawned for this direction keep failing. "
        f"Identify the root cause (direction scope, code complexity, systemic "
        f"architectural issue), affected files, and a recommended fix."
    )
    payload = validate_internal_payload(
        {"type": "investigate", "prompt": prompt, "base_branch": "main"},
        "direction_lifecycle:auto_pause_diagnosis",
    )
    task_context = {
        "direction_id": str(direction_id),
        "pause_reason": pause_reason,
        "diagnosis_trigger": "direction_auto_pause",
    }
    if fingerprint:
        task_context["diagnosis_fingerprint"] = str(fingerprint)
    if shared_directions:
        task_context["shared_fingerprint_directions"] = shared_directions
    if recent_failures:
        first_failure = recent_failures[0]
        task_context["last_failure_class"] = classify_failure(first_failure)
        task_context["failure_taxonomy"] = classify_failure_taxonomy(first_failure)
    new_task = Task(
        project_id=project.id,
        title=f"Investigate direction auto-pause: {(direction.get('text') or '')[:60]}",
        description=f"Auto-pause reason: {pause_reason}"[:500],
        payloads=[payload],
        context=task_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=70,
    )
    session.add(new_task)
    direction["diagnosis_in_progress"] = True
    direction["diagnosis_started_at"] = _utcnow().isoformat()
    logger.info(
        f"[DIAGNOSIS] Spawned investigate task {new_task.id} for auto-paused "
        f"direction {direction_id} ({pause_reason[:60]})"
    )
    return True


async def _process_direction_lifecycle(
    project: Project,
    session: AsyncSession,
    cooldown_minutes: float,
    busy_direction_ids: set,
    now: datetime,
) -> list:
    """Prune dead directions, auto-recover paused ones, and return eligible active directions.

    Mutates project.directions in place for auto-pause and recovery state changes.
    Commits the session if any direction state was modified.

    Returns a list of active, non-cooling directions eligible for research.
    """
    from app.strategic_completion import _check_direction_health, _check_direction_completion  # noqa: PLC0415

    directions_modified = False
    # Prune dead directions (expired/completed/rejected) to prevent JSON bloat
    live_dirs = [d for d in (project.directions or []) if d.get("status") in ("active", "proposed", "paused")]
    if len(live_dirs) < len(project.directions or []):
        logger.info(f"[AUTONOMOUS] Pruned {len(project.directions or []) - len(live_dirs)} dead directions from {project.id}")
        project.directions = live_dirs
        flag_modified(project, "directions")
        directions_modified = True
    # Retry BLOCKER escalation notifications whose initial delivery failed.
    # Runs before auto-recovery so a direction that recovers this cycle does
    # not silently lose its pending notification.
    for d in (project.directions or []):
        if not d.get("escalation_notification_pending"):
            continue
        retries = int(d.get("escalation_notification_retries", 0))
        if retries >= _MAX_ESCALATION_NOTIFICATION_RETRIES:
            logger.error(
                "[NOTIFY] Escalation notification retries exhausted",
                extra={
                    "event": "escalation_retries_exhausted",
                    "project_id": str(project.id),
                    "direction_id": d.get("id"),
                    "retries": retries,
                    "remediation_attempts": int(d.get("remediation_attempts", 0)),
                },
            )
            d.pop("escalation_notification_pending", None)
            directions_modified = True
            continue
        try:
            from app.notification_dispatch import post_remediation_escalation_message  # noqa: PLC0415
            await post_remediation_escalation_message(
                session, project.id, **_build_escalation_args(d),
            )
            d.pop("escalation_notification_pending", None)
            d.pop("escalation_notification_retries", None)
            directions_modified = True
            logger.info(
                f"[NOTIFY] Escalation retry succeeded for direction {d.get('id')} "
                f"after {retries} prior retries"
            )
        except Exception as _retry_exc:
            d["escalation_notification_retries"] = retries + 1
            directions_modified = True
            logger.warning(
                f"[NOTIFY] Escalation retry {retries + 1} failed for {d.get('id')}: {_retry_exc}"
            )
    # Auto-recovery: reactivate auto-paused directions past cooldown
    recovery_hours = _runtime_config.get("direction_auto_recovery_hours", 48)
    for d in (project.directions or []):
        if d.get("status") != "paused" or not d.get("auto_paused") or not d.get("paused_at"):
            continue
        hours = (_utcnow() - datetime.fromisoformat(d["paused_at"])).total_seconds() / 3600
        if hours < recovery_hours:
            continue
        if _check_direction_health(d) is None:
            d["status"] = "active"
            for k in ("auto_paused", "paused_reason", "paused_at"):
                d.pop(k, None)
            # Clear stale manual-intervention alert if the last pause's dominant error
            # fingerprint differs from what we last remediated. A different fingerprint
            # means the original root cause is gone; the UI alert should reflect that
            # rather than persisting indefinitely.
            current_fp = d.get("error_fingerprint")
            last_rem_fp = d.get("last_remediation_fingerprint")
            if (
                d.get("needs_manual_intervention")
                and current_fp and last_rem_fp
                and str(current_fp) != str(last_rem_fp)
            ):
                d.pop("needs_manual_intervention", None)
                logger.info(
                    f"[AUTONOMOUS] Cleared needs_manual_intervention on {d.get('id')}: "
                    f"fingerprint changed since remediation"
                )
            # Start remediation-effectiveness monitoring when recovering a direction whose
            # remediation task merged. Subsequent failures with the same error fingerprint
            # during this window signal the fix did not hold; escalation is triggered below
            # when the direction is re-paused while monitoring is active.
            if d.get("last_remediation_result") in ("merged", "partial_success"):
                d["remediation_monitor_started_at"] = _utcnow().isoformat()
            logger.info(f"[AUTONOMOUS] Auto-recovered direction {d.get('id')} after {hours:.0f}h cooldown")
            directions_modified = True
        else:
            d["paused_at"] = _utcnow().isoformat()
            logger.info(f"[AUTONOMOUS] Direction {d.get('id')} still unhealthy, extending pause")
            directions_modified = True
    active_directions = []
    auto_paused_any = False
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        did = d.get("id")
        # Skip directions with an unrolled research cycle still in progress
        if did and did in busy_direction_ids:
            logger.debug(
                f"[RESEARCH] Skipping direction {did}: research cycle already active"
            )
            continue
        # Clear stale diagnosis flags so a re-paused direction can be diagnosed again.
        # If the investigation task was reaped/timed out without reaching the completion handler,
        # diagnosis_in_progress stays True and permanently blocks future diagnosis spawning.
        if d.get("diagnosis_in_progress"):
            started_at = d.get("diagnosis_started_at")
            is_stale = True
            if started_at:
                try:
                    age_h = (_utcnow() - datetime.fromisoformat(started_at)).total_seconds() / 3600
                    is_stale = age_h >= _DIAGNOSIS_STALE_HOURS
                except (ValueError, TypeError):
                    is_stale = True
            if is_stale:
                logger.warning(
                    f"[DIAGNOSIS] Clearing stale diagnosis flags for direction {did} "
                    f"(started_at={started_at})"
                )
                d.pop("diagnosis_in_progress", None)
                d.pop("diagnosis_started_at", None)
                directions_modified = True
        # If a remediation monitor has expired while the direction stayed active,
        # the fix held — drop the monitor and clear any lingering manual-intervention
        # alert so the UI stops showing a permanent red banner after recovery.
        monitor_started_at = d.get("remediation_monitor_started_at")
        if monitor_started_at:
            expired = False
            age_desc = ""
            try:
                age_days = (_utcnow() - datetime.fromisoformat(monitor_started_at)).total_seconds() / 86400
                if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                    expired = True
                    age_desc = f"age {age_days:.1f}d"
            except (TypeError, ValueError):
                # Unparseable timestamp — safer to treat the monitor as expired
                # than to keep it active indefinitely and risk a false escalation.
                expired = True
                age_desc = f"unparseable timestamp {monitor_started_at!r}"
            if expired:
                d.pop("remediation_monitor_started_at", None)
                cleared = d.pop("needs_manual_intervention", None)
                # Stamp "held" on the remediation_history entry that started this monitor
                # so the digest funnel can count fixes that survived the full window.
                held_iso = _utcnow().isoformat()
                history = list(d.get("remediation_history") or [])
                for entry in reversed(history):
                    if entry.get("result") in ("merged", "partial_success"):
                        entry["result"] = "held"
                        entry["held_at"] = held_iso
                        d["remediation_history"] = history
                        break
                d["last_remediation_held_at"] = held_iso
                if d.get("last_remediation_result") in ("merged", "partial_success"):
                    d["last_remediation_result"] = "held"
                logger.info(
                    f"[AUTONOMOUS] Remediation monitor expired for {did} "
                    f"({age_desc}); fix held; "
                    f"{'cleared needs_manual_intervention' if cleared else 'no intervention flag to clear'}"
                )
                directions_modified = True
        last_researched = d.get("last_researched_at")
        if last_researched:
            minutes_ago = (now - datetime.fromisoformat(last_researched)).total_seconds() / 60
            # Adaptive cooldown: shorter after productive cycles (findings spawned tasks),
            # standard after unproductive cycles. Saturation still doubles whichever base applies.
            if d.get("last_cycle_productive"):
                effective_cooldown = float(
                    _runtime_config.get("productive_direction_cooldown_minutes", 45)
                )
            else:
                # Tiered unproductive cooldown: 1st consecutive failure waits 0.375x,
                # 2nd waits 0.75x, 3rd+ waits full. Avoids burning hours on transient
                # failures while still throttling persistently-broken directions.
                consecutive = int(d.get("consecutive_unproductive_cycles", 0) or 0)
                if consecutive <= 1:
                    multiplier = 0.375
                elif consecutive == 2:
                    multiplier = 0.75
                else:
                    multiplier = 1.0
                effective_cooldown = cooldown_minutes * multiplier
            if d.get("possibly_saturated"):
                effective_cooldown = effective_cooldown * 2.0
                logger.debug(
                    f"[RESEARCH] Direction {did} saturated — applying 2x cooldown ({effective_cooldown}m)"
                )
            if minutes_ago < effective_cooldown:
                logger.debug(
                    f"[RESEARCH] Skipping direction {did}: last researched "
                    f"{minutes_ago:.1f}m ago (cooldown: {effective_cooldown}m)"
                )
                continue
        # Auto-pause check — must come after cooldown to only evaluate directions eligible for research
        pause_reason = _check_direction_health(d)
        if pause_reason:
            d["status"] = "paused"
            d["paused_reason"] = pause_reason
            d["paused_at"] = _utcnow().isoformat()
            d["auto_paused"] = True
            d["auto_pause_count"] = d.get("auto_pause_count", 0) + 1
            # Only code-failure pauses (Rules 3/4) count toward auto-retirement.
            # Infrastructure-driven pauses (saturation, unproductive) shouldn't
            # permanently retire an otherwise healthy direction.
            if _is_code_failure_pause_reason(pause_reason):
                d["code_failure_pause_count"] = d.get("code_failure_pause_count", 0) + 1
            # Stamp the dominant error fingerprint from the direction's recent failures
            # so the diagnostics API can surface it and escalation can verify that the
            # same root cause keeps recurring after remediation.
            if _is_code_failure_pause_reason(pause_reason):
                try:
                    fingerprint = await _compute_direction_error_fingerprint(project.id, did, session)
                    if fingerprint:
                        d["error_fingerprint"] = fingerprint
                except Exception as _fp_exc:
                    logger.warning(f"[DIAGNOSIS] Failed to compute error fingerprint for {did}: {_fp_exc!r}")
            # Re-pause while remediation monitoring is active means the fix did not hold.
            # Flag for manual intervention once attempts reach the escalation threshold so
            # the diagnostics API and notification dispatcher can surface it to operators.
            monitor_started_at = d.get("remediation_monitor_started_at")
            monitor_expired = False
            if monitor_started_at:
                try:
                    age_days = (_utcnow() - datetime.fromisoformat(monitor_started_at)).total_seconds() / 86400
                    if age_days > _REMEDIATION_MONITOR_EXPIRY_DAYS:
                        monitor_expired = True
                        d.pop("remediation_monitor_started_at", None)
                        logger.info(
                            f"[AUTONOMOUS] Cleared stale remediation monitor on {did} "
                            f"(age {age_days:.1f}d > {_REMEDIATION_MONITOR_EXPIRY_DAYS}d)"
                        )
                except (TypeError, ValueError):
                    # Unparseable timestamp — safe default is to treat the monitor
                    # as expired so escalation logic does not fire off a bad value.
                    monitor_expired = True
                    d.pop("remediation_monitor_started_at", None)
                    logger.warning(
                        f"[AUTONOMOUS] Cleared unparseable remediation monitor on {did}: "
                        f"{monitor_started_at!r}"
                    )
            # Only escalate when the monitor is active AND the recurring failure matches
            # the fingerprint we already remediated (or fingerprint data is unavailable —
            # conservative fallback so we don't silently drop escalations).
            current_fp = d.get("error_fingerprint")
            last_rem_fp = d.get("last_remediation_fingerprint")
            fingerprints_match = (
                not current_fp or not last_rem_fp or str(current_fp) == str(last_rem_fp)
            )
            if (
                monitor_started_at and not monitor_expired
                and d.get("remediation_attempts", 0) >= 3
                and fingerprints_match
            ):
                if _detect_partial_remediation_success(d):
                    # Fix materially reduced failure rate even though the
                    # dominant fingerprint is still present — treat as partial
                    # progress so the direction is not escalated to manual
                    # intervention on the next remediation cycle.
                    d["last_remediation_result"] = "partial_success"
                    logger.info(
                        f"[AUTONOMOUS] Direction {did} showed partial remediation success "
                        f"(code-failure rate dropped >= {int(_PARTIAL_SUCCESS_RATE_DROP * 100)}%); "
                        f"skipping manual-intervention escalation"
                    )
                else:
                    d["needs_manual_intervention"] = True
                    logger.warning(
                        f"[AUTONOMOUS] Direction {did} flagged needs_manual_intervention "
                        f"after {d.get('remediation_attempts', 0)} remediation attempts"
                    )
                    try:
                        from app.notification_dispatch import post_remediation_escalation_message  # noqa: PLC0415
                        await post_remediation_escalation_message(
                            session, project.id, **_build_escalation_args(d),
                        )
                        # Clear any pending retry stamps from a prior failed escalation
                        d.pop("escalation_notification_pending", None)
                        d.pop("escalation_notification_retries", None)
                    except Exception as _esc_exc:
                        logger.warning(f"[NOTIFY] Failed to post remediation escalation message: {_esc_exc}")
                        # Stamp for retry on the next lifecycle cycle. Retry count is
                        # preserved if an earlier escalation on this direction was also
                        # pending; the retry loop enforces _MAX_ESCALATION_NOTIFICATION_RETRIES.
                        d["escalation_notification_pending"] = True
                        d["escalation_notification_retries"] = int(
                            d.get("escalation_notification_retries", 0)
                        )
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            auto_paused_any = True
            try:
                from app.notification_dispatch import post_direction_milestone_message  # noqa: PLC0415
                await post_direction_milestone_message(
                    session, project.id, d.get("text", did), "auto-paused", pause_reason
                )
            except Exception as _e:
                logger.warning(f"[NOTIFY] Failed to post auto-paused message: {_e}")
            try:
                await _spawn_direction_diagnosis(project, d, pause_reason, session)
            except Exception as _e:
                logger.warning(f"[DIAGNOSIS] Failed to spawn investigate task for {did}: {_e}")
            MAX_AUTO_PAUSE_CYCLES = _MAX_AUTO_PAUSE_CYCLES
            code_pause_count = d.get("code_failure_pause_count", 0)
            if code_pause_count >= MAX_AUTO_PAUSE_CYCLES:
                d["status"] = "completed"
                d["auto_completed"] = True
                d["auto_completed_reason"] = (
                    f"Auto-retired: direction hit {code_pause_count} code-failure "
                    f"auto-pauses without producing results. Slot freed for new directions."
                )
                d["completed_at"] = _utcnow().isoformat()
                logger.info(
                    f"[AUTONOMOUS] Auto-retired direction {did} after "
                    f"{code_pause_count} code-failure auto-pause cycles: {d.get('text', '')[:60]}"
                )
                from app.models import Alert, AlertSeverity  # noqa: PLC0415
                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.INFO,
                    title="Direction auto-retired",
                    message=(
                        f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                        f"{code_pause_count} consecutive code-failure auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    ),
                )
                session.add(alert)
                try:
                    from app.notification_dispatch import post_direction_milestone_message as _post_milestone  # noqa: PLC0415
                    _retire_reason = (
                        f"Auto-retired after {code_pause_count} consecutive code-failure auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    )
                    await _post_milestone(
                        session, project.id, d.get("text", did), "auto-retired", _retire_reason
                    )
                except Exception as _e:
                    logger.warning(f"[NOTIFY] Failed to post auto-retired message: {_e}")
                directions_modified = True
            else:
                # Spawn diagnosis research so the agent can investigate the failure
                # pattern before the 48h cooldown elapses — skipped on auto-retire
                # since a retired direction is terminal and has no future cycles.
                try:
                    from app.autonomous_research import _spawn_direction_diagnosis  # noqa: PLC0415
                    if await _spawn_direction_diagnosis(project, d, pause_reason, session):
                        d["diagnosis_spawned_at"] = _utcnow().isoformat()
                        directions_modified = True
                except Exception as _e:
                    logger.warning(
                        f"[DIAGNOSIS] Failed to spawn diagnosis research for {did}: {_e}"
                    )
            continue
        # Positive completion detection — mark for user review but don't pause
        completion_suggestion = _check_direction_completion(d)
        if completion_suggestion:
            if not d.get("possibly_complete"):
                d["possibly_complete"] = True
                d["completion_suggestion"] = completion_suggestion
                logger.info(f"[AUTONOMOUS] Direction {did} possibly complete: {completion_suggestion}")
                directions_modified = True
        else:
            if d.get("possibly_complete"):
                d.pop("possibly_complete", None)
                d.pop("completion_suggestion", None)
                directions_modified = True
        active_directions.append(d)
    if auto_paused_any or directions_modified:
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
    return active_directions
