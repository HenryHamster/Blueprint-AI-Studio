"""Utilization-triggered evergreen backlog auto-fill (direction d-2877ddc7).

Phase-0, defaulted OFF. This module is the additive idle-fill mechanism: when
the fleet is idle (low utilization) or every active direction is parked /
proof-waiting, the orchestrator spawns ONE low-priority research probe from a
curated, durable backlog (:class:`app.models.EvergreenBacklogItem`). It is
purely additive parallelism — it never competes with the release-critical path
and never spawns when the fleet is busy.

The decision policy (:func:`decide_evergreen_fill`) is PURE — it takes a
:class:`FleetSignal` snapshot and returns a :class:`FillDecision` with a
CLOSED-VOCABULARY reason — so the whole decision matrix is unit-testable
without a DB, an orchestrator, or a fleet. The DB/Task-creating helpers below
build that snapshot and act on a CREATE decision.

Hard invariants:

* NO direct fix/create spawn path. The only Task this module creates is a
  low-priority ``research`` probe. Implementation children come ONLY from the
  existing ``research_completion`` surviving-finding gates (reproduce-on-HEAD /
  actionability). :func:`apply_research_outcome` then transitions the ledger row.
* Mode gating: ``off`` never acts; ``observe`` evaluates and records the
  would-be decision but creates nothing; ``enforce`` acts.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List

from sqlalchemy import func as safunc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import (
    EvergreenBacklogItem,
    EvergreenFillDecisionLog,
    TASK_ORIGINATION_EVERGREEN,
    Task,
    TaskStatus,
    _utcnow,
)
from app.time_utils import _utcnow as _now

logger = logging.getLogger(__name__)

# --- Closed vocabularies -----------------------------------------------------

#: Fill modes (runtime config ``evergreen_fill_mode``).
FILL_MODES = frozenset({"off", "observe", "enforce"})

#: Ledger lifecycle statuses.
BACKLOG_STATUSES = frozenset(
    {"enrolled", "spawned", "advanced", "verified_stale", "retired"}
)

#: Decision actions.
DECISION_CREATE = "create"
DECISION_NOOP = "noop"

#: Trigger reasons (why a CREATE fired, or WOULD fire under observe).
TRIGGER_REASONS = frozenset({"low_utilization", "parked_proof_waiting"})

#: No-op reasons (closed vocabulary). Every non-CREATE outcome maps to exactly
#: one of these so observability is enumerable, never free-text.
NOOP_REASONS = frozenset(
    {
        "mode_off",
        "project_ineligible",
        "spend_ceiling",
        "circuit_open",
        "global_rate_limited",
        "busy_queue",
        "high_utilization",
        "no_headroom",
        "active_cap_reached",
        "daily_cap_reached",
        "no_candidate",
        "direction_conflict",
        "duplicate_lineage",
        "observe_mode",
    }
)

#: Union of every reason string a :class:`FillDecision` may carry.
DECISION_REASONS = TRIGGER_REASONS | NOOP_REASONS

#: Low-priority sentinel for evergreen probes. Distinct from the LOW=0 task
#: priority so evergreen idle-fill probes are dispatched strictly after genuine
#: low-priority work yet remain identifiable in the queue. The acceptance
#: contract pins this at exactly 10.
EVERGREEN_PROBE_PRIORITY = 10

#: Context / payload markers carried by every evergreen probe.
EVERGREEN_FILL_FLAG = "evergreen_fill"
EVERGREEN_ITEM_CONTEXT_KEY = "evergreen_backlog_item_id"


# --- Pure decision policy ----------------------------------------------------


@dataclass(frozen=True)
class FleetSignal:
    """Immutable snapshot of everything the decision policy needs.

    Built by the orchestrator hook from the fleet-utilization snapshot, the
    dispatchable-queue depth, ledger counts, and the project/global guards.
    Pure inputs only — no DB handle, no side effects.
    """

    mode: str
    util_now: float | None
    util_threshold: float
    dispatchable_queue_depth: int
    vm_slots_available: int | None
    min_free_slots: int
    active_evergreen_probes: int
    max_active: int
    daily_spawn_count: int
    daily_cap: int
    has_candidate: bool
    parked_proof_waiting: bool = False
    project_eligible: bool = True
    spend_ceiling_reached: bool = False
    circuit_open: bool = False
    global_rate_limited: bool = False
    candidate_conflict: bool = False
    duplicate_lineage: bool = False


@dataclass(frozen=True)
class FillDecision:
    """Result of :func:`decide_evergreen_fill` — a closed-vocabulary verdict."""

    action: str  # DECISION_CREATE | DECISION_NOOP
    reason: str  # one of DECISION_REASONS
    trigger: str | None = None  # one of TRIGGER_REASONS when create/would-create

    @property
    def should_create(self) -> bool:
        return self.action == DECISION_CREATE


def _noop(reason: str, trigger: str | None = None) -> FillDecision:
    assert reason in NOOP_REASONS, f"unknown noop reason {reason!r}"
    return FillDecision(action=DECISION_NOOP, reason=reason, trigger=trigger)


def _create(trigger: str) -> FillDecision:
    assert trigger in TRIGGER_REASONS, f"unknown trigger {trigger!r}"
    return FillDecision(action=DECISION_CREATE, reason=trigger, trigger=trigger)


def decide_evergreen_fill(signal: FleetSignal) -> FillDecision:
    """Pure decision policy. Returns a CREATE or NO-OP with a closed reason.

    Precedence is deliberate: mode/project/guard gates first (cheapest, most
    decisive), then the busy-fleet veto, then the trigger evaluation, then the
    capacity/dedup gates, and finally the observe-mode hold. Each isolated
    failing condition yields its own specific reason, which is exactly what the
    acceptance decision-matrix asserts.
    """
    if signal.mode not in FILL_MODES or signal.mode == "off":
        return _noop("mode_off")
    if not signal.project_eligible:
        return _noop("project_ineligible")
    if signal.spend_ceiling_reached:
        return _noop("spend_ceiling")
    if signal.circuit_open:
        return _noop("circuit_open")
    if signal.global_rate_limited:
        return _noop("global_rate_limited")

    # Busy-fleet veto: any dispatchable queued work means the fleet is NOT idle.
    if signal.dispatchable_queue_depth > 0:
        return _noop("busy_queue")

    # Trigger: idle (low utilization) OR all active directions parked/proof-waiting.
    trigger: str | None = None
    if signal.parked_proof_waiting:
        trigger = "parked_proof_waiting"
    elif signal.util_now is not None and signal.util_now < signal.util_threshold:
        trigger = "low_utilization"
    if trigger is None:
        return _noop("high_utilization")

    # Capacity / headroom gates — all must clear before we create.
    if (
        signal.vm_slots_available is not None
        and signal.vm_slots_available < signal.min_free_slots
    ):
        return _noop("no_headroom", trigger)
    if signal.active_evergreen_probes >= signal.max_active:
        return _noop("active_cap_reached", trigger)
    if signal.daily_spawn_count >= signal.daily_cap:
        return _noop("daily_cap_reached", trigger)
    if not signal.has_candidate:
        return _noop("no_candidate", trigger)
    if signal.candidate_conflict:
        return _noop("direction_conflict", trigger)
    if signal.duplicate_lineage:
        return _noop("duplicate_lineage", trigger)

    # Observe mode: everything would have fired, but we create nothing.
    if signal.mode == "observe":
        return _noop("observe_mode", trigger)

    return _create(trigger)


# --- Bounded in-memory observability ----------------------------------------

_RECENT_DECISIONS_MAX = 50


@dataclass
class _FillObservability:
    """Process-local, bounded counters + recent decisions for the admin surface."""

    reason_counts: Dict[str, int] = field(default_factory=dict)
    trigger_counts: Dict[str, int] = field(default_factory=dict)
    created_count: int = 0
    recent: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_DECISIONS_MAX)
    )

    def record(
        self,
        decision: FillDecision,
        *,
        project_id: str | None = None,
        signal: FleetSignal | None = None,
        item_id: str | None = None,
        finding_key: str | None = None,
        ts: datetime | None = None,
    ) -> None:
        self.reason_counts[decision.reason] = (
            self.reason_counts.get(decision.reason, 0) + 1
        )
        if decision.trigger:
            self.trigger_counts[decision.trigger] = (
                self.trigger_counts.get(decision.trigger, 0) + 1
            )
        if decision.should_create:
            self.created_count += 1
        entry: Dict[str, Any] = {
            "action": decision.action,
            "reason": decision.reason,
            "trigger": decision.trigger,
            "project_id": project_id,
            "item_id": item_id,
            "finding_key": finding_key,
            "ts": ts,
        }
        if signal is not None:
            entry.update(
                {
                    "mode": signal.mode,
                    "util_now": signal.util_now,
                    "util_threshold": signal.util_threshold,
                    "dispatchable_queue_depth": signal.dispatchable_queue_depth,
                    "vm_slots_available": signal.vm_slots_available,
                    "active_evergreen_probes": signal.active_evergreen_probes,
                    "daily_spawn_count": signal.daily_spawn_count,
                    "has_candidate": signal.has_candidate,
                }
            )
        self.recent.append(entry)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "created_count": self.created_count,
            "reason_counts": dict(self.reason_counts),
            "trigger_counts": dict(self.trigger_counts),
            "recent": list(self.recent),
        }


# Module-global singleton (mirrors the shared_state in-memory-counter pattern).
_OBSERVABILITY = _FillObservability()


def record_decision(
    decision: FillDecision,
    *,
    project_id: str | None = None,
    signal: FleetSignal | None = None,
    item_id: str | None = None,
    finding_key: str | None = None,
    ts: datetime | None = None,
) -> None:
    _OBSERVABILITY.record(
        decision,
        project_id=project_id,
        signal=signal,
        item_id=item_id,
        finding_key=finding_key,
        ts=ts,
    )


def get_fill_observability() -> Dict[str, Any]:
    return _OBSERVABILITY.snapshot()


def _reset_observability_for_tests() -> None:
    """Test helper — clear the process-local counters."""
    global _OBSERVABILITY
    _OBSERVABILITY = _FillObservability()


async def get_fill_observability_from_db(
    session, *, window_hours: int = 24
) -> Dict[str, Any]:
    """Aggregate fill decisions from the durable log over a trailing window.

    Returns the same four-key shape as :func:`get_fill_observability` so the
    existing endpoint and panel keep working unchanged.  Records are ordered
    most-recent-first; the ``recent`` list is capped at
    :data:`_RECENT_DECISIONS_MAX` entries.
    """
    cutoff = _now() - timedelta(hours=window_hours)
    rows = (
        (
            await session.execute(
                select(EvergreenFillDecisionLog)
                .where(EvergreenFillDecisionLog.ts >= cutoff)
                .order_by(EvergreenFillDecisionLog.ts.desc())
            )
        )
        .scalars()
        .all()
    )

    reason_counts: Dict[str, int] = {}
    trigger_counts: Dict[str, int] = {}
    created_count = 0
    recent: list = []

    for row in rows:
        reason_counts[row.reason] = reason_counts.get(row.reason, 0) + 1
        if row.trigger:
            trigger_counts[row.trigger] = trigger_counts.get(row.trigger, 0) + 1
        if row.action == DECISION_CREATE:
            created_count += 1
        if len(recent) < _RECENT_DECISIONS_MAX:
            recent.append(
                {
                    "action": row.action,
                    "reason": row.reason,
                    "trigger": row.trigger,
                    "project_id": str(row.project_id) if row.project_id else None,
                    "ts": row.ts.isoformat() if row.ts else None,
                }
            )

    return {
        "created_count": created_count,
        "reason_counts": reason_counts,
        "trigger_counts": trigger_counts,
        "recent": recent,
    }


#: Observe-mode noop reason — rows with this reason represent would-have-fired
#: decisions that observe mode suppressed.
_REASON_OBSERVE_MODE = "observe_mode"

#: Mode string for observe-mode rows.
_MODE_OBSERVE = "observe"


async def compute_observe_mode_verdict(
    session,
    *,
    window_hours: int = 168,
    max_fire_rate_pct: float = 20.0,
    min_evaluations: int = 10,
    daily_cap: int = 3,
) -> Dict[str, Any]:
    """Compute a go/no-go verdict for the observe-mode → enforce-mode transition.

    Reads ``EvergreenFillDecisionLog`` rows in ``observe`` mode within the
    trailing *window_hours* (default 7 days) and computes the **fire rate**:
    the fraction of observe-mode evaluations that would have triggered a spawn
    (``reason == 'observe_mode'``).

    Because observe mode never actually spawns, the daily/active caps never
    engage — so consecutive idle-fleet ticks within a single day all land as
    ``observe_mode`` noops, inflating the raw fire rate toward 100%.  The
    *cap-adjusted* fire rate groups would-have-fired rows by UTC calendar day,
    caps each day's count at *daily_cap*, and uses the capped total as the
    verdict numerator.  The raw ``fire_rate_pct`` is retained for informational
    display.

    Verdict rules (applied to the cap-adjusted rate):

    * ``insufficient_data`` — fewer than *min_evaluations* observe-mode rows in
      the window; cannot draw a meaningful conclusion yet.
    * ``go`` — cap-adjusted fire rate ≤ *max_fire_rate_pct*.
    * ``no_go`` — cap-adjusted fire rate > *max_fire_rate_pct*.

    Returns a dict with keys: ``verdict``, ``verdict_reason``,
    ``observe_evaluations``, ``would_have_fired``, ``fire_rate_pct``,
    ``window_hours``, ``max_fire_rate_pct``, ``min_evaluations``,
    ``daily_cap``, ``cap_adjusted_would_have_fired``,
    ``cap_adjusted_fire_rate_pct``.
    """
    cutoff = _now() - timedelta(hours=window_hours)
    rows = (
        (
            await session.execute(
                select(EvergreenFillDecisionLog)
                .where(EvergreenFillDecisionLog.mode == _MODE_OBSERVE)
                .where(EvergreenFillDecisionLog.ts >= cutoff)
            )
        )
        .scalars()
        .all()
    )

    total = len(rows)
    would_have_fired = sum(1 for r in rows if r.reason == _REASON_OBSERVE_MODE)

    # Group would-have-fired rows by UTC calendar day and cap each day at
    # daily_cap.  Observe mode never creates tasks, so the real daily cap never
    # engages — without this adjustment, a fleet idle all day produces
    # would_have_fired == total (100% raw rate) regardless of actual cadence.
    daily_fire_counts: Dict[Any, int] = defaultdict(int)
    for r in rows:
        if r.reason == _REASON_OBSERVE_MODE:
            day = r.ts.date() if r.ts else None
            daily_fire_counts[day] += 1
    cap_adjusted_would_have_fired = sum(
        min(count, daily_cap) for count in daily_fire_counts.values()
    )

    if total < min_evaluations:
        verdict = "insufficient_data"
        verdict_reason = (
            f"only {total} observe-mode evaluation(s) in the last {window_hours}h "
            f"(need ≥{min_evaluations})"
        )
        fire_rate_pct = None
        cap_adjusted_fire_rate_pct = None
    else:
        fire_rate_pct = round(100.0 * would_have_fired / total, 2)
        cap_adjusted_fire_rate_pct = round(
            100.0 * cap_adjusted_would_have_fired / total, 2
        )
        if cap_adjusted_fire_rate_pct <= max_fire_rate_pct:
            verdict = "go"
            verdict_reason = (
                f"cap-adjusted fire rate {cap_adjusted_fire_rate_pct}% is within "
                f"threshold {max_fire_rate_pct}% "
                f"({cap_adjusted_would_have_fired}/{total} cap-adjusted "
                f"evaluations would have fired; daily_cap={daily_cap})"
            )
        else:
            verdict = "no_go"
            verdict_reason = (
                f"cap-adjusted fire rate {cap_adjusted_fire_rate_pct}% exceeds "
                f"threshold {max_fire_rate_pct}% "
                f"({cap_adjusted_would_have_fired}/{total} cap-adjusted "
                f"evaluations would have fired; daily_cap={daily_cap})"
            )

    return {
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "observe_evaluations": total,
        "would_have_fired": would_have_fired,
        "fire_rate_pct": fire_rate_pct,
        "window_hours": window_hours,
        "max_fire_rate_pct": max_fire_rate_pct,
        "min_evaluations": min_evaluations,
        "daily_cap": daily_cap,
        "cap_adjusted_would_have_fired": cap_adjusted_would_have_fired,
        "cap_adjusted_fire_rate_pct": cap_adjusted_fire_rate_pct,
    }


# --- Enrollment / dedup ------------------------------------------------------


async def enroll_backlog_item(
    session,
    *,
    project_id: uuid.UUID,
    finding_key: str,
    title: str,
    detail: str | None = None,
    direction_id: str | None = None,
    provenance: str = "manual",
) -> EvergreenBacklogItem | None:
    """Enroll a candidate, deduped by (project_id, finding_key).

    Idempotent: re-enrolling an existing finding_key returns the existing row
    (a retired row is RE-OPENED to ``enrolled`` so operators can requeue a gap).
    Returns ``None`` only on an unrecoverable race.
    """
    finding_key = (finding_key or "").strip()
    if not finding_key:
        raise ValueError("finding_key must be non-empty")

    existing = (
        await session.execute(
            select(EvergreenBacklogItem).where(
                EvergreenBacklogItem.project_id == project_id,
                EvergreenBacklogItem.finding_key == finding_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == "retired":
            existing.status = "enrolled"
            existing.outcome = None
            existing.updated_at = _utcnow()
            await session.flush()
        return existing

    item = EvergreenBacklogItem(
        project_id=project_id,
        finding_key=finding_key,
        title=(title or finding_key)[:512],
        detail=detail,
        direction_id=direction_id,
        provenance=provenance,
        status="enrolled",
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent enroll won the unique race — return the durable winner.
        await session.rollback()
        return (
            await session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project_id,
                    EvergreenBacklogItem.finding_key == finding_key,
                )
            )
        ).scalar_one_or_none()
    return item


async def list_backlog_items(
    session,
    *,
    project_id: uuid.UUID,
    status: str | None = None,
    limit: int = 100,
) -> List[EvergreenBacklogItem]:
    stmt = select(EvergreenBacklogItem).where(
        EvergreenBacklogItem.project_id == project_id
    )
    if status is not None:
        stmt = stmt.where(EvergreenBacklogItem.status == status)
    stmt = stmt.order_by(EvergreenBacklogItem.created_at.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def backlog_summary(session, *, project_id: uuid.UUID) -> Dict[str, Any]:
    """Per-status counts for the admin backlog-summary surface."""
    rows = (
        await session.execute(
            select(EvergreenBacklogItem.status, safunc.count())
            .where(EvergreenBacklogItem.project_id == project_id)
            .group_by(EvergreenBacklogItem.status)
        )
    ).all()
    counts = dict.fromkeys(sorted(BACKLOG_STATUSES), 0)
    for status, count in rows:
        counts[status] = int(count)
    return {"project_id": str(project_id), "counts": counts}


# --- Candidate selection / caps / conflict ----------------------------------


async def select_candidate(
    session, *, project_id: uuid.UUID, max_attempts: int = 3
) -> EvergreenBacklogItem | None:
    """Oldest enrolled candidate that still has attempts left."""
    stmt = (
        select(EvergreenBacklogItem)
        .where(
            EvergreenBacklogItem.project_id == project_id,
            EvergreenBacklogItem.status == "enrolled",
            EvergreenBacklogItem.attempts < max_attempts,
        )
        .order_by(EvergreenBacklogItem.created_at.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def count_active_probes(session, *, project_id: uuid.UUID) -> int:
    """Backlog items with a live probe (status == spawned)."""
    return int(
        (
            await session.execute(
                select(safunc.count())
                .select_from(EvergreenBacklogItem)
                .where(
                    EvergreenBacklogItem.project_id == project_id,
                    EvergreenBacklogItem.status == "spawned",
                )
            )
        ).scalar()
        or 0
    )


def _evergreen_key_prefix(project_id: uuid.UUID) -> str:
    return f"evergreen-fill:{project_id}:%"


async def count_recent_spawns(
    session, *, project_id: uuid.UUID, since: datetime
) -> int:
    """Evergreen probe Tasks created for this project since ``since`` (daily cap).

    Counts on the indexed ``idempotency_key`` string column (every evergreen
    probe carries an ``evergreen-fill:<project>:<item>`` key) — dialect-safe
    across Postgres and the SQLite test backend, unlike JSON-path predicates.
    """
    return int(
        (
            await session.execute(
                select(safunc.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.created_at >= since,
                    Task.idempotency_key.like(_evergreen_key_prefix(project_id)),
                )
            )
        ).scalar()
        or 0
    )


async def has_direction_conflict(
    session, *, project_id: uuid.UUID, direction_id: str | None
) -> bool:
    """True when the candidate's direction already has live (non-evergreen) work.

    Evergreen fill must not pile onto a direction that the strategist is already
    actively driving — that would duplicate the lane the direction owns.
    """
    if not direction_id:
        return False
    active_statuses = (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
    )
    count = int(
        (
            await session.execute(
                select(safunc.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(active_statuses),
                    Task.context["direction_id"].as_string() == direction_id,
                    # Don't let our own evergreen probes count as a conflict —
                    # they carry an evergreen-fill idempotency key.
                    (Task.idempotency_key.is_(None))
                    | (~Task.idempotency_key.like(_evergreen_key_prefix(project_id))),
                )
            )
        ).scalar()
        or 0
    )
    return count > 0


async def has_duplicate_lineage(
    session, *, project_id: uuid.UUID, item: EvergreenBacklogItem
) -> bool:
    """True when an active task already carries this item's title or item id.

    Covers both the title-dedup and idempotency arms: if a probe for this ledger
    row (or a same-titled active task) already exists, don't spawn another.
    """
    active_statuses = (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
    )
    # Idempotency arm: an active probe already claims this exact ledger row.
    count = int(
        (
            await session.execute(
                select(safunc.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(active_statuses),
                    Task.idempotency_key
                    == evergreen_fill_idempotency_key(project_id, item.id),
                )
            )
        ).scalar()
        or 0
    )
    if count > 0:
        return True
    title_count = int(
        (
            await session.execute(
                select(safunc.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(active_statuses),
                    Task.title == _probe_title(item),
                )
            )
        ).scalar()
        or 0
    )
    return title_count > 0


# --- Task-creating path (the ONLY spawn this module performs) ----------------


def evergreen_fill_idempotency_key(project_id: uuid.UUID, item_id: uuid.UUID) -> str:
    """Idempotency key claiming ONE active evergreen probe per ledger row.

    Combined with the partial unique index ``ux_task_active_idempotency_key``
    (non-NULL keys in active statuses), two concurrent ticks can never both
    insert a probe for the same item — the loser hits IntegrityError and skips.
    """
    return f"evergreen-fill:{project_id}:{item_id}"


def _probe_title(item: EvergreenBacklogItem) -> str:
    return f"Evergreen research: {item.title}"


def build_probe_payload(
    item: EvergreenBacklogItem,
    *,
    base_branch: str,
    model: str | None = None,
    effort: str | None = None,
) -> Dict[str, Any]:
    prompt = (
        f"Evergreen idle-fill research probe for backlog item {item.finding_key}.\n\n"
        f"{item.title}\n\n{item.detail or ''}\n\n"
        "This is a LOW-priority background probe. Investigate whether the gap "
        "still reproduces on HEAD and produce an actionable, code-verified "
        "finding. Implementation work is spawned ONLY if your finding survives "
        "the existing reproduce-on-HEAD / actionability gates — do not request "
        "fix/create work directly."
    )
    payload: Dict[str, Any] = {
        "type": "research",
        EVERGREEN_FILL_FLAG: True,
        "base_branch": base_branch,
        "prompt": prompt,
    }
    if model:
        payload["model"] = model
    if effort:
        payload["effort"] = effort
    return payload


async def spawn_evergreen_probe(
    session,
    *,
    project_id: uuid.UUID,
    item: EvergreenBacklogItem,
    base_branch: str,
    model: str | None = None,
    effort: str | None = None,
) -> Task | None:
    """Create exactly ONE low-priority research probe for ``item``.

    Marks the ledger row ``spawned``, links ``spawned_task_id``, bumps
    ``attempts``. Returns the Task, or ``None`` if the idempotency claim lost a
    race (another tick already has a live probe for this row).
    """
    from app.task_locking import (
        AUTOMATIC_TASK_CREATION_ALLOWED,
        acquire_automatic_task_creation_authority,
    )

    with session.no_autoflush:
        creation_admission = await acquire_automatic_task_creation_authority(
            session,
            project_id,
            require_autonomous_enabled=True,
        )
    if creation_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
        logger.info(
            "[EVERGREEN] Automatic probe creation held for item %s: %s",
            item.id,
            creation_admission,
        )
        return None

    task = Task(
        project_id=project_id,
        title=_probe_title(item),
        description=item.detail or item.title,
        payloads=[
            build_probe_payload(
                item, base_branch=base_branch, model=model, effort=effort
            )
        ],
        context={
            EVERGREEN_FILL_FLAG: True,
            EVERGREEN_ITEM_CONTEXT_KEY: str(item.id),
            **({"direction_id": item.direction_id} if item.direction_id else {}),
        },
        priority=EVERGREEN_PROBE_PRIORITY,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_EVERGREEN,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        idempotency_key=evergreen_fill_idempotency_key(project_id, item.id),
    )
    session.add(task)
    # Snapshot item.id BEFORE the flush. The idempotency-race rollback below
    # expires every session-bound ORM instance (rollback expires regardless of
    # expire_on_commit=False), so reading item.id in the skip-log afterwards
    # would fire a sync lazy-load inside the AsyncSession -> MissingGreenlet,
    # propagating instead of the intended `return None`.
    _item_id = item.id
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        logger.info(
            "[EVERGREEN] idempotency race spawning probe for item %s — skipping",
            _item_id,
        )
        return None

    item.status = "spawned"
    item.spawned_task_id = task.id
    item.attempts = (item.attempts or 0) + 1
    item.updated_at = _utcnow()
    await session.flush()
    return task


# --- research_completion adapter --------------------------------------------


async def apply_research_outcome(
    session,
    *,
    item_id: str,
    impl_spawned: int,
    reproduce_rejected: int,
    surviving: int,
    environmental_failure: bool = False,
    verified_against_sha: str | None = None,
    max_attempts: int = 3,
) -> str | None:
    """Transition the backlog row from its probe's research outcome.

    Outcomes (returns the new status, or ``None`` if the row is missing):

    * surviving findings spawned children  → ``advanced``
    * findings rejected / non-actionable   → ``verified_stale``
    * environmental failure, attempts left → ``enrolled`` (requeued)
    * environmental failure, exhausted     → ``retired``
    """
    try:
        item_uuid = uuid.UUID(str(item_id))
    except (ValueError, TypeError):
        return None
    item = (
        await session.execute(
            select(EvergreenBacklogItem).where(EvergreenBacklogItem.id == item_uuid)
        )
    ).scalar_one_or_none()
    if item is None:
        return None

    if environmental_failure:
        if (item.attempts or 0) < max_attempts:
            item.status = "enrolled"
            item.outcome = "requeued_environmental"
        else:
            item.status = "retired"
            item.outcome = "retired_attempts_exhausted"
    elif impl_spawned > 0:
        item.status = "advanced"
        item.outcome = "children_spawned"
    else:
        # No surviving/actionable finding: the gap did not reproduce or was not
        # actionable on HEAD. Retire as verified-stale (NO speculative fix path).
        item.status = "verified_stale"
        item.outcome = (
            "reproduce_absent" if reproduce_rejected > 0 else "non_actionable"
        )
    if verified_against_sha:
        item.verified_against_sha = verified_against_sha[:64]
    item.updated_at = _utcnow()
    await session.flush()
    return item.status


# --- Orchestrator hook (throttled sibling of _check_idle_research) -----------


def _directions_parked_or_proof_waiting(project) -> bool:
    """Heuristic: the project has directions but none are actively progressing.

    Parked / proof-waiting holding states (``paused``/``blocked``/``deferred``/
    ``proposed``) mean the strategist's lanes are stalled — a legitimate trigger
    to keep the curated backlog moving without competing for the active lanes.
    Returns False when there is at least one ``active`` direction (the fleet is
    being driven) or no directions at all (idle handled by the util trigger).
    """
    directions = project.directions or []
    if not directions:
        return False
    has_active = any(d.get("status") == "active" for d in directions)
    return not has_active


async def check_evergreen_fill() -> None:
    """Throttled idle-fill sibling. Spawns at most one probe per eligible project.

    Mirrors ``_check_idle_research``'s throttle + per-project loop. Defaulted OFF
    via ``evergreen_fill_mode``; in ``observe`` it records the would-be decision
    but creates nothing. Imports are lazy to avoid import cycles with the
    orchestrator / autonomous_research modules.
    """
    from app import shared_state as _shared_state
    from app.shared_state import runtime_config as _runtime_config
    from app.database import AsyncSessionLocal
    from app.models import Project

    mode = str(_runtime_config.get("evergreen_fill_mode", "off")).lower()
    if mode not in FILL_MODES or mode == "off":
        return

    now = _utcnow()
    interval_minutes = int(_runtime_config.get("evergreen_fill_interval_minutes", 15))
    if _shared_state.last_evergreen_check and (
        now - _shared_state.last_evergreen_check
    ) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_evergreen_check = now

    util_threshold = float(_runtime_config.get("evergreen_fill_util_threshold", 0.5))
    min_free_slots = int(_runtime_config.get("evergreen_fill_min_free_slots", 2))
    max_active = int(_runtime_config.get("evergreen_fill_max_active", 1))
    daily_cap = int(_runtime_config.get("evergreen_fill_daily_cap", 3))
    max_global_per_hour = int(
        _runtime_config.get("max_global_autonomous_tasks_per_hour", 500)
    )

    try:
        async with AsyncSessionLocal() as session:
            from app.fleet_utilization import get_fleet_utilization_snapshot
            from app.branch_resolution import (
                get_project_integration_branch as _get_project_integration_branch,
            )

            snapshot = await get_fleet_utilization_snapshot(session)
            util_now = snapshot.get("util_now")
            vm_slots_available = snapshot.get("vm_slots_available")

            # Fleet-level dispatchable queue depth (QUEUED, non-approval, non-evergreen).
            dispatchable_queue_depth = int(
                (
                    await session.execute(
                        select(safunc.count())
                        .select_from(Task)
                        .where(
                            Task.status == TaskStatus.QUEUED,
                            Task.requires_approval.is_(False),
                            (Task.idempotency_key.is_(None))
                            | (~Task.idempotency_key.like("evergreen-fill:%")),
                        )
                    )
                ).scalar()
                or 0
            )

            # Global rate guard: evergreen probes spawned across all projects in
            # the trailing hour vs the cross-project autonomous cap.
            hour_ago = now - timedelta(hours=1)
            global_recent = int(
                (
                    await session.execute(
                        select(safunc.count())
                        .select_from(Task)
                        .where(
                            Task.created_at >= hour_ago,
                            Task.idempotency_key.like("evergreen-fill:%"),
                        )
                    )
                ).scalar()
                or 0
            )
            # Tracks probes spawned within this tick so the global cap is
            # re-evaluated on each iteration without an extra DB round-trip.
            tick_spawned = 0

            # Drop heavy JSONB columns that are never read on this path
            # (vision, running_update, problem_model, self_reflection_log, …).
            # Needed: id, directions (parked_proof_waiting), build_config (spend
            # ceiling), integration_branch / is_self_repo / github_repo /
            # promotion_target (branch resolver).
            _project_cols = load_only(
                Project.id,
                Project.directions,
                Project.build_config,
                Project.integration_branch,
                Project.is_self_repo,
                Project.github_repo,
                Project.promotion_target,
                raiseload=True,
            )
            projects = (
                (
                    await session.execute(
                        select(Project)
                        .where(
                            Project.autonomous_enabled.is_(True),
                            Project.paused.is_(False),
                            Project.not_archived(),
                        )
                        .options(_project_cols)
                    )
                )
                .scalars()
                .all()
            )

            day_ago = now - timedelta(hours=24)
            for project in projects:
                # Per-project capacity / dedup inputs.
                active_probes = await count_active_probes(
                    session, project_id=project.id
                )
                daily_count = await count_recent_spawns(
                    session, project_id=project.id, since=day_ago
                )
                candidate = await select_candidate(session, project_id=project.id)

                spend_reason = await _evergreen_spend_ceiling_reason(project, session)
                circuit_open = _evergreen_circuit_open(_shared_state, project)

                candidate_conflict = False
                duplicate_lineage = False
                if candidate is not None:
                    candidate_conflict = await has_direction_conflict(
                        session,
                        project_id=project.id,
                        direction_id=candidate.direction_id,
                    )
                    duplicate_lineage = await has_duplicate_lineage(
                        session, project_id=project.id, item=candidate
                    )

                # Re-evaluate the global cap each iteration using the trailing-hour
                # count plus probes already spawned in this tick.
                global_rate_limited = (
                    global_recent + tick_spawned
                ) >= max_global_per_hour

                signal = FleetSignal(
                    mode=mode,
                    util_now=util_now,
                    util_threshold=util_threshold,
                    dispatchable_queue_depth=dispatchable_queue_depth,
                    vm_slots_available=vm_slots_available,
                    min_free_slots=min_free_slots,
                    active_evergreen_probes=active_probes,
                    max_active=max_active,
                    daily_spawn_count=daily_count,
                    daily_cap=daily_cap,
                    has_candidate=candidate is not None,
                    parked_proof_waiting=_directions_parked_or_proof_waiting(project),
                    project_eligible=True,
                    spend_ceiling_reached=bool(spend_reason),
                    circuit_open=circuit_open,
                    global_rate_limited=global_rate_limited,
                    candidate_conflict=candidate_conflict,
                    duplicate_lineage=duplicate_lineage,
                )
                decision = decide_evergreen_fill(signal)
                candidate_item_id = str(candidate.id) if candidate is not None else None
                candidate_finding_key = (
                    candidate.finding_key if candidate is not None else None
                )
                record_decision(
                    decision,
                    project_id=str(project.id),
                    signal=signal,
                    item_id=candidate_item_id,
                    finding_key=candidate_finding_key,
                    ts=now,
                )
                log_row = EvergreenFillDecisionLog(
                    ts=now,
                    project_id=project.id,
                    action=decision.action,
                    reason=decision.reason,
                    trigger=decision.trigger,
                    mode=signal.mode,
                    util_now=signal.util_now,
                    util_threshold=signal.util_threshold,
                    dispatchable_queue_depth=signal.dispatchable_queue_depth,
                    vm_slots_available=signal.vm_slots_available,
                    active_evergreen_probes=signal.active_evergreen_probes,
                    daily_spawn_count=signal.daily_spawn_count,
                    has_candidate=signal.has_candidate,
                    item_id=candidate.id if candidate is not None else None,
                    finding_key=candidate_finding_key,
                )
                session.add(log_row)

                if not decision.should_create or candidate is None:
                    continue

                base_branch = _get_project_integration_branch(project)
                task = await spawn_evergreen_probe(
                    session,
                    project_id=project.id,
                    item=candidate,
                    base_branch=base_branch,
                )
                if task is not None:
                    tick_spawned += 1
                    await session.commit()
                    logger.info(
                        "[EVERGREEN] spawned probe %s for item %s (trigger=%s, project=%s)",
                        task.id,
                        candidate.id,
                        decision.trigger,
                        project.id,
                    )
            # Commit any decision-log rows for projects where no probe was spawned.
            await session.commit()
    except Exception as exc:  # pragma: no cover - defensive, never crash the loop
        logger.error("[EVERGREEN] check_evergreen_fill error: %s", exc, exc_info=True)


async def _evergreen_spend_ceiling_reason(project, session) -> str | None:
    """Reuse autonomous_research's spend-ceiling defer reason (lazy import)."""
    try:
        from app.autonomous_research import (
            _project_spend_ceiling_spawn_defer_reason,
        )

        return await _project_spend_ceiling_spawn_defer_reason(project, session)
    except Exception:  # noqa: BLE001 — the spend guard must never block idle-fill
        return None


def _evergreen_circuit_open(shared_state, project) -> bool:
    try:
        breaker = shared_state.get_circuit_breaker(str(project.id))
        return bool(breaker and breaker.get("state") == "OPEN")
    except Exception:  # noqa: BLE001 — circuit-breaker read must never crash the loop
        return False
