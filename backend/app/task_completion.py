"""Task completion handlers and their supporting functions.

Extracted from app.main to keep main.py focused on routing and orchestration.
These handlers are called from _process_post_commit() in main.py based on task type.

Functions that depend on items still in main.py (broadcast_to_project,
_spawn_strategic_review, _maybe_create_failure_alert, _create_strategic_alert,
classify_failure) use deferred imports inside their function bodies to avoid
the circular import that would arise if we imported them at module level
(since main.py imports this module).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

import app.shared_state as _shared_state
from app.database import AsyncSessionLocal
from app.github import create_branch, create_pr_comment, get_branch_sha, get_open_pull_requests
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    ProjectMessage,
    MessageType,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.routers.agent import get_github_token_for_task
from app.shared_state import runtime_config as _runtime_config
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _compute_first_attempt_succeeded(attempt_count: int, status: "TaskStatus") -> bool:
    """Return True if the task completed successfully on its first attempt (attempt_count <= 1)."""
    return attempt_count <= 1 and status == TaskStatus.COMPLETED


def _compute_momentum_factor(d: dict) -> float:
    """Return a momentum multiplier based on recent research history productivity."""
    history = d.get("research_history", [])
    if len(history) >= 2 and all(e.get("tasks_created", 0) > 0 for e in history[-2:]):
        return 2.0  # hot streak: last 2 cycles both productive
    if len(history) >= 3 and all(e.get("tasks_created", 0) == 0 for e in history[-3:]):
        return 0.5  # stalled: last 3 cycles all empty
    return 1.0  # neutral


# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}


def _update_cycle_history(project: Any, cycle_id: str, updates: Dict[str, Any]) -> None:
    """Find a cycle entry by cycle_id in project.vision and apply updates in-place."""
    vision = dict(project.vision or {})
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    for entry in cycle_history:
        if entry.get("cycle_id") == cycle_id:
            entry.update(updates)
            break
    vision["_strategic_cycle_history"] = cycle_history
    project.vision = vision
    flag_modified(project, "vision")


# ---------------------------------------------------------------------------
# Parser / Validator helpers
# ---------------------------------------------------------------------------

def _parse_plan_output(text: str) -> Optional[Dict[str, Any]]:
    """Extract structured plan JSON from agent output text."""
    try:
        # Find the start of the JSON block containing "subtasks"
        match = re.search(r'\{\s*"subtasks"\s*:\s*\[', text)
        if not match:
            return None
        start = match.start()
        # Walk forward to find the matching closing brace using bracket counting
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    return json.loads(candidate)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[PLAN] Failed to parse plan JSON: {e}", exc_info=True)
    return None


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


def _compute_saturation(history: list) -> bool:
    """Return True if last 3 cycles all produced 0 or 1 tasks."""
    if len(history) < 3:
        return False
    recent = history[-3:]
    return all(entry.get("tasks_created", 0) <= 1 for entry in recent)


def _parse_research_context(text: str) -> dict:
    """Parse a research_context string into its 3 sections and optional eviction summary."""
    result: dict = {"established": [], "failed": [], "open_questions": [], "eviction_summary": None}
    if not text:
        return result
    section_map = {
        "## Established Findings": "established",
        "## Failed Approaches": "failed",
        "## Open Questions": "open_questions",
    }
    current_key: Optional[str] = None
    in_eviction = False
    eviction_lines: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Eviction Summary":
            in_eviction = True
            current_key = None
        elif stripped in section_map:
            in_eviction = False
            current_key = section_map[stripped]
        elif in_eviction and stripped:
            eviction_lines.append(stripped)
        elif current_key and stripped.startswith("- "):
            result[current_key].append(stripped[2:])
    if eviction_lines:
        result["eviction_summary"] = "\n".join(eviction_lines)
    return result


def _parse_eviction_counts(summary_text: Optional[str]) -> dict:
    """Parse counts and failed titles from an eviction summary body."""
    result: dict = {"established": 0, "failed": 0, "open_questions": 0, "failed_titles": []}
    if not summary_text:
        return result
    for line in summary_text.splitlines():
        line = line.strip()
        # Check "Failed:" prefix before "evicted" keyword — titles may themselves contain "evicted"
        if line.startswith("Failed:"):
            titles_str = line[len("Failed:"):].strip()
            result["failed_titles"] = [t.strip() for t in titles_str.split(";") if t.strip()]
        elif "evicted" in line:
            m = re.search(r'(\d+) established', line)
            if m:
                result["established"] = int(m.group(1))
            m = re.search(r'(\d+) failed', line)
            if m:
                result["failed"] = int(m.group(1))
            m = re.search(r'(\d+) open question', line)
            if m:
                result["open_questions"] = int(m.group(1))
    return result


def _format_eviction_body(established: int, failed: int, open_questions: int, failed_titles: list) -> str:
    """Format the eviction summary body content (without the ## header line)."""
    count_parts = []
    if established > 0:
        count_parts.append(f"{established} established")
    if failed > 0:
        count_parts.append(f"{failed} failed")
    if open_questions > 0:
        plural = "s" if open_questions != 1 else ""
        count_parts.append(f"{open_questions} open question{plural}")
    lines = []
    if count_parts:
        lines.append(", ".join(count_parts) + " evicted")
    if failed_titles:
        lines.append("Failed: " + "; ".join(failed_titles))
    return "\n".join(lines)


def _build_research_context(
    prior_context: Optional[str],
    finding_titles: list,
    key_finding_descriptions: list,
    cycle_insights: list,
    history_entry: dict,
) -> str:
    """Build a compounding research_context document deterministically (no LLM)."""
    parsed = _parse_research_context(prior_context or "")

    # Parse prior eviction summary for accumulation
    prior_eviction = _parse_eviction_counts(parsed["eviction_summary"])

    # Append new findings to Established Findings as (proposed)
    for i, title in enumerate(finding_titles):
        desc = key_finding_descriptions[i] if i < len(key_finding_descriptions) else ""
        if desc:
            first_sentence = desc.split(".")[0].strip()
            entry_text = f"{title} (proposed). {first_sentence}" if first_sentence else f"{title} (proposed)"
        else:
            entry_text = f"{title} (proposed)"
        parsed["established"].append(entry_text)

    # Add cycle_insights to Open Questions (deduplicate)
    existing_oq = set(parsed["open_questions"])
    for insight in cycle_insights:
        if insight and insight not in existing_oq:
            parsed["open_questions"].append(insight)
            existing_oq.add(insight)

    def _format_doc(sections: dict, eviction_body: Optional[str] = None) -> str:
        parts: list = []
        if eviction_body:
            parts.append("## Eviction Summary")
            parts.append(eviction_body)
            parts.append("")
        for header, key in [
            ("## Established Findings", "established"),
            ("## Failed Approaches", "failed"),
            ("## Open Questions", "open_questions"),
        ]:
            parts.append(header)
            for item in sections[key]:
                parts.append(f"- {item}")
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    MAX_MAIN_CHARS = 7500
    EVICTION_MAX = 500
    TOTAL_MAX = MAX_MAIN_CHARS + EVICTION_MAX  # 8000

    doc = _format_doc(parsed)
    if len(doc) <= MAX_MAIN_CHARS:
        # No trimming needed; preserve any existing eviction summary
        if prior_eviction["established"] or prior_eviction["failed"] or prior_eviction["open_questions"]:
            eviction_body = _format_eviction_body(
                prior_eviction["established"],
                prior_eviction["failed"],
                prior_eviction["open_questions"],
                prior_eviction["failed_titles"],
            )
            return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]
        return doc

    # Snapshot sections before trimming
    before = {k: list(v) for k, v in parsed.items() if k in ("established", "failed", "open_questions")}

    # Trim oldest entries (from top of each section) to stay under main limit
    while len(doc) > MAX_MAIN_CHARS:
        trimmed = False
        for key in sorted(
            ["established", "failed", "open_questions"],
            key=lambda k: -len(parsed[k]),
        ):
            if len(parsed[key]) > 1:
                parsed[key] = parsed[key][1:]
                trimmed = True
                break
        if not trimmed:
            break
        doc = _format_doc(parsed)

    # Compute newly evicted entries
    new_established = len(before["established"]) - len(parsed["established"])
    new_failed = len(before["failed"]) - len(parsed["failed"])
    new_oq = len(before["open_questions"]) - len(parsed["open_questions"])

    evicted_failed_titles = [e[:60] for e in before["failed"][:new_failed]] if new_failed > 0 else []

    # Merge with prior eviction summary
    total_established = prior_eviction["established"] + new_established
    total_failed = prior_eviction["failed"] + new_failed
    total_oq = prior_eviction["open_questions"] + new_oq
    all_failed_titles = prior_eviction["failed_titles"] + evicted_failed_titles

    eviction_body = _format_eviction_body(total_established, total_failed, total_oq, all_failed_titles)
    return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]


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


