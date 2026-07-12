"""Post-spawn direction state management for research completion.

Extracted from app.research_completion to keep _handle_research_completion focused
on spawn-time concerns (dedup, scope calibration, child task creation). This module
holds the after-spawn logic that updates direction research_history, computes
saturation / dedup deadlock, auto-completes saturated directions, processes
direction notes and proposals, writes the summary artifact, and broadcasts the
completion event.
"""

import contextlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.broadcast import broadcast_to_project
from app.direction_metrics import (
    _fold_evicted_into_aggregate,
    build_evicted_cycle_snapshot,
)
from app.direction_outcomes import credit_gate_merged_count
from app.failure_classification import CO_DEFERRED_SHELVED
from app.research_context import (
    _build_research_context,
    build_recent_findings_evidence,
    extract_diagnosis_from_context,
)
from app.shared_state import DEFAULT_TASK_SCOPE
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
from app.shared_state import runtime_config as _runtime_config
from app.task_locking import with_design_doc_locked, with_directions_locked
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


RETIRED_DIRECTION_STATUSES = frozenset(
    {"expired", "retired", "superseded", "completed"}
)

_EVICTED_FINDINGS_CAP = 50
_ARCHIVED_DIAGNOSES_CAP = 50
_DIAGNOSIS_MAX_CHARS = 500
_CHALLENGE_BOUNDARY_BUCKETS = (
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
)
_CHALLENGE_AGREEMENT_BUCKETS = (
    CHALLENGE_AGREES,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)

def _empty_finding_challenge_summary() -> dict:
    return {
        "challenged_findings": 0,
        "agreement_distribution": {k: 0 for k in _CHALLENGE_AGREEMENT_BUCKETS},
        "boundary_distribution": {k: 0 for k in _CHALLENGE_BOUNDARY_BUCKETS},
        "fail_closed_unparseable": 0,
    }


def _summarize_finding_challenge(record: dict) -> dict:
    summary = _empty_finding_challenge_summary()
    summary["challenged_findings"] = 1
    agreement = record.get("agreement")
    if agreement not in _CHALLENGE_AGREEMENT_BUCKETS:
        agreement = CHALLENGE_INDETERMINATE
    summary["agreement_distribution"][agreement] += 1
    boundary = record.get("challenged_boundary")
    if boundary in _CHALLENGE_BOUNDARY_BUCKETS:
        summary["boundary_distribution"][boundary] += 1
    if agreement == CHALLENGE_INDETERMINATE and not record.get(
        "second_opinion_verdict"
    ):
        summary["fail_closed_unparseable"] += 1
    return summary


def _merge_finding_challenge_summary(target: dict, source: dict) -> None:
    target["challenged_findings"] += source.get("challenged_findings") or 0
    target["fail_closed_unparseable"] += source.get("fail_closed_unparseable") or 0
    for key in _CHALLENGE_AGREEMENT_BUCKETS:
        target["agreement_distribution"][key] += (
            source.get("agreement_distribution") or {}
        ).get(key) or 0
    for key in _CHALLENGE_BOUNDARY_BUCKETS:
        target["boundary_distribution"][key] += (
            source.get("boundary_distribution") or {}
        ).get(key) or 0


def _append_evicted_findings(direction: dict, new_entries: list) -> None:
    """Append evicted finding records to direction.history_aggregate.evicted_findings.

    Maintains a FIFO list capped at _EVICTED_FINDINGS_CAP entries to bound storage.
    """
    if not new_entries:
        return
    agg = direction.get("history_aggregate") or {}
    existing = list(agg.get("evicted_findings") or [])
    existing.extend(new_entries)
    if len(existing) > _EVICTED_FINDINGS_CAP:
        existing = existing[-_EVICTED_FINDINGS_CAP:]
    agg["evicted_findings"] = existing
    direction["history_aggregate"] = agg


def _append_archived_diagnosis(doc: dict, direction: dict, direction_id: str) -> None:
    """Archive a direction's diagnosis when it is completed/retired.

    Extracts the diagnosis summary from the direction's research_context and appends
    it to doc['retired_diagnoses_archive']. Deduplicates by direction_id: if an entry
    with the same direction_id already exists, replaces it instead of appending.
    Maintains FIFO list capped at _ARCHIVED_DIAGNOSES_CAP entries, each truncated
    to _DIAGNOSIS_MAX_CHARS.

    Operates on the doc dict in place so it can be called inside
    with_design_doc_locked (which passes the freshly-locked dict).
    """
    research_context = direction.get("research_context", "")
    diagnosis = extract_diagnosis_from_context(research_context)
    if not diagnosis or not diagnosis.strip():
        return
    diagnosis = diagnosis.strip()[:_DIAGNOSIS_MAX_CHARS]
    archive = list(doc.get("retired_diagnoses_archive") or [])

    # Deduplicate by direction_id: remove existing entry if present
    archive = [e for e in archive if e.get("direction_id") != direction_id]

    archive.append(
        {
            "direction_id": direction_id,
            "diagnosis": diagnosis,
            "archived_at": _utcnow().isoformat(),
        }
    )
    if len(archive) > _ARCHIVED_DIAGNOSES_CAP:
        archive = archive[-_ARCHIVED_DIAGNOSES_CAP:]
    doc["retired_diagnoses_archive"] = archive


def _compute_saturation(history: list) -> bool:
    """Return True if last 3 *genuine* cycles all produced 0 or 1 tasks.

    A cycle is dedup-zeroed (not genuine) when tasks_created==0 and dedup
    filters (hash_dedup or word_overlap_dedup) account for the zero — meaning
    the agent found real content but it was rejected as already-known. Those
    cycles do not prove the direction is exhausted, so they are skipped when
    building the saturation window.
    """
    genuine = []
    for entry in reversed(history):
        if entry.get("tasks_created", 0) == 0:
            fb = entry.get("filter_breakdown") or {}
            dedup_count = (fb.get("hash_dedup") or 0) + (
                fb.get("word_overlap_dedup") or 0
            )
            if dedup_count > 0:
                # Dedup-zeroed cycle — skip, not genuine exhaustion
                continue
        genuine.append(entry)
        if len(genuine) == 3:
            break
    if len(genuine) < 3:
        return False
    return all(entry.get("tasks_created", 0) <= 1 for entry in genuine)


def _detect_dedup_deadlock(history: list, window: int = 3) -> tuple[bool, int]:
    """Detect dedup deadlock: last `window` entries all have tasks_created==0 with dedup blocks.

    A direction is considered deadlocked when research cycles keep producing findings that are
    immediately rejected by hash or word-overlap deduplication, wasting VM compute. Detection
    requires that the last `window` entries all have tasks_created==0 AND at least 2 of those
    entries show non-zero hash_dedup or word_overlap_dedup counts.

    Returns (is_deadlocked, consecutive_empty_count).
    """
    if len(history) < window:
        return False, 0
    recent = history[-window:]
    if not all(e.get("tasks_created", 0) == 0 for e in recent):
        return False, 0
    # At least 2 of the last `window` entries must show dedup activity
    dedup_active_count = sum(
        1
        for e in recent
        if (e.get("filter_breakdown", {}).get("hash_dedup", 0) or 0)
        + (e.get("filter_breakdown", {}).get("word_overlap_dedup", 0) or 0)
        > 0
    )
    if dedup_active_count < 2:
        return False, 0
    # Count consecutive empty cycles from the end
    consecutive = 0
    for e in reversed(history):
        if e.get("tasks_created", 0) == 0:
            consecutive += 1
        else:
            break
    return True, consecutive


