"""Agent-facing inbound strategic-message poll endpoint.

The strategic agent (a provider session pinned to the operator's local runner)
polls this to pick up new operator chat messages in
near-real time. The ``since`` cursor plus ascending order let it drain only what
arrived after its last poll — no re-reading the whole thread and no cold
context rebuild — which is what makes the interactive latency target
(§0.2: first token ≤ ~5 s) reachable.

The cursor filter is ``created_at >= since`` (INCLUSIVE), not ``>``: the agent
dedupes by message ``id`` and advances its cursor to the MAX returned
``created_at``, then re-polls a full page from there. An inclusive boundary
means the message(s) sitting exactly on the cursor are re-delivered (harmless —
the agent drops them by id) rather than skipped. This closes two skip races a
strict ``>`` opens: (1) the ">limit truncation skip" — when more than ``limit``
messages share (or straddle) a single ``created_at`` and the page is capped, the
next ``>`` poll would jump past the un-returned remainder; (2) the "commit-race
skip" — a message committed with a ``created_at`` at-or-before the cursor after
the previous query snapshotted. Both are recovered because the agent re-scans
the boundary instant inclusively and dedupes by id.

Read-only, agent-token auth, project-scoped exactly like the strategic-context
fetch surface it sits beside (``agent_strategic_fetch.py``): the same
``_resolve_agent_project`` cross-project 404 semantics. The poll has a dedicated
240/minute remote-IP + project limit: a 750 ms cadence is 80/minute, so an
active worker plus a replacement overlap consumes 160/minute and still leaves
80/minute for scheduling jitter and immediate retries. Mutations stay on the
``/api/agent/strategic`` POST surface.

Registered under the same ``/api/agent`` prefix in ``app.main`` and OWNS the
bare ``GET /projects/{project_id}/messages`` path the agent poll helper expects.
Two sibling routes in ``agent_strategic_fetch.py`` deliberately live on DISTINCT
paths so nothing shadows this poll: the ``/messages/{ref}`` single-message
resolver and the ``/messages/open`` compact recovery listing. (Historical note:
the recovery listing briefly collided on bare ``/messages`` and, being included
first in ``app.main``, silently shadowed this poll — resolved 2026-07-05 by
moving the listing to ``/messages/open``.)
"""

import base64
import copy
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Sequence
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi.util import get_remote_address
from sqlmodel import select

from app.artifact_refs import artifact_ref_grants_from_message_context
from app.broadcast import broadcast_to_project
from app.database import get_session
from app.message_audience import stamp_message_audience
from app.models import (
    ArtifactRef,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.project_message_projection import (
    message_event_payload,
    message_updated_event_payload,
    project_message_base,
)
from app.project_message_creation import (
    build_project_message,
    create_operator_project_message,
    lock_project_and_next_message_sequence,
    parse_operator_message_type,
)
from app.resident_conversation_scope import (
    resident_broad_message_scope,
    resident_conversation_floor_sequence,
    resident_conversation_scope,
)
from app.strategic_payload_summaries import _fetch_open_operator_cursor_floor
from app.strategic_consultation_status import (
    completed_workday_consultation_id as _completed_workday_consultation_id,
    emit_consultation_activity_if_changed as _emit_consultation_activity_if_changed,
    project_current_consultation_activity as _project_current_consultation_activity,
)
from app.strategic_fast_live_work import _fast_live_work_authority
from app.strategic_workday_handoff_recovery import (
    PRIVATE_HANDOFF_RETIREMENT_LIMIT as _PRIVATE_HANDOFF_RETIREMENT_LIMIT,
    recover_retired_private_handoff as _recover_retired_private_handoff,
    retire_unavailable_private_handoff as _retire_unavailable_private_handoff,
)
from app.time_utils import _utcnow, utc_iso
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)

_MESSAGES_POLL_DEFAULT_LIMIT = 20
_MESSAGES_POLL_MAX_LIMIT = 20
_LOWERCASE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WORKDAY_MAX_IMAGE_DOWNLOAD_BYTES = 9_750_000
_WORKDAY_MAX_REQUIRED_IMAGES = 8

# Typed Fast is the low-latency conversational owner, but it deliberately has
# no Blueprint token or checkout. The prose fallback is an intentionally
# explicit operator sentinel, not a general intent classifier: ordinary,
# quoted, negative, and informational mentions of direct operations stay Fast.
_DIRECT_WORKDAY_RECOVERY_PREFIX_RE = re.compile(
    r"\A(?:GENERATION-[0-9]+\s+MUTATION-ONLY\s+RECOVERY\.|"
    r"CANONICAL\s+READBACK\s+CONTRADICTS\s+YOUR\s+CLAIM\.)",
    re.IGNORECASE,
)
_DIRECT_WORKDAY_IMPERATIVE_RE = re.compile(
    r"\b(?:use\s+direct\s+strategic\s+ops?\s+only|"
    r"execute\s+(?:the\s+)?(?:[a-z0-9-]+\s+){0,4}direct\s+"
    r"(?:strategic\s+)?ops?)\b",
    re.IGNORECASE,
)
_DIRECT_WORKDAY_NO_CONSULT_RE = re.compile(
    r"\bdo\s+not\s+consult(?:/delegate)?\b",
    re.IGNORECASE,
)
_DIRECT_WORKDAY_RECEIPT_RE = re.compile(
    r"\b(?:actual\s+)?raw\s+json\s+receipts?\b",
    re.IGNORECASE,
)

_RESIDENT_POLL_INTERVAL_SECONDS = 0.75
_RESIDENT_POLL_EXPECTED_PER_MINUTE = 80
_RESIDENT_POLL_REPLACEMENT_OVERLAP = 2
_RESIDENT_POLL_RATE_LIMIT_PER_MINUTE = 240
_RESIDENT_POLL_RATE_LIMIT = f"{_RESIDENT_POLL_RATE_LIMIT_PER_MINUTE}/minute"


def _operator_requests_direct_workday_execution(message: ProjectMessage) -> bool:
    """Return whether an operator explicitly chose the tool-capable lane.

    This is routing, not authorization. The conversation endpoint already
    requires project-member access; every later strategic mutation still needs
    the Resident task token, current process/lease, and project-scoped backend
    authorization enforced by ``POST /api/agent/strategic``.
    """

    context = message.context if isinstance(message.context, dict) else {}
    intent = context.get("operator_intent")
    if isinstance(intent, dict):
        lane_owner = str(intent.get("lane_owner") or "").strip().lower()
        action = str(intent.get("action") or "").strip().lower()
        kind = str(intent.get("kind") or "").strip().lower()
        if (
            lane_owner == "workday"
            and action == "execute"
            and kind in {
                "direct_strategic_ops",
                "managed_execution",
                "strategic_mutation",
            }
        ):
            return True

    content = str(message.content or "")
    return bool(
        _DIRECT_WORKDAY_RECOVERY_PREFIX_RE.search(content)
        and _DIRECT_WORKDAY_IMPERATIVE_RE.search(content)
        and _DIRECT_WORKDAY_NO_CONSULT_RE.search(content)
        and _DIRECT_WORKDAY_RECEIPT_RE.search(content)
    )


class FinalizedCallTranscript(BaseModel):
    """Coordinator-authenticated final transcript; operator identity is never input."""

    task_id: uuid.UUID
    resident_incarnation_id: uuid.UUID | None = None
    call_id: str = Field(..., min_length=1, max_length=128)
    call_generation: int = Field(..., ge=1)
    content: str = Field(..., min_length=1, max_length=20_000)
    message_type: str = Field(default="NOTE", max_length=32)
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    input_source: Literal["voice", "typed", "whiteboard"]


class CompleteCallTurn(BaseModel):
    task_id: uuid.UUID
    resident_incarnation_id: uuid.UUID | None = None
    call_id: str = Field(..., min_length=1, max_length=128)
    call_generation: int = Field(..., ge=1)
    input_revision: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    covered_message_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=20)
    content: str = Field(..., min_length=1, max_length=20_000)
    exact_text: str | None = Field(default=None, max_length=20_000)
    display_blocks: list[dict[str, str]] = Field(default_factory=list, max_length=8)


class EndResidentCall(BaseModel):
    """Exact worker/call fence for coordinator-initiated empty-room cleanup."""

    task_id: uuid.UUID
    resident_incarnation_id: uuid.UUID | None = None
    call_id: str = Field(..., min_length=1, max_length=128)
    call_generation: int = Field(..., ge=1)
    reason: Literal["operator_disconnected"]


class ClaimResidentMessages(BaseModel):
    """Exact read-poll batch to admit as one durable foreground turn."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    resident_incarnation_id: uuid.UUID | None = None
    incarnation_id: uuid.UUID | None = None
    message_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=20)
    lease_seconds: int = Field(default=45, ge=15, le=300)


class ClaimPublicConversationTurn(BaseModel):
    """Server-fenced admission for one Fast or explicit Standard turn."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    client_claim_id: uuid.UUID
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    message_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=20)
    owner: Literal["fast", "standard"]
    explicit_standard_fallback: bool = False
    lease_seconds: int = Field(default=45, ge=15, le=300)


class CompleteTurnWorkdayRequest(BaseModel):
    """The one bounded private investigation a Fast public turn may enqueue."""

    delivery_key: str = Field(..., min_length=1, max_length=128)
    question: str = Field(..., min_length=1, max_length=20_000)
    continuation_key: str | None = Field(default=None, max_length=128)


class CompletePublicConversationTurn(BaseModel):
    """The foreground lane's caller-supplied answer; no fallback text exists."""

    task_id: uuid.UUID
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    claim_id: uuid.UUID
    owner: Literal["fast", "standard"]
    content: str = Field(..., min_length=1, max_length=20_000)
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    envelope: dict[str, Any]
    workday_request: CompleteTurnWorkdayRequest | None = None


class AbandonPublicConversationTurn(BaseModel):
    """Release a failed Fast claim before legacy Workday may recover it."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    claim_id: uuid.UUID


class DeliverWorkdayConsultation(BaseModel):
    """Durable background packet request; Workday cannot supply reply text."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    claim_id: uuid.UUID
    delivery_key: str = Field(..., min_length=1, max_length=128)
    continuation_key: str | None = Field(default=None, max_length=128)
    question: str = Field(..., min_length=1, max_length=20_000)


class ClaimNextWorkdayConsultation(BaseModel):
    """Ask for one lease-bound internal packet; no public reply field exists."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    lease_seconds: int = Field(default=45, ge=15, le=300)


class WorkdayConsultationTraceEvent(BaseModel):
    elapsed_ms: int = Field(..., ge=0)
    kind: Literal["text", "tool", "tool_result", "error", "result"]
    label: str | None = Field(default=None, max_length=80)
    is_error: bool | None = None


class WorkdayConsultationAttemptDiagnostic(BaseModel):
    attempt: int = Field(..., ge=1, le=8)
    workday_claim_id: uuid.UUID
    outcome: Literal["completed", "abandoned", "failed"]
    terminal_predicate: str = Field(..., min_length=1, max_length=80)
    provider_terminal_reason: str | None = Field(default=None, max_length=255)
    provider_output_tail: str | None = Field(default=None, max_length=2_000)
    started_at: datetime
    ended_at: datetime
    turn_ms: int = Field(..., ge=0)
    ttft_ms: int | None = Field(default=None, ge=0)
    tool_call_count: int = Field(..., ge=0)
    tool_call_duration_ms: int = Field(..., ge=0)
    result_chars: int = Field(..., ge=0)
    trace: list[WorkdayConsultationTraceEvent] = Field(default_factory=list, max_length=128)
    trace_truncated: bool = False


class AcknowledgeWorkdayConsultation(BaseModel):
    """Close or deliberately release one exact internal delivery."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    workday_claim_id: uuid.UUID
    outcome: Literal["completed", "abandoned", "failed"]
    result_summary: str | None = Field(default=None, max_length=20_000)
    # Rolling-release compatibility: a Resident that was already inside a
    # provider turn when the diagnostic contract deployed cannot add this
    # field until it drains and auto-updates.  The coordinator records an
    # explicit bounded legacy diagnostic for that exact claim instead of
    # returning 422 forever and preventing the runner from draining.
    attempt_diagnostic: WorkdayConsultationAttemptDiagnostic | None = None


class RenewWorkdayConsultation(BaseModel):
    """Extend one exact private Workday packet before its lease expires."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    workday_claim_id: uuid.UUID
    lease_seconds: int = Field(default=45, ge=15, le=300)


class ClaimWorkdayContinuation(BaseModel):
    """Ask Fast for one server-created result-continuation receipt."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    lease_seconds: int = Field(default=45, ge=15, le=300)


