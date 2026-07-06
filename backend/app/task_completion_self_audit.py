"""Self-audit completion handler — ingest SELF_AUDIT_REPORT into SystemMaturity.

Extracted from ``app.task_completion`` as a single cohesive responsibility:

  * ``_parse_self_audit_findings`` / ``_normalize_self_audit_finding`` —
    parse and validate findings emitted by a self-audit task.
  * ``_maybe_auto_clear_direction_mode`` / ``_is_recent_strategic_mode_lock``
    — direction-mode propagation helpers used during ingest.
  * ``_handle_self_audit_completion`` — the entry point invoked from
    ``task_lifecycle`` when a SELF_AUDIT task completes.

``task_completion`` re-exports each public name so existing
``from app.task_completion import X`` callers keep working.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import (
    Project,
    RECOMMENDED_MODE_META_KEY,
    RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
    RECOMMENDED_MODE_SOURCE_STRATEGIC,
    SystemMaturity,
    Task,
)
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX
from app.task_locking import with_directions_locked
from app.time_utils import _utcnow
from app.json_extract import _extract_json_array

logger = logging.getLogger(__name__)

# Context key written by _handle_self_audit_completion; stores a bounded
# per-direction record of what the propagation pass decided and why.
SELF_AUDIT_PROPAGATION_RECORD_KEY = "self_audit_propagation_record"
_PROPAGATION_RECORD_MAX_ENTRIES = 200


def _record_skipped_propagation_note(
    direction: dict, proposed_mode: str, reason: str
) -> None:
    """Append a bounded, deduplicated note when self-audit propagation is skipped for a direction.

    Records what mode was proposed and why propagation was skipped so the decision
    is visible in the task record and to operators rather than silently dropped.
    Deduplicates identical skip notes to prevent unbounded growth when recurring
    audits encounter the same operator-pinned mode repeatedly.
    """
    notes = list(direction.get("notes") or [])
    new_note_text = f"Self-audit propagation skipped: proposed mode '{proposed_mode}' — {reason}"

    # Dedup: check if an identical skip note already exists
    existing_skip_note = next(
        (n for n in notes
         if n.get("source") == "self_audit_skip"
         and n.get("proposed_mode") == proposed_mode
         and n.get("text") == new_note_text),
        None
    )

    # Only append if no identical note exists
    if not existing_skip_note:
        notes.append(
            {
                "text": new_note_text,
                "source": "self_audit_skip",
                "proposed_mode": proposed_mode,
                "created_at": _utcnow().isoformat(),
            }
        )

    # Cap to max history size, keeping newest entries
    direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


def _record_auto_clear_note(direction: dict, cleared_from: str, count: int) -> None:
    """Append a bounded note to a direction dict when its mode is auto-cleared.

    Called by ``_maybe_auto_clear_direction_mode`` when a system's health has
    recovered. Re-homed here from the deleted ``app.self_audit_dispatch`` (the
    self-audit *dispatch* feeder was removed with the self-repair apparatus; the
    auto-clear note lives with the self-audit *completion* handler instead).
    """
    notes = list(direction.get("notes") or [])
    notes.append(
        {
            "text": (
                f"Auto-cleared from '{cleared_from}': system health recovered "
                f"({count} consecutive 'ship' maturity assessments)"
            ),
            "source": "auto_clear",
            "created_at": _utcnow().isoformat(),
        }
    )
    # Cap to max history size, keeping newest entries
    direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


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

    # Batch-fetch all existing SystemMaturity rows for this project in one query
    # to avoid an N+1 SELECT (one per finding) in the loop below.
    system_ids = {finding["system_id"] for finding in normalized}
    existing_rows_result = await session.execute(
        select(SystemMaturity).where(
            SystemMaturity.project_id == task.project_id,
            SystemMaturity.system_id.in_(system_ids),
        )
    )
    by_system_id: Dict[str, SystemMaturity] = {
        row.system_id: row for row in existing_rows_result.scalars().all()
    }

    # Collect (mode, system_id, consecutive_ship_count) for findings that may
    # propagate to directions; the actual directions read-modify-write is
    # serialized once, after the loop, through the canonical row lock so a
    # concurrent same-project completion is not clobbered.
    propagation_inputs: List[tuple] = []
    for finding in normalized:
        system_id = finding["system_id"]
        mode = finding["recommended_mode"]
        existing = by_system_id.get(system_id)
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
            new_row = SystemMaturity(
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
            session.add(new_row)
            by_system_id[system_id] = new_row
        upserted += 1
        if mode in _SELF_AUDIT_PROPAGATED_MODES or mode == "ship":
            propagation_inputs.append((mode, system_id, consecutive_ship_count))

    if project and propagation_inputs:
        effect_entries: List[Dict[str, Any]] = []

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
                        direction_id = d.get("id")
                        if _is_recent_strategic_mode_lock(d, mode_, now_):
                            effect_entries.append(
                                {
                                    "system_id": system_id_,
                                    "direction_id": direction_id,
                                    "proposed_mode": mode_,
                                    "outcome": "skipped",
                                    "reason": "recent stricter strategic mode lock",
                                }
                            )
                            continue
                        # Operator-pin guard: if the direction already has a mode
                        # whose source is not one of the auto sources (self_audit,
                        # strategic), treat it as a manual operator pin and never
                        # overwrite it — mirroring the auto-clear protection.
                        existing_meta = d.get(RECOMMENDED_MODE_META_KEY) or {}
                        existing_mode = d.get("recommended_mode")
                        if existing_mode is not None:
                            existing_source = (
                                existing_meta.get("source")
                                if isinstance(existing_meta, dict)
                                else None
                            )
                            _auto_sources = {
                                RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
                                RECOMMENDED_MODE_SOURCE_STRATEGIC,
                            }
                            if existing_source not in _auto_sources:
                                _record_skipped_propagation_note(
                                    d,
                                    mode_,
                                    "direction mode is operator-pinned and protected from auto propagation",
                                )
                                effect_entries.append(
                                    {
                                        "system_id": system_id_,
                                        "direction_id": direction_id,
                                        "proposed_mode": mode_,
                                        "outcome": "skipped",
                                        "reason": "operator-pinned mode",
                                    }
                                )
                                continue
                        d["recommended_mode"] = mode_
                        d[RECOMMENDED_MODE_META_KEY] = {
                            "mode": mode_,
                            "source": RECOMMENDED_MODE_SOURCE_SELF_AUDIT,
                            "source_task_id": str(task.id),
                            "set_at": now_.isoformat(),
                        }
                        effect_entries.append(
                            {
                                "system_id": system_id_,
                                "direction_id": direction_id,
                                "proposed_mode": mode_,
                                "outcome": "applied",
                                "reason": None,
                            }
                        )
                elif mode_ == "ship":
                    cleared = _maybe_auto_clear_direction_mode(
                        directions, system_id_, csc_, str(task.id), now_
                    )
                    for d in directions:
                        if (
                            d.get("status") != "active"
                            or d.get("system_id") != system_id_
                        ):
                            continue
                        direction_id = d.get("id")
                        if cleared:
                            effect_entries.append(
                                {
                                    "system_id": system_id_,
                                    "direction_id": direction_id,
                                    "proposed_mode": mode_,
                                    "outcome": "cleared",
                                    "reason": f"auto-cleared after {csc_} consecutive ship assessments",
                                }
                            )
                        else:
                            effect_entries.append(
                                {
                                    "system_id": system_id_,
                                    "direction_id": direction_id,
                                    "proposed_mode": mode_,
                                    "outcome": "skipped",
                                    "reason": f"ship assessment below threshold ({csc_} < {_SHIP_RECOVERY_THRESHOLD}) or mode not auto-clearable",
                                }
                            )
            return None

        # Drop the cached directions snapshot (expire_on_commit=False) so the
        # helper reads the row fresh under the lock.
        session.expire(project, ["directions"])
        await with_directions_locked(session, task.project_id, _propagate_modes)

        # Store a bounded propagation record in task.context for operator/debug visibility.
        ctx = dict(task.context or {})
        ctx[SELF_AUDIT_PROPAGATION_RECORD_KEY] = effect_entries[
            :_PROPAGATION_RECORD_MAX_ENTRIES
        ]
        task.context = ctx
        flag_modified(task, "context")

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
