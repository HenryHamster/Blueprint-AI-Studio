"""Resident strategic session lifecycle + agent-token heartbeat router.

The resident strategic agent (see docs/plans/resident-strategic-agent.md) is a
long-lived ``strategic_review`` task that stays IN_PROGRESS for the whole
operator session, pinned to one runner, chatting with the operator and doing
autonomous strategic work between turns. This module owns the *session*
lifecycle behind that agent:

* ``start_resident_session`` / ``stop_resident_session`` — spawn/adopt the
  resident task, stamp the shared context flags
  (``context["_resident_session"] == True`` +
  ``context["strategic_session_id"]``), arm/release the local-runner
  continuation *lease*, and record a compact session-state blob under a
  dedicated ``Project.vision`` key.
* ``renew_resident_session_lease`` + the agent-token heartbeat endpoint — the
  running resident agent renews the continuation lease (via the EXISTING
  ``workbench_state`` continuation entry point) and reads back whether the
  operator has requested a stop.

No DB migration: the session record lives in ``Project.vision`` under
``RESIDENT_SESSION_KEY`` (deliberately distinct from the operator-workbench
``_strategic_workbench`` prefs and the ``_strategic_local_continuation`` lease
marker). The lease itself is the existing local-runner continuation marker —
this module never invents a second freshness clock; it renews the marker
through ``apply_strategic_local_continuation_marker`` so the dead-man's-switch
semantics (owned by BE-safety) stay authoritative.

The reaper carve-out for these tasks lives in ``reaper_orphan_classify.py``
(keyed on the same ``_resident_session`` flag); the agent turn loop lives in
``agent/src/handlers/strategic-resident.ts`` (Wave 2). This file is the backend
session seam both depend on.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal, get_session
from app.failure_classification import ADMIN_CANCEL
from app.models import (
    Direction,
    Project,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.strategic_payload_summaries import _fetch_open_messages
from app.shared_state import limiter, runtime_config
from app.task_locking import (
    project_lock,
    task_lock,
    with_task_context_locked,
    with_vision_locked,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    apply_strategic_local_continuation_marker,
    persist_strategic_workbench_update,
    strategic_local_continuation_status_from_vision,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ``Project.vision`` key holding the resident-session record. Kept distinct from
# ``_strategic_workbench`` (per-operator prefs) and ``_strategic_local_continuation``
# (the lease marker) so the three concerns never clobber one another.
RESIDENT_SESSION_KEY = "_resident_strategic_session"

# ``Project.vision`` key holding the reason the *last* Workday auto-start was
# blocked or failed (surfaced by the strategic read model so the operator sees
# WHY a Workday session isn't running instead of silence). Distinct from
# ``RESIDENT_SESSION_KEY`` because a failed start compensates its own blob away —
# the reason must outlive the (absent) session record. Set on a blocked/failed
# auto-start; cleared on a clean start and on stop.
RESIDENT_SESSION_REASON_KEY = "_resident_strategic_session_reason"

# Blocked/failed reason codes. ``sessions_disabled`` (flag off) is derived by the
# read model from the flag itself and never recorded here; these two ARE recorded
# by the auto-start hook because they are runtime outcomes, not static config.
RESIDENT_SESSION_REASON_NO_RUNNER = "no_runner"
RESIDENT_SESSION_REASON_START_FAILED_PREFIX = "start_failed"
# Transient block: a batch strategic cycle is already in flight (IN_PROGRESS /
# EVALUATING) — no adoptable task exists and the spawn guard refuses to create a
# duplicate. The blocking cycle completes on its own; the strategic_completion
# handler re-tries the Workday auto-start once it finishes, so the operator
# session starts without manual re-entry into Workday mode.
RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT = "cycle_in_flight"
# Multi-device block reason: the runner the session is pinned to (an explicit
# operator override at start, OR the auto-picked device mid-session) is offline /
# no longer available. Derived by the read model from the live runner set + the
# pinned ``runner_id`` — Workday does NOT auto-migrate to another device; the
# operator restarts the session to pick another (operator decision (c)).
RESIDENT_SESSION_REASON_RUNNER_OFFLINE = "runner_offline"

# Runtime-config feature flag (default off). Registered by BE-safety in the
# settings registry; referenced here by name only.
STRATEGIC_SESSION_ENABLED_SETTING = "strategic_session_enabled"

# Runtime-config: max wall-clock a resident session may run WITHOUT any operator
# interaction before the heartbeat stops renewing the lease (so the legacy
# cadence reclaims authority) even though the session process is still alive.
# Operator interaction (a new operator message) re-arms it. Referenced by name;
# ``runtime_config`` supplies the registered default (14400s / 4h — see
# ``runtime_config_state.py``); this local fallback only applies if the key is
# ever absent from the registry, so it is pinned to that same 4h value to keep
# the effective cap self-documenting rather than silently disagreeing with it.
STRATEGIC_SESSION_MAX_UNATTENDED_SETTING = "strategic_session_max_unattended_seconds"
_DEFAULT_STRATEGIC_SESSION_MAX_UNATTENDED_SECONDS = 14400  # 4h

# A provisional spawn-claim (an active session blob whose ``task_id`` is still
# None because the spawn is in flight) blocks a concurrent start from spawning a
# second task. If the starting process dies mid-flight the claim would wedge new
# starts forever, so a claim older than this is treated as abandoned and may be
# reclaimed. Comfortably longer than a spawn takes (~30-60s).
_RESIDENT_START_CLAIM_STALE_SECONDS = 300  # 5 min

# Operator-authored message ``from_type`` values that count as an operator
# interaction for the unattended cap (operator messages are stored as ``user``;
# ``operator`` is included defensively).
_OPERATOR_MESSAGE_FROM_TYPES = ("user", "operator")

# created_by value stamped on tasks the strategist spawns (mirrors
# ``agent_tasks._STRATEGIC_AGENT_CREATED_BY`` / admin_agent_ops). Used to scope
# the heartbeat's work-state signal to the strategist's OWN in-flight work.
_STRATEGIC_AGENT_CREATED_BY = "STRATEGIC_AGENT"

# Direction statuses that are "live" (not retired/terminal) for the surface
# catalog — mirrors the directions_summary inclusion logic in strategic_spawning.
_ACTIVE_DIRECTION_STATUSES = ("active", "paused", "proposed")

# Maximum bytes stored for the agent's serialized exam-ledger (note field).
_EXAM_LEDGER_MAX_BYTES = 20_000

# Maximum bytes stored for the agent's serialized decisions register. The TS
# side caps serialization at 32,000 bytes (DECISIONS_REG_MAX_BYTES); this cap
# is defense-in-depth against a malformed blob.
_DECISIONS_REG_MAX_BYTES = 32_000

# Rolling window (seconds) over which a STRATEGIC_AGENT task that reached a
# terminal status counts toward ``work_state_summary.terminal_since`` — i.e.
# "how many of my spawned tasks just landed." Advisory only.
_WORK_STATE_TERMINAL_WINDOW_SECONDS = 3600

# Depth limit for the recursive CTE that walks STRATEGIC_AGENT task descendants
# in ``_compute_resident_work_state``. Matches the identical guard in
# ``agent_tasks._ANCESTOR_WALK_MAX_DEPTH`` — parent_task_id is admin-mutable
# so an explicit cap prevents cycle-induced infinite recursion.
_WORK_STATE_ANCESTOR_WALK_MAX_DEPTH = 20

# The resident task must be one of these (non-terminal) statuses to host / keep
# hosting a session. Mirrors ``strategic_state_read_model._LIVE_STRATEGIC_TASK_STATUSES``.
_LIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)

_RESIDENT_STRATEGIC_TASK_TYPE = "strategic_review"

# A live strategic_review task is only *adoptable* into a resident session while
# it has NOT yet begun executing. Handler dispatch — the one-shot
# ``handleStrategic`` vs the streaming ``handleStrategicResident`` — is decided
# ONCE, when the agent claims the task and reads its ``_resident_session`` context
# flag (agent/src/handlers/index.ts). A task the orchestrator has already moved
# past QUEUED (``started_at`` is stamped at the PROVISIONING transition in
# ``orchestrator.py``) has, or is about to have, an agent that already read its
# context as a one-shot; stamping the resident flag on it then would never
# re-route, leaving the "session" pointed at a task that never streams. Adoption
# is therefore restricted to a not-yet-dispatched task (QUEUED / PROVISIONING with
# no ``started_at``); anything already executing is left alone and a fresh
# resident task is spawned instead.
_ADOPTABLE_TASK_STATUSES = frozenset(
    {TaskStatus.QUEUED, TaskStatus.PROVISIONING}
)

# Session status values.
STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"


# ---------------------------------------------------------------------------
# Session-state helpers (Project.vision blob)
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    return f"ss-{uuid.uuid4().hex[:16]}"


def read_resident_session(vision: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the raw session-state blob from ``Project.vision`` (or None)."""
    blob = (vision or {}).get(RESIDENT_SESSION_KEY)
    return blob if isinstance(blob, dict) else None


def read_resident_session_blocked_reason(vision: dict[str, Any] | None) -> str | None:
    """Return the recorded reason the last Workday auto-start was blocked (or None).

    Read-only projection of the ``RESIDENT_SESSION_REASON_KEY`` blob for the
    strategic read model. Bounded to a short string.
    """
    blob = (vision or {}).get(RESIDENT_SESSION_REASON_KEY)
    if isinstance(blob, dict):
        reason = blob.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:200]
    return None


def _short_error(exc: BaseException) -> str:
    """Compact, single-line message for surfacing a failed auto-start reason.

    Prefers an ``HTTPException.detail`` (the actionable cause the start path
    raises) over the raw ``str(exc)``; falls back to the class name. Bounded.
    """
    detail = getattr(exc, "detail", None)
    text = str(detail) if detail else (str(exc) or exc.__class__.__name__)
    return " ".join(text.split())[:120]


async def set_resident_session_blocked_reason(
    session: AsyncSession,
    project: Project,
    reason: str | None,
) -> None:
    """Record (or, when *reason* is None, clear) why a Workday session isn't running.

    Writes the ``RESIDENT_SESSION_REASON_KEY`` blob under a project row lock on the
    CALLER session (so the identity-mapped ``project`` stays consistent and the
    same request's read model reflects it), then commits + refreshes. Best-effort:
    a failure here is logged and swallowed — recording *why* an auto-start was
    blocked must never itself break the operator's mode switch. A no-op clear
    (nothing recorded) is skipped so it never churns ``vision``.
    """
    if reason is None and read_resident_session_blocked_reason(project.vision) is None:
        return
    try:
        def _apply(vision: dict[str, Any]) -> None:
            if reason:
                vision[RESIDENT_SESSION_REASON_KEY] = {
                    "reason": reason[:200],
                    "recorded_at": _utcnow().isoformat(),
                }
            else:
                vision.pop(RESIDENT_SESSION_REASON_KEY, None)

        await with_vision_locked(session, project.id, _apply, skip_locked=False)
        await session.commit()
        await session.refresh(project)
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] failed to %s blocked-reason for project %s",
            "clear" if reason is None else "record",
            project.id,
            exc_info=True,
        )


async def _set_cycle_in_flight_reason(
    session: AsyncSession,
    project: Project,
    operator_user_id: str,
) -> None:
    """Record the ``cycle_in_flight`` reason, including the operator_user_id.

    Stores the operator_user_id alongside the reason so the completion-retry
    handler can re-run the Workday auto-start with the right user identity once
    the blocking cycle finishes. Best-effort: failure is logged and swallowed.
    """
    try:
        def _apply(vision: dict[str, Any]) -> None:
            vision[RESIDENT_SESSION_REASON_KEY] = {
                "reason": RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
                "recorded_at": _utcnow().isoformat(),
                "operator_user_id": str(operator_user_id)[:200],
            }

        await with_vision_locked(session, project.id, _apply, skip_locked=False)
        await session.commit()
        await session.refresh(project)
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] failed to record cycle_in_flight reason for project %s",
            project.id,
            exc_info=True,
        )


