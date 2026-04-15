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

from app.failure_classification import classify_failure as _classify_failure
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
            entry["prs_rejected"] = sum(
                1 for ct in child_tasks_rows
                if (ct.context or {}).get("pr_rejected")
            )
            _tasks_created = entry.get("tasks_created", 0)
            entry["conversion_rate"] = round(entry["prs_merged"] / _tasks_created, 3) if _tasks_created > 0 else None
            # Review quality signals: track how users respond to Blueprint's reviews
            entry["reviews_dismissed"] = sum(
                1 for ct in child_tasks_rows
                if (ct.context or {}).get("review_dismissed")
            )
            entry["reviews_overridden"] = sum(
                1 for ct in child_tasks_rows
                if (ct.context or {}).get("user_overrode_review")
            )
            entry["user_issues_caught"] = sum(
                1 for ct in child_tasks_rows
                if (ct.context or {}).get("user_caught_issues")
            )
            # avg_time_to_merge_hours: mean of (vm_stopped_at - created_at) for merged tasks
            merge_durations = [
                (ct.vm_stopped_at - ct.created_at).total_seconds() / 3600
                for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED
                and (ct.context or {}).get("pr_merged")
                and ct.vm_stopped_at is not None
                and ct.created_at is not None
            ]
            entry["avg_time_to_merge_hours"] = round(sum(merge_durations) / len(merge_durations), 2) if merge_durations else None
            # Capture failed task details so research agents can see WHY tasks failed
            INFRA_CATEGORIES = {"PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API"}
            failed_details = []
            infra_failures = 0
            for ct in child_tasks_rows:
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    reason = (ct.latest_status_message or "")[:120]
                    category = (ct.context or {}).get("failure_category", "")
                    failed_details.append({"title": ct.title or "", "reason": reason, "category": category})
                    if category in INFRA_CATEGORIES:
                        infra_failures += 1
            entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat
            entry["tasks_failed_infra"] = infra_failures

            # Stage dropout: per-cycle breakdown of why tasks didn't reach main
            _INFRA_CLASSES = {"PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API"}
            sd_code_error = 0
            sd_infra = 0
            for ct in child_tasks_rows:
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    fc = _classify_failure(ct)
                    if fc == "CODE_ERROR":
                        sd_code_error += 1
                    elif fc in _INFRA_CLASSES:
                        sd_infra += 1
            sd_review_capped = sum(1 for ct in child_tasks_rows if ct.requires_approval)
            research_task_obj = await session.get(Task, research_task_uuid)
            rollup_failed = (
                research_task_obj is not None
                and research_task_obj.status == TaskStatus.FAILED
            )
            sd_staging_only = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED
                and (ct.context or {}).get("pr_merged")
                and rollup_failed
            )
            entry["stage_dropout"] = {
                "tasks_failed_code_error": sd_code_error,
                "tasks_failed_infra": sd_infra,
                "tasks_review_capped": sd_review_capped,
                "tasks_merged_staging_only": sd_staging_only,
            }

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

            # Track per-scope merge rates so research agents can calibrate scope assignments
            scope_breakdown: dict = {}
            for ct in child_tasks_rows:
                scope = (ct.first_payload or {}).get("scope", "unknown")
                if scope not in scope_breakdown:
                    scope_breakdown[scope] = {"created": 0, "merged": 0, "failed": 0}
                scope_breakdown[scope]["created"] += 1
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged"):
                    scope_breakdown[scope]["merged"] += 1
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    scope_breakdown[scope]["failed"] += 1
            entry["scope_breakdown"] = scope_breakdown

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
