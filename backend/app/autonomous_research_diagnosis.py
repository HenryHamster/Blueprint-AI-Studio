"""Autonomous direction-diagnosis research spawning.

Extracted from :mod:`app.autonomous_research` to keep that module within its
size budget. The function is still re-exported from ``autonomous_research``
so existing callers (``direction_lifecycle`` and tests that import it from
there) continue to work unchanged.
"""
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


async def _spawn_direction_diagnosis(
    project: Project,
    direction: dict,
    pause_reason: str,
    session: AsyncSession,
) -> bool:
    """Spawn a diagnosis research task for a freshly auto-paused direction.

    Diagnosis research investigates WHY the direction keeps failing — it looks at
    the direction's recent error signatures, failure classifications, and playbook
    entries to produce targeted fix findings that address the root cause before the
    direction's 48h cooldown elapses.

    Returns True if a new diagnosis task was created, False if skipped (already an
    active diagnosis task for this direction, or the spawn couldn't be completed).
    """
    did = direction.get("id")
    if not did:
        return False

    # Skip if a diagnosis research is already active for this direction —
    # one diagnosis per pause event is plenty.
    existing = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
            Task.context["diagnosis_for_direction"].as_string() == did,
        )
    )).scalars().first()
    if existing is not None:
        logger.info(
            f"[DIAGNOSIS] Skipping diagnosis spawn for direction {did}: "
            f"task {existing.id} already active"
        )
        return False

    # Gather recent failed-task signatures for this direction to inform the agent.
    recent_failed_rows = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.FAILED,
            Task.context["direction_id"].as_string() == did,
        ).order_by(Task.created_at.desc()).limit(10)
    )).scalars().all()
    failed_signatures: list[dict] = []
    taxonomy_counts: dict[str, int] = {}
    for t in recent_failed_rows:
        ctx = t.context or {}
        payload = t.first_payload or {}
        category = ctx.get("failure_category") or ""
        taxonomy = ctx.get("failure_taxonomy") or ""
        signature = {
            "task_id": str(t.id),
            "title": (t.title or "")[:200],
            "task_type": payload.get("type", ""),
            "failure_category": category,
            "failure_taxonomy": taxonomy,
            "status_message": (t.latest_status_message or "")[:400],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        fingerprint = ctx.get("failure_fingerprint") or ctx.get("failure_stage")
        if fingerprint:
            signature["failure_fingerprint"] = str(fingerprint)[:200]
        failed_signatures.append(signature)
        if taxonomy:
            taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
        elif category:
            taxonomy_counts[category] = taxonomy_counts.get(category, 0) + 1

    # Pull playbook entries (operational knowledge) for the agent to reference.
    doc = project.design_doc or {}
    playbook_entries: dict[str, str] = {}
    for k, v in doc.items():
        if k.startswith("playbook/") and isinstance(v, dict):
            content = v.get("content") or ""
            if content:
                playbook_entries[k] = content[:2000]

    # Compact direction summary — just this direction, with recent history attached.
    raw_history = direction.get("research_history") or []
    _HISTORY_NUMERIC_FIELDS = {
        "tasks_created", "prs_merged", "tasks_failed",
        "tasks_failed_infra", "scope_breakdown",
    }
    compact_history = [
        {k: entry[k] for k in _HISTORY_NUMERIC_FIELDS if k in entry}
        for entry in raw_history[-5:]
    ]
    direction_summary: dict = {
        "id": did,
        "text": direction.get("text"),
        "priority": direction.get("priority"),
        "system_id": direction.get("system_id"),
        "paths": direction.get("paths") or [],
        "auto_pause_count": direction.get("auto_pause_count", 0),
    }
    if direction.get("category"):
        direction_summary["category"] = direction["category"]
    if compact_history:
        direction_summary["research_history"] = compact_history

    # Design doc summary — keys + playbook entries (same shape as standard research).
    design_doc_summary: dict = {
        "_keys": [k for k in doc if not k.startswith("_") and not k.startswith("playbook/")],
    }
    design_doc_summary.update(playbook_entries)

    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")

    remediation_history = {
        "remediation_attempts": int(direction.get("remediation_attempts") or 0),
        "last_remediation_result": direction.get("last_remediation_result"),
        "last_remediation_at": direction.get("last_remediation_at"),
        "last_remediation_task_id": direction.get("last_remediation_task_id"),
        "last_remediation_fingerprint": direction.get("last_remediation_fingerprint"),
        "last_remediation_failure_class": direction.get("last_remediation_failure_class"),
        "last_remediation_error_summary": direction.get("last_remediation_error_summary"),
    }

    research_payload: dict = {
        "type": "research",
        "research_mode": "diagnosis",
        "directions": [direction_summary],
        "current_direction_id": did,
        "design_doc": design_doc_summary,
        "base_branch": "main",
        "pause_reason": pause_reason,
        "failed_task_signatures": failed_signatures,
        "failure_taxonomy_counts": taxonomy_counts,
        "remediation_history": remediation_history,
        **({"model": research_model} if research_model else {}),
        **({"effort": research_effort} if research_effort else {}),
        **({"prompt_suffix": research_suffix} if research_suffix else {}),
    }
    if project.build_config:
        research_payload["build_config"] = project.build_config
    research_payload = validate_internal_payload(research_payload, "autonomous_research:diagnosis")

    direction_text = (direction.get("text") or "direction")[:160]
    new_task = Task(
        project_id=project.id,
        title=f"Diagnosis: {direction_text}",
        description=f"Auto-paused diagnosis for direction: {direction_text}",
        payloads=[research_payload],
        context={
            "diagnosis_for_direction": did,
            "direction_id": did,
            "pause_reason": pause_reason,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={
            "spawned_by": "AUTONOMOUS_LOOP",
            "trigger": "direction_auto_pause_diagnosis",
            "pause_reason": pause_reason,
        },
    )
    session.add(history)
    logger.info(
        f"[DIAGNOSIS] Spawned diagnosis research {new_task.id} for direction {did}: "
        f"{pause_reason} ({len(failed_signatures)} failed task signatures)"
    )
    return True