def _detect_already_fixed_deadlock(history: list, window: int = 3) -> tuple[bool, int]:
    """Detect already-fixed deadlock: last `window` consecutive history entries whose
    spawned tasks all terminated as SCOPE_EMPTY no-ops.

    Unlike the dedup deadlock (tasks_created==0), this mode DOES create tasks each
    cycle — the tasks simply terminate as CANCELLED+SCOPE_EMPTY because the work was
    already done on HEAD. Only entries that have ``scope_empty_noop_count`` populated
    (written by ``_update_direction_research_outcomes`` after child termination) are
    eligible. Legacy entries without the key stop the consecutive window.

    Returns (is_deadlocked, consecutive_noop_count).
    """
    consecutive = 0
    for e in reversed(history):
        if "scope_empty_noop_count" not in e:
            # Legacy entry without the field — treat as window break.
            break
        tasks_created = e.get("tasks_created", 0)
        scope_empty = e.get("scope_empty_noop_count", 0)
        if tasks_created >= 1 and scope_empty >= tasks_created:
            consecutive += 1
        else:
            break
    if consecutive < window:
        return False, 0
    return True, consecutive


def direction_refractory_minutes() -> float:
    """Refractory window stamped on a direction when its research cycle completes.

    Defaults to the operator-tuned ``autonomous_direction_cooldown_minutes`` so
    the post-cycle hold and the spawn-side recency cooldown are one knob — a
    refractory longer than the operator's cooldown silently overrides operator
    throttling intent. An explicit ``direction_refractory_minutes`` runtime
    value decouples them if a longer hold is ever wanted.
    """
    value = _runtime_config.get("direction_refractory_minutes")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    fallback = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    if isinstance(fallback, (int, float)) and not isinstance(fallback, bool):
        return float(fallback)
    return 240.0


def direction_saturation_refractory_minutes() -> float:
    """Longer hold for saturated / no-findings directions: 2x the standard window."""
    value = _runtime_config.get("direction_saturation_refractory_minutes")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 2.0 * direction_refractory_minutes()



async def _maybe_autocomplete_exhausted_direction(
    session: AsyncSession,
    task: Task,
    d: Dict[str, Any],
    did: str,
    pending_archived: list,
    *,
    exhausted: bool | None = None,
) -> bool:
    """Route a genuinely-exhausted direction to terminal COMPLETED under the
    shared guardrails. Returns True iff the direction was completed.

    Supersedes the #10112 dedup-deadlock auto-pause and the #10113 already-fixed
    -deadlock alert-only no-op: a direction with no new work left (saturated /
    dedup-deadlocked / already-fixed-deadlocked) that is NOT externally frozen and
    NOT code-failing is retired terminally instead of looping forever. The proven
    >=3-verified-merge completion route stays a separate, additive path.

    Preserves the same completion side-effects as the proven-merge route:
    ``auto_completed`` / ``auto_completed_reason`` / ``completed_at`` stamps, an
    INFO Alert, the deferred diagnosis archive, and the milestone message. The
    ``exhausted`` override lets a caller that has already detected a specific
    deadlock signal pass it in (see ``_should_autocomplete_exhausted``).
    """
    from app.strategic_completion import (
        _exhaustion_complete_reason,
        _should_autocomplete_exhausted,
    )

    if not _should_autocomplete_exhausted(d, exhausted=exhausted):
        return False

    reason = _exhaustion_complete_reason(d)
    d["status"] = "completed"
    d["completed_at"] = _utcnow().isoformat()
    d["auto_completed"] = True
    d["auto_completed_reason"] = reason
    # Capture diagnosis info for the deferred design_doc archive write (same as the
    # proven-merge route), performed after the directions commit.
    pending_archived.append((dict(d), did))
    session.add(
        Alert(
            project_id=task.project_id,
            severity=AlertSeverity.INFO,
            title="Direction auto-completed",
            message=(
                f"Direction '{d.get('text', did)[:80]}' was auto-completed: {reason}."
            ),
        )
    )
    try:
        from app.notification_dispatch import (
            post_direction_milestone_message,
        )

        await post_direction_milestone_message(
            session,
            task.project_id,
            direction_text=d.get("text", ""),
            milestone="auto-completed",
            reason=reason,
            direction_id=did,
        )
    except Exception as _notify_exc:
        logger.warning(
            f"[NOTIFY] Failed to post direction milestone message: {_notify_exc}"
        )
    logger.info(
        f"[RESEARCH] Auto-completed exhausted direction {did} "
        f"(reason: {reason!r}, task {task.id})"
    )
    return True


