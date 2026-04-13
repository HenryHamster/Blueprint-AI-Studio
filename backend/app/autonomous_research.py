"""Autonomous research spawning functions.

This module handles all aspects of autonomous research task spawning:
- Research task selection and rate limiting
- Strategic cycle management (design doc update → strategic review)
- Periodic task scheduling (security_audit, qa, deps)
- Post-merge and idle research triggers
- Direction health, auto-pause, and auto-recovery
"""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Task, Project, TaskHistory, TaskStatus, ProjectMessage,
)
from app.database import AsyncSessionLocal
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.max_plan_pool import _max_plan_pools, _account_available_for_model

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _compute_merge_rate(d: dict) -> float:
    """Return merged-PRs-per-cycle for a direction. Returns 0.5 (neutral) if never researched."""
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    if total_cycles == 0:
        return 0.5  # unknown, neutral
    return total_merged / total_cycles


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

    result: dict = {
        "window": {"from": window_from or "all_time", "to": now_iso, "vision_revision": vision_rev},
        "overall": {
            "cycles": total_cycles, "merged": total_merged, "failed": total_failed,
            "success_rate": success_rate, "trend": trend,
        },
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    return result


async def _spawn_design_doc_update(project: Project, session: AsyncSession):
    """Spawn a design doc update task (Phase 1 of strategic cycle).

    This runs as a separate task type so it doesn't block tactical research.
    On completion, it chains to _spawn_strategic_review (Phase 2).
    """
    from app.task_lifecycle import _has_active_strategic_task  # noqa: PLC0415
    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.debug(f"[STRATEGIC] Skipping design doc update — strategic task already active for {project.id}")
        return

    doc_model = _runtime_config.get("task_type_models", {}).get("design_doc_update") or \
                _runtime_config.get("task_type_models", {}).get("research") or None
    doc_effort = _runtime_config.get("task_type_efforts", {}).get("design_doc_update") or \
                 _runtime_config.get("task_type_efforts", {}).get("research") or None

    # Fetch open messages for the design doc agent
    open_msgs_result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
        ).order_by(ProjectMessage.created_at.asc()).limit(50)
    )
    open_msgs = [
        {"id": str(m.id), "type": m.message_type, "content": m.content,
         "context": m.context, "from": m.from_type,
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in open_msgs_result.scalars().all()
    ]

    payload: dict = {
        "type": "design_doc_update",
        "design_doc": project.design_doc or {},
        "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
        "project_name": project.name,
        "vision": project.vision or {},
        "open_messages": open_msgs,
        "base_branch": "main",
        **({"model": doc_model} if doc_model else {}),
        **({"effort": doc_effort} if doc_effort else {}),
    }
    cycle_id = "sc-" + uuid.uuid4().hex[:8]
    new_task = Task(
        project_id=project.id,
        title=f"Architecture review: {project.name}",
        description="Update project design document",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={"strategic_cycle_id": cycle_id},
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "design_doc_update"},
    )
    session.add(history)
    # Record cycle start in vision history (keep last 10 entries)
    vision = dict(project.vision or {})
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    cycle_history.append({
        "cycle_id": cycle_id,
        "started_at": _utcnow().isoformat(),
        "phase1_task_id": str(new_task.id),
        "phase1_completed_at": None,
        "phase2_task_id": None,
        "phase2_completed_at": None,
        "directions_created": 0,
        "directions_retired": 0,
        "vision_updated": False,
        "outcome": "in_progress",
    })
    vision["_strategic_cycle_history"] = cycle_history[-10:]
    project.vision = vision
    flag_modified(project, "vision")
    session.add(project)
    await session.commit()
    logger.info(f"[STRATEGIC] Spawned design_doc_update task {new_task.id} for project {project.id} (cycle {cycle_id})")


