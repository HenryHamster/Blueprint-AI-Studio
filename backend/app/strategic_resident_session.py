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

No dedicated session table: lifecycle state lives in ``Project.vision`` under
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
from sqlalchemy import bindparam, func, or_, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal, get_session
from app.failure_classification import ADMIN_CANCEL
from app.knowledge_query import build_strategic_memory_index
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
from app.strategic_payload_summaries import (
    _fetch_open_messages,
    _fetch_recent_handled_conversation,
)
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
    get_strategic_workbench_state,
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

# Blocked/failed reason codes recorded by the Workday auto-start hook. Workday
# mode itself is the on-switch, so there is no global disabled-state reason.
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

# A task row can remain IN_PROGRESS long after its resident subprocess or runner
# disappeared. Workday's synchronous ensure path must not trust that row
# forever: a worker is healthy only while its task heartbeat is recent.
#
# The resident heartbeat uses the bounded two-attempt / 10-second API policy.
# apiRequest has separate header and body deadlines, so a transient failing
# cycle can legitimately consume a little over 40 seconds before the loop can
# make its next attempt. Keep enough room for three full cycles plus scheduling
# slack; otherwise a healthy resident is cancelled while its generic task
# heartbeat and runner both remain live.
_RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS = 180

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
_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class _ResidentRecoverySuperseded(RuntimeError):
    """An exact-generation repair lost its race to stop/requeue/replacement."""


class _ResidentEnsureModeChanged(RuntimeError):
    """A hydration ensure observed that the operator already left Workday."""


class _ResidentCycleInFlight(HTTPException):
    """A one-shot strategic writer currently owns the project's authority."""

    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail="A strategic cycle is already in flight; Workday will start when it finishes",
        )



_RESIDENT_STRATEGIC_TASK_TYPE = "strategic_review"

# The resident agent uses the Claude streaming/session protocol even when the
# underlying model is served by a compatible third-party provider. OpenAI's
# Codex JSONL transport cannot host that protocol, so resident tasks must carry
# an explicit per-task routing constraint instead of inheriting the broader
# strategic category preference (which may validly put OpenAI first for normal
# one-shot strategic reviews).
_RESIDENT_PROVIDER_PREFERENCE = ("anthropic", "zai", "deepseek")

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
_ADOPTABLE_TASK_STATUSES = frozenset({TaskStatus.QUEUED, TaskStatus.PROVISIONING})

# Session status values.
STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"

# ── Operator-decision capture enforcement ──────────────────────────────────
# The resident agent ships an ``operator_decision_summary`` telemetry blob on
# the heartbeat: ``{total, captured, missed, capture_rate}`` where ``total`` is
# the number of operator turns seen and ``capture_rate`` is the integer percent
# of those turns that ended with a valid OPERATOR-DECISION marker. A chronically
# low rate means operator corrections are silently failing to reach the durable
# decisions register. We emit a strategist-internal notice when the rate is low
# AND the session has enough operator turns to judge it — a single early miss
# must NOT fire this. The dedup flag on the session blob keeps it to one notice
# per low-capture crossing; a recovery above the floor re-arms it.
#
# Minimum operator turns before the capture rate is judged (avoid firing on an
# early one-off miss on a fresh session).
OPERATOR_DECISION_CAPTURE_MIN_SAMPLE = 4
# capture_rate (integer percent) strictly below this is treated as chronically low.
OPERATOR_DECISION_CAPTURE_LOW_RATE_PCT = 60
# Dedup flag persisted on the session blob so the notice fires at most once per
# low-capture crossing.
OPERATOR_DECISION_LOW_CAPTURE_NOTIFIED_KEY = "_operator_decision_low_capture_notified"

# Seconds without a refreshed operator_decision_summary before an absence/staleness
# notice fires. At the ~30s heartbeat cadence this is ≥10 consecutive absent
# heartbeats — enough to distinguish a silent agent from a transient gap.
_OPERATOR_DECISION_SUMMARY_STALE_SECONDS = 300  # 5 min

# Dedup flag persisted on the session blob so the absence notice fires at most once
# per absent crossing; a recovery (agent resumes sending a valid summary) clears it.
OPERATOR_DECISION_ABSENT_NOTIFIED_KEY = "_operator_decision_absent_notified"


def _operator_decision_capture_chronically_low(summary: Any) -> bool:
    """True when the operator-decision capture rate is chronically low.

    Requires at least ``OPERATOR_DECISION_CAPTURE_MIN_SAMPLE`` operator turns
    (so a single early miss can't trip it) and a ``capture_rate`` strictly below
    ``OPERATOR_DECISION_CAPTURE_LOW_RATE_PCT``. Malformed / absent summaries and
    a null capture_rate (no turns yet) return False.
    """
    if not isinstance(summary, dict):
        return False
    total = summary.get("total")
    rate = summary.get("capture_rate")
    if not isinstance(total, int) or total < OPERATOR_DECISION_CAPTURE_MIN_SAMPLE:
        return False
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return False
    return rate < OPERATOR_DECISION_CAPTURE_LOW_RATE_PCT


def _operator_decision_capture_recovered(summary: Any) -> bool:
    """True when the capture rate has recovered to/above the low-rate floor.

    Symmetric to :func:`_operator_decision_capture_chronically_low`: a genuine
    recovery (enough samples AND rate at/above the floor) clears the dedup flag
    so a later low-capture crossing can re-fire. A dip below the sample floor is
    NOT a recovery — it leaves the flag untouched.
    """
    if not isinstance(summary, dict):
        return False
    total = summary.get("total")
    rate = summary.get("capture_rate")
    if not isinstance(total, int) or total < OPERATOR_DECISION_CAPTURE_MIN_SAMPLE:
        return False
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return False
    return rate >= OPERATOR_DECISION_CAPTURE_LOW_RATE_PCT


# ---------------------------------------------------------------------------
# Session-state helpers (Project.vision blob)
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    return f"ss-{uuid.uuid4().hex[:16]}"


def _replace_vision(target: dict[str, Any], replacement: dict[str, Any]) -> None:
    """Replace one already-locked vision snapshot without an ORM write here."""
    target.clear()
    target.update(replacement)


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


async def _record_cycle_in_flight_isolated(
    project_id: uuid.UUID,
    operator_user_id: str,
    *,
    session_factory=None,
) -> None:
    """Durably record the single-writer block after a resident start backs off."""
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            await _set_cycle_in_flight_reason(session, project, operator_user_id)
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] isolated cycle-in-flight record failed for project %s",
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
        "worker_generation": int(blob.get("worker_generation") or 1),
        "worker_state": blob.get("worker_state") or "ready",
        "worker_retry_until": blob.get("worker_retry_until"),
        "worker_retry_reason": blob.get("worker_retry_reason"),
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
        "operator_decision_summary": blob.get("_operator_decision_summary"),
        "turn_latency_summary": blob.get("_turn_latency_summary"),
    }


