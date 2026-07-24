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
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.shared_state as _shared_state
from app.auth import require_admin
from app.database import get_session
from app.models import (
    Project,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow

router = APIRouter()

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
    normalized = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<uuid>",
        msg,
        flags=re.IGNORECASE,
    )
    # Strip PR numbers (e.g. PR #123, #123, pr/123)
    normalized = re.sub(r"(?:PR\s*)?#\d+", "<pr>", normalized, flags=re.IGNORECASE)
    # Strip branch names like blueprint/task-abc123 or feature/some-branch-42
    normalized = re.sub(
        r"\b[a-z][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*\b",
        "<branch>",
        normalized,
        flags=re.IGNORECASE,
    )
    # Strip ISO timestamps (2026-04-11T12:34:56.789Z etc.)
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        "<ts>",
        normalized,
    )
    # Strip remaining standalone numbers (keep short words that happen to be digits like exit codes)
    normalized = re.sub(r"\b\d{4,}\b", "<id>", normalized)
    return normalized.strip()


def _error_fingerprint(msg: str, stage: "str | None" = None) -> str:
    """Return a short hash fingerprint of a normalized error message.

    When ``stage`` is provided, it is mixed into the hash so failures at
    different pipeline stages (test vs build vs merge) produce distinct
    fingerprints even when their error messages normalize identically.
    Callers that omit ``stage`` get the legacy message-only fingerprint.
    """
    normalized = _normalize_error_message(msg or "")
    prefix = f"{stage}|" if stage else ""
    return hashlib.sha256(f"{prefix}{normalized}".encode()).hexdigest()[:12]


def _task_error_fingerprint(task) -> "str | None":
    """Return the stage-aware fingerprint for ``task``, or None if no message."""
    msg = getattr(task, "latest_status_message", None) or ""
    if not msg:
        return None
    stage: str | None = None
    try:
        from app.failure_classification import classify_failure
        from app.failure_stage import _derive_failure_stage

        cls = (
            classify_failure(task, at_time=task.last_heartbeat or task.started_at) or ""
        )
        stage = _derive_failure_stage(cls, msg)
    except Exception:  # noqa: BLE001
        stage = None
    return _error_fingerprint(msg, stage)


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
        fp = _task_error_fingerprint(task)
        if fp and fp == target_fingerprint:
            count += 1
    return count


# Window for considering a direction "recently paused" when checking cross-direction
# fingerprint overlap. Matches the default auto-recovery cooldown — pauses older than
# this are assumed stale or already recovered and no longer evidence of a systemic
# issue.
_SHARED_FINGERPRINT_PAUSED_WINDOW_HOURS = 48


