"""Read-only findings-index query surface.

Extracted from ``app.research_completion`` (which still re-exports these symbols
for backward compatibility) so the small, cohesive findings-index query surface
lives in its own module away from the large research-completion handler.

Contains three functions used as a shared service across the codebase:
  * ``lookup_findings_by_source_ref`` — find findings citing a file/PR ref.
  * ``query_findings_index`` — export a structured findings index for prompts
    and the admin UI.
  * ``_shelve_findings_for_next_cycle`` — shelve findings onto a COMPLETED
    research task for next-cycle recovery.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import closing_session
from app.models import (
    Task,
    TaskStatus,
)
from app.finding_deduplication import (
    finding_hash as _finding_hash,
)
from app.source_ref_validation import _parse_ref
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def lookup_findings_by_source_ref(
    session: AsyncSession,
    project_id: Any,
    ref: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return findings whose source_refs cite the given file path or PR ref.

    Searches plan_output of research tasks for findings with a source whose
    normalized path matches ref (exact) or is a child path of ref (prefix).
    Supports file refs (e.g. "backend/app/foo.py") and PR/message refs
    (matched by exact ref string).

    Returns a list of dicts with task_id, direction_id, finding_title,
    finding_description, matched_sources, and finding_index.
    """
    ref_path, _, _ = _parse_ref(ref)

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.plan_output.isnot(None),
        )
        .order_by(Task.created_at.desc())
        .limit(1000)
    )
    tasks = (await session.execute(stmt)).scalars().all()

    matches: List[Dict[str, Any]] = []
    for task in tasks:
        plan = task.plan_output
        if not isinstance(plan, list):
            continue
        for idx, finding in enumerate(plan):
            if not isinstance(finding, dict):
                continue
            sources = finding.get("sources") or []
            matched_sources = []
            for src in sources:
                if not isinstance(src, dict):
                    continue
                src_ref = src.get("ref", "")
                src_path, _, _ = _parse_ref(src_ref)
                if src_path == ref_path or src_path.startswith(ref_path + "/"):
                    matched_sources.append(src)
            if matched_sources:
                matches.append(
                    {
                        "task_id": str(task.id),
                        "direction_id": (task.context or {}).get("direction_id"),
                        "finding_index": idx,
                        "finding_title": finding.get("title", ""),
                        "finding_description": (finding.get("description") or "")[:200],
                        "matched_sources": matched_sources,
                    }
                )
                if len(matches) >= limit:
                    return matches
    return matches


async def query_findings_index(
    session: AsyncSession,
    project_id: Any,
    direction_id: Optional[str] = None,
    limit: int = 50,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Return structured findings rows for the strategic cycle and admin display.

    Scans plan_output of completed research tasks for the project, optionally
    filtered to a single direction.  Each returned dict has:
      task_id, direction_id, completed_at (ISO), finding_title,
      finding_description, actionability_grade, claims (list), sources (list),
      finding_index — ready for prompt embedding or JSON serialisation.

    Bounded by ``days`` recency and ``limit`` total rows so prompt budgets are
    predictable.  Ordered newest-task-first, then by finding_index within task.
    """
    since = _utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.plan_output.isnot(None),
            Task.payloads[0]["type"].as_string() == "research",
            Task.created_at >= since,
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    tasks = (await session.execute(stmt)).scalars().all()

    rows: List[Dict[str, Any]] = []
    for task in tasks:
        ctx = task.context or {}
        task_dir = ctx.get("direction_id")
        if direction_id is not None and task_dir != direction_id:
            continue
        if task.status != TaskStatus.COMPLETED:
            continue
        if (task.first_payload or {}).get("type") != "research":
            continue
        plan = task.plan_output
        if not isinstance(plan, list):
            continue
        for idx, finding in enumerate(plan):
            if not isinstance(finding, dict):
                continue
            rows.append(
                {
                    "task_id": str(task.id),
                    "direction_id": task_dir,
                    "completed_at": task.created_at.isoformat()
                    if task.created_at
                    else None,
                    "finding_index": idx,
                    "finding_title": finding.get("title", ""),
                    "finding_description": finding.get("description", ""),
                    "actionability_grade": finding.get("actionability_grade", "MEDIUM"),
                    "verification_basis": finding.get(
                        "verification_basis", "pattern_inferred"
                    ),
                    "claims": finding.get("claims") or [],
                    "sources": finding.get("sources") or [],
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


async def _shelve_findings_for_next_cycle(
    task_id: uuid.UUID,
    findings: List[Dict[str, Any]],
    reason: str,
) -> None:
    """Append `findings` to a COMPLETED research task's
    ``context.deferred_findings`` in a fresh session so the next autonomous
    research spawn picks them up.

    The propagation chain is asymmetric:
      * `autonomous_research.py:_spawn_research_task` scans recent COMPLETED
        research tasks for `context.deferred_findings` and, for each hit on the
        same direction, injects the findings into the NEW task's
        `context.prior_deferred_findings`.
      * The recovery loop inside `_handle_research_completion` reads back from
        `prior_deferred_findings` only — never `deferred_findings`.

    So the write side must use `deferred_findings`. An earlier rev of this
    helper wrote to `prior_deferred_findings` directly and was caught in PR
    #5904 review — the data sat on the COMPLETED task forever because nothing
    scans that key.

    Use this from defer/race paths where the in-memory task was detached after
    an unlocked-window break and cannot be safely mutated by the caller (see
    `_ensure_branch` docstring). Findings are stamped with deferred_reason,
    deferred_count, originally_researched_at, and finding_hash so
    autonomous_research's TTL/dedup/decay filters can act on them like any
    other deferred entry.
    """
    actionable = [f for f in findings if isinstance(f, dict)]
    if not actionable:
        return
    now_iso = _utcnow().isoformat()
    stamped: list[dict] = []
    for f in actionable:
        s = dict(f)
        s["deferred_count"] = int(s.get("deferred_count", 0) or 0) + 1
        s["deferred_reason"] = reason
        s.setdefault("originally_researched_at", now_iso)
        if not s.get("finding_hash"):
            s["finding_hash"] = _finding_hash(
                s.get("title", ""), s.get("description", "")
            )
        stamped.append(s)
    try:
        async with closing_session() as session:
            res = await session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
            t = res.scalars().one_or_none()
            if t is None:
                logger.warning(f"[RESEARCH] shelve_findings: task {task_id} not found")
                return
            ctx = dict(t.context or {})
            existing = list(ctx.get("deferred_findings") or [])
            ctx["deferred_findings"] = existing + stamped
            t.context = ctx
            flag_modified(t, "context")
            t.latest_status_message = (
                f"Findings shelved ({reason}): {len(stamped)} deferred to next cycle"
            )[:500]
            session.add(t)
            await session.commit()
            logger.warning(
                f"[RESEARCH] Shelved {len(stamped)} finding(s) on task {task_id} "
                f"(reason={reason}) for next-cycle recovery"
            )
    except Exception:
        logger.exception(
            f"[RESEARCH] shelve_findings failed for task {task_id} reason={reason}"
        )