def resident_session_snapshot_for_task(
    vision: dict[str, Any] | None,
    task: Task | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Overlay the exact resident Task's authoritative retry state on a snapshot.

    The Project blob is a bounded UI projection written after the Task retry
    transaction commits. If that best-effort mirror fails, readers must still
    report a healthy intentional wait instead of telling the operator to restart
    Workday. The same read-through removes a stale wait marker after the Task is
    claimable again, without mutating either row or introducing a read-path lock.
    """
    snapshot = resident_session_snapshot(vision)
    if snapshot is None or task is None:
        return snapshot
    if str(snapshot.get("task_id") or "") != str(task.id):
        return snapshot

    context = task.context or {}
    try:
        snapshot_generation = int(snapshot.get("worker_generation") or 1)
        task_generation = int(context.get("_resident_worker_generation") or 1)
    except (TypeError, ValueError):
        return snapshot
    if (
        context.get("_resident_session") is not True
        or str(context.get("strategic_session_id") or "")
        != str(snapshot.get("session_id") or "")
        or task_generation != snapshot_generation
    ):
        return snapshot

    retry_until = task.retry_after_until
    if retry_until is not None and retry_until.tzinfo is not None:
        retry_until = retry_until.astimezone(timezone.utc).replace(tzinfo=None)
    current_now = now or _utcnow()
    derived = dict(snapshot)
    if (
        task.status in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}
        and retry_until is not None
        and retry_until > current_now
    ):
        derived["worker_state"] = "retry_wait"
        derived["worker_retry_until"] = retry_until.isoformat()
        derived["worker_retry_reason"] = str(
            task.latest_status_message or "Resident worker waiting before retry"
        )[:500]
    elif derived.get("worker_state") == "retry_wait":
        derived["worker_state"] = "ready"
        derived.pop("worker_retry_until", None)
        derived.pop("worker_retry_reason", None)
    return derived


async def authoritative_resident_session_snapshot(
    session: AsyncSession,
    project: Project,
) -> dict[str, Any] | None:
    """Return the resident snapshot with an authoritative Task read-through."""
    blob = read_resident_session(project.vision)
    task: Task | None = None
    task_id = (blob or {}).get("task_id") if isinstance(blob, dict) else None
    if task_id:
        try:
            task = await session.get(Task, uuid.UUID(str(task_id)))
        except (TypeError, ValueError):
            task = None
    return resident_session_snapshot_for_task(project.vision, task)


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


def _resident_worker_task_is_healthy(
    task: Task | None, blob: dict[str, Any], now: datetime
) -> bool:
    """Return whether the recorded worker is healthy enough to reuse."""
    if task is None:
        return False
    if task.status not in {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }:
        return False
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}:
        retry_until = task.retry_after_until
        if retry_until is not None and retry_until.tzinfo is not None:
            retry_until = retry_until.astimezone(timezone.utc).replace(tzinfo=None)
        if retry_until is not None and retry_until > now:
            # The Task column is authoritative. The blob is an operator-facing
            # projection that may transiently lag its post-commit mirror.
            return True
        # A retry deadline is not a claim guarantee: the retry scheduler and
        # runner still need a little time to turn an elapsed durable hold into
        # an IN_PROGRESS worker.  Keep the exact persisted retry generation
        # healthy for the same bounded window used for worker heartbeats.  This
        # covers the handoff just after ``retry_after_until`` without trusting a
        # retry marker forever if the scheduler or runner has actually died.
        blob_retry_until = _parse_iso_naive(blob.get("worker_retry_until"))
        if (
            blob.get("worker_state") == "retry_wait"
            and blob_retry_until is not None
            and blob_retry_until <= now
            and (now - blob_retry_until).total_seconds()
            <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS
        ):
            return True
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}:
        # A resident failure can requeue the exact task between worker attempts.
        # Its original session start is no longer a useful health signal once the
        # session has been alive for five minutes: the runner may have just sent
        # a task heartbeat while preparing the next attempt.  Treat that fresh
        # task heartbeat as a bounded retry/startup bridge, then reclaim only
        # after it actually goes stale.  Otherwise a recoverable provider or
        # reply-persistence error turns into a false "stale heartbeat" session
        # supersession at the five-minute start-claim boundary.
        heartbeat = task.last_heartbeat
        if heartbeat is not None:
            if heartbeat.tzinfo is not None:
                heartbeat = heartbeat.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                now - heartbeat
            ).total_seconds() <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS:
                return True
        started = _parse_iso_naive(blob.get("started_at"))
        return bool(
            started
            and (now - started).total_seconds() <= _RESIDENT_START_CLAIM_STALE_SECONDS
        )
    worker_heartbeat = _parse_iso_naive(blob.get("worker_heartbeat_at"))
    if worker_heartbeat is not None:
        return (
            now - worker_heartbeat
        ).total_seconds() <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS

    # The generic task heartbeat can bridge startup compatibility only. It
    # proves runner liveness, not resident-loop health, so it cannot renew a
    # steady-state worker after the bounded startup window.
    startup_seen = _parse_iso_naive(blob.get("started_at"))
    if (
        startup_seen is None
        or (now - startup_seen).total_seconds() > _RESIDENT_START_CLAIM_STALE_SECONDS
    ):
        return False
    heartbeat = task.last_heartbeat
    if heartbeat is None:
        return True
    if heartbeat.tzinfo is not None:
        heartbeat = heartbeat.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - heartbeat).total_seconds() <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS


async def sync_resident_worker_retry_wait(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    session_id: str,
    worker_generation: int,
) -> bool:
    """Mirror an exact resident task's durable retry hold into its session blob.

    Call only after the retry transition commits and releases the Task lock. This
    helper then follows the canonical Project -> Task lock order and re-validates
    session/task/generation before writing, so a superseded worker cannot mark a
    newer generation as waiting. A retry with no future deadline clears an older
    wait marker and remains immediately pullable.
    """
    now = _utcnow()
    changed = False
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is not None:
            current = read_resident_session(project.vision)
            if (
                resident_session_is_active(current)
                and str((current or {}).get("session_id")) == str(session_id)
                and str((current or {}).get("task_id")) == str(task_id)
                and int((current or {}).get("worker_generation") or 1)
                == worker_generation
            ):
                async with task_lock(session, task_id, skip_locked=False) as task:
                    context = (task.context or {}) if task is not None else {}
                    try:
                        task_generation = int(
                            context.get("_resident_worker_generation") or 1
                        )
                    except (TypeError, ValueError):
                        task_generation = -1
                    if (
                        task is not None
                        and task.status == TaskStatus.PROVISIONING
                        and context.get("_resident_session") is True
                        and str(context.get("strategic_session_id"))
                        == str(session_id)
                        and task_generation == worker_generation
                    ):
                        retry_until = task.retry_after_until
                        if retry_until is not None and retry_until.tzinfo is not None:
                            retry_until = retry_until.astimezone(timezone.utc).replace(
                                tzinfo=None
                            )
                        updated = dict(current or {})
                        if retry_until is not None and retry_until > now:
                            updated["worker_state"] = "retry_wait"
                            updated["worker_retry_until"] = retry_until.isoformat()
                            updated["worker_retry_reason"] = str(
                                task.latest_status_message
                                or "Resident worker waiting before retry"
                            )[:500]
                        elif updated.get("worker_state") == "retry_wait":
                            updated["worker_state"] = "ready"
                            updated.pop("worker_retry_until", None)
                            updated.pop("worker_retry_reason", None)
                        else:
                            updated = {}
                        if updated:
                            updated["updated_at"] = now.isoformat()

                            def _store_retry_wait(vision: dict[str, Any]) -> None:
                                vision[RESIDENT_SESSION_KEY] = updated

                            await with_vision_locked(
                                session,
                                project_id,
                                _store_retry_wait,
                                _locked_project=project,
                            )
                            changed = True
    await session.commit()
    return changed


async def clear_resident_worker_retry_wait(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    session_id: str,
    worker_generation: int,
) -> bool:
    """Clear retry-wait after an exact generation successfully claims."""
    changed = False
    now_iso = _utcnow().isoformat()
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is not None:
            current = read_resident_session(project.vision)
            if (
                resident_session_is_active(current)
                and str((current or {}).get("session_id")) == str(session_id)
                and str((current or {}).get("task_id")) == str(task_id)
                and int((current or {}).get("worker_generation") or 1)
                == worker_generation
            ):
                async with task_lock(session, task_id, skip_locked=False) as task:
                    context = (task.context or {}) if task is not None else {}
                    try:
                        task_generation = int(
                            context.get("_resident_worker_generation") or 1
                        )
                    except (TypeError, ValueError):
                        task_generation = -1
                    if (
                        task is not None
                        and task.status == TaskStatus.IN_PROGRESS
                        and context.get("_resident_session") is True
                        and str(context.get("strategic_session_id"))
                        == str(session_id)
                        and task_generation == worker_generation
                        and (current or {}).get("worker_state") == "retry_wait"
                    ):
                        updated = dict(current or {})
                        updated["worker_state"] = "ready"
                        updated.pop("worker_retry_until", None)
                        updated.pop("worker_retry_reason", None)
                        updated["updated_at"] = now_iso

                        def _clear_retry_wait(vision: dict[str, Any]) -> None:
                            vision[RESIDENT_SESSION_KEY] = updated

                        await with_vision_locked(
                            session,
                            project_id,
                            _clear_retry_wait,
                            _locked_project=project,
                        )
                        changed = True
    await session.commit()
    return changed


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
    the heartbeat cannot extend the dead-man's switch (reaper uses only
    last_heartbeat; ``_classify_resident_session`` in reaper_orphan_classify.py
    ignores this read-model path).
    """
    # One bounded recursive CTE aggregates the strategist task subtree without
    # a Python-side walk or cycle risk.
    terminal_cutoff = _utcnow() - timedelta(seconds=_WORK_STATE_TERMINAL_WINDOW_SECONDS)
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
            """)
            .bindparams(
                bindparam("project_id", type_=Task.__table__.c.project_id.type),
                bindparam("terminal_cutoff", type_=Task.__table__.c.updated_at.type),
            )
            .columns(
                max_updated=Task.__table__.c.updated_at.type,
            ),
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
    # SQLite returns the raw aggregate as text, while asyncpg returns a
    # tz-aware datetime. The ORM aggregates below use NaiveUTCDateTime.
    if isinstance(task_max, str):
        task_max = datetime.fromisoformat(task_max.replace("Z", "+00:00"))
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
    worker_generation: int,
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
    ``_resident_session``. Guarded on the session/generation identity so it only
    ever clears OUR claim, never a newer worker generation a concurrent starter
    legitimately owns.

    Best-effort: any failure here is logged and swallowed so it never masks the
    original start failure. The ``async with`` block auto-rolls-back/closes the
    dedicated session on error.
    """
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as session:

            def _clear(vision: dict[str, Any]) -> None:
                current = read_resident_session(vision)
                if (
                    isinstance(current, dict)
                    and str(current.get("session_id")) == str(session_id)
                    and int(current.get("worker_generation") or 1) == worker_generation
                ):
                    vision.pop(RESIDENT_SESSION_KEY, None)

            await with_vision_locked(session, project_id, _clear, skip_locked=False)
            if spawned_task_id is not None:
                # The project lock remains held. Acquire the task second and
                # cancel only the task born from THIS failed generation.
                async with task_lock(
                    session, spawned_task_id, skip_locked=False
                ) as task:
                    context = (task.context or {}) if task is not None else {}
                    if (
                        task is not None
                        and task.status in _LIVE_TASK_STATUSES
                        and context.get("_resident_session") is True
                        and str(context.get("strategic_session_id")) == str(session_id)
                        and int(context.get("_resident_worker_generation") or 1)
                        == worker_generation
                    ):
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
        (
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
        )
        .scalars()
        .all()
    )
    for task in rows:
        payload = task.first_payload or {}
        if payload.get("type") == _RESIDENT_STRATEGIC_TASK_TYPE:
            return task
    return None


