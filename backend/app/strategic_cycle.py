"""StrategicCycle table helpers.

Audit 2026-04-25 W2: cycles previously lived in a 10-entry sliding JSONB
list on ``Project.vision._strategic_cycle_history``. They now live in a
real table (``StrategicCycle``) so:

  - Stuck cycles (phase 1 crashed before the completion handler ran) are
    visible to the reaper instead of getting silently evicted by the cap.
  - The strategic agent can read an arbitrary historical window without
    bloating Project.vision JSONB. Callers that want a context-bounded
    view use ``recent_for_agent_context`` (default last 10).
  - Lifecycle events (terminal task status, vision rev bumps) update one
    column instead of patching a nested JSONB list.

This module is the single source of truth for cycle reads/writes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import StrategicCycle
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Default size of the "recent cycles" slice the strategic agent reads. The
# old 10-entry sliding window was a context-bloat guard; same number, but
# applied at read time so the table itself stays unbounded for diagnostics.
DEFAULT_AGENT_CONTEXT_WINDOW = 10

# Outcomes still in progress (used by stalled-cycle reaper).
ACTIVE_OUTCOMES: frozenset[str] = frozenset(
    {
        "in_progress",
        "phase1_running",
        "phase1_done",
        "phase2_running",
    }
)

OUTCOME_IN_PROGRESS = "in_progress"
OUTCOME_PHASE1_RUNNING = "phase1_running"
OUTCOME_PHASE1_DONE = "phase1_done"
OUTCOME_PHASE2_RUNNING = "phase2_running"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED_PHASE1 = "failed_phase1"
OUTCOME_FAILED_PHASE2 = "failed_phase2"
OUTCOME_STALLED_PHASE1 = "stalled_phase1"
OUTCOME_STALLED_PHASE2 = "stalled_phase2"
OUTCOME_FAILED_PHASE2_SPAWN = "failed_phase2_spawn"
OUTCOME_THINKER_ARTIFACTS_MISSING = "thinker_artifacts_missing"


def new_cycle_id() -> str:
    """Mint a new cycle id. Same shape as the legacy JSONB ids (``sc-<8 hex>``)
    so admin UI / log-grep continuity is preserved."""
    return "sc-" + uuid.uuid4().hex[:8]


async def create(
    session: AsyncSession,
    *,
    project_id,
    cycle_id: Optional[str] = None,
    phase1_task_id=None,
    baseline_vision: Optional[Mapping[str, Any]] = None,
) -> StrategicCycle:
    """Insert a new StrategicCycle row at phase-1 spawn.

    ``baseline_vision`` is snapshot at cycle start so the 3-way vision merge
    in ``strategic_completion`` can compare user edits against the same
    baseline the agent was given. Stored under ``cycle_metadata`` to avoid
    a per-project schema change.
    """
    cycle = StrategicCycle(
        id=cycle_id or new_cycle_id(),
        project_id=project_id,
        outcome=OUTCOME_PHASE1_RUNNING,
        phase1_task_id=phase1_task_id,
        phase1_started_at=_utcnow(),
        cycle_metadata={"baseline_vision": dict(baseline_vision or {})},
    )
    session.add(cycle)
    return cycle


async def get(session: AsyncSession, cycle_id: str) -> Optional[StrategicCycle]:
    return await session.get(StrategicCycle, cycle_id)


async def get_by_task(session: AsyncSession, task_id) -> Optional[StrategicCycle]:
    """Find the cycle this task belongs to (phase 1 or phase 2). Used by
    the lifecycle hook in ``task_lifecycle`` to backstop the synchronous
    phase1→phase2 chain."""
    stmt = select(StrategicCycle).where(
        (StrategicCycle.phase1_task_id == task_id)
        | (StrategicCycle.phase2_task_id == task_id)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def recent_for_agent_context(
    session: AsyncSession,
    project_id,
    *,
    limit: int = DEFAULT_AGENT_CONTEXT_WINDOW,
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` cycles serialized to the legacy
    cycle_history dict shape, newest first.

    Used by ``strategic_analysis`` to assemble the agent's context. Limit
    is applied at SQL — the table itself stays unbounded so diagnostics
    can scan further back (admin UI, reaper).
    """
    stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .order_by(desc(StrategicCycle.phase1_started_at))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_legacy_dict(c) for c in rows]