async def apply_post_spawn_state(
    session: AsyncSession,
    task: Task,
    findings: List[Dict[str, Any]],
    child_tasks: List[Task],
    deduplicated: List[tuple],
    insights: List[Dict[str, Any]],
    direction_notes: List[Dict[str, Any]],
    direction_proposals: List[Dict[str, Any]],
    hash_dedup_count: int,
    word_overlap_count: int,
    file_overlap_count: int,
    evidence_overlap_count: int,
    type_filtered_count: int,
    empty_prompt_count: int,
    research_branch: str,
    original_base: str,
    research_depth: int,
    parent_research_task_id: str | None,
    batch_cap_applied_count: int = 0,
    filtered_findings: List[Dict[str, Any]] | None = None,
    type_filtered_by_dir: dict | None = None,
    salvage_count: int = 0,
) -> None:
    """Apply all direction-state updates after research child tasks have been spawned.

    Responsibilities:
    - Append research_history entries for directions that produced findings
    - Compute saturation + dedup deadlock signals and create alerts/messages
    - Rebuild research_context for each direction
    - Auto-complete saturated directions that hit the merged-PR threshold
    - Annotate parent direction history for sub-research tasks
    - Process direction notes and direction proposals
    - Write the summary artifact
    - Transition task status (EVALUATING vs COMPLETED with zero children)
    - Broadcast the autonomous_activity event
    """
    # Update last_researched_at for directions that produced findings.
    # Also update the current direction if all findings were deduped — prevents
    # the system from immediately re-researching and hitting the same duplicates.
    direction_ids_with_findings = set()
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            direction_ids_with_findings.add(did)
    # Include directions that produced deduplicated (actionable) findings even
    # when no child tasks were created (e.g. testing, deferred spawn, or task
    # creation failure) so each such direction still gets a history entry.
    for _orig_i, _finding, _fhash in deduplicated:
        _did = _finding.get("direction_id")
        if _did:
            direction_ids_with_findings.add(_did)
    # If all findings were deduped OR no findings at all, still put the current
    # direction on cooldown and record a cycle entry. This closes a blind spot
    # where research tasks that completed with zero actionable findings (agent
    # subtask failure, malformed output, etc.) left no trace in research_history
    # and _check_direction_health could not see the stalled cycle.
    current_direction_id = (task.context or {}).get("direction_id") or (
        task.first_payload
    ).get("current_direction_id")
    skipped_duplicates = (
        hash_dedup_count
        + word_overlap_count
        + file_overlap_count
        + evidence_overlap_count
    )
    all_deduped = len(child_tasks) == 0 and skipped_duplicates > 0
    # Build filter breakdown string (computed here so it's available for summary artifact below)
    _filter_parts = []
    if hash_dedup_count:
        _filter_parts.append(f"{hash_dedup_count} hash-dedup")
    if word_overlap_count:
        _filter_parts.append(f"{word_overlap_count} word-overlap")
    if file_overlap_count:
        _filter_parts.append(f"{file_overlap_count} file-overlap")
    if evidence_overlap_count:
        _filter_parts.append(f"{evidence_overlap_count} evidence-overlap")
    if type_filtered_count:
        _filter_parts.append(f"{type_filtered_count} type-filtered")
    if empty_prompt_count:
        _filter_parts.append(f"{empty_prompt_count} empty-prompt")
    _filter_str = f" ({', '.join(_filter_parts)} skipped)" if _filter_parts else ""
    no_findings_cycle = len(child_tasks) == 0 and skipped_duplicates == 0
    if (all_deduped or no_findings_cycle) and current_direction_id:
        direction_ids_with_findings.add(current_direction_id)
    if direction_ids_with_findings:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            _pending_archived_diagnoses: list[tuple] = []
            now_iso = _utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_with_findings:
                    d["last_researched_at"] = now_iso
            # Append to research_history for each direction that produced findings
            finding_titles_by_dir: dict = {}
            finding_descriptions_by_dir: dict = {}
            finding_scopes_by_dir: dict = {}
            finding_claims_by_dir: dict = {}
            finding_sources_by_dir: dict = {}
            for orig_i, finding, fhash in deduplicated:
                did = finding.get("direction_id")
                if did:
                    finding_titles_by_dir.setdefault(did, []).append(
                        finding.get("title", f"Research finding #{orig_i + 1}")
                    )
                    finding_descriptions_by_dir.setdefault(did, []).append(
                        (finding.get("description", finding.get("prompt", "")))[:200]
                    )
                    finding_scopes_by_dir.setdefault(did, []).append(
                        finding.get("scope", DEFAULT_TASK_SCOPE)
                    )
                    finding_claims_by_dir.setdefault(did, []).append(
                        finding.get("claims") or []
                    )
                    finding_sources_by_dir.setdefault(did, []).append(
                        finding.get("sources") or []
                    )
            # Group filter-skip records by direction so each direction's
            # research_history entry can record the exact titles + reasons
            # that were rejected on this cycle. Bucket entries without a
            # direction_id under the task's current direction so they are
            # still surfaced next cycle. Also tally per-reason counts so the
            # filter_breakdown block can reflect only this direction's skips
            # instead of the global totals.
            filtered_findings_by_dir: dict = {}
            hash_dedup_by_dir: dict = {}
            word_overlap_by_dir: dict = {}
            file_overlap_by_dir: dict = {}
            evidence_overlap_by_dir: dict = {}
            for rec in filtered_findings or []:
                did = rec.get("direction_id") or current_direction_id
                if not did:
                    continue
                reason = rec.get("reason", "")
                filtered_findings_by_dir.setdefault(did, []).append(
                    {
                        "title": rec.get("title", "")[:120],
                        "reason": reason[:80],
                    }
                )
                if reason.startswith("hash match"):
                    hash_dedup_by_dir[did] = hash_dedup_by_dir.get(did, 0) + 1
                # Region-identity duplicates (active-task "region overlap with:" and
                # recent-terminal "recent terminal region overlap with:"), plus the
                # legacy "word overlap with:" prefix, all roll up to word_overlap_dedup.
                elif "region overlap" in reason or reason.startswith("word overlap"):
                    word_overlap_by_dir[did] = word_overlap_by_dir.get(did, 0) + 1
                elif reason.startswith("all files covered"):
                    file_overlap_by_dir[did] = file_overlap_by_dir.get(did, 0) + 1
                elif reason.startswith("evidence overlap"):
                    evidence_overlap_by_dir[did] = (
                        evidence_overlap_by_dir.get(did, 0) + 1
                    )

            # Count raw actionable findings per direction so each direction's
            # history entry records only its own findings_produced instead of
            # the global total. Applies the same parent-direction fallback used
            # for filtered_findings bucketing.
            findings_produced_by_dir: dict = {}
            grade_dist_by_dir: dict = {}
            basis_dist_by_dir: dict = {}
            challenge_summary_by_dir: dict = {}
            _valid_bases = (
                "code_verified",
                "path_verified",
                "pattern_inferred",
                "deduced",
            )
            for _f in findings:
                _did = _f.get("direction_id") or current_direction_id
                if _did:
                    findings_produced_by_dir[_did] = (
                        findings_produced_by_dir.get(_did, 0) + 1
                    )
                    _g = _f.get("actionability_grade", "MEDIUM")
                    if _g not in ("HIGH", "MEDIUM", "LOW"):
                        _g = "MEDIUM"
                    _gd = grade_dist_by_dir.setdefault(
                        _did, {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
                    )
                    _gd[_g] = _gd.get(_g, 0) + 1
                    _vb = _f.get("verification_basis", "pattern_inferred")
                    if _vb not in _valid_bases:
                        _vb = "pattern_inferred"
                    _bd = basis_dist_by_dir.setdefault(
                        _did,
                        {
                            "code_verified": 0,
                            "path_verified": 0,
                            "pattern_inferred": 0,
                            "deduced": 0,
                        },
                    )
                    _bd[_vb] = _bd.get(_vb, 0) + 1
                    _challenge = _f.get("finding_challenge")
                    if isinstance(_challenge, dict):
                        _summary = challenge_summary_by_dir.setdefault(
                            _did, _empty_finding_challenge_summary()
                        )
                        _merge_finding_challenge_summary(
                            _summary, _summarize_finding_challenge(_challenge)
                        )
            # Batch-load undismissed deadlock alerts for this project once (was up
            # to 2*N per-direction Alert existence queries). Dedup is matched
            # in-memory below by checking whether any alert of the relevant title
            # already references the direction id in its message text — the exact
            # equivalent of the per-direction ``Alert.message.contains(did)`` check.
            _dedup_deadlock_alert_messages: list[str] = []
            _af_deadlock_alert_messages: list[str] = []
            _undismissed_deadlock_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == task.project_id,
                            Alert.dismissed_at.is_(None),
                            Alert.title.in_(
                                [
                                    "Dedup deadlock detected",
                                    "Already-fixed deadlock detected",
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for _alert in _undismissed_deadlock_alerts:
                if _alert.title == "Dedup deadlock detected":
                    _dedup_deadlock_alert_messages.append(_alert.message or "")
                elif _alert.title == "Already-fixed deadlock detected":
                    _af_deadlock_alert_messages.append(_alert.message or "")
            # Read salvage activity from parent task context (set by
            # research_completion.py when salvage-bundle subtask was spawned).
            # Shared across all direction entries for this research cycle.
            _salvage_from_ctx: dict[str, Any] | None = (
                (task.context or {}).get("salvage_activity")
                if hasattr(task, "context")
                else None
            )
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [
                        ct
                        for ct in child_tasks
                        if (ct.context or {}).get("direction_id") == did
                    ]
                    # Clear deadlock_filtered_titles when the cycle is productive so
                    # we don't keep surfacing stale rejected titles after recovery.
                    if len(dir_child_tasks) > 0 and d.get("deadlock_filtered_titles"):
                        del d["deadlock_filtered_titles"]
                    # NOTE: churn_family_pause_count is intentionally NOT reset here.
                    # Creating child tasks is not the same as shipping merged PRs —
                    # unproductive/churning directions routinely spawn tasks that
                    # never merge. apply_post_spawn_state runs between lifecycle
                    # ticks, so resetting on mere task creation let a paused
                    # (counter=1) direction wipe its counter before the next tick
                    # could advance it to the >=2 auto-retire fence, freezing the
                    # zombie ladder one short of retirement. The counter is now
                    # incremented only by direction_lifecycle.py and never reset
                    # externally; a genuinely healthy direction simply stops being
                    # churn-paused, so a stale non-zero counter is harmless
                    # (ref bf6d8788).
                    # Compact history entry — just counters and a short summary.
                    # Full details available via task API using task_id.
                    titles = finding_titles_by_dir.get(did, [])[:3]
                    descriptions = finding_descriptions_by_dir.get(did, [])[:3]
                    _dir_grade_dist = grade_dist_by_dir.get(did)
                    _dir_basis_dist = basis_dist_by_dir.get(did)
                    _dir_challenge_summary = challenge_summary_by_dir.get(did)
                    entry = {
                        "timestamp": now_iso,
                        "task_id": str(task.id),
                        "tasks_created": sum(
                            1
                            for ct in dir_child_tasks
                            if not (
                                ct.status == TaskStatus.CANCELLED
                                and (
                                    (ct.context or {}).get(
                                        "prerequisite_escalated_to_parent"
                                    )
                                    or (ct.context or {}).get(CO_DEFERRED_SHELVED)
                                )
                            )
                        ),
                        "findings_produced": findings_produced_by_dir.get(did, 0),
                        "batch_cap_applied": batch_cap_applied_count,
                        "summary": "; ".join(t[:60] for t in titles)
                        if titles
                        else "no findings",
                        "finding_titles": titles,
                        "key_finding_descriptions": descriptions,
                        **(
                            {"grade_distribution": _dir_grade_dist}
                            if _dir_grade_dist
                            else {}
                        ),
                        **(
                            {"verification_basis_distribution": _dir_basis_dist}
                            if _dir_basis_dist
                            else {}
                        ),
                        **(
                            {"finding_challenge_summary": _dir_challenge_summary}
                            if _dir_challenge_summary
                            and _dir_challenge_summary.get("challenged_findings")
                            else {}
                        ),
                        "filter_breakdown": {
                            "hash_dedup": hash_dedup_by_dir.get(did, 0),
                            "word_overlap_dedup": word_overlap_by_dir.get(did, 0),
                            "file_overlap_dedup": file_overlap_by_dir.get(did, 0),
                            "evidence_overlap_dedup": evidence_overlap_by_dir.get(
                                did, 0
                            ),
                            "type_filtered": (type_filtered_by_dir or {}).get(did, 0),
                            "empty_prompt": empty_prompt_count,
                            "salvage_count": salvage_count,
                        },
                    }
                    # If the parent research task spawned a salvage-bundle subtask,
                    # record the salvage metadata in the direction history so that
                    # downstream consumers (research agent prompt, strategist,
                    # admin UI) can see that discarded findings were durably
                    # captured rather than silently dropped.
                    if _salvage_from_ctx:
                        entry["salvage"] = _salvage_from_ctx
                        entry["filter_breakdown"]["salvaged"] = (
                            _salvage_from_ctx.get("salvaged_count", 0)
                        )
                    # Record exact titles+reasons so the research agent can be
                    # shown what was filtered and steer to different approaches
                    # on its next cycle. Cap at 5 per cycle to bound prompt
                    # growth; priority ordering (high→medium→low) is already
                    # applied upstream in deduplicate_findings.
                    _dir_filtered = filtered_findings_by_dir.get(did) or []
                    if _dir_filtered:
                        entry["filtered_findings"] = _dir_filtered[:5]
                    if no_findings_cycle and not titles:
                        entry["no_findings"] = True
                    # Keep last 10 history entries; aggregate evicted entries to preserve lifetime stats
                    # Guard against duplicate task_ids on post-commit retry: if this task_id
                    # is already in the history, skip the append to ensure idempotency.
                    existing_task_ids = {e.get("task_id") for e in history}
                    agg = d.get("history_aggregate") or {}
                    for _ev in (agg.get("evicted_findings") or []):
                        if _ev.get("task_id"):
                            existing_task_ids.add(_ev.get("task_id"))
                    if entry.get("task_id") not in existing_task_ids:
                        new_history = history + [entry]
                    else:
                        new_history = history
                    if len(new_history) > 10:
                        d["history_aggregate"] = _fold_evicted_into_aggregate(
                            d.get("history_aggregate"), new_history[:-10]
                        )
                        # Store compact per-cycle snapshots so post-eviction patch paths
                        # (post-merge recompute, stranded-PR discount) can apply deltas to the
                        # aggregate rather than silently no-oping when the live entry is gone.
                        _evicted_snaps = d["history_aggregate"].setdefault(
                            "_evicted_cycle_snapshots", {}
                        )
                        for _ev in new_history[:-10]:
                            _tid = _ev.get("task_id")
                            if _tid:
                                _evicted_snaps[str(_tid)] = (
                                    build_evicted_cycle_snapshot(_ev)
                                )
                    d["research_history"] = new_history[-10:]
                    d["possibly_saturated"] = _compute_saturation(d["research_history"])
                    # Saturated directions get a longer refractory so the portfolio
                    # is not immediately re-queued on a direction that has no work left.
                    if d.get("possibly_saturated"):
                        _sat_refractory_min = direction_saturation_refractory_minutes()
                        if (
                            isinstance(_sat_refractory_min, (int, float))
                            and _sat_refractory_min > 0
                        ):
                            d["refractory_until"] = (
                                _utcnow()
                                + timedelta(minutes=float(_sat_refractory_min))
                            ).isoformat()

                    # Deadlock detection: 3+ consecutive 0-task cycles driven by dedup
                    _deadlocked, _consec = _detect_dedup_deadlock(d["research_history"])
                    if _deadlocked and d.get("status") == "active":
                        # #10112 superseded: retire a genuinely-exhausted dedup-
                        # deadlocked direction terminally instead of auto-pausing-and
                        # -recovering, UNLESS an external freeze or code-failure pattern
                        # means it is not truly exhausted — in which case completion
                        # is declined and the existing auto-pause + time-based recovery
                        # path below still fires.
                        await _maybe_autocomplete_exhausted_direction(
                            session,
                            task,
                            d,
                            did,
                            _pending_archived_diagnoses,
                            exhausted=True,
                        )
                    # Existing auto-pause path: only when the direction was NOT just
                    # completed above (status flips to 'completed' on completion).
                    if _deadlocked and d.get("status") == "active":
                        _blocked_titles = [
                            f.get("title", "")[:60]
                            for f in (task.plan_output or [])
                            if f.get("type")
                            not in (
                                "direction_proposal",
                                "direction_note",
                                "design_doc_observation",
                                "direction_update",
                            )
                            and f.get("title")
                        ][:3]
                        # Guard: only create alert if no undismissed alert exists for this direction
                        _existing_deadlock_alert = any(
                            did in _msg for _msg in _dedup_deadlock_alert_messages
                        )
                        if not _existing_deadlock_alert:
                            _deadlock_alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title="Dedup deadlock detected",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' (id: {did}) has "
                                    f"{_consec} consecutive research cycles with 0 tasks created "
                                    f"due to deduplication. "
                                    + (
                                        f"Top blocked findings: {'; '.join(_blocked_titles)}."
                                        if _blocked_titles
                                        else ""
                                    )
                                ),
                                task_id=task.id,
                            )
                            session.add(_deadlock_alert)
                            _deadlock_msg = ProjectMessage(
                                project_id=task.project_id,
                                message_type=MessageType.BUG_REPORT,
                                from_type="agent",
                                from_task_id=task.id,
                                content=(
                                    f"Dedup deadlock detected for direction "
                                    f"'{d.get('text', did)[:80]}' (id: {did}): "
                                    f"{_consec} consecutive research cycles produced 0 tasks "
                                    f"because findings keep matching existing hashes or "
                                    f"word-overlapping active tasks. "
                                    + (
                                        f"Blocked: {'; '.join(_blocked_titles)}. "
                                        if _blocked_titles
                                        else ""
                                    )
                                    + "Consider resetting the direction or waiting for "
                                    "blocked tasks to complete."
                                ),
                                context={
                                    "source": "dedup_deadlock",
                                    "direction_id": did,
                                    "consecutive_empty_cycles": _consec,
                                },
                            )
                            session.add(_deadlock_msg)
                            logger.warning(
                                f"[RESEARCH] Dedup deadlock: direction {did}, "
                                f"{_consec} consecutive empty cycles (task {task.id})"
                            )
                        # Auto-pause the direction to remove it from the research
                        # rotation. The existing auto-recovery path in
                        # direction_lifecycle._process_direction_lifecycle reactivates
                        # it once the cooldown elapses (direction_dedup_deadlock_recovery_hours,
                        # default 8h), giving in-flight tasks time to drain and HEAD
                        # to advance so genuinely-new findings become possible.
                        # needs_manual_intervention is intentionally NOT set here so
                        # the time-based recovery fires unblocked.
                        d["status"] = "paused"
                        d["auto_paused"] = True
                        d["paused_at"] = now_iso
                        d["paused_reason"] = (
                            f"dedup_deadlock: {_consec} consecutive zero-task cycles"
                        )
                        d["auto_pause_count"] = d.get("auto_pause_count", 0) + 1
                        logger.info(
                            f"[RESEARCH] Auto-paused direction {did} (dedup_deadlock, "
                            f"{_consec} consecutive empty cycles, task {task.id})"
                        )
                        # Collect rejected finding titles from the deadlocked window so
                        # the next research cycle's prompt can exclude them explicitly.
                        _dft_seen: set = set()
                        _dft: list = []
                        for _he in d["research_history"][-_consec:]:
                            for _ff in _he.get("filtered_findings") or []:
                                _t = (_ff.get("title") or "").strip()
                                if _t and _t not in _dft_seen:
                                    _dft_seen.add(_t)
                                    _dft.append(_t)
                                    if len(_dft) >= 15:
                                        break
                            if len(_dft) >= 15:
                                break
                        if _dft:
                            d["deadlock_filtered_titles"] = _dft

                    # Already-fixed deadlock detection: check prior completed cycles
                    # (exclude the just-added entry — its children haven't resolved yet).
                    _af_deadlocked, _af_consec = _detect_already_fixed_deadlock(
                        d["research_history"][:-1]
                    )
                    if _af_deadlocked and d.get("status") == "active":
                        # #10113 superseded: route a genuinely-exhausted already-fixed
                        # -deadlocked direction to terminal COMPLETED instead of the
                        # alert-only no-op. Detection uses history[:-1] (the just-added
                        # cycle's children have not resolved yet), so pass exhausted=True
                        # rather than re-deriving from the full history. If an external
                        # freeze / code-failure pattern blocks completion, fall through
                        # to the existing advisory alert below.
                        await _maybe_autocomplete_exhausted_direction(
                            session,
                            task,
                            d,
                            did,
                            _pending_archived_diagnoses,
                            exhausted=True,
                        )
                    if _af_deadlocked and d.get("status") == "active":
                        _existing_af_alert = any(
                            did in _msg for _msg in _af_deadlock_alert_messages
                        )
                        if not _existing_af_alert:
                            _af_alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title="Already-fixed deadlock detected",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' (id: {did}) has "
                                    f"{_af_consec} consecutive research cycles whose spawned "
                                    f"tasks all terminated as SCOPE_EMPTY (work already done "
                                    f"on HEAD). The direction is spending compute re-discovering "
                                    f"already-shipped fixes."
                                ),
                                task_id=task.id,
                            )
                            session.add(_af_alert)
                            _af_msg = ProjectMessage(
                                project_id=task.project_id,
                                message_type=MessageType.BUG_REPORT,
                                from_type="agent",
                                from_task_id=task.id,
                                content=(
                                    f"Already-fixed deadlock for direction "
                                    f"'{d.get('text', did)[:80]}' (id: {did}): "
                                    f"{_af_consec} consecutive research cycles produced tasks "
                                    f"that all resolved as SCOPE_EMPTY (the work was already "
                                    f"done on HEAD). Consider resetting the direction's scope "
                                    f"or retiring it if the goal is fully achieved."
                                ),
                                context={
                                    "source": "already_fixed_deadlock",
                                    "direction_id": did,
                                    "consecutive_noop_cycles": _af_consec,
                                },
                            )
                            session.add(_af_msg)
                            logger.warning(
                                f"[RESEARCH] Already-fixed deadlock: direction {did}, "
                                f"{_af_consec} consecutive scope-empty cycles (task {task.id})"
                            )

                    # Guard against duplicate appends on post-commit retry: only update
                    # recent_findings_evidence and outcome_progress_history if this is a
                    # new task_id (same guard as research_history above).
                    if entry.get("task_id") not in existing_task_ids:
                        _evicted_out: list = []
                        d["research_context"] = _build_research_context(
                            prior_context=d.get("research_context"),
                            finding_titles=finding_titles_by_dir.get(did, [])[:5],
                            key_finding_descriptions=finding_descriptions_by_dir.get(
                                did, []
                            )[:5],
                            finding_scopes=finding_scopes_by_dir.get(did, [])[:5],
                            finding_claims=finding_claims_by_dir.get(did, [])[:5],
                            finding_sources=finding_sources_by_dir.get(did, [])[:5],
                            cycle_insights=[i["summary"][:200] for i in insights[:3]],
                            history_entry=entry,
                            prior_history=d.get("research_history", [])[:-1],
                            evicted_out=_evicted_out,
                        )
                        _append_evicted_findings(d, _evicted_out)
                        prior_rfe = list(d.get("recent_findings_evidence") or [])
                        cur_rfe = build_recent_findings_evidence(
                            finding_titles=finding_titles_by_dir.get(did, []),
                            finding_claims=finding_claims_by_dir.get(did, []),
                            finding_sources=finding_sources_by_dir.get(did, []),
                        )
                        d["recent_findings_evidence"] = (prior_rfe + cur_rfe)[-5:]

                        # Record the per-cycle outcome-progress snapshot (bounded) so the
                        # advisory scaffolding detector can spot directions that keep
                        # merging PRs without advancing their stated success_evidence.
                        from app.strategic_completion import (
                            record_outcome_progress_snapshot,
                        )

                        record_outcome_progress_snapshot(d)

                    # Auto-complete if saturated and proven by merged PRs.
                    # Require >= 3 merged PRs for the PROVEN route (retires a
                    # productive saturated direction with a 'verified PRs merged'
                    # reason). When fewer than 3 are proven, fall back to the ADDITIVE
                    # exhaustion route: a genuinely-exhausted direction (saturated with
                    # no recent output) completes regardless of merge count under the
                    # code-failing / externally-frozen guardrails.
                    if d.get("status") == "active" and d.get("possibly_saturated"):
                        history = d.get("research_history", [])
                        agg = d.get("history_aggregate", {})
                        total_merged = sum(
                            credit_gate_merged_count(e) for e in history
                        ) + max(
                            0,
                            int(
                                agg.get(
                                    "total_verified_prs_merged",
                                    agg.get("total_prs_merged", 0),
                                )
                                or 0
                            )
                            - int(agg.get("total_stranded_prs_merged", 0) or 0),
                        )
                        # The PROVEN-merge SUCCESS route and the exhaustion
                        # route each fall back to their own merge-count /
                        # deadlock logic. The acceptance contract is enforced
                        # only at qualify (the ledger gate in compute_composite),
                        # so there is no pre-qualification coverage precondition
                        # on direction retirement here.
                        if total_merged < 3:
                            await _maybe_autocomplete_exhausted_direction(
                                session,
                                task,
                                d,
                                did,
                                _pending_archived_diagnoses,
                            )
                        elif total_merged >= 3:
                            total_cycles = len(history) + (agg.get("total_cycles") or 0)
                            d["status"] = "completed"
                            d["completed_at"] = _utcnow().isoformat()
                            d["auto_completed"] = True
                            d["auto_completed_reason"] = (
                                f"Auto-completed: direction saturated with {total_merged} "
                                f"verified PRs merged across {total_cycles} cycles"
                            )
                            # Capture diagnosis info for deferred design_doc write
                            # inside with_design_doc_locked (after the directions commit).
                            _pending_archived_diagnoses.append((dict(d), did))
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.INFO,
                                title="Direction auto-completed",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' was auto-completed: "
                                    f"saturated with {total_merged} verified PRs merged across {total_cycles} cycles."
                                ),
                            )
                            session.add(alert)
                            try:
                                from app.notification_dispatch import (
                                    post_direction_milestone_message,
                                )  # noqa: PLC0415

                                await post_direction_milestone_message(
                                    session,
                                    task.project_id,
                                    direction_text=d.get("text", ""),
                                    milestone="auto-completed",
                                    reason=d["auto_completed_reason"],
                                    direction_id=did,
                                )
                            except Exception as _notify_exc:
                                logger.warning(
                                    f"[NOTIFY] Failed to post direction milestone message: {_notify_exc}"
                                )

            # Also apply sub-research annotation directly on the in-memory
            # `directions` list before the locked write so both updates land
            # in a single atomic transaction.
            if (
                research_depth > 0
                and parent_research_task_id
                and direction_ids_with_findings
            ):
                for d in directions:
                    did = d.get("id")
                    if did not in direction_ids_with_findings:
                        continue
                    history = d.get("research_history", [])
                    if history:
                        last_entry = history[-1]
                        sub_notes = last_entry.get("sub_research_notes", [])
                        # Deduplicate by sub_task_id to prevent retried/replayed
                        # completions from appending the same note twice. Check against
                        # all notes in this entry and previous entries.
                        subtask_id = str(task.id)
                        all_subtask_ids = {note.get("sub_task_id") for note in sub_notes}
                        for prev_entry in history[:-1]:
                            all_subtask_ids.update(
                                note.get("sub_task_id")
                                for note in prev_entry.get("sub_research_notes", [])
                            )
                        if subtask_id not in all_subtask_ids:
                            sub_notes.append(
                                {
                                    "sub_task_id": subtask_id,
                                    "depth": research_depth,
                                    "tasks_created": len(child_tasks),
                                    "finding_titles": [
                                        f.get("title", "") for _, f, _ in deduplicated[:3]
                                    ],
                                    "insights": [
                                        i["summary"][:150] for i in insights[:2]
                                    ],  # up to 2 insights, capped
                                }
                            )
                        last_entry["sub_research_notes"] = sub_notes[-5:]  # keep last 5
                        history[-1] = last_entry
                        d["research_history"] = history

            # Serialize the directions write via row-lock so concurrent research
            # completions for different directions of the same project don't
            # clobber each other's entries via stale whole-array overwrites.
            _dir_mutations = {
                d["id"]: dict(d)
                for d in directions
                if d.get("id") in direction_ids_with_findings
            }
            session.expire(project, ["directions"])

            def _apply_main_directions(dirs: list) -> None:
                for fresh_d in dirs:
                    snap = _dir_mutations.get(fresh_d.get("id"))
                    if snap is not None:
                        # Remove keys deleted in the in-memory snapshot (e.g.
                        # deadlock_filtered_titles cleared on productive cycles).
                        for k in [k for k in list(fresh_d) if k not in snap]:
                            del fresh_d[k]
                        fresh_d.update(snap)

            await with_directions_locked(
                session, task.project_id, _apply_main_directions
            )
            await session.commit()

            # Serialize design_doc writes for archived diagnoses captured above.
            if _pending_archived_diagnoses:
                session.expire(project, ["design_doc"])

                def _apply_archived_diagnoses(doc: dict) -> None:
                    for _d_snap, _did in _pending_archived_diagnoses:
                        _append_archived_diagnosis(doc, _d_snap, _did)

                await with_design_doc_locked(
                    session, task.project_id, _apply_archived_diagnoses
                )
                await session.commit()

                # Reconcile problem-model gaps for the directions auto-completed
                # on this pass. Mirrors the strategic-completion _apply_gap_reconcile
                # pattern: prune each completed id from its gap's active_moves,
                # union into past_moves, and record a GapDeferral when the gap is
                # left unstaffed. Fail-open: a malformed/empty problem model must
                # never block the completion that already committed above.
                _completed_ids_for_reconcile = [
                    _did for _, _did in _pending_archived_diagnoses
                ]
                _reconcile_now_iso = _utcnow().isoformat()
                try:
                    from app.problem_model import (
                        reconcile_gaps_for_retired_directions,
                    )
                    from app.task_locking import with_problem_model_locked

                    def _apply_research_gap_reconcile(pm: dict) -> None:
                        reconcile_gaps_for_retired_directions(
                            pm, _completed_ids_for_reconcile, _reconcile_now_iso
                        )

                    await with_problem_model_locked(
                        session,
                        task.project_id,
                        _apply_research_gap_reconcile,
                    )
                    await session.commit()
                except Exception as _reconcile_exc:
                    logger.warning(
                        "[RESEARCH] Gap reconciliation failed for auto-completed "
                        "directions %s (non-fatal): %s",
                        _completed_ids_for_reconcile,
                        _reconcile_exc,
                    )

    # Process direction notes — append to the target direction's notes list
    if direction_notes:
        # Deferred import of strategic caps — shared with main.py
        from app.strategic_constants import (
            STRATEGIC_NOTES_HISTORY_MAX,
            STRATEGIC_RATIONALE_MAX,
        )

        _notes_to_apply: list[tuple[str, dict]] = []
        for note_finding in direction_notes:
            target_id = note_finding.get("direction_id")
            raw_note = (
                note_finding.get("description") or note_finding.get("prompt") or ""
            )
            if len(raw_note) > STRATEGIC_RATIONALE_MAX:
                logger.warning(
                    f"[RESEARCH] Task {task.id}: truncating direction note {len(raw_note)}→{STRATEGIC_RATIONALE_MAX} chars for direction {target_id}"
                )
            note_text = raw_note[:STRATEGIC_RATIONALE_MAX]
            if not note_text or not target_id:
                continue
            _notes_to_apply.append(
                (
                    target_id,
                    {
                        "text": note_text,
                        "added_at": _utcnow().isoformat(),
                        "added_by_task": str(task.id),
                    },
                )
            )

        if _notes_to_apply:
            _note_project = await session.get(Project, task.project_id)
            if _note_project:
                session.expire(_note_project, ["directions"])

                def _apply_notes(dirs: list) -> None:
                    for _target_id, _note_entry in _notes_to_apply:
                        for _d in dirs:
                            if _d.get("id") == _target_id:
                                _notes = list(_d.get("notes") or [])
                                # Dedup: only append if no existing note has the same text and added_by_task
                                _note_text = _note_entry.get("text", "")
                                _note_task = _note_entry.get("added_by_task")
                                if not any(
                                    n.get("text") == _note_text and n.get("added_by_task") == _note_task
                                    for n in _notes
                                ):
                                    _notes.append(_note_entry)
                                if len(_notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                    logger.warning(
                                        f"[RESEARCH] Task {task.id}: dropping {len(_notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {_target_id}"
                                    )
                                _d["notes"] = _notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                                break

                await with_directions_locked(session, task.project_id, _apply_notes)
                await session.commit()

    # Process direction proposals — add to Project.directions with status='proposed'
    proposals_created = 0
    if direction_proposals:
        # Deferred import to avoid circular dependency
        from app.strategic_completion import _is_direction_duplicate

        proposal_project = await session.get(Project, task.project_id)
        if proposal_project:
            # Use snapshot for dedup check (best-effort; write is locked below)
            proj_directions_snap = list(proposal_project.directions or [])
            rejected_proposals = [
                d for d in proj_directions_snap if d.get("status") == "rejected"
            ]
            pending_count = sum(
                1 for d in proj_directions_snap if d.get("status") == "proposed"
            )
            max_proposals = 10
            now_iso = _utcnow().isoformat()
            _new_proposals: list[dict] = []
            for proposal in direction_proposals[:5]:  # max 5 per research call
                if pending_count >= max_proposals:
                    logger.info(
                        f"[RESEARCH] Skipping direction proposal — max {max_proposals} pending proposals reached"
                    )
                    break
                proposal_text = (
                    proposal.get("prompt") or proposal.get("description") or ""
                )[:500]
                if not proposal_text.strip():
                    continue
                is_dup = _is_direction_duplicate(
                    proposal_text, proj_directions_snap, rejected_proposals
                )
                if is_dup:
                    logger.info(
                        "[RESEARCH] Skipping direction proposal — overlaps with existing direction"
                    )
                    continue
                new_direction = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": proposal_text,
                    "priority": proposal.get("priority", "medium")
                    if proposal.get("priority") in ("high", "medium", "low")
                    else "medium",
                    "paths": [],
                    "added_at": now_iso,
                    "status": "proposed",
                    "proposed_by": str(task.id),
                    "proposal_rationale": (proposal.get("description") or "")[:500],
                    "proposed_at": now_iso,
                }
                _new_proposals.append(new_direction)
                proj_directions_snap.append(new_direction)
                pending_count += 1
                proposals_created += 1
            if proposals_created:
                session.expire(proposal_project, ["directions"])

                def _apply_proposals(dirs: list) -> None:
                    existing_ids = {d.get("id") for d in dirs}
                    dirs.extend(
                        _np for _np in _new_proposals if _np["id"] not in existing_ids
                    )

                await with_directions_locked(session, task.project_id, _apply_proposals)
                await session.commit()
                logger.info(
                    f"[RESEARCH] Created {proposals_created} direction proposals from task {task.id}"
                )

    # Store a human-readable research summary artifact
    if child_tasks:
        direction_texts = []
        payload0 = task.first_payload
        if payload0.get("directions"):
            direction_texts = [
                payload0["directions"][0].get("text", "general improvement")
            ]
        summary_lines = [
            "# Research Summary",
            f"**Direction**: {direction_texts[0][:200] if direction_texts else 'general'}",
            f"**Tasks Created**: {len(child_tasks)}{_filter_str}",
            "",
            "## Findings:",
        ]
        for ct in child_tasks[:10]:
            p = ct.first_payload
            summary_lines.append(
                f"- [{p.get('type', '?')}] {ct.title}: {(ct.description or '')[:150]}"
            )
        summary = "\n".join(summary_lines)

        summary_artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.INVESTIGATION_REPORT,
            logs=summary,
        )
        session.add(summary_artifact)

    direction_id = (task.context or {}).get("direction_id", "?")
    status_msg = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + _filter_str
        + (
            f", {proposals_created} direction proposals pending review"
            if proposals_created
            else ""
        )
    )
    # Preserve capacity-degradation qualifier set by
    # _handle_research_completion's _summarize_dispatch_outcome. That call
    # runs before apply_post_spawn_state, but its result is overwritten when
    # this function sets latest_status_message below — re-read the marker from
    # context so users see "degraded: …" in the admin UI and task failures CLI.
    _degraded_reason = (task.context or {}).get("degraded_by_capacity_reason")
    if _degraded_reason:
        status_msg += f" (degraded: {_degraded_reason} — results may be incomplete)"

    # Snapshot plain values for the final log/broadcast below: the
    # duplicate-spawn catch around the terminal transition rolls the session
    # back, and rollback() expires every loaded ORM instance (even with
    # expire_on_commit=False) — a ``task.id`` / ``task.project_id`` read after
    # that would issue a sync lazy refresh and raise MissingGreenlet.
    _task_pk = task.id
    _task_id_str = str(_task_pk)
    _project_id = task.project_id
    _child_pks = [ct.id for ct in child_tasks]

    try:
        await _apply_terminal_transition(
            session,
            task,
            child_tasks=child_tasks,
            findings=findings,
            direction_notes=direction_notes,
            direction_proposals=direction_proposals,
            status_msg=status_msg,
            research_branch=research_branch,
            original_base=original_base,
            direction_id=direction_id,
            skipped_duplicates=skipped_duplicates,
            hash_dedup_count=hash_dedup_count,
            word_overlap_count=word_overlap_count,
            file_overlap_count=file_overlap_count,
            type_filtered_count=type_filtered_count,
            empty_prompt_count=empty_prompt_count,
            filtered_findings=filtered_findings,
        )
    except IntegrityError as _dup_exc:
        if "ux_task_active_idempotency_key" not in str(_dup_exc):
            raise
        # Benign duplicate-spawn guard (prod burst 2026-06-07 00:56): the
        # terminal→EVALUATING flip tried to re-enter the active-status window
        # of ``ux_task_active_idempotency_key`` while a peer task (a pipelined
        # duplicate research cycle, or a concurrent post-commit retry) already
        # holds this idempotency key in an active status. The constraint doing
        # its job is not a failure — roll back and leave the task in its
        # already-committed terminal state; the peer owns the direction's
        # active claim. Without this rollback the poisoned session cascaded
        # PendingRollbackError through the rest of post-commit and 500'd live
        # agents.
        await session.rollback()
        logger.warning(
            f"[RESEARCH] Task {_task_id_str}: post-spawn terminal transition hit "
            f"ux_task_active_idempotency_key (a peer already holds this "
            f"direction's active claim) — treating as already-spawned: "
            f"{str(_dup_exc)[:200]}"
        )
        # rollback() expired every loaded instance; the caller
        # (research_completion) keeps reading task/child attributes after this
        # returns, so re-load them while still inside the async greenlet
        # bridge (their rows are all committed — children before this call,
        # the task by the agent-status early commit). ``populate_existing``
        # (not refresh()) so the discarded EVALUATING mutation cannot survive
        # in-memory and re-flush; ``no_autoflush`` so the re-load itself cannot
        # autoflush that mutation and re-raise the violation.
        with contextlib.suppress(Exception), session.no_autoflush:
            await session.execute(
                select(Task)
                .where(Task.id.in_([_task_pk, *_child_pks]))
                .execution_options(populate_existing=True)
            )

    logger.info(
        f"[RESEARCH] Task {_task_id_str}: {len(findings)} raw findings, "
        f"{skipped_duplicates} deduped, {len(child_tasks)} tasks created "
        f"(direction: {str(direction_id)[:8]})"
    )
    await broadcast_to_project(
        str(_project_id),
        "autonomous_activity",
        {"research_task_id": _task_id_str, "tasks_created": len(child_tasks)},
    )