def find_directions_sharing_fingerprint(
    project,
    target_fingerprint: str,
    exclude_direction_id: str | None = None,
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
    for d in getattr(project, "directions", None) or []:
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
        matches.append(
            {
                "id": d.get("id"),
                "text": (d.get("text") or "")[:100],
                "paused_at": paused_at_raw,
                "auto_pause_count": int(d.get("auto_pause_count", 0) or 0),
            }
        )
    return matches


def _is_recurring_failure_class(proj_anomalies: dict, failure_class: str) -> bool:
    """Return True if the given failure class has a persistent anomaly with occurrence_count >= 3."""
    entry = proj_anomalies.get(failure_class)
    return bool(isinstance(entry, dict) and entry.get("occurrence_count", 0) >= 3)


def _expire_stale_project_anomalies(
    proj_anomalies: dict,
    *,
    seen_keys: set,
    stale_cutoff: str,
    initial_last_seen: dict[str, str],
) -> None:
    """Delete stale anomalies without clobbering concurrent updates.

    ``_compute_research_anomalies`` mutates the shared anomaly dict in memory.
    A parallel compute can refresh an entry after this run took its initial
    snapshot; deleting solely because the key was not seen by this run would
    reset that concurrent counter. Re-check that ``last_seen`` is unchanged
    before deleting.
    """
    for key in list(proj_anomalies):
        if key in seen_keys:
            continue
        entry = proj_anomalies.get(key) or {}
        if not isinstance(entry, dict):
            continue
        current_last_seen = _as_iso(entry.get("last_seen"))
        if current_last_seen != initial_last_seen.get(key, current_last_seen):
            continue
        if current_last_seen < stale_cutoff:
            del proj_anomalies[key]


async def _compute_research_anomalies(project_id, session: AsyncSession) -> list:
    """Analyze recent task data to identify pipeline anomalies for undirected research focus.

    Returns a list of {signal: str, severity: 'high'|'medium', details: str} dicts.
    """
    # Deferred import to avoid circular dependency (main.py imports this module)
    from app.failure_classification import classify_failure
    from app.direction_outcomes import (
        _is_cascade_cancelled,
        verified_merged_count,
    )

    now = _utcnow()
    window_48h = now - timedelta(hours=48)
    prior_window_start = now - timedelta(hours=96)
    momentum_window_start = now - timedelta(hours=72)

    # Query last 50 terminal tasks for this project
    terminal_tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                )
                .order_by(Task.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )

    # All tasks (any status) in the 72h momentum window — used as the denominator
    # for failure-rate momentum detection. Separate from terminal_tasks because
    # rate-based momentum needs total volume, not just failure count.
    all_tasks_72h = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.created_at >= momentum_window_start,
                )
            )
        )
        .scalars()
        .all()
    )

    anomalies = []

    # --- Failure class analysis ---
    # Exclude benign orchestrator-cascade terminations (requeue replacements,
    # dependency/parent cancellations, and pipeline supersessions) so that
    # routine lifecycle bookkeeping never produces false spike or fingerprint signals.
    recent_failures = [
        t
        for t in terminal_tasks
        if t.created_at and t.created_at >= window_48h and not _is_cascade_cancelled(t)
    ]
    prior_failures = [
        t
        for t in terminal_tasks
        if t.created_at
        and prior_window_start <= t.created_at < window_48h
        and not _is_cascade_cancelled(t)
    ]

    # Group by failure class
    recent_by_class: dict = {}
    for t in recent_failures:
        cls = classify_failure(t, at_time=t.last_heartbeat or t.started_at)
        recent_by_class.setdefault(cls, []).append(t)

    prior_by_class: dict = {}
    for t in prior_failures:
        cls = classify_failure(t, at_time=t.last_heartbeat or t.started_at)
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
                    pattern_counts[fp] = {
                        "fingerprint": fp,
                        "count": 0,
                        "example_message": msg,
                    }
                pattern_counts[fp]["count"] += 1
            # Sort descending by count, take top 3
            top_patterns = sorted(
                pattern_counts.values(), key=lambda x: x["count"], reverse=True
            )[:3]

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
            pattern_summary = (
                "; ".join(
                    f'({p["count"]}x) "{p["example_message"][:80]}"'
                    for p in top_patterns
                )
                if top_patterns
                else ""
            )
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

    # --- Failure rate momentum analysis ---
    # Catches gradual escalation that the surge check (count doubled across
    # 48h windows) misses: a steadily rising failure rate across three
    # consecutive 24h windows. Complementary, not a replacement.
    momentum_buckets = [0, 0, 0]  # [72-48h, 48-24h, 24-0h] failures
    total_buckets = [0, 0, 0]  # same windows, all-status totals
    momentum_dirs: set[str] = set()
    from app.time_utils import _to_naive_utc

    for t in terminal_tasks:
        if _is_cascade_cancelled(t):
            continue
        # Task.created_at TIMESTAMPTZ since 0061; momentum_window_start + now are naive.
        t_created = _to_naive_utc(t.created_at)
        if not t_created or t_created < momentum_window_start:
            continue
        hours_ago = (now - t_created).total_seconds() / 3600
        if hours_ago < 24:
            momentum_buckets[2] += 1
        elif hours_ago < 48:
            momentum_buckets[1] += 1
        else:
            momentum_buckets[0] += 1
        did = str((t.context or {}).get("direction_id") or "").strip()
        if did:
            momentum_dirs.add(did)
    for t in all_tasks_72h:
        t_created = _to_naive_utc(t.created_at)
        if not t_created:
            continue
        hours_ago = (now - t_created).total_seconds() / 3600
        if hours_ago < 24:
            total_buckets[2] += 1
        elif hours_ago < 48:
            total_buckets[1] += 1
        else:
            total_buckets[0] += 1
    if all(total_buckets[i] >= 3 for i in range(3)):
        rates = [momentum_buckets[i] / total_buckets[i] for i in range(3)]
        if rates[0] < rates[1] < rates[2] and rates[2] >= 0.20:
            anomalies.append(
                {
                    "signal": (
                        f"FailureRateMomentum: {rates[0]:.0%} → {rates[1]:.0%} → "
                        f"{rates[2]:.0%} across last 3x24h windows"
                    ),
                    "severity": "medium",
                    "details": (
                        f"Failure rate has risen monotonically across three "
                        f"consecutive 24h windows (72-48h: {rates[0]:.0%} "
                        f"[{momentum_buckets[0]}/{total_buckets[0]}], 48-24h: "
                        f"{rates[1]:.0%} [{momentum_buckets[1]}/{total_buckets[1]}], "
                        f"24-0h: {rates[2]:.0%} [{momentum_buckets[2]}/{total_buckets[2]}]). "
                        "Gradual acceleration suggests an emerging systemic issue "
                        "before the surge threshold trips."
                    ),
                    "rate_progression": [f"{r:.0%}" for r in rates],
                    "affected_directions": sorted(momentum_dirs),
                    "suggested_action": (
                        "Investigate the upward trend before it trips the surge "
                        "threshold — check recent deploys, infra health, and "
                        "direction-level failure patterns."
                    ),
                }
            )

    # --- Cross-direction systemic pattern analysis ---
    # When one normalized error fingerprint recurs across multiple directions in
    # the 48h window, a shared root cause (infra, framework, shared dep) is more
    # likely than direction-specific issues. Emit a distinct signal so operators
    # scope investigations systemically rather than treating each paused
    # direction as independent. Complements the per-class top_patterns view,
    # which doesn't distinguish "all in one direction" from "spread across many".
    fp_directions: dict[str, set[str]] = {}
    fp_counts: dict[str, int] = {}
    fp_example: dict[str, str] = {}
    for t in recent_failures:
        msg = t.latest_status_message or ""
        if not msg:
            continue
        did = str((t.context or {}).get("direction_id") or "").strip()
        if not did:
            continue
        fp = _error_fingerprint(msg)
        fp_directions.setdefault(fp, set()).add(did)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
        fp_example.setdefault(fp, msg)
    # Track (fingerprint, direction) counts so intra-direction recurrences can
    # be detected below without rescanning tasks.
    intra_fp_counts: dict[tuple[str, str], int] = {}
    intra_fp_example: dict[tuple[str, str], str] = {}
    for t in recent_failures:
        msg = t.latest_status_message or ""
        if not msg:
            continue
        did = str((t.context or {}).get("direction_id") or "").strip()
        if not did:
            continue
        fp = _error_fingerprint(msg)
        key = (fp, did)
        intra_fp_counts[key] = intra_fp_counts.get(key, 0) + 1
        intra_fp_example.setdefault(key, msg)
    for fp, dirs in fp_directions.items():
        if len(dirs) < 2 or fp_counts[fp] < 3:
            continue
        dir_list = sorted(dirs)
        anomalies.append(
            {
                "signal": (
                    f"CrossDirectionFingerprint_{fp} spans {len(dirs)} directions "
                    f"with {fp_counts[fp]} recent failures"
                ),
                "severity": "high",
                "details": (
                    f"Fingerprint {fp} ({fp_counts[fp]} recent failures) affects "
                    f"{len(dirs)} directions: {', '.join(dir_list)[:200]}. "
                    f'Example message: "{fp_example[fp][:120]}". '
                    "Suggests a systemic root cause (shared infra/framework/dep) "
                    "rather than direction-specific scope or code issues."
                ),
                "fingerprint": fp,
                "affected_directions": dir_list,
            }
        )

    # --- Intra-direction recurring fingerprint analysis ---
    # Same normalized fingerprint recurring 3+ times *within one direction* in
    # the 48h window signals a persistent direction-scoped problem (stale
    # finding, brittle test, unimplementable task). Emit a distinct signal so
    # diagnosis fires before auto-pause does. Skip fingerprints the cross-
    # direction branch already covered — that path spawns its own investigation.
    for (fp, did), count in intra_fp_counts.items():
        if count < 3:
            continue
        if len(fp_directions.get(fp, set())) >= 2 and fp_counts.get(fp, 0) >= 3:
            continue
        anomalies.append(
            {
                "signal": (
                    f"IntraDirectionFingerprint_{fp}_{did} "
                    f"{count} recent failures in one direction"
                ),
                "severity": "high",
                "details": (
                    f"Fingerprint {fp} recurred {count} times in direction {did} "
                    f"within the last 48h. "
                    f'Example message: "{intra_fp_example[(fp, did)][:120]}". '
                    "Investigate the root cause before the direction auto-pauses."
                ),
                "fingerprint": fp,
                "direction_id": did,
                "failure_count": count,
            }
        )

    # --- Direction merge rate and failure rate analysis ---
    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project:
        for direction in project.directions or []:
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
                    merged = verified_merged_count(entry)
                    rates.append(merged / created if created > 0 else 0.0)
                if rates[0] > rates[1] > rates[2] and rates[0] > 0.1:
                    anomalies.append(
                        {
                            "signal": f"Direction '{d_id}' merge rate declining: {rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}",
                            # Distinct persistence key so a direction tripping both
                            # the merge-decline and the fail-rate signal keeps both
                            # entries instead of one silently overwriting the other.
                            "anomaly_key": f"Direction_{d_id}_merge_decline",
                            "severity": "medium",
                            "details": (
                                f'Direction "{d_text}" (id: {d_id}) has seen a consistent decline in '
                                f"merge rate over the last 3 cycles ({rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}). "
                                "May indicate implementation barriers or stale findings."
                            ),
                        }
                    )

            # High non-infra failure rate in last 2 cycles (>50%)
            if len(history) >= 2:
                last2 = history[-2:]
                total_created = sum(e.get("tasks_created", 0) for e in last2)
                total_failed_raw = sum(e.get("tasks_failed", 0) for e in last2)
                total_failed_infra = sum(e.get("tasks_failed_infra", 0) for e in last2)
                total_failed = max(0, total_failed_raw - total_failed_infra)
                if total_created > 0 and total_failed / total_created > 0.5:
                    fail_pct = int(total_failed / total_created * 100)
                    anomalies.append(
                        {
                            "signal": f"Direction '{d_id}' has {fail_pct}% non-infra task failure rate in last 2 cycles",
                            # Distinct persistence key (see merge-decline signal above)
                            # so both direction signals survive when they co-fire.
                            "anomaly_key": f"Direction_{d_id}_fail_rate",
                            "severity": "high",
                            "details": (
                                f'Direction "{d_text}" (id: {d_id}) had {total_failed}/{total_created} tasks fail '
                                f"in the last 2 research cycles ({fail_pct}% non-infra failure rate). "
                                "The findings may be generating unimplementable tasks."
                            ),
                        }
                    )

    # --- Persist anomaly tracking ---
    proj_key = str(project_id)
    proj_anomalies = _shared_state.persistent_anomalies.setdefault(proj_key, {})
    initial_last_seen = {
        key: _as_iso((entry or {}).get("last_seen"))
        for key, entry in proj_anomalies.items()
        if isinstance(entry, dict)
    }
    now_iso = now.isoformat()
    seen_keys: set = set()
    for a in anomalies:
        # Build a stable key from the signal's failure class or direction id.
        signal = a["signal"]
        # Prefer an explicit anomaly_key the signal carries. Direction-level
        # signals set this so each signal *type* gets its own persistence key
        # (e.g. Direction_<id>_merge_decline vs Direction_<id>_fail_rate) —
        # otherwise a direction tripping both would have one entry silently
        # overwrite the other and lose an anomaly type.
        explicit_key = a.get("anomaly_key")
        if explicit_key:
            anomaly_key = explicit_key
        # Extract anomaly key: e.g. "CODE_ERROR" from "CODE_ERROR failures..."
        # or "Direction_d-abc123" from "Direction 'd-abc123' ..."
        elif signal.startswith("Direction '"):
            # Include direction ID to avoid key collisions across directions
            parts = signal.split("'")
            anomaly_key = (
                f"Direction_{parts[1]}" if len(parts) > 1 else signal.split()[0]
            )
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
        # Stash cross-direction fingerprint metadata so the proactive spawner
        # can build an investigation prompt without re-querying tasks.
        if anomaly_key == "FailureRateMomentum":
            entry = proj_anomalies[anomaly_key]
            if a.get("rate_progression"):
                entry["rate_progression"] = list(a["rate_progression"])
            if a.get("affected_directions") is not None:
                entry["affected_directions"] = list(a["affected_directions"])
            if a.get("suggested_action"):
                entry["suggested_action"] = a["suggested_action"]
        if anomaly_key.startswith("CrossDirectionFingerprint_"):
            entry = proj_anomalies[anomaly_key]
            if a.get("fingerprint"):
                entry["fingerprint"] = a["fingerprint"]
            if a.get("affected_directions"):
                entry["affected_directions"] = list(a["affected_directions"])
            fp = a.get("fingerprint")
            if fp and fp in fp_counts:
                entry["failure_count"] = fp_counts[fp]
                entry["example_message"] = fp_example.get(fp, "")[:500]
        if anomaly_key.startswith("IntraDirectionFingerprint_"):
            entry = proj_anomalies[anomaly_key]
            fp = a.get("fingerprint")
            did = a.get("direction_id")
            if fp:
                entry["fingerprint"] = fp
            if did:
                entry["direction_id"] = did
            if fp and did and (fp, did) in intra_fp_counts:
                entry["failure_count"] = intra_fp_counts[(fp, did)]
                entry["example_message"] = intra_fp_example.get((fp, did), "")[:500]
    # Expire anomalies not seen for 48h
    stale_cutoff = (now - timedelta(hours=48)).isoformat()
    _expire_stale_project_anomalies(
        proj_anomalies,
        seen_keys=seen_keys,
        stale_cutoff=stale_cutoff,
        initial_last_seen=initial_last_seen,
    )

    # Record when this signal was last freshly computed so staleness consumers
    # (pipeline-health summary, SR analytics, strategic spawning) can detect if
    # _compute_research_anomalies stops being called (e.g. aggregator loop dies).
    proj_anomalies["signal_last_computed_at"] = now_iso

    # Persist so occurrence counts survive pod restarts (writes to Project.design_doc;
    # caller owns the commit).
    try:
        await _shared_state.persist_anomaly_snapshot(proj_key, proj_anomalies, session)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ANOMALY_PERSIST] Failed to persist anomaly snapshot for %s",
            proj_key,
            exc_info=True,
        )

    # Record successful run for the silent-writer watchdog. Wrapped so a
    # tracking failure can never break detection.
    try:
        _shared_state.last_anomaly_detection_run_at = now
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ANOMALY_TRACKER] Failed to record last_anomaly_detection_run_at",
            exc_info=True,
        )

    return anomalies


