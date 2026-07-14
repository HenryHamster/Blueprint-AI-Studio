"""Turn stranded capability/foundation prerequisites into missing-foundation demand.

When a finding (at spawn time) or a blocked dependent (at resume time) declares a
prerequisite — a capability it needs, or a sibling task / finding-hash that must
land first — and NO task ever provides it, the dependent is parked BLOCKED,
re-evaluated by the reaper's blocked-resume sweep up to the parked re-eval cap,
then silently CANCELLED with give-up class ``never_satisfiable``. Nothing turns
that stranded need into a *request to BUILD the missing foundation*.

This leaf module is the PRODUCER half of that feedback loop. It does NOT spawn
work or write to the database — it computes, at READ time, an aggregated
"missing-foundation demand" signal that the autonomous-research / strategic layer
(or a digest metric) can consume to commission the foundation or retire the
demand. No new table and no migration: the inputs are the live BLOCKED population
and the durable give-up-cancellation markers the reaper already stamps.

A prerequisite counts as *missing* only when NO task actually provides it — reusing
the conservative matchers in :mod:`app.blocked_need_resolution` (the live
active-candidate matcher AND the shipped-completed-foundation matcher) rather than
re-deriving the match logic. A need satisfied by an in-flight or already-shipped
foundation is NOT demand; only a need whose foundation is absent or dead is.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlmodel import select

from app.blocked_need_resolution import (
    ACTIVE_CANDIDATE_STATUSES,
    COMPLETED_CANDIDATE_STATUSES,
    TERMINAL_CANDIDATE_STATUSES,
    completed_foundation_for_need,
    resolve_blocked_need,
)
from app.capability_registry import normalize_capability_name
from app.database import resolve_dialect_name
from app.finding_deduplication import normalize_prerequisite
from app.landed_signal import has_merge_unblock_signal
from app.models import Task, TaskStatus
from app.reaper_sweeps import UNRESUMABLE_CANCEL_CONTEXT_KEY
from app.task_blocked import get_blocker_needs
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Number of top demand entries named inline in the advisory prompt hint; the
# full (bounded) list always rides along in the payload field for the agent.
_HINT_ENTRY_LIMIT = 5

# Foundation-kind needs the demand signal tracks. A ``pr`` need names an external
# pull request, not a foundation this system would commission, so it is excluded —
# only ``capability`` and ``task`` (sibling / finding-hash) needs become demand.
# Matches ``finding_deduplication.VALID_PREREQUISITE_KINDS`` (``normalize_prerequisite``
# already rejects everything else), kept as an explicit local for the helper filter.
_FOUNDATION_NEED_KINDS = frozenset({"capability", "task"})

# Default window for folding give-up-CANCELLED dependents back into demand. Mirrors
# ``digest_metrics._UNRESUMABLE_CANCEL_WINDOW`` so a retirement stays visible for the
# same horizon the unresumable-cancellation health signal uses.
DEFAULT_LOOKBACK = timedelta(days=7)

# Bound the demand list and the per-entry sample of dependent task ids, exactly as
# the digest summary helpers cap their lists (newest-or-largest-first).
_DEMAND_LIST_LIMIT = 50
_SAMPLE_TASK_LIMIT = 10
# Cap the live-BLOCKED and cancelled fetches so a pathological backlog cannot
# unbounded-scan; mirrors the digest dependency-health query limits.
_BLOCKED_FETCH_LIMIT = 2000
_CANCELLED_FETCH_LIMIT = 1000

# The COMPLETED / terminal candidate scans (foundations that may satisfy a live
# blocker) grow without bound as a project accumulates history — and unlike the
# fetches above they were left un-capped. A long-lived project's full COMPLETED +
# FAILED/CANCELLED backlog loaded in one ``select(Task)`` of fat JSONB rows
# (context / payloads / plan_output) is what wedged the digest path on
# 2026-06-15: the scan blew the server statement_timeout (QueryCanceledError) and
# stalled the event loop materializing the result, starving every other request
# (heartbeats included) into the Cloud Run 300s ceiling. Bound them the same way
# as the fetches above: a generous recency window — a foundation a CURRENTLY
# blocked dependent waits on shipped recently relative to that dependent — plus a
# hard newest-first LIMIT backstop. Both are served by
# ``ix_task_project_status_updated`` (project_id, status, updated_at). The signal
# is advisory (digest metric + fail-open spawn hint), so the rare beyond-window
# foundation miss only over-reports demand and never gates control flow.
_CANDIDATE_FETCH_LOOKBACK = timedelta(days=30)
_CANDIDATE_FETCH_LIMIT = 2000


def _demand_identity(need: Dict[str, Any]) -> Optional[tuple]:
    """Return a ``(kind, identity_ref)`` grouping key for a foundation need, or None.

    Normalizes the need through :func:`finding_deduplication.normalize_prerequisite`
    (the single source of valid-kind / non-empty-ref policy) and keeps only
    foundation kinds. Capability refs are matched case-insensitively (mirroring the
    resolver), so they group by lowercased ref; task / finding-hash refs are
    case-sensitive and group verbatim.
    """
    norm = normalize_prerequisite(need)
    if norm is None or norm["kind"] not in _FOUNDATION_NEED_KINDS:
        return None
    kind = norm["kind"]
    ref = norm["ref"]
    if kind == "capability":
        identity_ref = normalize_capability_name(ref)
        if identity_ref is None:
            return None
    else:
        identity_ref = ref
    return (kind, identity_ref)


def unresolved_capability_needs(
    task: Task,
    *,
    active: List[Task],
    completed: List[Task],
    failed_parent_ids: Optional[set] = None,
    substance_index=None,
) -> List[Dict[str, Any]]:
    """Return a BLOCKED task's UNRESOLVED foundation/capability needs.

    A need (``{kind, ref, label}`` recorded under ``context['blocker']['needs']``,
    via :func:`task_blocked.get_blocker_needs`) is *unresolved* — i.e. genuine
    missing-foundation demand — when, for a foundation kind (``capability`` /
    ``task``):

      * no live ACTIVE candidate resolves it
        (:func:`blocked_need_resolution.resolve_blocked_need` returns ``None``), AND
      * no already-shipped COMPLETED foundation satisfies it
        (:func:`blocked_need_resolution.completed_foundation_for_need` returns
        ``None``), AND
      * no durable KnowledgeDocument already holds its substance
        (:func:`blocked_need_resolution.knowledge_substance_satisfies_need` returns
        ``False`` when ``substance_index`` is provided).

    A foundation that is absent, dead, or completed-but-unshipped (ghost merge /
    unmerged) matches NEITHER set, so it is reported. A need a real foundation
    provides — in flight or shipped — is dropped. A need whose substance persists as
    a durable KnowledgeDocument is also dropped: the work is done, even if the
    originating task is CANCELLED. ``pr`` needs are skipped (they name an external
    ref, not a foundation this system commissions). ``active`` / ``completed`` are
    pre-fetched same-project candidate sets; ``failed_parent_ids`` is the
    stranded-parent set used to reject ghost merges; ``substance_index`` is the
    project's knowledge-substance index from
    :func:`knowledge_query.fetch_knowledge_substance_index` (pass ``None`` or omit
    to skip the knowledge check).
    """
    from app.blocked_need_resolution import knowledge_substance_satisfies_need

    unresolved: List[Dict[str, Any]] = []
    for need in get_blocker_needs(task):
        if _demand_identity(need) is None:
            continue
        if resolve_blocked_need(task, need, active) is not None:
            continue
        if (
            completed_foundation_for_need(task, need, completed, failed_parent_ids)
            is not None
        ):
            continue
        if substance_index is not None and knowledge_substance_satisfies_need(
            need, substance_index
        ):
            continue
        unresolved.append(need)
    return unresolved


async def _fetch_candidates(
    session, project_id, statuses, *, since: Optional[datetime] = None, limit: Optional[int] = None
) -> List[Task]:
    """Same-project tasks in the given lifecycle statuses (a candidate set).

    ``since`` recency-bounds the scan (``updated_at >= since``) and ``limit`` caps
    it newest-first, so a long-lived project's unbounded COMPLETED / terminal
    backlog can't be loaded as one fat-row ``select(Task)`` (the digest-path wedge
    of 2026-06-15). In-flight (ACTIVE) candidates pass ``since=None``: an unshipped
    foundation still in flight matters regardless of age, and the active set is
    naturally bounded by fleet concurrency.
    """
    enum_statuses = [TaskStatus(s) for s in statuses]
    stmt = select(Task).where(
        Task.project_id == project_id,
        Task.status.in_(enum_statuses),
    )
    if since is not None:
        stmt = stmt.where(Task.updated_at >= since)
    if limit is not None:
        stmt = stmt.order_by(Task.updated_at.desc()).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def _fetch_recent_giveup_cancelled(
    session, project_id, window_start: datetime
) -> List[Task]:
    """Recently give-up-CANCELLED dependents carrying the unresumable marker.

    Pre-filters cheaply by ``updated_at`` (advances on the CANCELLED transition);
    the caller refines by the marker's own ``at`` timestamp. Branches the JSONB
    key-existence test for SQLite exactly as
    ``digest_metrics.compute_dependency_health_signals`` does — ``has_key`` compiles
    to the Postgres ``?`` operator, which errors on the SQLite smoke gate.
    """
    if resolve_dialect_name(session) == "sqlite":
        marker_present = func.json_extract(
            Task.context, f"$.{UNRESUMABLE_CANCEL_CONTEXT_KEY}"
        ).is_not(None)
    else:
        marker_present = Task.context.has_key(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    return (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.CANCELLED,
                    marker_present,
                    Task.updated_at >= window_start,
                )
                .limit(_CANCELLED_FETCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )


def _new_demand_entry(kind: str, ref: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "label": None,
        "dependent_count": 0,
        "still_blocked": 0,
        "already_cancelled": 0,
        "sample_task_ids": [],
        "direction_ids": set(),
        "oldest_blocked_at": None,
        "max_parked_reevals": 0,
    }


def _fold_dependent(
    entry: Dict[str, Any],
    *,
    task_id: str,
    label: Optional[str],
    direction_id: Optional[str],
    blocked_at_iso: Optional[str],
    parked_reevals: int,
    cancelled: bool,
) -> None:
    """Accumulate one stranded dependent into its demand entry."""
    entry["dependent_count"] += 1
    if cancelled:
        entry["already_cancelled"] += 1
    else:
        entry["still_blocked"] += 1
    if len(entry["sample_task_ids"]) < _SAMPLE_TASK_LIMIT:
        entry["sample_task_ids"].append(task_id)
    if not entry["label"] and label:
        entry["label"] = label
    if direction_id:
        entry["direction_ids"].add(direction_id)
    if blocked_at_iso and (
        entry["oldest_blocked_at"] is None
        or blocked_at_iso < entry["oldest_blocked_at"]
    ):
        entry["oldest_blocked_at"] = blocked_at_iso
    if parked_reevals > entry["max_parked_reevals"]:
        entry["max_parked_reevals"] = parked_reevals


async def collect_missing_capability_demand(
    session,
    project_id,
    *,
    now: Optional[datetime] = None,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> Dict[str, Any]:
    """Aggregate stranded capability/foundation prerequisites into demand entries.

    Scans, within ``project_id``:

      (a) currently BLOCKED tasks — each unresolved foundation need
          (:func:`unresolved_capability_needs`) is demand, counted as
          ``still_blocked``; and
      (b) give-up-CANCELLED dependents stamped with the reaper's
          ``UNRESUMABLE_CANCEL_CONTEXT_KEY`` marker within ``lookback`` whose need is
          a foundation kind — counted as ``already_cancelled``. These are the
          dependents the resume sweep already retired as missing-foundation
          (``never_satisfiable`` on a foundation need carries the explicit
          ``missing_foundation`` flag) or whose foundation died.

    Both are aggregated by normalized ``(kind, ref)`` capability/foundation
    identity. Each demand entry carries the capability ``ref`` + human ``label``,
    ``dependent_count``, a bounded ``sample_task_ids``, the ``direction_ids`` that
    need it, the ``oldest_blocked_at``, the ``max_parked_reevals`` observed, and the
    ``still_blocked`` / ``already_cancelled`` split. Returns ``{"total", "demands"}``
    with the list bounded and largest-demand-first (ties broken by oldest blocker).
    Read-only — no DB writes, no migration.
    """
    now = now or _utcnow()
    window_start = now - lookback
    window_iso = window_start.isoformat()

    candidate_since = now - _CANDIDATE_FETCH_LOOKBACK
    # ACTIVE: in-flight foundations matter regardless of age — LIMIT-only backstop.
    active = await _fetch_candidates(
        session,
        project_id,
        ACTIVE_CANDIDATE_STATUSES,
        limit=_CANDIDATE_FETCH_LIMIT,
    )
    # COMPLETED / terminal: recency + LIMIT bounded so the project's full history
    # is never loaded as one fat-row scan (see _CANDIDATE_FETCH_LOOKBACK).
    completed = await _fetch_candidates(
        session,
        project_id,
        COMPLETED_CANDIDATE_STATUSES,
        since=candidate_since,
        limit=_CANDIDATE_FETCH_LIMIT,
    )
    dead = await _fetch_candidates(
        session,
        project_id,
        TERMINAL_CANDIDATE_STATUSES,
        since=candidate_since,
        limit=_CANDIDATE_FETCH_LIMIT,
    )

    # Stranded-parent set so a ghost-merge / unshipped completed foundation does NOT
    # count as satisfying a need — mirrors ``reaper_sweeps._classify_blocker_needs``.
    failed_parent_ids = {c.id for c in dead}
    failed_parent_ids |= {c.id for c in completed if not has_merge_unblock_signal(c)}

    # Knowledge-substance index: a need whose substance is already captured in a
    # durable KnowledgeDocument is not missing-foundation demand — the work is done.
    from app.knowledge_query import fetch_knowledge_substance_index
    substance_index = await fetch_knowledge_substance_index(session, project_id)

    entries: Dict[tuple, Dict[str, Any]] = {}

    # (a) Live BLOCKED dependents.
    blocked = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.BLOCKED,
                )
                .limit(_BLOCKED_FETCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for task in blocked:
        ctx = task.context or {}
        direction_id = ctx.get("direction_id")
        parked_reevals = 0
        try:
            parked_reevals = int(ctx.get("_blocked_parked_reevals", 0))
        except (TypeError, ValueError):
            parked_reevals = 0
        blocked_at_iso = (
            task.blocked_at.isoformat()
            if isinstance(task.blocked_at, datetime)
            else None
        )
        for need in unresolved_capability_needs(
            task,
            active=active,
            completed=completed,
            failed_parent_ids=failed_parent_ids,
            substance_index=substance_index,
        ):
            identity = _demand_identity(need)
            if identity is None:
                continue
            entry = entries.get(identity)
            if entry is None:
                entry = _new_demand_entry(identity[0], need.get("ref"))
                entries[identity] = entry
            _fold_dependent(
                entry,
                task_id=str(task.id),
                label=need.get("label"),
                direction_id=direction_id,
                blocked_at_iso=blocked_at_iso,
                parked_reevals=parked_reevals,
                cancelled=False,
            )

    # (b) Recently give-up-CANCELLED dependents retired as missing-foundation.
    cancelled_tasks = await _fetch_recent_giveup_cancelled(
        session, project_id, window_start
    )
    for task in cancelled_tasks:
        marker = (task.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
        if not isinstance(marker, dict):
            continue
        at = marker.get("at")
        # ISO-8601 strings from a common naive-UTC source sort lexicographically.
        if not isinstance(at, str) or at < window_iso:
            continue
        need = marker.get("need")
        identity = _demand_identity(need) if isinstance(need, dict) else None
        if identity is None:
            continue
        entry = entries.get(identity)
        if entry is None:
            entry = _new_demand_entry(identity[0], need.get("ref"))
            entries[identity] = entry
        _fold_dependent(
            entry,
            task_id=str(task.id),
            label=need.get("label"),
            direction_id=marker.get("direction_id"),
            # The structured blocker (and blocked_at) is cleared on the cancel
            # transition; the marker's cancel time is the best available age proxy.
            blocked_at_iso=at,
            parked_reevals=0,
            cancelled=True,
        )

    demands = []
    for entry in entries.values():
        entry["direction_ids"] = sorted(entry["direction_ids"])
        demands.append(entry)
    # Largest demand first; ties broken by the oldest stranded dependent so the
    # most-aged stranding surfaces ahead of a fresher one of equal size.
    demands.sort(
        key=lambda e: (-e["dependent_count"], e["oldest_blocked_at"] or "")
    )
    return {"total": len(demands), "demands": demands[:_DEMAND_LIST_LIMIT]}


async def fetch_missing_demand_for_spawn(
    session,
    project_id,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Read-only, fail-open wrapper around :func:`collect_missing_capability_demand`.

    Spawn paths (autonomous research, strategic design-doc/review) call this to
    surface aggregated missing-foundation demand WITHOUT risking the spawn itself:
    any error computing the signal is swallowed and an empty
    ``{"total": 0, "demands": []}`` shape is returned, so the caller can inject the
    result unconditionally and simply omit the payload field when ``demands`` is
    empty. This never writes to the DB and never bypasses the caller's own
    dedup/cooldown/circuit-breaker guards — it is advisory context only.
    """
    try:
        return await collect_missing_capability_demand(session, project_id, now=now)
    except Exception as exc:  # noqa: BLE001 — advisory signal must not break spawns
        logger.warning(
            "fetch_missing_demand_for_spawn failed for project %s: %s — "
            "omitting missing-foundation demand from this spawn",
            project_id,
            exc,
        )
        return {"total": 0, "demands": []}