async def all_for_project(
    session: AsyncSession,
    project_id,
    *,
    limit: int = 200,
) -> list[StrategicCycle]:
    stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .order_by(desc(StrategicCycle.phase1_started_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_phase1_completed(
    session: AsyncSession,
    cycle_id: str,
    *,
    phase2_task_id=None,
) -> Optional[StrategicCycle]:
    """Phase 1 task reached a terminal status. If phase 2 is being spawned,
    pass its task id; this also flips outcome to phase2_running."""
    cycle = await get(session, cycle_id)
    if cycle is None:
        return None
    cycle.phase1_completed_at = _utcnow()
    if phase2_task_id is not None:
        cycle.phase2_task_id = phase2_task_id
        cycle.phase2_started_at = _utcnow()
        cycle.outcome = OUTCOME_PHASE2_RUNNING
    else:
        cycle.outcome = OUTCOME_PHASE1_DONE
    session.add(cycle)
    return cycle


async def mark_failure(
    session: AsyncSession,
    cycle_id: str,
    *,
    phase: str,
    reason: str,
    outcome_override: Optional[str] = None,
) -> Optional[StrategicCycle]:
    """Mark cycle as failed at ``phase`` (``"phase1"`` | ``"phase2"`` | ``"phase2_spawn"``).

    ``outcome_override`` replaces the default outcome for the phase when a
    more specific terminal outcome is appropriate (e.g.
    ``OUTCOME_THINKER_ARTIFACTS_MISSING``).  The timestamp logic for the phase
    still applies regardless of the override.
    """
    cycle = await get(session, cycle_id)
    if cycle is None:
        return None
    if phase == "phase1":
        cycle.outcome = outcome_override or OUTCOME_FAILED_PHASE1
        cycle.phase1_completed_at = cycle.phase1_completed_at or _utcnow()
    elif phase == "phase2":
        cycle.outcome = outcome_override or OUTCOME_FAILED_PHASE2
        cycle.phase2_completed_at = cycle.phase2_completed_at or _utcnow()
    elif phase == "phase2_spawn":
        cycle.outcome = outcome_override or OUTCOME_FAILED_PHASE2_SPAWN
        cycle.phase1_completed_at = cycle.phase1_completed_at or _utcnow()
    else:
        logger.warning(f"[STRATEGIC_CYCLE] mark_failure: unknown phase '{phase}'")
        return cycle
    cycle.failure_reason = (reason or "")[:2000]
    session.add(cycle)
    return cycle


async def mark_phase2_completed(
    session: AsyncSession,
    cycle_id: str,
    *,
    summary_text: Optional[str] = None,
    cycle_reasoning: Optional[str] = None,
    directions_created: int = 0,
    directions_retired: int = 0,
    tasks_spawned: int = 0,
    vision_updated: bool = False,
    memos_available: int = 0,
    memos_cited: int = 0,
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[StrategicCycle]:
    """Phase 2 task reached a terminal status with parsed output. Records
    the outcome accounting and merges any per-direction maps into
    cycle_metadata (preserving baseline_vision).

    ``memos_available`` is the number of memo artifacts fed to the strategic
    agent as citable context; ``memos_cited`` is the count whose task_ids
    appeared in the agent's output. Both are stored in cycle_metadata so
    operators can verify the direction success criterion (≥1 cited per cycle).
    """
    cycle = await get(session, cycle_id)
    if cycle is None:
        return None
    cycle.phase2_completed_at = _utcnow()
    cycle.outcome = OUTCOME_COMPLETED
    cycle.summary_text = summary_text
    cycle.cycle_reasoning = (cycle_reasoning or "")[:2000] if cycle_reasoning else None
    cycle.directions_created = directions_created
    cycle.directions_retired = directions_retired
    cycle.tasks_spawned = tasks_spawned
    cycle.vision_updated = vision_updated
    new_meta = dict(cycle.cycle_metadata or {})
    new_meta["memos_available"] = memos_available
    new_meta["memos_cited"] = memos_cited
    if extra_metadata:
        new_meta.update(extra_metadata)
    cycle.cycle_metadata = new_meta
    flag_modified(cycle, "cycle_metadata")
    session.add(cycle)
    return cycle


async def query_recent_memo_citations(
    session: AsyncSession,
    project_id,
    *,
    cap: int = 25,
) -> list[dict[str, Any]]:
    """Citable memo summaries for the current strategic cycle window.

    Window: COMPLETED memo tasks created since the *previous* strategic cycle
    started for this project (the cycle before the most recent one). When the
    project has fewer than two cycles the lower bound is unbounded.

    Returns ``[{"task_id": str, "summary_excerpt": str}]`` newest-first, capped
    at ``cap`` (oldest dropped). ``summary_excerpt`` is the first ~200 chars of
    ``plan_output['memo']['summary']`` — empty string when the memo has no
    parseable summary.
    """
    from app.models import Task, TaskStatus  # noqa: PLC0415

    recent_starts = (
        await session.execute(
            select(StrategicCycle.phase1_started_at)
            .where(StrategicCycle.project_id == project_id)
            .order_by(desc(StrategicCycle.phase1_started_at))
            .limit(2)
        )
    ).all()
    window_start = recent_starts[1][0] if len(recent_starts) >= 2 else None

    stmt = select(Task).where(
        Task.project_id == project_id,
        Task.status == TaskStatus.COMPLETED,
        Task.payloads[0]["type"].as_string() == "memo",
    )
    if window_start is not None:
        stmt = stmt.where(Task.created_at >= window_start)
    stmt = stmt.order_by(desc(Task.created_at)).limit(cap)
    rows = (await session.execute(stmt)).scalars().all()

    citations: list[dict[str, Any]] = []
    for t in rows:
        plan = t.plan_output if isinstance(t.plan_output, dict) else {}
        memo = plan.get("memo") if isinstance(plan.get("memo"), dict) else {}
        summary = str((memo or {}).get("summary") or "")
        citations.append({"task_id": str(t.id), "summary_excerpt": summary[:200]})
    return citations


async def ever_cited_memo(
    session: AsyncSession,
    project_id,
) -> dict[str, Any]:
    """All-time signal: has any completed cycle ever cited at least one memo?

    Scans the full cycle history (not just the recent display window) so the
    signal persists even after many cycles have elapsed since the citation.
    Returns the first cycle (chronologically) that cited a memo.
    """
    from app.models import CYCLE_META_MEMOS_CITED  # noqa: PLC0415

    stmt = (
        select(StrategicCycle)
        .where(
            StrategicCycle.project_id == project_id,
            StrategicCycle.outcome == OUTCOME_COMPLETED,
        )
        .order_by(StrategicCycle.phase1_started_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        cited = int((row.cycle_metadata or {}).get(CYCLE_META_MEMOS_CITED) or 0)
        if cited > 0:
            return {
                "ever_cited": True,
                "first_cycle_id": row.id,
                "first_cited_at": row.phase2_completed_at.isoformat()
                if row.phase2_completed_at
                else None,
            }
    return {"ever_cited": False, "first_cycle_id": None, "first_cited_at": None}


async def count_memo_exits(
    session: AsyncSession,
    project_id,
) -> int:
    """Count all COMPLETED tasks that exited via the memo pathway for a project.

    Supports verifying the direction success criterion of 5+ memo-pathway exits.
    """
    from app.models import Task, TaskStatus  # noqa: PLC0415

    result = await session.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.payloads[0]["type"].as_string() == "memo",
        )
    )
    return result.scalar() or 0


def count_cited_memos(memo_citations: list[dict[str, Any]], text: str) -> int:
    """Count how many memo task_ids from memo_citations appear in text.

    Callers pass the agent's cycle_reasoning/summary output so the cited count
    can be stored alongside memos_available in cycle_metadata.
    """
    if not text or not memo_citations:
        return 0
    return sum(1 for m in memo_citations if m.get("task_id") and m["task_id"] in text)


def baseline_vision(cycle: StrategicCycle) -> dict[str, Any]:
    """Read the snapshot of vision taken at cycle start. Used for 3-way
    merge when phase 2 lands."""
    meta = cycle.cycle_metadata or {}
    snap = meta.get("baseline_vision")
    return dict(snap) if isinstance(snap, dict) else {}


def _to_legacy_dict(cycle: StrategicCycle) -> dict[str, Any]:
    """Render a StrategicCycle row in the legacy cycle_history JSONB shape.

    Preserves backwards compat for callers that haven't been migrated yet
    (e.g. admin UI digest payload). Per-direction maps from cycle_metadata
    are spread back into the dict.
    """
    out: dict[str, Any] = {
        "cycle_id": cycle.id,
        "started_at": cycle.phase1_started_at.isoformat()
        if cycle.phase1_started_at
        else None,
        "phase1_task_id": str(cycle.phase1_task_id) if cycle.phase1_task_id else None,
        "phase1_completed_at": cycle.phase1_completed_at.isoformat()
        if cycle.phase1_completed_at
        else None,
        "phase2_task_id": str(cycle.phase2_task_id) if cycle.phase2_task_id else None,
        "phase2_completed_at": cycle.phase2_completed_at.isoformat()
        if cycle.phase2_completed_at
        else None,
        "directions_created": int(cycle.directions_created),
        "directions_retired": int(cycle.directions_retired),
        "tasks_spawned": int(cycle.tasks_spawned),
        "vision_updated": bool(cycle.vision_updated),
        "outcome": cycle.outcome,
    }
    if cycle.cycle_reasoning:
        out["cycle_reasoning"] = cycle.cycle_reasoning
    if cycle.summary_text:
        out["summary_text"] = cycle.summary_text
    if cycle.failure_reason:
        out["failure_reason"] = cycle.failure_reason
    # Spread metadata sub-maps back into the dict, but skip baseline_vision
    # (used internally for merge; not part of the public history shape).
    meta = cycle.cycle_metadata or {}
    for k, v in meta.items():
        if k == "baseline_vision":
            continue
        out[k] = v
    return out


async def find_stalled(
    session: AsyncSession,
    *,
    phase1_max_age: timedelta = timedelta(hours=3),
    phase2_max_age: timedelta = timedelta(hours=3),
    limit: int = 50,
) -> list[StrategicCycle]:
    """Cycles that have been in an active phase for too long.

    Used by the stalled-cycle reaper. Returns rows the reaper should
    transition to ``stalled_*`` states. Bounds are conservative — strategic
    tasks usually finish in <30 min; >3h means something crashed and the
    completion handler never fired.
    """
    now = _utcnow()
    phase1_cutoff = now - phase1_max_age
    phase2_cutoff = now - phase2_max_age
    stmt = (
        select(StrategicCycle)
        .where(
            StrategicCycle.outcome.in_(
                [
                    OUTCOME_IN_PROGRESS,
                    OUTCOME_PHASE1_RUNNING,
                    OUTCOME_PHASE1_DONE,
                    OUTCOME_PHASE2_RUNNING,
                ]
            ),
        )
        .order_by(StrategicCycle.phase1_started_at)
        .limit(limit * 4)  # over-fetch so phase-2 stall checks find candidates
    )
    candidates = (await session.execute(stmt)).scalars().all()
    stalled: list[StrategicCycle] = []
    for c in candidates:
        if c.outcome in (
            OUTCOME_IN_PROGRESS,
            OUTCOME_PHASE1_RUNNING,
            OUTCOME_PHASE1_DONE,
        ):
            if (
                c.phase1_started_at
                and c.phase1_started_at < phase1_cutoff
                and c.phase2_started_at is None
            ):
                stalled.append(c)
        elif c.outcome == OUTCOME_PHASE2_RUNNING:
            ref = c.phase2_started_at or c.phase1_started_at
            if ref and ref < phase2_cutoff:
                stalled.append(c)
        if len(stalled) >= limit:
            break
    return stalled
