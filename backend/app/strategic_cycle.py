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

from app.models import CYCLE_META_INTEGRITY_CONFLICT_COUNTS, StrategicCycle, Task
from app.strategic_constants import CYCLE_SUMMARY_MAX
from app.strategic_runtime_work import task_is_strategic_local_runner_hold
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
    baseline_problem_model: Optional[Mapping[str, Any]] = None,
    baseline_running_update: str | None = None,
) -> StrategicCycle:
    """Insert a new StrategicCycle row at phase-1 spawn.

    ``baseline_vision``, ``baseline_problem_model`` and
    ``baseline_running_update`` are snapshot at cycle start so the operator-wins
    merges in ``strategic_completion`` / the live API path can compare operator
    edits against the same baseline the agent was given. All three are stored
    under ``cycle_metadata`` to avoid a per-project schema change, and all three
    are skipped by :func:`_to_legacy_dict` so the snapshots never leak into the
    agent's cycle-history context.

    ``baseline_running_update`` is stored as a string (``""`` when the document
    was empty at spawn); a missing key on the row signals a legacy cycle with no
    snapshot, which :func:`baseline_running_update` reports as ``None`` so the
    reconciliation degrades to blind-assign.
    """
    cycle = StrategicCycle(
        id=cycle_id or new_cycle_id(),
        project_id=project_id,
        outcome=OUTCOME_PHASE1_RUNNING,
        phase1_task_id=phase1_task_id,
        phase1_started_at=_utcnow(),
        cycle_metadata={
            "baseline_vision": dict(baseline_vision or {}),
            "baseline_problem_model": dict(baseline_problem_model or {}),
            "baseline_running_update": baseline_running_update
            if isinstance(baseline_running_update, str)
            else "",
        },
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
    await session.flush()
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
    knowledge_available: int = 0,
    knowledge_cited: int = 0,
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[StrategicCycle]:
    """Phase 2 task reached a terminal status with parsed output. Records
    the outcome accounting and merges any per-direction maps into
    cycle_metadata (preserving baseline_vision).

    ``memos_available`` is the number of memo artifacts fed to the strategic
    agent as citable context; ``memos_cited`` is the count whose task_ids
    appeared in the agent's output. Both are stored in cycle_metadata so
    operators can verify the direction success criterion (≥1 cited per cycle).

    ``knowledge_available`` is the number of knowledge documents in the index
    fed to the agent; ``knowledge_cited`` is the count whose IDs appeared in
    the agent's cited set (via the cite_knowledge API op).
    """
    cycle = await get(session, cycle_id)
    if cycle is None:
        return None
    cycle.phase2_completed_at = _utcnow()
    cycle.outcome = OUTCOME_COMPLETED
    # Enforce the declared storage cap (STATUS/cycle strings are otherwise
    # unbounded here, mirroring the cycle_reasoning cap on the next line).
    cycle.summary_text = summary_text[:CYCLE_SUMMARY_MAX] if summary_text else summary_text
    cycle.cycle_reasoning = (cycle_reasoning or "")[:2000] if cycle_reasoning else None
    cycle.directions_created = directions_created
    cycle.directions_retired = directions_retired
    cycle.tasks_spawned = tasks_spawned
    cycle.vision_updated = vision_updated
    new_meta = dict(cycle.cycle_metadata or {})
    new_meta["memos_available"] = memos_available
    new_meta["memos_cited"] = memos_cited
    new_meta["knowledge_available"] = knowledge_available
    new_meta["knowledge_cited"] = knowledge_cited
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
    parseable summary. Retirement memo rows also carry compact durable excerpts
    from the spawned payload/context so the agent can still see the underlying
    evidence after summary trimming: ``citation_refs``, ``final_metrics_excerpt``,
    ``rationale_excerpt``, and ``reasoning_excerpt``.
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
        payload0 = (t.payloads or [{}])[0] if isinstance(t.payloads, list) else {}
        source_finding = (
            payload0.get("source_finding")
            if isinstance(payload0, dict)
            and isinstance(payload0.get("source_finding"), dict)
            else {}
        )
        context = t.context if isinstance(t.context, dict) else {}

        def _compact_metrics(value: Any) -> str:
            if isinstance(value, dict) and value:
                return ", ".join(f"{k}={v}" for k, v in value.items())
            return ""

        def _compact_text(value: Any) -> str:
            if isinstance(value, str):
                text = value.strip()
                return text[:200] if text else ""
            return ""

        entry: dict[str, Any] = {
            "task_id": str(t.id),
            "summary_excerpt": summary[:200],
        }
        # Enriched fields — additive, all optional. Consumers that read only
        # task_id/summary_excerpt are unaffected.
        reasoning_kind = (
            payload0.get("reasoning_kind") if isinstance(payload0, dict) else None
        )
        if reasoning_kind:
            entry["reasoning_kind"] = str(reasoning_kind)
        direction_id = (
            payload0.get("direction_id") if isinstance(payload0, dict) else None
        )
        if direction_id:
            entry["direction_id"] = str(direction_id)
        if isinstance(memo, dict):
            for field in ("tags", "citations", "reasoning"):
                val = memo.get(field)
                if val is not None:
                    entry[field] = val
            citations_val = memo.get("citations")
            if isinstance(citations_val, list) and citations_val:
                refs = ", ".join(
                    c.strip() for c in citations_val if isinstance(c, str) and c.strip()
                )
                if refs:
                    entry["citation_refs"] = refs
            success_evidence = _compact_text(
                source_finding.get("success_evidence")
                or memo.get("success_evidence")
                or context.get("success_evidence")
            )
            if success_evidence:
                entry["success_evidence"] = success_evidence
            retirement_condition = _compact_text(
                source_finding.get("retirement_condition")
                or memo.get("retirement_condition")
                or context.get("retirement_condition")
            )
            if retirement_condition:
                entry["retirement_condition"] = retirement_condition
            final_metrics_value = (
                source_finding.get("retirement_metrics")
                or memo.get("final_metrics")
                or context.get("final_metrics")
            )
            if final_metrics_value is not None:
                entry["final_metrics"] = final_metrics_value
            rationale_value = (
                source_finding.get("rationale")
                or memo.get("rationale")
                or context.get("retirement_rationale")
            )
            if rationale_value is not None:
                entry["rationale"] = rationale_value
            rationale_excerpt = _compact_text(rationale_value)
            if rationale_excerpt:
                entry["rationale_excerpt"] = rationale_excerpt
            reasoning_excerpt = _compact_text(memo.get("reasoning"))
            if reasoning_excerpt:
                entry["reasoning_excerpt"] = reasoning_excerpt
            final_metrics_excerpt = _compact_metrics(final_metrics_value)
            if final_metrics_excerpt:
                entry["final_metrics_excerpt"] = final_metrics_excerpt
        else:
            rationale_excerpt = _compact_text(
                source_finding.get("rationale")
                if isinstance(source_finding, dict)
                else None
            )
            if rationale_excerpt:
                entry["rationale_excerpt"] = rationale_excerpt
        citations.append(entry)
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


def baseline_problem_model(cycle: StrategicCycle) -> dict[str, Any]:
    """Read the snapshot of the problem model taken at cycle start.

    Mirrors :func:`baseline_vision`. Used by ``apply_agent_update`` (via the
    set_problem_model API path and the strategic-completion fallback) to run a
    3-way operator-wins merge so a concurrent operator edit to an
    operator-owned field is not clobbered by the agent's stale write."""
    meta = cycle.cycle_metadata or {}
    snap = meta.get("baseline_problem_model")
    return dict(snap) if isinstance(snap, dict) else {}


AGENT_PM_PROPOSAL_KEY = "agent_problem_model_proposal"
"""cycle_metadata key holding the agent's last proposed operator-owned subset
(intent.purpose / context / constraints) for the in-flight cycle. Set after each
successful ``set_problem_model`` write; absent until the first write. Skipped by
:func:`_to_legacy_dict` so it never leaks into agent cycle-history context."""


def agent_problem_model_proposal(cycle: StrategicCycle) -> dict[str, Any] | None:
    """Read the agent's last proposed operator-owned subset for this cycle.

    Returns ``None`` when the agent has not yet written problem_model this cycle
    (first/only write) so callers fall back to the cycle-start
    :func:`baseline_problem_model` snapshot. On a *second* same-cycle write this
    is preferred as the merge ancestor so the agent's own prior write is not
    misread as a mid-cycle operator edit (a genuine operator edit still moves
    ``current`` away from this proposal and wins). See
    :func:`set_agent_problem_model_proposal`."""
    meta = cycle.cycle_metadata or {}
    snap = meta.get(AGENT_PM_PROPOSAL_KEY)
    return dict(snap) if isinstance(snap, dict) else None


def set_agent_problem_model_proposal(
    cycle: StrategicCycle, subset: Mapping[str, Any]
) -> None:
    """Store the agent's pre-merge operator-owned subset for this cycle.

    ``subset`` is the shape produced by ``problem_model.operator_owned_subset``
    (intent.purpose / context / constraints). Flags ``cycle_metadata`` modified;
    the caller is responsible for ``session.add(cycle)`` / commit."""
    new_meta = dict(cycle.cycle_metadata or {})
    new_meta[AGENT_PM_PROPOSAL_KEY] = dict(subset)
    cycle.cycle_metadata = new_meta
    flag_modified(cycle, "cycle_metadata")


AGENT_VISION_PROPOSAL_KEY = "agent_vision_proposal"
"""cycle_metadata key holding the agent's last proposed vision for the in-flight
cycle. Set after each successful live ``set_vision`` write; absent until the
first write. Skipped by :func:`_to_legacy_dict` so it never leaks into agent
cycle-history context. Mirrors :data:`AGENT_PM_PROPOSAL_KEY`."""


def agent_vision_proposal(cycle: StrategicCycle) -> dict[str, Any] | None:
    """Read the agent's last proposed vision for this cycle.

    Returns ``None`` when the agent has not yet written vision this cycle
    (first/only write) so callers fall back to the cycle-start
    :func:`baseline_vision` snapshot. On a *second* same-cycle write this is
    preferred as the merge ancestor so the agent's own prior write is not
    misread as a mid-cycle operator edit (a genuine operator edit still moves
    ``current`` away from this proposal and wins). Mirrors
    :func:`agent_problem_model_proposal`."""
    meta = cycle.cycle_metadata or {}
    snap = meta.get(AGENT_VISION_PROPOSAL_KEY)
    return dict(snap) if isinstance(snap, dict) else None


def set_agent_vision_proposal(cycle: StrategicCycle, vision: Mapping[str, Any]) -> None:
    """Store the agent's pre-merge proposed vision for this cycle.

    Flags ``cycle_metadata`` modified; the caller is responsible for
    ``session.add(cycle)`` / commit. Mirrors
    :func:`set_agent_problem_model_proposal`."""
    new_meta = dict(cycle.cycle_metadata or {})
    new_meta[AGENT_VISION_PROPOSAL_KEY] = dict(vision)
    cycle.cycle_metadata = new_meta
    flag_modified(cycle, "cycle_metadata")


def baseline_running_update(cycle: StrategicCycle) -> str | None:
    """Read the snapshot of ``running_update`` taken at cycle start.

    Returns the snapshot string (``""`` when the document was empty at spawn),
    or ``None`` for a legacy cycle that predates the snapshot (no key on the
    row). Used by the live ``set_running_update`` op and the
    strategic-completion fallback to reconcile a concurrent operator edit
    operator-wins; ``None`` degrades the reconciliation to today's blind-assign.
    """
    meta = cycle.cycle_metadata or {}
    if "baseline_running_update" not in meta:
        return None
    snap = meta.get("baseline_running_update")
    return snap if isinstance(snap, str) else ""


def advance_running_update_baseline(cycle: StrategicCycle, value: str) -> None:
    """Advance the cycle's ``running_update`` baseline to ``value``.

    Called after every successful (non-operator-superseded) agent write so a
    subsequent same-cycle agent write reconciles against what the agent last
    persisted rather than the frozen cycle-start snapshot. Without this advance
    the second same-cycle write sees ``current != baseline`` and is misclassified
    as an operator edit and discarded.

    A genuine operator edit made between two agent writes still diverges from
    this advanced baseline, so operator-wins is preserved.

    Legacy cycles whose ``cycle_metadata`` never carried a ``baseline_running_update``
    key (:func:`baseline_running_update` returns ``None`` for them, degrading to
    blind-assign) are left untouched — writing the key would silently flip them
    onto the reconciliation path mid-cycle.
    """
    meta = cycle.cycle_metadata
    if not isinstance(meta, dict) or "baseline_running_update" not in meta:
        return
    if meta.get("baseline_running_update") == value:
        return
    new_meta = dict(meta)
    new_meta["baseline_running_update"] = value
    cycle.cycle_metadata = new_meta
    flag_modified(cycle, "cycle_metadata")


def record_integrity_conflict_counts(
    cycle: StrategicCycle,
    counts: dict[str, int],
) -> None:
    """Stamp integrity-conflict counts computed at cycle start into cycle_metadata.

    ``counts`` must carry the four conflict classes: orphaned_gaps,
    ungrounded_directions, stale_active_moves, and staffed_but_deferred.
    All four are stored as explicit integers so a value of 0 is
    distinguishable from a missing key (legacy cycle with no stamp).
    Callers are responsible for ``session.add(cycle)`` / commit.
    """
    new_meta = dict(cycle.cycle_metadata or {})
    new_meta[CYCLE_META_INTEGRITY_CONFLICT_COUNTS] = {
        "orphaned_gaps": int(counts.get("orphaned_gaps") or 0),
        "ungrounded_directions": int(counts.get("ungrounded_directions") or 0),
        "stale_active_moves": int(counts.get("stale_active_moves") or 0),
        "staffed_but_deferred": int(counts.get("staffed_but_deferred") or 0),
    }
    cycle.cycle_metadata = new_meta
    flag_modified(cycle, "cycle_metadata")


async def recent_integrity_conflict_trend(
    session: AsyncSession,
    project_id,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Return integrity-conflict trend data over the last ``limit`` completed cycles.

    Scans completed cycles newest-first, reads the stamped
    ``CYCLE_META_INTEGRITY_CONFLICT_COUNTS`` value, and counts how many
    consecutive cycles (from the most recent backward) are clean — i.e. all
    three conflict classes are zero.  Only cycles that actually carry the stamp
    contribute to the clean streak; cycles that predate the feature are skipped
    when computing the streak (they don't break it but they also don't extend
    it, so the caller can distinguish "2 verified clean" from "unknown history").

    Returns::

        {
            "stamped_cycles": int,          # cycles in window with a stamp
            "consecutive_clean_cycles": int,# unbroken clean streak from most-recent
            "last_counts": dict | None,     # counts from the most-recent stamped cycle
        }
    """
    stmt = (
        select(StrategicCycle)
        .where(
            StrategicCycle.project_id == project_id,
            StrategicCycle.outcome == OUTCOME_COMPLETED,
        )
        .order_by(desc(StrategicCycle.phase1_started_at))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    stamped: list[dict[str, int]] = []
    for row in rows:
        meta = row.cycle_metadata or {}
        raw = meta.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS)
        if isinstance(raw, dict):
            stamped.append(
                {
                    "orphaned_gaps": int(raw.get("orphaned_gaps") or 0),
                    "ungrounded_directions": int(raw.get("ungrounded_directions") or 0),
                    "stale_active_moves": int(raw.get("stale_active_moves") or 0),
                    # Legacy stamps (pre-staffed_but_deferred) omit this key; treat
                    # as absent rather than 0 so the caller can distinguish "verified
                    # clean on all four classes" from "old stamp, class unknown".
                    **(
                        {"staffed_but_deferred": int(raw["staffed_but_deferred"] or 0)}
                        if "staffed_but_deferred" in raw
                        else {}
                    ),
                }
            )

    # Walk rows newest-first to compute the streak. Unstamped cycles break the
    # streak just like dirty cycles do: an unstamped completed cycle (e.g. one
    # spawned via the resident-minimal path, which skips reconciliation) is an
    # unverified interval — claiming a clean streak through it is incorrect.
    # Legacy pre-feature cycles naturally appear at the tail of the window; if
    # all rows in the window are unstamped the streak stays 0, which is the
    # correct "unknown history" signal.
    # For the clean-streak, legacy stamps without staffed_but_deferred are not
    # counted as clean for that class — they break the streak to avoid silently
    # crediting cycles that never tracked the class.
    consecutive_clean = 0
    for row in rows:
        meta = row.cycle_metadata or {}
        raw = meta.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS)
        if not isinstance(raw, dict):
            break
        if "staffed_but_deferred" not in raw:
            # Legacy stamp — class was not tracked; break the streak.
            break
        counts = {
            "orphaned_gaps": int(raw.get("orphaned_gaps") or 0),
            "ungrounded_directions": int(raw.get("ungrounded_directions") or 0),
            "stale_active_moves": int(raw.get("stale_active_moves") or 0),
            "staffed_but_deferred": int(raw["staffed_but_deferred"] or 0),
        }
        if (
            counts["orphaned_gaps"] == 0
            and counts["ungrounded_directions"] == 0
            and counts["stale_active_moves"] == 0
            and counts["staffed_but_deferred"] == 0
        ):
            consecutive_clean += 1
        else:
            break

    return {
        "stamped_cycles": len(stamped),
        "consecutive_clean_cycles": consecutive_clean,
        "last_counts": stamped[0] if stamped else None,
    }


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
    # Spread metadata sub-maps back into the dict, but skip the baseline
    # snapshots (used internally for the 3-way merges; not part of the public
    # history shape, and must never leak into agent cycle-history context).
    meta = cycle.cycle_metadata or {}
    for k, v in meta.items():
        if k in (
            "baseline_vision",
            "baseline_problem_model",
            "baseline_running_update",
            AGENT_PM_PROPOSAL_KEY,
            AGENT_VISION_PROPOSAL_KEY,
        ):
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
    # Batch-load the phase-1/phase-2 linked tasks once (was one session.get per
    # candidate per phase) and look them up in-memory; a missing id resolves to
    # None exactly as session.get would, preserving the hold-check semantics.
    _linked_task_ids: set = set()
    for c in candidates:
        if c.phase1_task_id is not None:
            _linked_task_ids.add(c.phase1_task_id)
        if c.phase2_task_id is not None:
            _linked_task_ids.add(c.phase2_task_id)
    linked_tasks_by_id: dict = {}
    if _linked_task_ids:
        _linked_task_rows = (
            (await session.execute(select(Task).where(Task.id.in_(_linked_task_ids))))
            .scalars()
            .all()
        )
        linked_tasks_by_id = {t.id: t for t in _linked_task_rows}
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
                if c.phase1_task_id is not None:
                    linked_task = linked_tasks_by_id.get(c.phase1_task_id)
                    if task_is_strategic_local_runner_hold(linked_task):
                        continue
                stalled.append(c)
        elif c.outcome == OUTCOME_PHASE2_RUNNING:
            ref = c.phase2_started_at or c.phase1_started_at
            if ref and ref < phase2_cutoff:
                if c.phase2_task_id is not None:
                    linked_task = linked_tasks_by_id.get(c.phase2_task_id)
                    if task_is_strategic_local_runner_hold(linked_task):
                        continue
                stalled.append(c)
        if len(stalled) >= limit:
            break
    return stalled
