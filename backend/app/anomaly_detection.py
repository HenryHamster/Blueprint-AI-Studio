"""Anomaly detection for the research pipeline.

Extracted from app.task_completion to keep that module focused on completion
handlers. Functions here analyze task failure patterns and propose directions
based on persistent anomalies.
"""
import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

import app.shared_state as _shared_state
from app.models import Project, Task, TaskStatus
from app.strategic_completion import _is_direction_duplicate
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _as_iso(v) -> str:
    """Return ``v`` as an ISO-8601 string.

    ``persistent_anomalies`` stores ``first_seen`` / ``last_seen`` as ISO strings,
    but a legacy persisted snapshot (before the restore was fixed) may still
    leave datetime objects in memory across a pod restart. This coerces both
    shapes so string comparison against ``stale_cutoff`` is well-defined.
    """
    if isinstance(v, datetime):
        return v.isoformat()
    return v or ""


def _normalize_error_message(msg: str) -> str:
    """Strip task-specific identifiers from an error message so identical root causes group together.

    Removes UUIDs, PR numbers, branch names, timestamps, and numeric IDs so that
    messages differing only in task-specific details produce the same fingerprint.
    """
    if not msg:
        return ""
    # Strip UUIDs (8-4-4-4-12 hex)
    normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<uuid>', msg, flags=re.IGNORECASE)
    # Strip PR numbers (e.g. PR #123, #123, pr/123)
    normalized = re.sub(r'(?:PR\s*)?#\d+', '<pr>', normalized, flags=re.IGNORECASE)
    # Strip branch names like blueprint/task-abc123 or feature/some-branch-42
    normalized = re.sub(r'\b[a-z][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*\b', '<branch>', normalized, flags=re.IGNORECASE)
    # Strip ISO timestamps (2026-04-11T12:34:56.789Z etc.)
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?', '<ts>', normalized)
    # Strip remaining standalone numbers (keep short words that happen to be digits like exit codes)
    normalized = re.sub(r'\b\d{4,}\b', '<id>', normalized)
    return normalized.strip()


def _error_fingerprint(msg: str) -> str:
    """Return a short hash fingerprint of a normalized error message."""
    normalized = _normalize_error_message(msg or "")
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def count_matching_fingerprints(target_fingerprint: str, failed_tasks) -> int:
    """Count how many failed tasks share ``target_fingerprint``.

    Remediation-effectiveness tracking: after a remediation task completes for a
    direction, callers (direction_lifecycle) monitor subsequent failed tasks to
    decide whether the fix held. A non-zero return means the same root-cause
    error recurred and the remediation was ineffective.

    Accepts any iterable of objects exposing ``latest_status_message``. Tasks
    with no message are skipped (they cannot be fingerprinted meaningfully).
    """
    if not target_fingerprint:
        return 0
    count = 0
    for task in failed_tasks or ():
        msg = getattr(task, "latest_status_message", None) or ""
        if msg and _error_fingerprint(msg) == target_fingerprint:
            count += 1
    return count


# Window for considering a direction "recently paused" when checking cross-direction
# fingerprint overlap. Matches the default auto-recovery cooldown — pauses older than
# this are assumed stale or already recovered and no longer evidence of a systemic
# issue.
_SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS = 48


def find_directions_sharing_fingerprint(
    project, target_fingerprint: str, exclude_direction_id: Optional[str] = None,
) -> list[dict]:
    """Return other recently auto-paused directions that share ``target_fingerprint``.

    When multiple directions fail with the same normalized error fingerprint, a
    shared root cause (broken dependency, infra issue, framework bug) is more
    likely than direction-specific problems. The auto-pause path uses this to
    annotate diagnosis context with the systemic signal so the investigating
    agent scopes its fix correctly.

    Scans ``project.directions`` for entries with ``status=='paused'``,
    ``auto_paused`` truthy, and ``error_fingerprint == target_fingerprint``,
    excluding the direction identified by ``exclude_direction_id``. Only pauses
    within the last ``_SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS`` count. Returns
    compact dicts (``id``/``text``/``paused_at``/``auto_pause_count``) so callers
    can cheaply embed the list in a prompt. Returns an empty list when
    ``target_fingerprint`` is empty.
    """
    if not target_fingerprint:
        return []
    cutoff = _utcnow() - timedelta(hours=_SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS)
    matches: list[dict] = []
    for d in (getattr(project, "directions", None) or []):
        if exclude_direction_id and d.get("id") == exclude_direction_id:
            continue
        if d.get("status") != "paused" or not d.get("auto_paused"):
            continue
        if d.get("error_fingerprint") != target_fingerprint:
            continue
        paused_at_raw = d.get("paused_at")
        if paused_at_raw:
            try:
                paused_at = datetime.fromisoformat(paused_at_raw)
                if paused_at.tzinfo is not None:
                    paused_at = paused_at.astimezone(timezone.utc).replace(tzinfo=None)
                if paused_at < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
        matches.append({
            "id": d.get("id"),
            "text": (d.get("text") or "")[:100],
            "paused_at": paused_at_raw,
            "auto_pause_count": int(d.get("auto_pause_count", 0) or 0),
        })
    return matches


