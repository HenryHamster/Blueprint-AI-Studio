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
from app.direction_metrics import _fold_evicted_into_aggregate
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
            (source.get("agreement_distribution") or {}).get(key) or 0
        )
    for key in _CHALLENGE_BOUNDARY_BUCKETS:
        target["boundary_distribution"][key] += (
            (source.get("boundary_distribution") or {}).get(key) or 0
        )


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
    it to doc['retired_diagnoses_archive']. Maintains FIFO list capped at
    _ARCHIVED_DIAGNOSES_CAP entries, each truncated to _DIAGNOSIS_MAX_CHARS.

    Operates on the doc dict in place so it can be called inside
    with_design_doc_locked (which passes the freshly-locked dict).
    """
    research_context = direction.get("research_context", "")
    diagnosis = extract_diagnosis_from_context(research_context)
    if not diagnosis or not diagnosis.strip():
        return
    diagnosis = diagnosis.strip()[:_DIAGNOSIS_MAX_CHARS]
    archive = list(doc.get("retired_diagnoses_archive") or [])
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
    """Return True if last 3 cycles all produced 0 or 1 tasks."""
    if len(history) < 3:
        return False
    recent = history[-3:]
    return all(entry.get("tasks_created", 0) <= 1 for entry in recent)


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
                elif reason.startswith("word overlap"):
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
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [
                        ct
                        for ct in child_tasks
                        if (ct.context or {}).get("direction_id") == did
                    ]
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
                        "tasks_created": len(dir_child_tasks),
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
                            "type_filtered": type_filtered_count,
                            "empty_prompt": empty_prompt_count,
                        },
                    }
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
                    new_history = history + [entry]
                    if len(new_history) > 10:
                        d["history_aggregate"] = _fold_evicted_into_aggregate(
                            d.get("history_aggregate"), new_history[:-10]
                        )
                    d["research_history"] = new_history[-10:]
                    d["possibly_saturated"] = _compute_saturation(d["research_history"])
                    # Saturated directions get a longer refractory so the portfolio
                    # is not immediately re-queued on a direction that has no work left.
                    if d.get("possibly_saturated"):
                        _sat_refractory_min = direction_saturation_refractory_minutes()
                        if isinstance(_sat_refractory_min, (int, float)) and _sat_refractory_min > 0:
                            d["refractory_until"] = (
                                _utcnow() + timedelta(minutes=float(_sat_refractory_min))
                            ).isoformat()

                    # Deadlock detection: 3+ consecutive 0-task cycles driven by dedup
                    _deadlocked, _consec = _detect_dedup_deadlock(d["research_history"])
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
                        _existing_deadlock_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.title == "Dedup deadlock detected",
                                        Alert.dismissed_at.is_(None),
                                        Alert.message.contains(did),
                                    )
                                )
                            )
                            .scalars()
                            .first()
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

                    # Auto-complete direction if saturated with sufficient merged PRs
                    if (
                        d.get("status") == "active"
                        and d.get("possibly_saturated")
                        and entry.get("tasks_created", 0) > 0
                    ):
                        history = d.get("research_history", [])
                        agg = d.get("history_aggregate", {})
                        total_merged = sum(e.get("prs_merged", 0) for e in history) + (
                            agg.get("total_prs_merged") or 0
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
                            # Capture diagnosis info for deferred design_doc write
                            # inside with_design_doc_locked (after the directions commit).
                            _pending_archived_diagnoses.append((dict(d), did))
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
                        sub_notes.append(
                            {
                                "sub_task_id": str(task.id),
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
        note = (
            f"Research cycle {task.id} found {len(findings)} findings but created 0 tasks: "
            + _note_filter
        )
        project = await session.get(Project, task.project_id)
        if project and str(direction_id) != "?" and direction_id != "crosscutting":
            _zero_note_entry = {
                "t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "msg": note,
            }
            session.expire(project, ["directions"])

            def _apply_zero_note(dirs: list) -> None:
                for _d in dirs:
                    if _d.get("id") == direction_id:
                        _d.setdefault("notes", [])
                        _d["notes"] = (_d["notes"] or [])[-4:] + [_zero_note_entry]
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
