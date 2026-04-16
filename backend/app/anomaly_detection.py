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
        if key not in seen_keys and proj_anomalies[key]["last_seen"] < stale_cutoff:
            del proj_anomalies[key]

    return anomalies


# ---------------------------------------------------------------------------
# Anomaly-driven pipeline adjustments
# ---------------------------------------------------------------------------

# Failure classes that trigger automatic pipeline adjustments when surging
_ACTIONABLE_FAILURE_CLASSES = frozenset({"PROVISION_FAILURE", "EXTERNAL_API"})

# Minimum occurrence_count in persistent_anomalies to trigger adjustments
_ANOMALY_ACTION_THRESHOLD = 3


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

    logger.info(
        f"[ANOMALY_ACTION] Applied pipeline adjustments: "
        f"max_vms={new_max_vms}, failure_gate=0.6 "
        f"(triggers: {', '.join(triggered_keys)})"
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

    if still_active:
        return False

    # Anomalies cleared — restore previous values
    pre_max_vms = adjustments["pre_adjustment_max_vms"]
    pre_gate = adjustments["pre_adjustment_failure_gate"]

    if pre_max_vms is not None:
        # Only restore if the current value matches what we set — if an admin
        # changed it manually while adjustments were active, respect that.
        current_max_vms = rc["max_concurrent_vms"]
        expected_adjusted_vms = max(2, pre_max_vms // 2)
        if current_max_vms == expected_adjusted_vms:
            rc["max_concurrent_vms"] = pre_max_vms
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


async def check_anomaly_actions() -> dict:
    """Top-level entry point: apply or restore anomaly-driven adjustments.

    Called after _compute_research_anomalies in the autonomous research loop.
    Returns a dict describing what happened for broadcast purposes.
    """
    applied = _apply_anomaly_adjustments()
    if applied:
        return {
            "action": "adjusted",
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments["failure_gate_threshold"],
            "trigger_anomalies": _shared_state.anomaly_adjustments["trigger_anomalies"],
        }

    restored = _check_anomaly_restoration()
    if restored:
        return {
            "action": "restored",
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments["failure_gate_threshold"],
        }

    return {"action": "none"}


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
