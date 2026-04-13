"""Strategic cycle spawning functions.

Handles Phase 1 (design_doc_update) and Phase 2 (strategic_review) of the
strategic review cycle. Extracted from autonomous_research.py to free up
function slots in that module.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, Project, TaskHistory, TaskStatus, ProjectMessage
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _spawn_design_doc_update(project: Project, session: AsyncSession) -> bool:
    """Spawn a design doc update task (Phase 1 of strategic cycle).

    This runs as a separate task type so it doesn't block tactical research.
    On completion, it chains to _spawn_strategic_review (Phase 2).
    Returns True if a task was created, False if skipped.
    """
    from app.task_lifecycle import _has_active_strategic_task  # noqa: PLC0415
    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(f"[STRATEGIC] Skipping design doc update — strategic task already active for {project.id}")
        return False

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

    payload: dict = validate_internal_payload({
        "type": "design_doc_update",
        "design_doc": project.design_doc or {},
        "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
        "project_name": project.name,
        "vision": project.vision or {},
        "open_messages": open_msgs,
        "base_branch": "main",
        **({"model": doc_model} if doc_model else {}),
        **({"effort": doc_effort} if doc_effort else {}),
    }, "autonomous_research:design_doc_update")
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
    return True


async def _spawn_strategic_review(project: Project, session: AsyncSession, cycle_id: Optional[str] = None) -> bool:
    """Spawn Phase 2 of the strategic cycle: vision + direction review.

    Called by _handle_design_doc_completion after the design doc is updated.
    Uses type 'strategic_review' so it doesn't block tactical research.
    Returns True if a task was created, False if skipped.
    """
    from app.task_lifecycle import _has_active_strategic_task  # noqa: PLC0415
    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(f"[STRATEGIC] Skipping strategic review — strategic task already active for {project.id}")
        return False

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

    from app.strategic_analysis import _build_outcome_evaluation  # noqa: PLC0415
    payload: dict = validate_internal_payload({
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
    }, "autonomous_research:strategic_review")
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
    return True