class CompleteWorkdayContinuation(BaseModel):
    """Receipt-bound Fast follow-up for a completed Workday consultation."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    continuation_claim_id: uuid.UUID
    content: str = Field(..., min_length=1, max_length=20_000)
    idempotency_key: str = Field(..., min_length=1, max_length=255)


class RenewWorkdayContinuation(BaseModel):
    """Extend an exact Fast continuation receipt before it expires."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    continuation_claim_id: uuid.UUID
    lease_seconds: int = Field(default=45, ge=15, le=300)


class ReleaseWorkdayContinuation(BaseModel):
    """Release a failed continuation receipt without reopening completed output."""

    task_id: uuid.UUID
    session_id: str = Field(..., min_length=1, max_length=255)
    worker_generation: int = Field(..., ge=1)
    incarnation_id: uuid.UUID
    continuation_claim_id: uuid.UUID


def _resident_poll_rate_limit_key(request: Request) -> str:
    """Isolate high-frequency polls by network origin and project.

    The project component prevents unrelated residents behind one NAT or Cloud
    Run ingress address from consuming each other's budget. Authentication
    tokens are deliberately excluded: rotating a token must not evade admission.
    """
    remote = get_remote_address(request)
    project_scope = str(request.path_params.get("project_id") or "unknown-project")
    return f"resident-message-poll:{remote}:{project_scope}"


def _task_is_fast_owner(task: Task, project: Project) -> bool:
    context = task.context if isinstance(task.context, dict) else {}
    resident = (
        (project.vision or {}).get("_resident_strategic_session") or {}
        if isinstance(project.vision, dict)
        else {}
    )
    if (
        context.get("_resident_session") is True
        and resident.get("status") == "active"
        and not bool(resident.get("stop_requested"))
        and str(resident.get("task_id") or "") == str(task.id)
        and str(resident.get("session_id") or "")
        == str(context.get("strategic_session_id") or "")
        and _parse_persisted_worker_generation(resident.get("worker_generation"))
        == _parse_persisted_worker_generation(
            context.get("_resident_worker_generation")
        )
    ):
        # Typed Fast is a foreground role within the existing Resident task.
        return True
    return False


def _require_current_fast_task_identity(
    *,
    task: Task,
    project: Project,
    worker_generation: int,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Bind Fast API calls to the one active Resident task/session/generation."""
    context = task.context if isinstance(task.context, dict) else {}
    persisted_generation = _parse_persisted_worker_generation(
        context.get("_resident_worker_generation")
    )
    persisted_session_id = str(context.get("strategic_session_id") or "")
    if (
        context.get("_resident_session") is not True
        or not persisted_session_id
        or persisted_generation is None
        or persisted_generation != worker_generation
        or (session_id is not None and session_id != persisted_session_id)
        or not _task_is_fast_owner(task, project)
    ):
        raise HTTPException(status_code=409, detail="Fast Resident identity is superseded")
    return context


async def _claim_current_fast_process_incarnation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    project: Project,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
) -> uuid.UUID:
    """Renew the server-owned process lease while the caller holds Project lock."""
    from app.strategic_resident_session import claim_resident_worker_incarnation
    from app.task_locking import with_vision_locked

    accepted: uuid.UUID | None = None

    def _claim(vision: dict) -> None:
        nonlocal accepted
        accepted = claim_resident_worker_incarnation(
            vision,
            task_id=task_id,
            worker_generation=worker_generation,
            incarnation_id=incarnation_id,
        )
        if accepted is None:
            raise HTTPException(
                status_code=409,
                detail="Fast Resident process incarnation is superseded",
            )

    await with_vision_locked(
        session,
        project_id,
        _claim,
        _locked_project=project,
    )
    assert accepted is not None
    return accepted


def _require_current_fast_process_incarnation(
    *,
    project: Project,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
) -> None:
    """Do not let an old process complete/ack a live owner's work."""
    from app.strategic_resident_session import resident_worker_incarnation_is_current

    if not resident_worker_incarnation_is_current(
        project.vision,
        task_id=task_id,
        worker_generation=worker_generation,
        incarnation_id=incarnation_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Fast Resident process incarnation is superseded",
        )


def _memory_query_from_claimed_operator_rows(rows: Sequence[ProjectMessage]) -> str:
    """Return the bounded canonical operator text that drives V2 retrieval.

    The conversation claim is already the authoritative turn boundary. Keep the
    retrieval query ephemeral and byte-bounded: it guides selection but never
    becomes durable trace content.
    """

    joined = "\n".join(
        str(row.content or "")
        for row in sorted(rows, key=lambda row: (int(row.sequence or 0), str(row.id)))
    )
    return joined.encode("utf-8")[:16_384].decode("utf-8", "ignore")


async def _resolve_current_context_envelope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    reference: Any,
    session_id: str | None = None,
    claim_id: uuid.UUID | None = None,
    worker_generation: int | None = None,
    worker_incarnation: uuid.UUID | None = None,
):
    """Resolve a non-content pointer and reject stale privacy/authority state."""
    if not isinstance(reference, dict):
        raise HTTPException(
            status_code=409, detail="Strategic context envelope reference is invalid"
        )
    from app.strategic_memory_envelope import (
        get_fenced_context_envelope_by_reference,
        get_fenced_context_envelope_snapshot,
    )

    envelope = await get_fenced_context_envelope_by_reference(
        session,
        project_id=project_id,
        reference=reference,
    )
    if envelope is None:
        raise HTTPException(
            status_code=409,
            detail="Strategic context envelope is stale, expired, or revoked",
        )
    if any(
        value is not None
        for value in (
            session_id,
            claim_id,
            worker_generation,
            worker_incarnation,
        )
    ):
        try:
            snapshot_id = uuid.UUID(str(reference["snapshot_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(
                status_code=409,
                detail="Strategic context envelope reference is invalid",
            ) from None
        exact = await get_fenced_context_envelope_snapshot(
            session,
            project_id=project_id,
            snapshot_id=snapshot_id,
            session_id=session_id,
            claim_id=claim_id,
            worker_generation=worker_generation,
            worker_incarnation=worker_incarnation,
        )
        if (
            exact is None
            or exact.canonical_sha256 != envelope.canonical_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="Strategic context envelope belongs to another turn",
            )
    return envelope


async def _resolve_message_context_envelope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    message_id: uuid.UUID,
):
    """Lock a private packet/result and resolve its current envelope pointer."""
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == message_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="Private packet is unavailable")
    context = row.context if isinstance(row.context, dict) else {}
    envelope = await _resolve_current_context_envelope(
        session,
        project_id=project_id,
        reference=context.get("envelope"),
    )
    return row, envelope


async def _rebind_private_packet_envelope_to_current_worker(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    row: ProjectMessage,
    session_id: str,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
):
    """Make a restart-surviving Workday packet writable by its live claimant."""

    context = dict(row.context or {})
    try:
        fast_claim_id = uuid.UUID(str(context.get("claim_id")))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(
            status_code=409,
            detail="Private Workday packet Fast claim identity is invalid",
        ) from None
    from app.strategic_memory_envelope import (
        context_envelope_reference,
        rebind_fenced_context_envelope_for_worker,
    )

    rebound = await rebind_fenced_context_envelope_for_worker(
        session,
        project_id=project_id,
        reference=context.get("envelope"),
        session_id=session_id,
        claim_id=fast_claim_id,
        worker_generation=worker_generation,
        worker_incarnation=worker_incarnation,
    )
    if rebound is None:
        raise HTTPException(
            status_code=409,
            detail="Strategic context envelope is stale, expired, or revoked",
        )
    context["envelope"] = context_envelope_reference(rebound)
    row.context = context
    session.add(row)
    await session.flush()
    return rebound


