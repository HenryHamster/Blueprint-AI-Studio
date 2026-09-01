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

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, bindparam, func, or_, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal, get_session
from app.failure_classification import (
    ADMIN_CANCEL,
    RESIDENT_REPLY_PROTOCOL_FAILURE,
    resolved_failure_class,
)
from app.knowledge_query import build_strategic_memory_index
from app.livekit_tokens import (
    LiveKitConfigurationError,
    issue_resident_call_credentials,
)
from app.voice_credentials import (
    VoiceCredentialConfigurationError,
    resolve_resident_call_speech_credentials,
    validate_resident_call_deployment_configuration,
)
from app.models import (
    Direction,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicCycle,
    StrategicWatch,
    Task,
    TaskStatus,
    _utcnow,
)
from app.resident_conversation_scope import (
    RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY,
    RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY,
    resident_broad_message_scope,
    resident_conversation_floor_sequence,
)
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.strategic_consultation_status import canonical_consultation_activity_update
from app.strategic_payload_summaries import (
    _fetch_open_messages,
    _fetch_open_operator_cursor_floor,
    _fetch_recent_handled_conversation,
)
from app.strategic_session_activity import (
    resident_activity_contract,
    resident_activity_lifecycle_contract,
    resident_activity_semantics,
    resident_activity_with_server_fences,
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
RESIDENT_INFRASTRUCTURE_CONTEXT_KEY = "_resident_infrastructure"
RESIDENT_LIFECYCLE_DISPOSITION_CONTEXT_KEY = "_resident_lifecycle_disposition"
_RESIDENT_WORKER_INCARNATION_PROTOCOL = 1
# Runner pulls sort by priority before age. Resident is persistent infrastructure,
# so it must take the next free local slot ahead of ordinary high-priority work.
RESIDENT_TASK_PRIORITY = 101

# A call is deliberately a bounded, nested lease on the existing Workday
# session record, not a third interaction_mode or a new persistent table.  The
# generation is the public-reply fence: every call-owned mutation must present
# the exact id + generation recorded here.
RESIDENT_CALL_KEY = "call"
RESIDENT_TYPED_FAST_KEY = "typed_fast"
CALL_STATUS_ACTIVE = "active"
CALL_STATUS_ENDED = "ended"
CALL_STATUS_EXPIRED = "expired"
_DEFAULT_RESIDENT_CALL_TTL_SECONDS = 300
_RESIDENT_CALL_TTL_MIN_SECONDS = 30
_RESIDENT_CALL_TTL_MAX_SECONDS = 900
_RESIDENT_CALL_READY_DEADLINE_SECONDS = 30
_RESIDENT_CALL_MAX_DURATION_SECONDS = 7200
_RESIDENT_CALL_USAGE_LIMITS = {
    "input_audio_ms": _RESIDENT_CALL_MAX_DURATION_SECONDS * 1000,
    "output_audio_bytes": 2_000_000_000,
    "output_characters": 20_000_000,
    "provider_errors": 100_000,
}
_RESIDENT_WORKER_INCARNATION_LEASE_SECONDS = 180
_DEFAULT_TYPED_FAST_LEASE_TTL_SECONDS = 300
_TYPED_FAST_LEASE_TTL_MIN_SECONDS = 30
_TYPED_FAST_LEASE_TTL_MAX_SECONDS = 900

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
RESIDENT_SESSION_REASON_REPLY_PROTOCOL_FAILED = "reply_protocol_failed"
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

# A Workday start is itself an operator conversational action. Once the worker
# exists, enqueue one normal Fast-owned turn so the operator gets an immediate,
# context-aware opening instead of waiting for a later typed message. The input
# row stays canonical (Fast must be able to claim it) but carries a presentation
# marker so the UI can hide the synthetic operator bubble. The reply is always
# model-generated through the ordinary conversation completion fence.
RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY = "_resident_activation_prompt"
_RESIDENT_ACTIVATION_PROMPT = (
    "The operator just started Workday. Send one brief, natural, context-aware "
    "opening message now. Continue the existing conversation when useful; "
    "otherwise ask what they want to work through. Do not describe startup "
    "mechanics, claim completed work, or use a canned greeting."
)


class _ResidentActivationSuppressed(Exception):
    """Internal transaction signal: real current-session input already exists."""

# Legacy runtime-config export retained for compatibility. Continuous lifetime
# is process-liveness-bound now; operator silence is not a terminal condition.
STRATEGIC_SESSION_MAX_UNATTENDED_SETTING = "strategic_session_max_unattended_seconds"

# Typed Fast is activated by a server-owned Resident payload contract, never by
# an undocumented environment toggle on an individual runner.  The model map
# is derived from the existing fleet runtime configuration so it follows an
# operator's provider tier changes without duplicating provider model choices.
_TYPED_FAST_QUALIFICATION = "claude-warm-stream-json:phase0-v1"
_TYPED_FAST_COMPATIBLE_PROVIDERS = ("anthropic", "zai", "deepseek")


def _resident_typed_fast_payload() -> dict[str, Any]:
    """Return the trim-proof Fast launch contract for every Workday Resident.

    The foreground child remains restricted and may still decline readiness;
    this only makes the server-selected activation intent explicit at task
    birth and on adoption.  The agent then safely falls back to Workday if the
    qualified adapter cannot start.
    """
    qualification = os.getenv("CALL_FOREGROUND_QUALIFICATION", "").strip()
    if qualification != _TYPED_FAST_QUALIFICATION:
        return {"typed_fast_enabled": False}

    tier_map = runtime_config.get("provider_tier_model_map")
    models: dict[str, str] = {}
    for provider in _TYPED_FAST_COMPATIBLE_PROVIDERS:
        configured = (
            tier_map.get(provider, {}).get("haiku")
            if isinstance(tier_map, dict) and isinstance(tier_map.get(provider), dict)
            else None
        )
        if isinstance(configured, str) and configured.strip():
            models[provider] = configured.strip()
    anthropic_low = (
        runtime_config.get("provider_settings_by_effort", {})
        .get("anthropic", {})
        .get("low", {})
    )
    effort = anthropic_low.get("effort") if isinstance(anthropic_low, dict) else None
    return {
        "typed_fast_enabled": True,
        "typed_fast_models": models,
        "typed_fast_effort": effort if isinstance(effort, str) and effort else "low",
        "typed_fast_qualification": qualification,
    }


def resident_typed_fast_activation_enabled() -> bool:
    """True only when this control plane qualifies a typed Fast child.

    The Workday poll must not stamp conversation rows fast-eligible when no
    Resident can ever activate the Fast lane: the agent treats fast-marked
    rows as exclusively Fast-owned, so stamping them without an activatable
    adapter parks every operator message behind a child that will never exist.
    """
    return _resident_typed_fast_payload().get("typed_fast_enabled") is True

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
# A fresh heartbeat from the exact Task can corroborate a transient lag in the
# Resident-session projection, but it is emitted by an outer runner timer and
# cannot prove the conversation loop is making progress forever.
_RESIDENT_TASK_HEARTBEAT_CORROBORATION_GRACE_SECONDS = 120

# A provisional spawn-claim (an active session blob whose ``task_id`` is still
# None because the spawn is in flight) blocks a concurrent start from spawning a
# second task. If the starting process dies mid-flight the claim would wedge new
# starts forever, so a claim older than this is treated as abandoned and may be
# reclaimed. Comfortably longer than a spawn takes (~30-60s).
_RESIDENT_START_CLAIM_STALE_SECONDS = 300  # 5 min

# Supersession cause vocabulary. See the module docstring there for why a
# single fixed "heartbeat became stale" sentence was not good enough.
from app.resident_supersede_reasons import (  # noqa: E402
    RESIDENT_SUPERSEDE_ORPHANED_IDENTITY,
    RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE,
    RESIDENT_SUPERSEDE_REASON_TEXT,
    RESIDENT_SUPERSEDE_START_CLAIM_STALE,
    RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE,
    RESIDENT_SUPERSEDE_WORKER_TASK_MISSING,
    RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL,
)

# Operator-authored message ``from_type`` values that count as an operator
# interaction for the unattended cap (operator messages are stored as ``user``;
# ``operator`` is included defensively).
_OPERATOR_MESSAGE_FROM_TYPES = ("user", "operator")
_RESIDENT_ACTIVITY_PHASES = {
    "working",
    "waiting",
    "retrying",
    "stopping",
    "ended",
}
_RESIDENT_ACTIVITY_LAST_ACTION_MAX_CHARS = 240
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

# Hard row ceiling on the PROJECT-WIDE (every ``created_by``) non-terminal
# aggregate below. Mirrors ``agent_tasks._CENSUS_SCAN_LIMIT``. The live
# non-terminal set is small in normal operation, but "the fleet is small" is an
# assumption, not a guarantee, and a saturated fleet is precisely the condition
# this signal exists to reveal — so the scan is capped and a capped scan says so
# via ``project_scan_capped`` rather than returning a suspiciously round number.
_WORK_STATE_PROJECT_SCAN_LIMIT = 2000
_WORK_STATE_NON_TERMINAL_STATUSES = (
    "QUEUED",
    "PROVISIONING",
    "IN_PROGRESS",
    "EVALUATING",
)
_RESIDENT_WAKE_BARRIER_LIMIT = 20
_RESIDENT_WAKE_BARRIER_CONTEXT_KEYS = (
    "event_type",
    "receipt_kind",
    "cycle_id",
    "cycle_revision",
    "review_task_id",
    "direction_key",
    "verdict",
    "confidence",
    "policy_status",
    "next_cycle_id",
    "release_version_id",
    "version_number",
    "qualification_status",
    "candidate_sha",
    "promoted_sha",
    "serving_status",
    "served_sha",
    "reason",
    "resume_condition",
)

# Only settled StrategicCycle transitions are actionable to a standing
# Resident.  Phase progress is already owned by the running task/cycle and used
# to advance ``updated_at`` several times; treating every one of those writes as
# a wake signal pays for another strategic turn without presenting a decision.
_WORK_STATE_TERMINAL_CYCLE_OUTCOMES = (
    "completed",
    "failed_phase1",
    "failed_phase2",
    "failed_phase2_spawn",
    "stalled_phase1",
    "stalled_phase2",
    "thinker_artifacts_missing",
)

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
_RESIDENT_PROVIDER_PREFERENCE = ("anthropic", "openai", "zai", "deepseek")

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


def read_resident_call(vision: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the nested call lease from the canonical Resident state."""
    session_blob = read_resident_session(vision)
    call = (session_blob or {}).get(RESIDENT_CALL_KEY)
    return call if isinstance(call, dict) else None


def read_resident_typed_fast(vision: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the nested typed-Fast model-process lease, if any."""
    session_blob = read_resident_session(vision)
    lease = (session_blob or {}).get(RESIDENT_TYPED_FAST_KEY)
    return lease if isinstance(lease, dict) else None


def legacy_resident_worker_incarnation(
    task_id: uuid.UUID | str,
    worker_generation: int,
) -> uuid.UUID:
    """Stable process identity used when a rolling-deploy peer lacks fencing."""
    from app.conversation_coordinator import (
        legacy_conversation_claim_incarnation,
    )

    return legacy_conversation_claim_incarnation(
        uuid.UUID(str(task_id)),
        worker_generation,
    )


def resident_worker_incarnation_is_current(
    vision: dict[str, Any] | None,
    *,
    task_id: uuid.UUID | str,
    worker_generation: int,
    incarnation_id: uuid.UUID | None,
    now: datetime | None = None,
) -> bool:
    """Return whether one process owns the unexpired Resident-wide authority."""
    resident = read_resident_session(vision)
    if not resident_session_is_active(resident):
        return False
    requested = incarnation_id or legacy_resident_worker_incarnation(
        task_id,
        worker_generation,
    )
    recorded = resident.get("worker_incarnation_id")
    # Pre-fence session blobs belong only to the deterministic legacy process.
    if recorded is None:
        recorded = str(
            legacy_resident_worker_incarnation(task_id, worker_generation)
        )
    expires_at = _parse_iso_naive(
        resident.get("worker_incarnation_expires_at")
    )
    # A pre-fence blob has no process lease. It remains compatible only for the
    # legacy identity; the next locked heartbeat/claim materializes its lease.
    return (
        str(recorded) == str(requested)
        and (
            expires_at is None
            or expires_at > (now or _utcnow())
        )
    )


def resident_worker_matches_ended_identity(
    vision: dict[str, Any] | None,
    *,
    task_id: uuid.UUID | str,
    worker_generation: int,
    incarnation_id: uuid.UUID | None,
) -> bool:
    """Match the one process identity recorded on an already-ended session.

    This is deliberately not an authority check: an ended process may not
    heartbeat or mutate Strategic state.  It exists only so the status endpoint
    can accept that exact process's terminal callback after the backend has
    atomically ended its session and task.  A replacement changes the canonical
    blob identity, so a stale generation/incarnation no longer matches.
    """
    resident = read_resident_session(vision)
    if not isinstance(resident, dict):
        return False
    if resident.get("status") != STATUS_ENDED or not bool(
        resident.get("stop_requested")
    ):
        return False
    try:
        recorded_generation = int(resident.get("worker_generation") or 1)
    except (TypeError, ValueError):
        return False
    if (
        str(resident.get("task_id")) != str(task_id)
        or recorded_generation != worker_generation
    ):
        return False
    requested = incarnation_id or legacy_resident_worker_incarnation(
        task_id, worker_generation
    )
    recorded = resident.get("worker_incarnation_id")
    if recorded is None:
        recorded = str(
            legacy_resident_worker_incarnation(task_id, worker_generation)
        )
    return str(recorded) == str(requested)


def claim_resident_worker_incarnation(
    vision: dict[str, Any],
    *,
    task_id: uuid.UUID | str,
    worker_generation: int,
    incarnation_id: uuid.UUID | None,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """CAS the Resident-wide process lease while the Project row is locked."""
    resident = read_resident_session(vision)
    if not resident_session_is_active(resident):
        return None
    current_now = now or _utcnow()
    requested = incarnation_id or legacy_resident_worker_incarnation(
        task_id,
        worker_generation,
    )
    recorded_raw = resident.get("worker_incarnation_id")
    legacy_incarnation = legacy_resident_worker_incarnation(
        task_id,
        worker_generation,
    )
    try:
        recorded = (
            uuid.UUID(str(recorded_raw))
            if recorded_raw
            else legacy_incarnation
        )
    except (TypeError, ValueError, AttributeError):
        return None
    expires_at = _parse_iso_naive(
        resident.get("worker_incarnation_expires_at")
    )
    # During a rolling deploy an old Agent can still be inside a provider turn.
    # Materialize one bounded lease for its deterministic legacy identity before
    # allowing a random new-process identity to take over.
    if (
        recorded_raw is None
        and expires_at is None
        and requested != legacy_incarnation
        and resident.get("worker_incarnation_protocol")
        != _RESIDENT_WORKER_INCARNATION_PROTOCOL
    ):
        updated = dict(resident)
        updated["worker_incarnation_id"] = str(legacy_incarnation)
        updated["worker_incarnation_expires_at"] = (
            current_now
            + timedelta(seconds=_RESIDENT_WORKER_INCARNATION_LEASE_SECONDS)
        ).isoformat()
        vision[RESIDENT_SESSION_KEY] = updated
        return None
    if (
        recorded != requested
        and expires_at is not None
        and expires_at > current_now
    ):
        return None
    updated = dict(resident)
    updated["worker_incarnation_id"] = str(requested)
    updated["worker_incarnation_expires_at"] = (
        current_now
        + timedelta(seconds=_RESIDENT_WORKER_INCARNATION_LEASE_SECONDS)
    ).isoformat()
    vision[RESIDENT_SESSION_KEY] = updated
    return requested


def resident_call_is_active(
    call: dict[str, Any] | None, *, now: datetime | None = None
) -> bool:
    """True only for an unexpired active call lease."""
    if not isinstance(call, dict) or call.get("status") != CALL_STATUS_ACTIVE:
        return False
    expires_at = _parse_iso_naive(call.get("expires_at"))
    return expires_at is not None and expires_at > (now or _utcnow())


def resident_typed_fast_is_active(
    lease: dict[str, Any] | None, *, now: datetime | None = None
) -> bool:
    """True only for an unexpired active typed-Fast lease."""
    if not isinstance(lease, dict) or lease.get("status") != CALL_STATUS_ACTIVE:
        return False
    expires_at = _parse_iso_naive(lease.get("expires_at"))
    return expires_at is not None and expires_at > (now or _utcnow())


def resident_typed_fast_snapshot(
    vision: dict[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Return the non-secret typed-Fast lease projection for its Resident."""
    lease = read_resident_typed_fast(vision)
    if lease is None:
        return None
    current_now = now or _utcnow()
    status = lease.get("status")
    if status == CALL_STATUS_ACTIVE and not resident_typed_fast_is_active(lease, now=current_now):
        status = CALL_STATUS_EXPIRED
    return {
        "lease_id": lease.get("lease_id"),
        "generation": int(lease.get("generation") or 1),
        "kind": "typed_fast",
        "status": status,
        "started_at": lease.get("started_at"),
        "updated_at": lease.get("updated_at"),
        "expires_at": lease.get("expires_at"),
        "last_heartbeat_at": lease.get("last_heartbeat_at"),
        "worker_task_id": lease.get("worker_task_id"),
        "worker_generation": int(lease.get("worker_generation") or 1),
    }


def resident_call_snapshot(
    vision: dict[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Bounded public projection; never exposes the operator or pool account."""
    call = read_resident_call(vision)
    if call is None:
        return None
    current_now = now or _utcnow()
    status = call.get("status")
    if status == CALL_STATUS_ACTIVE and not resident_call_is_active(call, now=current_now):
        status = CALL_STATUS_EXPIRED
    return {
        "call_id": call.get("call_id"),
        "generation": int(call.get("generation") or 1),
        "status": status,
        "started_at": call.get("started_at"),
        "ready_at": call.get("ready_at"),
        "updated_at": call.get("updated_at"),
        "expires_at": call.get("expires_at"),
        "last_heartbeat_at": call.get("last_heartbeat_at"),
        "worker_task_id": call.get("worker_task_id"),
        "worker_generation": int(call.get("worker_generation") or 1),
        "runner_id": call.get("runner_id"),
        "input_revision": int(call.get("input_revision") or 0),
        "failure": call.get("failure"),
        "usage": _bounded_resident_call_usage(call.get("usage")),
    }


async def bind_resident_call_input_to_locked_project(
    session: AsyncSession,
    project: Project,
    *,
    actor_user_id: str,
    context: dict[str, Any],
    input_source: Literal["voice", "typed", "whiteboard"],
) -> dict[str, Any]:
    """Stamp/increment an active call input while the caller owns Project lock.

    This intentionally performs no commit: it is called from the existing
    canonical operator-message transaction after that transaction has acquired
    the Project row lock and before it allocates the message sequence.  Route
    the mutation through the re-entrant ``with_vision_locked`` bridge so the
    whole-column write remains centralized in the canonical helper.
    """
    result = context

    def _apply(vision: dict[str, Any]) -> None:
        nonlocal result
        resident = read_resident_session(vision)
        call = (resident or {}).get(RESIDENT_CALL_KEY)
        if not resident_call_is_active(call):
            return
        if str((call or {}).get("actor_user_id")) != str(actor_user_id):
            raise HTTPException(status_code=403, detail="Only the call owner may add call input")
        revision = int((call or {}).get("input_revision") or 0) + 1
        call["input_revision"] = revision
        call["updated_at"] = _utcnow().isoformat()
        resident[RESIDENT_CALL_KEY] = call
        vision[RESIDENT_SESSION_KEY] = resident
        result = {
            **context,
            "call_id": call.get("call_id"),
            "call_generation": int(call.get("generation") or 1),
            "input_source": input_source,
            "input_revision": revision,
        }

    await with_vision_locked(
        session,
        project.id,
        _apply,
        skip_locked=False,
        _locked_project=project,
    )
    return result


def _bounded_call_ttl_seconds(value: int | None) -> int:
    try:
        parsed = int(value or _DEFAULT_RESIDENT_CALL_TTL_SECONDS)
    except (TypeError, ValueError):
        parsed = _DEFAULT_RESIDENT_CALL_TTL_SECONDS
    return min(_RESIDENT_CALL_TTL_MAX_SECONDS, max(_RESIDENT_CALL_TTL_MIN_SECONDS, parsed))


def _bounded_resident_call_usage(value: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, upper in _RESIDENT_CALL_USAGE_LIMITS.items():
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        result[key] = min(upper, max(0, parsed))
    return result


async def acquire_resident_call_lease(
    session: AsyncSession,
    project: Project,
    *,
    operator_user_id: str,
    account_id: str | None,
    ttl_seconds: int | None = None,
) -> tuple[dict[str, Any], str | None, bool]:
    """Atomically acquire/reuse a nested call lease.

    Returns the full (server-only) lease and an expired account id whose old
    slot must be released by the caller after commit.  Capacity reservation is
    intentionally outside this DB lock; the endpoint compensates if this write
    loses its race.
    """
    now = _utcnow()
    now_iso = now.isoformat()
    ttl = _bounded_call_ttl_seconds(ttl_seconds)
    holder: dict[str, Any] = {"call": None, "expired_account_id": None, "created": False}

    def _apply(vision: dict[str, Any]) -> None:
        from app.strategic_cold_successor_authority import reject_active_cold_successor

        reject_active_cold_successor(vision)
        resident = read_resident_session(vision)
        if not resident_session_is_active(resident):
            raise HTTPException(status_code=409, detail="An active Workday Resident is required")
        existing = resident.get(RESIDENT_CALL_KEY)
        if resident_call_is_active(existing, now=now):
            if str(existing.get("actor_user_id")) != str(operator_user_id):
                raise HTTPException(
                    status_code=403,
                    detail="Only the call owner may reconnect",
                )
            holder["call"] = dict(existing)
            return
        if isinstance(existing, dict) and existing.get("status") == CALL_STATUS_ACTIVE:
            holder["expired_account_id"] = str(existing.get("account_id") or "") or None
        prior_generation = int(existing.get("generation") or 0) if isinstance(existing, dict) else 0
        call = {
            "call_id": f"call-{uuid.uuid4().hex[:16]}",
            "generation": prior_generation + 1,
            "status": CALL_STATUS_ACTIVE,
            "actor_user_id": str(operator_user_id),
            "started_at": now_iso,
            "updated_at": now_iso,
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "last_heartbeat_at": now_iso,
            "worker_task_id": resident.get("task_id"),
            "worker_generation": int(resident.get("worker_generation") or 1),
            "worker_incarnation_id": resident.get("worker_incarnation_id"),
            "runner_id": resident.get("runner_id"),
            "account_id": account_id,
            "slot_reserved": bool(account_id),
            "input_revision": 0,
            "failure": None,
        }
        resident[RESIDENT_CALL_KEY] = call
        vision[RESIDENT_SESSION_KEY] = resident
        holder["call"] = dict(call)
        holder["created"] = True

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    await session.commit()
    await session.refresh(project)
    return dict(holder["call"]), holder["expired_account_id"], bool(holder["created"])


async def release_resident_call_lease(
    session: AsyncSession,
    project: Project,
    *,
    call_id: str | None = None,
    generation: int | None = None,
    task_id: uuid.UUID | str | None = None,
    worker_generation: int | None = None,
    incarnation_id: uuid.UUID | None = None,
    reason: str = "ended",
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Fenced, idempotent call shutdown; returns lease, account id, changed."""
    now_iso = _utcnow().isoformat()
    holder: dict[str, Any] = {"call": None, "account_id": None, "changed": False}

    def _apply(vision: dict[str, Any]) -> None:
        resident = read_resident_session(vision)
        existing = (resident or {}).get(RESIDENT_CALL_KEY)
        if not isinstance(existing, dict):
            return
        if call_id is not None and str(existing.get("call_id")) != str(call_id):
            raise HTTPException(status_code=409, detail="Call lease superseded")
        if generation is not None and int(existing.get("generation") or 0) != generation:
            raise HTTPException(status_code=409, detail="Call generation superseded")
        if task_id is not None and not resident_worker_incarnation_is_current(
            vision,
            task_id=task_id,
            worker_generation=(
                worker_generation
                or int(existing.get("worker_generation") or 1)
            ),
            incarnation_id=incarnation_id,
            now=_utcnow(),
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident process incarnation superseded",
            )
        holder["call"] = dict(existing)
        if existing.get("status") == CALL_STATUS_ACTIVE:
            holder["account_id"] = str(existing.get("account_id") or "") or None
            existing["status"] = CALL_STATUS_ENDED if reason == "ended" else CALL_STATUS_EXPIRED
            existing["ended_at"] = now_iso
            existing["updated_at"] = now_iso
            existing["failure"] = reason if reason != "ended" else None
            holder["changed"] = True
            if isinstance(resident, dict):
                resident[RESIDENT_CALL_KEY] = existing
                vision[RESIDENT_SESSION_KEY] = resident

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    if holder["call"] is not None:
        await session.commit()
        await session.refresh(project)
    return holder["call"], holder["account_id"], bool(holder["changed"])


async def renew_resident_call_lease(
    session: AsyncSession,
    project: Project,
    *,
    task_id: str,
    call_id: str,
    generation: int,
    incarnation_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    ready: bool = False,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Renew only the exact Resident-owned nested lease."""
    now = _utcnow()
    now_iso = now.isoformat()
    ttl = _bounded_call_ttl_seconds(ttl_seconds)
    holder: dict[str, Any] = {"call": None, "release_account_id": None}

    def _apply(vision: dict[str, Any]) -> None:
        resident = read_resident_session(vision)
        call = (resident or {}).get(RESIDENT_CALL_KEY)
        if not resident_session_is_active(resident) or not isinstance(call, dict):
            raise HTTPException(status_code=409, detail="No active call lease")
        if (
            str(call.get("call_id")) != call_id
            or int(call.get("generation") or 0) != generation
            or str(call.get("worker_task_id")) != str(task_id)
            or not resident_worker_incarnation_is_current(
                vision,
                task_id=task_id,
                worker_generation=int(
                    (resident or {}).get("worker_generation") or 1
                ),
                incarnation_id=incarnation_id,
                now=now,
            )
            or call.get("status") != CALL_STATUS_ACTIVE
        ):
            raise HTTPException(status_code=409, detail="Call lease superseded")
        started_at = _parse_iso_naive(call.get("started_at"))
        if started_at is None:
            raise HTTPException(status_code=409, detail="Call lease has invalid start time")
        age_seconds = max(0.0, (now - started_at).total_seconds())
        expiry_reason: str | None = None
        if age_seconds >= _RESIDENT_CALL_MAX_DURATION_SECONDS:
            expiry_reason = "maximum_duration_reached"
        elif (
            not call.get("ready_at")
            and not ready
            and age_seconds >= _RESIDENT_CALL_READY_DEADLINE_SECONDS
        ):
            expiry_reason = "coordinator_not_ready"
        if expiry_reason is not None:
            call["status"] = CALL_STATUS_EXPIRED
            call["ended_at"] = now_iso
            call["updated_at"] = now_iso
            call["failure"] = expiry_reason
            holder["release_account_id"] = str(call.get("account_id") or "") or None
            call["slot_reserved"] = False
            resident[RESIDENT_CALL_KEY] = call
            vision[RESIDENT_SESSION_KEY] = resident
            holder["call"] = dict(call)
            return
        if ready and not call.get("ready_at"):
            call["ready_at"] = now_iso
        incoming_usage = _bounded_resident_call_usage(usage)
        if incoming_usage:
            prior_usage = _bounded_resident_call_usage(call.get("usage"))
            merged_usage = dict(prior_usage)
            for key, value in incoming_usage.items():
                merged_usage[key] = max(value, prior_usage.get(key, 0))
            call["usage"] = merged_usage
        call["last_heartbeat_at"] = now_iso
        call["updated_at"] = now_iso
        absolute_expiry = started_at + timedelta(
            seconds=_RESIDENT_CALL_MAX_DURATION_SECONDS
        )
        call["expires_at"] = min(
            now + timedelta(seconds=ttl), absolute_expiry
        ).isoformat()
        resident[RESIDENT_CALL_KEY] = call
        vision[RESIDENT_SESSION_KEY] = resident
        holder["call"] = dict(call)

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    await session.commit()
    await session.refresh(project)
    if holder["release_account_id"]:
        from app.max_plan_pool import release_max_plan_call_slot

        await release_max_plan_call_slot(
            holder["release_account_id"], project_id=str(project.id)
        )
    return resident_call_snapshot(project.vision) or {}


def _bounded_typed_fast_lease_ttl_seconds(value: int | None) -> int:
    try:
        parsed = int(value or _DEFAULT_TYPED_FAST_LEASE_TTL_SECONDS)
    except (TypeError, ValueError):
        parsed = _DEFAULT_TYPED_FAST_LEASE_TTL_SECONDS
    return min(_TYPED_FAST_LEASE_TTL_MAX_SECONDS, max(_TYPED_FAST_LEASE_TTL_MIN_SECONDS, parsed))


async def acquire_resident_typed_fast_lease(
    session: AsyncSession,
    project: Project,
    *,
    task_id: uuid.UUID | str,
    worker_generation: int,
    incarnation_id: uuid.UUID | None,
    account_id: str,
    ttl_seconds: int | None = None,
) -> tuple[dict[str, Any], str | None, bool]:
    """Persist the additional typed-Fast model-process slot behind its owner fence."""
    now = _utcnow()
    now_iso = now.isoformat()
    ttl = _bounded_typed_fast_lease_ttl_seconds(ttl_seconds)
    holder: dict[str, Any] = {"lease": None, "expired_account_id": None, "created": False}

    def _apply(vision: dict[str, Any]) -> None:
        from app.strategic_cold_successor_authority import reject_active_cold_successor

        reject_active_cold_successor(vision)
        resident = read_resident_session(vision)
        if (
            not resident_session_is_active(resident)
            or str((resident or {}).get("task_id")) != str(task_id)
            or int((resident or {}).get("worker_generation") or 1) != worker_generation
            or not resident_worker_incarnation_is_current(
                vision,
                task_id=task_id,
                worker_generation=worker_generation,
                incarnation_id=incarnation_id,
                now=now,
            )
        ):
            raise HTTPException(status_code=409, detail="Resident process incarnation superseded")
        existing = resident.get(RESIDENT_TYPED_FAST_KEY)
        if resident_typed_fast_is_active(existing, now=now):
            if (
                str(existing.get("worker_task_id")) != str(task_id)
                or int(existing.get("worker_generation") or 1) != worker_generation
                or str(existing.get("account_id") or "") != account_id
            ):
                raise HTTPException(status_code=409, detail="Typed Fast lease superseded")
            holder["lease"] = dict(existing)
            return
        if isinstance(existing, dict) and existing.get("status") == CALL_STATUS_ACTIVE:
            holder["expired_account_id"] = str(existing.get("account_id") or "") or None
        prior_generation = int(existing.get("generation") or 0) if isinstance(existing, dict) else 0
        lease = {
            "lease_id": f"typed-fast-{uuid.uuid4().hex[:16]}",
            "generation": prior_generation + 1,
            "kind": "typed_fast",
            "status": CALL_STATUS_ACTIVE,
            "started_at": now_iso,
            "updated_at": now_iso,
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "last_heartbeat_at": now_iso,
            "worker_task_id": str(task_id),
            "worker_generation": worker_generation,
            "worker_incarnation_id": str(incarnation_id) if incarnation_id else None,
            "account_id": account_id,
            "project_id": str(project.id),
            "slot_reserved": True,
        }
        resident[RESIDENT_TYPED_FAST_KEY] = lease
        vision[RESIDENT_SESSION_KEY] = resident
        holder["lease"] = dict(lease)
        holder["created"] = True

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    await session.commit()
    await session.refresh(project)
    return dict(holder["lease"]), holder["expired_account_id"], bool(holder["created"])


async def release_resident_typed_fast_lease(
    session: AsyncSession,
    project: Project,
    *,
    lease_id: str | None = None,
    generation: int | None = None,
    task_id: uuid.UUID | str | None = None,
    worker_generation: int | None = None,
    incarnation_id: uuid.UUID | None = None,
    reason: str = "ended",
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Fence and retire the typed-Fast slot before releasing pool capacity."""
    now_iso = _utcnow().isoformat()
    holder: dict[str, Any] = {"lease": None, "account_id": None, "changed": False}

    def _apply(vision: dict[str, Any]) -> None:
        resident = read_resident_session(vision)
        existing = (resident or {}).get(RESIDENT_TYPED_FAST_KEY)
        if not isinstance(existing, dict):
            return
        if lease_id is not None and str(existing.get("lease_id")) != str(lease_id):
            raise HTTPException(status_code=409, detail="Typed Fast lease superseded")
        if generation is not None and int(existing.get("generation") or 0) != generation:
            raise HTTPException(status_code=409, detail="Typed Fast lease generation superseded")
        if task_id is not None and not resident_worker_incarnation_is_current(
            vision,
            task_id=task_id,
            worker_generation=worker_generation or int(existing.get("worker_generation") or 1),
            incarnation_id=incarnation_id,
            now=_utcnow(),
        ):
            raise HTTPException(status_code=409, detail="Resident process incarnation superseded")
        holder["lease"] = dict(existing)
        if existing.get("status") == CALL_STATUS_ACTIVE:
            holder["account_id"] = str(existing.get("account_id") or "") or None
            existing["status"] = (
                CALL_STATUS_EXPIRED
                if reason in {"expired", "readiness_failed"}
                else CALL_STATUS_ENDED
            )
            existing["ended_at"] = now_iso
            existing["updated_at"] = now_iso
            existing["failure"] = reason if reason != "ended" else None
            existing["slot_reserved"] = False
            resident[RESIDENT_TYPED_FAST_KEY] = existing
            vision[RESIDENT_SESSION_KEY] = resident
            holder["changed"] = True

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    if holder["lease"] is not None:
        await session.commit()
        await session.refresh(project)
    return holder["lease"], holder["account_id"], bool(holder["changed"])


async def renew_resident_typed_fast_lease(
    session: AsyncSession,
    project: Project,
    *,
    task_id: str,
    worker_generation: int,
    incarnation_id: uuid.UUID | None,
    lease_id: str,
    generation: int,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Renew the exact typed Fast lease; a stale child cannot retain capacity."""
    now = _utcnow()
    now_iso = now.isoformat()
    ttl = _bounded_typed_fast_lease_ttl_seconds(ttl_seconds)
    holder: dict[str, Any] = {"release_account_id": None}

    def _apply(vision: dict[str, Any]) -> None:
        resident = read_resident_session(vision)
        lease = (resident or {}).get(RESIDENT_TYPED_FAST_KEY)
        if (
            not resident_session_is_active(resident)
            or not isinstance(lease, dict)
            or str(lease.get("lease_id")) != lease_id
            or int(lease.get("generation") or 0) != generation
            or str(lease.get("worker_task_id")) != str(task_id)
            or int(lease.get("worker_generation") or 1) != worker_generation
            or not resident_worker_incarnation_is_current(
                vision,
                task_id=task_id,
                worker_generation=worker_generation,
                incarnation_id=incarnation_id,
                now=now,
            )
            or lease.get("status") != CALL_STATUS_ACTIVE
        ):
            raise HTTPException(status_code=409, detail="Typed Fast lease superseded")
        if not resident_typed_fast_is_active(lease, now=now):
            lease["status"] = CALL_STATUS_EXPIRED
            lease["ended_at"] = now_iso
            lease["updated_at"] = now_iso
            lease["failure"] = "lease_expired"
            lease["slot_reserved"] = False
            holder["release_account_id"] = str(lease.get("account_id") or "") or None
        else:
            lease["last_heartbeat_at"] = now_iso
            lease["updated_at"] = now_iso
            lease["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
        resident[RESIDENT_TYPED_FAST_KEY] = lease
        vision[RESIDENT_SESSION_KEY] = resident

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    await session.commit()
    await session.refresh(project)
    if holder["release_account_id"]:
        from app.max_plan_pool import release_max_plan_auxiliary_slot

        await release_max_plan_auxiliary_slot(holder["release_account_id"], project_id=str(project.id))
    return resident_typed_fast_snapshot(project.vision) or {}


async def record_resident_call_playout_receipt(
    session: AsyncSession,
    project: Project,
    *,
    actor_user_id: str,
    call_id: str,
    generation: int,
    phrase_id: str,
    event: Literal["started", "interrupted", "completed"],
    reported_at: str | None,
) -> tuple[dict[str, Any], bool]:
    """Store a bounded, idempotent browser playout receipt on the call lease."""
    now_iso = _utcnow().isoformat()
    receipt_key = f"{phrase_id}:{event}"
    holder: dict[str, Any] = {"duplicate": False}

    def _apply(vision: dict[str, Any]) -> None:
        resident = read_resident_session(vision)
        call = (resident or {}).get(RESIDENT_CALL_KEY)
        if (
            not resident_call_is_active(call)
            or str((call or {}).get("call_id")) != call_id
            or int((call or {}).get("generation") or 0) != generation
            or str((call or {}).get("actor_user_id")) != str(actor_user_id)
        ):
            raise HTTPException(status_code=409, detail="Call lease superseded")
        prior = call.get("playout_receipts")
        receipts = list(prior) if isinstance(prior, list) else []
        if any(isinstance(item, dict) and item.get("key") == receipt_key for item in receipts):
            holder["duplicate"] = True
            return
        receipt = {
            "key": receipt_key,
            "phrase_id": phrase_id[:128],
            "event": event,
            "reported_at": reported_at[:80] if isinstance(reported_at, str) else None,
            "received_at": now_iso,
        }
        receipts.append(receipt)
        call["playout_receipts"] = receipts[-32:]
        call["last_playout"] = receipt
        call["updated_at"] = now_iso
        resident[RESIDENT_CALL_KEY] = call
        vision[RESIDENT_SESSION_KEY] = resident

    await with_vision_locked(session, project.id, _apply, skip_locked=False)
    await session.commit()
    await session.refresh(project)
    return resident_call_snapshot(project.vision) or {}, bool(holder["duplicate"])


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
    project_id = project.id
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
        try:
            await session.rollback()
            # Rollback expires every ORM attribute. Restore the caller's
            # instance so its next best-effort operation can safely reuse it.
            await session.refresh(project)
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] failed to recover blocked-reason session "
                "for project %s",
                project_id,
                exc_info=True,
            )
        logger.warning(
            "[RESIDENT_SESSION] failed to %s blocked-reason for project %s",
            "clear" if reason is None else "record",
            project_id,
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
    project_id = project.id
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
        try:
            await session.rollback()
            await session.refresh(project)
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] failed to recover cycle-in-flight session "
                "for project %s",
                project_id,
                exc_info=True,
            )
        logger.warning(
            "[RESIDENT_SESSION] failed to record cycle_in_flight reason for project %s",
            project_id,
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


def _resident_activity_snapshot(
    blob: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the bounded, operator-facing resident activity projection.

    The agent persists phase/action transitions in the session blob. Continuous
    lifetime is process-liveness-bound, so operator silence has no stop clock.
    """
    del now  # compatibility for callers that freeze read-model time in tests
    raw = blob.get("activity")
    activity = dict(raw) if isinstance(raw, dict) else {}
    phase = str(activity.get("phase") or "waiting").strip().lower()
    if phase not in _RESIDENT_ACTIVITY_PHASES:
        phase = "waiting"

    status = str(blob.get("status") or "").strip().lower()
    ended_at = _parse_iso_naive(blob.get("ended_at"))
    if status == STATUS_ENDED or bool(blob.get("stop_requested")) or ended_at is not None:
        phase = "ended"
    elif blob.get("worker_state") == "retry_wait":
        phase = "retrying"

    activity_contract = resident_activity_lifecycle_contract(activity, phase=phase)

    last_action = str(activity.get("last_action") or "").strip()
    if not last_action:
        last_action = {
            "working": "Working",
            "waiting": "Waiting for operator input or project movement",
            "retrying": str(
                blob.get("worker_retry_reason")
                or "Provider temporarily unavailable"
            ),
            "stopping": "Idle limit approaching",
            "ended": "Session ended",
        }[phase]

    def _count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    result: dict[str, Any] = {
        "phase": phase,
        "phase_started_at": activity.get("phase_started_at")
        or blob.get("updated_at")
        or blob.get("started_at"),
        "last_action": last_action[:_RESIDENT_ACTIVITY_LAST_ACTION_MAX_CHARS],
        **activity_contract,
        "consultation_verified": bool(
            activity.get("consultation_verified")
            and activity_contract.get("consultation_id")
        ),
        "pending_operator_messages": _count(
            activity.get("pending_operator_messages")
        ),
        "next_review_at": activity.get("next_review_at"),
        "idle_remaining_seconds": None,
        "pending_watches": _count(activity.get("pending_watches")),
        "fired_watches": _count(activity.get("fired_watches")),
    }
    return result


def _normalized_activity_update(
    value: Any,
    *,
    current: dict[str, Any] | None,
    now_iso: str,
    pending_operator_messages: int,
    pending_watches: int,
    fired_watches: int,
) -> dict[str, Any]:
    """Validate and bound the agent-authored portion of the activity object."""
    raw = value if isinstance(value, dict) else {}
    previous = current if isinstance(current, dict) else {}
    phase = str(raw.get("phase") or previous.get("phase") or "waiting").lower()
    if phase not in {"working", "waiting"}:
        phase = "waiting"
    previous_phase = str(previous.get("phase") or "")
    phase_started_at = (
        previous.get("phase_started_at")
        if previous_phase == phase
        else now_iso
    )
    last_action = str(
        raw.get("last_action")
        or previous.get("last_action")
        or ("Working" if phase == "working" else "Waiting for project movement")
    ).strip()
    next_review_at = raw.get("next_review_at")
    if not isinstance(next_review_at, str) or _parse_iso_naive(next_review_at) is None:
        next_review_at = None

    return {
        "phase": phase,
        "phase_started_at": phase_started_at or now_iso,
        "last_action": last_action[:_RESIDENT_ACTIVITY_LAST_ACTION_MAX_CHARS],
        **resident_activity_contract(raw, phase=phase),
        "pending_operator_messages": max(0, int(pending_operator_messages)),
        "next_review_at": next_review_at,
        "pending_watches": max(0, int(pending_watches)),
        "fired_watches": max(0, int(fired_watches)),
    }


def resident_session_snapshot(
    vision: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
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
        "activity": _resident_activity_snapshot(blob, now=now),
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


def _resident_task_awaiting_runner_pull(task: Task | None) -> bool:
    """True iff *task* is a local-runner pin the operator's runner has not pulled.

    Delegates to the canonical ``reaper_orphan_classify._is_runner_pinned_awaiting_pull``
    (``vm_instance_id`` prefixed ``runner:`` + ``started_at IS NULL``) so this
    predicate and the reaper carve-out can never drift apart. Imported lazily to
    keep the reaper's import graph off this module's load path.
    """
    if task is None:
        return False
    from app.reaper_orphan_classify import _is_runner_pinned_awaiting_pull

    return _is_runner_pinned_awaiting_pull(task)


def _resident_worker_health_verdict(
    task: Task | None,
    blob: dict[str, Any],
    now: datetime,
    *,
    pinned_runner_live: bool | None = None,
) -> str | None:
    """Return ``None`` when the recorded worker is reusable, else WHY it is not.

    The return value is one of the ``RESIDENT_SUPERSEDE_*`` cause codes, so the
    caller can record an accurate reason on the row it cancels rather than a
    fixed sentence.

    ``pinned_runner_live`` is the liveness of the runner a not-yet-pulled task is
    pinned to (``None`` = not resolved / unknown). It is the ONLY thing that can
    condemn a runner pin still awaiting its pull — see the carve-out below.
    """
    if task is None:
        return RESIDENT_SUPERSEDE_WORKER_TASK_MISSING
    if task.status not in {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }:
        return RESIDENT_SUPERSEDE_WORKER_TASK_TERMINAL
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}:
        retry_until = task.retry_after_until
        if retry_until is not None and retry_until.tzinfo is not None:
            retry_until = retry_until.astimezone(timezone.utc).replace(tzinfo=None)
        if retry_until is not None and retry_until > now:
            # The Task column is authoritative. The blob is an operator-facing
            # projection that may transiently lag its post-commit mirror.
            return None
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
            return None
    if task.status in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}:
        # --- Runner pin still AWAITING ITS PULL ---------------------------
        # A task hard-pinned to the operator's local runner sits PROVISIONING
        # with ``started_at IS NULL`` until that runner polls
        # ``POST /api/runner/next-task``. There is NO worker yet, so there is no
        # worker liveness to have gone stale — and the wait is bounded by the
        # RUNNER'S QUEUE DEPTH, which is routinely tens of minutes on a busy
        # single-runner fleet, not by anything this session controls.
        #
        # Judging that state by the start-claim clock below is what produced the
        # phase-2 supersession loop: the pin was condemned at the 5-minute mark,
        # the replacement generation spawned a BRAND-NEW task at the BACK of the
        # same runner queue, and the reset repeated — 177 generations deep, ~10
        # cycles/hour, zero completions, because the queue position was thrown
        # away faster than the runner could ever reach it. Supersession is only
        # meant to settle a genuine race between two live workers; applied to a
        # queued pin it is a self-inflicted livelock that can never converge.
        #
        # The reaper already reached this conclusion for the same state
        # (``_is_runner_pinned_awaiting_pull``): let the work wait for the runner
        # instead of churning requeue -> repin -> reap. Mirror it here. The only
        # thing that can condemn a pin awaiting pull is the PINNED RUNNER being
        # gone — an unresolved/unknown liveness never supersedes, because a
        # false "offline" here destroys queued work. A genuinely abandoned
        # local-required session is still bounded elsewhere by the orchestrator's
        # ``_fail_stale_local_runner_holds`` cloud-fallback sweep.
        #
        # Scoped to a VIRGIN pin — one that has never had a worker at all. A task
        # that has already heartbeated, or that carries a retry deadline / a
        # ``retry_wait`` session state, HAS a worker history and a bounded retry
        # contract of its own; those keep the existing reclaim behaviour below,
        # because for them a missing heartbeat is evidence of a DEAD worker
        # rather than of a worker that was never born. All 240 rows from the
        # production incident satisfy every clause here (240/240 pinned with
        # ``attempt_count = 0`` and no retry deadline).
        if (
            _resident_task_awaiting_runner_pull(task)
            and task.last_heartbeat is None
            and task.retry_after_until is None
            and blob.get("worker_state") != "retry_wait"
        ):
            if pinned_runner_live is False:
                return RESIDENT_SUPERSEDE_PINNED_RUNNER_OFFLINE
            return None
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
                return None
        started = _parse_iso_naive(blob.get("started_at"))
        if (
            started
            and (now - started).total_seconds() <= _RESIDENT_START_CLAIM_STALE_SECONDS
        ):
            return None
        return RESIDENT_SUPERSEDE_START_CLAIM_STALE
    worker_heartbeat = _parse_iso_naive(blob.get("worker_heartbeat_at"))
    if worker_heartbeat is not None:
        worker_age = (now - worker_heartbeat).total_seconds()
        if worker_age <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS:
            return None
        heartbeat = task.last_heartbeat
        if heartbeat is not None:
            if heartbeat.tzinfo is not None:
                heartbeat = heartbeat.astimezone(timezone.utc).replace(tzinfo=None)
            task_fresh = (
                now - heartbeat
            ).total_seconds() <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS
            if task_fresh and worker_age <= (
                _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS
                + _RESIDENT_TASK_HEARTBEAT_CORROBORATION_GRACE_SECONDS
            ):
                return None
            return RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE
        return RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE

    # During startup an absent heartbeat is expected until the resident handler
    # makes its first lease renewal. Keep the start claim briefly even when the
    # generic task heartbeat has not arrived yet.
    startup_seen = _parse_iso_naive(blob.get("started_at"))
    if (
        startup_seen is None
        or (now - startup_seen).total_seconds() > _RESIDENT_START_CLAIM_STALE_SECONDS
    ):
        return RESIDENT_SUPERSEDE_START_CLAIM_STALE
    heartbeat = task.last_heartbeat
    if heartbeat is None:
        return None
    if heartbeat.tzinfo is not None:
        heartbeat = heartbeat.astimezone(timezone.utc).replace(tzinfo=None)
    if (
        now - heartbeat
    ).total_seconds() <= _RESIDENT_WORKER_HEARTBEAT_STALE_SECONDS:
        return None
    return RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE


def _resident_worker_task_is_healthy(
    task: Task | None,
    blob: dict[str, Any],
    now: datetime,
    *,
    pinned_runner_live: bool | None = None,
) -> bool:
    """Return whether the recorded worker is healthy enough to reuse."""
    return (
        _resident_worker_health_verdict(
            task, blob, now, pinned_runner_live=pinned_runner_live
        )
        is None
    )


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
                            updated["activity"] = {
                                "phase": "retrying",
                                "phase_started_at": now.isoformat(),
                                "last_action": str(
                                    task.latest_status_message
                                    or "Provider temporarily unavailable"
                                )[:_RESIDENT_ACTIVITY_LAST_ACTION_MAX_CHARS],
                                "pending_operator_messages": int(
                                    (
                                        (current or {}).get("activity")
                                        if isinstance(
                                            (current or {}).get("activity"), dict
                                        )
                                        else {}
                                    ).get("pending_operator_messages")
                                    or 0
                                ),
                                "next_review_at": retry_until.isoformat(),
                            }
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
                        previous_activity = (
                            updated.get("activity")
                            if isinstance(updated.get("activity"), dict)
                            else {}
                        )
                        updated["activity"] = {
                            **previous_activity,
                            "phase": "waiting",
                            "phase_started_at": now_iso,
                            "last_action": "Retry hold cleared; waiting for work",
                            "next_review_at": None,
                        }

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


def _session_scope_conditions(
    resident_session_id: str | None,
    conversation_floor_sequence: int | None = None,
) -> list:
    """WHERE fragments scoping operator messages to a resident session.

    When ``resident_session_id`` is provided, only messages stamped for THAT
    session — plus legacy/cadence NULL-session rows (never hidden: backward
    compat so pre-migration backlog is not silently dropped) — are matched. A
    message stamped for a DIFFERENT session is excluded, so session A's messages
    do not pollute session B's pacing signal. When
    ``resident_session_id`` is None (older callers / cadence path) the scope is
    empty and the query behaves project-wide exactly as before.
    """
    if resident_session_id is None:
        return []
    return [
        resident_broad_message_scope(
            resident_session_id,
            conversation_floor_sequence=conversation_floor_sequence,
        )
    ]
def _resident_wake_barrier_contract():
    return and_(
        ProjectMessage.context["audience"].as_string()
        == "strategist-internal",
        ProjectMessage.context["resident_wake_barrier"].as_boolean().is_(True),
    )


async def _resident_wake_barrier_receipts(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return bounded exact receipts for open semantic Resident barriers."""

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.status == "open",
                    _resident_wake_barrier_contract(),
                )
                .order_by(ProjectMessage.created_at.asc(), ProjectMessage.id.asc())
                .limit(_RESIDENT_WAKE_BARRIER_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    receipts: list[dict[str, Any]] = []
    for row in rows:
        context = row.context if isinstance(row.context, dict) else {}
        receipts.append(
            {
                "id": str(row.id),
                "event_type": str(context.get("event_type") or "")[:128],
                "summary": str(row.content or "")[:2_000],
                "created_at": row.created_at.isoformat(),
                "task_id": str(row.from_task_id) if row.from_task_id else None,
                "context": {
                    key: context[key]
                    for key in _RESIDENT_WAKE_BARRIER_CONTEXT_KEYS
                    if key in context
                },
            }
        )
    return receipts


async def _compute_resident_work_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    resident_session_id: str | None = None,
    conversation_floor_sequence: int | None = None,
    resident_task_id: uuid.UUID | None = None,
) -> tuple[str | None, dict[str, int]]:
    """READ-ONLY snapshot of strategist-relevant world state for the heartbeat.

    Returns ``(work_state_rev, work_state_summary)``:

    * ``work_state_rev`` — a stable fingerprint over ACTIONABLE transitions:
      operator arrivals, terminal Strategic cycles, blockers/failures, and the
      quiescent barrier after the Resident's parallel task frontier drains.
      Routine heartbeats, status messages, and intermediate
      QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING motion deliberately do not
      change it. Exact watch receipts are paced independently by the agent so a
      watch resolved later in the same heartbeat wakes exactly once.
    * ``work_state_summary`` — advisory ``{in_flight, terminal_since,
      new_operator_messages}`` counts feeding the agent's tick context, PLUS
      ``project_*`` counts covering the whole project across every
      ``created_by``. The lineage keys answer "what did I spawn"; the
      ``project_*`` keys answer "what is the pipeline doing", which the
      strategist-anchored walk alone cannot see at all because the orchestrator
      authors nearly everything. Both are advisory — neither wakes a turn, and
      only the lineage keys gate exploration pacing.

    This is DELIBERATELY cheap (a handful of indexed ``max()``/``count()``
    aggregates) and STRICTLY read-only: it never reads or mutates the lease,
    the continuation freshness marker, or the session blob, so surfacing it on
    the heartbeat cannot extend the dead-man's switch (reaper uses only
    last_heartbeat; ``_classify_resident_session`` in reaper_orphan_classify.py
    ignores this read-model path).
    """
    # One bounded recursive CTE aggregates the strategist task subtree without
    # a Python-side walk or cycle risk. Every STRATEGIC_AGENT-created task is an
    # anchor so descendants with another provenance are still visible. DISTINCT
    # removes the duplicate paths produced when a Strategic-created descendant
    # is both an anchor and reached recursively from its parent.
    #
    # Do not count the long-lived Resident host task itself as owned work: its
    # generic task heartbeat keeps it IN_PROGRESS by design and would otherwise
    # make ``in_flight`` permanently non-zero.
    terminal_cutoff = _utcnow() - timedelta(seconds=_WORK_STATE_TERMINAL_WINDOW_SECONDS)
    tree_row = (
        await session.execute(
            text("""
                WITH RECURSIVE task_tree AS (
                    SELECT id, updated_at, completed_at, blocked_at, parked_at,
                           archived, status, 0 AS depth
                    FROM task
                    WHERE project_id = :project_id
                      AND created_by = :created_by
                      AND (:resident_task_id IS NULL OR id != :resident_task_id)
                    UNION ALL
                    SELECT t.id, t.updated_at, t.completed_at, t.blocked_at,
                           t.parked_at, t.archived, t.status,
                           tt.depth + 1
                    FROM task t
                    INNER JOIN task_tree tt ON t.parent_task_id = tt.id
                    WHERE t.project_id = :project_id
                      AND (:resident_task_id IS NULL OR t.id != :resident_task_id)
                      AND tt.depth < :max_depth
                ), task_state AS (
                    SELECT DISTINCT id, updated_at, completed_at, blocked_at,
                                    parked_at, archived, status
                    FROM task_tree
                )
                SELECT
                    max(coalesce(completed_at, updated_at)) FILTER (
                        WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                    ) AS max_terminal,
                    max(coalesce(completed_at, updated_at)) FILTER (
                        WHERE status IN ('FAILED', 'CANCELLED')
                    ) AS max_failure,
                    max(coalesce(blocked_at, updated_at)) FILTER (
                        WHERE NOT archived AND status = 'BLOCKED'
                    ) AS max_blocked,
                    max(coalesce(parked_at, updated_at)) FILTER (
                        WHERE NOT archived AND status = 'PARKED'
                    ) AS max_parked,
                    count(*) FILTER (WHERE NOT archived) AS total,
                    count(*) FILTER (
                        WHERE NOT archived
                          AND status IN (
                              'QUEUED', 'PROVISIONING',
                              'IN_PROGRESS', 'EVALUATING'
                          )
                    ) AS in_flight,
                    count(*) FILTER (
                        WHERE NOT archived AND status = 'BLOCKED'
                    ) AS blocked,
                    count(*) FILTER (
                        WHERE NOT archived AND status = 'PARKED'
                    ) AS parked,
                    count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                    count(*) FILTER (WHERE status = 'FAILED') AS failed,
                    count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
                    count(*) FILTER (
                        WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                          AND coalesce(completed_at, updated_at) >= :terminal_cutoff
                    ) AS terminal_since
                FROM task_state
            """)
            .bindparams(
                bindparam("project_id", type_=Task.__table__.c.project_id.type),
                bindparam("resident_task_id", type_=Task.__table__.c.id.type),
                bindparam("terminal_cutoff", type_=Task.__table__.c.updated_at.type),
            )
            .columns(
                max_terminal=Task.__table__.c.updated_at.type,
                max_failure=Task.__table__.c.updated_at.type,
                max_blocked=Task.__table__.c.updated_at.type,
                max_parked=Task.__table__.c.updated_at.type,
            ),
            {
                "project_id": project_id,
                "created_by": _STRATEGIC_AGENT_CREATED_BY,
                "resident_task_id": resident_task_id,
                "max_depth": _WORK_STATE_ANCESTOR_WALK_MAX_DEPTH,
                "terminal_cutoff": terminal_cutoff,
            },
        )
    ).fetchone()

    max_terminal = tree_row[0] if tree_row else None
    max_failure = tree_row[1] if tree_row else None
    max_blocked = tree_row[2] if tree_row else None
    max_parked = tree_row[3] if tree_row else None
    total: int = int(tree_row[4] or 0) if tree_row else 0
    in_flight: int = int(tree_row[5] or 0) if tree_row else 0
    blocked: int = int(tree_row[6] or 0) if tree_row else 0
    parked: int = int(tree_row[7] or 0) if tree_row else 0
    completed: int = int(tree_row[8] or 0) if tree_row else 0
    failed: int = int(tree_row[9] or 0) if tree_row else 0
    cancelled: int = int(tree_row[10] or 0) if tree_row else 0
    terminal_since: int = int(tree_row[11] or 0) if tree_row else 0

    # ── PROJECT-WIDE ambient activity (every ``created_by``) ──────────────
    #
    # Everything above is anchored on ``created_by = 'STRATEGIC_AGENT'``, which
    # answers "what did I spawn" and is silent about the pipeline: the
    # orchestrator authors the overwhelming majority of real work as
    # ``ORCHESTRATOR``, so a Resident watching only its own lineage reports a
    # quiet system while the fleet is busy. That is the wrong answer for the
    # agent that is supposed to be the front for the whole pipeline.
    #
    # SCOPE, and what is deliberately left out:
    #
    # * COUNTS ONLY, never rows. This runs on EVERY heartbeat, and its output
    #   lands in the resident's context and from there in the strategic memory
    #   envelope — the surface whose oversize degrades into a coreless stub and
    #   livelocks the reclaim loop (PR #17424). A fixed handful of integers is
    #   bounded by construction no matter how large the estate gets. The
    #   row-level view already exists and is correctly on-demand rather than
    #   ambient: ``./fetch task-census`` (GET .../task-census) returns the
    #   oldest non-terminal tasks with ages and blocked/parked reasons, under
    #   its own byte clamp. Duplicating that onto the heartbeat would put an
    #   unbounded payload on the hot path to say what a pull already says.
    # * NO by-type or by-author breakdown. Task type is a free string out of
    #   payload JSONB with structurally unbounded cardinality; a count map is
    #   untrimmable by the response clamp, so the census pays for
    #   ``bound_counts`` to carry one. The heartbeat does not need the
    #   vocabulary, only the volume.
    # * TERMINAL history is bounded by the existing one-hour window, not by a
    #   full-history count, so this cannot grow with the age of the project.
    # * The Resident's own host row is excluded, exactly as it is from the
    #   lineage walk above: its generic task heartbeat keeps it IN_PROGRESS for
    #   as long as the Resident is alive, so counting it would make
    #   ``project_in_flight`` permanently non-zero and a genuinely idle project
    #   indistinguishable from a busy one.
    #
    # And critically: these values are NOT folded into ``semantic_state`` /
    # ``work_state_rev``. The rev is a WAKE fingerprint — the agent runs a full
    # model turn whenever it changes (``shouldRunAutonomousTurn``). Anchoring it
    # to estate-wide activity would wake the Resident on every ORCHESTRATOR
    # transition anywhere in the project, which is the opposite failure mode
    # from blindness and a far more expensive one. Nor do they reuse the
    # ``in_flight`` / ``blocked`` / ``parked`` key names: the agent's
    # ``hasPendingOwnedWork`` reads ``in_flight`` to decide whether to SUPPRESS
    # unchanged-state exploration turns, so widening that key in place would
    # silence the Resident entirely for as long as the fleet had anything
    # running. Separate ``project_*`` keys are additive and advisory: they
    # inform the turn, they do not schedule it.
    project_rows = (
        await session.execute(
            text("""
                SELECT status, count(*) AS n
                FROM (
                    SELECT status
                    FROM task
                    WHERE project_id = :project_id
                      AND NOT archived
                      AND (:resident_task_id IS NULL OR id != :resident_task_id)
                      AND status IN (
                          'QUEUED', 'PROVISIONING', 'IN_PROGRESS',
                          'EVALUATING', 'BLOCKED', 'PARKED'
                      )
                    LIMIT :scan_limit
                ) AS live
                GROUP BY status
            """).bindparams(
                bindparam("project_id", type_=Task.__table__.c.project_id.type),
                bindparam("resident_task_id", type_=Task.__table__.c.id.type),
            ),
            {
                "project_id": project_id,
                "resident_task_id": resident_task_id,
                "scan_limit": _WORK_STATE_PROJECT_SCAN_LIMIT + 1,
            },
        )
    ).fetchall()
    project_by_status: dict[str, int] = {
        str(getattr(row[0], "value", row[0])): int(row[1] or 0)
        for row in project_rows
    }
    project_scanned = sum(project_by_status.values())
    project_in_flight = sum(
        project_by_status.get(status, 0)
        for status in _WORK_STATE_NON_TERMINAL_STATUSES
    )
    project_terminal_since = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        (
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.CANCELLED,
                        )
                    ),
                    Task.updated_at >= terminal_cutoff,
                )
            )
        ).scalar()
        or 0
    )

    msg_max = (
        await session.execute(
            select(func.max(ProjectMessage.created_at)).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(_OPERATOR_MESSAGE_FROM_TYPES),
                *_session_scope_conditions(
                    resident_session_id,
                    conversation_floor_sequence,
                ),
            )
        )
    ).scalar_one_or_none()
    terminal_cycle_max = (
        await session.execute(
            select(func.max(StrategicCycle.updated_at)).where(
                StrategicCycle.project_id == project_id,
                StrategicCycle.outcome.in_(_WORK_STATE_TERMINAL_CYCLE_OUTCOMES),
            )
        )
    ).scalar_one_or_none()
    # Release/qualification integration emits a narrow strategist-internal
    # barrier message. Do not wake on the much larger generic internal feed
    # (PR bookkeeping, telemetry, etc.); only this explicit boolean contract is
    # an autonomous decision boundary. The revision marker considers all such
    # rows so resolving the newest open row cannot roll the revision backwards;
    # the separate count remains open-only for advisory activity state.
    barrier_contract = _resident_wake_barrier_contract()
    barrier_message_row = (
        await session.execute(
            select(
                func.max(ProjectMessage.created_at).filter(barrier_contract),
                func.count().filter(
                    and_(barrier_contract, ProjectMessage.status == "open")
                ),
            ).where(ProjectMessage.project_id == project_id)
        )
    ).one()
    barrier_msg_max = barrier_message_row[0]
    pending_barriers = int(barrier_message_row[1] or 0)
    watch_counts = (
        await session.execute(
            select(
                func.count().filter(StrategicWatch.state == "pending"),
                func.count().filter(
                    StrategicWatch.state.in_(("fired", "expired"))
                ),
            ).where(StrategicWatch.project_id == project_id)
        )
    ).one()
    def _stamp(value: datetime | str | None) -> str:
        """Canonicalize SQLite text / asyncpg datetime aggregates."""

        if value is None:
            return ""
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = value
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.isoformat()

    # Intermediate successful completions are deliberately absent while any
    # work remains in flight. Once the whole frontier drains, the quiescent
    # barrier carries the latest terminal stamp + settled counts and wakes one
    # continuation turn. Failures/cancellations and blockers remain immediately
    # actionable even while sibling work continues.
    semantic_state: dict[str, Any] = {}
    if msg_max is not None:
        semantic_state["operator"] = _stamp(msg_max)
    if barrier_msg_max is not None:
        semantic_state["resident_barrier"] = _stamp(barrier_msg_max)
    if terminal_cycle_max is not None:
        semantic_state["terminal_cycle"] = _stamp(terminal_cycle_max)
    if blocked or max_blocked is not None:
        semantic_state["blocked"] = [blocked, _stamp(max_blocked)]
    if parked or max_parked is not None:
        semantic_state["parked"] = [parked, _stamp(max_parked)]
    if failed or cancelled or max_failure is not None:
        semantic_state["failed"] = [failed, cancelled, _stamp(max_failure)]
    if total > 0 and in_flight == 0:
        semantic_state["barrier"] = [
            completed,
            failed,
            cancelled,
            _stamp(max_terminal),
        ]
    work_state_rev = (
        hashlib.sha256(
            json.dumps(
                semantic_state,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        if semantic_state
        else None
    )

    new_operator_messages = (
        await session.execute(
            select(func.count())
            .select_from(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type.in_(_OPERATOR_MESSAGE_FROM_TYPES),
                ProjectMessage.status == "open",
                *_session_scope_conditions(
                    resident_session_id,
                    conversation_floor_sequence,
                ),
            )
        )
    ).scalar() or 0
    return work_state_rev, {
        "in_flight": int(in_flight),
        "terminal_since": int(terminal_since),
        "new_operator_messages": int(new_operator_messages),
        "pending_watches": int(watch_counts[0] or 0),
        "fired_watches": int(watch_counts[1] or 0),
        "pending_barriers": pending_barriers,
        "blocked": int(blocked),
        "parked": int(parked),
        "failed": int(failed),
        "quiescent": int(total > 0 and in_flight == 0),
        # Project-wide ambient activity across EVERY created_by. Advisory:
        # never folded into work_state_rev, never named like the lineage keys
        # the agent's pacing gate reads. See the block above the query.
        "project_in_flight": int(project_in_flight),
        "project_blocked": int(project_by_status.get("BLOCKED", 0)),
        "project_parked": int(project_by_status.get("PARKED", 0)),
        "project_terminal_since": int(project_terminal_since),
        "project_scan_capped": int(project_scanned > _WORK_STATE_PROJECT_SCAN_LIMIT),
    }


async def _release_start_claim(
    project_id: uuid.UUID,
    session_id: str,
    spawned_task_id: uuid.UUID | None,
    *,
    worker_generation: int,
    failure_reason: str | None = None,
    session_factory=None,
) -> None:
    """Compensate a failed start: end our claim and cancel its exact task.

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
            completion_events = []
            released_claim: dict[str, Any] = {
                "owned": False,
                "task_id": None,
            }

            def _clear(vision: dict[str, Any]) -> None:
                current = read_resident_session(vision)
                if (
                    isinstance(current, dict)
                    and str(current.get("session_id")) == str(session_id)
                    and int(current.get("worker_generation") or 1) == worker_generation
                ):
                    released_claim["owned"] = True
                    released_claim["task_id"] = current.get("task_id")
                    ended_at = _utcnow().isoformat()
                    ended = dict(current)
                    ended.update(
                        {
                            "status": STATUS_ENDED,
                            "stop_requested": False,
                            "worker_state": "restart_failed",
                            "ended_at": ended_at,
                            "updated_at": ended_at,
                        }
                    )
                    vision[RESIDENT_SESSION_KEY] = ended
                    vision[RESIDENT_SESSION_REASON_KEY] = {
                        "reason": (
                            f"{RESIDENT_SESSION_REASON_START_FAILED_PREFIX}: "
                            f"{failure_reason or 'resident start compensation'}"
                        )[:200],
                        "recorded_at": ended_at,
                    }

            await with_vision_locked(session, project_id, _clear, skip_locked=False)
            if released_claim["owned"]:
                # The provisional session may already own input rebound from its
                # predecessor. Release those rows and stamp its exact identity
                # before removing the failed authority, so the next start can
                # carry them without reopening arbitrary NULL-session backlog.
                from app.session_message_handoff import _apply_handoff_mutations

                await _apply_handoff_mutations(session, project_id, session_id)
            compensation_task_id = spawned_task_id
            if compensation_task_id is None and released_claim["task_id"]:
                # An adopted task is not assigned to ``spawned_task_id``. If its
                # finalize commit had an unknown outcome, the identity-guarded
                # session blob is the durable authority that names the exact
                # task this failed generation stamped. Recover it while the
                # Project lock remains held, then take Task second below.
                try:
                    compensation_task_id = uuid.UUID(
                        str(released_claim["task_id"])
                    )
                except (AttributeError, TypeError, ValueError):
                    logger.warning(
                        "[RESIDENT_SESSION] failed start session %s recorded "
                        "invalid task id %r",
                        session_id,
                        released_claim["task_id"],
                    )
            if compensation_task_id is not None:
                # The project lock remains held. Acquire the task second and
                # cancel only the task stamped by THIS failed generation.
                async with task_lock(
                    session, compensation_task_id, skip_locked=False
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
                        task.context = {
                            **context,
                            RESIDENT_INFRASTRUCTURE_CONTEXT_KEY: True,
                            RESIDENT_LIFECYCLE_DISPOSITION_CONTEXT_KEY: (
                                "start_compensation"
                            ),
                        }
                        session.add(task)
                        completion_event = await _reconcile_resident_terminal_task(
                            session,
                            task,
                            source="resident_start_compensation",
                        )
                        if completion_event is not None:
                            completion_events.append(completion_event)
            await session.commit()
        if completion_events:
            from app import strategic_cycle

            await strategic_cycle.emit_terminal_completion_events(completion_events)
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


def _apply_reply_protocol_recovery_hold(
    vision: dict[str, Any],
    task: Task | None,
    *,
    expected_identity: tuple[str, str, int],
    now_iso: str,
) -> bool:
    """Materialize one exact no-replay terminal while Project→Task locks are held."""
    current = read_resident_session(vision)
    session_id, expected_task_id, generation = expected_identity
    current_identity = (
        str(current.get("session_id")) if isinstance(current, dict) else "",
        str(current.get("task_id")) if isinstance(current, dict) else "",
        int(current.get("worker_generation") or 1) if isinstance(current, dict) else 0,
    )
    if (
        not resident_session_is_active(current)
        or current_identity != expected_identity
        or task is None
        or str(task.id) != expected_task_id
        or task.status not in _TERMINAL_TASK_STATUSES
        or not _task_matches_resident_identity(
            task, session_id=session_id, worker_generation=generation
        )
        or resolved_failure_class(task).strip().upper()
        != RESIDENT_REPLY_PROTOCOL_FAILURE
    ):
        return False
    ended = dict(current)
    ended.update({
        "status": STATUS_ENDED,
        "stop_requested": False,
        "worker_state": "recovery_required",
        "updated_at": now_iso,
        "ended_at": now_iso,
        "activity": {
            "phase": "ended",
            "phase_started_at": now_iso,
            "last_action": (
                "Authenticated operator reply delivery failed; explicit recovery required"
            ),
            "next_review_at": None,
        },
    })
    vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)
    vision[RESIDENT_SESSION_KEY] = ended
    vision[RESIDENT_SESSION_REASON_KEY] = {
        "reason": (
            f"{RESIDENT_SESSION_REASON_REPLY_PROTOCOL_FAILED}: bounded same-thread "
            "delivery correction exhausted; await canonical claim release before "
            "explicit recovery"
        ),
        "recorded_at": now_iso,
    }
    return True


async def _reply_protocol_recovery_start_mode(
    session: AsyncSession,
    project_id: uuid.UUID,
    predecessor_session_id: str,
    expected_interaction_mode: str | None,
) -> str | None:
    if expected_interaction_mode is not None:
        return "reply_recovery_required"
    claimed_predecessor = await session.scalar(
        select(ProjectMessage.id)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.resident_session_id == predecessor_session_id,
            ProjectMessage.from_type == "user",
            ProjectMessage.surface == MessageSurface.CONVERSATION,
            ProjectMessage.status == "open",
            or_(
                ProjectMessage.handling_state == MessageHandlingState.CLAIMED,
                ProjectMessage.handled_by_task_id.is_not(None),
                ProjectMessage.conversation_claim_id.is_not(None),
                ProjectMessage.conversation_claim_generation.is_not(None),
                ProjectMessage.conversation_claim_incarnation.is_not(None),
                ProjectMessage.conversation_claim_expires_at.is_not(None),
            ),
        )
        .with_for_update()
        .limit(1)
    )
    return "reply_recovery_claim_held" if claimed_predecessor is not None else None


def resident_worker_is_healthy(
    blob: dict[str, Any] | None,
    task: Task | None,
    now: datetime,
) -> bool:
    """Return whether a session blob has its exact, live Resident worker.

    A status-only session blob is not liveness evidence: it can outlive a
    terminal task, point at a replacement generation, or retain an old
    heartbeat. Reuse the same identity and bounded-freshness contracts as the
    session ensure path so the starvation sweep cannot suppress a halt from a
    merely active-looking projection.
    """
    if not resident_session_is_active(blob) or task is None:
        return False
    task_id = (blob or {}).get("task_id")
    session_id = (blob or {}).get("session_id")
    if not task_id or not session_id:
        return False
    try:
        worker_generation = int((blob or {}).get("worker_generation") or 1)
    except (TypeError, ValueError):
        return False
    if str(task.id) != str(task_id) or task.archived:
        return False
    if not _task_matches_resident_identity(
        task,
        session_id=str(session_id),
        worker_generation=worker_generation,
    ):
        return False
    return _resident_worker_task_is_healthy(task, blob or {}, now)


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
    reason: str,
    replacement_session_id: str | None = None,
) -> None:
    """Cancel one exact stale resident task while its row lock is held.

    Records WHY this row lost and WHICH generation/session won, so an operator
    reading the cancelled task can tell a genuine two-worker race from an
    infrastructure condemnation. The pre-fix code hard-coded a single
    "heartbeat became stale" sentence onto every supersession, which misreported
    the dominant real-world case: a runner pin that had never been pulled and so
    never had a heartbeat at all.
    """
    task.status = TaskStatus.CANCELLED
    cause = RESIDENT_SUPERSEDE_REASON_TEXT.get(reason, reason)
    winner = f"generation {replacement_generation}"
    if replacement_session_id:
        winner += f" (session {replacement_session_id})"
    task.latest_status_message = (
        f"Resident worker superseded by {winner} because {cause}"
    )
    context = dict(task.context or {})
    context["failure_category"] = ADMIN_CANCEL
    context[RESIDENT_INFRASTRUCTURE_CONTEXT_KEY] = True
    context[RESIDENT_LIFECYCLE_DISPOSITION_CONTEXT_KEY] = "worker_superseded"
    context["_resident_superseded"] = True
    context["_resident_superseded_at"] = now_iso
    context["_resident_superseded_by_generation"] = replacement_generation
    context["_resident_superseded_reason"] = reason
    if replacement_session_id:
        context["_resident_superseded_by_session_id"] = replacement_session_id
    task.context = context
    flag_modified(task, "context")


async def _spawn_resident_strategic_task(
    session: AsyncSession,
    project: Project,
    runner_id: str | None,
    *,
    operator_user_id: str,
    session_id: str,
    worker_generation: int = 1,
    conversation_floor_sequence: int | None = None,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
    blacklisted_accounts: list[str] | None = None,
    allow_paused_project: bool = False,
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
            conversation_floor_sequence=conversation_floor_sequence,
            worker_generation=worker_generation,
            session_summary=session_summary,
            decisions_register=decisions_register,
            blacklisted_accounts=blacklisted_accounts,
        ),
        extra_payload=_resident_typed_fast_payload(),
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
        task_priority=RESIDENT_TASK_PRIORITY,
        launched_by_user_id=str(operator_user_id),
        allow_paused_project=allow_paused_project,
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


async def _pinned_runner_is_live(
    session: AsyncSession, task: Task | None
) -> bool | None:
    """Liveness of the local runner *task* is hard-pinned to.

    ``None`` means "no opinion" — the task is not runner-pinned, or the pin is
    unparseable — and callers MUST treat that as non-condemning.

    Deliberately gates on LIVENESS ONLY (``_local_runner_is_live``: active status
    + recent heartbeat), NOT the fuller ``_resident_runner_is_available`` gate
    used to *select* a device. Choosing where to start a new session may be
    picky; deciding whether to destroy already-queued work may not. A live runner
    that transiently reports ``agent_available: false`` or a momentarily narrowed
    task-type list will still drain its pin queue, and cancelling its pin would
    throw away the queue position the task has already earned.
    """
    vm_id = getattr(task, "vm_instance_id", None)
    if not isinstance(vm_id, str) or not vm_id.startswith("runner:"):
        return None
    try:
        runner_uuid = uuid.UUID(vm_id.split(":", 1)[1].strip())
    except (TypeError, ValueError, IndexError):
        return None

    from app.models import LocalRunner
    from app.strategic_spawning import _local_runner_is_live

    runner = await session.get(LocalRunner, runner_uuid)
    if runner is None or getattr(runner, "status", None) == "deleted":
        # The pin names a device that no longer exists: nothing will ever pull
        # it, so this is the one case where waiting is strictly worse.
        return False
    return bool(_local_runner_is_live(runner))


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


def _resident_blacklisted_accounts(context: dict[str, Any] | None) -> list[str]:
    """Return a de-duplicated predecessor account denylist.

    A replacement may avoid an account that just exhausted its quota, but must
    not inherit that attempt's ``llm_*`` runtime identity.  The dispatch path
    selects the replacement account/provider/model anew.
    """
    raw = (context or {}).get("blacklisted_accounts")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        account_id = value.strip()
        if account_id and account_id not in result:
            result.append(account_id)
    return result


def _resident_born_context(
    session_id: str,
    runner_id: str | None,
    *,
    worker_generation: int = 1,
    conversation_floor_sequence: int | None = None,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
    blacklisted_accounts: list[str] | None = None,
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
        RESIDENT_INFRASTRUCTURE_CONTEXT_KEY: True,
        "strategic_session_id": session_id,
        "_resident_worker_generation": worker_generation,
        "provider_preference": list(_RESIDENT_PROVIDER_PREFERENCE),
    }
    if conversation_floor_sequence is not None:
        context[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY] = (
            conversation_floor_sequence
        )
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
    # A replacement Resident must never spend a fresh claim rediscovering an
    # account the predecessor already proved rate-limited.  Carry only this
    # cross-attempt routing guidance, not the predecessor's llm_* runtime
    # assignment: the dispatch path must still select a new account/provider.
    if blacklisted_accounts:
        context["blacklisted_accounts"] = list(blacklisted_accounts)
    return context


def _stamp_resident_context(
    task: Task,
    *,
    session_id: str,
    runner_id: str | None,
    project_autonomous_enabled: bool,
    worker_generation: int = 1,
    conversation_floor_sequence: int | None = None,
    open_messages: list[dict[str, Any]] | None = None,
    open_message_cursor_floor: str | None = None,
    exam_ledger: str | None = None,
    message_cursor: str | None = None,
    session_summary: str | None = None,
    decisions_register: str | None = None,
    blacklisted_accounts: list[str] | None = None,
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
    payloads = list(task.payloads or [])
    if payloads:
        first_payload = dict(task.first_payload or {})
        first_payload.update(_resident_typed_fast_payload())
        # Adoption can claim a cadence task created before the current Resident
        # deployment. Stamp the live canonical project fence at adoption time
        # rather than trusting that task's older bootstrap payload.
        first_payload["project_autonomous_enabled"] = bool(
            project_autonomous_enabled
        )
        if open_messages is not None:
            first_payload["open_messages"] = open_messages
            first_payload["open_message_count"] = len(open_messages)
        if open_message_cursor_floor:
            first_payload[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY] = (
                open_message_cursor_floor
            )
        task.payloads = [first_payload, *payloads[1:]]
        flag_modified(task, "payloads")
    context["_resident_session"] = True
    context[RESIDENT_INFRASTRUCTURE_CONTEXT_KEY] = True
    context["strategic_session_id"] = session_id
    context["_resident_worker_generation"] = worker_generation
    context["provider_preference"] = list(_RESIDENT_PROVIDER_PREFERENCE)
    if conversation_floor_sequence is not None:
        context[RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY] = (
            conversation_floor_sequence
        )
    if open_message_cursor_floor:
        context[RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY] = open_message_cursor_floor
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
    # Adopt-path twin of _resident_born_context: preserve only the denylist
    # needed to avoid an already-exhausted account.  Do not copy llm_* keys;
    # they identify the failed predecessor attempt and would pin the replacement.
    if blacklisted_accounts:
        context["blacklisted_accounts"] = list(blacklisted_accounts)
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
    task.priority = max(int(task.priority or 0), RESIDENT_TASK_PRIORITY)
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


def _uplift_unstarted_resident_task_priority(
    task: Task | None,
    *,
    expected_project_id: uuid.UUID,
    session_id: str,
    worker_generation: int,
) -> bool:
    """Raise one exact unstarted canonical Resident above ordinary runner work."""
    if task is None or task.archived:
        return False
    if task.project_id != expected_project_id:
        return False
    if task.status not in {TaskStatus.QUEUED, TaskStatus.PROVISIONING}:
        return False
    if task.started_at is not None:
        return False
    if (task.first_payload or {}).get("type") != _RESIDENT_STRATEGIC_TASK_TYPE:
        return False
    if not _task_matches_resident_identity(
        task,
        session_id=session_id,
        worker_generation=worker_generation,
    ):
        return False
    if int(task.priority or 0) >= RESIDENT_TASK_PRIORITY:
        return False
    task.priority = RESIDENT_TASK_PRIORITY
    return True


async def _ensure_existing_resident_task_priority(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    session_id: str,
    task_id: uuid.UUID,
    worker_generation: int,
) -> bool:
    """Idempotently uplift an unpulled Resident under Project -> Task locks."""
    async with project_lock(session, project_id, skip_locked=False) as locked_project:
        if locked_project is None:
            return False
        current = read_resident_session(locked_project.vision)
        if (
            not resident_session_is_active(current)
            or current.get("session_id") != session_id
            or str(current.get("task_id")) != str(task_id)
            or int(current.get("worker_generation") or 1) != worker_generation
        ):
            return False
        async with task_lock(session, task_id, skip_locked=False) as locked_task:
            changed = _uplift_unstarted_resident_task_priority(
                locked_task,
                expected_project_id=locked_project.id,
                session_id=session_id,
                worker_generation=worker_generation,
            )
            if changed:
                session.add(locked_task)
            return changed


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
    never clobbers a newer generation after a provisional-claim timeout. Session
    start is an operator interaction, so it anchors the initial marker to now;
    exact resident process heartbeats own subsequent Continuous renewal.
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
    vision.pop(RESIDENT_SESSION_REASON_KEY, None)
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
    vision.pop(RESIDENT_SESSION_REASON_KEY, None)
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


async def _next_resident_conversation_floor_sequence(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Return the first transcript sequence created after a new session claim."""

    latest = await session.scalar(
        select(func.max(ProjectMessage.sequence)).where(
            ProjectMessage.project_id == project_id
        )
    )
    return int(latest or 0) + 1


async def _rebind_predecessor_conversation_handoff(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    predecessor: dict[str, Any] | None,
    resident_session_id: str,
) -> int:
    """Bind only intentionally carried predecessor work to a new session.

    Current code stamps an exact predecessor-session marker while releasing an
    unanswered row. The second predicate is a rollout bridge for rows already
    released by the previous revision: canonical open conversation rows created
    during the immediate predecessor's lifetime belong to that just-ended chat.
    Older project-wide NULL backlog predates the predecessor start and remains
    below the new session floor. Private Workday packet/result rows use only the
    exact handoff marker (never the rollout time bridge), because their server
    provenance is session-bound and must not be inferred from timestamps.
    """

    if not isinstance(predecessor, dict):
        return 0
    predecessor_session_id = str(predecessor.get("session_id") or "").strip()
    if not predecessor_session_id:
        return 0

    from app.session_message_handoff import (
        RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
        _apply_handoff_mutations,
    )
    from app.conversation_coordinator import (
        reset_conversation_claim_for_session_handoff,
    )
    from app.project_message_projection import WAKEUP_STATE_CONTEXT_KEY

    # Repair any still-stamped rows first. This classifies already-answered rows
    # through the canonical handoff evidence check and stamps unanswered rows with
    # the durable marker used below, all inside the caller's Project lock.
    await _apply_handoff_mutations(
        session,
        project_id,
        predecessor_session_id,
    )
    # Test and production session factories may disable autoflush. The exact
    # rebind queries below intentionally target the just-released NULL-session
    # rows, so make that staged handoff state visible inside this transaction.
    await session.flush()

    predecessor_started_at = _parse_iso_naive(predecessor.get("started_at"))
    explicit_handoff = (
        ProjectMessage.context[
            RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY
        ].as_string()
        == predecessor_session_id
    )
    rollout_bridge: object
    if predecessor_started_at is not None:
        rollout_bridge = ProjectMessage.created_at >= predecessor_started_at
    else:
        # An unparseable predecessor lifetime cannot safely distinguish a
        # pre-boundary conversation row from old project backlog.
        rollout_bridge = and_(False)

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.resident_session_id.is_(None),
                    or_(explicit_handoff, rollout_bridge),
                )
                .order_by(
                    ProjectMessage.sequence.asc().nulls_last(),
                    ProjectMessage.created_at.asc(),
                    ProjectMessage.id.asc(),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    rebound = 0
    now = _utcnow()
    for row in rows:
        if dict(row.context or {}).get(RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY) is True:
            # A startup prompt is synthetic session-local input, not operator
            # intent. It must never cross a replacement boundary: otherwise
            # the replacement also creates its own activation and Fast sees two
            # startup turns in one claim. Retire the predecessor prompt while
            # preserving it as hidden audit history.
            row.status = "resolved"
            row.handling_state = MessageHandlingState.RESPONDED
            row.resolution = row.resolution or "session_handoff_activation_retired"
            row.resolved_at = row.resolved_at or now
            reset_conversation_claim_for_session_handoff(row)
            row.updated_at = now
            session.add(row)
            continue
        if (
            row.handling_state == MessageHandlingState.RESPONDED
            or row.resolution
        ):
            continue
        row.resident_session_id = resident_session_id
        row.handling_state = MessageHandlingState.PENDING
        row.handled_by_task_id = None
        reset_conversation_claim_for_session_handoff(row)
        row.context = {
            **dict(row.context or {}),
            WAKEUP_STATE_CONTEXT_KEY: "queued",
        }
        row.updated_at = now
        session.add(row)
        rebound += 1

    coordination_rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.resident_session_id.is_(None),
                )
                .order_by(
                    ProjectMessage.sequence.asc().nulls_last(),
                    ProjectMessage.created_at.asc(),
                    ProjectMessage.id.asc(),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    packet_claim_keys = (
        "_workday_consultation_claim_id",
        "_workday_consultation_claim_generation",
        "_workday_consultation_claim_incarnation",
        "_workday_consultation_claim_expires_at",
        "_workday_consultation_claim_started_at",
    )
    continuation_claim_keys = (
        "_workday_continuation_claim_id",
        "_workday_continuation_claim_generation",
        "_workday_continuation_claim_incarnation",
        "_workday_continuation_claim_expires_at",
        "_workday_continuation_claim_attempt",
    )
    for row in coordination_rows:
        context = dict(row.context or {})
        if (
            context.get(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY)
            != predecessor_session_id
        ):
            continue
        is_packet = bool(
            context.get("_conversation_consultation_packet") is True
            and context.get("audience") == "strategist-internal"
            and context.get("message_role") == "workday_consultation"
            and context.get("public_reply_owner") == "fast"
            and isinstance(
                context.get("_workday_consultation_atomic_reply_id"), str
            )
            and context.get("_workday_consultation_atomic_reply_id")
        )
        is_result = bool(
            context.get("_workday_consultation_result") is True
            and context.get("audience") == "strategist-internal"
            and context.get("message_role") == "workday_consultation_result"
            and context.get("public_reply_owner") == "fast"
        )
        if not is_packet and not is_result:
            continue
        for key in packet_claim_keys if is_packet else continuation_claim_keys:
            context.pop(key, None)
        row.context = context
        row.resident_session_id = resident_session_id
        row.handling_state = MessageHandlingState.PENDING
        row.handled_by_task_id = None
        row.updated_at = now
        session.add(row)
        rebound += 1
    return rebound


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
    allow_paused_project: bool = False,
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
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(project.vision)
    now = _utcnow()
    now_iso = now.isoformat()
    # Fast-path idempotency: an already-active session whose recorded task is
    # still live is returned as-is (no new session, no spawn). Correctness still
    # rests on the atomic claim below; this only spares the row lock for the
    # common "operator re-opens a running session" case.
    initial_vision = dict(project.vision or {})
    existing = read_resident_session(initial_vision)
    had_reason_at_start = RESIDENT_SESSION_REASON_KEY in initial_vision
    reason_at_start = initial_vision.get(RESIDENT_SESSION_REASON_KEY)
    existing_identity_at_start: tuple[str, str | None, int] | None = None
    if isinstance(existing, dict):
        try:
            existing_generation_at_start = int(
                existing.get("worker_generation") or 1
            )
        except (TypeError, ValueError):
            existing_generation_at_start = 1
        existing_identity_at_start = (
            str(existing.get("session_id")),
            str(existing.get("task_id")) if existing.get("task_id") else None,
            existing_generation_at_start,
        )
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
    # Unlike the session-state fields below, account denylist state belongs to
    # the predecessor *attempt*.  It is populated only after that exact task is
    # re-read under the recovery lock, never from an arbitrary old session blob.
    carried_blacklisted_accounts: list[str] = []
    conversation_floor_sequence: int | None = None
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
            and not had_reason_at_start
        ):
            existing_runner_id = str(existing.get("runner_id") or "").strip()
            requested_runner_id = str(runner_id or "").strip()
            if requested_runner_id and existing_runner_id != requested_runner_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Workday is already active on another device; stop the "
                        "current session before changing devices"
                    ),
                )
            existing_session_id = str(existing.get("session_id"))
            existing_generation = int(existing.get("worker_generation") or 1)
            if live is not None and await _ensure_existing_resident_task_priority(
                session,
                project.id,
                session_id=existing_session_id,
                task_id=live.id,
                worker_generation=existing_generation,
            ):
                await session.commit()
                await session.refresh(live)
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
    stale_locked_reason: str | None = None
    stale_orphan_task_ids: list[uuid.UUID] = []
    superseded_task_ids: list[uuid.UUID] = []
    async with project_lock(session, project.id, skip_locked=False) as locked_project:
        if locked_project is None:
            raise HTTPException(
                status_code=409,
                detail="Project unavailable to claim resident session",
            )
        reject_active_cold_successor(locked_project.vision)
        vision = dict(locked_project.vision or {})
        fresh = read_resident_session(vision)
        reply_recovery_hold = bool(
            isinstance(fresh, dict)
            and fresh.get("status") == STATUS_ENDED
            and fresh.get("worker_state") == "recovery_required"
        )
        if locked_project.archived:
            decision["mode"] = "project_archived"
        elif (
            locked_project.paused
            and not allow_paused_project
            and not resident_session_is_active(fresh)
        ):
            # A paused project still forbids creating a Resident. An already-active
            # Resident must reach the health/idempotency check below, though: the
            # operator-message preflight adopts that exact worker before inserting
            # the turn and must not mistake the no-new-work guard for a restart.
            decision["mode"] = "project_paused"
        if decision["mode"] is None and expected_interaction_mode is not None:
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
        if decision["mode"] is None and reply_recovery_hold:
            decision["mode"] = await _reply_protocol_recovery_start_mode(
                session,
                project.id,
                str(fresh.get("session_id") or ""),
                expected_interaction_mode,
            )
        if (
            decision["mode"] is None
            and resident_session_is_active(fresh)
            and fresh.get("task_id")
        ):
            # The task terminal commit and its post-commit reconciler are two
            # separate durable gates. Close that interval under the canonical
            # Project→Task locks so an ensure cannot supersede the exact task or
            # reset its still-held operator claim before recovery is materialized.
            try:
                recovery_task_id = uuid.UUID(str(fresh.get("task_id")))
                recovery_generation = int(fresh.get("worker_generation") or 1)
            except (TypeError, ValueError):
                recovery_task_id = None
                recovery_generation = 0
            if recovery_task_id is not None:
                recovery_identity = (
                    str(fresh.get("session_id")),
                    str(recovery_task_id),
                    recovery_generation,
                )
                async with task_lock(
                    session, recovery_task_id, skip_locked=False
                ) as recovery_task:
                    reply_recovery_hold = _apply_reply_protocol_recovery_hold(
                        vision,
                        recovery_task,
                        expected_identity=recovery_identity,
                        now_iso=now_iso,
                    )
                if reply_recovery_hold:
                    fresh = read_resident_session(vision)
                    await with_vision_locked(
                        session,
                        project.id,
                        lambda current_vision: _replace_vision(current_vision, vision),
                        _locked_project=locked_project,
                    )
                    decision["mode"] = await _reply_protocol_recovery_start_mode(
                        session,
                        project.id,
                        recovery_identity[0],
                        expected_interaction_mode,
                    )
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
            fresh_runner_id = str(fresh.get("runner_id") or "").strip()
            requested_runner_id = str(runner_id or "").strip()
            runner_mismatch = bool(
                requested_runner_id and fresh_runner_id != requested_runner_id
            )
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
                    if not runner_mismatch:
                        try:
                            existing_task_uuid = uuid.UUID(str(fresh_task_id))
                        except (TypeError, ValueError):
                            existing_task_uuid = None
                        if existing_task_uuid is not None:
                            # The Project lock remains held here. Its current
                            # session identity authorizes the nested Task lock
                            # and the exact, unstarted-only priority repair.
                            async with task_lock(
                                session, existing_task_uuid, skip_locked=False
                            ) as existing_task:
                                if _uplift_unstarted_resident_task_priority(
                                    existing_task,
                                    expected_project_id=locked_project.id,
                                    session_id=str(fresh.get("session_id")),
                                    worker_generation=fresh_generation,
                                ):
                                    session.add(existing_task)
                    decision["mode"] = (
                        "runner_mismatch" if runner_mismatch else "existing"
                    )
                else:
                    try:
                        fresh_task_uuid = uuid.UUID(str(fresh_task_id))
                    except (TypeError, ValueError):
                        fresh_task_uuid = None
                    if fresh_task_uuid is not None:
                        async with task_lock(
                            session, fresh_task_uuid, skip_locked=False
                        ) as locked_task:
                            # A pin the runner has not pulled yet is judged on
                            # its DEVICE's liveness, never on the start clock —
                            # resolve that here, under the same lock, so the
                            # verdict and the row it condemns agree.
                            pinned_runner_live = await _pinned_runner_is_live(
                                session, locked_task
                            )
                            health_verdict = _resident_worker_health_verdict(
                                locked_task,
                                fresh,
                                now,
                                pinned_runner_live=pinned_runner_live,
                            )
                            if health_verdict is None:
                                decision["mode"] = (
                                    "runner_mismatch"
                                    if runner_mismatch
                                    else "existing"
                                )
                            else:
                                stale_locked_task = locked_task
                                stale_locked_reason = health_verdict
                                carried_blacklisted_accounts = (
                                    _resident_blacklisted_accounts(
                                        dict(locked_task.context or {})
                                    )
                                )
                    if decision["mode"] is None:
                        decision["session_id"] = str(fresh.get("session_id"))
                        decision["worker_generation"] = fresh_generation + 1
            elif not _start_claim_is_stale(fresh, now):
                decision["mode"] = (
                    "runner_mismatch" if runner_mismatch else "existing"
                )
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

        if (
            decision["mode"] is None
            and locked_project.paused
            and not allow_paused_project
        ):
            # The active blob was stale enough to require a replacement. Preserve
            # Project.paused as the no-new-work authority instead of restarting it.
            decision["mode"] = "project_paused"

        if (
            decision["mode"] == "existing"
            and had_reason_at_start
            and fresh_identity == existing_identity_at_start
            and RESIDENT_SESSION_REASON_KEY in vision
            and vision.get(RESIDENT_SESSION_REASON_KEY) == reason_at_start
        ):
            # Compare-and-clear only the reason observed by this successful
            # idempotent start. A reason written or changed by a concurrent
            # blocked attempt remains authoritative.
            vision.pop(RESIDENT_SESSION_REASON_KEY, None)
            await with_vision_locked(
                session,
                project.id,
                lambda current: _replace_vision(current, vision),
                _locked_project=locked_project,
            )

        if decision["mode"] in {
            "existing",
            "recovery_superseded",
            "ensure_mode_changed",
            "reply_recovery_required",
            "reply_recovery_claim_held",
            "runner_mismatch",
            "project_archived",
            "project_paused",
        }:
            pass
        elif runner_block is not None:
            decision["mode"] = "blocked"
        else:
            session_id = decision.get("session_id") or new_session_id()
            worker_generation = int(decision.get("worker_generation") or 1)
            continuing_logical_session = bool(
                isinstance(fresh, dict)
                and str(fresh.get("session_id") or "") == session_id
            )
            persisted_conversation_floor = (
                resident_conversation_floor_sequence(fresh)
                if continuing_logical_session
                else None
            )
            conversation_floor_sequence = persisted_conversation_floor
            if conversation_floor_sequence is None:
                conversation_floor_sequence = (
                    await _next_resident_conversation_floor_sequence(
                        session,
                        project.id,
                    )
                )
            if (
                stale_locked_task is not None
                and stale_locked_task.status in _LIVE_TASK_STATUSES
            ):
                _mark_resident_task_superseded(
                    stale_locked_task,
                    now_iso=now_iso,
                    replacement_generation=worker_generation,
                    reason=(
                        stale_locked_reason
                        or RESIDENT_SUPERSEDE_WORKER_HEARTBEAT_STALE
                    ),
                    replacement_session_id=session_id,
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
                            reason=RESIDENT_SUPERSEDE_ORPHANED_IDENTITY,
                            replacement_session_id=session_id,
                        )
                        session.add(orphan_task)
                        superseded_task_ids.append(orphan_task.id)

            # Only after every live predecessor task has been terminalized under
            # Project -> Task locks may we sever and narrow its durable Fast turn.
            if not continuing_logical_session or persisted_conversation_floor is None:
                await _rebind_predecessor_conversation_handoff(
                    session,
                    project.id,
                    predecessor=fresh,
                    resident_session_id=session_id,
                )

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
                # Distinguish a session created by an incarnation-aware backend
                # from a pre-deploy live blob. A fresh session has no possible
                # legacy worker to protect, so its first random Agent may claim
                # immediately; only unmarked historical blobs receive grace.
                "worker_incarnation_protocol": (
                    _RESIDENT_WORKER_INCARNATION_PROTOCOL
                ),
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
                RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY: (
                    conversation_floor_sequence
                ),
                "activity": {
                    "phase": "waiting",
                    "phase_started_at": now_iso,
                    "last_action": "Resident session starting",
                    "pending_operator_messages": 0,
                    "next_review_at": None,
                    "pending_watches": 0,
                    "fired_watches": 0,
                },
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
    if decision["mode"] == "reply_recovery_required":
        raise _ResidentEnsureModeChanged(
            "Resident reply recovery requires an explicit operator Start"
        )
    if decision["mode"] == "reply_recovery_claim_held":
        raise HTTPException(
            status_code=409,
            detail=(
                "Resident reply recovery is waiting for the prior canonical "
                "conversation claim to be released"
            ),
        )
    if decision["mode"] == "recovery_superseded":
        raise _ResidentRecoverySuperseded(
            "Resident recovery identity was stopped, requeued, or superseded"
        )
    if decision["mode"] == "runner_mismatch":
        raise HTTPException(
            status_code=409,
            detail=(
                "Workday is already active on another device; stop the current "
                "session before changing devices"
            ),
        )
    if decision["mode"] == "existing":
        snapshot = await authoritative_resident_session_snapshot(session, project) or {}
        # A concurrent start owns (or is mid-starting) the session — return it.
        await _repair_stale_message_handoffs_after_ensure(session, project.id)
        return snapshot

    if decision["mode"] in {"project_archived", "project_paused"}:
        state = "archived" if decision["mode"] == "project_archived" else "paused"
        raise HTTPException(
            status_code=409,
            detail=f"Project is {state}; Resident start is blocked",
        )

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
                        task.launched_by_user_id = str(operator_user_id)
                        _stamp_resident_context(
                            task,
                            session_id=session_id,
                            runner_id=effective_runner_id,
                            project_autonomous_enabled=bool(
                                locked_project.autonomous_enabled
                            ),
                            worker_generation=worker_generation,
                            conversation_floor_sequence=(
                                conversation_floor_sequence
                            ),
                            open_messages=await _fetch_open_messages(
                                session,
                                project.id,
                                resident_session_id=session_id,
                                conversation_floor_sequence=(
                                    conversation_floor_sequence
                                ),
                            ),
                            open_message_cursor_floor=(
                                await _fetch_open_operator_cursor_floor(
                                    session,
                                    project.id,
                                    resident_session_id=session_id,
                                    conversation_floor_sequence=(
                                        conversation_floor_sequence
                                    ),
                                )
                            ),
                            exam_ledger=carried_exam_ledger,
                            message_cursor=carried_message_cursor,
                            session_summary=carried_session_summary,
                            decisions_register=carried_decisions_register,
                            blacklisted_accounts=carried_blacklisted_accounts,
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
                operator_user_id=str(operator_user_id),
                session_id=session_id,
                worker_generation=worker_generation,
                conversation_floor_sequence=conversation_floor_sequence,
                exam_ledger=carried_exam_ledger,
                message_cursor=carried_message_cursor,
                session_summary=carried_session_summary,
                decisions_register=carried_decisions_register,
                blacklisted_accounts=carried_blacklisted_accounts,
                allow_paused_project=allow_paused_project,
            )
            if task is None:
                from app.task_lifecycle import _has_active_strategic_task

                if await _has_active_strategic_task(
                    project.id,
                    session,
                    allow_paused_project=allow_paused_project,
                ):
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
            failure_reason=_short_error(exc),
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
            from app import strategic_cycle

            completion_event = await _reconcile_resident_terminal_task(
                fresh_session,
                task,
                source=(
                    "resident_worker_supersession"
                    if (task.context or {}).get("_resident_superseded") is True
                    else "resident_operator_stop"
                ),
            )
            if completion_event is not None:
                await fresh_session.commit()
                await strategic_cycle.emit_terminal_completion_events(
                    [completion_event]
                )
                await fresh_session.refresh(task)
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
                task,
                "",
                fresh_session,
                project=locked_project,
                preserve_terminal_cycle_disposition=completion_event is not None,
            )
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] _run_resident_completion_on_stop: completion processing failed for task %s",
            task_id,
            exc_info=True,
        )