async def _spawn_strategic_review(project: Project, session: AsyncSession, cycle_id: Optional[str] = None):
    """Spawn Phase 2 of the strategic cycle: vision + direction review.

    Called by _handle_design_doc_completion after the design doc is updated.
    Uses type 'strategic_review' so it doesn't block tactical research.
    """
    from app.task_lifecycle import _has_active_strategic_task  # noqa: PLC0415
    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.debug(f"[STRATEGIC] Skipping strategic review — strategic task already active for {project.id}")
        return

    # Build direction stats with rich data (same as previous strategic payload)
    all_active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    direction_stats = []
    for d in all_active_directions:
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})
        total_cycles = len(history) + (agg.get("total_cycles") or 0)
        total_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
        total_failed = sum(h.get("tasks_failed", 0) for h in history) + (agg.get("total_tasks_failed") or 0)
        recent_history = history[-5:]
        recent_findings = []
        recent_failures = []
        for h in recent_history:
            for desc in (h.get("key_finding_descriptions") or []):
                if desc and desc not in recent_findings:
                    recent_findings.append(desc)
            for fd in (h.get("failed_task_details") or []):
                if fd:
                    recent_failures.append(fd)
        stat_entry: dict = {
            "id": d.get("id"), "text": d.get("text"), "priority": d.get("priority"),
            "status": d.get("status"), "total_cycles": total_cycles,
            "prs_merged": total_merged, "tasks_failed": total_failed,
            "possibly_saturated": d.get("possibly_saturated", False),
        }
        ctx = d.get("research_context")
        if ctx:
            stat_entry["research_context"] = ctx[:3000]
        if recent_findings:
            stat_entry["recent_findings"] = recent_findings[:10]
        if recent_failures:
            stat_entry["recent_failures"] = recent_failures[:5]
        if d.get("system_id"):
            stat_entry["system_id"] = d["system_id"]
        direction_stats.append(stat_entry)

    # Fetch open messages
    open_msgs_result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
        ).order_by(ProjectMessage.created_at.asc()).limit(50)
    )
    open_msgs = [
        {"id": str(m.id), "type": m.message_type, "content": m.content,
         "context": m.context, "from": m.from_type,
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in open_msgs_result.scalars().all()
    ]

    review_model = _runtime_config.get("task_type_models", {}).get("strategic_review") or \
                   _runtime_config.get("task_type_models", {}).get("research") or None
    review_effort = _runtime_config.get("task_type_efforts", {}).get("strategic_review") or \
                    _runtime_config.get("task_type_efforts", {}).get("research") or None

    payload: dict = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "design_doc": project.design_doc or {},
        "vision": project.vision or {},
        "directions_summary": direction_stats,
        "outcome_evaluation": _build_outcome_evaluation(project),
        "project_name": project.name,
        "open_messages": open_msgs,
        "base_branch": "main",
        **({"model": review_model} if review_model else {}),
        **({"effort": review_effort} if review_effort else {}),
    }
    new_task = Task(
        project_id=project.id,
        title=f"Strategic review: {project.name}",
        description="Vision and direction review",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={"strategic_cycle_id": cycle_id} if cycle_id else {},
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "strategic_review"},
    )
    session.add(history)
    if cycle_id:
        # Record phase1 completion and phase2 task id in cycle history
        vision = dict(project.vision or {})
        cycle_history = list(vision.get("_strategic_cycle_history") or [])
        for entry in cycle_history:
            if entry.get("cycle_id") == cycle_id:
                entry["phase1_completed_at"] = _utcnow().isoformat()
                entry["phase2_task_id"] = str(new_task.id)
                break
        vision["_strategic_cycle_history"] = cycle_history
        project.vision = vision
        flag_modified(project, "vision")
        session.add(project)
    await session.commit()
    logger.info(f"[STRATEGIC] Spawned strategic_review task {new_task.id} for project {project.id}")


