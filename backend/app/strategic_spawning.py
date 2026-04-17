"""Strategic cycle spawning functions.

Handles Phase 1 (design_doc_update) and Phase 2 (strategic_review) of the
strategic review cycle. Extracted from autonomous_research.py to free up
function slots in that module.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, Project, TaskHistory, TaskStatus, ProjectMessage
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _build_shipped_changes_manifest(
    project: Project, session: AsyncSession
) -> list[dict]:
    """Build a manifest of merged tasks since the last architecture review.

    Uses design_doc._meta.last_updated as the cutoff, falling back to the
    most recent phase1_completed_at from vision._strategic_cycle_history.
    Returns a list of direction-grouped dicts, capped at 50 merged tasks.
    """
    # Determine cutoff timestamp.
    # Task.updated_at is TIMESTAMP WITHOUT TIME ZONE — the cutoff must be naive
    # UTC or asyncpg raises "can't subtract offset-naive and offset-aware
    # datetimes" when binding the parameter.
    def _parse_naive_utc(ts: str) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (ValueError, TypeError):
            return None

    cutoff: Optional[datetime] = None
    last_updated = (project.design_doc or {}).get("_meta", {}).get("last_updated")
    if last_updated:
        cutoff = _parse_naive_utc(last_updated)

    if cutoff is None:
        cycle_history = (project.vision or {}).get("_strategic_cycle_history") or []
        for entry in reversed(cycle_history):
            phase1_ts = entry.get("phase1_completed_at")
            if phase1_ts:
                cutoff = _parse_naive_utc(phase1_ts)
                if cutoff is not None:
                    break

    # Query completed tasks updated after cutoff (filter pr_merged in Python)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        )
        .order_by(Task.updated_at.desc())
        .limit(200)
    )
    if cutoff is not None:
        stmt = stmt.where(Task.updated_at > cutoff)

    result = await session.execute(stmt)
    completed_tasks = result.scalars().all()

    # Filter to merged tasks and collect manifest entries (cap at 50)
    manifest_entries: list[dict] = []
    for t in completed_tasks:
        ctx = t.context or {}
        if not ctx.get("pr_merged"):
            continue
        entry: dict = {
            "title": t.title,
            "direction_id": ctx.get("direction_id"),
            "changed_files": ctx.get("_pr_changed_files") or [],
            "scope": ctx.get("scope"),
        }
        manifest_entries.append(entry)
        if len(manifest_entries) >= 50:
            break

    if not manifest_entries:
        return []

    # Group by direction_id
    by_direction: dict[str, dict] = {}
    for entry in manifest_entries:
        dir_id = entry["direction_id"] or "unassigned"
        if dir_id not in by_direction:
            by_direction[dir_id] = {
                "direction_id": entry["direction_id"],
                "tasks": [],
                "all_changed_files": [],
            }
        by_direction[dir_id]["tasks"].append({
            "title": entry["title"],
            "scope": entry["scope"],
            "changed_files": entry["changed_files"],
        })
        for f in entry["changed_files"]:
            if f not in by_direction[dir_id]["all_changed_files"]:
                by_direction[dir_id]["all_changed_files"].append(f)

    return list(by_direction.values())


async def _build_recent_interventions(
    project: Project, session: AsyncSession, limit: int = 15, lookback_days: int = 21
) -> dict[str, Any]:
    """Build a time-series view of recent merged work + per-week merge-rate deltas.

    Returns {merges: [...], weekly_rates: [...]} where:
      - merges: last `limit` merged PRs, each with {pr_title, pr_number,
        direction_id, direction_text, week, merged_at}
      - weekly_rates: per-week create/merge counts + rate + delta vs prior week
        (same shape as admin_analytics._weekly_merge_rate_deltas)

    The strategic agent uses this to notice when recent features correlate
    with metric movement — correlation, NOT causation. The prompt frames
    the uncertainty explicitly.
    """
    cutoff = _utcnow() - timedelta(days=lookback_days)

    # All tasks in the lookback window — needed for the weekly rate calc
    tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.created_at >= cutoff,
        )
    )
    tasks_in_window = list(tasks_result.scalars().all())

    # Compute weekly rates using the shared helper
    from app.routers.admin_analytics import _weekly_merge_rate_deltas  # noqa: PLC0415
    weekly_rates = _weekly_merge_rate_deltas(tasks_in_window)

    # Build direction lookup for text enrichment
    directions_by_id: dict[str, str] = {
        d.get("id", ""): d.get("text", "")[:200]
        for d in (project.directions or [])
        if d.get("id")
    }

    # Extract the latest N merged tasks, newest first
    merged = [
        t for t in tasks_in_window
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
    ]
    merged.sort(
        key=lambda t: t.updated_at or t.created_at or datetime.min,
        reverse=True,
    )

    merges_out: list[dict[str, Any]] = []
    for t in merged[:limit]:
        ctx = t.context or {}
        merged_at = t.updated_at or t.created_at
        week_key = None
        if merged_at:
            d = merged_at.date()
            week_key = (d - timedelta(days=d.weekday())).isoformat()
        direction_id = ctx.get("direction_id")
        merges_out.append({
            "pr_title": (t.title or "")[:200],
            "pr_number": ctx.get("rollup_pr_number") or ctx.get("pr_number"),
            "direction_id": direction_id,
            "direction_text": directions_by_id.get(direction_id, "") if direction_id else "",
            "week": week_key,
            "merged_at": merged_at.isoformat() if merged_at else None,
        })

    return {
        "merges": merges_out,
        "weekly_rates": weekly_rates,
        "lookback_days": lookback_days,
    }


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

    # Build shipped_changes_manifest: tasks merged since the last arch review
    shipped_changes_manifest = await _build_shipped_changes_manifest(project, session)

    payload: dict = validate_internal_payload({
        "type": "design_doc_update",
        "design_doc": project.design_doc or {},
        "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
        "project_name": project.name,
        "vision": project.vision or {},
        "open_messages": open_msgs,
        "shipped_changes_manifest": shipped_changes_manifest,
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
        total_rejected = sum(h.get("prs_rejected", 0) for h in history) + (agg.get("total_prs_rejected") or 0)
        total_failed = sum(h.get("tasks_failed", 0) for h in history) + (agg.get("total_tasks_failed") or 0)
        total_failed_infra = sum(h.get("tasks_failed_infra", 0) for h in history) + (agg.get("total_tasks_failed_infra") or 0)
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
        total_prs = total_merged + total_rejected
        operator_rejection_rate = round(total_rejected / total_prs, 2) if total_prs > 0 else None
        stat_entry: dict = {
            "id": d.get("id"), "text": d.get("text"), "priority": d.get("priority"),
            "status": d.get("status"), "total_cycles": total_cycles,
            "prs_merged": total_merged, "tasks_failed": total_failed,
            "possibly_saturated": d.get("possibly_saturated", False),
        }
        if total_failed_infra > 0:
            stat_entry["tasks_failed_infra"] = total_failed_infra
        if total_rejected > 0:
            stat_entry["prs_rejected"] = total_rejected
        if operator_rejection_rate is not None and total_rejected > 0:
            stat_entry["operator_rejection_rate"] = operator_rejection_rate
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

    # Fetch unified conversation thread — last 30 messages chronologically,
    # regardless of status.  Gives the strategic agent a coherent back-and-forth
    # instead of flat, separated open/resolved lists.
    thread_result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
        ).order_by(ProjectMessage.created_at.desc()).limit(30)
    )
    all_thread_msgs = list(reversed(thread_result.scalars().all()))  # chronological
    conversation_thread = [
        {"id": str(m.id), "type": m.message_type, "content": m.content,
         "context": m.context, "from": m.from_type, "status": m.status,
         "resolution": m.resolution,
         "created_at": m.created_at.isoformat() if m.created_at else None,
         "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None}
        for m in all_thread_msgs
    ]
    # Executor still needs a flat list of open messages for resolve operations
    open_msgs = [msg for msg in conversation_thread if msg["status"] == "open"]

    review_model = _runtime_config.get("task_type_models", {}).get("strategic_review") or \
                   _runtime_config.get("task_type_models", {}).get("research") or None
    review_effort = _runtime_config.get("task_type_efforts", {}).get("strategic_review") or \
                    _runtime_config.get("task_type_efforts", {}).get("research") or None

    from app.strategic_analysis import _build_outcome_evaluation  # noqa: PLC0415
    recent_interventions = await _build_recent_interventions(project, session)
    payload: dict = validate_internal_payload({
        "type": "strategic_review",
        "research_mode": "strategic",
        "design_doc": project.design_doc or {},
        "vision": project.vision or {},
        "directions_summary": direction_stats,
        "outcome_evaluation": _build_outcome_evaluation(project),
        "recent_interventions": recent_interventions,
        "project_name": project.name,
        "open_messages": open_msgs,
        "conversation_thread": conversation_thread,
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