def _is_recurring_failure_class(proj_anomalies: dict, failure_class: str) -> bool:
    """Return True if the given failure class has a persistent anomaly with occurrence_count >= 3."""
    entry = proj_anomalies.get(failure_class)
    return bool(entry and entry.get("occurrence_count", 0) >= 3)


async def _compute_research_anomalies(project_id, session: AsyncSession) -> list:
    """Analyze recent task data to identify pipeline anomalies for undirected research focus.

    Returns a list of {signal: str, severity: 'high'|'medium', details: str} dicts.
    """
    # Deferred import to avoid circular dependency (main.py imports this module)
    from app.failure_classification import classify_failure  # noqa: PLC0415

    now = _utcnow()
    window_48h = now - timedelta(hours=48)
    prior_window_start = now - timedelta(hours=96)

    # Query last 50 terminal tasks for this project
    terminal_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        ).order_by(Task.created_at.desc()).limit(50)
    )).scalars().all()

    anomalies = []

    # --- Failure class analysis ---
    recent_failures = [t for t in terminal_tasks if t.created_at and t.created_at >= window_48h]
    prior_failures = [t for t in terminal_tasks if t.created_at and prior_window_start <= t.created_at < window_48h]

    # Group by failure class
    recent_by_class: dict = {}
    for t in recent_failures:
        cls = classify_failure(t)
        recent_by_class.setdefault(cls, []).append(t)

    prior_by_class: dict = {}
    for t in prior_failures:
        cls = classify_failure(t)
        prior_by_class[cls] = prior_by_class.get(cls, 0) + 1

    for cls, tasks in recent_by_class.items():
        count = len(tasks)
        prior_count = prior_by_class.get(cls, 0)

        # Build top error patterns when there are enough failures to be interesting
        top_patterns: list[dict] = []
        if count > 3:
            pattern_counts: dict[str, dict] = {}
            for t in tasks:
                msg = t.latest_status_message or ""
                fp = _error_fingerprint(msg)
                if fp not in pattern_counts:
                    pattern_counts[fp] = {"fingerprint": fp, "count": 0, "example_message": msg}
                pattern_counts[fp]["count"] += 1
            # Sort descending by count, take top 3
            top_patterns = sorted(pattern_counts.values(), key=lambda x: x["count"], reverse=True)[:3]

        if count > 3 and prior_count > 0 and count >= prior_count * 2:
            anomaly: dict = {
                "signal": f"{cls} failures increased {prior_count}x → {count}x in last 48h",
                "severity": "high",
                "details": (
                    f"{cls} failure count rose from {prior_count} to {count} between the prior "
                    f"48h window and the current one. Investigate common error patterns."
                ),
            }
            if top_patterns:
                anomaly["top_patterns"] = top_patterns
            anomalies.append(anomaly)
        elif count > 3:
            pattern_summary = "; ".join(
                f"({p['count']}x) \"{p['example_message'][:80]}\"" for p in top_patterns
            ) if top_patterns else ""
            detail_msg = (
                f"{count} tasks failed with class {cls} in the last 48h. "
                "Consider investigating shared root causes."
            )
            if pattern_summary:
                detail_msg += f" Top patterns: {pattern_summary}"
            anomaly = {
                "signal": f"{cls} failures: {count} occurrences in last 48h",
                "severity": "medium",
                "details": detail_msg,
            }
            if top_patterns:
                anomaly["top_patterns"] = top_patterns
            anomalies.append(anomaly)

    # --- Direction merge rate and failure rate analysis ---
    project = (await session.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project:
        for direction in (project.directions or []):
            if direction.get("status") != "active":
                continue
            history = direction.get("research_history") or []
            d_id = direction.get("id", "?")
            d_text = (direction.get("text") or "")[:60]

            # Declining merge rate across last 3 cycles
            if len(history) >= 3:
                last3 = history[-3:]
                rates = []
                for entry in last3:
                    created = entry.get("tasks_created", 0)
                    merged = entry.get("prs_merged", 0)
                    rates.append(merged / created if created > 0 else 0.0)
                if rates[0] > rates[1] > rates[2] and rates[0] > 0.1:
                    anomalies.append({
                        "signal": f"Direction '{d_id}' merge rate declining: {rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}",
                        "severity": "medium",
                        "details": (
                            f"Direction \"{d_text}\" (id: {d_id}) has seen a consistent decline in "
                            f"merge rate over the last 3 cycles ({rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}). "
                            "May indicate implementation barriers or stale findings."
                        ),
                    })

            # High failure rate in last 2 cycles (>50%)
            if len(history) >= 2:
                last2 = history[-2:]
                total_created = sum(e.get("tasks_created", 0) for e in last2)
                total_failed = sum(e.get("tasks_failed", 0) for e in last2)
                if total_created > 0 and total_failed / total_created > 0.5:
                    fail_pct = int(total_failed / total_created * 100)
                    anomalies.append({
                        "signal": f"Direction '{d_id}' has {fail_pct}% task failure rate in last 2 cycles",
                        "severity": "high",
                        "details": (
                            f"Direction \"{d_text}\" (id: {d_id}) had {total_failed}/{total_created} tasks fail "
                            f"in the last 2 research cycles ({fail_pct}% failure rate). "
                            "The findings may be generating unimplementable tasks."
                        ),
                    })

    # --- Persist anomaly tracking ---
    proj_key = str(project_id)
    proj_anomalies = _shared_state.persistent_anomalies.setdefault(proj_key, {})
    now_iso = now.isoformat()
    seen_keys: set = set()
    for a in anomalies:
        # Build a stable key from the signal's failure class or direction id
        signal = a["signal"]
        # Extract anomaly key: e.g. "CODE_ERROR" from "CODE_ERROR failures..."
        # or "Direction_d-abc123" from "Direction 'd-abc123' ..."
        if signal.startswith("Direction '"):
            # Include direction ID to avoid key collisions across directions
            parts = signal.split("'")
            anomaly_key = f"Direction_{parts[1]}" if len(parts) > 1 else signal.split()[0]
        else:
            parts = signal.split()
            anomaly_key = parts[0] if parts else signal
        if anomaly_key in seen_keys:
            # Already processed this key this cycle — update metadata only, don't double-count
            if anomaly_key in proj_anomalies:
                proj_anomalies[anomaly_key]["last_seen"] = now_iso
                if a["severity"] == "high":
                    proj_anomalies[anomaly_key]["severity"] = "high"
                    proj_anomalies[anomaly_key]["latest_details"] = a["details"]
            continue
        seen_keys.add(anomaly_key)
        if anomaly_key in proj_anomalies:
            proj_anomalies[anomaly_key]["occurrence_count"] += 1
            proj_anomalies[anomaly_key]["last_seen"] = now_iso
            proj_anomalies[anomaly_key]["severity"] = a["severity"]
            proj_anomalies[anomaly_key]["latest_details"] = a["details"]
        else:
            proj_anomalies[anomaly_key] = {
                "first_seen": now_iso,
                "last_seen": now_iso,
                "occurrence_count": 1,
                "severity": a["severity"],
                "latest_details": a["details"],
            }
    # Expire anomalies not seen for 48h
    stale_cutoff = (now - timedelta(hours=48)).isoformat()
    for key in list(proj_anomalies):
        if key not in seen_keys and _as_iso(proj_anomalies[key].get("last_seen")) < stale_cutoff:
            del proj_anomalies[key]

    return anomalies


# ---------------------------------------------------------------------------
# Anomaly-driven pipeline adjustments
# ---------------------------------------------------------------------------

# Failure classes that trigger automatic pipeline adjustments when surging
_ACTIONABLE_FAILURE_CLASSES = frozenset({"PROVISION_FAILURE", "EXTERNAL_API"})

# Minimum occurrence_count in persistent_anomalies to trigger adjustments
_ANOMALY_ACTION_THRESHOLD = 3

# Fast-recovery: rolling window (hours) and minimum completions required
_FAST_RECOVERY_WINDOW_HOURS = 2
_FAST_RECOVERY_MIN_SAMPLE = 5
# Tolerance: allow at most this many triggering failures in the window if they
# represent less than _FAST_RECOVERY_FAILURE_RATE_MAX of total results. Avoids
# pinning the throttle for hours when one intermittent failure occurs amid
# dozens of successes.
_FAST_RECOVERY_MAX_TOLERATED_FAILURES = 1
_FAST_RECOVERY_FAILURE_RATE_MAX = 0.10

# Max-duration safeguard: force graduated restoration after this many hours
# so an anomaly that never goes stale (due to ~12-24h intermittent failures)
# doesn't pin the pipeline in throttled mode indefinitely.
_MAX_ADJUSTMENT_DURATION_HOURS = 6
# Fraction of pre-adjustment max_concurrent_vms to restore after forced
# restoration (hedge — not full value — in case the anomaly re-triggers).
_FORCED_RESTORATION_VMS_FRACTION = 0.75
# Re-entry window: if the anomaly re-triggers within this many minutes of a
# forced restoration, log it so the reapply is distinguishable from a fresh
# surge (the adjusted_at timer resets normally on reapply).
_FORCED_RESTORATION_REENTRY_WINDOW_MINUTES = 30


def _apply_anomaly_adjustments() -> bool:
    """Check persistent anomalies across all projects and apply runtime adjustments.

    When PROVISION_FAILURE or EXTERNAL_API anomalies reach high severity with
    occurrence_count >= 3, automatically:
    - Reduce max_concurrent_vms by 50% (floor of 2)
    - Lower failure-rate dispatch gate from 0.8 to 0.6

    Returns True if adjustments were applied (or were already active).
    """
    adjustments = _shared_state.anomaly_adjustments
    rc = _shared_state.runtime_config

    # Find high-severity actionable anomalies across all projects
    triggered_keys: list[str] = []
    for proj_anomalies in _shared_state.persistent_anomalies.values():
        for anomaly_key, info in proj_anomalies.items():
            if (
                anomaly_key in _ACTIONABLE_FAILURE_CLASSES
                and info.get("severity") == "high"
                and info.get("occurrence_count", 0) >= _ANOMALY_ACTION_THRESHOLD
            ):
                triggered_keys.append(anomaly_key)

    if not triggered_keys:
        return False

    if adjustments["active"]:
        # Already adjusted — update trigger list but don't re-adjust
        adjustments["trigger_anomalies"] = sorted(set(triggered_keys))
        return True

    # Detect re-entry shortly after a forced restoration so the log line
    # reflects the context (same anomaly coming back, timer restarts).
    forced_restored_at = _parse_adjustments_ts(adjustments.get("forced_restored_at"))
    is_reentry = (
        forced_restored_at is not None
        and (_utcnow() - forced_restored_at)
        < timedelta(minutes=_FORCED_RESTORATION_REENTRY_WINDOW_MINUTES)
    )

    # Store pre-adjustment values for restoration
    adjustments["pre_adjustment_max_vms"] = rc["max_concurrent_vms"]
    adjustments["pre_adjustment_failure_gate"] = adjustments["failure_gate_threshold"]

    # Apply adjustments: reduce VMs by 50% (floor 2), tighten failure gate
    new_max_vms = max(2, rc["max_concurrent_vms"] // 2)
    rc["max_concurrent_vms"] = new_max_vms
    adjustments["failure_gate_threshold"] = 0.6

    adjustments["active"] = True
    adjustments["adjusted_at"] = _utcnow().isoformat()
    adjustments["trigger_anomalies"] = sorted(set(triggered_keys))

    if is_reentry:
        logger.info(
            f"[ANOMALY_ACTION] Reapplied pipeline adjustments within "
            f"{_FORCED_RESTORATION_REENTRY_WINDOW_MINUTES}min of forced restoration: "
            f"max_vms={new_max_vms}, failure_gate=0.6 "
            f"(triggers: {', '.join(triggered_keys)}). Max-duration timer reset."
        )
    else:
        logger.info(
            f"[ANOMALY_ACTION] Applied pipeline adjustments: "
            f"max_vms={new_max_vms}, failure_gate=0.6 "
            f"(triggers: {', '.join(triggered_keys)})"
        )
    return True


def _parse_adjustments_ts(iso_value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp stored in anomaly_adjustments back into a naive UTC datetime."""
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _check_max_duration_safeguard() -> bool:
    """Force a graduated restoration if adjustments have been active beyond the max duration.

    When intermittent infra failures recur every ~12-24h, the anomaly
    occurrence_count never drops below threshold (it's cleared only by 48h
    staleness) and fast-recovery never triggers (a fresh failure in the 2h
    window keeps resetting it). Without this safeguard the throttle can
    persist indefinitely.

    Graduated restoration on forced path:
      - failure_gate_threshold is fully restored to the pre-adjustment value
      - max_concurrent_vms is restored to 75% of the pre-adjustment value
        (hedge in case the anomaly re-triggers, not full)

    Returns True if a forced restoration was applied.
    """
    adjustments = _shared_state.anomaly_adjustments
    rc = _shared_state.runtime_config

    if not adjustments["active"]:
        return False

    adjusted_at = _parse_adjustments_ts(adjustments.get("adjusted_at"))
    if adjusted_at is None:
        return False

    elapsed = _utcnow() - adjusted_at
    if elapsed < timedelta(hours=_MAX_ADJUSTMENT_DURATION_HOURS):
        return False

    pre_max_vms = adjustments["pre_adjustment_max_vms"]
    pre_gate = adjustments["pre_adjustment_failure_gate"]

    # Failure gate: always restore fully.
    if pre_gate is not None:
        adjustments["failure_gate_threshold"] = pre_gate

    # max_concurrent_vms: graduated restoration to 75% of pre-adjustment,
    # but only if the current value still matches what we set (respect
    # admin overrides made while adjustments were active).
    if pre_max_vms is not None:
        current_max_vms = rc["max_concurrent_vms"]
        expected_adjusted_vms = max(2, pre_max_vms // 2)
        if current_max_vms < expected_adjusted_vms:
            # deploy_health has paused (=0) or is ramping up (1-2) — this is NOT
            # an admin override. Defer forced restoration until deploy resumes so
            # we don't lose the pre-adjustment capacity.
            logger.info(
                f"[ANOMALY_ACTION] Deferring max-duration safeguard: current_max_vms="
                f"{current_max_vms} is below anomaly-adjusted floor ({expected_adjusted_vms}) — "
                f"deploy_health appears paused/ramping; preserving anomaly stash"
            )
            return False
        if current_max_vms == expected_adjusted_vms:
            graduated = max(2, int(pre_max_vms * _FORCED_RESTORATION_VMS_FRACTION))
            rc["max_concurrent_vms"] = graduated
        elif current_max_vms >= pre_max_vms:
            # Already at or above pre-adjustment (e.g., deploy_health ramped back
            # to the original capacity). Nothing to restore; just clear stash.
            pass
        else:
            logger.info(
                f"[ANOMALY_ACTION] max_concurrent_vms was manually changed "
                f"({expected_adjusted_vms} → {current_max_vms}) during anomaly adjustment — "
                f"keeping current value on forced restoration"
            )

    adjustments["forced_restoration_count"] = (
        adjustments.get("forced_restoration_count", 0) + 1
    )
    adjustments["forced_restored_at"] = _utcnow().isoformat()
    adjustments["active"] = False
    adjustments["adjusted_at"] = None
    adjustments["trigger_anomalies"] = []
    adjustments["pre_adjustment_max_vms"] = None
    adjustments["pre_adjustment_failure_gate"] = None

    logger.warning(
        f"[ANOMALY_ACTION] Forced restoration after "
        f"{elapsed.total_seconds() / 3600:.1f}h of anomaly adjustments "
        f"(max: {_MAX_ADJUSTMENT_DURATION_HOURS}h). "
        f"max_vms={rc['max_concurrent_vms']}, "
        f"failure_gate={adjustments['failure_gate_threshold']}. "
        f"forced_restoration_count={adjustments['forced_restoration_count']}"
    )
    return True


def _check_anomaly_restoration() -> bool:
    """Restore pre-adjustment values when all triggering anomalies have cleared.

    An anomaly is considered cleared when its occurrence_count drops below the
    threshold or it has been expired (removed from persistent_anomalies by the
    48h staleness check in _compute_research_anomalies).

    Does NOT restore if pre_adjustment_max_vms is None (manual admin override
    occurred while adjustments were active — the admin's setting takes priority).

    Returns True if adjustments were restored.
    """
    adjustments = _shared_state.anomaly_adjustments
    rc = _shared_state.runtime_config

    if not adjustments["active"]:
        return False

    # Check if any actionable anomaly still qualifies
    still_active = False
    for proj_anomalies in _shared_state.persistent_anomalies.values():
        for anomaly_key, info in proj_anomalies.items():
            if (
                anomaly_key in _ACTIONABLE_FAILURE_CLASSES
                and info.get("severity") == "high"
                and info.get("occurrence_count", 0) >= _ANOMALY_ACTION_THRESHOLD
            ):
                still_active = True
                break
        if still_active:
            break

    came_via_fast_recovery = False
    if still_active:
        # Fast-recovery path: restore proactively when recent outcomes are clean
        # even though the anomaly occurrence_count hasn't dropped yet.
        if not _check_fast_recovery():
            return False
        came_via_fast_recovery = True
        logger.info(
            "[ANOMALY_ACTION] Fast recovery: no triggering failures in %dh window "
            "with sufficient sample — restoring pipeline settings early",
            _FAST_RECOVERY_WINDOW_HOURS,
        )

    # Anomalies cleared (normal path or fast recovery) — restore previous values
    pre_max_vms = adjustments["pre_adjustment_max_vms"]
    pre_gate = adjustments["pre_adjustment_failure_gate"]

    if pre_max_vms is not None:
        # Only restore if the current value matches what we set — if an admin
        # changed it manually while adjustments were active, respect that.
        current_max_vms = rc["max_concurrent_vms"]
        expected_adjusted_vms = max(2, pre_max_vms // 2)
        if current_max_vms < expected_adjusted_vms:
            if not came_via_fast_recovery:
                # deploy_health has paused (=0) or is ramping up (1-2) — this
                # is NOT an admin override. Defer restoration until deploy_health
                # finishes ramping so we don't clear the stash and lose the
                # original capacity. deploy_health's pre_pause_max_vms already
                # captured our pre_adjustment stash at pause time (see
                # deploy_health.py:647-667), so on resume the full capacity is
                # preserved.
                logger.info(
                    f"[ANOMALY_ACTION] Deferring restoration: current_max_vms="
                    f"{current_max_vms} is below anomaly-adjusted floor ({expected_adjusted_vms}) — "
                    f"deploy_health appears paused/ramping; preserving anomaly stash"
                )
                return False
            # Fast-recovery affirmed the trigger cleared via health-history
            # probes while deploy_health held rc below our floor. deploy_health
            # now owns max_concurrent_vms — leave rc alone and clear the
            # anomaly stash so the resume path can ramp cleanly.
            logger.info(
                "[ANOMALY_ACTION] Fast recovery via health history: leaving "
                "max_concurrent_vms=%d for deploy_health resume",
                current_max_vms,
            )
        elif current_max_vms == expected_adjusted_vms:
            rc["max_concurrent_vms"] = pre_max_vms
        elif current_max_vms >= pre_max_vms:
            # Already at or above pre-adjustment (deploy_health ramp completed
            # or admin bumped past original). Nothing to restore; clear stash.
            pass
        else:
            logger.info(
                f"[ANOMALY_ACTION] max_concurrent_vms was manually changed "
                f"({expected_adjusted_vms} → {current_max_vms}) during anomaly adjustment — "
                f"keeping current value"
            )

    if pre_gate is not None:
        adjustments["failure_gate_threshold"] = pre_gate

    adjustments["active"] = False
    adjustments["adjusted_at"] = None
    adjustments["trigger_anomalies"] = []
    adjustments["pre_adjustment_max_vms"] = None
    adjustments["pre_adjustment_failure_gate"] = None

    logger.info(
        f"[ANOMALY_ACTION] Restored pipeline settings: "
        f"max_vms={rc['max_concurrent_vms']}, "
        f"failure_gate={adjustments['failure_gate_threshold']}"
    )
    return True


def _check_fast_recovery() -> bool:
    """Return True if recent task outcomes indicate the triggering issues have resolved.

    Examines the rolling 2-hour task_results window.  Restoration is only allowed
    when BOTH conditions hold:

    1. At least ``_FAST_RECOVERY_MIN_SAMPLE`` completions (success or failure) exist
       in the window — avoids false positives during idle periods.
    2. Zero failures whose failure_class matches any trigger anomaly — a single
       fresh failure of the triggering class keeps the throttle in place.

    For PROVISION_FAILURE triggers an additional check is applied: zero
    ``provision_timeout_pre_claim`` pipeline events in the same 2-hour window,
    which confirms that the GCP zone layer is no longer rejecting VMs.

    This is deliberately conservative: presence of *any* triggering failure in
    the window blocks fast recovery, even if the overall failure rate is low.
    """
    adjustments = _shared_state.anomaly_adjustments
    trigger_anomalies = set(adjustments.get("trigger_anomalies", []))
    if not trigger_anomalies:
        return False

    cutoff = (_utcnow() - timedelta(hours=_FAST_RECOVERY_WINDOW_HOURS)).isoformat()
    recent_results = [
        r for r in _shared_state.deploy_monitoring["task_results"]
        if r[0] >= cutoff
    ]

    # Need a meaningful sample to avoid restoring during an idle lull.
    # Exception: when the pipeline is paused (max_concurrent_vms == 0), no tasks
    # run and task_results stops growing, so the min-sample bar can never be met.
    # Fall back to the deploy_health recovery ring buffer (probes continue during
    # pause). If it shows a full window of healthy probes, treat that as
    # equivalent evidence that the triggering condition has resolved.
    if len(recent_results) < _FAST_RECOVERY_MIN_SAMPLE:
        if _shared_state.runtime_config.get("max_concurrent_vms", 0) != 0:
            return False
        from app.deploy_health import (  # noqa: PLC0415
            _HEALTH_HISTORY_SIZE,
            get_recovery_history_snapshot,
        )
        history = get_recovery_history_snapshot()
        if len(history) < _HEALTH_HISTORY_SIZE or not all(history):
            return False
        return True

    # Tolerate a small number of triggering-class failures when they represent
    # a tiny fraction of recent results (one intermittent failure shouldn't pin
    # the throttle for hours). Genuine ongoing issues exceed either bound.
    triggering_failures = sum(
        1 for r in recent_results
        if r[1] == "failure" and len(r) > 2 and r[2] in trigger_anomalies
    )
    if triggering_failures > _FAST_RECOVERY_MAX_TOLERATED_FAILURES:
        return False
    if triggering_failures > 0 and (
        triggering_failures / len(recent_results) >= _FAST_RECOVERY_FAILURE_RATE_MAX
    ):
        return False

    # For PROVISION_FAILURE: also verify no recent provision timeout pipeline events
    if "PROVISION_FAILURE" in trigger_anomalies:
        window_cutoff_dt = _utcnow() - timedelta(hours=_FAST_RECOVERY_WINDOW_HOURS)
        recent_provision_events = sum(
            1 for e in _shared_state.deploy_monitoring["pipeline_events"]
            if e.get("type") == "provision_timeout_pre_claim"
            and e.get("ts") and e["ts"] >= window_cutoff_dt
        )
        if recent_provision_events > 0:
            return False

    return True


async def check_anomaly_actions() -> dict:
    """Top-level entry point: apply or restore anomaly-driven adjustments.

    Called after _compute_research_anomalies in the autonomous research loop.
    Returns a dict describing what happened for broadcast purposes.
    'is_new' is True only on the first activation (not on repeat cycles while
    already active), so callers can avoid creating duplicate alerts.

    Ordering:
    1. Max-duration safeguard — if adjustments have been active beyond the
       limit, force graduated restoration regardless of anomaly state.
    2. Apply adjustments when triggering anomalies still qualify.
    3. Check normal/fast-recovery restoration otherwise.
    """
    forced = _check_max_duration_safeguard()
    if forced:
        return {
            "action": "forced_restored",
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments["failure_gate_threshold"],
            "forced_restoration_count": _shared_state.anomaly_adjustments["forced_restoration_count"],
        }

    was_active = _shared_state.anomaly_adjustments["active"]
    applied = _apply_anomaly_adjustments()
    if applied:
        return {
            "action": "adjusted",
            "is_new": not was_active,
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments["failure_gate_threshold"],
            "trigger_anomalies": _shared_state.anomaly_adjustments["trigger_anomalies"],
            "pre_adjustment_max_vms": _shared_state.anomaly_adjustments["pre_adjustment_max_vms"],
        }

    restored = _check_anomaly_restoration()
    if restored:
        return {
            "action": "restored",
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments["failure_gate_threshold"],
        }

    return {"action": "none"}


def _expire_stale_anomalies() -> int:
    """Remove persistent anomalies whose last_seen is older than 48h.

    Independent of detection runs — safe to call on a schedule even when no
    research cycle has touched a project recently. Returns the count expired.
    """
    stale_cutoff = (_utcnow() - timedelta(hours=48)).isoformat()
    expired = 0
    for proj_key, proj_anomalies in list(_shared_state.persistent_anomalies.items()):
        for key in list(proj_anomalies):
            last_seen = _as_iso(proj_anomalies[key].get("last_seen"))
            if last_seen and last_seen < stale_cutoff:
                del proj_anomalies[key]
                expired += 1
        if not proj_anomalies:
            del _shared_state.persistent_anomalies[proj_key]
    return expired


async def _spawn_held_direction_investigations(session: AsyncSession) -> int:
    """Spawn investigation tasks for every direction currently on hold.

    Complements autonomous_research._check_idle_research's per-project invocation
    by running on the faster anomaly-sweep cadence (~10 min vs 30 min) and by
    covering projects with autonomous_enabled=False — direction holds fire for
    every project, but the research idle loop only iterates autonomous ones.

    Delegates the per-project spawn (including in-flight dedup and failure-context
    collection) to the existing helper in autonomous_research so the two entry
    points stay in sync.
    """
    # Deferred import: autonomous_research imports from anomaly_detection.
    from app.autonomous_research import _spawn_direction_hold_investigation  # noqa: PLC0415

    held = _shared_state.get_held_directions()
    if not held:
        return 0

    project_ids: set[str] = {
        info.get("project_id") for info in held.values() if info.get("project_id")
    }
    total = 0
    for pid_str in project_ids:
        try:
            pid = uuid.UUID(pid_str)
        except (TypeError, ValueError):
            continue
        project = await session.get(Project, pid)
        if project is None:
            continue
        try:
            total += await _spawn_direction_hold_investigation(project, session)
        except Exception:
            logger.exception(
                "[ANOMALY_SWEEP] direction-hold investigation spawn failed for project %s",
                pid_str,
            )
    return total


async def anomaly_maintenance_sweep() -> dict:
    """Periodic maintenance independent of autonomous research.

    Expires stale persistent anomalies (>48h since last_seen), re-evaluates
    pipeline adjustments so that throttles get restored even when no research
    cycle is running, and spawns investigation tasks for any directions that
    are currently on hold. Returns a small summary dict for logging.
    """
    expired = _expire_stale_anomalies()
    action = await check_anomaly_actions()
    spawned = 0
    # Skip the DB session entirely when no directions are held — the common case
    # on a healthy pipeline, and it keeps standalone callers DB-free.
    if _shared_state.get_held_directions():
        try:
            from app.database import AsyncSessionLocal  # noqa: PLC0415
            async with AsyncSessionLocal() as session:
                spawned = await _spawn_held_direction_investigations(session)
        except Exception:
            logger.exception("[ANOMALY_SWEEP] held-direction investigation pass failed")
    if expired or action["action"] != "none" or spawned:
        logger.info(
            "[ANOMALY_SWEEP] expired=%d action=%s spawned_investigations=%d",
            expired, action["action"], spawned,
        )
    return {
        "expired": expired,
        "action": action["action"],
        "spawned_investigations": spawned,
    }


async def _check_anomaly_proposals(project, session):
    """Generate direction proposals from persistent anomalies (occurrence_count >= 3).

    Creates at most 1 proposal per call. Proposals require human approval (status='proposed').
    """
    proj_key = str(project.id)
    proj_anomalies = _shared_state.persistent_anomalies.get(proj_key, {})
    if not proj_anomalies:
        return

    proj_directions = list(project.directions or [])
    rejected_proposals = [d for d in proj_directions if d.get("status") == "rejected"]
    pending_count = sum(1 for d in proj_directions if d.get("status") == "proposed")
    max_proposals = 10

    if pending_count >= max_proposals:
        return

    now_iso = _utcnow().isoformat()
    for anomaly_key, info in sorted(proj_anomalies.items(), key=lambda x: x[1].get("occurrence_count", 0), reverse=True):
        if info["occurrence_count"] < 3:
            continue

        # Build proposal text based on the anomaly key pattern
        details = info.get("latest_details", "")
        if anomaly_key.startswith("Direction_"):
            # Direction-level anomaly (declining merge rate / high failure)
            proposal_text = f"Investigate {anomaly_key} anomaly — {details}"[:500]
        else:
            # Failure class anomaly (e.g. CODE_ERROR, PROVISION_FAILURE)
            proposal_text = f"Investigate and reduce {anomaly_key} task failures — {details}"[:500]

        if _is_direction_duplicate(proposal_text, proj_directions, rejected_proposals):
            logger.info(f"[ANOMALY] Skipping proposal for {anomaly_key} — overlaps with existing direction")
            continue

        new_direction = {
            "id": f"d-{uuid.uuid4().hex[:8]}",
            "text": proposal_text,
            "priority": "high" if info["severity"] == "high" else "medium",
            "paths": [],
            "added_at": now_iso,
            "status": "proposed",
            "proposed_by": "ANOMALY_DETECTOR",
            "proposal_rationale": (
                f"Anomaly '{anomaly_key}' detected {info['occurrence_count']} times "
                f"since {info['first_seen']}. Severity: {info['severity']}."
            )[:500],
            "proposed_at": now_iso,
        }
        proj_directions.append(new_direction)
        project.directions = proj_directions
        flag_modified(project, "directions")
        session.add(project)
        logger.info(f"[ANOMALY] Created direction proposal for persistent anomaly: {anomaly_key}")

        # Reset occurrence count so we don't re-propose until it recurs
        info["occurrence_count"] = 0

        # Cap at 1 anomaly-driven proposal per cycle
        break


async def _expire_stale_proposals(session) -> int:
    """Expire direction proposals older than 30 days across all projects."""
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    projects = (await session.execute(
        select(Project).where(Project.autonomous_enabled.is_(True))
    )).scalars().all()
    total_expired = 0
    for project in projects:
        dirs = list(project.directions or [])
        expired_count = 0
        for d in dirs:
            if d.get("status") == "proposed" and (d.get("proposed_at") or d.get("added_at", "")) < cutoff:
                d["status"] = "expired"
                d["expired_at"] = _utcnow().isoformat()
                expired_count += 1
        if expired_count:
            project.directions = dirs
            flag_modified(project, "directions")
            session.add(project)
            total_expired += expired_count
            logger.info(f"[DIRECTIONS] Expired {expired_count} stale proposals for project {project.id}")
    if total_expired:
        await session.commit()
    return total_expired
