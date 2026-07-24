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
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
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
)
from app.project_message_projection import (
    message_event_payload,
    message_updated_event_payload,
    project_message_base,
)
from app.project_message_creation import build_project_message
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

_RESIDENT_POLL_INTERVAL_SECONDS = 0.75
_RESIDENT_POLL_EXPECTED_PER_MINUTE = 80
_RESIDENT_POLL_REPLACEMENT_OVERLAP = 2
_RESIDENT_POLL_RATE_LIMIT_PER_MINUTE = 240
_RESIDENT_POLL_RATE_LIMIT = f"{_RESIDENT_POLL_RATE_LIMIT_PER_MINUTE}/minute"


def _resident_poll_rate_limit_key(request: Request) -> str:
    """Isolate high-frequency polls by network origin and project.

    The project component prevents unrelated residents behind one NAT or Cloud
    Run ingress address from consuming each other's budget. Authentication
    tokens are deliberately excluded: rotating a token must not evade admission.
    """
    remote = get_remote_address(request)
    project_scope = str(request.path_params.get("project_id") or "unknown-project")
    return f"resident-message-poll:{remote}:{project_scope}"


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


def _validated_sha256(value: Any, *, field: str) -> str:
    checksum = str(value or "")
    if _LOWERCASE_SHA256_RE.fullmatch(checksum) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return checksum


def _is_agent_compatible_signed_image_url(value: str) -> bool:
    """Mirror the resident agent's signed-GCS attachment transport allowlist."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    if hostname != "storage.googleapis.com" and not hostname.endswith(
        ".storage.googleapis.com"
    ):
        return False
    signatures = parse_qs(parsed.query, keep_blank_values=True).get(
        "X-Goog-Signature", []
    )
    return any(bool(signature) for signature in signatures)



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
        # Include legacy NULL-session backlog, but never leak a different
        # Workday session into this resident's conversation.
        conditions.append(
            or_(
                ProjectMessage.resident_session_id == session_id,
                ProjectMessage.resident_session_id.is_(None),
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
                    if since_dt is not None and row.created_at <= since_dt
                ),
            )
            break
        except WorkdayArtifactBatchLimit as batch_limit:
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
        fresh_rows = (
            (
                await session.execute(
                    select(ProjectMessage)
                    .where(*conditions)
                    .order_by(*_poll_message_ordering())
                    .limit(limit)
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

    projected_messages: list[dict[str, Any]] = []
    for row in remaining_rows:
        projected = _poll_message_dict(row)
        extras = artifact_attachments.get(row.id, [])
        if extras:
            context = dict(projected.get("context") or {})
            ordinary = context.get("attachments")
            ordinary_attachments = (
                list(ordinary) if isinstance(ordinary, list) else []
            )
            context["attachments"] = ordinary_attachments + extras
            projected["context"] = context
        projected_messages.append(projected)

    return {"messages": projected_messages, "server_time": server_time.isoformat()}