# ---------------------------------------------------------------------------
# Anomaly-driven pipeline adjustments
# ---------------------------------------------------------------------------

# Failure classes that trigger automatic pipeline adjustments when surging.
# CODE_ERROR is included but uses a higher threshold and gentler reduction —
# code regressions tend to be direction-specific rather than infra-wide, so
# aggressive pipeline-level throttling is less appropriate than for infra
# failures.
_ACTIONABLE_FAILURE_CLASSES = frozenset(
    {"PROVISION_FAILURE", "EXTERNAL_API", "CODE_ERROR"}
)
_INFRA_FAILURE_CLASSES = frozenset({"PROVISION_FAILURE", "EXTERNAL_API"})

# Minimum occurrence_count in persistent_anomalies to trigger adjustments
_ANOMALY_ACTION_THRESHOLD = 3
# CODE_ERROR requires a larger surge than infra classes before throttling.
_CODE_ERROR_ACTION_THRESHOLD = 5
# Fraction of max_concurrent_vms retained after a CODE_ERROR-only throttle
# (25% reduction, vs the 50% reduction for infra-class triggers).
_CODE_ERROR_REDUCTION_RETAINED_FRACTION = 0.75


def _threshold_for_class(cls: str) -> int:
    """Occurrence_count required for ``cls`` to trigger pipeline throttling."""
    if cls == "CODE_ERROR":
        return _CODE_ERROR_ACTION_THRESHOLD
    return _ANOMALY_ACTION_THRESHOLD


