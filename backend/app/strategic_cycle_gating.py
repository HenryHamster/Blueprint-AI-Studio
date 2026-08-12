"""No-delta strategic cycle gating.

A strategic cycle assembles and re-sends the full strategic context (~137 KB
across ~17 turns) whether or not anything changed since the previous cycle.
For a quiet project that costs the full price for no new information — the
strategist is approximately 47% of all LLM spend (measured baseline: see
docs/strategic-cycle-cost-baseline.md), and every cadence cycle pays for the
assembly regardless of delta.

This module implements the cheap pre-check that runs on the cadence path
BEFORE context assembly and short-circuits the cycle when there is no
meaningful delta. No-delta is defined from durable, already-available signals:

  * new PR merges           (``count_merged_since``)
  * new terminal transitions (completed / failed / cancelled) — excluding the
    strategist's own task types, whose completion is the cycle itself
  * new operator messages   (``ProjectMessage.from_type == 'user'``)
  * release-version lifecycle movement (phase change / promotion / goal edit)

The gate is deliberately conservative. A false "no delta" silently skips real
strategic work, which is far worse than an occasional unnecessary cycle. Every
must-run condition fails CLOSED (run), and every delta signal RUNS on
ambiguity. When signals are ambiguous, RUN.

Skipped cycles are recorded in ``project.vision`` (a counter plus a bounded
log) so they are visible and countable rather than looking like the strategist
silently stopped working. The watermark — the most recent cycle's
``phase1_started_at`` — is DERIVED from the StrategicCycle table and never
advances on a skip, so a delta that arrives mid-check is caught on the next
tick.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select

from app import strategic_cycle as strategic_cycle_mod
from app.database import resolve_dialect_name
from app.message_convergence import OPEN_STATUS
from app.models import (
    Project,
    ProjectMessage,
    ReleaseVersion,
    StrategicCycle,
    Task,
    TaskStatus,
)
from app.problem_model import project_has_purpose
from app.release_version import count_merged_since
from app.strategic_authority_locks import commit_legacy_authorized
from app.strategic_trigger import strategic_refresh_pending
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_locking import with_vision_locked
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# StrategicCycle outcomes that mean the cycle itself failed or stalled. A cycle
# spawned right after one of these is a recovery decision, never routine.
# Mirrors strategic_spawning._FAILED_CYCLE_OUTCOMES (rebuilt here from the
# cycle-outcome constants so the cheap cadence path never has to import the
# huge spawning module).
_FAILED_CYCLE_OUTCOMES = frozenset(
    {
        strategic_cycle_mod.OUTCOME_FAILED_PHASE1,
        strategic_cycle_mod.OUTCOME_FAILED_PHASE2,
        strategic_cycle_mod.OUTCOME_STALLED_PHASE1,
        strategic_cycle_mod.OUTCOME_STALLED_PHASE2,
        strategic_cycle_mod.OUTCOME_FAILED_PHASE2_SPAWN,
        strategic_cycle_mod.OUTCOME_THINKER_ARTIFACTS_MISSING,
    }
)

# Bound on the vision skip log so a repeatedly-quiet project cannot grow vision
# without bound. The skip COUNTER (_strategic_no_delta_skip_count) is unbounded
# by design — it is a cheap integer and exists to make skips countable.
_MAX_SKIP_LOG = 50


@dataclass
class CycleDeltaSignals:
    """Durable signals that changed since the last-cycle watermark.

    ``open_user_messages`` is NOT a delta signal — it is a persistent state
    (unanswered operator intent) that feeds the must-run check. The rest are
    deltas: anything nonzero since the watermark is new information the last
    cycle could not have reasoned about.
    """

    new_merges: int = 0
    new_completions: int = 0
    new_failures: int = 0
    new_user_messages: int = 0
    open_user_messages: int = 0
    version_phase_changed: bool = False

    @property
    def has_delta(self) -> bool:
        return (
            self.new_merges > 0
            or self.new_completions > 0
            or self.new_failures > 0
            or self.new_user_messages > 0
            or self.version_phase_changed
        )

    def as_dict(self) -> dict:
        return {
            "new_merges": self.new_merges,
            "new_completions": self.new_completions,
            "new_failures": self.new_failures,
            "new_user_messages": self.new_user_messages,
            "open_user_messages": self.open_user_messages,
            "version_phase_changed": self.version_phase_changed,
        }


@dataclass
class CycleGateDecision:
    """Outcome of the no-delta gate for one cadence tick."""

    skip: bool
    watermark: datetime | None
    signals: CycleDeltaSignals
    must_run_reasons: list[str]
    reason: str | None = None


def _active_directions(project: Project) -> list[dict]:
    return [
        d for d in (project.directions or []) if d.get("status") == "active"
    ]


def must_run_reasons(
    project: Project,
    last_outcome: str | None,
    open_user_messages: int,
    vision: dict | None,
) -> list[str]:
    """Fail-closed reasons a cadence tick must run regardless of delta.

    Returns a list of reason slugs. An EMPTY list means nothing structurally
    demands a cycle this tick — the gate may consult the delta signals.
    """
    reasons: list[str] = []
    if last_outcome is None:
        reasons.append("bootstrap:no_prior_cycle")
    if strategic_refresh_pending(vision):
        reasons.append("pending_strategic_refresh")
    if (vision or {}).get("_pending_strategic_review"):
        reasons.append("pending_strategic_review_retry")
    if not project_has_purpose(project):
        reasons.append("project_lacks_purpose")
    active_dirs = _active_directions(project)
    if not active_dirs:
        reasons.append("no_active_directions")
    elif all(d.get("possibly_saturated") for d in active_dirs):
        reasons.append("all_directions_saturated")
    if last_outcome in _FAILED_CYCLE_OUTCOMES:
        reasons.append("prior_cycle_failed")
    if open_user_messages > 0:
        reasons.append("open_operator_message")
    return reasons


async def latest_cycle_watermark(
    session, project_id: uuid.UUID
) -> tuple[datetime | None, str | None]:
    """Return (watermark, outcome) for the most recent StrategicCycle row.

    The watermark is that cycle's ``phase1_started_at`` — the last time the
    strategist began assembling context. It is DERIVED from the table (never
    written by a skip), so skipping never advances it and a delta that arrives
    mid-check is caught on the next tick. ``None`` means no prior cycle
    (bootstrap).
    """
    row = (
        await session.execute(
            select(StrategicCycle)
            .where(StrategicCycle.project_id == project_id)
            .order_by(StrategicCycle.phase1_started_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None, None
    return row.phase1_started_at, row.outcome


def _not_strategic_type_predicate(
    session, dialect_name: str | None = None
) -> ColumnElement[bool]:
    """Portable SQL predicate for task payload types outside the strategist's own.

    The strategist's own task types (design_doc_update / strategic_review) are
    excluded from the terminal-transition delta because a completed one IS the
    cycle itself. The payload type lives in ``Task.payloads[0]["type"]``, which
    has no portable JSON extraction on SQLite: ``.as_string()`` renders the
    JSONB accessor through ``JSON_QUOTE(JSON_EXTRACT(...))`` (Postgres-oriented)
    while the portable path uses ``json_extract`` directly. Resolve the dialect
    once with ``resolve_dialect_name`` — the same single dialect-handling path
    the efficiency snapshot uses — rather than duplicating dialect logic here.
    ``dialect_name`` is an optional override so the SQLite branch is directly
    testable independent of the engine a test session happens to be bound to.
    """
    if dialect_name is None:
        dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        payload_type = func.json_extract(Task.payloads, "$[0].type")
    else:
        payload_type = Task.payloads[0]["type"].as_string()
    return ~payload_type.in_(sorted(STRATEGIC_TASK_TYPES))


async def compute_cycle_delta_signals(
    session, project_id: uuid.UUID, watermark: datetime | None
) -> CycleDeltaSignals:
    """Count durable, already-available deltas since ``watermark``.

    All queries are indexed count scans on a single project — orders of
    magnitude cheaper than the strategic context assembly they guard.
    """
    signals = CycleDeltaSignals()
    if watermark is None:
        # Bootstrap: nothing to compare against. The must-run check already
        # forces a cycle, so skip the delta queries entirely.
        return signals

    signals.new_merges = await count_merged_since(session, project_id, watermark)

    # Non-strategic terminal transitions since the watermark. The strategist's
    # own task types are excluded: a completed design_doc_update /
    # strategic_review IS the cycle itself, and counting it would make every
    # completed cycle guarantee the next check sees a "delta".
    _not_strategic_type = _not_strategic_type_predicate(session)
    signals.new_completions = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.completed_at >= watermark,
                    _not_strategic_type,
                )
            )
        ).scalar()
        or 0
    )
    # FAILED/CANCELLED tasks do not stamp completed_at; the terminal transition
    # is what bumps updated_at, and terminal rows stop heartbeating so the value
    # is stable. Over-counting (a post-terminal write bumping updated_at) is
    # harmless: it errs toward RUN, never toward a false skip.
    signals.new_failures = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                    Task.updated_at >= watermark,
                    _not_strategic_type,
                )
            )
        ).scalar()
        or 0
    )

    # Operator messages: created since the watermark (a delta) plus the number
    # currently open (a must-run state — the strategist payload feeds on open
    # messages, so unanswered operator intent demands a cycle).
    signals.new_user_messages = int(
        (
            await session.execute(
                select(func.count(ProjectMessage.id)).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.created_at >= watermark,
                )
            )
        ).scalar()
        or 0
    )
    signals.open_user_messages = int(
        (
            await session.execute(
                select(func.count(ProjectMessage.id)).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.status == OPEN_STATUS,
                )
            )
        ).scalar()
        or 0
    )

    # Release-version lifecycle movement since the watermark: a version opened,
    # a phase transition (build -> stabilize), a promotion, or a goal edit.
    signals.version_phase_changed = bool(
        (
            await session.execute(
                select(func.count(ReleaseVersion.id)).where(
                    ReleaseVersion.project_id == project_id,
                    or_(
                        ReleaseVersion.opened_at >= watermark,
                        ReleaseVersion.phase_changed_at >= watermark,
                        ReleaseVersion.promoted_at >= watermark,
                        ReleaseVersion.goal_updated_at >= watermark,
                    ),
                )
            )
        ).scalar()
        or 0
    )
    return signals


async def should_skip_strategic_cycle(
    session, project: Project
) -> CycleGateDecision:
    """Decide whether the cadence tick can skip the expensive context assembly.

    Conservative ordering: any must-run condition immediately RUNS; only when
    nothing structurally demands a cycle do the delta signals get consulted.
    All comparisons are against timestamps stored in the DB (the watermark and
    the durable signal rows), so the decision is deterministic under a given
    database state — no wall-clock is consulted on the decision path.
    """
    watermark, last_outcome = await latest_cycle_watermark(session, project.id)
    signals = await compute_cycle_delta_signals(session, project.id, watermark)
    must_run = must_run_reasons(
        project, last_outcome, signals.open_user_messages, project.vision or {}
    )
    if must_run:
        return CycleGateDecision(
            skip=False,
            watermark=watermark,
            signals=signals,
            must_run_reasons=must_run,
            reason="must_run",
        )
    if signals.has_delta:
        return CycleGateDecision(
            skip=False,
            watermark=watermark,
            signals=signals,
            must_run_reasons=[],
            reason="delta",
        )
    return CycleGateDecision(
        skip=True,
        watermark=watermark,
        signals=signals,
        must_run_reasons=[],
        reason="no_meaningful_delta_since_last_cycle",
    )


async def record_no_delta_skip(
    session,
    project_id: uuid.UUID,
    decision: CycleGateDecision,
    *,
    expected_legacy_generation: int | None = None,
) -> None:
    """Persist a cheap, auditable skip record on ``project.vision``.

    Writes a bounded log (``_strategic_no_delta_skips``, newest last) and an
    unbounded counter (``_strategic_no_delta_skip_count``). Caller must hold
    the project's spawn lock; this acquires the project row lock itself and
    commits.
    """
    if not decision.skip:
        return
    entry = {
        "skipped_at": _utcnow().isoformat(),
        "watermark": decision.watermark.isoformat() if decision.watermark else None,
        "signals": decision.signals.as_dict(),
    }

    def _mutate(vision: dict) -> None:
        vision["_strategic_no_delta_skip_count"] = (
            vision.get("_strategic_no_delta_skip_count", 0) + 1
        )
        skips = vision.get("_strategic_no_delta_skips") or []
        if not isinstance(skips, list):
            skips = []
        skips = skips[-_MAX_SKIP_LOG + 1 :]  # keep newest, cap at _MAX_SKIP_LOG
        skips.append(entry)
        vision["_strategic_no_delta_skips"] = skips

    await with_vision_locked(session, project_id, _mutate)
    await commit_legacy_authorized(session, project_id, expected_legacy_generation)