async def _apply_terminal_transition(
    session: AsyncSession,
    task: Task,
    *,
    child_tasks: List[Task],
    findings: List[Dict[str, Any]],
    direction_notes: List[Dict[str, Any]],
    direction_proposals: List[Dict[str, Any]],
    status_msg: str,
    research_branch: str,
    original_base: str,
    direction_id,
    skipped_duplicates: int,
    hash_dedup_count: int,
    word_overlap_count: int,
    file_overlap_count: int,
    type_filtered_count: int,
    empty_prompt_count: int,
    filtered_findings: List[Dict[str, Any]] | None = None,
) -> None:
    """Flip the research task to its post-spawn status and commit.

    Extracted from ``apply_post_spawn_state`` so the caller can wrap the
    whole transition (status mutation + zero-children note + commit) in the
    duplicate-spawn IntegrityError guard. The commits here can violate
    ``ux_task_active_idempotency_key`` when a peer already holds the task's
    idempotency key in an active status — the caller treats that as benign.
    """
    # Retry/dedup false-complete guard: ``child_tasks`` is only the children
    # spawned in THIS execution pass. A research task whose first attempt spawned
    # children but did not durably finish gets retried; on the retry it re-derives
    # the same findings, which now HASH-DEDUP against the children it already
    # spawned, leaving this-pass ``child_tasks`` empty. Taking the zero-children
    # COMPLETED branch here would orphan those live children: rollup machinery
    # (_check_and_trigger_rollup) early-returns unless the parent is EVALUATING,
    # so the research/<id> -> automain rollup PR never opens and all descendant
    # work is stranded on the research branch. Before treating this as a genuine
    # zero-children cycle, check for children created on a PRIOR pass.
    has_existing_children = False
    if not child_tasks:
        has_existing_children = bool(
            (
                await session.execute(
                    select(Task.id).where(Task.parent_task_id == task.id).limit(1)
                )
            )
            .scalars()
            .first()
        )
    if not child_tasks and not has_existing_children:
        # findings_documented: research produced only metadata findings (direction_notes,
        # direction_proposals, or design_doc_observations counted in type_filtered_count)
        # with no implementation children — record as a successful documentation outcome.
        _has_metadata = bool(
            direction_notes or direction_proposals or type_filtered_count > 0
        )
        _completion_outcome = "findings_documented" if _has_metadata else None

        task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            status_msg + " — auto-completed (no actionable tasks)"
        )
        task.context = {
            **(task.context or {}),
            "research_branch": research_branch,
            "original_base": original_base,
            **(
                {"completion_outcome": "findings_documented"}
                if _completion_outcome
                else {}
            ),
        }
        session.add(task)

        _note_parts = []
        if skipped_duplicates:
            _note_parts.append(
                f"{skipped_duplicates} duplicate(s) skipped"
                + (
                    f" ({hash_dedup_count} hash-match, {word_overlap_count} word-overlap,"
                    f" {file_overlap_count} file-overlap)"
                    if hash_dedup_count or word_overlap_count or file_overlap_count
                    else ""
                )
            )
        if type_filtered_count:
            _note_parts.append(
                f"{type_filtered_count} type-filtered (design_doc_observation/direction_update)"
            )
        if empty_prompt_count:
            _note_parts.append(f"{empty_prompt_count} had no prompt")
        _note_filter = (
            ("; ".join(_note_parts) + ".") if _note_parts else "not actionable."
        )
        # Surface individual filtered finding titles + reasons (up to 10) so
        # the strategist can distinguish correctly-filtered noise from findings
        # wrongly dropped — aggregate counts alone are not enough.
        _filtered_detail_parts = []
        if filtered_findings:
            _ff_list = list(filtered_findings)
            _cap = 10
            for i, ff in enumerate(_ff_list[:_cap]):
                _title = (ff.get("title") or "?")[:120]
                _reason = (ff.get("reason") or "")[:80]
                _filtered_detail_parts.append(f"'{_title}' ({_reason})")
            if len(_ff_list) > _cap:
                _filtered_detail_parts.append(f"and {len(_ff_list) - _cap} more")
            _detail_line = " Filtered: " + "; ".join(_filtered_detail_parts) + "."
        else:
            _detail_line = ""
        note = (
            f"Research cycle {task.id} found {len(findings)} findings but created 0 tasks: "
            + _note_filter
            + _detail_line
        )
        project = await session.get(Project, task.project_id)
        if project and str(direction_id) != "?" and direction_id != "crosscutting":
            _zero_note_entry = {
                "t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "msg": note,
                "added_by_task": str(task.id),
            }
            session.expire(project, ["directions"])

            def _apply_zero_note(dirs: list) -> None:
                for _d in dirs:
                    if _d.get("id") == direction_id:
                        _notes = list(_d.get("notes") or [])
                        # Dedup: only append if no existing note has the same msg and added_by_task
                        _note_msg = _zero_note_entry.get("msg")
                        _note_task = _zero_note_entry.get("added_by_task")
                        if not any(
                            n.get("msg") == _note_msg and n.get("added_by_task") == _note_task
                            for n in _notes
                        ):
                            _notes.append(_zero_note_entry)
                        _d["notes"] = _notes[-5:]
                        break

            await with_directions_locked(session, task.project_id, _apply_zero_note)
        elif project:
            msg = ProjectMessage(
                project_id=task.project_id,
                message_type=MessageType.NOTE,
                from_type="agent",
                from_task_id=task.id,
                content=note,
                context={
                    "source": "research_zero_children",
                    "direction_id": str(direction_id),
                },
            )
            session.add(msg)

        await session.commit()
        logger.info(
            f"[RESEARCH] Task {task.id}: 0 children created, auto-completed (direction: {str(direction_id)[:8]})"
        )
    else:
        task.status = TaskStatus.EVALUATING
        if not child_tasks and has_existing_children:
            # Routed here despite a zero-new-children pass because this research
            # already owns children from a prior attempt (all of this pass's
            # findings deduped against them). Do NOT emit the "created 0 tasks /
            # no actionable tasks" messaging — it would falsely claim the
            # research produced nothing. Rollup machinery owns the live children.
            task.latest_status_message = (
                f"Research re-derived already-spawned findings ({skipped_duplicates} "
                "deduped against existing children) — awaiting rollup of existing children"
            )
        else:
            task.latest_status_message = status_msg
        task.context = {
            **(task.context or {}),
            "research_branch": research_branch,
            "original_base": original_base,
        }
        session.add(task)
        await session.commit()
