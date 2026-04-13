"""Strategic completion handler and direction health checks.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains direction health/completion checks, duplicate detection,
and the _handle_strategic_completion handler.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, ProjectMessage, MessageType, Task, TaskStatus
from app.research_completion import _update_cycle_history, _compute_saturation
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _record_strategic_task_failure(task: Task, session: AsyncSession) -> None:
    """Record a FAILED strategic task in the circuit breaker and cycle history.

    Called from _process_post_commit when a design_doc_update or strategic_review
    task terminates with FAILED status (VM crash, timeout, etc.).
    """
    project = await session.get(Project, task.project_id)
    if not project:
        return
    vision = dict(project.vision or {})
    failures = vision.get("_strategic_failures", 0) + 1
    vision["_strategic_failures"] = failures
    vision["_strategic_failures_at"] = _utcnow().isoformat()
    project.vision = vision
    flag_modified(project, "vision")
    cycle_id = (task.context or {}).get("strategic_cycle_id")
    if cycle_id:
        task_type = task.first_payload.get("type", "")
        failed_phase = "phase1" if task_type == "design_doc_update" else "phase2"
        _update_cycle_history(project, cycle_id, {
            f"{failed_phase}_completed_at": _utcnow().isoformat(),
            "outcome": "failed",
            "failed_phase": failed_phase,
            "error": (task.latest_status_message or "task failed")[:200],
        })
    session.add(project)
    logger.warning(
        f"[STRATEGIC] {task.first_payload.get('type')} task {task.id} FAILED "
        f"(failure #{failures}): {(task.latest_status_message or '')[:120]}"
    )


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
        # Auto-derive system statuses from direction outcomes
        # Agents often leave everything as "planned" — infer from actual merge/direction data
        vision_systems = new_vision.get("systems")
        if isinstance(vision_systems, list):
            directions_list = project.directions or []
            for sys in vision_systems:
                if not isinstance(sys, dict) or not sys.get("id"):
                    continue
                sys_id = sys["id"]
                # Count directions mapped to this system and their merged PRs
                mapped_dirs = [d for d in directions_list if d.get("system_id") == sys_id]
                total_merged = 0
                for d in mapped_dirs:
                    for h in d.get("research_history", []):
                        total_merged += h.get("prs_merged", 0)
                    agg = d.get("history_aggregate", {})
                    total_merged += agg.get("total_prs_merged", 0)
                # Only upgrade status, never downgrade what the agent set
                agent_status = sys.get("status", "planned")
                STATUS_RANK = {"planned": 0, "in_progress": 1, "functional": 2, "mature": 3}
                agent_rank = STATUS_RANK.get(agent_status, 0)
                if total_merged >= 5 and agent_rank < 2:
                    sys["status"] = "functional"
                elif (total_merged > 0 or len(mapped_dirs) > 0) and agent_rank < 1:
                    sys["status"] = "in_progress"
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
        from app.strategic_constants import (  # noqa: PLC0415
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
                if op_entry.get("source"):
                    new_dir["source"] = str(op_entry["source"])[:64]
                if op_entry.get("paths"):
                    new_dir["paths"] = op_entry["paths"]
                raw_category = op_entry.get("category")
                if raw_category in ("product", "structural", "mixed"):
                    new_dir["category"] = raw_category
                if op_entry.get("system_id"):
                    sid = str(op_entry["system_id"])
                    if len(sid) > STRATEGIC_SYSTEM_ID_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                    new_dir["system_id"] = sid[:STRATEGIC_SYSTEM_ID_MAX]
                current_active = sum(1 for d in directions if d.get('status') == 'active')
                if current_active >= _runtime_config.get('max_active_directions', 5):
                    logger.warning(f'[STRATEGIC] Skipping direction create: active count {current_active} >= cap')
                    continue
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
                        if "category" in changes:
                            if changes["category"] in ("product", "structural", "mixed"):
                                d["category"] = changes["category"]
                            elif changes["category"] is None:
                                d.pop("category", None)
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
    from app.strategic_constants import STRATEGIC_MESSAGE_CONTENT_MAX, STRATEGIC_RESOLUTION_MAX  # noqa: PLC0415

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


def _build_outcome_evaluation(project) -> dict:
    """Build a planned-vs-actual assessment for the strategic agent.

    Compares what the vision predicted against what directions actually produced.
    All data lives on project.directions and project.vision — no DB queries needed.
    """
    from app.research_completion import _parse_research_context  # noqa: PLC0415
    vision = project.vision or {}
    directions = project.directions or []
    now_iso = _utcnow().isoformat()

    # Determine evaluation window: since last vision update, or all time
    window_from = vision.get("last_updated")
    vision_rev = vision.get("revision", 0)

    # Per-direction outcomes
    direction_outcomes = []
    for d in directions:
        status = d.get("status", "active")
        if status not in ("active", "expired", "paused"):
            continue
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})

        # Filter history to evaluation window
        if window_from:
            window_history = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            window_history = list(history)

        cycles = len(window_history)
        merged = sum(h.get("prs_merged", 0) for h in window_history)
        failed = sum(h.get("tasks_failed", 0) for h in window_history)

        # Extract confirmed vs proposed findings from research_context
        confirmed = []
        proposed = []
        failed_approaches = []
        ctx = d.get("research_context", "")
        if ctx:
            parsed = _parse_research_context(ctx)
            for item in parsed.get("established", []):
                if "(confirmed)" in item:
                    confirmed.append(item.replace("(confirmed)", "").strip())
                elif "(proposed)" in item:
                    proposed.append(item.replace("(proposed)", "").strip())
                else:
                    confirmed.append(item)  # Unmarked items are treated as confirmed
            failed_approaches = parsed.get("failed", [])

        outcome: dict = {
            "id": d.get("id"),
            "text": (d.get("text") or "")[:200],
            "system_id": d.get("system_id"),
            "status": status,
            "cycles": cycles,
            "merged": merged,
            "failed": failed,
        }
        if confirmed:
            outcome["confirmed"] = confirmed[:15]
        if proposed:
            outcome["proposed"] = proposed[:10]
        if failed_approaches:
            outcome["failed_approaches"] = failed_approaches[:10]
        # Include lifetime totals for context
        lifetime_cycles = len(history) + (agg.get("total_cycles") or 0)
        lifetime_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
        if lifetime_cycles > cycles:
            outcome["lifetime"] = {"cycles": lifetime_cycles, "merged": lifetime_merged}

        # Aggregate type distribution across window cycles
        type_dist: dict = {}
        product_weighted = 0.0
        for cycle_entry in window_history:
            for t, count in cycle_entry.get("type_distribution", {}).items():
                type_dist[t] = type_dist.get(t, 0) + count
            tasks_in_cycle = cycle_entry.get("tasks_created", 0)
            product_weighted += cycle_entry.get("product_ratio", 0.0) * tasks_in_cycle
        total_typed = sum(type_dist.values())
        if type_dist:
            outcome["type_distribution"] = type_dist
            outcome["product_ratio"] = round(product_weighted / total_typed, 2) if total_typed > 0 else None

        direction_outcomes.append(outcome)

    # Overall health
    total_cycles = sum(d["cycles"] for d in direction_outcomes)
    total_merged = sum(d["merged"] for d in direction_outcomes)
    total_failed = sum(d["failed"] for d in direction_outcomes)
    success_rate = round(total_merged / max(total_merged + total_failed, 1), 2)

    # Trend: compare first half vs second half of all window history entries
    all_window_entries = []
    for d in directions:
        history = d.get("research_history", [])
        if window_from:
            entries = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            entries = list(history)
        all_window_entries.extend(entries)
    all_window_entries.sort(key=lambda h: h.get("timestamp") or "")
    trend = "stable"
    if len(all_window_entries) >= 4:
        mid = len(all_window_entries) // 2
        first_half = all_window_entries[:mid]
        second_half = all_window_entries[mid:]
        first_rate = sum(h.get("prs_merged", 0) for h in first_half) / max(len(first_half), 1)
        second_rate = sum(h.get("prs_merged", 0) for h in second_half) / max(len(second_half), 1)
        if second_rate > first_rate * 1.3:
            trend = "improving"
        elif second_rate < first_rate * 0.7:
            trend = "declining"

    # System-level status — cross-reference vision systems with direction outcomes
    systems = []
    vision_systems = vision.get("systems", [])
    if isinstance(vision_systems, list):
        for sys in vision_systems:
            if not isinstance(sys, dict):
                continue
            sys_id = sys.get("id")
            if not sys_id:
                continue
            # Find directions mapped to this system
            mapped = [d for d in direction_outcomes if d.get("system_id") == sys_id]
            sys_merged = sum(d["merged"] for d in mapped)
            sys_failed = sum(d["failed"] for d in mapped)
            systems.append({
                "id": sys_id,
                "name": sys.get("name", ""),
                "planned_status": sys.get("status", "unknown"),
                "directions_count": len(mapped),
                "merged": sys_merged,
                "failed": sys_failed,
            })

    # Overall type distribution across all directions
    overall_type_dist: dict = {}
    overall_product_weighted = 0.0
    for d_out in direction_outcomes:
        for t, count in d_out.get("type_distribution", {}).items():
            overall_type_dist[t] = overall_type_dist.get(t, 0) + count
        tasks_count = sum(d_out.get("type_distribution", {}).values())
        if d_out.get("product_ratio") is not None:
            overall_product_weighted += d_out["product_ratio"] * tasks_count
    overall_total_typed = sum(overall_type_dist.values())
    overall_product_ratio = round(overall_product_weighted / overall_total_typed, 2) if overall_total_typed > 0 else None

    overall_summary: dict = {
        "cycles": total_cycles, "merged": total_merged, "failed": total_failed,
        "success_rate": success_rate, "trend": trend,
    }
    if overall_type_dist:
        overall_summary["type_distribution"] = overall_type_dist
    if overall_product_ratio is not None:
        overall_summary["product_ratio"] = overall_product_ratio

    result: dict = {
        "window": {"from": window_from or "all_time", "to": now_iso, "vision_revision": vision_rev},
        "overall": overall_summary,
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    return result