async def _record_blocked_reason_isolated(
    project_id: uuid.UUID,
    reason: str | None,
    *,
    session_factory=None,
) -> None:
    """Record (or clear) the Workday blocked-reason on a fresh, DEDICATED session.

    Opens its own ``AsyncSessionLocal`` (injectable for tests, the same idiom as
    ``_release_start_claim``), re-loads the project there, and delegates to
    ``set_resident_session_blocked_reason``. Isolating the reason write on a fresh
    connection means it is durably recorded even when the auto-start ran on a
    now-poisoned session (a Cloud SQL drop / protocol-corruption class failure
    invalidates that connection), and never touches the operator's request
    session. Best-effort: any failure is logged and swallowed — recording *why* a
    Workday auto-start was blocked must never itself break the mode switch.
    """
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            await set_resident_session_blocked_reason(session, project, reason)
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] isolated blocked-reason %s failed for project %s",
            "clear" if reason is None else "record",
            project_id,
            exc_info=True,
        )


def resident_session_is_active(blob: dict[str, Any] | None) -> bool:
    """True iff *blob* describes a live (not ended, not stop-requested) session."""
    return (
        isinstance(blob, dict)
        and blob.get("status") == STATUS_ACTIVE
        and not blob.get("stop_requested")
    )


def resident_session_snapshot(vision: dict[str, Any] | None) -> dict[str, Any] | None:
    """Public, bounded projection of the session-state blob for API responses."""
    blob = read_resident_session(vision)
    if blob is None:
        return None
    return {
        "session_id": blob.get("session_id"),
        "task_id": blob.get("task_id"),
        "runner_id": blob.get("runner_id"),
        "status": blob.get("status"),
        "stop_requested": bool(blob.get("stop_requested")),
        "started_at": blob.get("started_at"),
        "updated_at": blob.get("updated_at"),
        "ended_at": blob.get("ended_at"),
        "last_progress_at": blob.get("last_progress_at"),
        "last_operator_interaction_at": blob.get("last_operator_interaction_at"),
        "compaction_count": blob.get("compaction_count"),
        "last_compaction_at": blob.get("last_compaction_at"),
    }


def _parse_iso_naive(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a naive-UTC ``datetime`` (or None).

    Session-blob timestamps are written via ``_utcnow().isoformat()`` (naive
    UTC), and ``ProjectMessage.created_at`` is stored naive-UTC, so this
    normalizes any tz-aware cursor to naive UTC before comparison. Non-strings
    and unparseable values return None (caller decides the fallback).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _start_claim_is_stale(blob: dict[str, Any] | None, now: datetime) -> bool:
    """True iff *blob* is a provisional spawn-claim old enough to reclaim.

    A provisional claim whose starting process died would otherwise wedge new
    starts forever; one older than ``_RESIDENT_START_CLAIM_STALE_SECONDS`` (or
    with an unparseable ``started_at``) is abandoned and reclaimable.
    """
    started = _parse_iso_naive((blob or {}).get("started_at"))
    if started is None:
        return True
    return (now - started).total_seconds() > _RESIDENT_START_CLAIM_STALE_SECONDS


def _session_scope_conditions(resident_session_id: str | None) -> list:
    """WHERE fragments scoping operator messages to a resident session.

    When ``resident_session_id`` is provided, only messages stamped for THAT
    session — plus legacy/cadence NULL-session rows (never hidden: backward
    compat so pre-migration backlog is not silently dropped) — are matched. A
    message stamped for a DIFFERENT session is excluded, so session A's messages
    do not re-arm session B's unattended cap or pollute B's pacing signal. When
    ``resident_session_id`` is None (older callers / cadence path) the scope is
    empty and the query behaves project-wide exactly as before.
    """
    if resident_session_id is None:
        return []
    return [
        or_(
            ProjectMessage.resident_session_id == resident_session_id,
            ProjectMessage.resident_session_id.is_(None),
        )
    ]


async def _latest_operator_interaction_at(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    resident_session_id: str | None = None,
) -> datetime | None:
    """Return the newest operator-authored message time for the project (or None).

    Operator messages are the durable "operator interaction" signal the
    unattended cap resets on. Naive-UTC ``created_at`` is returned directly. When
    ``resident_session_id`` is given, only messages addressed to THIS session (or
    unstamped legacy/cadence rows) reset the clock — see
    ``_session_scope_conditions``.
    """
    return (
        await session.execute(
            select(func.max(ProjectMessage.created_at)).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(_OPERATOR_MESSAGE_FROM_TYPES),
                *_session_scope_conditions(resident_session_id),
            )
        )
    ).scalar_one_or_none()