async def _reconcile_resident_terminal_task(
    session: AsyncSession,
    task: Task,
    *,
    source: str,
) -> dict[str, Any] | None:
    """Reconcile a Resident cancellation and snapshot its post-commit receipt.

    The caller commits before emitting the returned event.
    """
    from app import strategic_cycle

    reconciled = await strategic_cycle.reconcile_terminal_task(
        session,
        task,
        source=source,
    )
    if reconciled is None:
        return None
    return strategic_cycle.terminal_completion_event(
        reconciled,
        task,
        source=source,
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
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(project.vision)
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
    ended_holder: dict[str, Any] = {"blob": None, "typed_fast_account_id": None}

    def _release_and_end(vision: dict[str, Any]) -> None:
        reject_active_cold_successor(vision)
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
        ended["activity"] = {
            **(
                ended.get("activity")
                if isinstance(ended.get("activity"), dict)
                else {}
            ),
            "phase": "ended",
            "phase_started_at": now_iso,
            "last_action": "Session stopped by operator",
            "next_review_at": None,
        }
        typed_fast = ended.get(RESIDENT_TYPED_FAST_KEY)
        if isinstance(typed_fast, dict) and typed_fast.get("status") == CALL_STATUS_ACTIVE:
            ended_holder["typed_fast_account_id"] = (
                str(typed_fast.get("account_id") or "") or None
            )
            typed_fast["status"] = CALL_STATUS_ENDED
            typed_fast["ended_at"] = now_iso
            typed_fast["updated_at"] = now_iso
            typed_fast["failure"] = "parent_stopped"
            typed_fast["slot_reserved"] = False
            ended[RESIDENT_TYPED_FAST_KEY] = typed_fast
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
                ctx[RESIDENT_INFRASTRUCTURE_CONTEXT_KEY] = True
                ctx[RESIDENT_LIFECYCLE_DISPOSITION_CONTEXT_KEY] = "operator_stop"
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

            # A handoff statement failure must not poison the outer stop
            # transaction. ``begin_nested`` flushes the already-staged
            # Project/Task end state before opening the SAVEPOINT; successful
            # message mutations still commit with that state, while a failed
            # handoff rolls back only its SAVEPOINT and leaves the stop
            # committable. The stale-session repair sweep can converge any
            # messages that remain attached to the ended generation.
            async with session.begin_nested():
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
    typed_fast_account_id = ended_holder["typed_fast_account_id"]
    if typed_fast_account_id:
        from app.max_plan_pool import release_max_plan_auxiliary_slot

        await release_max_plan_auxiliary_slot(
            typed_fast_account_id, project_id=str(project.id)
        )
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
            await _enqueue_resident_activation_turn(
                fresh,
                project,
                operator_user_id=operator_user_id,
                snapshot=snapshot,
            )
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


async def _bind_pending_operator_message_to_resident(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    message_id: uuid.UUID,
    resident_session_id: str,
    operator_user_id: str,
) -> bool:
    """Bind one exact unowned operator turn to the newly ensured Resident.

    This is the narrow fallback for a Workday message accepted after the prior
    Resident's lease ended. Normal predecessor handoff already binds the row in
    ``start_resident_session``'s transaction. The exact-row pass also covers a
    project with no parseable predecessor without pulling older NULL-session
    backlog across the new conversation floor.
    """

    resident_session_id = resident_session_id.strip()
    if not resident_session_id:
        return False
    # Lock the canonical mode before the Project (the same prefs -> Project
    # order used by mode updates). A committed or concurrently committing Chat
    # decision must win over an active-but-not-yet-stopped Resident blob.
    workbench = await get_strategic_workbench_state(
        session,
        operator_user_id,
        project_id,
        for_update=True,
        populate_existing=True,
    )
    if workbench.get("interaction_mode") != "continuous":
        return False
    # Match the canonical Project -> message lock order used by message create
    # and session stop. The earlier ensure snapshot is only advisory: a Stop or
    # replacement can commit before this fallback transaction reaches the row.
    locked_project = await session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_project is None:
        return False
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(locked_project.vision)
    current = read_resident_session(locked_project.vision)
    if (
        not resident_session_is_active(current)
        or str((current or {}).get("session_id") or "") != resident_session_id
    ):
        return False
    row = await session.scalar(
        select(ProjectMessage)
        .where(
            ProjectMessage.id == message_id,
            ProjectMessage.project_id == project_id,
            ProjectMessage.from_type == "user",
            ProjectMessage.surface == MessageSurface.CONVERSATION,
            ProjectMessage.status == "open",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return False
    if row.resident_session_id not in (None, resident_session_id):
        return False
    if (
        row.handling_state != MessageHandlingState.PENDING
        or row.handled_by_task_id is not None
        or row.conversation_claim_id is not None
        or row.resolution
    ):
        return row.resident_session_id == resident_session_id

    from app.conversation_coordinator import (
        reset_conversation_claim_for_session_handoff,
    )
    from app.project_message_projection import WAKEUP_STATE_CONTEXT_KEY

    row.resident_session_id = resident_session_id
    row.handling_state = MessageHandlingState.PENDING
    row.handled_by_task_id = None
    reset_conversation_claim_for_session_handoff(row)
    row.context = {
        **dict(row.context or {}),
        WAKEUP_STATE_CONTEXT_KEY: "queued",
    }
    row.updated_at = _utcnow()
    session.add(row)
    await session.commit()
    return True


async def ensure_resident_session_for_operator_message(
    project_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    operator_user_id: str,
    session_factory=None,
) -> dict[str, Any] | None:
    """Rearm Workday after an operator message crosses an idle-lease boundary.

    The message is already committed before this runs, so a runner outage can
    never lose operator input. Manual Stop is respected because it atomically
    persists Chat mode; only a still-selected Workday (continuous) session may
    restart. The start path is project-locked/idempotent, so duplicate or
    concurrent sends converge on one Resident task.
    """

    factory = session_factory or AsyncSessionLocal
    try:
        async with factory() as inspect_session:
            message = await inspect_session.get(ProjectMessage, message_id)
            if (
                message is None
                or message.project_id != project_id
                or message.from_type != "user"
                or message.surface != MessageSurface.CONVERSATION
                or message.status != "open"
                or message.handling_state != MessageHandlingState.PENDING
                or message.resolution
            ):
                return None
        snapshot = await ensure_resident_session_before_operator_message(
            project_id,
            operator_user_id=operator_user_id,
            session_factory=factory,
        )
        if snapshot is None or snapshot.get("status") != STATUS_ACTIVE:
            return snapshot
        async with factory() as bind_session:
            bound = await _bind_pending_operator_message_to_resident(
                bind_session,
                project_id,
                message_id=message_id,
                resident_session_id=str(snapshot.get("session_id") or ""),
                operator_user_id=operator_user_id,
            )
        return snapshot if bound else None
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] post-message Workday ensure failed for project "
            "%s message %s; durable input remains pending",
            project_id,
            message_id,
            exc_info=True,
        )
        return None


async def ensure_resident_session_before_operator_message(
    project_id: uuid.UUID,
    *,
    operator_user_id: str,
    session_factory=None,
) -> dict[str, Any] | None:
    """Ensure the Workday owner before a new non-idempotent send is inserted.

    The caller invokes this only after proving an idempotency key is new. A
    stored Chat mode is a deliberate Stop and returns without side effects. In
    Workday mode, the persisted runner preference (falling back to the ended
    session's pin) is authoritative; an unavailable preferred device fails
    visibly instead of silently migrating the conversation or cloud-arming a
    one-shot strategic task.
    """

    factory = session_factory or AsyncSessionLocal
    async with factory() as fresh:
        project = await fresh.get(Project, project_id)
        if project is None:
            return None
        workbench = await get_strategic_workbench_state(
            fresh,
            operator_user_id,
            project_id,
        )
        if workbench.get("interaction_mode") != "continuous":
            return None
        current = read_resident_session(project.vision)
        runner_id = str(
            workbench.get("preferred_runner_id")
            or ((current or {}).get("runner_id") if isinstance(current, dict) else "")
            or ""
        ).strip() or None
        try:
            snapshot = await start_resident_session(
                fresh,
                project,
                operator_user_id=operator_user_id,
                runner_id=runner_id,
                session_factory=factory,
                expected_interaction_mode="continuous",
            )
        except _ResidentEnsureModeChanged:
            # Explicit Stop won the race and atomically persisted Chat mode.
            # Let the final message create linearize in Chat without a 500.
            return None
        await set_resident_session_blocked_reason(fresh, project, None)
        # No synthetic activation is enqueued here: the real message that
        # immediately follows this ensure is the exact activation turn.
        return snapshot


async def _enqueue_resident_activation_turn(
    session: AsyncSession,
    project: Project,
    *,
    operator_user_id: str,
    snapshot: dict[str, Any],
) -> None:
    """Best-effort enqueue of the model-generated Workday opening turn.

    This intentionally uses the canonical operator-message creation path: Fast
    sees, claims, and completes the turn exactly like a typed operator message,
    including its normal memory/context envelope and public-reply fence. The
    per-session idempotency key makes retries/adoption safe. Failure here cannot
    undo a successfully started Workday session.
    """

    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id or snapshot.get("status") != STATUS_ACTIVE:
        return
    project_id = project.id
    try:
        # Local import avoids the project_message_creation -> resident-session
        # association import cycle at module load time.
        from app.project_message_creation import (  # noqa: PLC0415
            create_operator_project_message,
        )
        from app.strategic_conversation_query import (  # noqa: PLC0415
            strategic_conversation_predicate,
        )

        async def suppress_when_real_input_exists(
            locked_session: AsyncSession,
            _message_id: uuid.UUID,
            message_context: dict[str, Any],
        ) -> dict[str, Any]:
            # create_operator_project_message invokes this after locking the
            # Project row. Every operator-message creator takes that same lock,
            # so checking and inserting the synthetic activation are atomic.
            # A recovered predecessor turn is the actual reason the new
            # session woke; Fast must claim it alone instead of co-batching a
            # made-up startup request into the same public answer.
            real_input_id = await locked_session.scalar(
                select(ProjectMessage.id)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.resident_session_id == session_id,
                    ProjectMessage.status == "open",
                    ProjectMessage.from_type == "user",
                    strategic_conversation_predicate(),
                    func.coalesce(
                        ProjectMessage.context[
                            RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY
                        ].as_boolean(),
                        False,
                    ).is_(False),
                )
                .limit(1)
            )
            if real_input_id is not None:
                raise _ResidentActivationSuppressed
            return message_context

        await create_operator_project_message(
            session,
            project=project,
            user_id=operator_user_id,
            message_type=MessageType.NOTE,
            content=_RESIDENT_ACTIVATION_PROMPT,
            context=None,
            server_context={RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY: True},
            workbench_context=None,
            idempotency_key=f"resident-activation:{session_id}",
            surface=MessageSurface.CONVERSATION,
            include_strategic_session_snapshot=False,
            precommit_validate=suppress_when_real_input_exists,
        )
    except _ResidentActivationSuppressed:
        # The precommit callback may run after harmless arming bookkeeping;
        # roll the whole attempted synthetic message transaction back. The
        # real current-session row remains durably bound and ready for Fast.
        await session.rollback()
        return
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        logger.warning(
            "[RESIDENT_SESSION] Workday started but activation turn enqueue "
            "failed for project %s session %s",
            project_id,
            session_id,
            exc_info=True,
        )


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
            from app.strategic_cold_successor_authority import (
                has_exact_cold_successor_markers,
            )

            if has_exact_cold_successor_markers(task):
                from app.strategic_cold_successor_lifecycle import (
                    reconcile_cold_successor_terminal,
                )

                return await reconcile_cold_successor_terminal(session, task_uuid)
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
            task_failure_class = resolved_failure_class(task).strip().upper()
            if task_failure_class == RESIDENT_REPLY_PROTOCOL_FAILURE:
                handled = False
                async with project_lock(session, project_id, skip_locked=False) as locked:
                    if locked is None:
                        return False
                    vision = dict(locked.vision or {})
                    async with task_lock(session, task_uuid, skip_locked=False) as locked_task:
                        handled = _apply_reply_protocol_recovery_hold(
                            vision,
                            locked_task,
                            expected_identity=expected_identity,
                            now_iso=_utcnow().isoformat(),
                        )
                        if not handled:
                            return False
                    await with_vision_locked(
                        session,
                        project_id,
                        lambda current_vision: _replace_vision(current_vision, vision),
                        _locked_project=locked,
                    )
                if handled:
                    await session.commit()
                    logger.warning(
                        "[RESIDENT_SESSION] terminal worker %s requires explicit "
                        "reply-protocol recovery for project %s; automatic restart suppressed",
                        task_uuid,
                        project_id,
                    )
                return handled
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
    incarnation_id: uuid.UUID | None = None,
    worker_generation: int = 1,
    progress: bool = False,
    autonomous_turn: bool = False,
    note: str | None = None,
    message_cursor: str | None = None,
    handled_message_ids: list[str] | None = None,
    turn_latency_summary: dict | None = None,
    operator_decision_summary: dict | None = None,
    compaction_count: int | None = None,
    last_compaction_at: str | None = None,
    decisions_reg: str | None = None,
    activity: dict[str, Any] | None = None,
    derive_consultation_status: bool = False,
    repo_head: str | None = None,
) -> dict[str, Any]:
    """Renew the continuation lease from a resident-agent heartbeat.

    An exact session/task/generation/incarnation heartbeat renews Continuous
    authority after locking the server-owned workbench mode. Operator silence is
    not terminal: a healthy quiet process may remain resident indefinitely. Chat
    or Stop, a newer process incarnation, and a replacement session fence the
    renewal. Another live strategic task pauses mutation authority without
    ending the Resident. A dead process emits no exact heartbeat, so ordinary
    process-heartbeat reaping still releases it.

    Returns the lease view the agent loop steers on:
    ``{authority_held, leased_until, stop_requested, status}``.
    """
    normalized_repo_head = (
        repo_head.lower()
        if isinstance(repo_head, str)
        and re.fullmatch(r"[0-9a-fA-F]{40}", repo_head)
        else None
    )
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

    # READ-ONLY work-state signal for the agent's state-gated pacing. Computed
    # once, up front, from cheap aggregate queries; additive to every return
    # branch below. It NEVER touches lease/freshness/session state, so it cannot
    # extend the dead-man's switch (reaper uses only last_heartbeat; this path
    # is not consulted by _classify_resident_session). Older agents ignore the
    # extra fields.
    work_state_rev, work_state_summary = await _compute_resident_work_state(
        session,
        project.id,
        resident_session_id=session_id,
        conversation_floor_sequence=resident_conversation_floor_sequence(blob),
        resident_task_id=uuid.UUID(str(task_id)),
    )
    fired_watch_receipts: list[dict[str, Any]] = []
    fired_barrier_receipts = await _resident_wake_barrier_receipts(
        session,
        project.id,
    )
    handled_message_id_set = {
        value
        for value in (handled_message_ids or [])
        if isinstance(value, str) and value
    }
    if handled_message_id_set:
        fired_barrier_receipts = [
            receipt
            for receipt in fired_barrier_receipts
            if receipt["id"] not in handled_message_id_set
        ]
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
            "project_autonomous_enabled": bool(project.autonomous_enabled),
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": status,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "fired_watches": fired_watch_receipts,
            "fired_barriers": fired_barrier_receipts,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }

    # Canonical ordering for every mode writer is prefs -> Project. Holding this
    # row lock through commit gives Chat/Stop and heartbeat one linearization:
    # heartbeat-first may renew once, while a committed Chat preference can never
    # be overwritten or rearmed by the old Resident.
    server_workbench = await get_strategic_workbench_state(
        session,
        str(op_id or ""),
        project.id,
        for_update=True,
        populate_existing=True,
    )
    other_strategic_active = False

    holder = {
        "authority_held": False,
        "effective_authority_held": False,
        "incarnation_held": False,
        "legacy_grace_until": None,
        "lease_now": None,
        "stopped": False,
        "mode_left": False,
        "typed_fast_account_id": None,
        # Set in-lock when this heartbeat crosses into chronically-low operator-
        # decision capture and no prior notice was posted; drives the single
        # strategist-internal emit after the lease commits.
        "emit_low_capture_notice": False,
        # Set in-lock when operator_decision_summary has been absent for longer
        # than _OPERATOR_DECISION_SUMMARY_STALE_SECONDS; drives a distinct
        # strategist-internal notice after the lease commits.
        "emit_absent_notice": False,
    }
    activity_for_update = activity
    consultation_verified_for_update = False

    def _end_for_mode_change(
        vision: dict[str, Any], current: dict[str, Any]
    ) -> None:
        """Atomically end OUR accepted incarnation after Workday leaves Continuous."""
        ended_now = holder.get("lease_now") or now
        ended_now_iso = ended_now.isoformat()
        ended = dict(current)
        ended["status"] = STATUS_ENDED
        ended["stop_requested"] = True
        ended["ended_at"] = ended_now_iso
        ended["updated_at"] = ended_now_iso
        ended["activity"] = {
            **(
                ended.get("activity")
                if isinstance(ended.get("activity"), dict)
                else {}
            ),
            "phase": "ended",
            "phase_started_at": ended_now_iso,
            "last_action": "Session stopped after Workday left Continuous mode",
            "next_review_at": None,
        }
        typed_fast = ended.get(RESIDENT_TYPED_FAST_KEY)
        if isinstance(typed_fast, dict) and typed_fast.get("status") == CALL_STATUS_ACTIVE:
            holder["typed_fast_account_id"] = (
                str(typed_fast.get("account_id") or "") or None
            )
            typed_fast = dict(typed_fast)
            typed_fast.update(
                status=CALL_STATUS_ENDED,
                ended_at=ended_now_iso,
                updated_at=ended_now_iso,
                failure="parent_stopped",
                slot_reserved=False,
            )
            ended[RESIDENT_TYPED_FAST_KEY] = typed_fast
        vision[RESIDENT_SESSION_KEY] = ended
        vision.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)

    def _renew(vision: dict[str, Any]) -> None:
        # Sample after acquiring the Project lock. Pre-lock reads can block
        # longer than the remaining incarnation lease.
        lease_now = _utcnow()
        lease_now_iso = lease_now.isoformat()
        holder["lease_now"] = lease_now
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
        accepted_incarnation = claim_resident_worker_incarnation(
            vision,
            task_id=task_id,
            worker_generation=worker_generation,
            incarnation_id=incarnation_id,
            now=lease_now,
        )
        if accepted_incarnation is None:
            current_after_claim = read_resident_session(vision) or {}
            legacy_incarnation = legacy_resident_worker_incarnation(
                task_id,
                worker_generation,
            )
            legacy_expiry = _parse_iso_naive(
                current_after_claim.get("worker_incarnation_expires_at")
            )
            if (
                incarnation_id is not None
                and incarnation_id != legacy_incarnation
                and str(current_after_claim.get("worker_incarnation_id"))
                == str(legacy_incarnation)
                and legacy_expiry is not None
                and legacy_expiry > lease_now
            ):
                holder["legacy_grace_until"] = legacy_expiry
                return
            holder["stopped"] = True
            return
        holder["incarnation_held"] = True
        # The CAS replaces the nested blob; continue from that fenced copy so
        # the ordinary heartbeat update cannot overwrite the new process lease.
        fresh = read_resident_session(vision)
        if not isinstance(fresh, dict):
            holder["stopped"] = True
            return
        # The accepted process incarnation may renew only while the canonical,
        # server-owned preference remains Continuous. The prefs row is already
        # locked before Project, so a committed Chat decision wins atomically;
        # a stale competing incarnation never reaches this terminal mutation.
        if server_workbench.get("interaction_mode") != "continuous":
            holder["mode_left"] = True
            _end_for_mode_change(vision, fresh)
            return

        first_agent_heartbeat = not fresh.get("first_agent_heartbeat_at")
        renew_freshness = bool(progress)
        autonomous_renewal = bool(autonomous_turn) and not progress
        heartbeat_workbench: dict[str, Any] = {
            **server_workbench,
            "interaction_mode": "continuous",
            "session_started_at": (
                server_workbench.get("session_started_at") or fresh.get("started_at")
            ),
        }
        if runner_id:
            heartbeat_workbench["preferred_runner_id"] = runner_id
        apply_strategic_local_continuation_marker(
            vision,
            heartbeat_workbench,
            operator_user_id=str(op_id or ""),
            preferred_runner_id=str(runner_id) if runner_id else None,
            renewal_reason=(
                "strategic_progress"
                if renew_freshness
                else "autonomous_turn"
                if autonomous_renewal
                else "first_agent_heartbeat"
                if first_agent_heartbeat
                else "continuous_liveness"
            ),
        )
        post = strategic_local_continuation_status_from_vision(
            vision, operator_user_id=op_id
        )
        marker_owner_started = post.get("owner_session_started_at")
        expected_owner_started = heartbeat_workbench.get("session_started_at")
        holder["authority_held"] = (
            bool(post.get("fresh"))
            and bool(post.get("owned_by_current_operator"))
            and (
                not marker_owner_started
                or marker_owner_started == expected_owner_started
            )
        )
        holder["effective_authority_held"] = bool(holder["authority_held"]) and not (
            other_strategic_active
        )

        updated = dict(fresh)
        updated["updated_at"] = lease_now_iso
        updated["worker_state"] = "ready"
        updated.pop("worker_retry_until", None)
        updated.pop("worker_retry_reason", None)
        updated["worker_heartbeat_at"] = lease_now_iso
        if first_agent_heartbeat:
            updated["first_agent_heartbeat_at"] = lease_now_iso
        if progress:
            updated["last_progress_at"] = lease_now_iso
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
        if normalized_repo_head is not None:
            updated["_resident_repo_head"] = normalized_repo_head
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
            updated["_operator_decision_summary_at"] = lease_now_iso
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
                and (lease_now - last_summary_at).total_seconds()
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
        updated["activity"] = _normalized_activity_update(
            activity_for_update,
            current=(
                fresh.get("activity")
                if isinstance(fresh.get("activity"), dict)
                else None
            ),
            now_iso=lease_now_iso,
            pending_operator_messages=int(
                work_state_summary.get("new_operator_messages") or 0
            ),
            pending_watches=int(
                work_state_summary.get("pending_watches") or 0
            ),
            fired_watches=int(work_state_summary.get("fired_watches") or 0),
        )
        # The request body necessarily reflects the Agent's state *before* it
        # receives this heartbeat response. Apply server-owned authority/call
        # fences in the same locked vision write so Fast cannot claim in the
        # commit-to-next-heartbeat gap while the persisted activity still says
        # autonomous Workday is actionable.
        updated["activity"] = resident_activity_with_server_fences(
            updated["activity"],
            authority_held=holder["effective_authority_held"],
            foreground_call_active=resident_call_is_active(
                fresh.get(RESIDENT_CALL_KEY), now=lease_now
            ),
        )
        updated["activity"]["consultation_verified"] = bool(
            consultation_verified_for_update
            and updated["activity"].get("consultation_id")
        )
        vision[RESIDENT_SESSION_KEY] = updated

    mode_cancelled_task_id: uuid.UUID | None = None
    async with project_lock(session, project.id, skip_locked=False) as locked_project:
        if locked_project is None:
            holder["stopped"] = True
        else:
            if derive_consultation_status:
                activity_for_update, consultation_verified_for_update = (
                    await canonical_consultation_activity_update(
                        session, project_id=project.id,
                        resident_session_id=session_id, activity=activity,
                    )
                )
            # Conversation availability is independent from mutation authority.
            # Sample competitors under Project serialization so a batch cycle
            # pauses this heartbeat without terminating the Resident.
            other_strategic_rows = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.project_id == project.id,
                            Task.id != uuid.UUID(str(task_id)),
                            Task.status.in_(list(_LIVE_TASK_STATUSES)),
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
            await with_vision_locked(
                session,
                project.id,
                _renew,
                _locked_project=locked_project,
            )
            if holder["mode_left"]:
                try:
                    resident_task_uuid = uuid.UUID(str(task_id))
                except (TypeError, ValueError):
                    resident_task_uuid = None
                if resident_task_uuid is not None:
                    async with task_lock(
                        session, resident_task_uuid, skip_locked=False
                    ) as locked_task:
                        if (
                            locked_task is not None
                            and locked_task.project_id == locked_project.id
                            and locked_task.status in _LIVE_TASK_STATUSES
                            and _task_matches_resident_identity(
                                locked_task,
                                session_id=session_id,
                                worker_generation=worker_generation,
                            )
                        ):
                            locked_task.status = TaskStatus.CANCELLED
                            locked_task.latest_status_message = (
                                "Resident strategic session stopped after Workday "
                                "left Continuous mode"
                            )
                            locked_task.context = {
                                **(locked_task.context or {}),
                                "failure_category": ADMIN_CANCEL,
                                RESIDENT_INFRASTRUCTURE_CONTEXT_KEY: True,
                                RESIDENT_LIFECYCLE_DISPOSITION_CONTEXT_KEY: (
                                    "workday_mode_left"
                                ),
                            }
                            session.add(locked_task)
                            mode_cancelled_task_id = locked_task.id
    if holder["legacy_grace_until"] is not None:
        legacy_grace_until = holder["legacy_grace_until"]
        lease_now = holder["lease_now"] or _utcnow()
        await session.commit()
        await session.refresh(project)
        refreshed = read_resident_session(project.vision) or {}
        return {
            "authority_held": False,
            "project_autonomous_enabled": bool(project.autonomous_enabled),
            "resident_incarnation_held": False,
            "resident_incarnation_id": str(incarnation_id),
            "admission_status": "legacy_grace",
            "retry_after_seconds": max(
                1,
                int((legacy_grace_until - lease_now).total_seconds()) + 1,
            ),
            "leased_until": legacy_grace_until.isoformat(),
            "stop_requested": False,
            "status": refreshed.get("status") or STATUS_ACTIVE,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "fired_watches": fired_watch_receipts,
            "fired_barriers": fired_barrier_receipts,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
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
            "project_autonomous_enabled": bool(project.autonomous_enabled),
            "resident_incarnation_held": False,
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": refreshed.get("status") or STATUS_ENDED,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "fired_watches": fired_watch_receipts,
            "fired_barriers": fired_barrier_receipts,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
    if holder["mode_left"]:
        # Release any claimed conversation turn in the SAME transaction as the
        # ended blob/task. A committed Chat preference therefore cannot strand a
        # turn even when the mode endpoint's best-effort stop never ran.
        mode_rearm: tuple | None = None
        try:
            from app.session_message_handoff import _apply_handoff_mutations

            # Isolate DB failures from the outer mode-end transaction. Without
            # a SAVEPOINT, catching a PostgreSQL statement error still leaves
            # the session rollback-only, so the commit below would discard the
            # ended blob and task cancellation despite the best-effort contract.
            async with session.begin_nested():
                _, mode_rearm = await _apply_handoff_mutations(
                    session, project.id, session_id
                )
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] mode-change handoff staging raised for session %s — stop still committed",
                session_id,
                exc_info=True,
            )
        await session.commit()
        await session.refresh(project)
        typed_fast_account_id = holder["typed_fast_account_id"]
        if typed_fast_account_id:
            from app.max_plan_pool import release_max_plan_auxiliary_slot

            await release_max_plan_auxiliary_slot(
                typed_fast_account_id, project_id=str(project.id)
            )
        if mode_rearm is not None:
            try:
                from app.session_message_handoff import _run_handoff_rearm

                await _run_handoff_rearm(session, project.id, session_id, mode_rearm)
            except Exception:
                logger.warning(
                    "[RESIDENT_SESSION] mode-change handoff rearm raised for session %s — stop still committed",
                    session_id,
                    exc_info=True,
                )
        if mode_cancelled_task_id is not None:
            try:
                await _run_resident_completion_on_stop(mode_cancelled_task_id)
            except Exception:
                logger.warning(
                    "[RESIDENT_SESSION] mode-change completion raised for task %s — stop still committed",
                    mode_cancelled_task_id,
                    exc_info=True,
                )
        cont = strategic_local_continuation_status_from_vision(
            project.vision, operator_user_id=op_id
        )
        refreshed = read_resident_session(project.vision) or {}
        return {
            "authority_held": False,
            "project_autonomous_enabled": bool(project.autonomous_enabled),
            "resident_incarnation_held": holder["incarnation_held"],
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": refreshed.get("status") or STATUS_ENDED,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "fired_watches": fired_watch_receipts,
            "fired_barriers": fired_barrier_receipts,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
    # Resolve exact task watches while this heartbeat owns the project mutation
    # boundary. The agent separately fingerprints these bounded receipts: that
    # preserves WHICH completion Strategic awaited and also lets a watch that
    # fires during this heartbeat wake exactly once without folding routine task
    # ``updated_at`` writes back into work_state_rev.
    try:
        from app.strategic_spawn_requests import drain_deferred_spawn_requests
        from app.strategic_watches import (
            resolve_project_task_watches,
            serialize_watch,
        )

        await drain_deferred_spawn_requests(
            session,
            project_id=project.id,
            burst_cap=int(runtime_config.get("strategist_spawn_window_cap", 100)),
            burst_window_minutes=int(
                runtime_config.get("strategist_spawn_window_minutes", 60)
            ),
        )
        _visible_watches = await resolve_project_task_watches(
            session,
            project_id=project.id,
            now=now,
        )
        fired_watch_receipts = [serialize_watch(row) for row in _visible_watches]
        work_state_summary = {
            **work_state_summary,
            "fired_watches": len(fired_watch_receipts),
        }
    except Exception:
        logger.warning(
            "[RESIDENT_SESSION] strategic watch resolution failed for session %s — ignored",
            session_id,
            exc_info=True,
        )

    # Mirror the exam ledger and message cursor into task.context so a runner
    # restart can read them back on startup (the agent reads task.context at pull
    # time; project.vision is not visible to the agent).
    _need_ctx_update = (
        exam_ledger_str is not None
        or bool(message_cursor)
        or decisions_reg is not None
        or normalized_repo_head is not None
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
                if normalized_repo_head is not None:
                    ctx["_resident_repo_head"] = normalized_repo_head

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
    # Mark consumed operator messages and exact project-scoped wake barriers as
    # handled. Ordinary NULL-session rows remain untouchable; only server-stamped
    # strategist-internal barriers may be acknowledged by an owning Resident.
    handled_state_changed = False
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
                    handled_result = await session.execute(
                        sa_update(ProjectMessage)
                        .where(
                            ProjectMessage.project_id == project.id,
                            ProjectMessage.id.in_(safe_ids),
                            or_(
                                ProjectMessage.resident_session_id == session_id,
                                and_(
                                    ProjectMessage.resident_session_id.is_(None),
                                    _resident_wake_barrier_contract(),
                                ),
                            ),
                            ProjectMessage.status == "open",
                        )
                        .values(status="handled")
                    )
                    handled_state_changed = bool(handled_result.rowcount)
        except Exception:
            logger.warning(
                "[RESIDENT_SESSION] handled_message_ids update failed for session %s — ignored",
                session_id,
                exc_info=True,
            )
    if handled_state_changed:
        # Return the post-ack baseline. Otherwise the next heartbeat can observe
        # the Resident's own acknowledgement as a fresh state transition and
        # schedule a redundant model turn.
        await session.flush()
        work_state_rev, work_state_summary = await _compute_resident_work_state(
            session,
            project.id,
            resident_session_id=session_id,
            conversation_floor_sequence=resident_conversation_floor_sequence(
                read_resident_session(project.vision)
            ),
            resident_task_id=uuid.UUID(str(task_id)),
        )
        fired_barrier_receipts = await _resident_wake_barrier_receipts(
            session,
            project.id,
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
            "project_autonomous_enabled": bool(project.autonomous_enabled),
            "leased_until": cont.get("expires_at"),
            "stop_requested": True,
            "status": refreshed.get("status") or STATUS_ENDED,
            "work_state_rev": work_state_rev,
            "work_state_summary": work_state_summary,
            "fired_watches": fired_watch_receipts,
            "fired_barriers": fired_barrier_receipts,
            "exam_ledger": None,
            "active_direction_ids": active_direction_ids,
            "direction_grounding": direction_grounding,
        }
    return {
        "authority_held": bool(cont.get("fresh"))
        and not refreshed_stop
        and holder["effective_authority_held"],
        "project_autonomous_enabled": bool(project.autonomous_enabled),
        "resident_incarnation_held": holder["incarnation_held"],
        "resident_incarnation_id": str(
            incarnation_id
            or legacy_resident_worker_incarnation(task_id, worker_generation)
        ),
        "leased_until": cont.get("expires_at"),
        "stop_requested": refreshed_stop,
        "status": refreshed.get("status") or STATUS_ACTIVE,
        "work_state_rev": work_state_rev,
        "work_state_summary": work_state_summary,
        "fired_watches": fired_watch_receipts,
        "fired_barriers": fired_barrier_receipts,
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
    strategic state — problem model, memory notes, and running update — so the
    compaction checkpoint carries current data rather than the session-start
    bootstrap snapshot, which would be stale after mid-session mutations
    (set-problem-model, set-memory, set-running-update).
    """
    project = await _resolve_agent_project(project_id, token, session)
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(project.vision)
    resident = read_resident_session(project.vision)
    if not resident_session_is_active(resident):
        raise HTTPException(status_code=409, detail="No active resident session for checkpoint")
    token_task_id = str(token.get("sub") or "")
    if not token_task_id or str(resident.get("task_id") or "") != token_task_id:
        raise HTTPException(status_code=403, detail="Token is not the current resident task")
    resident_session_id = str(resident.get("session_id") or "")
    if not resident_session_id:
        raise HTTPException(status_code=409, detail="Resident session identity is incomplete")
    open_messages = await _fetch_open_messages(
        session,
        project.id,
        resident_session_id=resident_session_id,
        conversation_floor_sequence=resident_conversation_floor_sequence(resident),
    )
    # Recently-answered operator conversation tail (status handled/resolved) so a
    # marker-less correction the operator just made survives the compaction
    # restart instead of vanishing with the open feed. Read-only, non-actionable.
    recent_handled_conversation = await _fetch_recent_handled_conversation(
        session, project.id
    )
    # Apply the SAME agent-view trims the session bootstrap applies so the
    # refreshed compaction grounding matches the shape the session started from.
    # The bootstrap ships _agent_view_problem_model(project.problem_model) and
    # effective_purpose; returning the raw project.problem_model / project.vision
    # here would make a successful refresh re-bloat the checkpoint with
    # archived_gaps, rejected_agent_edits, unwindowed per-gap memory, and the
    # internal vision continuation blob — the opposite of the coherence the
    # refresh preserves.
    from app.problem_model import resolve_project_purpose
    from app.release_qualifier import current_effective_policy_projection
    from app.strategic_problem_view import _agent_view_problem_model

    return {
        "problem_model": _agent_view_problem_model(project.problem_model),
        "strategic_memory_notes": await build_strategic_memory_index(session, project.id),
        "running_update": project.running_update or "",
        # The bootstrap ships effective_purpose; carry it onto the refreshed
        # compaction grounding so a post-compaction session keeps the same
        # project-purpose grounding it started with instead of dropping it.
        "effective_purpose": resolve_project_purpose(project),
        # Refresh volatile release truth at the same boundary as the other
        # compaction grounding; never re-prime a long-lived Resident with the
        # policy snapshot from session start after the live policy changed.
        "current_effective_policy": current_effective_policy_projection(project),
        "open_messages": open_messages,
        "recent_handled_conversation": recent_handled_conversation,
    }


class ResidentSessionHeartbeatRequest(BaseModel):
    session_id: str
    task_id: str
    progress: bool | None = None
    # Autonomous examination progress. Set by the agent's autonomous turn-loop
    # when a completed working turn (tool calls / TICK-VERDICT — not an
    # interrupt/timeout/dead subprocess) finished WITHOUT applying a strategic
    # mutation. It renews continuation freshness for an unattended autonomous
    # review, whose healthy steady state is "examined, found CLEAR, no mutation".
    # A chat/monitor session never runs these turns, so it never sets this flag
    # and the operator-chat mutation/operator freshness gate is unchanged.
    autonomous_turn: bool | None = None
    worker_generation: int = 1
    resident_incarnation_id: uuid.UUID | None = None
    note: str | None = None
    message_cursor: str | None = None
    handled_message_ids: list[str] | None = None
    turn_latency_summary: dict | None = None
    operator_decision_summary: dict | None = None
    compaction_count: int | None = None
    last_compaction_at: str | None = None
    decisions_reg: str | None = None
    activity: dict[str, Any] | None = None
    repo_head: str | None = None
    call_id: str | None = None
    call_generation: int | None = None
    call_ttl_seconds: int | None = None
    call_ready: bool = False
    call_usage: dict[str, Any] | None = None
    typed_fast_lease_id: str | None = None
    typed_fast_generation: int | None = None
    typed_fast_ttl_seconds: int | None = None
    conversation_claim_id: uuid.UUID | None = None
    conversation_claim_incarnation: uuid.UUID | None = None
    conversation_claim_lease_seconds: int = Field(default=45, ge=15, le=300)


class ResidentCallSpeechCredentialsRequest(BaseModel):
    """One exact active-call fence for Coordinator-only speech credentials."""

    task_id: str
    resident_incarnation_id: uuid.UUID | None = None
    call_id: str
    call_generation: int


class ResidentTypedFastLeaseRequest(BaseModel):
    task_id: str
    worker_generation: int = Field(default=1, ge=1)
    resident_incarnation_id: uuid.UUID | None = None
    lease_id: str | None = Field(default=None, min_length=1, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    reason: str = Field(default="ended", max_length=128)


@router.post("/projects/{project_id}/strategic-session/typed-fast-lease/acquire")
@limiter.limit("30/minute")
async def acquire_agent_typed_fast_lease(
    request: Request,
    project_id: uuid.UUID,
    payload: ResidentTypedFastLeaseRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Reserve and fence Typed Fast before the agent may launch its model child."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    project = await _resolve_agent_project(project_id, token, session)
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(project.vision)
    task = await session.get(Task, uuid.UUID(payload.task_id))
    if task is None or (task.context or {}).get("_resident_session") is not True:
        raise HTTPException(status_code=409, detail="Typed Fast requires a Resident task")
    from app.max_plan_pool import (
        get_task_llm_account_id,
        release_max_plan_auxiliary_slot,
        reserve_max_plan_auxiliary_slot,
    )

    account_id = get_task_llm_account_id(task)
    if not account_id:
        return {"acquired": False, "reason": "no_pooled_account"}
    existing = read_resident_typed_fast(project.vision)
    if isinstance(existing, dict) and existing.get("status") == CALL_STATUS_ACTIVE and not resident_typed_fast_is_active(existing):
        _lease, stale_account_id, changed = await release_resident_typed_fast_lease(
            session,
            project,
            lease_id=str(existing.get("lease_id") or ""),
            generation=int(existing.get("generation") or 0),
            reason="expired",
        )
        if changed and stale_account_id:
            await release_max_plan_auxiliary_slot(stale_account_id, project_id=str(project.id))
        existing = read_resident_typed_fast(project.vision)
    reserved_here = False
    if not resident_typed_fast_is_active(existing):
        reserved_here = await reserve_max_plan_auxiliary_slot(account_id, project_id=str(project.id))
        if not reserved_here:
            return {"acquired": False, "reason": "no_concurrent_capacity"}
    try:
        lease, expired_account_id, created = await acquire_resident_typed_fast_lease(
            session,
            project,
            task_id=payload.task_id,
            worker_generation=payload.worker_generation,
            incarnation_id=payload.resident_incarnation_id,
            account_id=account_id,
        )
    except Exception:
        if reserved_here:
            await release_max_plan_auxiliary_slot(account_id, project_id=str(project.id))
        raise
    if reserved_here and not created:
        await release_max_plan_auxiliary_slot(account_id, project_id=str(project.id))
    if expired_account_id:
        await release_max_plan_auxiliary_slot(expired_account_id, project_id=str(project.id))
    return {"acquired": True, "lease": resident_typed_fast_snapshot(project.vision)}


@router.post("/projects/{project_id}/strategic-session/typed-fast-lease/release")
@limiter.limit("30/minute")
async def release_agent_typed_fast_lease(
    request: Request,
    project_id: uuid.UUID,
    payload: ResidentTypedFastLeaseRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Release the exact Typed Fast slot on normal child or parent shutdown."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    if payload.lease_id is None or payload.generation is None:
        raise HTTPException(status_code=400, detail="typed Fast lease id and generation are required")
    project = await _resolve_agent_project(project_id, token, session)
    _lease, account_id, changed = await release_resident_typed_fast_lease(
        session,
        project,
        lease_id=payload.lease_id,
        generation=payload.generation,
        task_id=payload.task_id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.resident_incarnation_id,
        reason=payload.reason,
    )
    if changed and account_id:
        from app.max_plan_pool import release_max_plan_auxiliary_slot

        await release_max_plan_auxiliary_slot(account_id, project_id=str(project.id))
    return {"released": changed}


@router.post("/projects/{project_id}/strategic-call/speech-credentials")
@limiter.limit("30/minute")
async def resident_call_speech_credentials(
    request: Request,
    project_id: uuid.UUID,
    payload: ResidentCallSpeechCredentialsRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Vend speech credentials only to the Resident owning this active call.

    This deliberately does not reuse runner dispatch credentials: a Workday
    Resident can live for hours before a call begins, while provider keys are
    needed only by its deterministic Coordinator.  The browser and Fast child
    never call this route.  Do not log request or response bodies here: the
    response contains provider credentials.
    """
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    project = await _resolve_agent_project(project_id, token, session)
    active_call = read_resident_call(project.vision)
    if (
        not resident_call_is_active(active_call)
        or str((active_call or {}).get("worker_task_id")) != str(payload.task_id)
        or str((active_call or {}).get("call_id")) != payload.call_id
        or int((active_call or {}).get("generation") or 0)
        != payload.call_generation
        or not resident_worker_incarnation_is_current(
            project.vision,
            task_id=payload.task_id,
            worker_generation=int(
                (active_call or {}).get("worker_generation") or 1
            ),
            incarnation_id=payload.resident_incarnation_id,
        )
    ):
        # Avoid exposing whether another Resident owns a call.  In particular,
        # do not invoke Secret Manager before every server-owned fence passes.
        raise HTTPException(
            status_code=409,
            detail="No active call is owned by this Resident",
        )
    task = await session.get(Task, uuid.UUID(payload.task_id))
    if task is None or (task.context or {}).get("_resident_session") is not True:
        raise HTTPException(status_code=409, detail="Call requires a Resident task")
    try:
        speech = await resolve_resident_call_speech_credentials(task)
    except VoiceCredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not speech:
        raise HTTPException(status_code=503, detail="Resident call speech is not configured")
    try:
        qualification = validate_resident_call_deployment_configuration()
    except VoiceCredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"speech": speech, "foreground_qualification": qualification}


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
    from app.strategic_cold_successor_authority import reject_active_cold_successor

    reject_active_cold_successor(project.vision)
    if str(token.get("sub")) != str(payload.task_id):
        # The heartbeat must come from the resident session task's own token.
        raise HTTPException(status_code=403, detail="Token task mismatch")
    activity_before = resident_activity_semantics(
        _resident_activity_snapshot(read_resident_session(project.vision) or {})
    )
    async def _emit_activity_transition_if_changed() -> None:
        activity_after = resident_activity_semantics(
            _resident_activity_snapshot(read_resident_session(project.vision) or {})
        )
        if activity_after == activity_before:
            return
        from app.strategic_events import emit_strategic_session_state_updated

        await emit_strategic_session_state_updated(
            project.id,
            task_id=payload.task_id,
            activity=activity_after,
        )

    response = await renew_resident_session_lease(
        session,
        project,
        session_id=payload.session_id,
        task_id=payload.task_id,
        incarnation_id=payload.resident_incarnation_id,
        progress=bool(payload.progress),
        autonomous_turn=bool(payload.autonomous_turn),
        worker_generation=payload.worker_generation,
        note=payload.note,
        message_cursor=payload.message_cursor,
        handled_message_ids=payload.handled_message_ids,
        turn_latency_summary=payload.turn_latency_summary,
        operator_decision_summary=payload.operator_decision_summary,
        compaction_count=payload.compaction_count,
        last_compaction_at=payload.last_compaction_at,
        decisions_reg=payload.decisions_reg,
        activity=payload.activity,
        derive_consultation_status=True,
        repo_head=payload.repo_head,
    )
    if response.get("resident_incarnation_held") is False:
        await session.commit()
        await _emit_activity_transition_if_changed()
        return response
    if (
        payload.conversation_claim_incarnation is not None
        and payload.conversation_claim_id is None
    ):
        raise HTTPException(
            status_code=400,
            detail="conversation claim incarnation requires a claim id",
        )
    if payload.conversation_claim_id is not None:
        from app.conversation_coordinator import (
            legacy_conversation_claim_incarnation,
            renew_conversation_claim,
        )

        response["conversation_claim_held"] = await renew_conversation_claim(
            session,
            project_id=project.id,
            claim_id=payload.conversation_claim_id,
            task_id=uuid.UUID(payload.task_id),
            worker_generation=payload.worker_generation,
            incarnation_id=(
                payload.conversation_claim_incarnation
                or legacy_conversation_claim_incarnation(
                    uuid.UUID(payload.task_id),
                    payload.worker_generation,
                )
            ),
            lease_seconds=payload.conversation_claim_lease_seconds,
        )
        await session.commit()
    if payload.call_id is not None or payload.call_generation is not None:
        if not payload.call_id or payload.call_generation is None:
            raise HTTPException(
                status_code=400,
                detail="call_id and call_generation must be supplied together",
            )
        response["call"] = await renew_resident_call_lease(
            session,
            project,
            task_id=payload.task_id,
            incarnation_id=payload.resident_incarnation_id,
            call_id=payload.call_id,
            generation=payload.call_generation,
            ttl_seconds=payload.call_ttl_seconds,
            ready=payload.call_ready,
            usage=payload.call_usage,
        )
    else:
        response["call"] = resident_call_snapshot(project.vision)
    if payload.typed_fast_lease_id is not None or payload.typed_fast_generation is not None:
        if not payload.typed_fast_lease_id or payload.typed_fast_generation is None:
            raise HTTPException(
                status_code=400,
                detail="typed_fast_lease_id and typed_fast_generation must be supplied together",
            )
        try:
            response["typed_fast"] = await renew_resident_typed_fast_lease(
                session,
                project,
                task_id=payload.task_id,
                worker_generation=payload.worker_generation,
                incarnation_id=payload.resident_incarnation_id,
                lease_id=payload.typed_fast_lease_id,
                generation=payload.typed_fast_generation,
                ttl_seconds=payload.typed_fast_ttl_seconds,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            # Typed Fast is optional acceleration. A fenced/lapsed child must
            # fall back to Workday rather than make the parent heartbeat fail.
            response["typed_fast"] = resident_typed_fast_snapshot(project.vision)
    else:
        response["typed_fast"] = resident_typed_fast_snapshot(project.vision)
    active_call = read_resident_call(project.vision)
    if (
        resident_call_is_active(active_call)
        and str((active_call or {}).get("worker_task_id")) == str(payload.task_id)
    ):
        try:
            response["call_transport"] = issue_resident_call_credentials(
                call=active_call or {}, task_id=payload.task_id
            )
        except LiveKitConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _emit_activity_transition_if_changed()
    return response