async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met."""
    from app.main import _maybe_create_circuit_breaker_alert  # noqa: PLC0415
    from app.task_lifecycle import _increment_research_spawn_count  # noqa: PLC0415
    from app.strategic_completion import _check_direction_health, _check_direction_completion  # noqa: PLC0415
    from app.research_completion import _compute_momentum_factor  # noqa: PLC0415
    from app.anomaly_detection import _compute_research_anomalies  # noqa: PLC0415
    # Guard: autonomous must be enabled
    if not project.autonomous_enabled:
        return

    # Playbook auto-seed removed: the hardcoded seed entries were specific to the
    # Blueprint pipeline repo and polluted new projects with wrong build/test
    # instructions. Agents discover the correct playbook entries by exploring
    # the actual codebase and writing them via `./playbook set`.
    # Guard: don't spawn if user has no available accounts (avoids immediate failure)
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(_account_available_for_model(a, research_model) for a in user_pool) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        logger.info(f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts")
        return
    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        # Check if cooldown has elapsed → transition to HALF-OPEN
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        # Check if existing test task is stuck — treat as failure after 30 min
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
            elif not test_task:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} no longer exists (deleted/reaped) → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task deleted/reaped — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
    # Guard: must have active directions that aren't in cooldown or mid-cycle
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    now = _utcnow()

    # Build set of direction IDs that already have an active research cycle.
    # Only count tasks where the agent is still working (not EVALUATING, where
    # the agent is done and we're just waiting for children to merge).
    busy_direction_ids: set = set()
    active_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS,
            ]),
        )
    )).scalars().all()
    for t in active_research:
        payload = t.first_payload
        if payload.get("type") == "research":
            # current_direction_id identifies the targeted direction; null means
            # undirected/cross-cutting analysis which doesn't block any direction.
            current_did = payload.get("current_direction_id")
            if current_did:
                busy_direction_ids.add(current_did)

    directions_modified = False
    # Prune dead directions (expired/completed/rejected) to prevent JSON bloat
    live_dirs = [d for d in (project.directions or []) if d.get("status") in ("active", "proposed", "paused")]
    if len(live_dirs) < len(project.directions or []):
        logger.info(f"[AUTONOMOUS] Pruned {len(project.directions or []) - len(live_dirs)} dead directions from {project.id}")
        project.directions = live_dirs
        flag_modified(project, "directions")
        directions_modified = True
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
        last_researched = d.get("last_researched_at")
        if last_researched:
            minutes_ago = (now - datetime.fromisoformat(last_researched)).total_seconds() / 60
            effective_cooldown = cooldown_minutes
            if d.get("possibly_saturated"):
                effective_cooldown = cooldown_minutes * 2.0
                logger.debug(
                    f"[RESEARCH] Direction {did} saturated — applying 2x cooldown ({cooldown_minutes*2}m)"
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
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            auto_paused_any = True
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
    # Allow strategic cycles to proceed even without active directions (they create directions).
    # Stop forcing after 3 consecutive strategic failures to prevent infinite loops.
    vision_data = project.vision or {}
    strategic_failures = vision_data.get("_strategic_failures", 0)
    # Auto-decay: if last failure was >48h ago, reset counter for recovery purposes
    if strategic_failures >= 3:
        failures_at = vision_data.get("_strategic_failures_at")
        if failures_at:
            try:
                last_failure = datetime.fromisoformat(failures_at.replace('Z', '+00:00'))
                if (_utcnow() - last_failure).total_seconds() > 48 * 3600:
                    strategic_failures = 0
                    logger.info(f"[STRATEGIC] Auto-decayed _strategic_failures for project {project.id} (last failure: {failures_at})")
            except (ValueError, TypeError):
                pass  # malformed timestamp, keep current value
    # Check if ANY direction is active in the project (ignoring cooldown filter).
    # active_directions only contains non-cooldown directions, so a project with
    # active-but-cooling directions should not trigger a strategic cycle.
    any_active_in_project = any(d.get("status") == "active" for d in (project.directions or []))
    force_strategic = strategic_failures < 3 and (
        not vision_data.get("purpose") or not any_active_in_project
    )
    if not active_directions and not force_strategic:
        return
    # Pick up to N non-conflicting directions for parallel research.
    # active_directions preserves UI drag-order (index 0 = highest priority).
    # Build vision priority lookup: directions tagged with a system_id that appears
    # earlier in vision.priorities get a lower (better) rank.
    vision_priorities = (vision_data.get("priorities") or []) if vision_data else []
    vision_priority_index: dict = {}
    for idx, sys_id in enumerate(vision_priorities):
        if isinstance(sys_id, str):
            vision_priority_index[sys_id] = idx

    def _vision_rank(d: dict) -> int:
        """0 = top vision priority, len = unlisted. Directions without system_id get neutral rank."""
        sid = d.get("system_id")
        if not sid:
            return len(vision_priorities)  # neutral — don't penalize untagged
        return vision_priority_index.get(sid, len(vision_priorities) + 1)  # unlisted slightly below neutral

    # Sort all eligible directions by priority for greedy selection
    chosen_directions: list[dict] = []
    if active_directions:
        import random
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        never_researched = [d for d in active_directions if not d.get("last_researched_at")]
        already_researched = [d for d in active_directions if d.get("last_researched_at")]

        # Sort never-researched by priority + vision alignment
        never_researched.sort(key=lambda d: (
            priority_rank.get(d.get("priority", "medium"), 1),
            _vision_rank(d),
        ))
        # Sort already-researched by priority, vision, saturation, merge rate, staleness
        already_researched.sort(key=lambda d: (
            priority_rank.get(d.get("priority", "medium"), 1),
            _vision_rank(d),
            1 if d.get("possibly_saturated") else 0,
            -(_compute_merge_rate(d) * _compute_momentum_factor(d)),
            d.get("last_researched_at") or ""
        ))
        # Prioritize never-researched, then already-researched
        sorted_eligible = never_researched + already_researched

        # Greedy selection: pick non-conflicting directions up to the parallel cap.
        # conflicts_with is a mutual exclusion list — if A lists B, they can't run
        # at the same time regardless of which was picked first.
        blocked_ids: set = set(busy_direction_ids)
        # Expand blocked set from already-running directions' conflicts
        all_dir_map = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
        for did in busy_direction_ids:
            d = all_dir_map.get(did)
            if d:
                blocked_ids.update(d.get("conflicts_with") or [])
        for d in (project.directions or []):
            if any(rid in (d.get("conflicts_with") or []) for rid in busy_direction_ids):
                blocked_ids.add(d.get("id"))

        max_parallel = _runtime_config.get("max_parallel_research_per_project", 2)
        for d in sorted_eligible:
            did = d.get("id")
            if did in blocked_ids:
                continue
            chosen_directions.append(d)
            # Block this direction and its conflicts
            blocked_ids.add(did)
            blocked_ids.update(d.get("conflicts_with") or [])
            for other in sorted_eligible:
                if did in (other.get("conflicts_with") or []):
                    blocked_ids.add(other.get("id"))
            if len(chosen_directions) >= max_parallel:
                break

        if chosen_directions:
            for d in chosen_directions:
                did = d.get("id", "?")
                merge_rate = _compute_merge_rate(d)
                momentum = _compute_momentum_factor(d)
                logger.debug(f"[RESEARCH] Selected direction {did} merge_rate={merge_rate:.2f} momentum={momentum:.1f} vision_rank={_vision_rank(d)}")
    elif not force_strategic:
        return  # No directions and not a strategic cycle — nothing to do

    # Check for a pending strategic review from a previously failed chain.
    # If the design_doc → strategic_review spawn failed last cycle, retry it now
    # rather than waiting up to 6 more research spawns (~3+ hours).
    pending_sr = (project.vision or {}).get('_pending_strategic_review')
    if pending_sr:
        try:
            await _spawn_strategic_review(project, session, cycle_id=pending_sr.get('cycle_id'))
            project.vision = {**project.vision}
            project.vision.pop('_pending_strategic_review', None)
            flag_modified(project, 'vision')
            session.add(project)
            logger.info(f'[STRATEGIC] Retried pending strategic review for project {project.id} (originally failed at {pending_sr.get("failed_at")})')
        except Exception as retry_exc:
            logger.error(f'[STRATEGIC] Retry of pending strategic review failed for project {project.id}: {retry_exc}')
            # Leave the marker — will retry on the next research spawn cycle
        return  # Don't spawn tactical research this cycle

    # Strategic cycle check — runs BEFORE the research blocking guard so it's
    # never blocked by an active tactical research task.  The design_doc_update
    # and strategic_review use their own task types and run in parallel.
    all_active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    # Use a stored spawn counter instead of derived research_history count.
    # The history-based count doesn't increment when cycles produce no novel
    # findings, causing undirected/strategic cycles to repeat indefinitely.
    # Seed from research_history on first access so existing projects don't reset.
    research_spawn_count = (project.vision or {}).get("_research_spawn_count")
    if research_spawn_count is None:
        research_spawn_count = sum(
            len(d.get("research_history", [])) + d.get("history_aggregate", {}).get("total_cycles", 0)
            for d in all_active_directions
        )
        project.vision = {**(project.vision or {}), "_research_spawn_count": research_spawn_count}
        flag_modified(project, "vision")
        session.add(project)
        logger.info(f"[RESEARCH] Seeded _research_spawn_count={research_spawn_count} from research_history")
    strategic_frequency = _runtime_config.get("strategic_research_frequency", 6)
    is_strategic = (
        (strategic_frequency > 0 and research_spawn_count > 0 and research_spawn_count % strategic_frequency == 0)
        or force_strategic
    )
    if is_strategic:
        await _spawn_design_doc_update(project, session)
        research_spawn_count += 1
        project.vision = {**(project.vision or {}), "_research_spawn_count": research_spawn_count}
        flag_modified(project, "vision")
        session.add(project)
        logger.info(f"[AUTONOMOUS] Spawning design doc update (strategic cycle #{research_spawn_count - 1}, vision rev {(project.vision or {}).get('revision', 0)})")

    # Guard: limit parallel research tasks per project.
    # EVALUATING parents are just waiting on children — they don't block new research.
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 2)
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    active_research_count = sum(1 for t in active_tasks if (t.first_payload).get("type") == "research")
    if active_research_count >= max_parallel:
        return
    # Reduce chosen_directions to the remaining slots
    remaining_slots = max_parallel - active_research_count
    chosen_directions = chosen_directions[:remaining_slots]
    # Guard: global rate limit across ALL projects
    from sqlalchemy import func
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit for research spawns
    recent_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    recent_research_count = sum(1 for t in recent_research if (t.first_payload).get("type") == "research")
    if recent_research_count >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Also cap by per-project rate limit
    rate_limit_remaining = _runtime_config["max_autonomous_tasks_per_hour"] - recent_research_count
    if rate_limit_remaining < len(chosen_directions):
        chosen_directions = chosen_directions[:max(0, rate_limit_remaining)]

    # If no active directions exist (e.g., all paused), only strategic runs (already spawned above).
    if not chosen_directions and not active_directions:
        return

    # Autonomous research creates independent tasks (no parent_task_id), so they
    # must always branch from main.  Staging branches (research/*, plan/*) are
    # ephemeral — they get deleted when their parent's rollup merges.  An
    # independent task using someone else's staging branch will break when that
    # rollup cleans up.  Actual subresearch (spawned by _handle_research_completion
    # with parent_task_id set) correctly inherits its parent's staging branch.
    base_branch = "main"

    # Use per-type defaults
    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    # Every Nth cycle is "undirected" — the agent explores the whole codebase
    # without a specific direction lens, finding cross-cutting improvements.
    undirected_frequency = _runtime_config.get("undirected_research_frequency", 4)
    is_undirected = undirected_frequency > 0 and research_spawn_count > 0 and research_spawn_count % undirected_frequency == 0
    # Don't run undirected on strategic cycles
    if is_strategic:
        is_undirected = False

    # Handle undirected cycle (only first slot, rest are directed)
    anomalies = None
    if is_undirected:
        anomalies = await _compute_research_anomalies(project.id, session)

    # Build and spawn research tasks — one per chosen direction (or one undirected)
    directions_to_spawn = chosen_directions if chosen_directions else [None]  # None = undirected
    if is_undirected:
        directions_to_spawn = [None]  # Undirected replaces directed for this cycle

    spawned_tasks = []
    for chosen_direction in directions_to_spawn:
        # Build lightweight direction summaries — full histories are available
        # via the API. This keeps the payload under the OS E2BIG limit (~128KB).
        direction_summaries = []
        for d in all_active_directions:
            summary: dict = {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "system_id": d.get("system_id"),
                "possibly_saturated": d.get("possibly_saturated", False),
                "possibly_complete": d.get("possibly_complete", False),
            }
            if d.get("conflicts_with"):
                summary["conflicts_with"] = d["conflicts_with"]
            if d.get("completion_criteria"):
                summary["completion_criteria"] = d["completion_criteria"]
            # Include compact research context (truncated) — not full history
            ctx = d.get("research_context")
            if ctx:
                summary["research_context"] = ctx[:2000]
            direction_summaries.append(summary)

        # Design doc summary — just keys and playbook, not full content.
        # Agent can fetch specific sections via ./design-doc get <key>.
        doc = project.design_doc or {}
        design_doc_summary: dict = {
            "_keys": [k for k in doc if not k.startswith("_") and not k.startswith("playbook/")],
        }
        # Include playbook entries (small, operationally critical)
        for k, v in doc.items():
            if k.startswith("playbook/") and isinstance(v, dict):
                design_doc_summary[k] = v.get("content", "")

        research_payload: dict = {
            "type": "research",
            "directions": direction_summaries,
            "current_direction_id": None if chosen_direction is None else chosen_direction.get("id"),
            "design_doc": design_doc_summary,
            "base_branch": base_branch,
            **({"model": research_model} if research_model else {}),
            **({"effort": research_effort} if research_effort else {}),
            **({"prompt_suffix": research_suffix} if research_suffix else {}),
        }
        # Pre-extract design doc context for the current direction's system_id
        if chosen_direction:
            dir_system_id = chosen_direction.get("system_id")
            if dir_system_id and project.design_doc:
                doc = project.design_doc
                doc_systems = doc.get("systems", [])
                doc_capabilities = doc.get("capabilities", {})
                relevant: dict = {"system_id": dir_system_id}
                for sys in (doc_systems if isinstance(doc_systems, list) else []):
                    if isinstance(sys, dict) and sys.get("id") == dir_system_id:
                        relevant["system"] = sys
                        break
                if isinstance(doc_capabilities, dict):
                    related_caps = {
                        cap_name: cap_val for cap_name, cap_val in doc_capabilities.items()
                        if isinstance(cap_val, dict) and cap_val.get("status") in ("missing", "partial")
                    }
                    if related_caps:
                        relevant["missing_capabilities"] = related_caps
                research_payload["direction_design_context"] = relevant
            if chosen_direction.get("possibly_complete"):
                research_payload["direction_possibly_complete"] = True
                research_payload["completion_suggestion"] = chosen_direction.get("completion_suggestion", "")
        if chosen_direction is None:
            if anomalies:
                research_payload["anomaly_signals"] = anomalies
            direction_text = "Cross-cutting codebase analysis"
            logger.info(f"[AUTONOMOUS] Spawning UNDIRECTED research cycle (cycle #{research_spawn_count})")
        else:
            direction_text = (chosen_direction.get("text") or "general improvement")[:80]

        new_task = Task(
            project_id=project.id,
            title=f"Research: {direction_text}",
            description=f"Autonomous research for: {direction_text}",
            payloads=[research_payload],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        await _increment_research_spawn_count(project, session)
        history = TaskHistory(
            task_id=new_task.id, action="SPAWNED",
            details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "post_merge_or_idle"}
        )
        session.add(history)
        spawned_tasks.append(new_task)

    await session.commit()

    # Track first task for HALF_OPEN → CLOSED transition
    if breaker["state"] == "HALF_OPEN" and spawned_tasks:
        breaker["half_open_task_id"] = str(spawned_tasks[0].id)

    for t in spawned_tasks:
        logger.info(f"[AUTONOMOUS] Spawned research task {t.id} for project {project.id}")
    if len(spawned_tasks) > 1:
        logger.info(f"[AUTONOMOUS] Spawned {len(spawned_tasks)} parallel research tasks for project {project.id}")


async def _spawn_periodic_task(project: Project, task_type: str, session: AsyncSession):
    """Spawn a periodic non-research task (qa, security_audit, deps) if its schedule is due."""
    from app.main import _maybe_create_circuit_breaker_alert  # noqa: PLC0415
    from sqlalchemy import func

    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return

    periodic_cfg = _runtime_config.get("autonomous_periodic_tasks", {}).get(task_type, {})
    if not periodic_cfg.get("enabled", False):
        return

    interval_hours = periodic_cfg.get("interval_hours", 168)

    # Guard: no task of this type already QUEUED/PROVISIONING/IN_PROGRESS
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if (t.first_payload).get("type") == task_type:
            return

    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit
    recent_auto = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if recent_auto >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Check when this type last ran (most recent completed task of this type)
    completed_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        ).order_by(Task.created_at.desc()).limit(100)
    )).scalars().all()
    last_run = None
    for t in completed_tasks:
        if (t.first_payload).get("type") == task_type:
            last_run = t.created_at
            break

    now = _utcnow()
    if last_run is not None:
        elapsed = (now - last_run).total_seconds() / 3600
        if elapsed < interval_hours:
            return

    # Infer base_branch from the most recent task in this project
    recent_task = (await session.execute(
        select(Task).where(Task.project_id == project.id)
        .order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()
    base_branch = (recent_task.first_payload).get("base_branch", "main") if recent_task else "main"

    task_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    task_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None
    # agent_prompt_suffixes: supported config key — dict of task_type → prompt suffix string,
    # appended to the agent prompt for matching task types (validated in admin PATCH endpoint).
    task_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)
    payload: dict = {
        "type": task_type,
        "base_branch": base_branch,
        **({"model": task_model} if task_model else {}),
        **({"effort": task_effort} if task_effort else {}),
        **({"prompt_suffix": task_suffix} if task_suffix else {}),
    }
    new_task = Task(
        project_id=project.id,
        title=f"{task_type.upper()} scan: autonomous periodic",
        description=f"Automated {task_type} task triggered by autonomous periodic scheduler",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "periodic_schedule", "task_type": task_type}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)

    logger.info(f"[AUTONOMOUS] Spawned periodic {task_type} task {new_task.id} for project {project.id}")


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task."""
    try:
        async with AsyncSessionLocal() as session:
            projects = (await session.execute(
                select(Project).where(
                    Project.github_owner == owner,
                    Project.github_repo == repo,
                    Project.autonomous_enabled.is_(True),
                )
            )).scalars().all()
            if not projects:
                return

            # Spawn if VM slots are available (PROVISIONING + IN_PROGRESS, not blocked QUEUED)
            from sqlalchemy import func
            vm_consuming = (await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                )
            )).scalar() or 0

            if vm_consuming < _runtime_config["max_concurrent_vms"]:
                for project in projects:
                    await _spawn_research_task(project, session)
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error checking research trigger for {owner}/{repo}: {e}", exc_info=True)


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    from app.anomaly_detection import _expire_stale_proposals, _check_anomaly_proposals  # noqa: PLC0415
    now = _utcnow()
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    if _shared_state.last_idle_check and (now - _shared_state.last_idle_check) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_idle_check = now

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func

            # Expire stale proposals before processing
            await _expire_stale_proposals(session)

            projects = (await session.execute(
                select(Project).where(Project.autonomous_enabled.is_(True))
            )).scalars().all()

            for project in projects:
                # Log pending proposals awaiting review
                pending_proposals = [d for d in (project.directions or []) if d.get("status") == "proposed"]
                if pending_proposals:
                    logger.info(f"[DIRECTIONS] Project {project.name} has {len(pending_proposals)} pending direction proposals awaiting review")

                active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
                # Allow projects with no directions through if vision needs bootstrapping
                vision_needs_bootstrap = not (project.vision or {}).get("purpose")
                strategic_recovery_needed = not active_directions and (
                    (project.vision or {}).get("_strategic_failures", 0) < 3
                )
                if not active_directions and not vision_needs_bootstrap and not strategic_recovery_needed:
                    continue

                # Spawn if VM slots are available — count only tasks consuming VMs,
                # not QUEUED tasks blocked on dependencies
                vm_consuming = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).scalar() or 0

                if vm_consuming < _runtime_config["max_concurrent_vms"]:
                    await _spawn_research_task(project, session)

                    for task_type in ("security_audit", "qa", "deps"):
                        await _spawn_periodic_task(project, task_type, session)

                # Check persistent anomalies for direction proposals (regardless of VM slots)
                await _check_anomaly_proposals(project, session)
                await session.commit()
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error in idle research check: {e}", exc_info=True)