def _compute_adjusted_max_vms(pre_max_vms: int, triggers) -> int:
    """Return the target max_concurrent_vms for the given trigger set.

    Aggressive 50% reduction when any infra class is among the triggers;
    gentler 25% reduction for CODE_ERROR-only triggers. Floor of 2 either way.
    """
    if set(triggers) & _INFRA_FAILURE_CLASSES:
        return max(2, pre_max_vms // 2)
    return max(2, int(pre_max_vms * _CODE_ERROR_REDUCTION_RETAINED_FRACTION))


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

    When an actionable failure class reaches high severity with occurrence_count
    at or above its class-specific threshold (3 for PROVISION_FAILURE/EXTERNAL_API,
    5 for CODE_ERROR), automatically:
    - Reduce max_concurrent_vms (50% for infra triggers, 25% for CODE_ERROR-only),
      floor of 2
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
                and info.get("occurrence_count", 0) >= _threshold_for_class(anomaly_key)
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
    is_reentry = forced_restored_at is not None and (
        _utcnow() - forced_restored_at
    ) < timedelta(minutes=_FORCED_RESTORATION_REENTRY_WINDOW_MINUTES)

    # Store pre-adjustment values for restoration
    adjustments["pre_adjustment_max_vms"] = rc["max_concurrent_vms"]
    adjustments["pre_adjustment_failure_gate"] = adjustments["failure_gate_threshold"]

    # Apply adjustments: reduce VMs (50% for infra triggers, 25% for CODE_ERROR-only),
    # floor 2; tighten failure gate.
    new_max_vms = _compute_adjusted_max_vms(rc["max_concurrent_vms"], triggered_keys)
    rc["max_concurrent_vms"] = new_max_vms
    adjustments["failure_gate_threshold"] = 0.6

    adjustments["active"] = True
    adjustments["adjusted_at"] = _utcnow().isoformat()
    adjustments["trigger_anomalies"] = sorted(set(triggered_keys))
    # Remember the target we set so restoration/safeguard paths can compare against
    # it even if trigger_anomalies drifts while the adjustment is active.
    adjustments["adjusted_max_vms"] = new_max_vms

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


def _parse_adjustments_ts(iso_value: str | None) -> datetime | None:
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
        expected_adjusted_vms = adjustments.get("adjusted_max_vms")
        if expected_adjusted_vms is None:
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
    adjustments["adjusted_max_vms"] = None

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
                and info.get("occurrence_count", 0) >= _threshold_for_class(anomaly_key)
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
        expected_adjusted_vms = adjustments.get("adjusted_max_vms")
        if expected_adjusted_vms is None:
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
    adjustments["adjusted_max_vms"] = None

    logger.info(
        f"[ANOMALY_ACTION] Restored pipeline settings: "
        f"max_vms={rc['max_concurrent_vms']}, "
        f"failure_gate={adjustments['failure_gate_threshold']}"
    )
    return True


def manual_restore_anomaly_adjustments() -> dict:
    """Operator-initiated early restore of the active anomaly throttle.

    Unlike _check_anomaly_restoration, this does NOT require the triggering
    anomalies to have cleared — the operator asserts the root cause is
    resolved and wants full capacity back now rather than waiting on the
    automatic safeguard. It still respects the same deploy_health edge cases
    the automatic path guards (capacity independently paused/ramped) rather
    than blindly overwriting the live max_concurrent_vms.

    Returns a dict describing the outcome (``restored`` is False when there is
    no active adjustment to clear).
    """
    adjustments = _shared_state.anomaly_adjustments
    rc = _shared_state.runtime_config

    if not adjustments["active"]:
        return {"restored": False, "reason": "no_active_adjustment"}

    pre_max_vms = adjustments["pre_adjustment_max_vms"]
    pre_gate = adjustments["pre_adjustment_failure_gate"]
    cleared_triggers = list(adjustments.get("trigger_anomalies") or [])

    if pre_max_vms is not None:
        current_max_vms = rc["max_concurrent_vms"]
        expected_adjusted_vms = adjustments.get("adjusted_max_vms")
        if expected_adjusted_vms is None:
            expected_adjusted_vms = max(2, pre_max_vms // 2)
        if current_max_vms < expected_adjusted_vms:
            # deploy_health has paused (=0) or is ramping (1-2) below our floor —
            # it now owns max_concurrent_vms. Leave rc alone and only clear the
            # stash; deploy_health's pre_pause_max_vms already captured our
            # pre_adjustment capacity, so its resume path ramps cleanly.
            logger.info(
                "[ANOMALY_ACTION] Manual restore: current_max_vms=%d below "
                "anomaly-adjusted floor (%d) — deploy_health owns capacity; "
                "clearing stash without overwriting live value",
                current_max_vms,
                expected_adjusted_vms,
            )
        elif current_max_vms >= pre_max_vms:
            # Already at or above pre-adjustment capacity; nothing to restore.
            pass
        else:
            # Restore the operator's stashed pre-adjustment capacity. This is an
            # explicit command, so we restore fully even if the live value
            # diverged from our adjusted floor (the divergence the automatic
            # path would treat as an admin override to preserve).
            rc["max_concurrent_vms"] = pre_max_vms

    if pre_gate is not None:
        adjustments["failure_gate_threshold"] = pre_gate

    adjustments["active"] = False
    adjustments["adjusted_at"] = None
    adjustments["trigger_anomalies"] = []
    adjustments["pre_adjustment_max_vms"] = None
    adjustments["pre_adjustment_failure_gate"] = None
    adjustments["adjusted_max_vms"] = None

    logger.info(
        "[ANOMALY_ACTION] Manual restore by operator: max_vms=%s, "
        "failure_gate=%s, cleared_triggers=%s",
        rc["max_concurrent_vms"],
        adjustments["failure_gate_threshold"],
        cleared_triggers,
    )
    return {
        "restored": True,
        "max_concurrent_vms": rc["max_concurrent_vms"],
        "failure_gate_threshold": adjustments["failure_gate_threshold"],
        "cleared_trigger_anomalies": cleared_triggers,
    }


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
        r for r in _shared_state.deploy_monitoring["task_results"] if r[0] >= cutoff
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
        from app.deploy_health import (
            _HEALTH_HISTORY_SIZE,
            get_recovery_history_snapshot,
        )

        history = get_recovery_history_snapshot()
        if len(history) < _HEALTH_HISTORY_SIZE or not all(history):  # noqa: SIM103
            return False
        return True

    # Tolerate a small number of triggering-class failures when they represent
    # a tiny fraction of recent results (one intermittent failure shouldn't pin
    # the throttle for hours). Genuine ongoing issues exceed either bound.
    triggering_failures = sum(
        1
        for r in recent_results
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
            1
            for e in _shared_state.deploy_monitoring["pipeline_events"]
            if e.get("type") == "provision_timeout_pre_claim"
            and e.get("ts")
            and e["ts"] >= window_cutoff_dt
        )
        if recent_provision_events > 0:
            return False

    return True


async def seed_task_results_from_db(
    session: AsyncSession, hours: int = _FAST_RECOVERY_WINDOW_HOURS
) -> int:
    """Backfill deploy_monitoring['task_results'] from recently completed tasks.

    The fast-recovery window relies on an in-memory deque that empties on pod
    restart, leaving the throttle pinned for ~2h until enough new completions
    accrue. This rebuilds the deque from tasks whose vm_stopped_at falls within
    the window so fast-recovery can fire on the first sweep after restart.
    """
    from app.failure_classification import classify_failure

    cutoff = _utcnow() - timedelta(hours=hours)
    stmt = (
        select(Task)
        .where(Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]))
        .where(Task.vm_stopped_at.is_not(None))
        .where(Task.vm_stopped_at >= cutoff)
        .order_by(Task.vm_stopped_at)
    )
    rows = (await session.execute(stmt)).scalars().all()

    deque_ref = _shared_state.deploy_monitoring["task_results"]
    seeded = 0
    for task in rows:
        success = task.status == TaskStatus.COMPLETED
        failure_class = (
            ""
            if success
            else classify_failure(task, at_time=task.last_heartbeat or task.started_at)
        )
        task_type = task.first_payload.get("type", "")
        ts_iso = task.vm_stopped_at.isoformat()
        deque_ref.append(
            (ts_iso, "success" if success else "failure", failure_class, task_type)
        )
        seeded += 1
    return seeded


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
            "failure_gate_threshold": _shared_state.anomaly_adjustments[
                "failure_gate_threshold"
            ],
            "forced_restoration_count": _shared_state.anomaly_adjustments[
                "forced_restoration_count"
            ],
        }

    was_active = _shared_state.anomaly_adjustments["active"]
    applied = _apply_anomaly_adjustments()
    if applied:
        return {
            "action": "adjusted",
            "is_new": not was_active,
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments[
                "failure_gate_threshold"
            ],
            "trigger_anomalies": _shared_state.anomaly_adjustments["trigger_anomalies"],
            "pre_adjustment_max_vms": _shared_state.anomaly_adjustments[
                "pre_adjustment_max_vms"
            ],
        }

    restored = _check_anomaly_restoration()
    if restored:
        return {
            "action": "restored",
            "max_concurrent_vms": _shared_state.runtime_config["max_concurrent_vms"],
            "failure_gate_threshold": _shared_state.anomaly_adjustments[
                "failure_gate_threshold"
            ],
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
            entry = proj_anomalies[key]
            if not isinstance(entry, dict):
                continue
            last_seen = _as_iso(entry.get("last_seen"))
            if last_seen and last_seen < stale_cutoff:
                del proj_anomalies[key]
                expired += 1
        if not proj_anomalies:
            del _shared_state.persistent_anomalies[proj_key]
    return expired


async def anomaly_maintenance_sweep() -> dict:
    """Periodic maintenance independent of autonomous research.

    Expires stale persistent anomalies (>48h since last_seen) and re-evaluates
    pipeline adjustments so that throttles get restored even when no research
    cycle is running. Returns a small summary dict for logging.
    """
    expired = _expire_stale_anomalies()
    action = await check_anomaly_actions()
    if expired or action["action"] != "none":
        logger.info(
            "[ANOMALY_SWEEP] expired=%d action=%s",
            expired,
            action["action"],
        )
    return {
        "expired": expired,
        "action": action["action"],
    }


async def _expire_stale_proposals(session) -> int:
    """Expire direction proposals older than 30 days across all projects.

    P2 (p2-direction-table): previously this sweep did one bulk
    ``SELECT ... FOR NO KEY UPDATE`` over every autonomous project at once,
    which made it the dominant lock-timeout victim whenever any single project
    row was held by the per-task hot path (incident 2026-05-29). It now reads
    project IDs WITHOUT locking, then expires each project's stale proposals
    one at a time under that single project's brief row lock via
    ``with_directions_locked`` (which dual-writes the normalised ``direction``
    table). A project whose row is currently held is skipped this run and its
    proposals simply expire on the next idle-research cycle (~5 min) — a
    stale-proposal cleanup tolerates the deferral, so the expiry is never
    silently dropped, just deferred. No project-wide bulk lock is taken.
    """
    from app.task_locking import with_directions_locked

    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    # Read candidate project IDs without any lock — a plain scalar select that
    # never contends with the hot path. We re-read each project's directions
    # fresh inside its own row lock below, so this snapshot can be stale.
    project_ids = (
        await session.execute(
            select(Project.id)
            .where(
                Project.autonomous_enabled.is_(True),
                Project.not_archived(),
            )
        )
    ).scalars().all()
    total_expired = 0
    for project_id in project_ids:
        # Per-project counter captured via closure; the mutate_fn runs against
        # the directions list freshly loaded inside the project's row lock.
        expired_holder = {"count": 0}

        def _expire(dirs: list, _holder=expired_holder) -> None:
            now_iso = _utcnow().isoformat()
            for d in dirs:
                if (
                    d.get("status") == "proposed"
                    and (d.get("proposed_at") or d.get("added_at", "")) < cutoff
                ):
                    d["status"] = "expired"
                    d["expired_at"] = now_iso
                    _holder["count"] += 1

        # skip_locked=True: a project row held by a concurrent same-project
        # completion is skipped this run (deferred, never dropped). Only this
        # one project's row is touched, so a busy project never blocks the
        # sweep for the others.
        applied = await with_directions_locked(
            session, project_id, _expire, skip_locked=True
        )
        if applied and expired_holder["count"]:
            await session.commit()
            total_expired += expired_holder["count"]
            logger.info(
                f"[DIRECTIONS] Expired {expired_holder['count']} stale proposals "
                f"for project {project_id}"
            )
        else:
            # Nothing changed (or row was held under skip_locked); release any
            # transaction state before moving to the next project so we don't
            # hold a connection/transaction open across the whole sweep.
            await session.rollback()
    return total_expired


@router.get("/anomaly-tracker/snapshot")
async def get_anomaly_tracker_snapshot(
    project_id: uuid.UUID | None = Query(default=None),
    freshness_hours: int = Query(default=24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Read-only snapshot of shared_state.persistent_anomalies.

    Surfaces which fingerprints the detection pipeline is currently tracking,
    how aged they are, and when the detection sweep last ran (so a silent
    writer failure is visible). Pure passthrough of stored entries — no
    normalization, no garbage collection.
    """
    pid_filter = str(project_id) if project_id else None
    now = _utcnow()
    stale_cutoff_iso = (now - timedelta(hours=freshness_hours)).isoformat()

    # ``last_dispatch_attempt_at`` was previously enriched from
    # RegressionRemediationCase rows written by the pipeline-regression
    # dispatcher. The dispatcher + its model have been removed, so no rows
    # exist; the field is kept (always None) to preserve the API contract.

    projects_payload: List[Dict[str, Any]] = []
    total = 0
    for proj_key, proj_anomalies in _shared_state.persistent_anomalies.items():
        if pid_filter and proj_key != pid_filter:
            continue
        if not proj_anomalies:
            continue
        entries: List[Dict[str, Any]] = []
        for anomaly_key, entry in proj_anomalies.items():
            if not isinstance(entry, dict):
                continue
            first_seen = entry.get("first_seen")
            last_seen = entry.get("last_seen")
            first_seen_iso = (
                first_seen.isoformat()
                if isinstance(first_seen, datetime)
                else (first_seen or None)
            )
            last_seen_iso = (
                last_seen.isoformat()
                if isinstance(last_seen, datetime)
                else (last_seen or None)
            )
            stuck = bool(last_seen_iso and last_seen_iso < stale_cutoff_iso)
            entries.append(
                {
                    "key": anomaly_key,
                    "first_seen": first_seen_iso,
                    "last_seen": last_seen_iso,
                    "occurrence_count": entry.get("occurrence_count", 0),
                    "severity": entry.get("severity"),
                    "latest_details": entry.get("latest_details"),
                    "fingerprint": entry.get("fingerprint"),
                    "affected_directions": entry.get("affected_directions"),
                    "last_dispatch_attempt_at": None,
                    "stuck": stuck,
                }
            )
        # Sort by last_seen desc; entries with missing last_seen sink to the bottom.
        entries.sort(key=lambda e: e.get("last_seen") or "", reverse=True)
        total += len(entries)
        projects_payload.append({"project_id": proj_key, "anomalies": entries})

    last_run = _shared_state.last_anomaly_detection_run_at
    return {
        "last_detection_run_at": last_run.isoformat() if last_run else None,
        "freshness_window_hours": freshness_hours,
        "total": total,
        "projects": projects_payload,
    }