async def _find_spawned_resident_strategic_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    session_id: str,
    worker_generation: int,
) -> Task | None:
    """Recover this start request's born-resident task after an immediate claim.

    A pinned local runner can claim the task between the canonical spawn's commit
    and :func:`_find_adoptable_strategic_review_task`.  At that point the task is
    no longer adoptable (``started_at`` is set / status is ``IN_PROGRESS``), but
    it is already safe to finalize because the creating INSERT carried both the
    resident flag and this request's unique ``strategic_session_id``.  Refresh
    rows from the database so an identity-map copy created by the spawn does not
    hide a claim committed on the runner's connection.
    """
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.archived.is_(False),
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                        ]
                    ),
                )
                .order_by(Task.created_at.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for task in rows:
        context = task.context or {}
        if (
            context.get("_resident_session") is True
            and str(context.get("strategic_session_id") or "") == str(session_id)
            and int(context.get("_resident_worker_generation") or 1)
            == worker_generation
            and (task.first_payload or {}).get("type") == _RESIDENT_STRATEGIC_TASK_TYPE
        ):
            return task
    return None


def _task_matches_resident_identity(
    task: Task | None,
    *,
    session_id: str,
    worker_generation: int,
) -> bool:
    """Return whether *task* was born for one exact resident worker."""
    if (
        task is None
        or (task.first_payload or {}).get("type") != _RESIDENT_STRATEGIC_TASK_TYPE
    ):
        return False
    context = task.context or {}
    try:
        task_generation = int(context.get("_resident_worker_generation") or 1)
    except (TypeError, ValueError):
        return False
    return bool(
        context.get("_resident_session") is True
        and str(context.get("strategic_session_id") or "") == str(session_id)
        and task_generation == worker_generation
    )


async def _resident_task_ids_for_identity(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    session_id: str,
    worker_generation: int,
) -> list[uuid.UUID]:
    """Find live tasks born for an exact resident session/generation.

    Callers hold the Project row lock and then acquire each returned Task lock in
    sorted order before mutation. This recovers the crash window where the born
    task committed but the provisional session blob still has ``task_id=None``.
    """
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.archived.is_(False),
                    Task.status.in_(list(_LIVE_TASK_STATUSES)),
                )
                .order_by(Task.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return [
        task.id
        for task in rows
        if _task_matches_resident_identity(
            task,
            session_id=session_id,
            worker_generation=worker_generation,
        )
    ]


def _mark_resident_task_superseded(
    task: Task,
    *,
    now_iso: str,
    replacement_generation: int,
) -> None:
    """Cancel one exact stale resident task while its row lock is held."""
    task.status = TaskStatus.CANCELLED
    task.latest_status_message = (
        "Resident worker superseded after its resident heartbeat became stale"
    )
    context = dict(task.context or {})
    context["failure_category"] = ADMIN_CANCEL
    context["_resident_superseded"] = True
    context["_resident_superseded_at"] = now_iso
    context["_resident_superseded_by_generation"] = replacement_generation
    task.context = context
    flag_modified(task, "context")


async def _spawn_resident_strategic_task(
    session: AsyncSession,
    project: Project,
    runner_id: str | None,
    *,
    session_id: str,
    worker_generation: int = 1,
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
            session_id,
            runner_id,
            exam_ledger=exam_ledger,
            message_cursor=message_cursor,
            worker_generation=worker_generation,
            session_summary=session_summary,
            decisions_register=decisions_register,
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
    # Recover ONLY the task born from this exact session/generation claim. A
    # cadence task can appear after the pre-claim adoption hint and make the
    # canonical spawn guard create nothing; a generic adoptable lookup would then
    # hijack that unrelated one-shot.
    return await _find_spawned_resident_strategic_task(
        session,
        project.id,
        session_id,
        worker_generation,
    )


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
    """Load the project's allow-listed local runners, most-recently-seen first.

    Shared query for auto-pick, the picker options, and override validation, so
    they all scan the same ordered device set (matching
    ``_resolve_local_continuation_runner_affinity``'s ordering).
    """
    from app.models import LocalRunner
    from app.runner_project_binding import runner_allowed_for_project

    return list(
        (
            await session.execute(
                select(LocalRunner)
                .where(
                    runner_allowed_for_project(project_id),
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
    """Resolve an explicit override ``runner_id`` to a runner on this project's allow-list.

    Returns the ``LocalRunner`` when it exists, is on *project_id*'s allow-list
    (enrollment project or a secondary binding), and is not deleted — else None
    (the caller raises a clear "device not found" error rather than silently
    pinning to an unknown/foreign device). Availability is checked separately by
    the caller so it can distinguish "not found" (404) from "offline" (409 block).
    """
    from app.models import LocalRunner

    try:
        runner_uuid = uuid.UUID(str(runner_id))
    except (ValueError, TypeError):
        return None
    runner = await session.get(LocalRunner, runner_uuid)
    if runner is None or runner.status == "deleted":
        return None
    from app.runner_project_binding import allowed_project_ids

    if project_id not in await allowed_project_ids(session, runner):
        return None
    return runner


async def build_resident_runner_options(
    session: AsyncSession,
    project_id: uuid.UUID,
    runner_rows: list | None = None,
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

    if runner_rows is None:
        runner_rows = await _load_resident_runner_rows(session, project_id)

    options: list[dict[str, Any]] = []
    for runner in runner_rows:
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
    worker_generation: int = 1,
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
        "_resident_worker_generation": worker_generation,
        "provider_preference": list(_RESIDENT_PROVIDER_PREFERENCE),
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
    worker_generation: int = 1,
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
    context["_resident_worker_generation"] = worker_generation
    context["provider_preference"] = list(_RESIDENT_PROVIDER_PREFERENCE)
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
    worker_generation: int,
    workbench_state: dict[str, Any],
    task_id_str: str,
    operator_user_id: str,
    effective_runner_id: str | None,
) -> bool:
    """Arm the continuation lease + stamp the resident task_id onto OUR claim.

    Shared finalize body for both the adopt and spawn paths. Only mutates the
    session blob when it is still the exact session/generation claim we own —
    never clobbers a newer generation after a provisional-claim timeout. Arms with
    an explicit
    ``renewal_reason='operator_interaction'`` because a session START is itself
    an operator interaction: freshness must anchor to NOW regardless of any
    leftover (possibly stale) marker, else the brand-new session would be born
    non-authoritative (fresh==False) and the cloud cadence would keep spawning
    competing cycles until the first progress op.
    """
    current = read_resident_session(vision)
    if (
        not isinstance(current, dict)
        or str(current.get("session_id")) != str(session_id)
        or int(current.get("worker_generation") or 1) != worker_generation
    ):
        return False
    apply_strategic_local_continuation_marker(
        vision,
        workbench_state,
        operator_user_id=str(operator_user_id),
        preferred_runner_id=effective_runner_id,
        renewal_reason="operator_interaction",
    )
    finalized = dict(current)
    finalized["task_id"] = task_id_str
    finalized["worker_state"] = "ready"
    finalized["updated_at"] = _utcnow().isoformat()
    vision[RESIDENT_SESSION_KEY] = finalized
    return True


def _arm_and_finalize_vision_no_runner(
    vision: dict[str, Any],
    *,
    session_id: str,
    worker_generation: int,
    workbench_state: dict[str, Any],
    operator_user_id: str,
) -> bool:
    """Arm the continuation lease without a backing task (``allow_no_runner``).

    Same identity gate as :func:`_arm_and_finalize_vision` but does NOT set
    ``task_id`` (remains None) or advance ``worker_state`` past ``"starting"``
    — the session is live but has no backing agent process to stream.
    """
    current = read_resident_session(vision)
    if (
        not isinstance(current, dict)
        or str(current.get("session_id")) != str(session_id)
        or int(current.get("worker_generation") or 1) != worker_generation
    ):
        return False
    apply_strategic_local_continuation_marker(
        vision,
        workbench_state,
        operator_user_id=str(operator_user_id),
        preferred_runner_id=None,
        renewal_reason="operator_interaction",
    )
    finalized = dict(current)
    finalized["updated_at"] = _utcnow().isoformat()
    vision[RESIDENT_SESSION_KEY] = finalized
    return True


async def _repair_stale_message_handoffs_after_ensure(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Best-effort retry for a prior session's interrupted message handoff."""
    try:
        from app.session_message_handoff import (
            repair_stale_session_message_handoffs,
        )

        await repair_stale_session_message_handoffs(
            session,
            project_id,
        )
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        logger.warning(
            "[RESIDENT_SESSION] stale message handoff repair failed for project %s; "
            "a later ensure will retry",
            project_id,
            exc_info=True,
        )


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
    expected_recovery_identity: tuple[str, str, int] | None = None,
    expected_interaction_mode: str | None = None,
    allow_no_runner: bool = False,
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
    stale_predecessor: tuple[str, str | None, int] | None = None
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
        if (
            isinstance(_raw_ledger, str)
            and 0 < len(_raw_ledger.encode()) <= _EXAM_LEDGER_MAX_BYTES
        ):
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
        if (
            isinstance(_raw_decisions, str)
            and 0 < len(_raw_decisions.encode()) <= _DECISIONS_REG_MAX_BYTES
        ):
            carried_decisions_register = _raw_decisions
    if resident_session_is_active(existing):
        task_id = existing.get("task_id")
        live = None
        if task_id:
            try:
                live = await session.get(Task, uuid.UUID(str(task_id)))
            except (ValueError, TypeError):
                live = None
        if (
            expected_interaction_mode is None
            and _resident_worker_task_is_healthy(live, existing, now)
        ):
            snapshot = (
                resident_session_snapshot_for_task(project.vision, live, now=now) or {}
            )
            await _repair_stale_message_handoffs_after_ensure(
                session, project.id
            )
            return snapshot
        # Snapshot the complete predecessor identity so a delayed repair can
        # replace only this exact worker generation.
        try:
            predecessor_generation = int(existing.get("worker_generation") or 1)
        except (TypeError, ValueError):
            predecessor_generation = 1
        stale_predecessor = (
            str(existing.get("session_id")),
            str(task_id) if task_id else None,
            predecessor_generation,
        )

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
        if effective_runner_id is None and not allow_no_runner:
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

    # --- Atomic claim (single-active-session invariant) ----------------------
    decision: dict[str, Any] = {"mode": None}
    # Project→Task is the canonical order. Identity, health, retirement, and
    # the replacement claim become visible together at the commit below.
    stale_locked_task: Task | None = None
    stale_orphan_task_ids: list[uuid.UUID] = []
    superseded_task_ids: list[uuid.UUID] = []
    async with project_lock(session, project.id, skip_locked=False) as locked_project:
        if locked_project is None:
            raise HTTPException(
                status_code=409,
                detail="Project unavailable to claim resident session",
            )
        vision = dict(locked_project.vision or {})
        fresh = read_resident_session(vision)
        if expected_interaction_mode is not None:
            if expected_interaction_mode != "continuous":
                raise ValueError(
                    "expected_interaction_mode only supports 'continuous'"
                )
            # Hydration ensures are conditional, unlike an intentional manual
            # Start. Re-read the committed prefs row only after acquiring the
            # canonical Project lock. A mode-leave save takes prefs -> Project;
            # this path never locks prefs, so it cannot invert that order. If
            # leave already committed, the populate-existing read observes chat
            # and the ensure cannot recreate the resident or its Task.
            workbench = await get_strategic_workbench_state(
                session,
                operator_user_id,
                project.id,
                populate_existing=True,
            )
            if workbench.get("interaction_mode") != expected_interaction_mode:
                decision["mode"] = "ensure_mode_changed"
        if (
            decision["mode"] is None
            and expected_recovery_identity is not None
        ):
            expected_session_id, expected_task_id, expected_generation = (
                expected_recovery_identity
            )
            current_identity = (
                str(fresh.get("session_id")) if isinstance(fresh, dict) else "",
                str(fresh.get("task_id")) if isinstance(fresh, dict) else "",
                int(fresh.get("worker_generation") or 1) if isinstance(fresh, dict) else 0,
            )
            if not resident_session_is_active(fresh) or current_identity != (
                expected_session_id, expected_task_id, expected_generation
            ):
                decision["mode"] = "recovery_superseded"
        if decision["mode"] is None and resident_session_is_active(fresh):
            fresh_task_id = fresh.get("task_id")
            try:
                fresh_generation = int(fresh.get("worker_generation") or 1)
            except (TypeError, ValueError):
                fresh_generation = 1
            fresh_identity = (
                str(fresh.get("session_id")),
                str(fresh_task_id) if fresh_task_id else None,
                fresh_generation,
            )
            if fresh_task_id:
                if stale_predecessor is None or fresh_identity != stale_predecessor:
                    decision["mode"] = "existing"
                else:
                    try:
                        fresh_task_uuid = uuid.UUID(str(fresh_task_id))
                    except (TypeError, ValueError):
                        fresh_task_uuid = None
                    if fresh_task_uuid is not None:
                        async with task_lock(
                            session, fresh_task_uuid, skip_locked=False
                        ) as locked_task:
                            if _resident_worker_task_is_healthy(
                                locked_task, fresh, now
                            ):
                                decision["mode"] = "existing"
                            else:
                                stale_locked_task = locked_task
                    if decision["mode"] is None:
                        decision["session_id"] = str(fresh.get("session_id"))
                        decision["worker_generation"] = fresh_generation + 1
            elif not _start_claim_is_stale(fresh, now):
                decision["mode"] = "existing"
            elif stale_predecessor is not None:
                if fresh_identity != stale_predecessor:
                    decision["mode"] = "existing"
                else:
                    decision["session_id"] = str(fresh.get("session_id"))
                    decision["worker_generation"] = fresh_generation + 1
                    stale_orphan_task_ids = await _resident_task_ids_for_identity(
                        session,
                        project.id,
                        session_id=str(fresh.get("session_id")),
                        worker_generation=fresh_generation,
                    )

        if decision["mode"] in {
            "existing",
            "recovery_superseded",
            "ensure_mode_changed",
        }:
            pass
        elif runner_block is not None:
            decision["mode"] = "blocked"
        else:
            session_id = decision.get("session_id") or new_session_id()
            worker_generation = int(decision.get("worker_generation") or 1)
            if (
                stale_locked_task is not None
                and stale_locked_task.status in _LIVE_TASK_STATUSES
            ):
                _mark_resident_task_superseded(
                    stale_locked_task,
                    now_iso=now_iso,
                    replacement_generation=worker_generation,
                )
                session.add(stale_locked_task)
                superseded_task_ids.append(stale_locked_task.id)

            for orphan_task_id in stale_orphan_task_ids:
                async with task_lock(
                    session, orphan_task_id, skip_locked=False
                ) as orphan_task:
                    if (
                        orphan_task is not None
                        and orphan_task.status in _LIVE_TASK_STATUSES
                        and _task_matches_resident_identity(
                            orphan_task,
                            session_id=str(fresh.get("session_id")),
                            worker_generation=fresh_generation,
                        )
                    ):
                        _mark_resident_task_superseded(
                            orphan_task,
                            now_iso=now_iso,
                            replacement_generation=worker_generation,
                        )
                        session.add(orphan_task)
                        superseded_task_ids.append(orphan_task.id)

            if (
                adopt_task is not None
                and stale_predecessor is not None
                and str(adopt_task.id) == stale_predecessor[1]
            ):
                adopt_task = None
            vision[RESIDENT_SESSION_KEY] = {
                "version": 2,
                "session_id": session_id,
                "task_id": str(adopt_task.id) if adopt_task is not None else None,
                "worker_generation": worker_generation,
                "worker_state": "starting",
                "runner_id": effective_runner_id,
                "operator_user_id": str(operator_user_id),
                "status": STATUS_ACTIVE,
                "stop_requested": False,
                "started_at": now_iso,
                "updated_at": now_iso,
                "ended_at": None,
                "last_progress_at": None,
                "last_operator_interaction_at": now_iso,
            }
            if carried_exam_ledger:
                vision[RESIDENT_SESSION_KEY]["_exam_ledger"] = carried_exam_ledger
            if carried_message_cursor:
                vision[RESIDENT_SESSION_KEY]["_message_cursor"] = carried_message_cursor
            if carried_session_summary:
                vision[RESIDENT_SESSION_KEY]["_session_summary"] = (
                    carried_session_summary
                )
            if carried_decisions_register:
                vision[RESIDENT_SESSION_KEY]["_decisions_reg"] = (
                    carried_decisions_register
                )
            await with_vision_locked(
                session,
                project.id,
                lambda current: _replace_vision(current, vision),
                _locked_project=locked_project,
            )
            decision["session_id"] = session_id
            decision["worker_generation"] = worker_generation
            if effective_runner_id is None and allow_no_runner:
                decision["mode"] = "no_runner"
            else:
                decision["mode"] = "adopt" if adopt_task is not None else "spawn"
    await session.commit()
    await session.refresh(project)

    if decision["mode"] == "ensure_mode_changed":
        raise _ResidentEnsureModeChanged(
            "Interaction mode changed before resident ensure committed"
        )
    if decision["mode"] == "recovery_superseded":
        raise _ResidentRecoverySuperseded(
            "Resident recovery identity was stopped, requeued, or superseded"
        )
    if decision["mode"] == "existing":
        snapshot = await authoritative_resident_session_snapshot(session, project) or {}
        # A concurrent start owns (or is mid-starting) the session — return it.
        await _repair_stale_message_handoffs_after_ensure(session, project.id)
        return snapshot

    if decision["mode"] == "blocked":
        # The selected/auto-picked device is unavailable and we are not backing
        # off to a concurrent session — surface the deferred block (no claim blob
        # was written under the lock, so nothing is left to compensate).
        assert runner_block is not None
        raise runner_block

    for superseded_task_id in superseded_task_ids:
        # Direct server-side cancellation bypasses the normal agent terminal
        # callback. Flush any stashed transition/completion work before the new
        # worker can begin, using the same guarded bridge as operator stop.
        await _run_resident_completion_on_stop(
            superseded_task_id, session_factory=session_factory
        )

    # ── No-runner fallback ──────────────────────────────────────────────────
    # When ``allow_no_runner`` is True and no eligible runner exists, arm the
    # continuous-mode workbench lease and finalize the session blob WITHOUT a
    # backing task. The session remains in ``"starting"`` worker state — it is
    # live but has no agent process to stream. This lets the real session
    # lifecycle (start→heartbeat→stop→handoff) be exercised through production
    # code paths on a VM that cannot host a runner (no bwrap/userns/Docker).
    if decision["mode"] == "no_runner":
        project_id_for_comp = None
        try:
            workbench_state = (
                await persist_strategic_workbench_update(
                    session,
                    operator_user_id,
                    project.id,
                    {"interaction_mode": "continuous"},
                    sync_local_continuation_marker=False,
                    arm_interaction_mode=False,
                    commit=False,
                )
            ).state

            finalize_holder = {"owned": False}

            def _arm_no_runner(vision: dict[str, Any]) -> None:
                finalize_holder["owned"] = _arm_and_finalize_vision_no_runner(
                    vision,
                    session_id=session_id,
                    worker_generation=worker_generation,
                    workbench_state=workbench_state,
                    operator_user_id=operator_user_id,
                )

            await with_vision_locked(
                session, project.id, _arm_no_runner, skip_locked=False
            )
            if not finalize_holder["owned"]:
                raise HTTPException(
                    status_code=409,
                    detail="Resident start claim superseded by a newer worker generation",
                )
            await session.commit()
        except Exception:
            project_id_for_comp = project.id
            logger.exception(
                "[RESIDENT_SESSION] no-runner start failed for project %s "
                "(session %s); compensating on a fresh session",
                project_id_for_comp,
                session_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.warning(
                    "[RESIDENT_SESSION] caller-session rollback failed during "
                    "no-runner start compensation for project %s",
                    project_id_for_comp,
                    exc_info=True,
                )
            await _release_start_claim(
                project_id_for_comp,
                session_id,
                None,  # spawned_task_id — no task was spawned
                worker_generation=worker_generation,
                session_factory=session_factory,
            )
            raise

        await session.refresh(project)
        logger.info(
            "[RESIDENT_SESSION] started no-runner session %s for project %s",
            session_id,
            project.id,
        )
        snapshot = resident_session_snapshot(project.vision) or {}
        await _repair_stale_message_handoffs_after_ensure(session, project.id)
        return snapshot

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
            # Acquire and flush the per-user workbench row BEFORE Project→Task.
            # Workbench saves use MessagesNoisePrefs→Project, so doing the upsert
            # inside the Project/Task critical section creates an AB-BA cycle.
            # Keeping all three writes in this transaction preserves atomicity.
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
                            worker_generation=worker_generation,
                            exam_ledger=carried_exam_ledger,
                            message_cursor=carried_message_cursor,
                            session_summary=carried_session_summary,
                            decisions_register=carried_decisions_register,
                        )
                        session.add(task)
                        vision = dict(locked_project.vision or {})
                        if not _arm_and_finalize_vision(
                            vision,
                            session_id=session_id,
                            worker_generation=worker_generation,
                            workbench_state=workbench_state,
                            task_id_str=str(task.id),
                            operator_user_id=operator_user_id,
                            effective_runner_id=effective_runner_id,
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Resident start claim superseded by a newer worker generation",
                            )
                        await with_vision_locked(
                            session,
                            project.id,
                            lambda current: _replace_vision(current, vision),
                            _locked_project=locked_project,
                        )
                        task_id_str = str(task.id)
                        adopted = True
            if adopted:
                await session.commit()
            else:
                # Candidate no longer adoptable (claimed as a one-shot under us):
                # spawn a fresh resident task instead of hijacking it.
                # Discard the uncommitted workbench upsert before the canonical
                # spawn (which commits internally), then reload the Project whose
                # ORM state the rollback expired. The claim itself was committed
                # in the prior transaction and remains intact.
                project_id_for_spawn = project.id
                await session.rollback()
                project = await session.get(Project, project_id_for_spawn)
                if project is None:
                    raise HTTPException(status_code=404, detail="Project not found")
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
                worker_generation=worker_generation,
                exam_ledger=carried_exam_ledger,
                message_cursor=carried_message_cursor,
                session_summary=carried_session_summary,
                decisions_register=carried_decisions_register,
            )
            if task is None:
                from app.task_lifecycle import _has_active_strategic_task

                if await _has_active_strategic_task(project.id, session):
                    raise _ResidentCycleInFlight()
                raise HTTPException(status_code=409, detail="No strategic_review task available to host a resident session")
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

            finalize_holder = {"owned": False}

            def _arm_and_finalize(vision: dict[str, Any]) -> None:
                finalize_holder["owned"] = _arm_and_finalize_vision(
                    vision,
                    session_id=session_id,
                    worker_generation=worker_generation,
                    workbench_state=workbench_state,
                    task_id_str=task_id_str,
                    operator_user_id=operator_user_id,
                    effective_runner_id=effective_runner_id,
                )

            await with_vision_locked(
                session, project.id, _arm_and_finalize, skip_locked=False
            )
            if not finalize_holder["owned"]:
                raise HTTPException(
                    status_code=409,
                    detail="Resident start claim superseded by a newer worker generation",
                )
            await session.commit()
    except Exception as exc:
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
            project_id,
            session_id,
            spawned_task_id,
            worker_generation=worker_generation,
            session_factory=session_factory,
        )
        if isinstance(exc, _ResidentCycleInFlight):
            await _record_cycle_in_flight_isolated(
                project_id,
                operator_user_id,
                session_factory=session_factory,
            )
        raise

    await session.refresh(project)
    logger.info(
        "[RESIDENT_SESSION] started session %s (task %s) for project %s",
        session_id,
        task_id_str,
        project.id,
    )
    snapshot = resident_session_snapshot(project.vision) or {}
    await _repair_stale_message_handoffs_after_ensure(
        session, project.id
    )
    return snapshot


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

            await _handle_strategic_completion(
                task, "", fresh_session, project=locked_project
            )
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

    def _identity(
        value: dict[str, Any] | None,
    ) -> tuple[str, str | None, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            generation = int(value.get("worker_generation") or 1)
        except (TypeError, ValueError):
            generation = -1
        task_id_value = value.get("task_id")
        return (
            str(value.get("session_id")),
            str(task_id_value) if task_id_value else None,
            generation,
        )

    expected_identity = _identity(blob)
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
    ended_holder: dict[str, dict[str, Any] | None] = {"blob": None}

    def _release_and_end(vision: dict[str, Any]) -> None:
        # Release the continuation lease (the resident session is over) and mark
        # the session blob ended, in one locked read-modify-write.
        current = read_resident_session(vision)
        if _identity(current) != expected_identity:
            # Stop raced a replacement. Do not end the replacement, pop its
            # continuation lease, or cancel the task named by our stale pre-lock
            # snapshot. The caller rolls back the tentative workbench-mode write.
            return
        vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)
        ended = dict(current)
        ended["status"] = STATUS_ENDED
        ended["stop_requested"] = True
        ended["ended_at"] = now_iso
        ended["updated_at"] = now_iso
        vision[RESIDENT_SESSION_KEY] = ended
        ended_holder["blob"] = ended

    await with_vision_locked(session, project.id, _release_and_end, skip_locked=False)
    ended_blob = ended_holder["blob"]
    if ended_blob is None:
        await session.rollback()
        await session.refresh(project)
        return resident_session_snapshot(project.vision) or {}

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
    task_id = ended_blob.get("task_id")
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
                and _task_matches_resident_identity(
                    locked_task,
                    session_id=str(ended_blob.get("session_id")),
                    worker_generation=int(ended_blob.get("worker_generation") or 1),
                )
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

    # Apply handoff mutations BEFORE the commit so they land in the same
    # transaction as the session-end. If the process dies between the mutations
    # and the commit, the whole transaction rolls back — both the session-end
    # AND the message release — which is safe (the session is still "active"
    # and the next renew/stop retries the handoff). This replaces the prior
    # pattern of committing the session-end first and then running handoff in a
    # separate follow-up transaction, which could leave messages permanently
    # invisible to any replacement session if the second transaction never ran.
    _session_id = ended_blob.get("session_id")
    _handoff_rearm: tuple | None = None
    if _session_id:
        try:
            from app.session_message_handoff import (
                _apply_handoff_mutations,
                _run_handoff_rearm,
            )

            _, _handoff_rearm = await _apply_handoff_mutations(
                session, project.id, _session_id
            )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] message handoff staging raised for session %s — stop still committed",
                _session_id,
                exc_info=True,
            )

    await session.commit()
    await session.refresh(project)
    logger.info(
        "[RESIDENT_SESSION] stopped session for project %s (task %s)",
        project.id,
        task_id,
    )

    if _session_id and _handoff_rearm is not None:
        try:
            from app.session_message_handoff import _run_handoff_rearm

            await _run_handoff_rearm(
                session, project.id, _session_id, _handoff_rearm
            )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] message handoff rearm raised for session %s — stop still committed",
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
    session when a live, eligible local runner exists (the session runs on the
    operator's box, not a cloud VM). LEAVING it auto-stops. Called from the mode-change admin endpoint AFTER the
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
        await _record_blocked_reason_isolated(project_id, None, session_factory=factory)
        return None
    except Exception:
        # Belt-and-suspenders: no part of the auto start/stop path may break the
        # operator's mode switch (already persisted before this ran).
        logger.warning(
            "[RESIDENT_SESSION] sync for interaction_mode change failed for project %s",
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
            snapshot = await start_resident_session(
                fresh,
                project,
                operator_user_id=operator_user_id,
                runner_id=runner_id,
                session_factory=session_factory,
                expected_interaction_mode="continuous",
            )
            # Started cleanly — drop any stale blocked-reason on the same session.
            await set_resident_session_blocked_reason(fresh, project, None)
            return snapshot
    except Exception as exc:
        if isinstance(exc, (_ResidentCycleInFlight, _ResidentEnsureModeChanged)):
            return None
        # Track-1 makes start fail fast + clean (real error logged, compensated
        # on a fresh session, no half-initialized blob left). The dedicated
        # session above may be poisoned by that failure, so record the surfaced
        # reason on ANOTHER fresh session and stand down.
        logger.warning(
            "[RESIDENT_SESSION] auto-start on Workday entry failed for project %s: %s",
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



async def _mark_terminal_recovery_failed(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_identity: tuple[str, str, int],
    exc: BaseException,
    *,
    session_factory=None,
) -> None:
    """End only the still-current failed worker so the UI never lies active."""
    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as session:
            session_id, expected_task_id, generation = expected_identity
            # The operator id is stored in the exact blob; reload it before the
            # workbench check because prefs are per operator.
            project = await session.get(Project, project_id)
            if project is None:
                return
            blob = read_resident_session(project.vision)
            operator_user_id = (
                str(blob.get("operator_user_id") or "")
                if isinstance(blob, dict)
                else ""
            )
            if not operator_user_id:
                return
            workbench = await get_strategic_workbench_state(
                session, operator_user_id, project_id
            )
            if workbench.get("interaction_mode") != "continuous":
                return

            async with project_lock(session, project_id, skip_locked=False) as locked:
                if locked is None:
                    return
                vision = dict(locked.vision or {})
                current = read_resident_session(vision)
                current_identity = (
                    str(current.get("session_id")) if isinstance(current, dict) else "",
                    str(current.get("task_id")) if isinstance(current, dict) else "",
                    int(current.get("worker_generation") or 1)
                    if isinstance(current, dict)
                    else 0,
                )
                if (
                    not resident_session_is_active(current)
                    or current_identity != expected_identity
                    or expected_task_id != str(task_id)
                ):
                    return
                async with task_lock(session, task_id, skip_locked=False) as locked_task:
                    if locked_task is not None and (
                        locked_task.status not in _TERMINAL_TASK_STATUSES
                        or not _task_matches_resident_identity(
                            locked_task,
                            session_id=session_id,
                            worker_generation=generation,
                        )
                    ):
                        return

                now_iso = _utcnow().isoformat()
                ended = dict(current)
                ended.update(
                    {
                        "status": STATUS_ENDED,
                        "stop_requested": False,
                        "worker_state": "restart_failed",
                        "updated_at": now_iso,
                        "ended_at": now_iso,
                    }
                )
                vision[RESIDENT_SESSION_KEY] = ended
                vision[RESIDENT_SESSION_REASON_KEY] = {
                    "reason": (
                        f"{RESIDENT_SESSION_REASON_START_FAILED_PREFIX}: "
                        f"{_short_error(exc)}"
                    )[:200],
                    "recorded_at": now_iso,
                }
                await with_vision_locked(
                    session,
                    project_id,
                    lambda current: _replace_vision(current, vision),
                    _locked_project=locked,
                )
            await session.commit()
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] failed to expose terminal recovery for project %s",
            project_id,
            exc_info=True,
        )


async def reconcile_terminal_resident_worker(
    task_id: uuid.UUID | str,
    *,
    session_factory=None,
) -> bool:
    """Replace one exact terminal Workday worker, or expose a recoverable Start.

    Called after the agent-status early commit. Re-reads Task + Project on a
    fresh session and acts only when the task is still terminal, the active blob
    points at the same session/task/generation, and that operator is still in
    continuous mode. Requeues, explicit stops, and newer generations are no-ops.
    """
    factory = session_factory or AsyncSessionLocal
    try:
        task_uuid = uuid.UUID(str(task_id))
    except (TypeError, ValueError):
        return False

    project_id: uuid.UUID | None = None
    expected_identity: tuple[str, str, int] | None = None
    try:
        async with factory() as session:
            task = await session.get(Task, task_uuid)
            if task is None or task.status not in _TERMINAL_TASK_STATUSES:
                return False
            project_id = task.project_id
            project = await session.get(Project, project_id)
            if project is None:
                return False
            blob = read_resident_session(project.vision)
            if not resident_session_is_active(blob):
                return False
            try:
                expected_identity = (
                    str(blob.get("session_id")),
                    str(blob.get("task_id")),
                    int(blob.get("worker_generation") or 1),
                )
            except (TypeError, ValueError):
                return False
            if expected_identity[1] != str(task_uuid) or not _task_matches_resident_identity(
                task,
                session_id=expected_identity[0],
                worker_generation=expected_identity[2],
            ):
                return False
            operator_user_id = str(blob.get("operator_user_id") or "")
            if not operator_user_id:
                return False
            workbench = await get_strategic_workbench_state(
                session, operator_user_id, project_id
            )
            if workbench.get("interaction_mode") != "continuous":
                return False
            runner_id = str(blob.get("runner_id") or "") or None
            await start_resident_session(
                session,
                project,
                operator_user_id=operator_user_id,
                runner_id=runner_id,
                session_factory=factory,
                expected_recovery_identity=expected_identity,
                expected_interaction_mode="continuous",
            )
            logger.info(
                "[RESIDENT_SESSION] replaced terminal worker %s for project %s",
                task_uuid,
                project_id,
            )
            return True
    except (_ResidentRecoverySuperseded, _ResidentEnsureModeChanged):
        return False
    except Exception as exc:
        logger.warning(
            "[RESIDENT_SESSION] terminal worker recovery failed for task %s",
            task_uuid,
            exc_info=True,
        )
        if project_id is not None and expected_identity is not None:
            await _mark_terminal_recovery_failed(
                project_id,
                task_uuid,
                expected_identity,
                exc,
                session_factory=factory,
            )
        return False
async def renew_resident_session_lease(
    session: AsyncSession,
    project: Project,
    *,
    session_id: str,
    task_id: str,
    worker_generation: int = 1,
    progress: bool = False,
    note: str | None = None,
    message_cursor: str | None = None,
    handled_message_ids: list[str] | None = None,
    turn_latency_summary: dict | None = None,
    operator_decision_summary: dict | None = None,
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
    if (
        blob.get("session_id") != session_id
        or str(blob.get("task_id")) != str(task_id)
        or int(blob.get("worker_generation") or 1) != worker_generation
    ):
        raise HTTPException(
            status_code=409, detail="Heartbeat session/task/generation mismatch"
        )
    op_id = blob.get("operator_user_id")
    runner_id = blob.get("runner_id")
    stop_requested = bool(blob.get("stop_requested"))
    status = blob.get("status") or STATUS_ACTIVE
    now = _utcnow()
    now_iso = now.isoformat()

    # READ-ONLY work-state signal for the agent's state-gated pacing. Computed
    # once, up front, from cheap aggregate queries; additive to every return
    # branch below. It NEVER touches lease/freshness/session state, so it cannot
    # extend the dead-man's switch (reaper uses only last_heartbeat; this path
    # is not consulted by _classify_resident_session). Older agents ignore the
    # extra fields.
    work_state_rev, work_state_summary = await _compute_resident_work_state(
        session, project.id, resident_session_id=session_id
    )
    # Conversation availability is independent from mutation authority. A
    # resident may answer while a batch strategic cycle is active, but it must
    # stay chat-only until that cycle releases the project's strategic lane.
    other_strategic_rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.id != uuid.UUID(str(task_id)),
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    other_strategic_active = any(
        (row.first_payload or {}).get("type")
        in {"design_doc_update", _RESIDENT_STRATEGIC_TASK_TYPE}
        for row in other_strategic_rows
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
        # Set in-lock when this heartbeat crosses into chronically-low operator-
        # decision capture and no prior notice was posted; drives the single
        # strategist-internal emit after the lease commits.
        "emit_low_capture_notice": False,
        # Set in-lock when operator_decision_summary has been absent for longer
        # than _OPERATOR_DECISION_SUMMARY_STALE_SECONDS; drives a distinct
        # strategist-internal notice after the lease commits.
        "emit_absent_notice": False,
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
        if (
            fresh.get("session_id") != session_id
            or str(fresh.get("task_id")) != str(task_id)
            or int(fresh.get("worker_generation") or 1) != worker_generation
        ):
            holder["stopped"] = True
            return

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
            marker.get("owner_session_started_at") if isinstance(marker, dict) else None
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
        updated["worker_state"] = "ready"
        updated.pop("worker_retry_until", None)
        updated.pop("worker_retry_reason", None)
        updated["worker_heartbeat_at"] = now_iso
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
        # Persist the operator-decision capture telemetry so a restart retains
        # visibility into capture rate across a full session.
        if isinstance(operator_decision_summary, dict):
            updated["_operator_decision_summary"] = operator_decision_summary
            # Stamp the time of the most recent valid summary so absence
            # detection can measure elapsed time on future heartbeats.
            updated["_operator_decision_summary_at"] = now_iso
            # Enforce: cross into a strategist-internal notice when the capture
            # rate is chronically low. De-dupe on a persisted flag so the notice
            # fires at most once per low-capture crossing; a genuine recovery
            # above the floor re-arms it for the next crossing.
            already_notified = bool(
                fresh.get(OPERATOR_DECISION_LOW_CAPTURE_NOTIFIED_KEY)
            )
            if (
                _operator_decision_capture_chronically_low(operator_decision_summary)
                and not already_notified
            ):
                updated[OPERATOR_DECISION_LOW_CAPTURE_NOTIFIED_KEY] = True
                holder["emit_low_capture_notice"] = True
            elif (
                _operator_decision_capture_recovered(operator_decision_summary)
                and already_notified
            ):
                updated[OPERATOR_DECISION_LOW_CAPTURE_NOTIFIED_KEY] = False
            # Recovery from absence: agent resumed sending — clear dedup flag.
            if bool(fresh.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY)):
                updated[OPERATOR_DECISION_ABSENT_NOTIFIED_KEY] = False
        else:
            # operator_decision_summary is None — check if the telemetry blob
            # has stopped refreshing long enough to warrant an absence notice.
            prior_summary = fresh.get("_operator_decision_summary")
            last_summary_at = _parse_iso_naive(
                fresh.get("_operator_decision_summary_at")
            )
            already_absent_notified = bool(
                fresh.get(OPERATOR_DECISION_ABSENT_NOTIFIED_KEY)
            )
            if (
                not already_absent_notified
                and isinstance(prior_summary, dict)
                and last_summary_at is not None
                and (now - last_summary_at).total_seconds()
                > _OPERATOR_DECISION_SUMMARY_STALE_SECONDS
                and isinstance(prior_summary.get("total"), int)
                and prior_summary["total"] >= OPERATOR_DECISION_CAPTURE_MIN_SAMPLE
            ):
                updated[OPERATOR_DECISION_ABSENT_NOTIFIED_KEY] = True
                holder["emit_absent_notice"] = True
        # Persist compaction metadata reported by the agent. These are
        # opaque agent-reported values — the backend never derives them.
        if isinstance(compaction_count, int) and compaction_count > 0:
            updated["compaction_count"] = compaction_count
        if isinstance(last_compaction_at, str) and last_compaction_at:
            updated["last_compaction_at"] = last_compaction_at
        vision[RESIDENT_SESSION_KEY] = updated

    await with_vision_locked(session, project.id, _renew, skip_locked=False)
    if holder["stopped"]:
        # Identity/generation changed while this heartbeat waited for the project
        # lock. Release the lock and return before mirroring context or marking any
        # messages handled: those side effects belong exclusively to the winner.
        await session.commit()
        await session.refresh(project)
        cont = strategic_local_continuation_status_from_vision(
            project.vision, operator_user_id=op_id
        )
        refreshed = read_resident_session(project.vision) or {}
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
    # Mirror the exam ledger and message cursor into task.context so a runner
    # restart can read them back on startup (the agent reads task.context at pull
    # time; project.vision is not visible to the agent).
    _need_ctx_update = (
        exam_ledger_str is not None or bool(message_cursor) or decisions_reg is not None
    )
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
    # Emit the strategist-internal low-capture notice decided in-lock. Posts the
    # ledger row into THIS transaction so it commits atomically with the dedup
    # flag; failure to post is non-fatal (the flag is already set, so the notice
    # simply won't repeat — better than blocking the heartbeat).
    if holder["emit_low_capture_notice"] and isinstance(
        operator_decision_summary, dict
    ):
        try:
            from app.notification_dispatch import (
                post_operator_decision_capture_low_message,
            )

            try:
                _notice_task_id: uuid.UUID | None = uuid.UUID(str(task_id))
            except (ValueError, TypeError):
                _notice_task_id = None
            await post_operator_decision_capture_low_message(
                session,
                project.id,
                total=int(operator_decision_summary.get("total") or 0),
                captured=int(operator_decision_summary.get("captured") or 0),
                missed=int(operator_decision_summary.get("missed") or 0),
                capture_rate=int(operator_decision_summary.get("capture_rate") or 0),
                session_id=session_id,
                task_id=_notice_task_id,
            )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] operator-decision low-capture notice failed for session %s — ignored",
                session_id,
                exc_info=True,
            )
    # Emit the strategist-internal absence notice decided in-lock. Posts the ledger
    # row into THIS transaction so it commits atomically with the dedup flag; failure
    # is non-fatal (the flag is already set so the notice simply won't repeat).
    if holder["emit_absent_notice"]:
        try:
            from app.notification_dispatch import (
                post_operator_decision_summary_absent_message,
            )

            _refreshed_blob = read_resident_session(project.vision) or {}
            _last_summary_at = str(
                _refreshed_blob.get("_operator_decision_summary_at") or ""
            )
            try:
                _absent_task_id: uuid.UUID | None = uuid.UUID(str(task_id))
            except (ValueError, TypeError):
                _absent_task_id = None
            await post_operator_decision_summary_absent_message(
                session,
                project.id,
                last_summary_at=_last_summary_at,
                session_id=session_id,
                task_id=_absent_task_id,
            )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] operator-decision absent notice failed for session %s — ignored",
                session_id,
                exc_info=True,
            )
    # Apply handoff mutations BEFORE the commit when the session is being capped
    # or lapsed, so the message release lands in the same transaction as the
    # session-end. This closes the non-atomic two-transaction window where a
    # process death after the first commit (session-end) but before the second
    # (handoff) would leave messages permanently stamped with the ended session
    # and invisible to any replacement.
    _cap_rearm: tuple | None = None
    if holder["capped"] or holder["lapsed"]:
        try:
            from app.session_message_handoff import (
                _apply_handoff_mutations,
            )

            _, _cap_rearm = await _apply_handoff_mutations(
                session, project.id, session_id
            )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] message handoff staging raised for session %s (cap/lapse) — ignored",
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
        if _cap_rearm is not None:
            try:
                from app.session_message_handoff import _run_handoff_rearm

                await _run_handoff_rearm(
                    session, project.id, session_id, _cap_rearm
                )
            except Exception:
                logger.warning(
                    "[RESIDENT_SESSION] message handoff rearm raised for session %s (cap/lapse) — ignored",
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
        and holder["authority_held"]
        and not other_strategic_active,
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
    # Recently-answered operator conversation tail (status handled/resolved) so a
    # marker-less correction the operator just made survives the compaction
    # restart instead of vanishing with the open feed. Read-only, non-actionable.
    recent_handled_conversation = await _fetch_recent_handled_conversation(
        session, project.id
    )
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
        "strategic_memory_notes": await build_strategic_memory_index(session, project.id),
        "running_update": project.running_update or "",
        "vision": _agent_visible_vision(project.vision),
        "open_messages": open_messages,
        "recent_handled_conversation": recent_handled_conversation,
    }


class ResidentSessionHeartbeatRequest(BaseModel):
    session_id: str
    task_id: str
    progress: bool | None = None
    worker_generation: int = 1
    note: str | None = None
    message_cursor: str | None = None
    handled_message_ids: list[str] | None = None
    turn_latency_summary: dict | None = None
    operator_decision_summary: dict | None = None
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
        worker_generation=payload.worker_generation,
        note=payload.note,
        message_cursor=payload.message_cursor,
        handled_message_ids=payload.handled_message_ids,
        turn_latency_summary=payload.turn_latency_summary,
        operator_decision_summary=payload.operator_decision_summary,
        compaction_count=payload.compaction_count,
        last_compaction_at=payload.last_compaction_at,
        decisions_reg=payload.decisions_reg,
    )
