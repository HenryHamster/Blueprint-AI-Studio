"""Durable next-cycle shelving for research findings."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.context_bounds import bound_deferred_findings
from app.database import closing_session
from app.finding_deduplication import finding_hash as _finding_hash
from app.models import Task
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)


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
            ctx["deferred_findings"] = bound_deferred_findings(existing + stamped)
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