async def _compute_resident_work_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    resident_session_id: str | None = None,
) -> tuple[str | None, dict[str, int]]:
    """READ-ONLY snapshot of strategist-relevant world state for the heartbeat.

    Returns ``(work_state_rev, work_state_summary)``:

    * ``work_state_rev`` — a monotonic marker: the newest ISO-8601 timestamp
      across the change sources the strategist cares about — STRATEGIC_AGENT-
      created tasks' and their descendant tasks' latest status transitions
      (``Task.updated_at`` advances on every transition via ``onupdate``),
      operator-authored ``ProjectMessage`` arrivals, and ``StrategicCycle``
      mutation stamps. The agent compares this against the rev it recorded at
      its last autonomous turn to decide whether the world moved. ``None`` when
      the project has no such activity yet, in which case an older agent
      degrades to floor-tick pacing.
    * ``work_state_summary`` — advisory ``{in_flight, terminal_since,
      new_operator_messages}`` counts feeding the agent's tick context.

    This is DELIBERATELY cheap (a handful of indexed ``max()``/``count()``
    aggregates) and STRICTLY read-only: it never reads or mutates the lease,
    the continuation freshness marker, or the session blob, so surfacing it on
    the heartbeat cannot extend the dead-man's switch (contract pinned by
    ``test_heartbeat_liveness_only_does_not_extend_lease``).
    """
    # A single recursive CTE finds the entire strategist subtree (roots +
    # transitive descendants) bounded at _WORK_STATE_ANCESTOR_WALK_MAX_DEPTH
    # levels, then aggregates max(updated_at), in-flight count, and
    # terminal-since count in the same query — no Python-side BFS, no
    # unbounded loop, no cycle risk.
    terminal_cutoff = _utcnow() - timedelta(
        seconds=_WORK_STATE_TERMINAL_WINDOW_SECONDS
    )
    tree_row = (
        await session.execute(
            text("""
                WITH RECURSIVE task_tree AS (
                    SELECT id, updated_at, archived, status, 0 AS depth
                    FROM task
                    WHERE project_id = :project_id
                      AND created_by = :created_by
                    UNION ALL
                    SELECT t.id, t.updated_at, t.archived, t.status,
                           tt.depth + 1
                    FROM task t
                    INNER JOIN task_tree tt ON t.parent_task_id = tt.id
                    WHERE t.project_id = :project_id
                      AND tt.depth < :max_depth
                )
                SELECT
                    max(updated_at) AS max_updated,
                    count(*) FILTER (
                        WHERE NOT archived
                          AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                    ) AS in_flight,
                    count(*) FILTER (
                        WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                          AND updated_at >= :terminal_cutoff
                    ) AS terminal_since
                FROM task_tree
            """),
            {
                "project_id": project_id,
                "created_by": _STRATEGIC_AGENT_CREATED_BY,
                "max_depth": _WORK_STATE_ANCESTOR_WALK_MAX_DEPTH,
                "terminal_cutoff": terminal_cutoff,
            },
        )
    ).fetchone()

    task_max = tree_row[0] if tree_row else None
    in_flight: int = int(tree_row[1] or 0) if tree_row else 0
    terminal_since: int = int(tree_row[2] or 0) if tree_row else 0

    msg_max = (
        await session.execute(
            select(func.max(ProjectMessage.created_at)).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(_OPERATOR_MESSAGE_FROM_TYPES),
                *_session_scope_conditions(resident_session_id),
            )
        )
    ).scalar_one_or_none()
    cycle_max = (
        await session.execute(
            select(func.max(StrategicCycle.updated_at)).where(
                StrategicCycle.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    # task_max comes from raw SQL (asyncpg returns TIMESTAMPTZ as tz-aware);
    # msg_max/cycle_max go through NaiveUTCDateTime and are tz-naive. Normalise.
    if task_max is not None and task_max.tzinfo is not None:
        task_max = task_max.astimezone(timezone.utc).replace(tzinfo=None)
    stamps = [dt for dt in (task_max, msg_max, cycle_max) if dt is not None]
    work_state_rev = max(stamps).isoformat() if stamps else None

    new_operator_messages = (
        await session.execute(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(_OPERATOR_MESSAGE_FROM_TYPES),
                ProjectMessage.status == "open",
                *_session_scope_conditions(resident_session_id),
            )
        )
    ).scalar() or 0
    return work_state_rev, {
        "in_flight": int(in_flight),
        "terminal_since": int(terminal_since),
        "new_operator_messages": int(new_operator_messages),
    }


async def _release_start_claim(
    project_id: uuid.UUID,
    session_id: str,
    spawned_task_id: uuid.UUID | None,
    *,
    session_factory=None,
) -> None:
    """Compensate a failed start: drop our claim + cancel any task we spawned.

    Runs on a DEDICATED, fresh ``AsyncSession``/connection — NEVER the caller's
    session, which the original in-``try`` failure may have poisoned (a Cloud SQL
    drop / protocol-corruption class invalidates the connection and leaves the
    session's async greenlet portal broken; any further op on it — including a
    fresh pool checkout+pre-ping — then raises ``MissingGreenlet`` and MASKS the
    real start failure the caller is about to re-raise). Opening its own session
    via ``AsyncSessionLocal`` (injectable as ``session_factory`` for tests, the
    same idiom as ``release_qualifier._finalize_in_dedicated_session``) means
    compensation runs on a clean connection regardless of the caller session's
    state, so a start that fails between the atomic claim commit and the finalize
    commit never leaves an active-but-taskless session record wedging future
    starts, nor an untracked (orphaned) ``strategic_review`` task with no
    ``_resident_session``. Guarded on ``session_id`` so it only ever clears OUR
    own claim, never a session a concurrent starter legitimately owns.

    Best-effort: any failure here is logged and swallowed so it never masks the
    original start failure. The ``async with`` block auto-rolls-back/closes the
    dedicated session on error.
    """
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as session:

            def _clear(vision: dict[str, Any]) -> None:
                current = read_resident_session(vision)
                if isinstance(current, dict) and str(
                    current.get("session_id")
                ) == str(session_id):
                    vision.pop(RESIDENT_SESSION_KEY, None)

            await with_vision_locked(session, project_id, _clear, skip_locked=False)
            if spawned_task_id is not None:
                task = await session.get(Task, spawned_task_id)
                if task is not None and task.status in _LIVE_TASK_STATUSES:
                    task.status = TaskStatus.CANCELLED
                    task.latest_status_message = (
                        "Resident strategic session start failed; "
                        "cancelling orphaned strategic_review task"
                    )
                    session.add(task)
            await session.commit()
    except Exception:
        # Compensation is best-effort — it must never mask the original start
        # failure the caller is about to re-raise.
        logger.exception(
            "[RESIDENT_SESSION] compensation failed releasing claim %s (project %s)",
            session_id,
            project_id,
        )


# ---------------------------------------------------------------------------
# Task adoption / spawn
# ---------------------------------------------------------------------------


async def _find_adoptable_strategic_review_task(
    session: AsyncSession, project_id: uuid.UUID
) -> Task | None:
    """Return an *adoptable* ``strategic_review`` task for the project, if any.

    Adoptable == not yet executing (``status in _ADOPTABLE_TASK_STATUSES`` and no
    ``started_at``), so stamping the resident-session flags onto it will still
    route it to ``handleStrategicResident`` when the agent claims it. A strategic
    cycle that is already PROVISIONING/IN_PROGRESS is a one-shot that has already
    dispatched (or is about to) to ``handleStrategic``; adopting it would point
    the session at a task that never streams, so it is deliberately excluded here
    and the caller spawns a fresh resident task instead (single-authority
    invariant preserved: only a not-yet-started cadence cycle is adopted).
    """
    rows = (
        await session.execute(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.archived.is_(False),
                Task.status.in_(list(_ADOPTABLE_TASK_STATUSES)),
                Task.started_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )
    ).scalars().all()
    for task in rows:
        payload = task.first_payload or {}
        if payload.get("type") == _RESIDENT_STRATEGIC_TASK_TYPE:
            return task
    return None


async def _spawn_resident_strategic_task(
    session: AsyncSession,
    project: Project,
    runner_id: str | None,
    *,
    session_id: str,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
) -> Task | None:
    """Spawn a fresh ``strategic_review`` task via the canonical spawn path.

    Reuses ``strategic_spawning._spawn_strategic_review`` (imported lazily to
    avoid the heavy import at module load and any import cycle) rather than
    re-implementing the strategic payload build. That spawn honors the existing
    ``_has_active_strategic_task`` guard: if a one-shot strategic cycle is already
    mid-flight it creates NOTHING (returns False), so the re-query below finds no
    freshly-spawned adoptable task and returns None — the caller then refuses
    rather than silently adopting the in-flight one-shot. The spawn commits
    internally; on success it leaves a QUEUED (``started_at``-less) task the
    re-query adopts.

    The resident-session flags are seeded via ``extra_context`` so the spawned
    task is committed ALREADY carrying ``_resident_session`` /
    ``strategic_session_id`` in the same INSERT — it is never committed claimable
    without the flag, so a warm runner that claims it in the sub-millisecond
    window before the session blob is finalized still routes to the streaming
    ``handleStrategicResident`` (handler dispatch reads the context flag at claim
    time). This is the SPAWN-path equivalent of the ADOPT path's under-lock stamp;
    no separate post-spawn stamping transaction remains.

    The HARD local-runner pin rides in via ``runner_pin_id`` (not just the
    ``preferred_runner_target_id`` affinity hint): when a runner is resolved the
    task is born PROVISIONING with ``vm_instance_id='runner:{id}'`` +
    ``context['runner_target_id']`` — the exact contract
    ``POST /api/runner/next-task`` filters on — so the operator's local runner
    claims it directly, WITHOUT the general VM-dispatch cycle (which may be paused,
    ``max_concurrent_vms=0``). Born QUEUED (unpinned cloud fallback) only when no
    eligible runner exists (``runner_id`` is None).
    """
    from app.strategic_spawning import _spawn_strategic_review

    await _spawn_strategic_review(
        project,
        session,
        preferred_runner_target_id=str(runner_id) if runner_id else None,
        # Hard birth pin: the spawned strategic_review is born PROVISIONING with
        # ``vm_instance_id='runner:{id}'`` + ``context['runner_target_id']`` so the
        # operator's local runner claims it directly via POST /api/runner/next-task
        # — independent of the general VM-dispatch cycle (which may be paused,
        # ``max_concurrent_vms=0``). When no runner is eligible (``runner_id`` None)
        # this stays None and the task is born QUEUED (degraded cloud fallback).
        runner_pin_id=str(runner_id) if runner_id else None,
        extra_context=_resident_born_context(
            session_id, runner_id, exam_ledger=exam_ledger, message_cursor=message_cursor,
            session_summary=session_summary, decisions_register=decisions_register,
        ),
        # Spawn MINIMALLY: skip the ~2-minute synchronous strategic-context build
        # (design-doc grounding + capability backing-path verification GitHub
        # content/tree fetches, branch-SHA lookups, recent-intervention ancestry,
        # drift/version-phase/ci-compliance). This runs inside the operator's
        # synchronous START request; the heavy build holds the request's DB
        # connection open long enough for asyncpg/Cloud SQL to idle-close it, so
        # the finalize flush 500s before ``task_id`` ever lands (~143s). The
        # resident agent builds its own context on the runner via Claude Code, so
        # pre-building it at spawn time is unnecessary — the born-context flags +
        # runner pin still ride in via ``extra_context``.
        resident_minimal=True,
    )
    return await _find_adoptable_strategic_review_task(session, project.id)


def _resident_runner_display_name(runner: Any) -> str:
    """Human label for a runner in the device picker (hostname, else short id).

    Shared by the block-reason messages and the read-model picker options so the
    device the operator sees named in "device <name> offline …" matches the label
    in the dropdown.
    """
    hostname = getattr(runner, "hostname", None)
    if isinstance(hostname, str) and hostname.strip():
        return hostname.strip()[:160]
    return f"runner-{str(runner.id)[:8]}"


def _resident_runner_is_available(runner: Any) -> bool:
    """True iff *runner* can host a resident session RIGHT NOW (multi-device gate).

    "Available" (operator decision) == active status + recent heartbeat (LIVE) AND
    handles the ``strategic_review`` task type AND its agent is ready
    (``agent_available`` not False). This single predicate is shared by the
    auto-pick, the explicit-override validation, and the read-model picker so all
    three agree on which devices can run Workday. Reuses the exact
    ``strategic_spawning`` eligibility predicates the dispatch path uses.
    """
    from app.strategic_spawning import (
        _local_runner_agent_available,
        _local_runner_handles_task_type,
        _local_runner_is_live,
    )

    return bool(
        _local_runner_is_live(runner)
        and _local_runner_handles_task_type(runner, _RESIDENT_STRATEGIC_TASK_TYPE)
        and _local_runner_agent_available(runner)
    )


async def _load_resident_runner_rows(
    session: AsyncSession, project_id: uuid.UUID
) -> list[Any]:
    """Load the project's non-deleted local runners, most-recently-seen first.

    Shared query for auto-pick, the picker options, and override validation, so
    they all scan the same ordered device set (matching
    ``_resolve_local_continuation_runner_affinity``'s ordering).
    """
    from app.models import LocalRunner

    return list(
        (
            await session.execute(
                select(LocalRunner)
                .where(
                    LocalRunner.project_id == project_id,
                    LocalRunner.status != "deleted",
                )
                .order_by(
                    LocalRunner.last_seen_at.desc(),
                    LocalRunner.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )


async def _auto_select_local_runner(
    session: AsyncSession, project_id: uuid.UUID
) -> str | None:
    """Auto-PICK a LIVE, strategic-capable, agent-available local runner id.

    Returns the id of the first *available* runner (see
    ``_resident_runner_is_available``) — most-recently-seen first — so a resident
    session started WITHOUT an explicit override runs on the operator's box. When
    NONE is available returns None; the caller then BLOCKS the session (Workday
    does not fall back to a cloud VM — operator decision (c)).
    """
    for runner in await _load_resident_runner_rows(session, project_id):
        if _resident_runner_is_available(runner):
            return str(runner.id)
    return None


async def _resolve_override_runner(
    session: AsyncSession, project_id: uuid.UUID, runner_id: str
) -> Any | None:
    """Resolve an explicit override ``runner_id`` to a runner OF THIS PROJECT.

    Returns the ``LocalRunner`` when it exists, belongs to *project_id*, and is not
    deleted — else None (the caller raises a clear "device not found" error rather
    than silently pinning to an unknown/foreign device). Availability is checked
    separately by the caller so it can distinguish "not found" (404) from
    "offline" (409 block).
    """
    from app.models import LocalRunner

    try:
        runner_uuid = uuid.UUID(str(runner_id))
    except (ValueError, TypeError):
        return None
    runner = await session.get(LocalRunner, runner_uuid)
    if (
        runner is None
        or str(runner.project_id) != str(project_id)
        or runner.status == "deleted"
    ):
        return None
    return runner


async def build_resident_runner_options(
    session: AsyncSession, project_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Build the device-picker options for the project's resident-session runners.

    One entry per non-deleted local runner: ``id``, ``name`` (hostname or short
    id), ``hostname``, ``online`` (LIVE: active + recent heartbeat), ``agent_ready``
    (``agent_available`` not False), and ``eligible`` (online + agent-ready + can
    handle ``strategic_review`` == the full availability gate). The read model
    surfaces this so the UI can render the auto-pick/override dropdown and show
    each device's online status; the pinned ``runner_id`` rides on the session
    snapshot.
    """
    from app.strategic_spawning import (
        _local_runner_agent_available,
        _local_runner_handles_task_type,
        _local_runner_is_live,
    )

    options: list[dict[str, Any]] = []
    for runner in await _load_resident_runner_rows(session, project_id):
        online = bool(_local_runner_is_live(runner))
        agent_ready = bool(_local_runner_agent_available(runner))
        handles = bool(
            _local_runner_handles_task_type(runner, _RESIDENT_STRATEGIC_TASK_TYPE)
        )
        hostname = getattr(runner, "hostname", None)
        options.append(
            {
                "id": str(runner.id),
                "name": _resident_runner_display_name(runner),
                "hostname": hostname.strip()[:160]
                if isinstance(hostname, str) and hostname.strip()
                else None,
                "online": online,
                "agent_ready": agent_ready,
                "eligible": bool(online and agent_ready and handles),
            }
        )
    return options


def _resident_born_context(
    session_id: str,
    runner_id: str | None,
    *,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
) -> dict[str, Any]:
    """Build the resident-session context flags a SPAWNED task is BORN with.

    Same flag/pin shape ``_stamp_resident_context`` would apply a beat later, but
    returned as a plain dict so it can be threaded into the canonical spawn's
    ``extra_context`` and committed in the SAME INSERT that creates the task. That
    is what closes the SPAWN-path race: because the ``strategic_review`` task is
    committed already carrying ``_resident_session`` (+ ``strategic_session_id``
    and, when a runner is pinned, the hard local-runner intent), it never exists
    in a committed, claimable state without the flag. A warm local runner that
    claims it before the session blob is finalized therefore reads
    ``_resident_session`` at claim time and dispatches the streaming
    ``handleStrategicResident`` — never the one-shot ``handleStrategic``. The pin
    semantics are identical to stamping (``STRATEGIC_LOCAL_RUNNER_REQUIRED`` +
    preferred runner + ``resident_session`` reason), just set at birth.
    """
    context: dict[str, Any] = {
        "_resident_session": True,
        "strategic_session_id": session_id,
    }
    if runner_id:
        context["preferred_runner_target_id"] = str(runner_id)
        # Hard local-runner intent so dispatch pins the warm session to the
        # operator's runner instead of falling back to a fresh cloud VM.
        context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] = True
        context[STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY] = "resident_session"
    # Carry the persisted exam ledger forward across a session restart: the agent
    # restores it from ``task.context["_exam_ledger"]`` on startup, so a NEW
    # session born on a fresh task continues the examination rotation instead of
    # resetting to never-examined-first. Absent (first-ever start) leaves the
    # context byte-identical to before.
    if exam_ledger:
        context["_exam_ledger"] = exam_ledger
    # Carry the message cursor forward so the agent's poll window doesn't regress
    # to the 2-minute fallback on restart (which re-delivers already-answered
    # messages). Absent on first-ever start — no cursor has been established yet.
    if message_cursor:
        context["message_cursor"] = message_cursor
    # Carry the session summary forward so the new session's bootstrap includes
    # "Prior session context" from the previous session's set-session-summary op.
    if session_summary:
        context["_session_summary"] = session_summary
    # Carry the decisions register forward so the new session restores the
    # strategist's cross-tick decision memory instead of resetting to an empty
    # array (which would cause re-derivation of settled facts).
    if decisions_register:
        context["_decisions_reg"] = decisions_register
    return context


def _stamp_resident_context(
    task: Task,
    *,
    session_id: str,
    runner_id: str | None,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
) -> None:
    """Stamp the shared resident-session flags + AUTHORITATIVE runner pin onto *task*.

    Used by the ADOPT path, which re-uses an existing (not-yet-executing) cadence
    ``strategic_review`` task under a row lock, so the flags are merged onto its
    already-committed context. The SPAWN path instead seeds the equivalent flags
    at task birth via ``_resident_born_context`` — see that helper for why the
    spawned task can never exist claimable without the flag.

    When a session runner is resolved (``runner_id`` set) the adopted task is
    ALSO hard runner-pinned IN PLACE — ``status=PROVISIONING``,
    ``vm_instance_id='runner:{id}'``, ``context['runner_target_id']=str(id)``,
    ``vm_provisioned_at`` stamped — the exact contract
    ``POST /api/runner/next-task`` filters on and the same one
    ``orchestrator._try_runner_dispatch`` (and the resident SPAWN path) apply. This
    makes the adopted resident task directly pullable by the operator's local
    runner WITHOUT the general VM-dispatch cycle, which may be paused
    (``max_concurrent_vms=0``); otherwise a resident session that ADOPTS a cadence
    cycle (rather than spawning fresh) would wedge un-runnable until dispatch
    resumes — the SPAWN-path gap's twin. The caller only reaches here when
    ``_task_is_adoptable`` held, so the task is QUEUED/PROVISIONING with
    ``started_at IS NULL``; overwriting the pin is safe and leaves ``started_at``
    NULL (the runner stamps it on pull, which also keeps the reaper carve-out
    ``reaper_orphan_classify._is_runner_pinned_awaiting_pull`` protecting it). Any
    stale cadence strategic-hold marker is cleared (mirroring
    ``_try_runner_dispatch``) so the 6h local-runner hold sweep can't fail a task
    we just pinned. The caller ``session.add(task)``s + commits under the same
    lock, so the column pin lands atomically with the context flags.

    With NO runner (unpinned cloud fallback) ONLY the resident flags are stamped
    and the task keeps its existing status / vm id — unchanged from before.
    """
    context = dict(task.context or {})
    context["_resident_session"] = True
    context["strategic_session_id"] = session_id
    # Carry the persisted exam ledger forward (adopt-path twin of the born-context
    # seed) so an adopted cadence task continues the examination rotation across a
    # restart. Absent leaves the merged context unchanged.
    if exam_ledger:
        context["_exam_ledger"] = exam_ledger
    # Carry the message cursor forward so the poll window survives a restart.
    if message_cursor:
        context["message_cursor"] = message_cursor
    # Carry the session summary forward so the bootstrap includes "Prior session
    # context" from the previous session's set-session-summary output.
    if session_summary:
        context["_session_summary"] = session_summary
    # Carry the decisions register forward so the adopted task restores the
    # strategist's cross-tick decision memory.
    if decisions_register:
        context["_decisions_reg"] = decisions_register
    if runner_id:
        context["preferred_runner_target_id"] = str(runner_id)
        # Hard local-runner intent so dispatch pins the warm session to the
        # operator's runner instead of falling back to a fresh cloud VM.
        context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] = True
        context.setdefault(
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY, "resident_session"
        )
        # AUTHORITATIVE runner pin (not merely the affinity hint above): the
        # adopted task becomes directly pullable by POST /api/runner/next-task,
        # independent of the (possibly paused) VM-dispatch cycle. Mirrors
        # orchestrator._try_runner_dispatch + the resident SPAWN-path birth pin.
        context["runner_target_id"] = str(runner_id)
        # Drop any stale strategic local-runner hold marker so the hold sweep
        # (orchestrator._fail_stale_local_runner_holds) can't fail a now-pinned
        # task — same cleanup _try_runner_dispatch does on pin.
        context.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
        task.status = TaskStatus.PROVISIONING
        task.vm_instance_id = f"runner:{runner_id}"
        task.vm_provisioned_at = _utcnow()
    task.context = context
    flag_modified(task, "context")


def _task_is_adoptable(task: Task | None) -> bool:
    """True iff *task* is a not-yet-executing, resident-adoptable strategic cycle.

    Mirrors the filter in ``_find_adoptable_strategic_review_task`` exactly, but
    operates on an already-loaded (ideally freshly row-locked) Task so the adopt
    decision can be RE-VALIDATED inside the claim transaction: a warm runner may
    have claimed the QUEUED ``strategic_review`` as a one-shot (stamping
    ``started_at`` / advancing past ``_ADOPTABLE_TASK_STATUSES``) between the
    pre-lock finder read and the resident-context stamp. If this returns False
    the candidate is no longer adoptable and the caller spawns fresh instead of
    pointing the session at a task that already dispatched as a one-shot
    (finding 2).
    """
    if task is None or task.archived:
        return False
    if task.status not in _ADOPTABLE_TASK_STATUSES:
        return False
    if task.started_at is not None:
        return False
    payload = task.first_payload or {}
    return payload.get("type") == _RESIDENT_STRATEGIC_TASK_TYPE


def _arm_and_finalize_vision(
    vision: dict[str, Any],
    *,
    session_id: str,
    workbench_state: dict[str, Any],
    task_id_str: str,
    operator_user_id: str,
    effective_runner_id: str | None,
) -> None:
    """Arm the continuation lease + stamp the resident task_id onto OUR claim.

    Shared finalize body for both the adopt and spawn paths. Only mutates the
    session blob when it is still the claim we own (``session_id`` match) — never
    clobbers a session a concurrent starter took over. Arms with an explicit
    ``renewal_reason='operator_interaction'`` because a session START is itself
    an operator interaction: freshness must anchor to NOW regardless of any
    leftover (possibly stale) marker, else the brand-new session would be born
    non-authoritative (fresh==False) and the cloud cadence would keep spawning
    competing cycles until the first progress op.
    """
    current = read_resident_session(vision)
    if not isinstance(current, dict) or str(current.get("session_id")) != str(
        session_id
    ):
        return
    apply_strategic_local_continuation_marker(
        vision,
        workbench_state,
        operator_user_id=str(operator_user_id),
        preferred_runner_id=effective_runner_id,
        renewal_reason="operator_interaction",
    )
    finalized = dict(current)
    finalized["task_id"] = task_id_str
    finalized["updated_at"] = _utcnow().isoformat()
    vision[RESIDENT_SESSION_KEY] = finalized


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_resident_session(
    session: AsyncSession,
    project: Project,
    *,
    operator_user_id: str,
    runner_id: str | None = None,
    session_factory=None,
) -> dict[str, Any]:
    """Start (or return the already-running) resident strategic session.

    Adopts an *adoptable* (not-yet-executing) ``strategic_review`` cadence cycle
    if one exists — a cycle already PROVISIONING/IN_PROGRESS as a one-shot is left
    alone (adopting it would point the session at a task that never streams,
    finding 1) — else spawns a fresh resident task via the canonical path (which
    keeps the ``_has_active_strategic_task`` guard, so a spawn is refused, not
    forced, while a one-shot is mid-flight). Multi-device selection: an explicit
    ``runner_id`` is an operator OVERRIDE (must be a real project runner that is
    available now, else 404 "device not found" / 409 "device offline"); with no
    override an available runner is AUTO-PICKED so the warm session runs on the
    operator's box. When the selected/auto-picked device is unavailable the start
    is BLOCKED (raises 409) rather than dispatched to a cloud VM — Workday runs on
    the operator's box and never auto-migrates (operator decision (c)). Stamps the
    resident-session context flags;
    arms the local-runner continuation lease by putting the operator workbench
    into ``continuous`` mode (the canonical marker-arming entry point); and
    records the session-state blob under ``RESIDENT_SESSION_KEY``. Idempotent: a
    second call while an active session with a live task exists returns that
    session.

    Single-active-session invariant under concurrency: the active-check + the
    spawn decision are made inside an atomic, project-row-locked *claim* that is
    committed BEFORE any spawn. Two concurrent starts serialize on the row lock
    — the first commits its claim; the second re-reads the fresh blob under the
    lock, sees the active claim, and backs off — so they can never both spawn.
    A provisional spawn-claim carries ``task_id = None`` until the spawned task
    is finalized. A failure between the claim and the finalize is compensated
    (claim released, any spawned task cancelled) so no orphan / wedge remains.

    ``session_factory`` is the dedicated-session factory the compensation path
    (``_release_start_claim``) opens its FRESH cleanup session with — it defaults
    to the real ``AsyncSessionLocal`` and is injectable only so a test can bind
    compensation to the same engine/connection as its fixture session (the
    production ``AsyncSessionLocal`` rebinds to a different engine under the
    Postgres test harness, so a bare fresh session would clean up the wrong DB).
    """
    now = _utcnow()
    now_iso = now.isoformat()

    # Fast-path idempotency: an already-active session whose recorded task is
    # still live is returned as-is (no new session, no spawn). Correctness still
    # rests on the atomic claim below; this only spares the row lock for the
    # common "operator re-opens a running session" case.
    existing = read_resident_session(project.vision)
    stale_session_id: str | None = None
    # Carry the persisted exam ledger forward across a session restart. A real
    # restart ENDS the prior resident task and starts a NEW session on a fresh
    # (spawned/adopted) task, so the ledger the heartbeat mirrored onto the OLD
    # task's context would otherwise be lost and the examination rotation would
    # reset to never-examined-first. Capture it from the prior session blob (the
    # heartbeat also mirrors the ledger into the blob) and seed it into the new
    # task's context + blob below so the agent restores it on startup. Bounded to
    # the same byte cap the heartbeat enforces; a malformed/oversized value is
    # dropped (best-effort — a lost ledger only costs one rotation reset).
    carried_exam_ledger: str | None = None
    carried_message_cursor: str | None = None
    carried_session_summary: str | None = None
    carried_decisions_register: str | None = None
    if isinstance(existing, dict):
        _raw_ledger = existing.get("_exam_ledger")
        if isinstance(_raw_ledger, str) and 0 < len(
            _raw_ledger.encode()
        ) <= _EXAM_LEDGER_MAX_BYTES:
            carried_exam_ledger = _raw_ledger
        # Carry the message cursor forward so a restarted session resumes from the
        # high-water mark instead of falling back to the 2-minute window.
        _raw_cursor = existing.get("_message_cursor")
        if isinstance(_raw_cursor, str) and _raw_cursor:
            carried_message_cursor = _raw_cursor
        # Carry the session summary forward so the new session's bootstrap includes
        # context from the prior session (set-session-summary op).
        _raw_summary = existing.get("_session_summary")
        if isinstance(_raw_summary, str) and _raw_summary.strip():
            carried_session_summary = _raw_summary
        # Carry the decisions register forward so a restarted session restores
        # the strategist's cross-tick decision memory instead of resetting to
        # an empty array (which would cause re-derivation of settled facts).
        # Byte-size guard: defense-in-depth against a malformed blob matching the
        # TS-side cap (DECISIONS_REG_MAX_BYTES in strategic-resident-lib.ts).
        _raw_decisions = existing.get("_decisions_reg")
        if isinstance(_raw_decisions, str) and 0 < len(
            _raw_decisions.encode()
        ) <= _DECISIONS_REG_MAX_BYTES:
            carried_decisions_register = _raw_decisions
    if resident_session_is_active(existing):
        task_id = existing.get("task_id")
        live = None
        if task_id:
            try:
                live = await session.get(Task, uuid.UUID(str(task_id)))
            except (ValueError, TypeError):
                live = None
        if live is not None and live.status in _LIVE_TASK_STATUSES:
            return resident_session_snapshot(project.vision) or {}
        # Active blob but its task is dead/absent → the record is stale and
        # reclaimable. Remember which session_id we judged stale so the in-lock
        # claim reclaims only THAT record (deferring to any fresher session a
        # concurrent starter may have written since).
        stale_session_id = existing.get("session_id")

    # Resolve the runner to pin the warm session to (multi-device selection).
    #
    #  * OVERRIDE — an explicit ``runner_id`` is an operator override: it must be a
    #    real runner OF THIS PROJECT (else 404 "device not found") AND available
    #    right now (else BLOCK — never silently fall back to a different device or
    #    to cloud).
    #  * AUTO-PICK — with no override, auto-select an available runner so Workday
    #    runs on the operator's box. If NONE is available the session is BLOCKED
    #    (an enroll-a-runner reason), not dispatched to a cloud VM.
    #
    # The block is DEFERRED (captured, not raised here) so a start that turns out
    # to back off to a concurrent in-flight session — resolved only under the claim
    # row-lock below — never raises a spurious "no device" (that concurrent owner
    # already resolved its own runner). Raised after the lock iff we actually own
    # the claim, and BEFORE any claim blob is written, so no wedged claim leaks.
    effective_runner_id: str | None = None
    runner_block: HTTPException | None = None
    if runner_id:
        override = await _resolve_override_runner(session, project.id, str(runner_id))
        if override is None:
            runner_block = HTTPException(
                status_code=404,
                detail="Selected device not found for this project",
            )
        elif not _resident_runner_is_available(override):
            runner_block = HTTPException(
                status_code=409,
                detail=(
                    f"device {_resident_runner_display_name(override)} offline — "
                    "restart the session to pick another device"
                ),
            )
        else:
            effective_runner_id = str(override.id)
    else:
        effective_runner_id = await _auto_select_local_runner(session, project.id)
        if effective_runner_id is None:
            runner_block = HTTPException(
                status_code=409,
                detail="No device available — enroll a runner to run Workday",
            )

    # Adopt a not-yet-executing strategic_review cadence cycle if one exists
    # (this is a HINT read; the finalize below re-validates it under a task row
    # lock before stamping — finding 2). A cycle already PROVISIONING/IN_PROGRESS
    # as a one-shot is NOT adoptable — stamping the resident flag on it would never
    # re-route it to the streaming handler (finding 1) — so only when no adoptable
    # cycle exists do we spawn a fresh resident task.
    adopt_task = await _find_adoptable_strategic_review_task(session, project.id)

    session_id = new_session_id()

    # --- Atomic claim (single-active-session invariant) ----------------------
    decision: dict[str, Any] = {"mode": None}

    def _claim(vision: dict[str, Any]) -> None:
        fresh = read_resident_session(vision)
        if resident_session_is_active(fresh):
            fresh_task_id = fresh.get("task_id")
            if fresh_task_id:
                # A fully-recorded active session already owns this project.
                # Defer to it unless it is the very record we judged stale
                # (dead task) in the pre-lock check above.
                if str(fresh.get("session_id")) != str(stale_session_id):
                    decision["mode"] = "existing"
                    return
            elif not _start_claim_is_stale(fresh, now):
                # A provisional spawn-claim from a concurrent start still in
                # flight — back off rather than spawn a second task.
                decision["mode"] = "existing"
                return
        if runner_block is not None:
            # We are NOT backing off (no concurrent session to defer to) but the
            # selected/auto-picked device is unavailable — BLOCK without writing a
            # claim blob (so nothing wedges future starts). Raised after the lock.
            decision["mode"] = "blocked"
            return
        vision[RESIDENT_SESSION_KEY] = {
            "version": 1,
            "session_id": session_id,
            "task_id": str(adopt_task.id) if adopt_task is not None else None,
            "runner_id": effective_runner_id,
            "operator_user_id": str(operator_user_id),
            "status": STATUS_ACTIVE,
            "stop_requested": False,
            "started_at": now_iso,
            "updated_at": now_iso,
            "ended_at": None,
            "last_progress_at": None,
            # Seed the unattended-cap clock: the operator just started the
            # session, which is itself an interaction.
            "last_operator_interaction_at": now_iso,
        }
        # Preserve the carried-forward exam ledger on the new session blob too, so
        # a subsequent restart BEFORE the new session's first heartbeat still has a
        # ledger to carry forward (the heartbeat mirror re-writes it thereafter).
        if carried_exam_ledger:
            vision[RESIDENT_SESSION_KEY]["_exam_ledger"] = carried_exam_ledger
        # Preserve the message cursor on the new session blob so a restart BEFORE
        # the first heartbeat still restores the high-water mark correctly.
        if carried_message_cursor:
            vision[RESIDENT_SESSION_KEY]["_message_cursor"] = carried_message_cursor
        # Preserve the session summary on the new blob so a restart BEFORE the
        # first heartbeat still carries it into the agent's bootstrap prompt.
        if carried_session_summary:
            vision[RESIDENT_SESSION_KEY]["_session_summary"] = carried_session_summary
        # Preserve the decisions register on the new blob so a restart BEFORE
        # the first heartbeat still restores cross-tick decision memory.
        if carried_decisions_register:
            vision[RESIDENT_SESSION_KEY]["_decisions_reg"] = carried_decisions_register
        decision["mode"] = "adopt" if adopt_task is not None else "spawn"

    await with_vision_locked(session, project.id, _claim, skip_locked=False)
    await session.commit()
    await session.refresh(project)

    if decision["mode"] == "existing":
        # A concurrent start owns (or is mid-starting) the session — return it.
        return resident_session_snapshot(project.vision) or {}

    if decision["mode"] == "blocked":
        # The selected/auto-picked device is unavailable and we are not backing
        # off to a concurrent session — surface the deferred block (no claim blob
        # was written under the lock, so nothing is left to compensate).
        assert runner_block is not None
        raise runner_block

    # We own ``session_id``. Materialize the task (adopt or spawn), stamp the
    # resident context flags, arm the continuation lease, and finalize the blob.
    # Any failure here is compensated so no untracked task / wedged claim leaks.
    #
    # interaction_mode=continuous is persisted WITHOUT its own vision write /
    # commit (``sync_local_continuation_marker=False``, ``commit=False``); we arm
    # the marker (the lease) and finalize the blob in ONE atomic vision write per
    # path below. ``arm_interaction_mode=False``: do NOT fire a legacy
    # strategic-refresh cadence spawn — the resident task IS the strategist.
    workbench_update: dict[str, Any] = {"interaction_mode": "continuous"}
    if effective_runner_id:
        workbench_update["preferred_runner_id"] = effective_runner_id

    spawned_task_id: uuid.UUID | None = None
    task_id_str: str | None = None
    try:
        adopted = False
        if decision["mode"] == "adopt":
            # Finding 2: RE-VALIDATE the adopt candidate under Project-then-Task
            # row locks (canonical order), held through this transaction's commit.
            # The pre-lock ``adopt_task`` read is only a hint — a warm runner may
            # have claimed that QUEUED strategic_review as a one-shot (stamping
            # ``started_at`` / advancing its status) before our resident-context
            # stamp becomes durable. Re-reading it fresh + locked here, and
            # stamping + committing while both rows stay locked, means either (a)
            # it is still adoptable and our stamp lands before any runner can
            # claim it, or (b) it already dispatched as a one-shot and we fall
            # through to spawn a fresh resident task instead — never pointing the
            # session at a task that will not stream.
            #
            # Canonical Project-then-Task lock order (deadlock-safe against the
            # dispatch / strategic-completion paths). Both helpers re-SELECT with
            # ``populate_existing=True``, so ``locked_task`` reflects a concurrent
            # runner claim committed on another connection — a stale identity-map
            # copy would silently defeat the re-validation.
            async with project_lock(
                session, project.id, skip_locked=False
            ) as locked_project:
                if locked_project is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Project unavailable to finalize resident session",
                    )
                async with task_lock(
                    session, adopt_task.id, skip_locked=False
                ) as locked_task:
                    if _task_is_adoptable(locked_task):
                        task = locked_task
                        _stamp_resident_context(
                            task,
                            session_id=session_id,
                            runner_id=effective_runner_id,
                            exam_ledger=carried_exam_ledger,
                            message_cursor=carried_message_cursor,
                            session_summary=carried_session_summary,
                            decisions_register=carried_decisions_register,
                        )
                        session.add(task)
                        workbench_state = (
                            await persist_strategic_workbench_update(
                                session,
                                operator_user_id,
                                project.id,
                                workbench_update,
                                sync_local_continuation_marker=False,
                                arm_interaction_mode=False,
                                commit=False,
                            )
                        ).state
                        vision = dict(locked_project.vision or {})
                        _arm_and_finalize_vision(
                            vision,
                            session_id=session_id,
                            workbench_state=workbench_state,
                            task_id_str=str(task.id),
                            operator_user_id=operator_user_id,
                            effective_runner_id=effective_runner_id,
                        )
                        locked_project.vision = vision
                        flag_modified(locked_project, "vision")
                        session.add(locked_project)
                        task_id_str = str(task.id)
                        adopted = True
            if adopted:
                await session.commit()
            else:
                # Candidate no longer adoptable (claimed as a one-shot under us):
                # spawn a fresh resident task instead of hijacking it.
                decision["mode"] = "spawn"

        if not adopted:
            # SPAWN path: the spawned strategic_review is BORN with the resident
            # context flags + runner pin seeded into its creating INSERT (via
            # ``_spawn_resident_strategic_task`` -> ``extra_context``), so it never
            # exists in a committed, claimable state without ``_resident_session``.
            # A separate post-spawn stamping transaction (the old approach) left a
            # sub-millisecond window in which a warm runner could claim the task
            # flag-less and dispatch it as the one-shot ``handleStrategic``; born-
            # with-the-flag closes that race. No re-stamp here — we only finalize
            # the session blob (record task_id) + arm the lease below.
            task = await _spawn_resident_strategic_task(
                session,
                project,
                effective_runner_id,
                session_id=session_id,
                exam_ledger=carried_exam_ledger,
                message_cursor=carried_message_cursor,
                session_summary=carried_session_summary,
                decisions_register=carried_decisions_register,
            )
            if task is None:
                raise HTTPException(
                    status_code=409,
                    detail="No strategic_review task available to host a resident session",
                )
            spawned_task_id = task.id
            workbench_state = (
                await persist_strategic_workbench_update(
                    session,
                    operator_user_id,
                    project.id,
                    workbench_update,
                    sync_local_continuation_marker=False,
                    arm_interaction_mode=False,
                    commit=False,
                )
            ).state
            task_id_str = str(task.id)

            def _arm_and_finalize(vision: dict[str, Any]) -> None:
                _arm_and_finalize_vision(
                    vision,
                    session_id=session_id,
                    workbench_state=workbench_state,
                    task_id_str=task_id_str,
                    operator_user_id=operator_user_id,
                    effective_runner_id=effective_runner_id,
                )

            await with_vision_locked(
                session, project.id, _arm_and_finalize, skip_locked=False
            )
            await session.commit()
    except Exception:
        # Snapshot the project id into a plain local BEFORE the rollback below.
        # ``session.rollback()`` expires EVERY ORM attribute on ``project`` (this
        # happens even under ``expire_on_commit=False``), so a later ``project.id``
        # read would trigger a sync lazy-load in async context and raise
        # ``MissingGreenlet`` — masking the real failure and leaking a half-
        # compensated start. Every post-rollback use below reads this snapshot,
        # never the (now-expired) ORM attribute. (``session_id`` and
        # ``spawned_task_id`` are already plain locals captured before the rollback.)
        project_id = project.id
        # Log the ORIGINAL failure (with traceback) FIRST, before any
        # compensation — so the true cause is visible next time even if the
        # caller session is poisoned and its own rollback trips over the dead
        # connection. Compensation below runs on a FRESH session/connection, so
        # it can no longer mask this exception (the prior bug: a poisoned-session
        # rollback inside compensation raised MissingGreenlet on a pool pre-ping
        # and replaced the real error, leaving a half-initialized session behind).
        logger.exception(
            "[RESIDENT_SESSION] start failed for project %s (session %s); "
            "compensating on a fresh session",
            project_id,
            session_id,
        )
        # Best-effort caller-session rollback so its connection is returned
        # cleanly. If the connection was already poisoned this may itself raise;
        # swallow it — compensation runs on a fresh session regardless, and we
        # must still re-raise the ORIGINAL failure below, not the rollback error.
        try:
            await session.rollback()
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] caller-session rollback failed during start "
                "compensation for project %s (continuing on a fresh session)",
                project_id,
                exc_info=True,
            )
        await _release_start_claim(
            project_id, session_id, spawned_task_id, session_factory=session_factory
        )
        raise

    await session.refresh(project)
    logger.info(
        "[RESIDENT_SESSION] started session %s (task %s) for project %s",
        session_id,
        task_id_str,
        project.id,
    )
    return resident_session_snapshot(project.vision) or {}


async def _run_resident_completion_on_stop(
    task_id: uuid.UUID,
    *,
    session_factory=None,
) -> None:
    """Run strategic completion processing for an operator-stopped resident task.

    When an operator stops a resident session, the backing task is cancelled
    server-side before the agent can post its terminal status, so the normal
    post-commit completion pipeline never fires.  This helper bridges that gap:
    it opens a fresh, dedicated session (the same isolation idiom as
    ``_release_start_claim``), re-fetches the task, and calls
    ``_handle_strategic_completion`` so cycle-closure events, pending playbook
    entries, and retirement memos all run exactly once.

    Only invoked when the task context holds stashed strategic work (a
    qualify_request, version_phase_decision, new_version_goal, or
    new_acceptance_criteria written by set_problem_model / set_vision, or
    any api_was_used marker set during the session).  Skipping the handler for
    tasks with no activity avoids false _strategic_failures increments: the
    handler's fallback path (no API, no parseable logs) would otherwise record
    a spurious failure and raise an alert.

    When there IS work to process the project row is locked via
    ``lock_project_for_strategic_transition`` before calling the handler,
    satisfying the C3 locking invariant the api_was_used branch requires.

    Best-effort: any exception is logged and swallowed — completion processing
    must never mask or roll back the stop itself.
    """
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as fresh_session:
            task = await fresh_session.get(Task, task_id)
            if task is None:
                logger.warning(
                    "[RESIDENT_SESSION] _run_resident_completion_on_stop: task %s not found",
                    task_id,
                )
                return
            from app.strategic_completion import (
                QUALIFY_REQUEST_CONTEXT_KEY,
                VERSION_PHASE_DECISION_CONTEXT_KEY,
                NEW_VERSION_GOAL_CONTEXT_KEY,
                NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
                _handle_strategic_completion,
            )

            ctx = task.context or {}
            # Keys stashed by set_problem_model / set_vision API endpoints
            _STASH_KEYS = (
                QUALIFY_REQUEST_CONTEXT_KEY,
                VERSION_PHASE_DECISION_CONTEXT_KEY,
                NEW_VERSION_GOAL_CONTEXT_KEY,
                NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
            )
            # Counters / flags set by strategic API calls during the session
            _API_MARKERS = (
                "_strategic_vision_set",
                "_strategic_dirs_created",
                "_strategic_dirs_retired",
                "_strategic_dirs_staged",
                "_strategic_dirs_updated",
                "_strategic_proposals_approved",
                "_strategic_proposals_rejected",
                "_strategic_tasks_spawned",
                "_strategic_api_called",
            )
            has_strategic_work = any(ctx.get(k) for k in _STASH_KEYS) or any(
                ctx.get(k) for k in _API_MARKERS
            )
            if not has_strategic_work:
                logger.info(
                    "[RESIDENT_SESSION] _run_resident_completion_on_stop: task %s has no stashed "
                    "strategic work — skipping completion processing (operator stop with no API activity)",
                    task_id,
                )
                return

            # Acquire the project row lock before entering the handler.  The
            # api_was_used branch inside _handle_strategic_completion assumes the
            # caller pre-acquired the lock (C3 pair-locker); calling without it
            # would run mark_phase2_completed and the _apply_* chain unlocked.
            from app.task_locking import lock_project_for_strategic_transition

            locked_project = await lock_project_for_strategic_transition(
                fresh_session, task.project_id
            )
            if locked_project is None:
                logger.warning(
                    "[RESIDENT_SESSION] _run_resident_completion_on_stop: could not acquire project "
                    "lock for task %s — skipping completion processing",
                    task_id,
                )
                return

            await _handle_strategic_completion(task, "", fresh_session, project=locked_project)
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] _run_resident_completion_on_stop: completion processing failed for task %s",
            task_id,
            exc_info=True,
        )


async def stop_resident_session(
    session: AsyncSession,
    project: Project,
    *,
    operator_user_id: str | None = None,
) -> dict[str, Any]:
    """Stop the resident session: release the lease and drive the task terminal.

    Sets ``stop_requested`` + ``status=ended`` on the session blob, releases the
    continuation lease (workbench back to ``chat`` mode pops the marker), and
    drives the resident ``strategic_review`` task to a terminal status so the
    legacy cadence reclaims authority immediately (instant manual reclaim).

    No-op when there is no session-state blob at all: a stop against a project
    that never ran a session (e.g. the auto-stop on a ``continuous``→non-continuous
    transition while the feature is dark, or a manual stop with nothing running)
    must NOT synthesize a phantom ``{session_id: null, status: "ended"}`` record —
    that would spuriously churn ``Project.vision`` and surface a non-null null-id
    "ended" session in API responses even though nothing ever started. A dark /
    never-started feature stays fully inert.
    """
    blob = read_resident_session(project.vision)
    if blob is None:
        # Nothing to stop — leave ``Project.vision`` untouched (no synthetic
        # ended blob, no lease pop, no mode churn) so the feature stays inert.
        return resident_session_snapshot(project.vision) or {}
    now_iso = _utcnow().isoformat()
    op_id = operator_user_id or (blob or {}).get("operator_user_id")

    # Put the operator workbench back to chat mode (no vision write / no commit
    # here — the lease marker is popped and the blob is ended together in the one
    # atomic vision write below, keeping the whole stop a single transaction).
    if op_id:
        await persist_strategic_workbench_update(
            session,
            str(op_id),
            project.id,
            {"interaction_mode": "chat"},
            sync_local_continuation_marker=False,
            arm_interaction_mode=False,
            commit=False,
        )

    # Canonical Project→Task lock order (one change fixes the AB-BA deadlock,
    # context lost-update, and double-completion findings). The heartbeat mirror
    # takes the project lock (with_vision_locked) THEN this task's row lock; the
    # prior stop code mutated + session.add-ed the task BEFORE with_vision_locked,
    # so autoflush locked the task row first (Task→Project, the opposing order).
    # Ending the blob first and locking the task only while the project lock is
    # still held makes stop strictly Project→Task, so neither path can deadlock.
    def _release_and_end(vision: dict[str, Any]) -> None:
        # Release the continuation lease (the resident session is over) and mark
        # the session blob ended, in one locked read-modify-write.
        vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)
        current = vision.get(RESIDENT_SESSION_KEY)
        if not isinstance(current, dict):
            # The record vanished under us (a concurrent removal after the
            # pre-lock read) — release the lease but do NOT resurrect a synthetic
            # null-id ended blob (same phantom-write the top-level ``blob is None``
            # guard prevents).
            return
        ended = dict(current)
        ended["status"] = STATUS_ENDED
        ended["stop_requested"] = True
        ended["ended_at"] = now_iso
        ended["updated_at"] = now_iso
        vision[RESIDENT_SESSION_KEY] = ended

    await with_vision_locked(session, project.id, _release_and_end, skip_locked=False)

    # Drive the resident task terminal under the TASK row lock, held with the
    # project lock from above. Re-read FRESH inside the lock (task_lock uses
    # populate_existing) to fix two hazards of the prior unlocked session.get:
    # (1) context lost-update — the failure_category merge reads the CURRENT
    #     context, so it can't clobber a concurrent strategic-API commit's
    #     _strategic_api_* markers (that writer must take the project lock we hold
    #     first, so it can't reach the task lock); (2) double-completion — status
    #     is re-checked, so a task that already went terminal on its own is left
    #     to the normal post-commit pipeline (mark_phase2_completed has no guard).
    cancelled_task_id: uuid.UUID | None = None
    task_id = (blob or {}).get("task_id")
    resident_task_uuid: uuid.UUID | None = None
    if task_id:
        try:
            resident_task_uuid = uuid.UUID(str(task_id))
        except (ValueError, TypeError):
            resident_task_uuid = None
    if resident_task_uuid is not None:
        async with task_lock(
            session, resident_task_uuid, skip_locked=False
        ) as locked_task:
            if (
                locked_task is not None
                and locked_task.status in _LIVE_TASK_STATUSES
            ):
                locked_task.status = TaskStatus.CANCELLED
                locked_task.latest_status_message = (
                    "Resident strategic session stopped by operator"
                )
                ctx = dict(locked_task.context or {})
                ctx["failure_category"] = ADMIN_CANCEL
                locked_task.context = ctx
                flag_modified(locked_task, "context")
                session.add(locked_task)
                cancelled_task_id = locked_task.id

    await session.commit()
    await session.refresh(project)
    logger.info(
        "[RESIDENT_SESSION] stopped session for project %s (task %s)",
        project.id,
        task_id,
    )

    # Best-effort: resolve open messages from the ending session so they don't
    # re-appear as fresh inbox items in later strategic payloads.
    _session_id = (blob or {}).get("session_id")
    if _session_id:
        try:
            from app.session_message_handoff import handoff_session_messages

            _handed_off = await handoff_session_messages(session, project.id, _session_id)
            if _handed_off:
                await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.warning(
                "[RESIDENT_SESSION] message handoff raised for session %s — stop still committed",
                _session_id,
                exc_info=True,
            )

    if cancelled_task_id is not None:
        try:
            await _run_resident_completion_on_stop(cancelled_task_id)
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] stop-time completion processing raised for task %s — stop still committed",
                cancelled_task_id,
                exc_info=True,
            )

    return resident_session_snapshot(project.vision) or {}


async def sync_resident_session_for_interaction_mode(
    session: AsyncSession,
    project: Project,
    *,
    operator_user_id: str,
    previous_mode: str | None,
    new_mode: str | None,
    session_factory=None,
) -> dict[str, Any] | None:
    """Auto start/stop the resident session on an operator ``interaction_mode`` change.

    Workday (``continuous``) is the on-switch. ENTERING it auto-starts the resident
    session when the ``strategic_session_enabled`` flag is on AND a live, eligible
    local runner exists (the session runs on the operator's box, not a cloud VM);
    LEAVING it auto-stops. Called from the mode-change admin endpoint AFTER the
    workbench state (the mode itself) has already been committed, so this is a
    best-effort side effect — it NEVER raises and never blocks the mode switch.

    **Session isolation (poison containment).** The auto-start + reason-recording
    run on FRESH, dedicated ``AsyncSessionLocal`` sessions — NEVER the caller's
    request ``session`` (mirrors ``_release_start_claim``'s Track-1 idiom). A
    failed auto-start can invalidate its own connection (the Cloud SQL drop /
    protocol-corruption class ``start_resident_session`` guards against); running
    it on the request session would poison it, so the trailing
    ``get_project_strategic_state`` read the endpoint runs on the SAME session
    would 5xx a mode change that already committed — violating the "never breaks
    the mode switch" guarantee, and the blocked-reason would fail to record too.
    Isolating the work keeps the request session clean; a ``start_failed`` reason
    is recorded on a session distinct from the (possibly poisoned) start session
    so it is durably surfaced regardless. The caller's ``session`` is rolled back
    up front purely to (a) release the shared connection under the single-connection
    test pool and (b) expire its identity-mapped ``project`` so the trailing read
    reflects the freshly-committed session blob / reason (``expire_on_commit`` is
    False in prod, so a cached ``project`` would otherwise read stale).

    **NOTE (follow-up #3 — mode-change latency).** The auto-start is still
    ``await``-ed inline, so entering Workday delays the HTTP response by the whole
    spawn/adopt (+ up to three 30s lock waits). It is deliberately NOT moved to a
    fire-and-forget / FastAPI ``BackgroundTask``: on Cloud Run, CPU is throttled
    to ~0 once the response is sent, so a ~30s post-response start would be
    starved/killed and silently never complete, leaving the session wedged in a
    "starting" limbo with no task. A lossy background start is strictly worse than
    a slow-but-correct mode switch, so the real fix for #3 is to make the SPAWN
    fast, not to background a start that may never finish. Tracked as a follow-up.

    Resilience: a blocked/failed auto-start is recorded as a reason the strategic
    read model surfaces (``no_runner`` / ``start_failed: <msg>``), never a silent
    no-op or a 5xx. A clean start (or any stop) clears the recorded reason.
    Returns the session snapshot on a successful auto-start, else None.

    Only the ENTER/LEAVE transitions act; an unrelated workbench save (e.g. a
    ``selected_refs`` edit that keeps the same mode) is a no-op here.
    """
    entered = new_mode == "continuous" and previous_mode != "continuous"
    left = previous_mode == "continuous" and new_mode != "continuous"
    if not entered and not left:
        return None

    if entered and not runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False):
        # Feature off: the read model derives ``sessions_disabled`` from the flag
        # itself — nothing to record, nothing to start. Short-circuit BEFORE
        # touching the caller session or opening any dedicated session.
        return None

    factory = session_factory or AsyncSessionLocal

    # Snapshot the project id into a plain local BEFORE the rollback below. The
    # rollback intentionally expires the caller's cached ORM ``project`` (so the
    # endpoint's trailing read reloads the freshly-committed state), but that also
    # means any ``project.id`` read AFTER it would trigger a sync lazy-load in
    # async context → ``MissingGreenlet``. Every post-rollback use in this function
    # reads this plain snapshot, never the (now-expired) ORM attribute.
    project_id = project.id

    # Release the caller session's transaction / expire its cached ``project`` so
    # (a) the shared single-connection test pool is free for the dedicated session
    # below, and (b) the endpoint's trailing read reloads the freshly-committed
    # state. The mode itself was already committed by the caller, so this discards
    # nothing. Best-effort: a request session that is somehow unusable here must
    # still not break the mode switch.
    try:
        await session.rollback()
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] caller-session rollback before sync failed for "
            "project %s (continuing on a dedicated session)",
            project_id,
            exc_info=True,
        )

    try:
        if entered:
            return await _auto_start_on_workday_entry(
                project_id,
                operator_user_id=operator_user_id,
                session_factory=factory,
            )

        # left continuous → auto-stop (hand authority back to the cadence) and
        # clear any recorded reason, on a dedicated session.
        try:
            async with factory() as fresh:
                fresh_project = await fresh.get(Project, project_id)
                if fresh_project is not None:
                    await stop_resident_session(
                        fresh, fresh_project, operator_user_id=operator_user_id
                    )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] auto-stop on Workday exit failed for project %s",
                project_id,
                exc_info=True,
            )
        await _record_blocked_reason_isolated(
            project_id, None, session_factory=factory
        )
        return None
    except Exception:
        # Belt-and-suspenders: no part of the auto start/stop path may break the
        # operator's mode switch (already persisted before this ran).
        logger.warning(
            "[RESIDENT_SESSION] sync for interaction_mode change failed for "
            "project %s",
            project_id,
            exc_info=True,
        )
        return None


async def _auto_start_on_workday_entry(
    project_id: uuid.UUID,
    *,
    operator_user_id: str,
    session_factory,
) -> dict[str, Any] | None:
    """Auto-start the resident session on Workday entry, on a dedicated session.

    Gates on (and pins to) a LIVE, strategic-capable local runner — the same
    eligibility the manual start path auto-selects with. No runner → record
    ``no_runner`` and stand down (do NOT dispatch to a cloud VM; Workday runs on
    the operator's box). The runner-gate + start run on ONE fresh session; a
    ``start_failed`` reason is recorded on a SEPARATE fresh session because the
    start session may be poisoned by the failure. Never raises.
    """
    try:
        async with session_factory() as fresh:
            project = await fresh.get(Project, project_id)
            if project is None:
                return None
            runner_id = await _auto_select_local_runner(fresh, project_id)
            if runner_id is None:
                # Healthy session — record the gate reason directly on it.
                await set_resident_session_blocked_reason(
                    fresh, project, RESIDENT_SESSION_REASON_NO_RUNNER
                )
                return None
            # Pre-check: if no adoptable strategic_review exists AND a batch
            # strategic cycle is in flight, the spawn path would be refused with
            # a generic 409. Detect this early and record a named reason so the
            # read model can surface a non-alarming "waiting on active cycle"
            # state, and the completion handler can retry once the cycle finishes.
            _adopt_hint = await _find_adoptable_strategic_review_task(
                fresh, project.id
            )
            if _adopt_hint is None:
                from app.task_lifecycle import _has_active_strategic_task

                if await _has_active_strategic_task(project.id, fresh):
                    await _set_cycle_in_flight_reason(
                        fresh, project, operator_user_id
                    )
                    return None
            snapshot = await start_resident_session(
                fresh,
                project,
                operator_user_id=operator_user_id,
                runner_id=runner_id,
            )
            # Started cleanly — drop any stale blocked-reason on the same session.
            await set_resident_session_blocked_reason(fresh, project, None)
            return snapshot
    except Exception as exc:
        # Track-1 makes start fail fast + clean (real error logged, compensated
        # on a fresh session, no half-initialized blob left). The dedicated
        # session above may be poisoned by that failure, so record the surfaced
        # reason on ANOTHER fresh session and stand down.
        logger.warning(
            "[RESIDENT_SESSION] auto-start on Workday entry failed for project "
            "%s: %s",
            project_id,
            exc,
            exc_info=True,
        )
        await _record_blocked_reason_isolated(
            project_id,
            f"{RESIDENT_SESSION_REASON_START_FAILED_PREFIX}: {_short_error(exc)}",
            session_factory=session_factory,
        )
        return None


async def renew_resident_session_lease(
    session: AsyncSession,
    project: Project,
    *,
    session_id: str,
    task_id: str,
    progress: bool = False,
    note: str | None = None,
    message_cursor: str | None = None,
    handled_message_ids: list[str] | None = None,
    turn_latency_summary: dict | None = None,
    compaction_count: int | None = None,
    last_compaction_at: str | None = None,
    decisions_reg: str | None = None,
) -> dict[str, Any]:
    """Renew the continuation lease from a resident-agent heartbeat.

    Validates the heartbeat against the recorded session, then applies four
    coordinated lease rules:

    * **Progress-gated freshness.** Continuation *freshness* renews only on
      ``progress=True`` (an applied strategic op) OR fresh operator interaction —
      passing ``renewal_reason='strategic_progress'`` to
      ``apply_strategic_local_continuation_marker``. A bare liveness heartbeat
      (``progress=False``) is liveness-only: it PRESERVES the marker's existing
      freshness (re-passing its own ``last_seen_at``, no ``renewal_reason``) and
      never bumps it, so mere runner liveness can't extend the dead-man's switch.

    * **Unattended cap → END the session.** If the operator has not interacted
      for longer than ``strategic_session_max_unattended_seconds`` (an elapsed,
      inherently-sustained condition) the session is ENDED: ``stop_requested`` +
      ``status=ended`` are stamped on the blob and the lease released, and
      ``stop_requested=True`` / ``authority_held=False`` is returned. The agent
      ends cleanly on ``stop_requested`` and posts the task terminal, so the
      resident ``strategic_review`` frees and the legacy cadence resumes — a
      productive-but-unattended session thus winds down within ≤ the cap rather
      than pinning strategic authority (and blocking ``_spawn_strategic_review``)
      forever. A fresh operator message BEFORE the cap elapses re-arms the clock
      and keeps the session alive.

    * **Lease lapse → END the session.** If, after applying this heartbeat, the
      continuation freshness is genuinely STALE — a marker with a valid freshness
      timestamp elapsed past the TTL (``strategic_continuation_ttl_seconds``),
      i.e. no progress AND no operator interaction for the whole window — the
      session is likewise ENDED and ``stop_requested=True`` returned, handing
      authority back to the cadence within ≤ the TTL. This keys off the
      elapsed-time ``reason == "stale"`` verdict specifically, NOT a transient /
      benign non-fresh read (a just-armed session, a missing marker, or a missing
      timestamp is never treated as a lapse), so a freshly-started or
      actively-chatting / actively-working session is NEVER stop-requested.

    * **Stop/heartbeat race.** The FRESH in-lock blob is re-read inside the row
      lock; if a stop landed after the pre-lock snapshot the lease is NOT
      re-armed and ``stop_requested=True`` / ``authority_held=False`` is returned.

    Returns the lease view the agent loop steers on:
    ``{authority_held, leased_until, stop_requested, status}``.
    """
    blob = read_resident_session(project.vision)
    if blob is None:
        raise HTTPException(status_code=404, detail="No resident session for project")
    if blob.get("session_id") != session_id or str(blob.get("task_id")) != str(task_id):
        raise HTTPException(status_code=409, detail="Heartbeat session/task mismatch")

    op_id = blob.get("operator_user_id")
    runner_id = blob.get("runner_id")
    stop_requested = bool(blob.get("stop_requested"))
    status = blob.get("status") or STATUS_ACTIVE
    now = _utcnow()
    now_iso = now.isoformat()

    # READ-ONLY work-state signal for the agent's state-gated pacing. Computed
    # once, up front, from cheap aggregate queries; additive to every return
    # branch below. It NEVER touches lease/freshness/session state, so it cannot
    # extend the dead-man's switch (pinned by
    # test_heartbeat_liveness_only_does_not_extend_lease). Older agents ignore
    # the extra fields.
    work_state_rev, work_state_summary = await _compute_resident_work_state(
        session, project.id, resident_session_id=session_id
    )

    # Parse the agent's serialized exam-ledger from the heartbeat note (JSON,
    # guarded). When valid, the string is written into the session blob as
    # ``_exam_ledger`` so a restart can restore the ledger instead of beginning
    # from scratch. Best-effort: malformed or absent JSON is silently ignored.
    exam_ledger_str: str | None = None
    if isinstance(note, str) and note.strip():
        try:
            parsed_note = json.loads(note)
            if isinstance(parsed_note, dict):
                # Only store when the full JSON fits; truncating mid-value
                # yields invalid JSON that causes JSON.parse to throw on restart.
                if len(note.encode()) <= _EXAM_LEDGER_MAX_BYTES:
                    exam_ledger_str = note
        except Exception:
            pass

    # Cheap indexed read of live direction keys for the agent's surface-catalog
    # refresh AND direction_grounding (text + status) so the compaction
    # checkpoint can show current directions rather than session-start
    # snapshots. Returns only non-retired directions so newly created or newly
    # retired directions immediately enter/leave the rotation on the next tick.
    _direction_rows = (
        await session.execute(
            select(Direction.direction_key, Direction.text, Direction.status).where(
                Direction.project_id == project.id,
                Direction.status.in_(list(_ACTIVE_DIRECTION_STATUSES)),
            )
        )
    ).all()
    active_direction_ids = [str(row[0]) for row in _direction_rows]
    direction_grounding: list[dict[str, str]] = [
        {"id": str(row[0]), "text": str(row[1] or ""), "status": str(row[2])}
        for row in _direction_rows
    ]

    # Stop-requested / ended (pre-lock snapshot): do NOT renew — let the lease
    # lapse so the cadence can reclaim, and signal the agent to wind down.
    if stop_requested or status == STATUS_ENDED:
        cont = strategic_local_continuation_status_from_vision(
            project.vision, operator_user_id=op_id
        )
        return {
            "authority_held": False,
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": status,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }

    # Unattended cap: compare now against the last operator interaction, taking
    # the newer of the recorded blob timestamp and any fresh operator message.
    max_unattended = runtime_config.get(
        STRATEGIC_SESSION_MAX_UNATTENDED_SETTING,
        _DEFAULT_STRATEGIC_SESSION_MAX_UNATTENDED_SECONDS,
    )
    last_op_dt = _parse_iso_naive(
        blob.get("last_operator_interaction_at")
    ) or _parse_iso_naive(blob.get("started_at"))
    latest_msg_dt = await _latest_operator_interaction_at(
        session, project.id, resident_session_id=session_id
    )
    op_reengaged = latest_msg_dt is not None and (
        last_op_dt is None or latest_msg_dt > last_op_dt
    )
    effective_last_op = latest_msg_dt if op_reengaged else last_op_dt
    unattended_exceeded = (
        not op_reengaged
        and effective_last_op is not None
        and (now - effective_last_op).total_seconds() > float(max_unattended)
    )

    holder = {
        "authority_held": False,
        "stopped": False,
        "capped": False,
        "lapsed": False,
    }

    def _end_session(vision: dict[str, Any], current: dict[str, Any]) -> None:
        """Drive OUR session blob terminal + release the lease, in-lock.

        Stamps ``stop_requested`` + ``status=ended`` so a re-query reflects the
        ending and the next heartbeat short-circuits at the pre-lock stop check;
        pops the continuation marker so required-local tasks fall back to cloud
        and the cadence is no longer pinned (mirrors ``stop_resident_session``).
        The agent reads ``stop_requested=True`` from the response and ends its
        loop, posting the resident task terminal so the cadence reclaims.
        """
        ended = dict(current)
        ended["status"] = STATUS_ENDED
        ended["stop_requested"] = True
        ended["ended_at"] = now_iso
        ended["updated_at"] = now_iso
        vision[RESIDENT_SESSION_KEY] = ended
        vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)

    def _renew(vision: dict[str, Any]) -> None:
        fresh = read_resident_session(vision)
        if not isinstance(fresh, dict):
            # Session record vanished under us — treat as no authority.
            holder["stopped"] = True
            return
        # Stop/heartbeat race: re-read the FRESH in-lock blob. If a stop landed
        # after our pre-lock snapshot, do NOT re-arm the lease.
        if bool(fresh.get("stop_requested")) or fresh.get("status") == STATUS_ENDED:
            holder["stopped"] = True
            return
        # Finding 1 (unattended cap): the session has run past the max-unattended
        # cap with no operator interaction. This is an elapsed, sustained
        # condition — END the session (release the lease, stamp ended) so the
        # resident task can go terminal and the cadence reclaims, rather than
        # leaving it alive-but-passive and blocking the cadence indefinitely.
        if unattended_exceeded:
            holder["capped"] = True
            _end_session(vision, fresh)
            return

        marker = vision.get(STRATEGIC_LOCAL_CONTINUATION_KEY)
        owner_started = (
            marker.get("owner_session_started_at")
            if isinstance(marker, dict)
            else None
        )
        prior_last_seen = (
            marker.get("last_seen_at") if isinstance(marker, dict) else None
        )
        # Finding 4: this heartbeat renews freshness ONLY when the request body
        # reports progress==True (an applied strategic op). Operator interaction
        # also renews freshness per the lease contract, but via the operator's
        # OWN message/workbench path (sync_local_continuation_marker=True) — not
        # this heartbeat, which uses a fresh operator message only to re-arm the
        # unattended cap (below). Bare liveness (progress=False) NEVER bumps it.
        renew_freshness = bool(progress)
        synthetic_workbench: dict[str, Any] = {
            "interaction_mode": "continuous",
            "session_started_at": owner_started or fresh.get("started_at"),
        }
        if runner_id:
            synthetic_workbench["preferred_runner_id"] = runner_id

        if renew_freshness:
            apply_strategic_local_continuation_marker(
                vision,
                synthetic_workbench,
                operator_user_id=str(op_id or ""),
                preferred_runner_id=str(runner_id) if runner_id else None,
                renewal_reason="strategic_progress",
            )
            holder["authority_held"] = True
        elif isinstance(marker, dict) and prior_last_seen:
            # Liveness-only heartbeat: PRESERVE the existing freshness (re-pass
            # the marker's own last_seen_at, no renewal_reason) — never bump it,
            # so bare liveness cannot extend the dead-man's-switch lease. The
            # authority we report is then whatever the un-bumped freshness says.
            synthetic_workbench["last_interaction_at"] = prior_last_seen
            apply_strategic_local_continuation_marker(
                vision,
                synthetic_workbench,
                operator_user_id=str(op_id or ""),
                preferred_runner_id=str(runner_id) if runner_id else None,
                renewal_reason=None,
            )
            holder["authority_held"] = True
        # else: no marker to preserve — do NOT arm one on bare liveness (that
        # would renew freshness from nothing, defeating the progress gate).

        # Finding 1 (lease lapse): after applying this heartbeat, if the
        # continuation freshness is genuinely STALE — a marker with a valid
        # freshness timestamp that has elapsed past the TTL, i.e. no progress AND
        # no operator interaction for the whole window — END the session so the
        # cadence reclaims. We key strictly off the elapsed-time ``stale`` verdict
        # (an inherently-sustained condition): a just-armed/fresh session reads
        # ``armed``, and a missing marker / missing timestamp reads
        # ``not_enabled`` / ``missing_timestamp`` — none of which are treated as a
        # lapse — so a freshly-started or actively-working session is never ended.
        post = strategic_local_continuation_status_from_vision(
            vision, operator_user_id=op_id
        )
        if post.get("reason") == "stale":
            holder["lapsed"] = True
            # Report no authority (the marker is stale) and end the session.
            holder["authority_held"] = False
            _end_session(vision, fresh)
            return

        updated = dict(fresh)
        updated["updated_at"] = now_iso
        if progress:
            updated["last_progress_at"] = now_iso
        if op_reengaged and latest_msg_dt is not None:
            updated["last_operator_interaction_at"] = latest_msg_dt.isoformat()
        # Persist the agent's serialized exam-ledger so it survives a restart.
        # ``_exam_ledger`` is NOT exposed by ``resident_session_snapshot``; it is
        # an opaque blob the agent reads back via ``task.context`` on startup.
        if exam_ledger_str is not None:
            updated["_exam_ledger"] = exam_ledger_str
        # Persist the decisions register so a full restart (VM recycle, process
        # crash) can restore settled cross-tick decisions instead of falling back
        # to empty (which re-opens the re-derivation gap the register closes).
        if decisions_reg is not None:
            updated["_decisions_reg"] = decisions_reg
        # Persist the advancing message cursor so a restart resumes from the
        # high-water mark rather than re-delivering already-answered messages.
        if message_cursor:
            updated["_message_cursor"] = message_cursor
        # Persist the rolling per-kind turn-latency summary for operator-visible
        # observability. Bounded to a simple dict; ignored when absent/None.
        if isinstance(turn_latency_summary, dict):
            updated["_turn_latency_summary"] = turn_latency_summary
        # Persist compaction metadata reported by the agent. These are
        # opaque agent-reported values — the backend never derives them.
        if isinstance(compaction_count, int) and compaction_count > 0:
            updated["compaction_count"] = compaction_count
        if isinstance(last_compaction_at, str) and last_compaction_at:
            updated["last_compaction_at"] = last_compaction_at
        vision[RESIDENT_SESSION_KEY] = updated

    await with_vision_locked(session, project.id, _renew, skip_locked=False)
    # Mirror the exam ledger and message cursor into task.context so a runner
    # restart can read them back on startup (the agent reads task.context at pull
    # time; project.vision is not visible to the agent).
    _need_ctx_update = exam_ledger_str is not None or bool(message_cursor) or decisions_reg is not None
    if _need_ctx_update:
        try:
            _ledger_task_id = uuid.UUID(task_id)
        except (ValueError, TypeError):
            _ledger_task_id = None
        if _ledger_task_id is not None:

            def _mirror_ledger(ctx: dict) -> None:
                # Only these keys are the resident-heartbeat's durable
                # restart state; every OTHER key in ``ctx`` (e.g. the
                # strategic-API ``_strategic_api_called`` / ``_strategic_vision_set``
                # flags) belongs to another writer and is preserved untouched.
                if exam_ledger_str is not None:
                    ctx["_exam_ledger"] = exam_ledger_str
                if message_cursor:
                    ctx["message_cursor"] = message_cursor
                if decisions_reg is not None:
                    ctx["_decisions_reg"] = decisions_reg

            # Re-read Task.context FRESH under FOR UPDATE (populate_existing)
            # before the read-modify-write so a concurrent strategic-API
            # whole-column write cannot be clobbered (lost update), and vice
            # versa. We already hold the project row lock here (``with_vision_locked``
            # above holds it until this session commits), so acquiring the task
            # lock now is the canonical Project→Task order and cannot deadlock.
            # skip_locked=False so this durable restart-state mirror waits for
            # (rather than silently drops) the row; the wait is bounded and, since
            # we hold the project lock, the row is effectively uncontended by the
            # strategic-API paths (they cannot hold the task lock without first
            # holding the project lock we own).
            await with_task_context_locked(
                session,
                _ledger_task_id,
                _mirror_ledger,
                skip_locked=False,
                timeout=10.0,
            )
    # Mark inbound operator messages as handled so a restart does not re-answer
    # them and so they stop counting as open backlog. Scoped to the project and
    # session to avoid touching rows that belong to another session's conversation.
    if handled_message_ids:
        # Wrap in a SAVEPOINT so a DB error here only rolls back the
        # handled-ids UPDATE, leaving the outer transaction (the vision lease
        # renewal + task-context mirror) intact and committed below.
        try:
            safe_ids: list[uuid.UUID] = []
            for uid in handled_message_ids:
                if not isinstance(uid, str) or not uid:
                    continue
                try:
                    safe_ids.append(uuid.UUID(uid))
                except ValueError:
                    pass
            if safe_ids:
                async with session.begin_nested():
                    await session.execute(
                        sa_update(ProjectMessage)
                        .where(
                            ProjectMessage.project_id == project.id,
                            ProjectMessage.id.in_(safe_ids),
                            # Strictly own-session: legacy/cadence NULL-session
                            # rows are project-wide standing messages (e.g.
                            # BUG_REPORTs) that a single session's heartbeat must
                            # NOT resolve — the poll delivers them for visibility
                            # only. Enforced invariant (test_heartbeat_handled_ids
                            # _only_affect_own_session) and mirrored by
                            # session_message_handoff's never-touch-unstamped rule.
                            ProjectMessage.resident_session_id == session_id,
                            ProjectMessage.status == "open",
                        )
                        .values(status="handled")
                    )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] handled_message_ids update failed for session %s — ignored",
                session_id,
                exc_info=True,
            )
    await session.commit()
    await session.refresh(project)

    cont = strategic_local_continuation_status_from_vision(
        project.vision, operator_user_id=op_id
    )
    refreshed = read_resident_session(project.vision) or {}
    refreshed_stop = bool(refreshed.get("stop_requested"))

    if holder["stopped"]:
        return {
            "authority_held": False,
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": refreshed.get("status") or STATUS_ENDED,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
    if holder["capped"] or holder["lapsed"]:
        # Finding 1: the unattended cap was exceeded, or the lease genuinely
        # lapsed (stale past TTL). The session has been ENDED in-lock (blob
        # stamped ended + lease released) so the resident task can go terminal
        # and the legacy cadence reclaims strategic authority. Signal the agent
        # to wind down.
        # Best-effort: resolve session messages so they don't re-enter the inbox.
        try:
            from app.session_message_handoff import handoff_session_messages

            _handed_off = await handoff_session_messages(session, project.id, session_id)
            if _handed_off:
                await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.warning(
                "[RESIDENT_SESSION] message handoff raised for session %s (cap/lapse) — ignored",
                session_id,
                exc_info=True,
            )
        return {
            "authority_held": False,
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": refreshed.get("status") or STATUS_ENDED,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
    return {
        "authority_held": bool(cont.get("fresh"))
        and not refreshed_stop
        and holder["authority_held"],
        "leased_until": cont.get("expires_at"),
        "stop_requested": refreshed_stop,
        "status": refreshed.get("status") or STATUS_ACTIVE,
        "work_state_rev": work_state_rev,
        "work_state_summary": work_state_summary,
        "exam_ledger": exam_ledger_str,
        "active_direction_ids": active_direction_ids,
        "direction_grounding": direction_grounding,
    }


# ---------------------------------------------------------------------------
# Agent-token heartbeat endpoint
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/strategic-session/checkpoint-state")
@limiter.limit("30/minute")
async def agent_fetch_checkpoint_state(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return current project state for the resident agent's compaction checkpoint.

    Called only at compaction time (context-window pressure). Returns the freshest
    strategic state — problem model, memory notes, running update, and vision —
    so the compaction checkpoint carries current data rather than the session-start
    bootstrap snapshot, which would be stale after mid-session mutations
    (set-problem-model, set-memory, set-running-update).
    """
    project = await _resolve_agent_project(project_id, token, session)
    open_messages = await _fetch_open_messages(session, project.id)
    # Apply the SAME agent-view trims the session bootstrap applies so the
    # refreshed compaction grounding matches the shape the session started from.
    # The bootstrap ships _agent_view_problem_model(project.problem_model) and
    # _agent_visible_vision(project.vision) (strategic_spawning.py:5711-5712);
    # returning the raw project.problem_model / project.vision here would make a
    # successful refresh re-bloat the checkpoint with archived_gaps,
    # rejected_agent_edits, unwindowed per-gap memory, and the internal vision
    # continuation blob — the opposite of the coherence the refresh preserves.
    from app.strategic_local_runner import _agent_visible_vision
    from app.strategic_problem_view import _agent_view_problem_model

    return {
        "problem_model": _agent_view_problem_model(project.problem_model),
        "strategic_memory_notes": project.strategic_memory_notes or [],
        "running_update": project.running_update or "",
        "vision": _agent_visible_vision(project.vision),
        "open_messages": open_messages,
    }


class ResidentSessionHeartbeatRequest(BaseModel):
    session_id: str
    task_id: str
    progress: bool | None = None
    note: str | None = None
    message_cursor: str | None = None
    handled_message_ids: list[str] | None = None
    turn_latency_summary: dict | None = None
    compaction_count: int | None = None
    last_compaction_at: str | None = None
    decisions_reg: str | None = None


@router.post("/projects/{project_id}/strategic-session/heartbeat")
@limiter.limit("60/minute")
async def resident_session_heartbeat(
    request: Request,
    project_id: uuid.UUID,
    payload: ResidentSessionHeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Renew the resident session's continuation lease and read stop state.

    Agent-token auth (mirrors ``agent_strategic_fetch`` — the token's ``sub``
    task must belong to ``project_id``). The heartbeating token must be the
    resident session task itself.
    """
    project = await _resolve_agent_project(project_id, token, session)
    if str(token.get("sub")) != str(payload.task_id):
        # The heartbeat must come from the resident session task's own token.
        raise HTTPException(status_code=403, detail="Token task mismatch")
    return await renew_resident_session_lease(
        session,
        project,
        session_id=payload.session_id,
        task_id=payload.task_id,
        progress=bool(payload.progress),
        note=payload.note,
        message_cursor=payload.message_cursor,
        handled_message_ids=payload.handled_message_ids,
        turn_latency_summary=payload.turn_latency_summary,
        compaction_count=payload.compaction_count,
        last_compaction_at=payload.last_compaction_at,
        decisions_reg=payload.decisions_reg,
    )
