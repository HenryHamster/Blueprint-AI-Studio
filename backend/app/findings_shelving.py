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


# Durable per-finding "retired disposition" record key.  Written whenever a
# shelved/deferred finding is dropped at the deferred_finding_ttl_days TTL by
# either drop site (autonomous_research's recovery scan and
# research_completion's recovery block) so operators and the strategic cycle can
# see WHICH findings were retired, when, and why — instead of only an aggregate
# count.
RETIRED_DISPOSITION_KEY = "finding_retired_dispositions"


def build_retired_disposition_records(
    expired_findings: List[Dict[str, Any]],
    *,
    existing_records: List[Dict[str, Any]] | None = None,
    now_iso: str | None = None,
) -> List[Dict[str, Any]]:
    """Build the idempotent retired-disposition record list for expired findings.

    Merges into ``existing_records`` (the value already under
    ``context[RETIRED_DISPOSITION_KEY]``) keyed on ``finding_hash`` so repeated
    recovery scans of the same expired finding never duplicate a disposition.
    Each record carries finding_hash, title, direction_id, deferred_reason,
    deferred_count, retired_at (ISO), retired_by ("ttl") and disposition
    ("retired"), plus ``source_task_id`` when the finding names one (stamped by
    the spawn-side recovery scan).

    Pure: allocates no DB state and raises nothing, so it is inherently
    fail-open for call sites that merge into an in-memory task context.
    """
    if not expired_findings:
        return list(existing_records or [])
    records = list(existing_records or [])
    seen = {r.get("finding_hash") for r in records if r.get("finding_hash")}
    stamp = now_iso or _utcnow().isoformat()
    for f in expired_findings:
        if not isinstance(f, dict):
            continue
        fhash = f.get("finding_hash") or _finding_hash(
            f.get("title", ""), f.get("description", "") or f.get("prompt", "")
        )
        if not fhash or fhash in seen:
            continue
        seen.add(fhash)
        rec: Dict[str, Any] = {
            "finding_hash": fhash,
            "title": f.get("title") or (f.get("description") or "")[:60],
            "direction_id": f.get("direction_id"),
            "deferred_reason": f.get("deferred_reason"),
            "deferred_count": int(f.get("deferred_count", 0) or 0),
            "retired_at": stamp,
            "retired_by": "ttl",
            "disposition": "retired",
        }
        if f.get("source_task_id"):
            rec["source_task_id"] = str(f["source_task_id"])
        records.append(rec)
    return records


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
            ctx.pop("_research_spawn_pending", None)
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