def missing_demand_prompt_hint(
    demand: Optional[Dict[str, Any]],
    registry_names: Optional[List[str]] = None,
) -> Optional[str]:
    """Build an advisory prompt suffix describing missing-foundation demand.

    Returns ``None`` when there is no demand so callers omit the hint (and the
    payload field) entirely on the empty path. The hint frames the aggregated
    demand as a decision to act on — commission the missing foundation capability
    OR record an explicit retirement of the stranded dependents — without
    prescribing either. The top :data:`_HINT_ENTRY_LIMIT` demands are named inline;
    the full bounded list rides along in the ``missing_capability_demand`` field.

    ``registry_names`` is the optional strategist-curated capability vocabulary
    (canonical kebab-case names from ``problem_model.capabilities``). When a
    capability demand ref corresponds to a registry capability (its normalized
    form matches a registry name), the CANONICAL registry name is shown instead of
    the consumer's free-prose label, so producers and consumers converge on one
    string over time. Omitted/None leaves today's label-preferred behavior.
    """
    if not demand:
        return None
    demands = demand.get("demands") or []
    if not demands:
        return None
    registry_set = {n for n in (registry_names or []) if isinstance(n, str)}
    lines: List[str] = []
    for entry in demands[:_HINT_ENTRY_LIMIT]:
        ref = entry.get("label") or entry.get("ref") or "(unnamed)"
        kind = entry.get("kind") or "capability"
        # Vocabulary convergence: when this capability demand maps onto a curated
        # registry capability, name it by its canonical registry name.
        if kind == "capability" and registry_set:
            _canonical = normalize_capability_name(entry.get("ref"))
            if _canonical and _canonical in registry_set:
                ref = _canonical
        dependent_count = entry.get("dependent_count", 0)
        still_blocked = entry.get("still_blocked", 0)
        already_cancelled = entry.get("already_cancelled", 0)
        lines.append(
            f"- {kind}:{ref} — {dependent_count} stranded dependent(s) "
            f"({still_blocked} still blocked, {already_cancelled} already cancelled)"
        )
    total = demand.get("total", len(demands))
    header = (
        "Missing-foundation demand: the following capability/foundation "
        "prerequisites are declared by dependents but NO task provides them, so "
        "the dependents are stranded. Either propose/create the missing foundation "
        "capability, or explicitly recommend retiring the stranded demand:"
    )
    parts = [header, *lines]
    if total > len(lines):
        parts.append(
            f"(+{total - len(lines)} more — full list in missing_capability_demand)"
        )
    return "\n".join(parts)