async def _compute_research_anomalies(project_id, session: AsyncSession) -> list:
    """Analyze recent task data to identify pipeline anomalies for undirected research focus.

    Returns a list of {signal: str, severity: 'high'|'medium', details: str} dicts.
    """
    # Deferred import to avoid circular dependency (main.py imports this module)
    from app.main import classify_failure  # noqa: PLC0415

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


def _check_direction_health(direction: dict) -> Optional[str]:
    """Return a pause reason if this direction should be auto-paused, else None."""
    history = direction.get("research_history") or []

    # Rule 1: Persistent saturation — possibly_saturated AND last 5+ cycles all produced ≤1 task
    if direction.get("possibly_saturated") and len(history) >= 5:
        recent5 = history[-5:]
        if all(entry.get("tasks_created", 0) <= 1 for entry in recent5):
            return "Saturated: last 5 research cycles produced no actionable findings"

    # Rule 2: Zero merge rate — 5+ cycles with 0 PRs merged total, and the most
    # recent cycle has no tasks still in-flight (tasks_created == tasks_completed + tasks_failed)
    if len(history) >= 5:
        total_merged = sum(entry.get("prs_merged", 0) for entry in history)
        if total_merged == 0:
            last = history[-1]
            created = last.get("tasks_created", 0)
            finished = last.get("tasks_completed", 0) + last.get("tasks_failed", 0)
            if created == 0 or finished >= created:
                total_created = sum(entry.get("tasks_created", 0) for entry in history)
                return f"Unproductive: 0 PRs merged from {len(history)} research cycles ({total_created} tasks created)"

    # Rule 3: High failure rate — last 3 cycles have >70% tasks failed
    if len(history) >= 3:
        recent3 = history[-3:]
        total_created_3 = sum(entry.get("tasks_created", 0) for entry in recent3)
        total_failed_3 = sum(entry.get("tasks_failed", 0) for entry in recent3)
        if total_created_3 > 0 and total_failed_3 / total_created_3 > 0.7:
            return "High failure rate: >70% of spawned tasks failed in last 3 cycles"

    return None


def _check_direction_completion(direction: dict) -> Optional[str]:
    """Return a completion suggestion string if this direction appears successfully complete, else None."""
    history = direction.get("research_history") or []
    if len(history) < 4:
        return None
    total_merged = sum(entry.get("prs_merged", 0) for entry in history)
    if total_merged < 3:
        return None
    recent2 = history[-2:]
    recent_created = sum(e.get("tasks_created", 0) for e in recent2)
    recent_merged = sum(e.get("prs_merged", 0) for e in recent2)
    if recent_created <= 2 and recent_merged <= 1:
        return (
            f"Direction appears complete: {total_merged} PRs merged across "
            f"{len(history)} cycles, last 2 cycles low-yield"
        )
    return None


def _is_direction_duplicate(proposal_text: str, existing_directions: list, rejected_proposals: list) -> bool:
    """Return True if proposal_text overlaps with an existing or recently rejected direction."""
    STOPWORDS = {'the', 'a', 'an', 'to', 'for', 'and', 'or', 'in', 'of', 'is', 'it',
                 'this', 'that', 'with', 'by', 'we', 'should', 'be', 'are', 'on', 'as',
                 'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
    words = set(proposal_text.lower().split()) - STOPWORDS
    if len(words) < 2:
        return False
    for d in existing_directions:
        existing_text = (d.get("text") or "").lower()
        existing_words = set(existing_text.split()) - STOPWORDS
        if len(words & existing_words) >= 4:
            return True
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    for entry in rejected_proposals:
        if entry.get("rejected_at", "") < cutoff:
            continue
        rejected_words = set((entry.get("text") or "").lower().split()) - STOPWORDS
        if len(words & rejected_words) >= 4:
            return True
    return False


def _parse_research_findings(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract structured research findings JSON array from agent output text."""
    json_str = _extract_json_array(text, "title", "prompt")
    if json_str:
        try:
            findings = json.loads(json_str)
            if isinstance(findings, list):
                return findings
        except json.JSONDecodeError:
            # JSON may be truncated — try to recover complete objects
            # Find last complete top-level object using depth tracking
            depth = 0
            last_close = -1
            in_string = False
            escape_next = False
            for i, c in enumerate(json_str):
                if escape_next:
                    escape_next = False
                    continue
                if c == '\\' and in_string:
                    escape_next = True
                    continue
                if c == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        last_close = i
            if last_close > 0:
                try:
                    truncated = json_str[:last_close + 1] + ']'
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        logger.info(f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON")
                        return findings
                except json.JSONDecodeError:
                    pass
    return None


def _parse_research_insights(task_id: str) -> List[Dict[str, Any]]:
    """Read structured research insights from the agent's /tmp output file."""
    insights_path = f"/tmp/research_insights_{task_id}.json"
    try:
        if not os.path.exists(insights_path):
            return []
        with open(insights_path, "r") as f:
            raw = f.read(200_000)
        data = json.loads(raw)
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict) and "summary" in i]
    except Exception as e:
        logger.warning(f"[RESEARCH] Failed to read insights file {insights_path}: {e}")
    return []


def _validate_plan_dependencies(subtasks: List[Dict]) -> bool:
    """Validate that plan dependencies are acyclic (topological sort check)."""
    n = len(subtasks)
    visited = set()
    in_stack = set()

    def has_cycle(node: int) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in subtasks[node].get("depends_on_indices", []):
            if 0 <= dep < n and has_cycle(dep):
                return True
        in_stack.discard(node)
        return False

    return not any(has_cycle(i) for i in range(n) if i not in visited)


def _log_file_overlaps(subtasks: List[Dict], label: str = "PLAN") -> List[tuple]:
    """Log warnings when subtasks share related_files (for observability).

    Returns a list of (i, j, overlapping_files) tuples for each overlapping pair.
    """
    overlaps = []
    for i, a in enumerate(subtasks):
        a_files = set(a.get("related_files", []))
        if not a_files:
            continue
        for j, b in enumerate(subtasks):
            if j <= i:
                continue
            b_files = set(b.get("related_files", []))
            overlap = a_files & b_files
            if overlap:
                a_title = a.get("title", f"#{i}")
                b_title = b.get("title", f"#{j}")
                logger.info(f"[{label}] File overlap: '{a_title}' and '{b_title}' share {', '.join(sorted(overlap))}")
                overlaps.append((i, j, overlap))
    return overlaps


def _inject_file_overlap_deps(subtasks: List[Dict], label: str = "PLAN") -> None:
    """Add depends_on_indices entries to enforce serial execution for subtasks sharing files.

    For each file touched by more than one subtask, builds a chain in index order so that
    subtask[i+1] depends on subtask[i].  Auto-generated deps are merged (union, no duplicates)
    with any explicit depends_on_indices already present in the plan.
    """
    # Build file → [subtask indices] mapping
    file_to_indices: Dict[str, List[int]] = {}
    for idx, subtask in enumerate(subtasks):
        for f in subtask.get("related_files", []):
            file_to_indices.setdefault(f, []).append(idx)

    # For each file group with >1 subtask, inject chain deps
    for file_path, indices in file_to_indices.items():
        if len(indices) < 2:
            continue
        # indices are already in insertion order (which matches plan order)
        for pos in range(len(indices) - 1):
            predecessor = indices[pos]
            successor = indices[pos + 1]
            existing = subtasks[successor].setdefault("depends_on_indices", [])
            if predecessor not in existing:
                existing.append(predecessor)
                succ_title = subtasks[successor].get("title", f"#{successor}")
                pred_title = subtasks[predecessor].get("title", f"#{predecessor}")
                logger.debug(f"[{label}] Serializing subtasks {pred_title!r} → {succ_title!r} due to shared file: {file_path}")


