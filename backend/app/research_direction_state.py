"""Post-spawn direction state management for research completion.

Extracted from app.research_completion to keep _handle_research_completion focused
on spawn-time concerns (dedup, scope calibration, child task creation). This module
holds the after-spawn logic that updates direction research_history, computes
saturation / dedup deadlock, auto-completes saturated directions, processes
direction notes and proposals, writes the summary artifact, and broadcasts the
completion event.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
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
from app.research_context import _build_research_context, build_recent_findings_evidence
from app.shared_state import DEFAULT_TASK_SCOPE
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


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
        1 for e in recent
        if (e.get("filter_breakdown", {}).get("hash_dedup", 0) or 0)
        + (e.get("filter_breakdown", {}).get("word_overlap_dedup", 0) or 0) > 0
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
    current_direction_id = (task.context or {}).get("direction_id") or (task.first_payload).get("current_direction_id")
    skipped_duplicates = hash_dedup_count + word_overlap_count + file_overlap_count + evidence_overlap_count
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
                        finding.get("title", f"Research finding #{orig_i+1}")
                    )
                    finding_descriptions_by_dir.setdefault(did, []).append(
                        (finding.get("description", finding.get("prompt", "")))[:200]
                    )
                    finding_scopes_by_dir.setdefault(did, []).append(
                        finding.get("scope", DEFAULT_TASK_SCOPE)
                    )
                    finding_claims_by_dir.setdefault(did, []).append(finding.get("claims") or [])
                    finding_sources_by_dir.setdefault(did, []).append(finding.get("sources") or [])
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
            for rec in (filtered_findings or []):
                did = rec.get("direction_id") or current_direction_id
                if not did:
                    continue
                reason = rec.get("reason", "")
                filtered_findings_by_dir.setdefault(did, []).append({
                    "title": rec.get("title", "")[:120],
                    "reason": reason[:80],
                })
                if reason.startswith("hash match"):
                    hash_dedup_by_dir[did] = hash_dedup_by_dir.get(did, 0) + 1
                elif reason.startswith("word overlap"):
                    word_overlap_by_dir[did] = word_overlap_by_dir.get(did, 0) + 1
                elif reason.startswith("all files covered"):
                    file_overlap_by_dir[did] = file_overlap_by_dir.get(did, 0) + 1
                elif reason.startswith("evidence overlap"):
                    evidence_overlap_by_dir[did] = evidence_overlap_by_dir.get(did, 0) + 1

            # Count raw actionable findings per direction so each direction's
            # history entry records only its own findings_produced instead of
            # the global total. Applies the same parent-direction fallback used
            # for filtered_findings bucketing.
            findings_produced_by_dir: dict = {}
            grade_dist_by_dir: dict = {}
            for _f in findings:
                _did = _f.get("direction_id") or current_direction_id
                if _did:
                    findings_produced_by_dir[_did] = findings_produced_by_dir.get(_did, 0) + 1
                    _g = _f.get("actionability_grade", "MEDIUM")
                    if _g not in ("HIGH", "MEDIUM", "LOW"):
                        _g = "MEDIUM"
                    _gd = grade_dist_by_dir.setdefault(_did, {"HIGH": 0, "MEDIUM": 0, "LOW": 0})
                    _gd[_g] = _gd.get(_g, 0) + 1
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [ct for ct in child_tasks if (ct.context or {}).get("direction_id") == did]
                    # Compact history entry — just counters and a short summary.
                    # Full details available via task API using task_id.
                    titles = finding_titles_by_dir.get(did, [])[:3]
                    descriptions = finding_descriptions_by_dir.get(did, [])[:3]
                    _dir_grade_dist = grade_dist_by_dir.get(did)
                    entry = {
                        "timestamp": now_iso,
                        "task_id": str(task.id),
                        "tasks_created": len(dir_child_tasks),
                        "findings_produced": findings_produced_by_dir.get(did, 0),
                        "batch_cap_applied": batch_cap_applied_count,
                        "summary": "; ".join(t[:60] for t in titles) if titles else "no findings",
                        "finding_titles": titles,
                        "key_finding_descriptions": descriptions,
                        **({"grade_distribution": _dir_grade_dist} if _dir_grade_dist else {}),
                        "filter_breakdown": {
                            "hash_dedup": hash_dedup_by_dir.get(did, 0),
                            "word_overlap_dedup": word_overlap_by_dir.get(did, 0),
                            "file_overlap_dedup": file_overlap_by_dir.get(did, 0),
                            "evidence_overlap_dedup": evidence_overlap_by_dir.get(did, 0),
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

                    # Deadlock detection: 3+ consecutive 0-task cycles driven by dedup
                    _deadlocked, _consec = _detect_dedup_deadlock(d["research_history"])
                    if _deadlocked and d.get("status") == "active":
                        _blocked_titles = [
                            f.get("title", "")[:60]
                            for f in (task.plan_output or [])
                            if f.get("type") not in (
                                "direction_proposal", "direction_note",
                                "design_doc_observation", "direction_update",
                            )
                            and f.get("title")
                        ][:3]
                        # Guard: only create alert if no undismissed alert exists for this direction
                        _existing_deadlock_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.title == "Dedup deadlock detected",
                                Alert.dismissed_at.is_(None),
                                Alert.message.contains(did),
                            )
                        )).scalars().first()
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
                                        if _blocked_titles else ""
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
                                        if _blocked_titles else ""
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

                    d["research_context"] = _build_research_context(
                        prior_context=d.get("research_context"),
                        finding_titles=finding_titles_by_dir.get(did, [])[:5],
                        key_finding_descriptions=finding_descriptions_by_dir.get(did, [])[:5],
                        finding_scopes=finding_scopes_by_dir.get(did, [])[:5],
                        finding_claims=finding_claims_by_dir.get(did, [])[:5],
                        finding_sources=finding_sources_by_dir.get(did, [])[:5],
                        cycle_insights=[i["summary"][:200] for i in insights[:3]],
                        history_entry=entry,
                        prior_history=d.get("research_history", [])[:-1],
                    )
                    prior_rfe = list(d.get("recent_findings_evidence") or [])
                    cur_rfe = build_recent_findings_evidence(
                        finding_titles=finding_titles_by_dir.get(did, []),
                        finding_claims=finding_claims_by_dir.get(did, []),
                        finding_sources=finding_sources_by_dir.get(did, []),
                    )
                    d["recent_findings_evidence"] = (prior_rfe + cur_rfe)[-5:]

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
                            try:
                                from app.notification_dispatch import post_direction_milestone_message  # noqa: PLC0415
                                await post_direction_milestone_message(
                                    session, task.project_id,
                                    direction_text=d.get("text", ""),
                                    milestone="auto-completed",
                                    reason=d["auto_completed_reason"],
                                    direction_id=did,
                                )
                            except Exception as _notify_exc:
                                logger.warning(f"[NOTIFY] Failed to post direction milestone message: {_notify_exc}")

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
        from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
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
        # Deferred import to avoid circular dependency
        from app.strategic_completion import _is_direction_duplicate
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
                    logger.info("[RESEARCH] Skipping direction proposal — overlaps with existing direction")
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
            '# Research Summary',
            f'**Direction**: {direction_texts[0][:200] if direction_texts else "general"}',
            f'**Tasks Created**: {len(child_tasks)}{_filter_str}',
            '',
            '## Findings:',
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

    direction_id = (task.context or {}).get("direction_id", "?")
    status_msg = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + _filter_str
        + (f", {proposals_created} direction proposals pending review" if proposals_created else "")
    )

    if not child_tasks:
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = status_msg + " — auto-completed (no actionable tasks)"
        task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
        session.add(task)

        _note_parts = []
        if skipped_duplicates:
            _note_parts.append(f"{skipped_duplicates} duplicate(s) skipped"
                               + (f" ({hash_dedup_count} hash-match, {word_overlap_count} word-overlap,"
                                  f" {file_overlap_count} file-overlap)"
                                  if hash_dedup_count or word_overlap_count or file_overlap_count else ""))
        if type_filtered_count:
            _note_parts.append(f"{type_filtered_count} type-filtered (design_doc_observation/direction_update)")
        if empty_prompt_count:
            _note_parts.append(f"{empty_prompt_count} had no prompt")
        _note_filter = ("; ".join(_note_parts) + ".") if _note_parts else "not actionable."
        note = (
            f"Research cycle {task.id} found {len(findings)} findings but created 0 tasks: "
            + _note_filter
        )
        project = await session.get(Project, task.project_id)
        if project and str(direction_id) != "?" and direction_id != "crosscutting":
            directions = list(project.directions or [])
            for d in directions:
                if d.get("id") == direction_id:
                    d.setdefault("notes", [])
                    d["notes"] = (d["notes"] or [])[-4:] + [{
                        "t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        "msg": note,
                    }]
                    break
            project.directions = directions
            flag_modified(project, "directions")
            session.add(project)
        elif project:
            msg = ProjectMessage(
                project_id=task.project_id,
                message_type=MessageType.NOTE,
                from_type="agent",
                from_task_id=task.id,
                content=note,
                context={"source": "research_zero_children", "direction_id": str(direction_id)},
            )
            session.add(msg)

        await session.commit()
        logger.info(f"[RESEARCH] Task {task.id}: 0 children created, auto-completed (direction: {str(direction_id)[:8]})")
    else:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = status_msg
        task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
        session.add(task)
        await session.commit()

    logger.info(
        f"[RESEARCH] Task {task.id}: {len(findings)} raw findings, "
        f"{skipped_duplicates} deduped, {len(child_tasks)} tasks created "
        f"(direction: {str(direction_id)[:8]})"
    )
    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})