def compute_unproven_demanded_capabilities(
    demand_result: dict[str, Any] | None,
    problem_model: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return demand entries whose capability is both demanded AND unproven-working.

    Computes the intersection of two sets:

      * **Demanded capabilities** from ``demand_result`` (the output of
        :func:`collect_missing_capability_demand`): capability-kind demands whose
        ``ref`` normalizes to a canonical name.

      * **Unproven working capabilities** from ``problem_model``: capabilities
        declared ``working`` whose :func:`working_capability_proof_status` returns
        an evidence status other than ``"proven"`` (i.e. ``unproven`` / ``partial``
        / ``unverified`` / ``contradicted``).

    Each returned demand entry is enriched with the additional key
    ``"proof_status"`` so the caller (the earning loop, the reaper sweep, or the
    advisory hint builder) can see exactly what evidence gap remains.

    Returns ``[]`` when either input is empty or absent — never raises.
    """
    from app.capability_registry import (
        normalize_capability_name,
        working_capability_proof_status,
    )

    if not demand_result:
        return []
    demands = demand_result.get("demands") or []
    if not demands or not isinstance(problem_model, dict):
        return []

    # Build the proof-status map for working capabilities only.
    proof_by_name = working_capability_proof_status(problem_model)
    if not proof_by_name:
        return []

    # Unproven working capabilities: evidence status is anything other than
    # "proven" (unproven / partial / unverified / contradicted).
    unproven_working: set[str] = {
        name
        for name, status in proof_by_name.items()
        if status != "proven"
    }
    if not unproven_working:
        return []

    result: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for entry in demands:
        kind = entry.get("kind")
        if kind != "capability":
            continue
        raw_ref = entry.get("ref")
        canonical = normalize_capability_name(raw_ref)
        if canonical is None or canonical not in unproven_working:
            continue
        if canonical in seen_refs:
            continue
        seen_refs.add(canonical)
        enriched = dict(entry)
        enriched["proof_status"] = proof_by_name[canonical]
        result.append(enriched)

    return result