async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str,
    label: str, session: AsyncSession
) -> bool:
    """Create a branch, or verify it already exists.

    Returns True on success, False on failure (task set to FAILED).
    """
    # Deferred import to avoid circular dependency
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    gh_token = owner = repo = None
    last_err = None
    for _attempt in range(3):
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
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
                except Exception:
                    pass
            if _attempt < 2:
                logger.warning(f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {5 * (2 ** _attempt)}s: {e!r}")
                await asyncio.sleep(5 * (2 ** _attempt))
            else:
                if gh_token:
                    logger.error(f"[{label}] Branch {branch_name} creation failed after 3 attempts: {last_err!r}")
                else:
                    logger.error(f"[{label}] Cannot create branch — GitHub token unavailable: {last_err!r}")
    e = last_err
    task.status = TaskStatus.FAILED
    task.latest_status_message = f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    session.add(task)
    await _maybe_create_failure_alert(task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200])
    await session.commit()
    return False


# ---------------------------------------------------------------------------
# Completion handlers
# ---------------------------------------------------------------------------

async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    # Deferred import to avoid circular dependency
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
        session.add(task)
        await _maybe_create_failure_alert(task, session, "Plan agent produced no valid subtasks")
        await session.commit()
        return

    subtasks = plan_data["subtasks"]

    # Cap subtask count to prevent runaway VM provisioning
    if len(subtasks) > 10:
        subtasks = subtasks[:10]
        logger.info(f"[PLAN] Capped subtask count from {len(plan_data['subtasks'])} to 10")

    if not _validate_plan_dependencies(subtasks):
        task.latest_status_message = "Plan has circular dependencies"
        task.status = TaskStatus.FAILED
        session.add(task)
        await _maybe_create_failure_alert(task, session, "Plan has circular dependencies")
        await session.commit()
        return

    # Store parsed plan and set parent to EVALUATING (children executing)
    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = (task.first_payload).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    plan_branch = f"plan/{short_id}"

    # Create staging branch for this plan's subtasks
    if not await _ensure_branch(task, plan_branch, parent_base, "PLAN", session):
        return

    # Store branch info on the plan task for rollup later
    task.context = {**(task.context or {}), "plan_branch": plan_branch, "parent_base": parent_base}
    session.add(task)

    # Check if agent already spawned children via /api/agent/spawn
    existing_children_result = await session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    all_children = existing_children_result.scalars().all()

    # Use spawned_task_ids from plan_data (embedded by agent) to preserve original spawn order.
    # Relying on created_at is non-deterministic: tasks spawned in a single batch share the same
    # timestamp and PostgreSQL returns ties in heap order, which may not match subtasks order.
    spawned_task_ids = plan_data.get("spawned_task_ids", [])
    if spawned_task_ids and all_children:
        children_by_id = {str(c.id): c for c in all_children}
        existing_children = [children_by_id[sid] for sid in spawned_task_ids if sid in children_by_id]
    else:
        existing_children = list(all_children)

    if existing_children:
        # Agent already spawned subtasks — enrich with branch info, dependencies, and approval
        logger.info(f"[PLAN] Agent already spawned {len(existing_children)} children, enriching...")
        parent_payload = task.first_payload
        model = parent_payload.get('model')
        effort = parent_payload.get('effort')
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        parent_direction_id = (task.context or {}).get('direction_id')
        parent_research_task_id = (task.context or {}).get('research_task_id')

        for i, child in enumerate(existing_children):
            child_payload = child.first_payload
            child_risk = child_payload.get('risk', 'low')
            related_files = child_payload.get('related_files', [])

            # Enrich prompt with related files if not already present
            prompt_text = child_payload.get('prompt', '')
            if related_files and 'Key files to review or modify' not in prompt_text:
                child_payload['prompt'] = prompt_text + "\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)

            # Update base_branch to plan branch and inherit model/effort
            child_payload['base_branch'] = plan_branch
            if model and not child_payload.get('model'):
                child_payload['model'] = model
            if effort and not child_payload.get('effort'):
                child_payload['effort'] = effort
            if create_suffix and not child_payload.get('prompt_suffix'):
                child_payload['prompt_suffix'] = create_suffix

            # Normalize type
            _allowed_types = {'create', 'refactor', 'migrate', 'qa'}
            raw_type = child_payload.get('type', 'create')
            child_payload['type'] = raw_type if raw_type in _allowed_types else 'create'

            child.payloads = [child_payload]
            child.requires_approval = False  # high-risk children execute freely; approval gates the final rollup merge
            child_approval_reason = child_payload.get('approval_reason') if child_risk == 'high' else None
            existing_ctx = child.context or {}
            child.context = {
                **existing_ctx,
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_payload.get('scope', 'small'),
                "risk": child_risk,
                "related_files": related_files,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
                # Inherit direction_id from plan parent if present and not overridden by finding
                **({"direction_id": parent_direction_id} if parent_direction_id and not existing_ctx.get('direction_id') else {}),
                **({"research_task_id": parent_research_task_id} if parent_research_task_id and not existing_ctx.get('research_task_id') else {}),
            }
            session.add(child)

        # Resolve dependencies in the same pass — do NOT commit between enrichment and
        # dependency wiring.  A mid-enrichment commit would let workers pick up children
        # with the correct base_branch but missing depends_on, causing out-of-order execution.
        for i, subtask_def in enumerate(subtasks):
            if i < len(existing_children):
                dep_indices = subtask_def.get("depends_on_indices", [])
                existing_children[i].depends_on = [
                    str(existing_children[j].id)
                    for j in dep_indices
                    if 0 <= j < len(existing_children)
                ]
                session.add(existing_children[i])

        overlaps = _log_file_overlaps(subtasks, "PLAN")
        for (i, j, shared_files) in overlaps:
            for idx in (i, j):
                if idx < len(existing_children):
                    ctx = dict(existing_children[idx].context or {})
                    warnings = ctx.get('file_overlap_warnings', [])
                    other_title = subtasks[j if idx == i else i].get('title', f'#{j if idx == i else i}')
                    warnings.append({'shared_files': sorted(shared_files), 'with_task': other_title})
                    ctx['file_overlap_warnings'] = warnings
                    existing_children[idx].context = ctx
                    session.add(existing_children[idx])
        # Single atomic commit: base_branch, requires_approval, context, and depends_on
        # are all written together so no worker can observe a partially-enriched child.
        await session.commit()

        for ct in existing_children:
            await broadcast_to_project(str(task.project_id), "task_updated",
                                       {"task_id": str(ct.id), "status": str(ct.status)})

        logger.info(f"[PLAN] Enriched {len(existing_children)} agent-spawned subtasks for plan {task.id}")
        return

    # First pass: create all child tasks
    parent_payload = task.first_payload
    model = parent_payload.get('model')
    effort = parent_payload.get('effort')
    plan_parent_context = {
        "parent_task_id": str(task.id),
        "parent_title": task.title,
        "parent_prompt": (parent_payload.get("prompt") or parent_payload.get("description") or "")[:500]
    }
    parent_direction_id = (task.context or {}).get('direction_id')
    parent_research_task_id = (task.context or {}).get('research_task_id')
    child_tasks = []
    for subtask_def in subtasks:
        related_files = subtask_def.get("related_files", [])
        prompt_text = subtask_def.get("prompt", "")
        full_prompt = prompt_text + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt_text
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        subtask_priority = TASK_PRIORITY_MAP.get(subtask_def.get("priority", "medium"), 50)
        subtask_model = subtask_def.get('model') or model
        subtask_effort = subtask_def.get('effort') or effort
        child_scope = subtask_def.get('scope', 'small')
        child_risk = subtask_def.get('risk', 'low')
        child_approval_reason = subtask_def.get('approval_reason') if child_risk == 'high' else None
        _allowed_types = {'create', 'refactor', 'migrate', 'qa'}
        raw_type = subtask_def.get('type', 'create')
        subtask_type = raw_type if raw_type in _allowed_types else 'create'
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            payloads=[{
                "type": subtask_type,
                "prompt": full_prompt,
                "base_branch": plan_branch,
                **({"model": subtask_model} if subtask_model else {}),
                **({"effort": subtask_effort} if subtask_effort else {}),
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_scope,
                "risk": child_risk,
                "related_files": related_files,
                "parent_context": plan_parent_context,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
                # Inherit direction_id from plan parent if present and not overridden by finding
                **({"direction_id": parent_direction_id} if parent_direction_id else {}),
                **({"research_task_id": parent_research_task_id} if parent_research_task_id else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
            priority=subtask_priority,
        )
        session.add(child)
        child_tasks.append(child)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Inject serial dependencies for subtasks that share files (additive only)
    # Save original (explicit) deps so we can restore them if injection creates a cycle
    original_deps = {i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)}
    _inject_file_overlap_deps(subtasks, "PLAN")

    # Re-validate after file overlap injection — injection can create cycles
    # (e.g., A explicitly depends on B, but file overlap adds B → A)
    if not _validate_plan_dependencies(subtasks):
        logger.error("[PLAN] Circular dependency after file overlap injection — reverting to explicit deps only")
        for i, st in enumerate(subtasks):
            if original_deps.get(i):
                st["depends_on_indices"] = original_deps[i]
            else:
                st.pop("depends_on_indices", None)

    # Second pass: resolve depends_on_indices to actual UUIDs
    for i, subtask_def in enumerate(subtasks):
        dep_indices = subtask_def.get("depends_on_indices", [])
        child_tasks[i].depends_on = [
            str(child_tasks[j].id)
            for j in dep_indices
            if 0 <= j < len(child_tasks)
        ]
        session.add(child_tasks[i])

    # Log file overlap warnings for observability
    _log_file_overlaps(subtasks, "PLAN")

    await session.commit()

    # Record history
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PLAN_DECOMPOSITION", "parent_plan_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()
    logger.info(f"[PLAN] Created {len(child_tasks)} subtasks from plan {task.id}")


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
            payloads=[{
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }],
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
            payloads=[{
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
            }],
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


async def _handle_investigation_completion(task: Task, payload, session: AsyncSession):
    """Store investigation report logs in plan_output and mark task completed."""
    if payload.logs:
        try:
            task.plan_output = json.loads(payload.logs)
        except (json.JSONDecodeError, Exception):
            task.plan_output = {"raw": payload.logs}
    task.status = TaskStatus.COMPLETED
    session.add(task)


async def _handle_design_doc_completion(task: Task, logs: str, session: AsyncSession):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.
    """
    # Deferred imports to avoid circular dependency
    from app.main import _spawn_strategic_review, _create_strategic_alert  # noqa: PLC0415

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
    try:
        await session.refresh(project)
        await _spawn_strategic_review(project, session, cycle_id=cycle_id)
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
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
            _update_cycle_history(project, cycle_id, {
                "phase1_completed_at": _utcnow().isoformat(),
                "outcome": "failed_phase2_spawn",
                "error": str(chain_exc)[:200],
            })
        flag_modified(project, 'vision')
        session.add(project)
    session.add(task)
    await session.commit()


async def _handle_strategic_completion(task: Task, logs: str, session: AsyncSession):
    """Handle strategic review completion.

    Vision and direction updates were already applied incrementally via POST /api/agent/strategic
    during agent execution. This handler just records final status and checks for issues.
    Falls back to log parsing for backward compatibility if the agent used the old approach.
    """
    # Deferred imports to avoid circular dependency
    from app.main import _create_strategic_alert  # noqa: PLC0415

    project = await session.get(Project, task.project_id)
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    vision_rev = (project.vision or {}).get("revision", 0)
    active_dirs = sum(1 for d in (project.directions or []) if d.get("status") == "active")

    # Check if the agent used the new incremental API during execution
    task_payload = task.first_payload
    spawned_vision_rev = (task_payload.get("vision") or {}).get("revision", 0)
    api_was_used = (task.context or {}).get("_strategic_vision_set", False) or vision_rev > spawned_vision_rev

    if api_was_used:
        cycle_id = (task.context or {}).get("strategic_cycle_id")
        if cycle_id:
            _update_cycle_history(project, cycle_id, {
                "phase2_completed_at": _utcnow().isoformat(),
                "vision_updated": True,
                "outcome": "completed",
            })
            session.add(project)
        task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {active_dirs} active directions"
        session.add(task)
        await session.commit()
        logger.info(f"[STRATEGIC] Task {task.id} complete via API: vision rev {vision_rev}, {active_dirs} active directions")
        return

    # Fallback: try log-based parsing for backward compatibility
    try:
        parsed = json.loads(logs[:500_000]) if logs else None
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if not parsed or not isinstance(parsed, dict):
        # No API calls and no parseable output — record failure
        project = await session.get(Project, task.project_id)
        if project:
            current_vision = project.vision or {}
            failures = current_vision.get("_strategic_failures", 0) + 1
            current_vision["_strategic_failures"] = failures
            current_vision["_strategic_failures_at"] = _utcnow().isoformat()
            project.vision = current_vision
            flag_modified(project, "vision")
            cycle_id = (task.context or {}).get("strategic_cycle_id")
            if cycle_id:
                _update_cycle_history(project, cycle_id, {
                    "phase2_completed_at": _utcnow().isoformat(),
                    "outcome": "failed",
                })
            session.add(project)
        task.latest_status_message = "Strategic output could not be parsed"
        session.add(task)
        await session.commit()
        failure_num = current_vision.get("_strategic_failures", "?") if project else "?"
        logger.warning(f"[STRATEGIC] Task {task.id}: unparseable output (failure #{failure_num})")
        if project:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review could not parse agent output (failure #{failure_num}, task {str(task.id)[:8]})")
        return

    new_vision = parsed.get("vision")
    direction_ops = parsed.get("direction_operations", [])

    # Lock project row to prevent concurrent direction/vision mutations
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    # Store raw output for admin visibility
    task.plan_output = parsed
    session.add(task)

    # Update vision — merge with current DB state to avoid overwriting concurrent user edits
    if new_vision and isinstance(new_vision, dict):
        current_vision = dict(project.vision or {})
        # Check if user edited vision while the strategic cycle was running
        payload_vision_rev = (task.first_payload.get("vision") or {}).get("revision", 0)
        db_vision_rev = current_vision.get("revision", 0)
        if db_vision_rev > payload_vision_rev:
            # User edited vision since this cycle started — merge carefully
            # Keep user's purpose/criteria if they changed them, take agent's systems/priorities/context
            logger.info(f"[STRATEGIC] Vision was edited during cycle (payload rev {payload_vision_rev}, DB rev {db_vision_rev}) — merging")
            merged = dict(current_vision)  # Start from user's current state
            # Agent's structural analysis takes precedence for these fields
            for key in ("systems", "priorities", "context"):
                if key in new_vision:
                    merged[key] = new_vision[key]
            # Only update purpose/criteria if the user didn't change them
            if current_vision.get("purpose") == (task.first_payload.get("vision") or {}).get("purpose"):
                if "purpose" in new_vision:
                    merged["purpose"] = new_vision["purpose"]
            if current_vision.get("success_criteria") == (task.first_payload.get("vision") or {}).get("success_criteria"):
                if "success_criteria" in new_vision:
                    merged["success_criteria"] = new_vision["success_criteria"]
            # Preserve any extra fields the agent added
            for key in new_vision:
                if key not in merged:
                    merged[key] = new_vision[key]
            new_vision = merged
        new_vision["revision"] = (db_vision_rev or 0) + 1
        new_vision["last_updated"] = _utcnow().isoformat()
        new_vision.pop("_strategic_failures", None)  # Reset failure counter on success
        # Preserve internal tracking fields not produced by the agent
        if "_strategic_cycle_history" in current_vision:
            new_vision["_strategic_cycle_history"] = current_vision["_strategic_cycle_history"]
        project.vision = new_vision
        flag_modified(project, "vision")
        logger.info(f"[STRATEGIC] Updated vision for project {project.id} to revision {new_vision['revision']}")
    else:
        # No vision produced — increment failure counter to prevent infinite strategic loops
        current_vision = dict(project.vision or {})
        failures = current_vision.get("_strategic_failures", 0) + 1
        current_vision["_strategic_failures"] = failures
        current_vision["_strategic_failures_at"] = _utcnow().isoformat()
        project.vision = current_vision
        flag_modified(project, "vision")
        logger.warning(f"[STRATEGIC] Task {task.id}: no vision produced (failure #{failures})")
        if failures >= 2:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review produced no vision ({failures} consecutive failures, task {str(task.id)[:8]})")

    # Process direction operations (capped to prevent flooding)
    MAX_DIRECTION_OPS = 10
    MAX_DIRECTION_CREATES = 5
    ops_applied = 0
    if direction_ops and isinstance(direction_ops, list):
        if len(direction_ops) > MAX_DIRECTION_OPS:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating {len(direction_ops)} direction ops to {MAX_DIRECTION_OPS}")
            direction_ops = direction_ops[:MAX_DIRECTION_OPS]
        directions = list(project.directions or [])
        creates_count = 0

        # Deferred import to avoid circular dependency with app.main.
        from app.main import (
            STRATEGIC_DIRECTION_TEXT_MAX,
            STRATEGIC_NOTES_HISTORY_MAX,
            STRATEGIC_RATIONALE_MAX,
            STRATEGIC_SYSTEM_ID_MAX,
        )

        for op_entry in direction_ops:
            if not isinstance(op_entry, dict):
                continue
            op = op_entry.get("op")
            rationale = str(op_entry.get("rationale", ""))
            if len(rationale) > STRATEGIC_RATIONALE_MAX:
                logger.warning(f"[STRATEGIC] Task {task.id}: truncating rationale {len(rationale)}→{STRATEGIC_RATIONALE_MAX} chars")
                rationale = rationale[:STRATEGIC_RATIONALE_MAX]

            if op == "create":
                if creates_count >= MAX_DIRECTION_CREATES:
                    continue
                text = op_entry.get("text", "").strip()
                if not text:
                    continue
                if len(text) > STRATEGIC_DIRECTION_TEXT_MAX:
                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                text = text[:STRATEGIC_DIRECTION_TEXT_MAX]
                # Dedup: skip if an active/proposed direction has very similar text
                text_lower = text.lower()
                if any(d.get("status") in ("active", "proposed") and
                       d.get("text", "").lower().strip() == text_lower
                       for d in directions):
                    logger.info(f"[STRATEGIC] Skipping duplicate direction '{text[:60]}' for project {project.id}")
                    continue
                raw_priority = op_entry.get("priority", "medium")
                priority = raw_priority if raw_priority in ("high", "medium", "low") else "medium"
                new_dir = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": text,
                    "status": "active",
                    "priority": priority,
                    "notes": [{"text": f"Created by strategic review: {rationale}", "added_at": _utcnow().isoformat()}] if rationale else [],
                    "research_history": [],
                }
                if op_entry.get("paths"):
                    new_dir["paths"] = op_entry["paths"]
                if op_entry.get("system_id"):
                    sid = str(op_entry["system_id"])
                    if len(sid) > STRATEGIC_SYSTEM_ID_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                    new_dir["system_id"] = sid[:STRATEGIC_SYSTEM_ID_MAX]
                directions.append(new_dir)
                ops_applied += 1
                creates_count += 1
                logger.info(f"[STRATEGIC] Created direction '{text[:60]}' for project {project.id}")

            elif op == "update":
                dir_id = op_entry.get("direction_id")
                changes = op_entry.get("changes", {})
                if not dir_id or not changes:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        if "text" in changes:
                            new_text = str(changes["text"])
                            if len(new_text) > STRATEGIC_DIRECTION_TEXT_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(new_text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                            d["text"] = new_text[:STRATEGIC_DIRECTION_TEXT_MAX]
                        if "priority" in changes and changes["priority"] in ("high", "medium", "low"):
                            d["priority"] = changes["priority"]
                        if "paths" in changes:
                            d["paths"] = changes["paths"]
                        if "system_id" in changes:
                            if changes["system_id"]:
                                sid_new = str(changes["system_id"])
                                if len(sid_new) > STRATEGIC_SYSTEM_ID_MAX:
                                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid_new)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                                d["system_id"] = sid_new[:STRATEGIC_SYSTEM_ID_MAX]
                            else:
                                d["system_id"] = None
                        if "status" in changes and changes["status"] in ("active", "paused", "expired"):
                            # Block reactivating a direction the user manually paused
                            if changes["status"] == "active" and d.get("status") == "paused" and not d.get("auto_paused"):
                                logger.warning(f"[STRATEGIC] Blocked reactivation of user-paused direction {dir_id}")
                            else:
                                d["status"] = changes["status"]
                        # Record rationale as a note
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Strategic update: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(f"[STRATEGIC] Updated direction {dir_id} for project {project.id}: {list(changes.keys())}")
                        break

            elif op == "retire":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        d["status"] = "expired"
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Retired by strategic review: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(f"[STRATEGIC] Retired direction {dir_id} for project {project.id}")
                        break

        if ops_applied > 0:
            project.directions = directions
            flag_modified(project, "directions")

    # Update cycle history with final outcome
    cycle_id = (task.context or {}).get("strategic_cycle_id")
    if cycle_id:
        dirs_created = sum(1 for op in (direction_ops or []) if isinstance(op, dict) and op.get("op") == "create")
        dirs_retired = sum(1 for op in (direction_ops or []) if isinstance(op, dict) and op.get("op") == "retire")
        vision_produced = bool(new_vision and isinstance(new_vision, dict))
        _update_cycle_history(project, cycle_id, {
            "phase2_completed_at": _utcnow().isoformat(),
            "vision_updated": vision_produced,
            "directions_created": dirs_created,
            "directions_retired": dirs_retired,
            "outcome": "completed" if vision_produced else "failed",
        })

    session.add(project)

    # Deferred import of message/resolution caps — same STRATEGIC_* namespace
    from app.main import STRATEGIC_MESSAGE_CONTENT_MAX, STRATEGIC_RESOLUTION_MAX

    # Process messages from agent (questions, blockers, notes)
    msgs_created = 0
    for msg_entry in (parsed.get("messages") or [])[:5]:
        if not isinstance(msg_entry, dict):
            continue
        content = (msg_entry.get("content") or "").strip()
        if not content:
            continue
        if len(content) > STRATEGIC_MESSAGE_CONTENT_MAX:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating message content {len(content)}→{STRATEGIC_MESSAGE_CONTENT_MAX} chars")
            content = content[:STRATEGIC_MESSAGE_CONTENT_MAX]
        try:
            msg_type = MessageType(msg_entry.get("type", "NOTE").upper())
        except ValueError:
            msg_type = MessageType.NOTE
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=msg_entry.get("context") or {},
        )
        session.add(new_msg)
        msgs_created += 1

    # Resolve messages the agent addressed
    msgs_resolved = 0
    # Support both formats: resolved_messages (with resolution text) and resolved_message_ids (legacy)
    resolved_entries = parsed.get("resolved_messages") or []
    if not resolved_entries:
        # Fall back to legacy format (plain ID list)
        resolved_entries = [{"id": mid} for mid in (parsed.get("resolved_message_ids") or [])]
    for entry in resolved_entries[:10]:
        try:
            msg_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            resolution_text = entry.get("resolution", "Addressed by strategic review") if isinstance(entry, dict) else "Addressed by strategic review"
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
            if msg and msg.project_id == project.id and msg.status == "open":
                msg.status = "resolved"
                if resolution_text:
                    rt = str(resolution_text)
                    if len(rt) > STRATEGIC_RESOLUTION_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating resolution {len(rt)}→{STRATEGIC_RESOLUTION_MAX} chars")
                    msg.resolution = rt[:STRATEGIC_RESOLUTION_MAX]
                else:
                    msg.resolution = "Addressed by strategic review"
                msg.resolved_at = _utcnow()
                session.add(msg)
                msgs_resolved += 1
        except (ValueError, TypeError):
            pass

    vision_rev = (project.vision or {}).get("revision", "?")
    task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} messages, {msgs_resolved} resolved"
    session.add(task)
    await session.commit()  # Single commit: vision + directions + messages + task status
    logger.info(f"[STRATEGIC] Task {task.id} complete: vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} msgs created, {msgs_resolved} resolved")


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    # Depth guard: prevent infinite research recursion
    MAX_RESEARCH_DEPTH = 3
    research_depth = (task.context or {}).get("research_depth", 0)
    parent_research_task_id = (task.context or {}).get("parent_research_task_id")
    if research_depth >= MAX_RESEARCH_DEPTH:
        task.latest_status_message = f"Research depth limit ({MAX_RESEARCH_DEPTH}) reached — not spawning further tasks"
        session.add(task)
        await session.commit()
        logger.warning(f"[RESEARCH] Depth limit reached for task {task.id} (depth={research_depth})")
        return

    findings = _parse_research_findings(logs)
    insights = _parse_research_insights(str(task.id))
    if not findings:
        task.latest_status_message = "Research completed with no actionable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    # Store all findings for admin visibility before filtering
    task.plan_output = findings
    session.add(task)
    await session.commit()

    # Separate direction proposals and notes from actionable findings — these are processed
    # separately and must not enter the hash-based deduplication loop (they never create tasks,
    # so their hashes would never be stored, allowing identical entries to accumulate).
    direction_proposals = [f for f in findings if f.get("type") == "direction_proposal"]
    direction_notes = [f for f in findings if f.get("type") == "direction_note"]
    findings = [f for f in findings if f.get("type") not in ("direction_proposal", "direction_note")]

    # Cap findings to prevent runaway task creation
    if len(findings) > 15:
        findings = findings[:15]

    original_base = (task.first_payload).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    research_branch = f"research/{short_id}"

    # Create staging branch for this research batch
    if not await _ensure_branch(task, research_branch, original_base, "RESEARCH", session):
        return

    # Validate dependencies if present
    if not _validate_plan_dependencies(findings):
        logger.warning("[RESEARCH] Findings have circular dependencies — ignoring depends_on_indices")
        for f in findings:
            f.pop("depends_on_indices", None)

    # Query active/queued tasks for deduplication
    existing_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING, TaskStatus.PROVISIONING])
        )
    )).scalars().all()
    existing_titles = [t.title.lower() for t in existing_tasks]

    # Query recent tasks (last 90 days) for hash-based deduplication across research cycles
    ninety_days_ago = _utcnow() - timedelta(days=90)
    recent_tasks_for_dedup = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.created_at >= ninety_days_ago,
        )
    )).scalars().all()
    recent_finding_hashes = {
        t.context["finding_hash"] for t in recent_tasks_for_dedup if t.context and t.context.get("finding_hash")
    }
    # Build a map of finding_hash -> outcome for failed/cancelled tasks for better logging
    failed_hashes = {
        t.context["finding_hash"]: t.status
        for t in recent_tasks_for_dedup
        if t.context and t.context.get("finding_hash")
        and t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
    }
    # Also suppress tasks that have an active suppress_until field (set on user-cancelled low-priority tasks)
    now_iso = _utcnow().isoformat()
    for t in recent_tasks_for_dedup:
        if t.context and t.context.get("suppress_until") and t.context["suppress_until"] > now_iso:
            fhash = t.context.get("finding_hash")
            if fhash:
                recent_finding_hashes.add(fhash)
                failed_hashes.setdefault(fhash, "suppressed")

    # Query open PR artifacts to detect file-scope conflicts with in-flight work
    open_pr_stmt = (
        select(Artifact.pr_url, Artifact.logs, Task.context)
        .join(Task, Artifact.task_id == Task.id)
        .where(
            Task.project_id == task.project_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
            Task.status.not_in([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
        )
        .limit(20)
    )
    open_pr_rows = (await session.execute(open_pr_stmt)).all()
    open_pr_files: set[str] = set()
    for _, _, task_ctx in open_pr_rows:
        if task_ctx:
            for f in (task_ctx.get('related_files') or []):
                open_pr_files.add(f)

    def _finding_hash(title: str, description: str) -> str:
        return hashlib.sha256((title + description).encode()).hexdigest()[:16]

    def _is_duplicate(finding_title: str, existing: list[str]) -> bool:
        words = set(finding_title.lower().split()) - {'the', 'a', 'an', 'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
        for title in existing:
            overlap = words & set(title.split())
            if len(overlap) >= 3:  # 3+ meaningful words in common
                return True
        return False

    # Sort findings by priority: high → medium → low (preserve original order within each tier).
    # The orchestrator dispatches by (status, priority, created_at), so higher-priority
    # tasks naturally run first without needing artificial wave dependencies.
    priority_order = {"high": 0, "medium": 1, "low": 2}
    indexed_findings = list(enumerate(findings))
    indexed_findings.sort(key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1))

    # Deduplicate findings against recent tasks
    skipped_duplicates = 0
    deduplicated: list[tuple[int, dict, str]] = []  # (orig_i, finding, fhash)
    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i+1}")
        description = finding.get("description", finding.get("prompt", ""))
        fhash = _finding_hash(title, description)
        if fhash in recent_finding_hashes:
            outcome = failed_hashes.get(fhash, "prior")
            logger.info(f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched {outcome} task")
            skipped_duplicates += 1
            continue
        if _is_duplicate(title, existing_titles):
            logger.warning(f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task")
            skipped_duplicates += 1
            continue
        # Skip if all related_files for this finding are already covered by open PRs
        finding_files = set(finding.get('related_files', []))
        if finding_files and finding_files.issubset(open_pr_files):
            logger.info(f"[RESEARCH] Skipping finding '{title}' — all related files already have open PRs")
            skipped_duplicates += 1
            continue
        deduplicated.append((orig_i, finding, fhash))

    # Create all child tasks — no wave dependencies, priority ordering handles dispatch order
    child_tasks = []
    finding_indices = []  # Maps child_tasks index → findings array index

    for orig_i, finding, fhash in deduplicated:
        scope = finding.get("scope", "small")
        risk = finding.get("risk", "low")
        title = finding.get("title", f"Research finding #{orig_i+1}")
        prompt = finding.get("prompt", finding.get("description", ""))

        # Determine task type early — some findings don't spawn tasks
        finding_type = finding.get("type", "improvement")

        if finding_type == "design_doc_observation":
            # Append to design doc proposals for Phase 1 to review
            obs_project = await session.get(Project, task.project_id)
            if obs_project:
                doc = dict(obs_project.design_doc or {})
                proposals = doc.get("proposed_changes", [])
                proposals.append({
                    "id": f"pc-{uuid.uuid4().hex[:8]}",
                    "observation": finding.get("description", "")[:500],
                    "section": finding.get("section", "general"),
                    "proposed_by_task": str(task.id),
                    "proposed_at": _utcnow().isoformat(),
                    "status": "pending",
                })
                doc["proposed_changes"] = proposals[-30:]  # cap at 30 pending
                obs_project.design_doc = doc
                flag_modified(obs_project, "design_doc")
                session.add(obs_project)
            continue  # Don't create a task

        if finding_type == "direction_update":
            # Process as a direction modification proposal, not a task
            target_id = finding.get("direction_id")
            if not target_id:
                continue  # direction_update requires a target direction_id
            dir_project = await session.get(Project, task.project_id)
            if not dir_project:
                continue
            directions = list(dir_project.directions or [])
            for d in directions:
                if d.get("id") == target_id:
                    d["pending_update"] = {
                        "suggested_text": finding.get("suggested_text", d.get("text")),
                        "suggested_priority": finding.get("suggested_priority", d.get("priority")),
                        "suggested_paths": finding.get("suggested_paths", d.get("paths", [])),
                        "rationale": finding.get("description", "")[:300],
                        "proposed_at": _utcnow().isoformat(),
                        "proposed_by": str(task.id),
                    }
                    break
            dir_project.directions = directions
            flag_modified(dir_project, "directions")
            session.add(dir_project)
            continue  # Don't create a task for direction_update findings

        if not prompt:
            continue
        if finding_type == "research":
            task_type = "research"  # deeper investigation before implementation
        elif finding_type in ("security_audit", "qa", "deps", "docs", "migrate", "perf"):
            task_type = finding_type  # spawn specialized agent
        elif finding_type == "refactor":
            task_type = "refactor"  # code quality improvement, no behavior change
        elif scope == "large":
            task_type = "plan"     # large scope needs decomposition
        else:
            task_type = "create"   # small/medium scope is single-PR work
        # Pre-spawn approval removed: the review pipeline handles quality gates.
        # Only final merge to main requires human approval (critical files gate).
        needs_approval = False

        direction_id = finding.get("direction_id")
        # Apply per-type model/effort overrides — take the higher of finding vs config
        _model_rank = {"claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1}
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
        finding_model = finding.get('model')
        config_model = _runtime_config.get("task_type_models", {}).get(task_type)
        if finding_model and config_model:
            finding_model = finding_model if _model_rank.get(finding_model, 0) >= _model_rank.get(config_model, 0) else config_model
        elif config_model:
            finding_model = config_model
        finding_effort = finding.get('effort')
        config_effort = _runtime_config.get("task_type_efforts", {}).get(task_type)
        if finding_effort and config_effort:
            finding_effort = finding_effort if _effort_rank.get(finding_effort, 0) >= _effort_rank.get(config_effort, 0) else config_effort
        elif config_effort:
            finding_effort = config_effort

        related_files = finding.get("related_files", [])
        full_prompt = prompt + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt
        type_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)

        # For sub-research tasks, enrich prompt with parent context to avoid duplication
        if task_type == "research":
            sibling_titles = [
                f.get('title', '') for _, f, _ in deduplicated
                if f.get('title') and f != finding
            ][:5]
            if sibling_titles:
                parent_summary = (
                    '\n\nContext from parent research task: '
                    f'This sub-research is part of a broader investigation (task {str(task.id)[:8]}) '
                    f'that also produced these parallel findings:\n' +
                    '\n'.join(f'- {t}' for t in sibling_titles) +
                    '\nFocus your investigation on the specific area described above without duplicating these parallel efforts.'
                )
                full_prompt = full_prompt + parent_summary

        finding_priority = TASK_PRIORITY_MAP.get(finding.get("priority", "medium"), 50)
        # Inherit parent direction_id if the finding doesn't specify one
        parent_direction_id = (task.context or {}).get("direction_id")
        new_task = Task(
            project_id=task.project_id,
            title=title,
            description=finding.get("description", prompt[:200]),
            parent_task_id=task.id,
            payloads=[{
                "type": task_type,
                "prompt": full_prompt,
                "base_branch": research_branch,
                **(({'model': finding_model} if finding_model else {})),
                **(({'effort': finding_effort} if finding_effort else {})),
                **({"prompt_suffix": type_suffix} if type_suffix else {}),
            }],
            context={
                "research_task_id": str(task.id),
                "parent_research_task_id": str(task.id),
                "finding_index": orig_i,
                "scope": scope,
                "risk": risk,
                "research_branch": research_branch,
                "original_base": original_base,
                "research_depth": research_depth + 1,
                "related_files": related_files,
                "finding_hash": fhash,
                **({"direction_id": direction_id} if direction_id else ({"direction_id": parent_direction_id} if parent_direction_id else {})),
                **({"parent_direction_id": parent_direction_id} if parent_direction_id else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=needs_approval,
            depends_on=[],
            priority=finding_priority,
        )
        session.add(new_task)
        child_tasks.append(new_task)
        finding_indices.append(orig_i)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Resolve explicit depends_on_indices to actual UUIDs (agent-specified inter-finding deps only)
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for ci, ct in enumerate(child_tasks):
        fi = finding_indices[ci]
        dep_indices = findings[fi].get("depends_on_indices", [])
        explicit_deps = [
            str(child_tasks[finding_to_child[j]].id)
            for j in dep_indices
            if j in finding_to_child
        ]
        if explicit_deps:
            ct.depends_on = explicit_deps
            session.add(ct)

    # Log file overlap warnings for observability and surface them to agents
    overlaps = _log_file_overlaps(findings, "RESEARCH")
    for (i, j, shared_files) in overlaps:
        for fi in (i, j):
            ci = finding_to_child.get(fi)
            if ci is not None and ci < len(child_tasks):
                ctx = dict(child_tasks[ci].context or {})
                warnings = ctx.get('file_overlap_warnings', [])
                other_fi = j if fi == i else i
                other_title = findings[other_fi].get('title', f'#{other_fi}')
                warnings.append({'shared_files': sorted(shared_files), 'with_task': other_title})
                ctx['file_overlap_warnings'] = warnings
                child_tasks[ci].context = ctx
                session.add(child_tasks[ci])

    await session.commit()

    # Update last_researched_at for directions that produced findings.
    # Also update the current direction if all findings were deduped — prevents
    # the system from immediately re-researching and hitting the same duplicates.
    direction_ids_with_findings = set()
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            direction_ids_with_findings.add(did)
    # If all findings were deduped, still put the current direction on cooldown
    current_direction_id = (task.context or {}).get("direction_id") or (task.first_payload).get("current_direction_id")
    all_deduped = len(child_tasks) == 0 and skipped_duplicates > 0
    if all_deduped and current_direction_id:
        direction_ids_with_findings.add(current_direction_id)
    if direction_ids_with_findings:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            now_iso = _utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_with_findings:
                    d["last_researched_at"] = now_iso
            # Append to research_history for each direction that produced findings
            finding_titles_by_dir: dict = {}
            finding_descriptions_by_dir: dict = {}
            for orig_i, finding, fhash in deduplicated:
                did = finding.get("direction_id")
                if did:
                    finding_titles_by_dir.setdefault(did, []).append(
                        finding.get("title", f"Research finding #{orig_i+1}")
                    )
                    finding_descriptions_by_dir.setdefault(did, []).append(
                        (finding.get("description", finding.get("prompt", "")))[:200]
                    )
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [ct for ct in child_tasks if (ct.context or {}).get("direction_id") == did]
                    entry = {
                        "timestamp": now_iso,
                        "task_id": str(task.id),
                        "tasks_created": len(dir_child_tasks),
                        "child_task_ids": [str(ct.id) for ct in dir_child_tasks],
                        "finding_titles": finding_titles_by_dir.get(did, [])[:5],
                        "key_finding_descriptions": finding_descriptions_by_dir.get(did, [])[:5],
                        "cycle_insights": [i["summary"][:200] for i in insights[:3]],
                    }
                    # Keep last 10 history entries; aggregate evicted entries to preserve lifetime stats
                    new_history = history + [entry]
                    if len(new_history) > 10:
                        evicted = new_history[:-10]
                        prev_agg = d.get("history_aggregate", {
                            "total_cycles": 0, "total_tasks_created": 0,
                            "total_prs_merged": 0, "total_tasks_failed": 0,
                            "total_tasks_cancelled": 0, "first_cycle_at": None,
                        })
                        for ev in evicted:
                            prev_agg["total_cycles"] += 1
                            prev_agg["total_tasks_created"] += ev.get("tasks_created", 0)
                            prev_agg["total_prs_merged"] += ev.get("prs_merged", 0)
                            prev_agg["total_tasks_failed"] += ev.get("tasks_failed", 0)
                            prev_agg["total_tasks_cancelled"] += ev.get("tasks_cancelled", 0)
                            if not prev_agg["first_cycle_at"] and ev.get("timestamp"):
                                prev_agg["first_cycle_at"] = ev["timestamp"]
                        d["history_aggregate"] = prev_agg
                    d["research_history"] = new_history[-10:]
                    d["possibly_saturated"] = _compute_saturation(d["research_history"])
                    d["research_context"] = _build_research_context(
                        prior_context=d.get("research_context"),
                        finding_titles=finding_titles_by_dir.get(did, [])[:5],
                        key_finding_descriptions=finding_descriptions_by_dir.get(did, [])[:5],
                        cycle_insights=[i["summary"][:200] for i in insights[:3]],
                        history_entry=entry,
                    )

                    # Auto-complete direction if saturated with sufficient merged PRs
                    if (
                        d.get("status") == "active"
                        and d.get("possibly_saturated")
                        and entry.get("tasks_created", 0) > 0
                    ):
                        history = d.get("research_history", [])
                        agg = d.get("history_aggregate", {})
                        total_merged = (
                            sum(e.get("prs_merged", 0) for e in history)
                            + (agg.get("total_prs_merged") or 0)
                        )
                        if total_merged >= 3:
                            total_cycles = len(history) + (agg.get("total_cycles") or 0)
                            d["status"] = "completed"
                            d["completed_at"] = _utcnow().isoformat()
                            d["auto_completed"] = True
                            d["auto_completed_reason"] = (
                                f"Auto-completed: direction saturated with {total_merged} "
                                f"total PRs merged across {total_cycles} cycles"
                            )
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.INFO,
                                title="Direction auto-completed",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' was auto-completed: "
                                    f"saturated with {total_merged} PRs merged across {total_cycles} cycles."
                                ),
                            )
                            session.add(alert)

            project.directions = directions
            flag_modified(project, "directions")
            session.add(project)

            # If this is a sub-research task (depth > 0), also annotate the parent direction's
            # most recent research_history entry with a note about sub-research findings.
            if research_depth > 0 and parent_research_task_id and direction_ids_with_findings:
                parent_project = await session.get(Project, task.project_id)
                if parent_project and parent_project.directions:
                    parent_dirs = list(parent_project.directions)
                    for d in parent_dirs:
                        did = d.get("id")
                        if did not in direction_ids_with_findings:
                            continue
                        history = d.get("research_history", [])
                        if history:
                            last_entry = history[-1]
                            sub_notes = last_entry.get("sub_research_notes", [])
                            sub_notes.append({
                                "sub_task_id": str(task.id),
                                "depth": research_depth,
                                "tasks_created": len(child_tasks),
                                "finding_titles": [f.get("title", "") for _, f, _ in deduplicated[:3]],
                                "insights": [i["summary"][:150] for i in insights[:2]],  # up to 2 insights, capped
                            })
                            last_entry["sub_research_notes"] = sub_notes[-5:]  # keep last 5
                            history[-1] = last_entry
                            d["research_history"] = history
                    parent_project.directions = parent_dirs
                    flag_modified(parent_project, "directions")
                    session.add(parent_project)

    # Process direction notes — append to the target direction's notes list
    if direction_notes:
        # Deferred import of strategic caps — shared with main.py
        from app.main import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
        note_project = await session.get(Project, task.project_id)
        if note_project:
            note_directions = list(note_project.directions or [])
            changed = False
            for note_finding in direction_notes:
                target_id = note_finding.get("direction_id")
                raw_note = note_finding.get("description") or note_finding.get("prompt") or ""
                if len(raw_note) > STRATEGIC_RATIONALE_MAX:
                    logger.warning(f"[RESEARCH] Task {task.id}: truncating direction note {len(raw_note)}→{STRATEGIC_RATIONALE_MAX} chars for direction {target_id}")
                note_text = raw_note[:STRATEGIC_RATIONALE_MAX]
                if not note_text or not target_id:
                    continue
                for d in note_directions:
                    if d.get("id") == target_id:
                        notes = d.get("notes", [])
                        notes.append({
                            "text": note_text,
                            "added_at": _utcnow().isoformat(),
                            "added_by_task": str(task.id),
                        })
                        if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                            logger.warning(f"[RESEARCH] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {target_id}")
                        d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        changed = True
                        break
            if changed:
                note_project.directions = note_directions
                flag_modified(note_project, "directions")
                session.add(note_project)

    # Process direction proposals — add to Project.directions with status='proposed'
    proposals_created = 0
    if direction_proposals:
        proposal_project = await session.get(Project, task.project_id)
        if proposal_project:
            proj_directions = list(proposal_project.directions or [])
            rejected_proposals = [d for d in proj_directions if d.get("status") == "rejected"]
            pending_count = sum(1 for d in proj_directions if d.get("status") == "proposed")
            max_proposals = 10
            now_iso = _utcnow().isoformat()
            for proposal in direction_proposals[:5]:  # max 5 per research call
                if pending_count >= max_proposals:
                    logger.info(f"[RESEARCH] Skipping direction proposal — max {max_proposals} pending proposals reached")
                    break
                proposal_text = (proposal.get("prompt") or proposal.get("description") or "")[:500]
                if not proposal_text.strip():
                    continue
                if _is_direction_duplicate(proposal_text, proj_directions, rejected_proposals):
                    logger.info(f"[RESEARCH] Skipping direction proposal — overlaps with existing direction")
                    continue
                new_direction = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": proposal_text,
                    "priority": proposal.get("priority", "medium") if proposal.get("priority") in ("high", "medium", "low") else "medium",
                    "paths": [],
                    "added_at": now_iso,
                    "status": "proposed",
                    "proposed_by": str(task.id),
                    "proposal_rationale": (proposal.get("description") or "")[:500],
                    "proposed_at": now_iso,
                }
                proj_directions.append(new_direction)
                pending_count += 1
                proposals_created += 1
            if proposals_created:
                proposal_project.directions = proj_directions
                flag_modified(proposal_project, "directions")
                session.add(proposal_project)
                logger.info(f"[RESEARCH] Created {proposals_created} direction proposals from task {task.id}")

    # Store a human-readable research summary artifact
    if child_tasks:
        direction_texts = []
        payload0 = task.first_payload
        if payload0.get('directions'):
            direction_texts = [payload0['directions'][0].get('text', 'general improvement')]
        summary_lines = [
            f'# Research Summary',
            f'**Direction**: {direction_texts[0][:200] if direction_texts else "general"}',
            f'**Tasks Created**: {len(child_tasks)} ({skipped_duplicates} duplicates skipped)',
            f'',
            f'## Findings:',
        ]
        for ct in child_tasks[:10]:
            p = ct.first_payload
            summary_lines.append(f'- [{p.get("type", "?")}] {ct.title}: {(ct.description or "")[:150]}')
        summary = '\n'.join(summary_lines)

        summary_artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.INVESTIGATION_REPORT,
            logs=summary,
        )
        session.add(summary_artifact)

    task.status = TaskStatus.EVALUATING
    direction_id = (task.context or {}).get("direction_id", "?")
    logger.info(
        f"[RESEARCH] Task {task.id}: {len(findings)} raw findings, "
        f"{skipped_duplicates} deduped, {len(child_tasks)} tasks created "
        f"(direction: {str(direction_id)[:8]})"
    )
    task.latest_status_message = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + (f" ({skipped_duplicates} skipped as duplicates)" if skipped_duplicates else "")
        + (f", {proposals_created} direction proposals pending review" if proposals_created else "")
    )
    task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[RESEARCH] Created {len(child_tasks)} tasks from research {task.id}")