@router.post("/projects/{project_id}/strategic-call/finalized-transcript")
@limiter.limit("60/minute")
async def create_finalized_call_transcript(
    request: Request,
    project_id: uuid.UUID,
    payload: FinalizedCallTranscript,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Persist one canonical call input using the server-owned lease actor.

    The coordinator may authenticate only as the Workday Resident task.  It
    cannot choose an operator, project, generation, or reply owner; all are
    checked against the locked nested lease immediately before the existing
    idempotent operator-message transaction commits.
    """
    if str(payload.task_id) != str(token.get("sub")):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    project = await _resolve_agent_project(project_id, token, session)
    task = await session.get(Task, payload.task_id)
    task_context = task.context if task is not None else {}
    if task is None or task_context.get("_resident_session") is not True:
        raise HTTPException(status_code=409, detail="Finalized transcripts require a Resident task")

    from app.strategic_resident_session import (
        bind_resident_call_input_to_locked_project,
        read_resident_call,
        resident_call_is_active,
        resident_worker_incarnation_is_current,
    )

    def _lease_actor(vision: dict | None) -> str:
        call = read_resident_call(vision)
        if (
            not resident_call_is_active(call)
            or str((call or {}).get("call_id")) != payload.call_id
            or int((call or {}).get("generation") or 0) != payload.call_generation
            or str((call or {}).get("worker_task_id")) != str(payload.task_id)
            or not resident_worker_incarnation_is_current(
                vision,
                task_id=payload.task_id,
                worker_generation=int(
                    (call or {}).get("worker_generation") or 1
                ),
                incarnation_id=payload.resident_incarnation_id,
            )
        ):
            raise HTTPException(status_code=409, detail="Call lease superseded")
        actor = str((call or {}).get("actor_user_id") or "").strip()
        if not actor:
            raise HTTPException(status_code=409, detail="Call lease has no operator actor")
        return actor

    actor_user_id = _lease_actor(project.vision)
    message_context = {
        "call_id": payload.call_id,
        "call_generation": payload.call_generation,
        "input_source": payload.input_source,
    }

    async def _revalidate_call_lease(
        locked_session: AsyncSession,
        _message_id: uuid.UUID,
        current_context: dict[str, Any],
    ) -> dict[str, Any]:
        locked_project = (
            await locked_session.execute(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked_project is None or _lease_actor(locked_project.vision) != actor_user_id:
            raise HTTPException(status_code=409, detail="Call lease superseded")
        locked_task = (
            await locked_session.execute(
                select(Task)
                .where(Task.id == payload.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        locked_context = locked_task.context if locked_task is not None else {}
        if (
            locked_task is None
            or locked_task.status != TaskStatus.IN_PROGRESS
            or locked_context.get("_resident_session") is not True
            or str(locked_context.get("strategic_session_id"))
            != str(
                (locked_project.vision or {})
                .get("_resident_strategic_session", {})
                .get("session_id")
            )
        ):
            raise HTTPException(status_code=409, detail="Resident task is no longer live")
        return await bind_resident_call_input_to_locked_project(
            locked_session,
            locked_project,
            actor_user_id=actor_user_id,
            context=current_context,
            input_source=payload.input_source,
        )

    result = await create_operator_project_message(
        session,
        project=project,
        user_id=actor_user_id,
        message_type=parse_operator_message_type(payload.message_type),
        content=payload.content,
        context=message_context,
        workbench_context=None,
        idempotency_key=payload.idempotency_key,
        surface=MessageSurface.CONVERSATION,
        include_strategic_session_snapshot=False,
        precommit_validate=_revalidate_call_lease,
    )
    if result.message_event is not None:
        await broadcast_to_project(str(project_id), "message_created", result.message_event)
    return {
        "id": result.message_id,
        "status": result.status,
        "message": _poll_message_dict(result.message),
        "input_revision": (result.message.context or {}).get("input_revision"),
    }


@router.post("/projects/{project_id}/strategic-call/end")
@limiter.limit("30/minute")
async def end_resident_call_after_operator_disconnect(
    request: Request,
    project_id: uuid.UUID,
    payload: EndResidentCall,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Release a call only when its exact Resident worker observes an empty room."""
    if str(payload.task_id) != str(token.get("sub")):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    project = await _resolve_agent_project(project_id, token, session)
    task = await session.get(Task, payload.task_id)
    if task is None or (task.context or {}).get("_resident_session") is not True:
        raise HTTPException(status_code=409, detail="Call end requires a Resident task")

    from app.strategic_resident_session import (
        read_resident_call,
        release_resident_call_lease,
        resident_call_snapshot,
        resident_worker_incarnation_is_current,
    )

    call = read_resident_call(project.vision)
    if (
        not isinstance(call, dict)
        or str(call.get("call_id")) != payload.call_id
        or int(call.get("generation") or 0) != payload.call_generation
        or str(call.get("worker_task_id")) != str(payload.task_id)
        or not resident_worker_incarnation_is_current(
            project.vision,
            task_id=payload.task_id,
            worker_generation=int(call.get("worker_generation") or 1),
            incarnation_id=payload.resident_incarnation_id,
        )
    ):
        raise HTTPException(status_code=409, detail="Call lease superseded")

    _call, account_id, changed = await release_resident_call_lease(
        session,
        project,
        call_id=payload.call_id,
        generation=payload.call_generation,
        task_id=payload.task_id,
        worker_generation=int(call.get("worker_generation") or 1),
        incarnation_id=payload.resident_incarnation_id,
        reason=payload.reason,
    )
    if changed and account_id:
        from app.max_plan_pool import release_max_plan_call_slot

        await release_max_plan_call_slot(account_id, project_id=str(project.id))
    public_call = resident_call_snapshot(project.vision)
    if changed:
        await broadcast_to_project(
            str(project.id), "call_updated", {"call": public_call}
        )
    return {
        "project_id": str(project.id),
        "call": public_call,
        "status": "ended" if changed else "already_ended",
    }


@router.get("/projects/{project_id}/strategic-call/inputs")
@limiter.limit("120/minute")
async def get_call_inputs(
    request: Request,
    project_id: uuid.UUID,
    call_id: str = Query(..., min_length=1, max_length=128),
    call_generation: int = Query(..., ge=1),
    resident_incarnation_id: uuid.UUID | None = Query(default=None),
    message_id: uuid.UUID | None = Query(default=None),
    after_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=20),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Fetch canonical typed/voice/Whiteboard turns for the active coordinator."""
    if message_id is not None and after_sequence is not None:
        raise HTTPException(status_code=400, detail="message_id and after_sequence are mutually exclusive")
    project = await _resolve_agent_project(project_id, token, session)
    task_id = uuid.UUID(str(token.get("sub")))
    task = await session.get(Task, task_id)
    task_context = task.context if task is not None else {}
    from app.strategic_resident_session import (
        read_resident_call,
        resident_call_is_active,
        resident_worker_incarnation_is_current,
    )

    call = read_resident_call(project.vision)
    if (
        task is None
        or task_context.get("_resident_session") is not True
        or not resident_call_is_active(call)
        or str((call or {}).get("call_id")) != call_id
        or int((call or {}).get("generation") or 0) != call_generation
        or str((call or {}).get("worker_task_id")) != str(task_id)
        or not resident_worker_incarnation_is_current(
            project.vision,
            task_id=task_id,
            worker_generation=int(
                (call or {}).get("worker_generation") or 1
            ),
            incarnation_id=resident_incarnation_id,
        )
    ):
        raise HTTPException(status_code=409, detail="Call lease superseded")

    conditions = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.from_type == "user",
        ProjectMessage.surface == MessageSurface.CONVERSATION,
        # Apply the call fence before the bounded page limit.  Filtering only
        # after ``LIMIT`` lets unrelated ordinary chat rows starve canonical
        # call input delivery indefinitely (and lets any project member turn
        # that into a cheap availability attack against the call owner).
        ProjectMessage.context["call_id"].as_string() == call_id,
        ProjectMessage.context["call_generation"].as_integer() == call_generation,
    ]
    if message_id is not None:
        conditions.append(ProjectMessage.id == message_id)
    if after_sequence is not None:
        conditions.append(ProjectMessage.sequence > after_sequence)
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(*conditions)
                .order_by(ProjectMessage.sequence.asc(), ProjectMessage.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # Keep an in-process defence-in-depth fence for malformed/cross-dialect JSON
    # values even though the SQL predicate above is what protects the page limit.
    rows = [
        row
        for row in rows
        if isinstance(row.context, dict)
        and row.context.get("call_id") == call_id
        and int(row.context.get("call_generation") or 0) == call_generation
    ]
    attachments = await _materialize_workday_artifact_attachments(session, project_id, rows)
    messages: list[dict[str, Any]] = []
    for row in rows:
        # The call coordinator advances its durable cursor by sequence, while
        # the legacy resident poll intentionally exposes a narrower seven-field
        # projection.  Add it only on this call-specific endpoint; without it
        # every otherwise-valid call input is discarded by the coordinator's
        # strict canonical-input parser.
        projected = {**_poll_message_dict(row), "sequence": row.sequence}
        if attachments.get(row.id):
            projected["context"] = {
                **dict(projected.get("context") or {}),
                "attachments": attachments[row.id],
            }
        messages.append(projected)
    return {
        "call_id": call_id,
        "call_generation": call_generation,
        "input_revision": int((call or {}).get("input_revision") or 0),
        "messages": messages,
    }


@router.post("/projects/{project_id}/strategic-call/complete-turn")
@limiter.limit("60/minute")
async def complete_call_turn(
    request: Request,
    project_id: uuid.UUID,
    payload: CompleteCallTurn,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Atomically commit the Fast-owned public answer for one fenced input revision."""
    if str(payload.task_id) != str(token.get("sub")):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    if len(set(payload.covered_message_ids)) != len(payload.covered_message_ids):
        raise HTTPException(status_code=400, detail="covered_message_ids contains duplicates")
    allowed_blocks = {"latex", "code", "table", "artifact_ref"}
    normalized_blocks: list[dict[str, str]] = []
    for block in payload.display_blocks:
        if set(block) != {"type", "content"} or block.get("type") not in allowed_blocks:
            raise HTTPException(status_code=400, detail="Invalid display block")
        content = str(block["content"])
        if not content or len(content) > 10_000:
            raise HTTPException(status_code=400, detail="Invalid display block content")
        normalized_blocks.append({"type": block["type"], "content": content})

    await _resolve_agent_project(project_id, token, session)
    task = await session.get(Task, payload.task_id)
    if task is None or (task.context or {}).get("_resident_session") is not True:
        raise HTTPException(status_code=409, detail="Call completion requires a Resident task")
    # Take the Project authority boundary before the message sequence and user
    # rows, matching the existing Project -> sequence -> message lock order.
    locked_project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    locked_task = (
        await session.execute(
            select(Task)
            .where(Task.id == payload.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    from app.strategic_resident_session import (
        read_resident_call,
        resident_call_is_active,
        resident_worker_incarnation_is_current,
    )

    call = read_resident_call(locked_project.vision if locked_project else None)
    if (
        locked_project is None
        or locked_task is None
        or locked_task.status != TaskStatus.IN_PROGRESS
        or (locked_task.context or {}).get("_resident_session") is not True
        or not resident_call_is_active(call)
        or str((call or {}).get("call_id")) != payload.call_id
        or int((call or {}).get("generation") or 0) != payload.call_generation
        or str((call or {}).get("worker_task_id")) != str(payload.task_id)
        or int((call or {}).get("input_revision") or 0) != payload.input_revision
        or not resident_worker_incarnation_is_current(
            locked_project.vision,
            task_id=payload.task_id,
            worker_generation=int(
                (call or {}).get("worker_generation") or 1
            ),
            incarnation_id=payload.resident_incarnation_id,
        )
    ):
        raise HTTPException(status_code=409, detail="Call input revision superseded")

    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == payload.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        context = existing.context if isinstance(existing.context, dict) else {}
        expected_covered_ids = sorted(
            str(message_id) for message_id in payload.covered_message_ids
        )
        if (
            existing.from_type != "strategic"
            or existing.from_task_id != payload.task_id
            or existing.content != payload.content.strip()
            or context.get("call_id") != payload.call_id
            or int(context.get("call_generation") or 0) != payload.call_generation
            or int(context.get("input_revision") or 0) != payload.input_revision
            or context.get("_resident_reply_covered_message_ids")
            != expected_covered_ids
            or context.get("exact_text") != (payload.exact_text or None)
            or (context.get("display_blocks") or []) != normalized_blocks
        ):
            raise HTTPException(status_code=409, detail="idempotency_key already belongs to a different message")
        return {"id": str(existing.id), "status": "duplicate", "input_revision": payload.input_revision}

    covered_rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.id.in_(payload.covered_message_ids),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in covered_rows}
    if len(by_id) != len(payload.covered_message_ids):
        raise HTTPException(status_code=409, detail="covered message is unavailable")
    for message_id in payload.covered_message_ids:
        inbound = by_id[message_id]
        inbound_context = inbound.context if isinstance(inbound.context, dict) else {}
        if (
            inbound.from_type != "user"
            or inbound.status != "open"
            or inbound_context.get("call_id") != payload.call_id
            or int(inbound_context.get("call_generation") or 0) != payload.call_generation
            or int(inbound_context.get("input_revision") or 0) > payload.input_revision
        ):
            raise HTTPException(status_code=409, detail="covered message is outside this active call input")

    sequence = await lock_project_and_next_message_sequence(session, project_id)
    context = stamp_message_audience(
        {
            "call_id": payload.call_id,
            "call_generation": payload.call_generation,
            "input_revision": payload.input_revision,
            "message_role": "operator_reply",
            "_resident_reply_covered_message_ids": sorted(
                str(message_id) for message_id in payload.covered_message_ids
            ),
            **({"exact_text": payload.exact_text} if payload.exact_text else {}),
            **({"display_blocks": normalized_blocks} if normalized_blocks else {}),
        },
        "question_answer",
        message_type="NOTE",
    )
    output = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=payload.task_id,
        content=payload.content.strip(),
        context=context,
        idempotency_key=payload.idempotency_key,
        resident_session_id=(locked_project.vision or {}).get("_resident_strategic_session", {}).get("session_id"),
    )
    session.add(output)
    for inbound in covered_rows:
        inbound.status = "handled"
        inbound.handling_state = MessageHandlingState.RESPONDED
        inbound.handled_by_task_id = payload.task_id
        session.add(inbound)
    await session.commit()
    await session.refresh(output)
    await broadcast_to_project(str(project_id), "message_created", message_event_payload(output))
    return {"id": str(output.id), "status": "created", "input_revision": payload.input_revision}


@router.post("/projects/{project_id}/conversation/turns/claim")
@limiter.limit("120/minute")
async def claim_public_conversation_turn(
    request: Request,
    project_id: uuid.UUID,
    payload: ClaimPublicConversationTurn,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Claim one foreground answer owner; Workday has no public-owner route."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == payload.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Foreground task is not active")
    owner = payload.owner
    if owner != "fast":
        # Do not turn every in-progress project task into a public Strategic
        # identity while a future Standard lease protocol is still unbuilt.
        raise HTTPException(
            status_code=409,
            detail="Standard fallback is disabled until it has a server-provisioned lease",
        )
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    accepted_incarnation = await _claim_current_fast_process_incarnation(
        session,
        project_id=project_id,
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )

    from app.conversation_coordinator import (
        ConversationPublicReplyOwner,
        OperatorTurnParked,
        claim_operator_messages,
    )

    try:
        claim = await claim_operator_messages(
            session,
            project_id=project_id,
            session_id=payload.session_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            incarnation_id=accepted_incarnation,
            message_ids=payload.message_ids,
            lease_seconds=payload.lease_seconds,
            public_reply_owner=ConversationPublicReplyOwner(owner),
            canonical_conversation_only=True,
            client_claim_id=payload.client_claim_id,
            claim_idempotency_key=payload.idempotency_key,
            conversation_floor_sequence=resident_conversation_floor_sequence(
                (project.vision or {}).get("_resident_strategic_session") or {}
            ),
        )
    except OperatorTurnParked as parked:
        # A park is a committed decision with its own reason -- never a rollback.
        await session.commit()
        raise HTTPException(status_code=409, detail=parked.reason) from None
    if claim is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Turn is already claimed or changed")
    transcript_sequence = max(int(row.sequence or 0) for row in claim.rows)
    if transcript_sequence <= 0:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Turn is missing its canonical transcript sequence",
        )
    from app.strategic_memory_envelope import (
        ContextEnvelopeFenceError,
        build_and_store_context_envelope_snapshot,
    )

    try:
        attachment_manifests = await _canonical_delivery_attachment_manifests(
            session,
            project_id,
            claim.rows,
        )
        live_work_authority = await _fast_live_work_authority(
            session,
            project=project,
            resident_task_id=task.id,
        )
        envelope = await build_and_store_context_envelope_snapshot(
            session,
            project_id=project_id,
            session_id=payload.session_id,
            claim_id=claim.claim_id,
            worker_generation=payload.worker_generation,
            worker_incarnation=accepted_incarnation,
            input_revision=transcript_sequence,
            transcript_sequence=transcript_sequence,
            decision_authority=live_work_authority,
            memory_query=_memory_query_from_claimed_operator_rows(claim.rows),
            attachment_manifests=attachment_manifests,
        )
    except (ContextEnvelopeFenceError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return {
        "claim_id": str(claim.claim_id),
        "owner": owner,
        "message_ids": [str(row.id) for row in claim.rows],
        "attempt": claim.attempt,
        "lease_expires_at": utc_iso(claim.lease_expires_at),
        "envelope": envelope.model_dump(mode="json"),
    }


@router.post("/projects/{project_id}/conversation/turns/complete")
@limiter.limit("120/minute")
async def complete_public_conversation_turn(
    request: Request,
    project_id: uuid.UUID,
    payload: CompletePublicConversationTurn,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Commit caller-provided Fast/Standard text after rechecking owner state."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Foreground task is not active")
    task_context = task.context if isinstance(task.context, dict) else {}
    if payload.owner != "fast":
        raise HTTPException(
            status_code=409,
            detail="Standard fallback is disabled until it has a server-provisioned lease",
        )
    task_context = _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    # A committed receipt is safe to replay after its short-lived derived
    # envelope has expired: it creates no new prompt, packet, or public text.
    # The coordinator below still proves the idempotency key, claim, owner,
    # content, and optional Workday request exactly match. New completions must
    # always resolve the live privacy/authority pointer first.
    existing_completion = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == payload.idempotency_key.strip(),
            )
        )
    ).scalar_one_or_none()
    if existing_completion is None:
        await _resolve_current_context_envelope(
            session,
            project_id=project_id,
            reference=payload.envelope,
            session_id=task_context.get("strategic_session_id"),
            claim_id=payload.claim_id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
        )

    from app.conversation_coordinator import (
        ConversationPublicReplyOwner,
        complete_claimed_public_turn,
    )

    try:
        result = await complete_claimed_public_turn(
            session,
            project_id=project_id,
            claim_id=payload.claim_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            incarnation_id=payload.incarnation_id,
            owner=ConversationPublicReplyOwner(payload.owner),
            content=payload.content,
            idempotency_key=payload.idempotency_key,
            resident_session_id=task_context.get("strategic_session_id"),
            workday_request=(
                payload.workday_request.model_dump()
                if payload.workday_request is not None
                else None
            ),
            envelope=payload.envelope,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    # Re-emit the canonical event for an exact replay too. A completion response
    # and its post-commit websocket event can both be lost after the durable row
    # was written; the foreground then retries this receipt specifically to
    # reconcile that ambiguity. Project-message delivery is idempotent by row id,
    # while suppressing this event leaves Chat waiting for its fallback poll.
    await broadcast_to_project(
        str(project_id), "message_created", message_event_payload(result.message)
    )
    response: dict[str, Any] = {
        "id": str(result.message.id),
        "status": "duplicate" if result.duplicate else "created",
    }
    if result.consultation_packet is not None:
        packet_context = (
            result.consultation_packet.context
            if isinstance(result.consultation_packet.context, dict)
            else {}
        )
        response["workday_packet"] = {
            "packet_id": str(result.consultation_packet.id),
            "consultation_id": packet_context.get(
                "_workday_consultation_atomic_reply_id"
            ),
            "continuation_key": packet_context.get(
                "_conversation_consultation_continuation"
            ),
            "envelope": packet_context.get("envelope"),
        }
    return response


@router.post("/projects/{project_id}/conversation/turns/abandon")
@limiter.limit("120/minute")
async def abandon_public_conversation_turn(
    request: Request,
    project_id: uuid.UUID,
    payload: AbandonPublicConversationTurn,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Release a Fast claim only when it cannot race a completed reply."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Foreground task is not active")
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    from app.conversation_coordinator import abandon_fast_public_turn

    try:
        status = await abandon_fast_public_turn(
            session,
            project_id=project_id,
            claim_id=payload.claim_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            incarnation_id=payload.incarnation_id,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    consultation_id = (
        await _completed_workday_consultation_id(
            session,
            project_id=project_id,
            claim_id=payload.claim_id,
        )
        if status == "already_completed"
        else None
    )
    await session.commit()
    return {
        "status": status,
        "legacy_fallback_admissible": status
        in {"abandoned", "already_abandoned"},
        "consultation_id": consultation_id,
    }


@router.post("/projects/{project_id}/workday-consultations")
@limiter.limit("120/minute")
async def deliver_workday_consultation(
    request: Request,
    project_id: uuid.UUID,
    payload: DeliverWorkdayConsultation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Give Workday a durable Fast-owned consultation packet, never reply text."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        project is None
        or task is None
        or task.status != TaskStatus.IN_PROGRESS
    ):
        raise HTTPException(status_code=409, detail="Workday session is superseded or inactive")
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    claim_rows = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.conversation_claim_id == payload.claim_id,
            )
            .with_for_update()
        )
    ).scalars().all()
    if not claim_rows or any(
        row.from_type != "user"
        or row.status != "open"
        or row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != payload.task_id
        or row.conversation_claim_generation != payload.worker_generation
        or row.conversation_claim_incarnation != payload.incarnation_id
        or row.conversation_claim_expires_at is None
        or row.conversation_claim_expires_at <= _utcnow()
        or row.resident_session_id not in {None, payload.session_id}
        or (row.context or {}).get("_conversation_public_reply_owner") != "fast"
        for row in claim_rows
    ):
        raise HTTPException(
            status_code=409,
            detail="Consultation requires the live Fast-owned claim",
        )

    from app.strategic_memory_envelope import build_and_store_context_envelope_snapshot
    from app.conversation_coordinator import ConversationClaim, create_or_get_workday_consultation_packet

    claim = ConversationClaim(
        claim_id=payload.claim_id,
        rows=tuple(claim_rows),
        lease_expires_at=max(row.conversation_claim_expires_at for row in claim_rows if row.conversation_claim_expires_at is not None),
        attempt=max(row.conversation_claim_attempt for row in claim_rows),
    )
    live_work_authority = await _fast_live_work_authority(
        session,
        project=project,
        resident_task_id=task.id,
    )
    attachment_manifests = await _canonical_delivery_attachment_manifests(
        session,
        project_id,
        claim_rows,
    )
    envelope = await build_and_store_context_envelope_snapshot(
        session,
        project_id=project_id,
        session_id=payload.session_id,
        claim_id=payload.claim_id,
        worker_generation=payload.worker_generation,
        worker_incarnation=payload.incarnation_id,
        input_revision=max(int(row.sequence or 0) for row in claim_rows),
        transcript_sequence=max(int(row.sequence or 0) for row in claim_rows),
        decision_authority=live_work_authority,
        memory_query=_memory_query_from_claimed_operator_rows(claim_rows),
        attachment_manifests=attachment_manifests,
    )
    try:
        packet = await create_or_get_workday_consultation_packet(
            session,
            project_id=project_id,
            claim=claim,
            envelope=envelope,
            delivery_key=payload.delivery_key,
            question=payload.question,
            continuation_key=payload.continuation_key,
            resident_session_id=payload.session_id,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return {
        "packet_id": str(packet.message.id),
        "status": "duplicate" if packet.duplicate else "created",
        "envelope": packet.message.context["envelope"],
        "continuation_key": packet.message.context["_conversation_consultation_continuation"],
    }


def _workday_consultation_packet_response(
    *,
    packet: ProjectMessage,
    workday_claim_id: uuid.UUID,
    lease_expires_at: datetime,
    attempt: int,
    envelope: Any,
) -> dict[str, Any]:
    """Project only server-created packet fields to the authenticated Resident."""
    context = packet.context if isinstance(packet.context, dict) else {}
    from app.conversation_coordinator import bounded_workday_consultation_attempts
    from app.strategic_workday_mutation_rebind import (
        WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
        WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
        WORKDAY_OPERATION_LEDGER_LIMIT,
        bounded_workday_mutation_receipts,
    )
    ledger = context.get(WORKDAY_OPERATION_LEDGER_CONTEXT_KEY)
    ledger_context = {
        WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: ledger
        if isinstance(ledger, list)
        else []
    }
    return {
        "packet_id": str(packet.id),
        "workday_claim_id": str(workday_claim_id),
        "lease_expires_at": utc_iso(lease_expires_at),
        "attempt": attempt,
        "claim_id": context.get("claim_id"),
        "delivery_key": context.get("delivery_key"),
        "continuation_key": context.get("_conversation_consultation_continuation"),
        "consultation_id": context.get("_workday_consultation_atomic_reply_id"),
        "question": packet.content,
        "envelope": envelope.model_dump(mode="json"),
        "prior_attempts": bounded_workday_consultation_attempts(context),
        "prior_receipts": bounded_workday_mutation_receipts(
            ledger_context,
            packet_id=packet.id,
            limit=WORKDAY_OPERATION_LEDGER_LIMIT,
        ),
    }


@router.post("/projects/{project_id}/workday-consultations/claim-next")
@limiter.limit("120/minute")
async def claim_next_workday_consultation(
    request: Request,
    project_id: uuid.UUID,
    payload: ClaimNextWorkdayConsultation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Atomically lease one durable Fast-authored packet to current Workday."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Workday session is superseded or inactive")
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    accepted_incarnation = await _claim_current_fast_process_incarnation(
        session,
        project_id=project_id,
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    from app.conversation_coordinator import claim_next_workday_consultation_packet

    packet_claim = None
    resolved_envelope = None
    retired_handoffs = 0
    while True:
        packet_claim = await claim_next_workday_consultation_packet(
            session,
            project_id=project_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            worker_incarnation=accepted_incarnation,
            resident_session_id=payload.session_id,
            lease_seconds=payload.lease_seconds,
        )
        if packet_claim is None:
            break
        try:
            packet_row, _ = await _resolve_message_context_envelope(
                session,
                project_id=project_id,
                message_id=packet_claim.message.id,
            )
            resolved_envelope = await _rebind_private_packet_envelope_to_current_worker(
                session,
                project_id=project_id,
                row=packet_row,
                session_id=payload.session_id,
                worker_generation=payload.worker_generation,
                worker_incarnation=accepted_incarnation,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            await _retire_unavailable_private_handoff(
                session,
                row=packet_claim.message,
            )
            await _recover_retired_private_handoff(
                session,
                project=project,
                task=task,
                row=packet_claim.message,
                worker_generation=payload.worker_generation,
                worker_incarnation=accepted_incarnation,
                resident_session_id=payload.session_id,
            )
            logger.warning(
                "[STRATEGIC] Retired private Workday packet %s because its "
                "context envelope is unavailable",
                packet_claim.message.id,
            )
            retired_handoffs += 1
            resolved_envelope = None
            if retired_handoffs >= _PRIVATE_HANDOFF_RETIREMENT_LIMIT:
                logger.warning(
                    "[STRATEGIC] Paused private Workday packet retirement after "
                    "%s rows; the next poll will resume",
                    retired_handoffs,
                )
                packet_claim = None
                break
            continue
        break
    await session.commit()
    if packet_claim is None:
        return {"status": "empty"}
    return {
        "status": "claimed",
        "packet": _workday_consultation_packet_response(
            packet=packet_claim.message,
            workday_claim_id=packet_claim.workday_claim_id,
            lease_expires_at=packet_claim.lease_expires_at,
            attempt=packet_claim.attempt,
            envelope=resolved_envelope,
        ),
    }


@router.post("/projects/{project_id}/workday-consultations/{packet_id}/ack")
@limiter.limit("120/minute")
async def acknowledge_workday_consultation(
    request: Request,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    payload: AcknowledgeWorkdayConsultation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Complete or explicitly release an exact Workday packet lease."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Workday session is superseded or inactive")
    # Snapshot the ORM attribute into a local before any rollback in this
    # function: a rollback expires attached instances, and a bare `task.id`
    # read after that point requires an async refresh that is unreachable in
    # a synchronous attribute-access expression (MissingGreenlet).
    task_id = task.id
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task_id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    from app.conversation_coordinator import (
        acknowledge_workday_consultation_packet,
        record_workday_consultation_gate_failure,
    )

    try:
        await _resolve_message_context_envelope(
            session,
            project_id=project_id,
            message_id=packet_id,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            await record_workday_consultation_gate_failure(
                session,
                project_id=project_id,
                packet_id=packet_id,
                task_id=task_id,
                resident_session_id=payload.session_id,
                gate="envelope_resolution",
                detail=str(exc.detail),
            )
            await session.commit()
        raise

    try:
        duplicate = await acknowledge_workday_consultation_packet(
            session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
            resident_session_id=payload.session_id,
            workday_claim_id=payload.workday_claim_id,
            outcome=payload.outcome,
            result_summary=payload.result_summary,
            attempt_diagnostic=(
                payload.attempt_diagnostic.model_dump(mode="json")
                if payload.attempt_diagnostic is not None
                else None
            ),
        )
    except ValueError as exc:
        await session.rollback()
        await record_workday_consultation_gate_failure(
            session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            resident_session_id=payload.session_id,
            gate="acknowledge",
            detail=str(exc),
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    activity_transition = await _project_current_consultation_activity(
        session,
        project=project,
        resident_session_id=payload.session_id,
    )
    await session.commit()
    await _emit_consultation_activity_if_changed(
        project_id,
        task_id=task_id,
        activity=activity_transition,
    )
    return {"status": "duplicate" if duplicate else "acknowledged"}


@router.post("/projects/{project_id}/workday-consultations/{packet_id}/renew")
@limiter.limit("120/minute")
async def renew_workday_consultation(
    request: Request,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    payload: RenewWorkdayConsultation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Renew only a still-live, exact private Workday delivery lease."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Workday session is superseded or inactive")
    # Snapshot the ORM attribute into a local before any rollback in this
    # function: a rollback expires attached instances, and a bare `task.id`
    # read after that point requires an async refresh that is unreachable in
    # a synchronous attribute-access expression (MissingGreenlet).
    task_id = task.id
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task_id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    from app.conversation_coordinator import (
        record_workday_consultation_gate_failure,
        renew_workday_consultation_packet,
    )

    try:
        await _resolve_message_context_envelope(
            session,
            project_id=project_id,
            message_id=packet_id,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            await record_workday_consultation_gate_failure(
                session,
                project_id=project_id,
                packet_id=packet_id,
                task_id=task_id,
                resident_session_id=payload.session_id,
                gate="envelope_resolution",
                detail=str(exc.detail),
            )
            await session.commit()
        raise

    try:
        renewed = await renew_workday_consultation_packet(
            session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
            resident_session_id=payload.session_id,
            workday_claim_id=payload.workday_claim_id,
            lease_seconds=payload.lease_seconds,
        )
    except ValueError as exc:
        await session.rollback()
        await record_workday_consultation_gate_failure(
            session,
            project_id=project_id,
            packet_id=packet_id,
            task_id=task_id,
            resident_session_id=payload.session_id,
            gate="renew",
            detail=str(exc),
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return {
        "workday_claim_id": str(renewed.workday_claim_id),
        "lease_expires_at": utc_iso(renewed.lease_expires_at),
        "attempt": renewed.attempt,
    }


def _workday_continuation_response(
    claim: Any,
    envelope: Any,
    current_live_work_authority: dict[str, Any],
) -> dict[str, Any]:
    """Project a server-created result plus current bounded managed-state truth."""
    packet = claim.message
    context = packet.context if isinstance(packet.context, dict) else {}
    return {
        "result_packet_id": str(packet.id),
        "continuation_claim_id": str(claim.continuation_claim_id),
        "lease_expires_at": utc_iso(claim.lease_expires_at),
        "attempt": claim.attempt,
        "claim_id": context.get("claim_id"),
        "delivery_key": context.get("delivery_key"),
        "continuation_key": context.get("_conversation_consultation_continuation"),
        "consultation_id": context.get("_workday_consultation_atomic_reply_id"),
        "result_summary": packet.content,
        "envelope": envelope.model_dump(mode="json"),
        "current_live_work_authority": current_live_work_authority,
    }


@router.post("/projects/{project_id}/workday-consultations/continuations/claim-next")
@limiter.limit("120/minute")
async def claim_next_workday_continuation(
    request: Request,
    project_id: uuid.UUID,
    payload: ClaimWorkdayContinuation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Issue Fast an exact receipt for an eventual Workday result follow-up."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Fast session is superseded or inactive")
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    accepted_incarnation = await _claim_current_fast_process_incarnation(
        session,
        project_id=project_id,
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    from app.conversation_coordinator import claim_next_workday_consultation_continuation

    claim = None
    resolved_envelope = None
    retired_handoffs = 0
    missing_recovery_checked = False
    while True:
        claim = await claim_next_workday_consultation_continuation(
            session,
            project_id=project_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            worker_incarnation=accepted_incarnation,
            resident_session_id=payload.session_id,
            lease_seconds=payload.lease_seconds,
        )
        if claim is None:
            if not missing_recovery_checked:
                missing_recovery_checked = True
                from app.strategic_workday_handoff_recovery import (
                    recover_one_missing_workday_request,
                )

                recovered = await recover_one_missing_workday_request(
                    session,
                    project=project,
                    task=task,
                    worker_generation=payload.worker_generation,
                    worker_incarnation=accepted_incarnation,
                    session_id=payload.session_id,
                )
                if recovered is not None:
                    continue
            break
        try:
            _, resolved_envelope = await _resolve_message_context_envelope(
                session,
                project_id=project_id,
                message_id=claim.message.id,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            await _retire_unavailable_private_handoff(
                session,
                row=claim.message,
            )
            await _recover_retired_private_handoff(
                session,
                project=project,
                task=task,
                row=claim.message,
                worker_generation=payload.worker_generation,
                worker_incarnation=accepted_incarnation,
                resident_session_id=payload.session_id,
            )
            logger.warning(
                "[STRATEGIC] Retired private Workday continuation %s because "
                "its context envelope is unavailable",
                claim.message.id,
            )
            retired_handoffs += 1
            resolved_envelope = None
            if retired_handoffs >= _PRIVATE_HANDOFF_RETIREMENT_LIMIT:
                logger.warning(
                    "[STRATEGIC] Paused private Workday continuation retirement "
                    "after %s rows; the next poll will resume",
                    retired_handoffs,
                )
                claim = None
                break
            continue
        break
    if claim is None:
        await session.commit()
        return {"status": "empty"}
    from app.strategic_workday_mutation_rebind import (
        bounded_workday_mutation_receipts,
        workday_mutation_receipts_have_more,
    )

    result_context = claim.message.context if isinstance(claim.message.context, dict) else {}
    try:
        source_packet_id = uuid.UUID(str(result_context.get("source_packet_id")))
        workday_claim_id = uuid.UUID(
            str(result_context.get("_workday_consultation_ack_claim_id"))
        )
    except (TypeError, ValueError, AttributeError):
        source_packet_id = workday_claim_id = None
    mutation_receipts = bounded_workday_mutation_receipts(
        result_context,
        packet_id=source_packet_id,
        workday_claim_id=workday_claim_id,
        task_id=task.id,
        resident_session_id=payload.session_id,
        worker_generation=payload.worker_generation,
        worker_incarnation=accepted_incarnation,
    )
    current_live_work_authority = await _fast_live_work_authority(
        session,
        project=project,
        resident_task_id=task.id,
        result_evidence_text=claim.message.content,
        consultation_operation_receipts=mutation_receipts,
    )
    current_live_work_authority["consultation_mutation_receipts"] = mutation_receipts
    current_live_work_authority.setdefault("collection_metadata", {})[
        "consultation_mutation_receipts"
    ] = {
        "returned_count": len(mutation_receipts),
        "has_more": workday_mutation_receipts_have_more(result_context),
    }
    await session.commit()
    return {
        "status": "claimed",
        "continuation": _workday_continuation_response(
            claim,
            resolved_envelope,
            current_live_work_authority,
        ),
    }


@router.post("/projects/{project_id}/workday-consultations/continuations/{result_packet_id}/complete")
@limiter.limit("120/minute")
async def complete_workday_continuation(
    request: Request,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    payload: CompleteWorkdayContinuation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Commit the only public reply allowed for a result-continuation receipt."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Fast session is superseded or inactive")
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    # A response can be lost after the transaction committed.  This replay has
    # no need to resolve (or expose) the derived envelope: the public text and
    # continuation receipt are already durable.  Keep the comparison stricter
    # than the coordinator's general idempotency helper because this route has
    # a result-packet path parameter as part of its public contract.
    normalized_idempotency_key = payload.idempotency_key.strip()
    normalized_content = payload.content.strip()
    existing_completion = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == normalized_idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing_completion is not None:
        existing_context = (
            existing_completion.context
            if isinstance(existing_completion.context, dict)
            else {}
        )
        if (
            existing_completion.from_type != "strategic"
            or existing_completion.from_task_id != task.id
            or existing_completion.content != normalized_content
            or existing_completion.resident_session_id != payload.session_id
            or existing_context.get("_workday_continuation_claim_id")
            != str(payload.continuation_claim_id)
            or existing_context.get("_workday_result_packet_id")
            != str(result_packet_id)
            or existing_context.get("_conversation_public_reply_owner") != "fast"
        ):
            raise HTTPException(
                status_code=409,
                detail="idempotency_key already belongs to a different continuation",
            )
        # Release the Project/Task locks before realtime delivery. The strict
        # identity comparison above proves this is the canonical committed
        # continuation, so replaying its row-id event is safe and closes the
        # commit-succeeded/event-lost window without creating any new output.
        activity_transition = await _project_current_consultation_activity(
            session,
            project=project,
            resident_session_id=payload.session_id,
        )
        await session.commit()
        await _emit_consultation_activity_if_changed(
            project_id,
            task_id=task.id,
            activity=activity_transition,
        )
        await broadcast_to_project(
            str(project_id),
            "message_created",
            message_event_payload(existing_completion),
        )
        return {"id": str(existing_completion.id), "status": "duplicate"}
    await _resolve_message_context_envelope(
        session,
        project_id=project_id,
        message_id=result_packet_id,
    )
    from app.conversation_coordinator import complete_workday_consultation_continuation

    try:
        result = await complete_workday_consultation_continuation(
            session,
            project_id=project_id,
            result_packet_id=result_packet_id,
            continuation_claim_id=payload.continuation_claim_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
            resident_session_id=payload.session_id,
            content=payload.content,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    activity_transition = await _project_current_consultation_activity(
        session,
        project=project,
        resident_session_id=payload.session_id,
    )
    await session.commit()
    await _emit_consultation_activity_if_changed(
        project_id,
        task_id=task.id,
        activity=activity_transition,
    )
    await broadcast_to_project(
        str(project_id), "message_created", message_event_payload(result.message)
    )
    return {"id": str(result.message.id), "status": "duplicate" if result.duplicate else "created"}


@router.post(
    "/projects/{project_id}/workday-consultations/continuations/"
    "{result_packet_id}/renew"
)
@limiter.limit("120/minute")
async def renew_workday_continuation(
    request: Request,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    payload: RenewWorkdayContinuation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Renew one exact, current Fast continuation receipt."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=409, detail="Fast session is superseded or inactive"
        )
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    await _resolve_message_context_envelope(
        session,
        project_id=project_id,
        message_id=result_packet_id,
    )
    from app.conversation_coordinator import (
        renew_workday_consultation_continuation as renew_continuation,
    )

    try:
        renewed = await renew_continuation(
            session,
            project_id=project_id,
            result_packet_id=result_packet_id,
            continuation_claim_id=payload.continuation_claim_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
            resident_session_id=payload.session_id,
            lease_seconds=payload.lease_seconds,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return {
        "continuation_claim_id": str(renewed.continuation_claim_id),
        "lease_expires_at": utc_iso(renewed.lease_expires_at),
        "attempt": renewed.attempt,
    }


@router.post(
    "/projects/{project_id}/workday-consultations/continuations/"
    "{result_packet_id}/release"
)
@limiter.limit("120/minute")
async def release_workday_continuation(
    request: Request,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    payload: ReleaseWorkdayContinuation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Release one failed continuation receipt, idempotently."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    task = (
        await session.execute(
            select(Task).where(Task.id == payload.task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or task is None or task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=409, detail="Fast session is superseded or inactive"
        )
    _require_current_fast_task_identity(
        task=task,
        project=project,
        worker_generation=payload.worker_generation,
        session_id=payload.session_id,
    )
    _require_current_fast_process_incarnation(
        project=project,
        task_id=task.id,
        worker_generation=payload.worker_generation,
        incarnation_id=payload.incarnation_id,
    )
    await _resolve_message_context_envelope(
        session,
        project_id=project_id,
        message_id=result_packet_id,
    )
    from app.conversation_coordinator import (
        release_workday_consultation_continuation as release_continuation,
    )

    try:
        status = await release_continuation(
            session,
            project_id=project_id,
            result_packet_id=result_packet_id,
            continuation_claim_id=payload.continuation_claim_id,
            task_id=task.id,
            worker_generation=payload.worker_generation,
            worker_incarnation=payload.incarnation_id,
            resident_session_id=payload.session_id,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return {"status": status}


class PermanentWorkdayArtifactError(ValueError):
    def __init__(self, message_id: uuid.UUID, reason: str):
        super().__init__(reason)
        self.message_id = message_id
        self.reason = reason


class TransientWorkdayArtifactError(RuntimeError):
    pass


class WorkdayArtifactBatchLimit(RuntimeError):
    def __init__(self, first_excluded_message_id: uuid.UUID):
        super().__init__("required image attachment batch limit reached")
        self.first_excluded_message_id = first_excluded_message_id


def _parse_persisted_worker_generation(value: Any) -> int | None:
    """Parse persisted Resident authority, defaulting only missing state to 1."""
    if value is None:
        return 1
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        generation = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            generation = int(raw)
        except ValueError:
            return None
    else:
        return None
    return generation if generation >= 1 else None


async def _lock_resident_poll_project(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Freshen and share-lock canonical Resident authority through the poll."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_resident_claim_project(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Exclusively lock conversation authority across validation and claim."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _parse_since(value: str | None) -> datetime | None:
    """Parse the ``since`` cursor into naive-UTC for comparison.

    ``ProjectMessage.created_at`` is stored via ``NaiveUTCDateTime`` (tz stripped,
    values are UTC), so a tz-aware ISO cursor is converted to UTC and made naive
    before comparison; a naive cursor is used as-is (already assumed UTC). An
    unparseable value is a client error (400), not a silent no-op, so a broken
    cursor can't quietly re-stream the whole thread every poll.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid 'since' timestamp (expected ISO-8601)",
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed

def _poll_message_ordering() -> tuple[Any, ...]:
    """One deterministic order for initial selection and the final fence."""
    return (
        ProjectMessage.created_at.asc(),
        ProjectMessage.sequence.asc().nulls_last(),
        ProjectMessage.id.asc(),
    )


def _poll_message_dict(message: ProjectMessage) -> dict[str, Any]:
    """Project one message to the exact inbound-poll shape the contract fixes.

    Reuses ``project_message_base`` for enum/timestamp/empty-context
    normalization, then narrows to the seven contract fields so no extra state
    (project_id, from_task_id, resolution, ...) leaks onto the agent surface.
    """
    base = project_message_base(message)
    return {
        "id": base["id"],
        "created_at": base["created_at"],
        "from_type": base["from_type"],
        "status": base["status"],
        "message_type": base["message_type"],
        "content": base["content"],
        "context": base["context"],
    }


def _ordered_delivery_attachments(
    context: object,
    artifact_attachments: Sequence[dict[str, Any]],
    *,
    degraded: bool,
) -> list[dict[str, Any]]:
    """Mirror the exact poll order used by the signed Fast attachment manifest."""

    if degraded:
        return []
    raw_ordinary = context.get("attachments") if isinstance(context, dict) else None
    ordinary = list(raw_ordinary) if isinstance(raw_ordinary, list) else []
    return ordinary + list(artifact_attachments)


async def _canonical_delivery_attachment_manifests(
    session: AsyncSession,
    project_id: uuid.UUID,
    rows: Sequence[ProjectMessage],
) -> dict[str, list[dict[str, Any]]]:
    """Bind exactly the attachments the poll will expose, in poll order."""

    from app.strategic_memory_envelope import canonical_attachment_manifest

    artifacts_degraded = False
    try:
        artifact_attachments = await _materialize_workday_artifact_attachments(
            session,
            project_id,
            list(rows),
        )
    except WorkdayArtifactBatchLimit:
        # Recovery of an immutable oversized claim keeps every text row but
        # deliberately omits all attachments, matching the poll projection.
        artifact_attachments = {}
        artifacts_degraded = True

    manifests: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row_context = row.context if isinstance(row.context, dict) else {}
        delivered_attachments = _ordered_delivery_attachments(
            row_context,
            artifact_attachments.get(row.id, []),
            degraded=artifacts_degraded,
        )
        manifests[str(row.id)] = canonical_attachment_manifest(delivered_attachments)
    return manifests


def _validated_sha256(value: Any, *, field: str) -> str:
    checksum = str(value or "")
    if _LOWERCASE_SHA256_RE.fullmatch(checksum) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return checksum


def _is_agent_compatible_signed_image_url(value: str) -> bool:
    """Mirror the resident agent's signed-GCS attachment transport allowlist."""
    try:
        parsed = urlparse(value)
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or port is not None:
        return False
    if hostname != "storage.googleapis.com" and not hostname.endswith(
        ".storage.googleapis.com"
    ):
        return False
    signatures = parse_qs(parsed.query, keep_blank_values=True).get(
        "X-Goog-Signature", []
    )
    return len(signatures) == 1 and bool(signatures[0].strip())



async def _materialize_workday_artifact_attachments(
    session: AsyncSession,
    project_id: uuid.UUID,
    messages: list[ProjectMessage],
    image_budget_exempt_message_ids: frozenset[uuid.UUID] = frozenset(),
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Resolve message-granted immutable tool revisions into model attachments."""
    refs_by_message: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
    granted_ids: set[uuid.UUID] = set()
    for message in messages:
        context = message.context if isinstance(message.context, dict) else {}
        raw_refs = context.get("artifact_refs")
        if raw_refs is None:
            continue
        if not isinstance(raw_refs, list):
            raise PermanentWorkdayArtifactError(
                message.id, "artifact_refs must be a list"
            )
        parsed: list[uuid.UUID] = []
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, dict):
                raise PermanentWorkdayArtifactError(
                    message.id, "artifact_ref grant must be an object"
                )
            if "artifact_ref_id" not in raw_ref:
                continue
            raw_id = raw_ref.get("artifact_ref_id")
            try:
                ref_id = uuid.UUID(str(raw_id))
            except (TypeError, ValueError):
                raise PermanentWorkdayArtifactError(
                    message.id, "artifact_ref grant identity is invalid"
                ) from None
            parsed.append(ref_id)
        canonical = artifact_ref_grants_from_message_context(context)
        if canonical != frozenset(parsed) or len(canonical) != len(parsed):
            raise PermanentWorkdayArtifactError(
                message.id, "artifact_ref grant projection is inconsistent"
            )
        refs_by_message[message.id] = tuple(parsed)
        granted_ids.update(parsed)

    if not granted_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ArtifactRef).where(
                    ArtifactRef.project_id == project_id,
                    ArtifactRef.id.in_(granted_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    refs = {row.id: row for row in rows}
    missing_ids = granted_ids - set(refs)
    if missing_ids:
        for message_id, message_ref_ids in refs_by_message.items():
            if missing_ids.intersection(message_ref_ids):
                raise PermanentWorkdayArtifactError(
                    message_id,
                    "one or more promised artifact revisions are unavailable",
                )

    from app.storage import (
        StorageUriError,
        artifact_object_matches_receipt,
        artifact_ref_render_uri,
        generate_download_signed_url,
    )

    materialized: dict[uuid.UUID, list[dict[str, Any]]] = {}
    delivered_required_images = 0
    for message_id, message_ref_ids in refs_by_message.items():
        required_image_count = sum(
            1
            for ref_id in message_ref_ids
            if refs[ref_id].kind == "whiteboard"
        )
        if required_image_count > _WORKDAY_MAX_REQUIRED_IMAGES:
            raise PermanentWorkdayArtifactError(
                message_id, "required image attachment limit exceeded"
            )
        if message_id not in image_budget_exempt_message_ids:
            if (
                delivered_required_images + required_image_count
                > _WORKDAY_MAX_REQUIRED_IMAGES
            ):
                raise WorkdayArtifactBatchLimit(message_id)
            delivered_required_images += required_image_count
        attachments: list[dict[str, Any]] = []
        for ref_id in message_ref_ids:
            ref = refs[ref_id]
            try:
                checksum = _validated_sha256(
                    ref.checksum, field="artifact revision checksum"
                )
            except ValueError as exc:
                raise PermanentWorkdayArtifactError(message_id, str(exc)) from None
            snapshot = (
                ref.content_snapshot
                if isinstance(ref.content_snapshot, dict)
                else {}
            )
            retrieval = (
                ref.retrieval_metadata
                if isinstance(ref.retrieval_metadata, dict)
                else {}
            )
            if ref.kind == "notes":
                text = snapshot.get("text")
                if not isinstance(text, str):
                    raise PermanentWorkdayArtifactError(
                        message_id, "Notes artifact revision is corrupt"
                    )
                truncated = snapshot.get("truncated")
                original_char_count = snapshot.get("original_char_count")
                if (
                    retrieval.get("capability") != "inline_bounded_text"
                    or not isinstance(truncated, bool)
                    or isinstance(original_char_count, bool)
                    or not isinstance(original_char_count, int)
                    or original_char_count < len(text)
                    or retrieval.get("content_truncated") is not truncated
                    or (truncated and original_char_count <= len(text))
                    or (not truncated and original_char_count != len(text))
                ):
                    raise PermanentWorkdayArtifactError(
                        message_id, "Notes artifact bounds metadata is corrupt"
                    )
                delivered_text = text
                if truncated:
                    delivered_text += (
                        "\n\n[Artifact snapshot truncated: showing the first "
                        f"{len(text)} of {original_char_count} characters.]"
                    )
                attachments.append(
                    {
                        "type": "file",
                        "mime_type": "text/markdown",
                        "name": f"Notes revision {ref.revision}.md",
                        "data": base64.b64encode(
                            delivered_text.encode("utf-8")
                        ).decode("ascii"),
                        "artifact_ref_id": str(ref.id),
                        "checksum": checksum,
                        "truncated": truncated,
                        "snapshot_char_count": len(text),
                        "original_char_count": original_char_count,
                    }
                )
            elif ref.kind == "whiteboard":
                storage_uri = retrieval.get("storage_uri")
                mime_type = str(retrieval.get("mime_type") or "").lower()
                try:
                    image_checksum = _validated_sha256(
                        retrieval.get("image_checksum"),
                        field="Whiteboard image checksum",
                    )
                except ValueError as exc:
                    raise PermanentWorkdayArtifactError(
                        message_id, str(exc)
                    ) from None
                if not isinstance(storage_uri, str) or not storage_uri:
                    raise PermanentWorkdayArtifactError(
                        message_id, "Whiteboard artifact render is missing"
                    )
                if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise PermanentWorkdayArtifactError(
                        message_id, "Whiteboard artifact render type is invalid"
                    )
                size_bytes = retrieval.get("size_bytes")
                if (
                    isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 1
                ):
                    raise PermanentWorkdayArtifactError(
                        message_id, "Whiteboard artifact size receipt is invalid"
                    )
                if size_bytes > _WORKDAY_MAX_IMAGE_DOWNLOAD_BYTES:
                    raise PermanentWorkdayArtifactError(
                        message_id,
                        "Whiteboard artifact render exceeds the Workday image limit",
                    )
                if retrieval.get("capability") != "project_tool_rendered_image":
                    raise PermanentWorkdayArtifactError(
                        message_id, "Whiteboard artifact capability is invalid"
                    )
                try:
                    attachment_id = uuid.UUID(
                        str(retrieval.get("attachment_id"))
                    )
                except (TypeError, ValueError):
                    raise PermanentWorkdayArtifactError(
                        message_id,
                        "Whiteboard artifact receipt identity is invalid",
                    ) from None
                generation = retrieval.get("generation")
                if (
                    generation is not None
                    and (
                        isinstance(generation, bool)
                        or not isinstance(generation, int)
                        or generation < 1
                    )
                ):
                    raise PermanentWorkdayArtifactError(
                        message_id,
                        "Whiteboard artifact generation receipt is invalid",
                    )
                expected_storage_uri = artifact_ref_render_uri(
                    project_id,
                    ref.tool_id,
                    ref.revision,
                    attachment_id,
                )
                if storage_uri != expected_storage_uri:
                    raise PermanentWorkdayArtifactError(
                        message_id,
                        "Whiteboard artifact storage identity is invalid",
                    )
                try:
                    object_exists = await artifact_object_matches_receipt(
                        storage_uri,
                        expected_size_bytes=size_bytes,
                        expected_generation=generation,
                    )
                except StorageUriError as exc:
                    raise PermanentWorkdayArtifactError(
                        message_id, str(exc)
                    ) from None
                except Exception as exc:
                    raise TransientWorkdayArtifactError(
                        "Whiteboard artifact metadata is temporarily unavailable"
                    ) from exc
                if not object_exists:
                    raise PermanentWorkdayArtifactError(
                        message_id, "Whiteboard artifact render is missing"
                    )
                try:
                    signed_url = await generate_download_signed_url(
                        storage_uri, expiration_minutes=10
                    )
                except Exception as exc:
                    raise TransientWorkdayArtifactError(
                        "Whiteboard artifact signing is temporarily unavailable"
                    ) from exc
                if not signed_url or not _is_agent_compatible_signed_image_url(
                    signed_url
                ):
                    raise TransientWorkdayArtifactError(
                        "Whiteboard artifact render URL is not accepted by the Resident"
                    )
                attachments.append(
                    {
                        "type": "image",
                        "mime_type": mime_type,
                        "name": f"Whiteboard revision {ref.revision}",
                        "url": signed_url,
                        "artifact_ref_id": str(ref.id),
                        "required": True,
                        "sha256": image_checksum,
                        "checksum": checksum,
                        "image_checksum": image_checksum,
                    }
                )
            else:
                raise PermanentWorkdayArtifactError(
                    message_id, "unsupported artifact revision kind"
                )
        materialized[message_id] = attachments
    return materialized

async def _quarantine_permanent_artifact_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    failure: PermanentWorkdayArtifactError,
) -> tuple[ProjectMessage | None, ProjectMessage | None]:
    """Atomically supersede one poison message and create an exact-once notice."""
    source = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.id == failure.message_id,
                    ProjectMessage.project_id == project_id,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one_or_none()
    )
    if (
        source is None
        or source.status != "open"
        or source.handling_state != MessageHandlingState.PENDING
    ):
        return None, None
    source_context = source.context if isinstance(source.context, dict) else {}
    observed_artifact_refs = copy.deepcopy(source_context.get("artifact_refs"))
    try:
        await _materialize_workday_artifact_attachments(
            session, project_id, [source]
        )
    except PermanentWorkdayArtifactError as current_failure:
        failure = current_failure
    else:
        return None, None

    # Release the poll's shared Project authority lock before taking the
    # canonical Project -> message exclusive mutation order. The network
    # metadata check above never runs while exclusive locks are held.
    await session.rollback()
    await session.execute(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    source = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.id == failure.message_id,
                    ProjectMessage.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one_or_none()
    )
    current_context = (
        source.context
        if source is not None and isinstance(source.context, dict)
        else {}
    )
    if (
        source is None
        or source.status != "open"
        or source.handling_state != MessageHandlingState.PENDING
        or current_context.get("artifact_refs") != observed_artifact_refs
    ):
        return None, None
    notice_key = f"artifact-handoff-failed:{source.id}"
    existing_notice = (
        (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.idempotency_key == notice_key,
                )
            )
        )
        .scalars()
        .one_or_none()
    )

    now = datetime.now(timezone.utc)
    source_context = dict(source.context or {})
    source_context["artifact_handoff_failure"] = {
        "version": 1,
        "state": "quarantined",
        "reason": failure.reason,
        "workday_inspected": False,
        "repair_action": "re-share the tool revision",
    }
    source.context = source_context
    source.status = "resolved"
    source.handling_state = MessageHandlingState.SUPERSEDED
    source.handled_by_task_id = None
    source.resolution = (
        "Workday did not receive or inspect the promised tool revision; "
        "re-share the revision."
    )
    source.resolved_at = now
    session.add(source)

    created_notice: ProjectMessage | None = None
    if existing_notice is None:
        created_notice = await build_project_message(
            session,
            project_id=project_id,
            surface=MessageSurface.CONVERSATION,
            handling_state=MessageHandlingState.RESPONDED,
            message_type=MessageType.NOTE,
            from_type="system",
            content=(
                "Workday did not receive or inspect the promised tool revision. "
                "The message was not answered; re-share the Notes or Whiteboard "
                "revision."
            ),
            context=stamp_message_audience({
                "event_type": "artifact_handoff_failed",
                "source_message_id": str(source.id),
                "workday_inspected": False,
                "reason": failure.reason,
            }, "artifact_handoff_failed", message_type="NOTE"),
            status="resolved",
            resolution="Operator-visible artifact handoff failure notice",
            reply_to_message_id=source.id,
            resident_session_id=source.resident_session_id,
            resolved_at=now,
            idempotency_key=notice_key,
        )
        session.add(created_notice)
        await session.flush()
    return source, created_notice


@router.post("/projects/{project_id}/messages/claim")
@limiter.limit(_RESIDENT_POLL_RATE_LIMIT, key_func=_resident_poll_rate_limit_key)
async def claim_resident_messages(
    request: Request,
    project_id: uuid.UUID,
    payload: ClaimResidentMessages,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Atomically admit one read-poll batch to an exact Resident generation."""
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    # Resolve project membership before taking the exclusive authority lock.
    await _resolve_agent_project(project_id, token, session)
    await session.rollback()
    project = await _lock_resident_claim_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == payload.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    task_context = task.context if task is not None else {}
    authenticated_session_id = str(
        (task_context or {}).get("strategic_session_id") or ""
    )
    authenticated_generation = _parse_persisted_worker_generation(
        (task_context or {}).get("_resident_worker_generation")
    )
    resident_blob = (
        (project.vision or {}).get("_resident_strategic_session") or {}
    )
    current_generation = _parse_persisted_worker_generation(
        resident_blob.get("worker_generation")
    )
    if (
        task is None
        or task.status not in {TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING}
        or (task_context or {}).get("_resident_session") is not True
        or authenticated_session_id != payload.session_id
        or authenticated_generation != payload.worker_generation
        or current_generation != payload.worker_generation
        or resident_blob.get("status") != "active"
        or bool(resident_blob.get("stop_requested"))
        or str(resident_blob.get("session_id") or "") != payload.session_id
        or str(resident_blob.get("task_id") or "") != str(payload.task_id)
    ):
        raise HTTPException(
            status_code=409,
            detail="Resident worker generation superseded or inactive",
        )

    from app.strategic_resident_session import (
        claim_resident_worker_incarnation,
        read_resident_call,
        resident_call_is_active,
    )

    if resident_call_is_active(read_resident_call(project.vision)):
        raise HTTPException(
            status_code=409,
            detail="Fast Resident owns public replies while a call is active",
        )
    from app.task_locking import with_vision_locked

    accepted_resident_incarnation: uuid.UUID | None = None

    def _claim_process_incarnation(vision: dict) -> None:
        nonlocal accepted_resident_incarnation
        accepted_resident_incarnation = claim_resident_worker_incarnation(
            vision,
            task_id=payload.task_id,
            worker_generation=payload.worker_generation,
            incarnation_id=payload.resident_incarnation_id,
        )
        if accepted_resident_incarnation is None:
            raise HTTPException(
                status_code=409,
                detail="Resident process incarnation superseded",
            )

    await with_vision_locked(
        session,
        project_id,
        _claim_process_incarnation,
        _locked_project=project,
    )

    from app.conversation_coordinator import (
        OperatorTurnParked,
        claim_operator_messages,
        legacy_conversation_claim_incarnation,
    )

    try:
        claim = await claim_operator_messages(
            session,
            project_id=project_id,
            session_id=payload.session_id,
            task_id=payload.task_id,
            worker_generation=payload.worker_generation,
            incarnation_id=(
                payload.incarnation_id
                or legacy_conversation_claim_incarnation(
                    payload.task_id,
                    payload.worker_generation,
                )
            ),
            message_ids=payload.message_ids,
            lease_seconds=payload.lease_seconds,
            conversation_floor_sequence=resident_conversation_floor_sequence(
                resident_blob
            ),
        )
    except OperatorTurnParked as parked:
        await session.commit()
        raise HTTPException(status_code=409, detail=parked.reason) from None
    if claim is None:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Operator batch was already claimed or changed",
        )
    await session.commit()
    return {
        "claim_id": str(claim.claim_id),
        "message_ids": [str(row.id) for row in claim.rows],
        "lease_expires_at": utc_iso(claim.lease_expires_at),
        "attempt": claim.attempt,
        "incarnation_supported": True,
        "incarnation_id": str(
            payload.incarnation_id
            or legacy_conversation_claim_incarnation(
                payload.task_id,
                payload.worker_generation,
            )
        ),
        "resident_incarnation_id": str(accepted_resident_incarnation),
    }


@router.get("/projects/{project_id}/messages")
@limiter.limit(_RESIDENT_POLL_RATE_LIMIT, key_func=_resident_poll_rate_limit_key)
async def agent_poll_messages(
    request: Request,
    project_id: uuid.UUID,
    since: str | None = Query(default=None),
    from_type: str = Query(default="user"),
    status: str = Query(default="open"),
    session_id: str | None = Query(default=None),
    worker_generation: int | None = Query(default=None, ge=1),
    limit: int = Query(
        default=_MESSAGES_POLL_DEFAULT_LIMIT,
        ge=1,
        le=_MESSAGES_POLL_MAX_LIMIT,
    ),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return operator messages at-or-after ``since`` for the strategic agent.

    Filters on ``project_id`` + ``from_type`` (default ``user``) + ``status``
    (default ``open``) and, when a cursor is supplied, ``created_at >= since``
    (INCLUSIVE); orders ``created_at`` ascending and caps at ``limit`` — served
    by the ``ix_projectmessage_project_created_at`` index. The agent dedupes by
    message ``id`` and advances its cursor to the MAX returned ``created_at``, so
    the inclusive boundary re-delivers only the message(s) on the cursor instant
    (dropped by id) instead of skipping the un-returned remainder of a truncated
    page or a message committed at-or-before the cursor. ``server_time`` is
    captured BEFORE the query and returned so a cold client with no messages yet
    still has a monotonic cursor to start from.

    Cross-project access returns 404 via ``_resolve_agent_project`` (probing
    can't enumerate other projects' ids).
    """
    project = await _resolve_agent_project(project_id, token, session)
    authenticated_task_id = uuid.UUID(str(token.get("sub")))
    authenticated_task = await session.get(Task, authenticated_task_id)
    task_context = (
        authenticated_task.context if authenticated_task is not None else {}
    )
    resident_poll = bool(task_context and task_context.get("_resident_session") is True)
    conversation_floor_sequence: int | None = None
    open_message_cursor_floor: str | None = None
    if resident_poll:
        # Resident replacement takes the Project row exclusively. Hold a fresh
        # shared lock through this poll so validation and message selection are
        # one authority boundary, never validate-gen1/read-after-gen2.
        project = await _lock_resident_poll_project(session, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        authenticated_task = await session.get(
            Task, authenticated_task_id, populate_existing=True
        )
        task_context = (
            authenticated_task.context if authenticated_task is not None else {}
        )
        authenticated_session_id = str(
            task_context.get("strategic_session_id") or ""
        )
        authenticated_generation = _parse_persisted_worker_generation(
            task_context.get("_resident_worker_generation")
        )
        if (
            task_context.get("_resident_session") is not True
            or not authenticated_session_id
            or authenticated_generation is None
            or (session_id is not None and session_id != authenticated_session_id)
            or (
                worker_generation is not None
                and worker_generation != authenticated_generation
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker identity mismatch",
            )

        resident_blob = (
            (project.vision or {}).get("_resident_strategic_session") or {}
        )
        conversation_floor_sequence = resident_conversation_floor_sequence(
            resident_blob
        )
        current_generation = _parse_persisted_worker_generation(
            resident_blob.get("worker_generation")
        )
        if (
            resident_blob.get("status") != "active"
            or bool(resident_blob.get("stop_requested"))
            or str(resident_blob.get("session_id") or "")
            != authenticated_session_id
            or str(resident_blob.get("task_id") or "")
            != str(authenticated_task_id)
            or current_generation is None
            or current_generation != authenticated_generation
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker generation superseded or inactive",
            )
        # The call coordinator owns finalized inputs and every public reply
        # while its nested lease is live.  Suppress ordinary Workday pickup at
        # the source instead of allowing the legacy turn loop to consume a
        # message and then fail later at the reply CAS.
        from app.strategic_resident_session import resident_call_is_active

        active_call = resident_blob.get("call")
        if (
            resident_call_is_active(active_call)
            and str((active_call or {}).get("worker_task_id"))
            == str(authenticated_task_id)
        ):
            open_message_cursor_floor = await _fetch_open_operator_cursor_floor(
                session,
                project.id,
                resident_session_id=authenticated_session_id,
                conversation_floor_sequence=conversation_floor_sequence,
            )
            await session.commit()
            return {
                "messages": [],
                "server_time": datetime.now(timezone.utc).isoformat(),
                "call_active": True,
                "open_message_cursor_floor": open_message_cursor_floor,
            }
        # Caller-supplied scope is advisory only; authenticated task authority
        # always selects the current Resident conversation.
        session_id = authenticated_session_id
        # Remote artifact metadata/signing must never hold Project authority
        # locks. A final authority/fingerprint fence below validates the page.
        await session.commit()

    server_time = datetime.now(timezone.utc)
    since_dt = _parse_since(since)

    conditions = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.from_type == from_type,
        ProjectMessage.status == status,
    ]
    if since_dt is not None:
        # INCLUSIVE boundary (>=, not >): the agent dedupes by id and advances
        # its cursor to the max returned created_at, so re-scanning the cursor
        # instant closes both the >limit truncation skip and the commit-race
        # skip a strict > would open (see module docstring).
        conditions.append(ProjectMessage.created_at >= since_dt)
    if session_id:
        conditions.append(
            resident_broad_message_scope(
                session_id,
                conversation_floor_sequence=conversation_floor_sequence,
            )
        )

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(*conditions)
                .order_by(*_poll_message_ordering())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # A crashed Fast process may leave one immutable claimed turn in front of
    # newer unclaimed input.  Ordinary paging must never cut that turn: the
    # claim coordinator will (correctly) reject a subset forever.  Once any
    # member reaches the resident page, replace the ordinary page with the
    # exact full open group.  Typed claims are created from this endpoint's
    # bounded page, so a valid group fits within the same server cap.
    resident_claim_group_ids: list[uuid.UUID] = []
    resident_claim_group_id: uuid.UUID | None = None
    resident_claim_group_key: str | None = None
    resident_claim_group_selected = False
    resident_claim_artifacts_degraded = False
    if resident_poll:
        claimed_head = next(
            (
                row
                for row in rows
                if row.conversation_claim_id is not None
                and (row.context or {}).get("_conversation_public_reply_owner")
                == "fast"
                and isinstance(
                    (row.context or {}).get(
                        "_conversation_claim_idempotency_key"
                    ),
                    str,
                )
            ),
            None,
        )
        if claimed_head is not None:
            resident_claim_group_id = claimed_head.conversation_claim_id
            claim_group_conditions = [
                ProjectMessage.project_id == project_id,
                ProjectMessage.conversation_claim_id
                == claimed_head.conversation_claim_id,
                ProjectMessage.from_type == from_type,
                ProjectMessage.status == status,
            ]
            if session_id:
                claim_group_conditions.append(
                    resident_conversation_scope(
                        session_id,
                        conversation_floor_sequence=conversation_floor_sequence,
                    )
                )
            exact_claim_rows = (
                (
                    await session.execute(
                        select(ProjectMessage)
                        .where(*claim_group_conditions)
                        .order_by(*_poll_message_ordering())
                    )
                )
                .scalars()
                .all()
            )
            resident_claim_group_ids = [row.id for row in exact_claim_rows]
            raw_claim_key = (claimed_head.context or {}).get(
                "_conversation_claim_idempotency_key"
            )
            resident_claim_group_key = (
                raw_claim_key.strip()
                if isinstance(raw_claim_key, str) and raw_claim_key.strip()
                else None
            )
            if exact_claim_rows and len(exact_claim_rows) <= _MESSAGES_POLL_MAX_LIMIT:
                rows = exact_claim_rows
                resident_claim_group_selected = True

    remaining_rows = list(rows)
    while True:
        try:
            artifact_attachments = await _materialize_workday_artifact_attachments(
                session,
                project_id,
                remaining_rows,
                image_budget_exempt_message_ids=frozenset(
                    row.id
                    for row in remaining_rows
                    if (
                        not resident_claim_group_selected
                        and since_dt is not None
                        and row.created_at <= since_dt
                    )
                ),
            )
            break
        except WorkdayArtifactBatchLimit as batch_limit:
            if resident_claim_group_selected:
                # Membership recovery is atomic: shrinking an old Fast turn
                # would make its immutable coordinator claim unrecoverable.
                # Preserve every text row and explicitly degrade attachments;
                # Fast can then ask for a resend or consult Workday instead of
                # polling the same impossible prefix forever.
                artifact_attachments = {}
                resident_claim_artifacts_degraded = True
                break
            prefix: list[ProjectMessage] = []
            for row in remaining_rows:
                if row.id == batch_limit.first_excluded_message_id:
                    break
                prefix.append(row)
            if not prefix:
                raise HTTPException(
                    status_code=503,
                    detail="Artifact delivery batch could not make progress",
                    headers={"Retry-After": "1"},
                ) from None
            remaining_rows = prefix
        except PermanentWorkdayArtifactError as failure:
            try:
                source, notice = await _quarantine_permanent_artifact_message(
                    session, project_id, failure
                )
            except Exception:
                logger.warning(
                    "Workday artifact revalidation failed transiently for project %s; "
                    "poll will retry",
                    project_id,
                    exc_info=True,
                )
                await session.rollback()
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Exact tool revision is temporarily unavailable; "
                        "retry the message poll"
                    ),
                    headers={"Retry-After": "2"},
                ) from None
            if source is None:
                await session.rollback()
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Tool revision state changed during delivery; "
                        "retry the message poll"
                    ),
                    headers={"Retry-After": "1"},
                ) from None
            await session.commit()
            await broadcast_to_project(
                str(project_id),
                "message_updated",
                message_updated_event_payload(source),
            )
            if notice is not None:
                await broadcast_to_project(
                    str(project_id),
                    "message_created",
                    message_event_payload(notice),
                )
            # Force a fresh authority/message query on the next poll. This
            # never advances the agent cursor past a repaired or quarantined
            # source using rows expired by the lock-order rollback.
            raise HTTPException(
                status_code=503,
                detail="Conversation changed during delivery; retry the message poll",
                headers={"Retry-After": "1"},
            ) from None
        except Exception:
            logger.warning(
                "Workday artifact materialization failed transiently for project %s; "
                "poll will retry",
                project_id,
                exc_info=True,
            )
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail=(
                    "Exact tool revision is temporarily unavailable; "
                    "retry the message poll"
                ),
                headers={"Retry-After": "2"},
            ) from None


    if resident_poll:
        page_fingerprint = [_poll_message_dict(row) for row in remaining_rows]
        await session.rollback()
        project = await _lock_resident_poll_project(session, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        authenticated_task = await session.get(
            Task, authenticated_task_id, populate_existing=True
        )
        current_task_context = (
            authenticated_task.context if authenticated_task is not None else {}
        )
        current_session_id = str(
            current_task_context.get("strategic_session_id") or ""
        )
        current_task_generation = _parse_persisted_worker_generation(
            current_task_context.get("_resident_worker_generation")
        )
        resident_blob = (
            (project.vision or {}).get("_resident_strategic_session") or {}
        )
        current_project_generation = _parse_persisted_worker_generation(
            resident_blob.get("worker_generation")
        )
        if (
            current_task_context.get("_resident_session") is not True
            or current_session_id != session_id
            or current_task_generation is None
            or current_project_generation is None
            or current_task_generation != current_project_generation
            or resident_blob.get("status") != "active"
            or bool(resident_blob.get("stop_requested"))
            or str(resident_blob.get("session_id") or "") != current_session_id
            or str(resident_blob.get("task_id") or "")
            != str(authenticated_task_id)
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker generation superseded or inactive",
            )
        if resident_claim_group_selected and resident_claim_group_id is not None:
            final_conditions = [
                ProjectMessage.project_id == project_id,
                ProjectMessage.conversation_claim_id == resident_claim_group_id,
                ProjectMessage.from_type == from_type,
                ProjectMessage.status == status,
            ]
            if session_id:
                final_conditions.append(
                    resident_conversation_scope(
                        session_id,
                        conversation_floor_sequence=conversation_floor_sequence,
                    )
                )
            fresh_query = select(ProjectMessage).where(*final_conditions)
        else:
            fresh_query = select(ProjectMessage).where(*conditions).limit(limit)
        fresh_rows = (
            (
                await session.execute(
                    fresh_query
                    .order_by(*_poll_message_ordering())
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        fresh_prefix = list(fresh_rows[: len(remaining_rows)])
        fresh_fingerprint = [_poll_message_dict(row) for row in fresh_prefix]
        if fresh_fingerprint != page_fingerprint:
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail=(
                    "Conversation changed during artifact delivery; "
                    "retry the message poll"
                ),
                headers={"Retry-After": "1"},
            )
        remaining_rows = fresh_prefix
        # Independent of the cursor-bounded page: generic repair can release an
        # older non-conversation Activity row after session bootstrap. Holding
        # current Project authority while computing this MIN makes the additive
        # floor a truthful control signal for the Agent to rewind and repoll.
        conversation_floor_sequence = resident_conversation_floor_sequence(
            resident_blob
        )
        open_message_cursor_floor = await _fetch_open_operator_cursor_floor(
            session,
            project.id,
            resident_session_id=current_session_id,
            conversation_floor_sequence=conversation_floor_sequence,
        )

    # The ordinary Workday poll remains deliberately broad: it also carries
    # activity/non-conversation work that must not disappear during the typed
    # foreground rollout.  Give Fast a server-computed subset from the one
    # canonical predicate instead of asking it to infer visibility from mutable
    # message context or failing an entire mixed page at its claim boundary.
    fast_message_ids: list[str] = []
    from app.strategic_resident_session import (
        resident_typed_fast_activation_enabled,
    )

    # Stamping rows fast-eligible hands them to the typed Fast lane
    # exclusively; the agent never lets legacy Workday answer them. Without a
    # server-qualified Fast activation that lane can never run, so the stamp
    # would silently park operator messages forever -- leave the rows on the
    # legacy Workday reply path instead.
    if resident_poll and remaining_rows and resident_typed_fast_activation_enabled():
        from app.strategic_conversation_query import strategic_conversation_predicate

        canonical_ids = set(
            (
                await session.execute(
                    select(ProjectMessage.id).where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.id.in_([row.id for row in remaining_rows]),
                        ProjectMessage.from_type == "user",
                        strategic_conversation_predicate(),
                    )
                )
            ).scalars().all()
        )
        fast_message_ids = [
            str(row.id)
            for row in remaining_rows
            if row.id in canonical_ids
            and (
                (row.context or {}).get("_conversation_public_reply_owner")
                == "fast"
                or (
                    row.conversation_claim_id is None
                    and not _operator_requests_direct_workday_execution(row)
                )
            )
        ]

    fast_message_id_set = set(fast_message_ids)
    projected_messages: list[dict[str, Any]] = []
    for row in remaining_rows:
        projected = _poll_message_dict(row)
        # Carry the server-qualified lane choice into the agent projection.  A
        # direct Workday execution can legitimately spend several minutes in
        # authenticated strategic tools before it can publish its receipt-bound
        # reply; the agent uses this marker only to select that deeper turn
        # budget.  Always remove any caller-supplied value first so ordinary
        # conversation cannot self-upgrade its execution allowance.
        projected_context = dict(projected.get("context") or {})
        projected_context.pop("_direct_workday_execution", None)
        if _operator_requests_direct_workday_execution(row):
            projected_context["_direct_workday_execution"] = True
        projected["context"] = projected_context
        if resident_claim_artifacts_degraded:
            if str(row.id) not in fast_message_id_set:
                projected["content"] = (
                    f"{projected.get('content') or ''}\n\n"
                    "[System recovery notice: attachments from this interrupted "
                    "turn exceeded the safe delivery limit and were not included. "
                    "Ask the operator to resend the relevant artifact if needed.]"
                )
            degraded_context = dict(projected.get("context") or {})
            degraded_context["_artifact_delivery_deferred"] = True
            projected["context"] = degraded_context
        extras = artifact_attachments.get(row.id, [])
        context = dict(projected.get("context") or {})
        delivered_attachments = _ordered_delivery_attachments(
            context,
            extras,
            degraded=resident_claim_artifacts_degraded,
        )
        if delivered_attachments:
            context["attachments"] = delivered_attachments
        else:
            context.pop("attachments", None)
        projected["context"] = context
        projected_messages.append(projected)

    visible_message_ids = {row.id for row in remaining_rows}
    fast_claim_groups: list[dict[str, object]] = []
    if resident_claim_group_ids and resident_claim_group_key is not None:
        fast_claim_groups.append(
            {
                "idempotency_key": resident_claim_group_key,
                "message_ids": [str(value) for value in resident_claim_group_ids],
                "member_count": len(resident_claim_group_ids),
                "complete": set(resident_claim_group_ids) == visible_message_ids,
                "artifacts_degraded": resident_claim_artifacts_degraded,
            }
        )

    return {
        "messages": projected_messages,
        "server_time": utc_iso(server_time),
        "open_message_cursor_floor": open_message_cursor_floor,
        # New agents must atomically claim the exact materialized batch before
        # giving it to a provider. Older agents ignore this additive field during
        # the rolling-deploy window; generation fencing still prevents overlap.
        "claim_required": resident_poll,
        # Additive: old Workday agents continue using ``messages``. Typed Fast
        # must submit only these ids to its canonical claim endpoint.
        "fast_message_ids": fast_message_ids,
        # Authoritative completeness metadata for recovering a durable Fast
        # claim after process/session restart.  Older agents ignore it.
        "fast_claim_groups": fast_claim_groups,
    }
