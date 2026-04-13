"""Direction research outcome tracking.

Extracted from task_lifecycle.py to relieve its line-count ceiling.
Contains _update_direction_research_outcomes, called whenever a child task
of a research cycle reaches a terminal status so that research_history
entries accumulate live outcome statistics.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, TaskStatus


async def _update_direction_research_outcomes(
    project_id: uuid.UUID, direction_id: str, research_task_id: str, session: AsyncSession
) -> None:
    """Update a direction's research_history entry with child task outcome counts.

    Called whenever a child task of a research cycle reaches a terminal status so
    that research_history entries accumulate live outcome statistics.
    """
    from app.research_completion import _parse_research_context  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if not project:
        return
    directions = list(project.directions or [])
    updated = False
    for d in directions:
        if d.get("id") != direction_id:
            continue
        history = d.get("research_history", [])
        for entry in history:
            if entry.get("task_id") != research_task_id:
                continue
            research_task_uuid = None
            try:
                research_task_uuid = uuid.UUID(entry.get("task_id", ""))
            except (ValueError, AttributeError):
                pass
            if not research_task_uuid:
                break
            child_tasks_stmt = select(Task).where(Task.parent_task_id == research_task_uuid)
            child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
            if not child_tasks_rows:
                break
            entry["tasks_completed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED)
            entry["tasks_failed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED)
            entry["tasks_cancelled"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED)
            entry["prs_merged"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged")
            )
            # Capture failed task details so research agents can see WHY tasks failed
            failed_details = []
            for ct in child_tasks_rows:
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    reason = (ct.latest_status_message or "")[:120]
                    failed_details.append({"title": ct.title or "", "reason": reason})
            entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat

            # Classify child tasks as product vs structural
            PRODUCT_TYPES = {"create", "feature", "improvement"}
            type_counts: dict = {}
            product_count = 0
            structural_count = 0
            for ct in child_tasks_rows:
                task_type = (ct.first_payload or {}).get("type", "unknown")
                type_counts[task_type] = type_counts.get(task_type, 0) + 1
                if task_type in PRODUCT_TYPES:
                    product_count += 1
                else:
                    structural_count += 1
            entry["type_distribution"] = type_counts
            total_typed = product_count + structural_count
            entry["product_ratio"] = round(product_count / total_typed, 2) if total_typed > 0 else 0.0
            entry["structural_count"] = structural_count

            # Maintain research_context: promote proposed findings based on outcomes
            ctx = d.get("research_context", "")
            if ctx:
                parsed = _parse_research_context(ctx)
                for ct in child_tasks_rows:
                    title = ct.title or ""
                    if not title:
                        continue
                    if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                        # Move matching proposed finding to Failed Approaches
                        reason = (ct.latest_status_message or "")[:120]
                        new_established = []
                        moved = False
                        for item in parsed["established"]:
                            if title.lower() in item.lower():
                                parsed["failed"].append(f"{title} — {reason}" if reason else title)
                                moved = True
                            else:
                                new_established.append(item)
                        if moved:
                            parsed["established"] = new_established
                    elif ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged"):
                        # Promote (proposed) finding to (confirmed)
                        new_established = []
                        for item in parsed["established"]:
                            if title.lower() in item.lower() and "(proposed)" in item:
                                new_established.append(item.replace("(proposed)", "(confirmed)"))
                            else:
                                new_established.append(item)
                        parsed["established"] = new_established

                # Reassemble the document (preserve eviction summary if present)
                parts: list = []
                if parsed.get("eviction_summary"):
                    parts.append("## Eviction Summary")
                    parts.append(parsed["eviction_summary"])
                    parts.append("")
                for header, key in [
                    ("## Established Findings", "established"),
                    ("## Failed Approaches", "failed"),
                    ("## Open Questions", "open_questions"),
                ]:
                    parts.append(header)
                    for item in parsed[key]:
                        parts.append(f"- {item}")
                    parts.append("")
                updated_ctx = "\n".join(parts).strip() + "\n"
                d["research_context"] = updated_ctx[:8000]

            updated = True
            break
        if updated:
            break
    if updated:
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
