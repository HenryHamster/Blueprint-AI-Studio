"""Receipt-bound private autonomous Workday findings for Fast delivery.

The long-lived Resident may run broad Workday turns between operator messages,
but typed Fast remains the only public conversational author.  This module
mints one short server-owned autonomous-turn lease and lets that exact turn
create at most one bounded strategist-internal result.  The established Fast
continuation coordinator later claims that result and creates the sole public
message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.message_audience import stamp_message_audience
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    Task,
)
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)
from app.task_locking import with_vision_locked
from app.strategic_fast_live_work import _fast_live_work_authority
from app.strategic_memory_envelope import build_and_store_context_envelope_snapshot
from app.time_utils import _utcnow


AUTONOMOUS_HANDOFF_OPS = frozenset(
    {
        "begin_autonomous_turn",
        "report_autonomous_finding",
        "end_autonomous_turn",
    }
)
_RESIDENT_KEY = "_resident_strategic_session"
_LEASE_KEY = "autonomous_private_turn"
_RESULT_MARKER = "_workday_consultation_result"
_AUTONOMOUS_MARKER = "_autonomous_workday_handoff"
_PACKET_MARKER = "_conversation_consultation_packet"
_COOLDOWN_SECONDS = 300
_LEASE_TTL_MIN_SECONDS = 30
_LEASE_TTL_MAX_SECONDS = 600
_SUMMARY_MAX = 8_000
_EVIDENCE_MAX = 12
_EVIDENCE_ITEM_MAX = 256
_KINDS = frozenset({"question", "blocker", "finding"})


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _bad_request(f"{field} must be a UUID") from exc


def _as_generation(value: Any) -> int:
    if isinstance(value, bool):
        raise _bad_request("worker_generation must be a positive integer")
    try:
        generation = int(value)
    except (TypeError, ValueError) as exc:
        raise _bad_request("worker_generation must be a positive integer") from exc
    if generation < 1:
        raise _bad_request("worker_generation must be a positive integer")
    return generation


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _resident(project: Project) -> dict[str, Any]:
    raw = (project.vision or {}).get(_RESIDENT_KEY)
    if not isinstance(raw, dict) or raw.get("status") != "active":
        raise _conflict("Resident session is superseded or inactive")
    return dict(raw)


def _request_identity(
    project: Project,
    task: Task,
    payload: Any,
) -> tuple[dict[str, Any], str, int, uuid.UUID]:
    data = payload.data if isinstance(payload.data, dict) else {}
    session_id = str(data.get("session_id") or "").strip()
    if not session_id or len(session_id) > 255:
        raise _bad_request("session_id is required and must be at most 255 characters")
    generation = _as_generation(data.get("worker_generation"))
    incarnation = payload.resident_incarnation_id
    if incarnation is None:
        raise _conflict("Resident process incarnation is required")
    resident = _resident(project)
    task_context = task.context if isinstance(task.context, dict) else {}
    if (
        str(resident.get("session_id") or "") != session_id
        or str(resident.get("task_id") or "") != str(task.id)
        or resident.get("worker_generation") != generation
        or str(task_context.get("strategic_session_id") or "") != session_id
        or task_context.get("_resident_worker_generation") != generation
    ):
        raise _conflict("Autonomous turn identity is stale or mismatched")
    return resident, session_id, generation, incarnation


def _lease_response(lease: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
    return {
        "status": "duplicate" if duplicate else "created",
        "autonomous_turn_id": lease["turn_id"],
        "autonomous_turn_lease_id": lease["lease_id"],
        "autonomous_handoff_idempotency_key": lease["client_turn_id"],
        "lease_expires_at": lease["expires_at"],
        "lease_status": lease["status"],
    }


async def _store_lease(
    session: AsyncSession,
    project: Project,
    resident: dict[str, Any],
    lease: dict[str, Any],
) -> None:
    resident[_LEASE_KEY] = lease
    resident["updated_at"] = _utcnow().isoformat()

    def _mutate(vision: dict[str, Any]) -> None:
        vision[_RESIDENT_KEY] = resident

    await with_vision_locked(
        session,
        project.id,
        _mutate,
        _locked_project=project,
    )


def _same_lease(
    lease: dict[str, Any],
    *,
    turn_id: uuid.UUID,
    lease_id: uuid.UUID,
    client_turn_id: uuid.UUID,
    task: Task,
    session_id: str,
    generation: int,
    incarnation: uuid.UUID,
) -> bool:
    return bool(
        str(lease.get("turn_id") or "") == str(turn_id)
        and str(lease.get("lease_id") or "") == str(lease_id)
        and str(lease.get("client_turn_id") or "") == str(client_turn_id)
        and str(lease.get("task_id") or "") == str(task.id)
        and str(lease.get("session_id") or "") == session_id
        and lease.get("worker_generation") == generation
        and str(lease.get("worker_incarnation") or "") == str(incarnation)
    )


async def _open_private_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
) -> list[ProjectMessage]:
    return list(
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.resident_session_id == session_id,
                    ProjectMessage.handling_state.in_(
                        (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
                    ),
                )
                .order_by(ProjectMessage.sequence, ProjectMessage.id)
            )
        )
        .scalars()
        .all()
    )


def _is_result(row: ProjectMessage) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        context.get("message_role") == "workday_consultation_result"
        and context.get(_RESULT_MARKER) is True
        and context.get("audience") == "strategist-internal"
        and context.get("public_reply_owner") == "fast"
    )


def _explicit_consultation_is_open(rows: list[ProjectMessage]) -> bool:
    for row in rows:
        context = row.context if isinstance(row.context, dict) else {}
        if (
            context.get("message_role") == "workday_consultation"
            and context.get(_PACKET_MARKER) is True
        ) or (_is_result(row) and context.get(_AUTONOMOUS_MARKER) is not True):
            return True
    return False


async def _begin(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    payload: Any,
) -> dict[str, Any]:
    # ``project`` is the canonical row already held FOR UPDATE by the agent
    # operation handler.  The Resident activity blob is heartbeat-lagged, so
    # it may still say ``active`` after the operator disables project autonomy.
    # Fail closed on the canonical switch before inspecting or mutating any
    # autonomous-turn lease state.
    if project.autonomous_enabled is not True:
        raise _conflict("Project autonomy is disabled")
    resident, session_id, generation, incarnation = _request_identity(
        project, task, payload
    )
    if (task.first_payload or {}).get("typed_fast_enabled") is not True:
        raise _conflict("Typed Fast is not active for this Resident")
    data = payload.data
    client_turn_id = _as_uuid(data.get("client_turn_id"), "client_turn_id")
    try:
        ttl_seconds = int(data.get("ttl_seconds") or 300)
    except (TypeError, ValueError) as exc:
        raise _bad_request("ttl_seconds must be an integer") from exc
    ttl_seconds = min(
        _LEASE_TTL_MAX_SECONDS,
        max(_LEASE_TTL_MIN_SECONDS, ttl_seconds),
    )
    now = _utcnow()
    existing = resident.get(_LEASE_KEY)
    if isinstance(existing, dict):
        existing_expiry = _parse_time(existing.get("expires_at"))
        if (
            str(existing.get("client_turn_id") or "") == str(client_turn_id)
            and str(existing.get("task_id") or "") == str(task.id)
            and str(existing.get("session_id") or "") == session_id
            and existing.get("worker_generation") == generation
            and str(existing.get("worker_incarnation") or "") == str(incarnation)
            and existing_expiry is not None
            and existing_expiry > now
        ):
            return _lease_response(existing, duplicate=True)
        if (
            existing.get("status") in {"active", "reported"}
            and existing_expiry is not None
            and existing_expiry > now
        ):
            raise _conflict("Another autonomous Workday turn already owns the lease")

    activity = resident.get("activity") if isinstance(resident.get("activity"), dict) else {}
    if activity.get("autonomy_reason_code") in {
        "authority_not_held",
        "foreground_call",
    }:
        raise _conflict("Resident autonomy is currently paused")
    from app.strategic_resident_session import read_resident_call, resident_call_is_active

    if resident_call_is_active(read_resident_call(project.vision), now=now):
        raise _conflict("A foreground call currently owns Workday")
    if _explicit_consultation_is_open(
        await _open_private_rows(
            session, project_id=project.id, session_id=session_id
        )
    ):
        raise _conflict("A Fast-requested Workday consultation is already open")

    lease = {
        "turn_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "client_turn_id": str(client_turn_id),
        "task_id": str(task.id),
        "session_id": session_id,
        "worker_generation": generation,
        "worker_incarnation": str(incarnation),
        "status": "active",
        "started_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    await _store_lease(session, project, resident, lease)
    session.add(project)
    await session.commit()
    return _lease_response(lease, duplicate=False)


def _normalize_finding(data: Any) -> tuple[str, str, list[str], str]:
    if not isinstance(data, dict):
        raise _bad_request("report_autonomous_finding data must be an object")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in _KINDS:
        raise _bad_request("kind must be question, blocker, or finding")
    summary = str(data.get("summary") or "").strip()
    if not summary or len(summary) > _SUMMARY_MAX:
        raise _bad_request(f"summary must be 1-{_SUMMARY_MAX} characters")
    raw_refs = data.get("evidence_refs", [])
    if not isinstance(raw_refs, list) or len(raw_refs) > _EVIDENCE_MAX:
        raise _bad_request(f"evidence_refs must contain at most {_EVIDENCE_MAX} strings")
    evidence_refs: list[str] = []
    seen: set[str] = set()
    for raw in raw_refs:
        if not isinstance(raw, str):
            raise _bad_request("evidence_refs entries must be strings")
        ref = raw.strip()
        if not ref or len(ref) > _EVIDENCE_ITEM_MAX:
            raise _bad_request(
                f"evidence_refs entries must be 1-{_EVIDENCE_ITEM_MAX} characters"
            )
        if ref not in seen:
            evidence_refs.append(ref)
            seen.add(ref)
    canonical = json.dumps(
        {"kind": kind, "summary": summary, "evidence_refs": evidence_refs},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return kind, summary, evidence_refs, hashlib.sha256(canonical).hexdigest()


def _finding_content(kind: str, summary: str, evidence_refs: list[str]) -> str:
    lines = [f"Autonomous Workday {kind}", "", summary]
    if evidence_refs:
        lines.extend(
            [
                "",
                "Evidence references (correlation candidates; server state remains authoritative):",
                *[f"- {ref}" for ref in evidence_refs],
            ]
        )
    return "\n".join(lines)


async def _existing_handoff(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task: Task,
    session_id: str,
    generation: int,
    incarnation: uuid.UUID,
    turn_id: uuid.UUID,
    lease_id: uuid.UUID,
    client_turn_id: uuid.UUID,
    fingerprint: str,
    content: str,
) -> ProjectMessage | None:
    key = f"autonomous-workday-handoff:{task.id}:{client_turn_id}"
    row = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    context = row.context if isinstance(row.context, dict) else {}
    if not (
        row.from_type == "system"
        and row.resident_session_id == session_id
        and row.content == content
        and context.get(_AUTONOMOUS_MARKER) is True
        and context.get("autonomous_finding_fingerprint") == fingerprint
        and context.get("autonomous_turn_id") == str(turn_id)
        and context.get("autonomous_turn_lease_id") == str(lease_id)
        and context.get("autonomous_handoff_idempotency_key")
        == str(client_turn_id)
        and context.get("autonomous_task_id") == str(task.id)
        and context.get("autonomous_worker_generation") == generation
        and context.get("autonomous_worker_incarnation") == str(incarnation)
    ):
        raise _conflict("Autonomous handoff idempotency key conflicts")
    return row


async def _last_autonomous_result(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
) -> ProjectMessage | None:
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.resident_session_id == session_id,
                )
                .order_by(ProjectMessage.updated_at.desc(), ProjectMessage.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return next(
        (
            row
            for row in rows
            if isinstance(row.context, dict)
            and row.context.get(_AUTONOMOUS_MARKER) is True
        ),
        None,
    )


async def _report(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    payload: Any,
) -> dict[str, Any]:
    resident, session_id, generation, incarnation = _request_identity(
        project, task, payload
    )
    turn_id = payload.autonomous_turn_id
    lease_id = payload.autonomous_turn_lease_id
    client_turn_id = payload.autonomous_handoff_idempotency_key
    if turn_id is None or lease_id is None or client_turn_id is None:
        raise _conflict("Server-owned autonomous turn provenance is required")
    kind, summary, evidence_refs, fingerprint = _normalize_finding(payload.data)
    content = _finding_content(kind, summary, evidence_refs)
    existing = await _existing_handoff(
        session,
        project_id=project.id,
        task=task,
        session_id=session_id,
        generation=generation,
        incarnation=incarnation,
        turn_id=turn_id,
        lease_id=lease_id,
        client_turn_id=client_turn_id,
        fingerprint=fingerprint,
        content=content,
    )
    if existing is not None:
        return {
            "status": "duplicate",
            "result_packet_id": str(existing.id),
            "delivery_owner": "fast",
        }

    lease = resident.get(_LEASE_KEY)
    if not isinstance(lease, dict) or not _same_lease(
        lease,
        turn_id=turn_id,
        lease_id=lease_id,
        client_turn_id=client_turn_id,
        task=task,
        session_id=session_id,
        generation=generation,
        incarnation=incarnation,
    ):
        raise _conflict("Autonomous turn lease is stale or mismatched")
    expires_at = _parse_time(lease.get("expires_at"))
    now = _utcnow()
    if lease.get("status") != "active" or expires_at is None or expires_at <= now:
        raise _conflict("Autonomous turn lease is no longer active")

    open_rows = await _open_private_rows(
        session, project_id=project.id, session_id=session_id
    )
    if _explicit_consultation_is_open(open_rows):
        raise _conflict("A Fast-requested Workday consultation is already open")
    if any(_is_result(row) for row in open_rows):
        raise _conflict("A private Workday result is already awaiting Fast")
    latest = await _last_autonomous_result(
        session, project_id=project.id, session_id=session_id
    )
    if latest is not None and latest.updated_at > now - timedelta(seconds=_COOLDOWN_SECONDS):
        raise _conflict("Autonomous Workday handoff cooldown is active")

    sequence = await lock_project_and_next_message_sequence(session, project.id)
    live_authority = await _fast_live_work_authority(
        session,
        project=project,
        resident_task_id=task.id,
        result_evidence_text=content,
    )
    envelope = await build_and_store_context_envelope_snapshot(
        session,
        project_id=project.id,
        session_id=session_id,
        claim_id=turn_id,
        worker_generation=generation,
        worker_incarnation=incarnation,
        input_revision=max(0, sequence - 1),
        transcript_sequence=max(0, sequence - 1),
        decision_authority=live_authority,
        memory_query=summary,
        expires_at=now + timedelta(hours=1),
    )
    result_key = f"autonomous-workday-handoff:{task.id}:{client_turn_id}"
    context = stamp_message_audience(
        {
            _RESULT_MARKER: True,
            _AUTONOMOUS_MARKER: True,
            "message_role": "workday_consultation_result",
            "public_reply_owner": "fast",
            "result_state": "completed",
            "claim_id": str(turn_id),
            "delivery_key": f"autonomous-{kind}",
            "_conversation_consultation_continuation": "autonomous-workday-v1",
            "autonomous_finding_kind": kind,
            "autonomous_finding_fingerprint": fingerprint,
            "autonomous_evidence_refs": evidence_refs,
            "autonomous_turn_id": str(turn_id),
            "autonomous_turn_lease_id": str(lease_id),
            "autonomous_handoff_idempotency_key": str(client_turn_id),
            "autonomous_task_id": str(task.id),
            "autonomous_worker_generation": generation,
            "autonomous_worker_incarnation": str(incarnation),
            "envelope": envelope.model_dump(mode="json"),
        },
        "workday_consultation_result",
        message_type=MessageType.NOTE.value,
    )
    result = await build_project_message(
        session,
        project_id=project.id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="system",
        content=content,
        context=context,
        idempotency_key=result_key,
        resident_session_id=session_id,
    )
    session.add(result)
    lease = dict(lease)
    lease.update(
        status="reported",
        report_message_id=str(result.id),
        report_fingerprint=fingerprint,
        updated_at=now.isoformat(),
    )
    resident = _resident(project)
    await _store_lease(session, project, resident, lease)
    session.add(project)
    await session.commit()
    return {
        "status": "created",
        "result_packet_id": str(result.id),
        "delivery_owner": "fast",
    }


async def _end(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    payload: Any,
) -> dict[str, Any]:
    resident, session_id, generation, incarnation = _request_identity(
        project, task, payload
    )
    turn_id = payload.autonomous_turn_id
    lease_id = payload.autonomous_turn_lease_id
    client_turn_id = payload.autonomous_handoff_idempotency_key
    if turn_id is None or lease_id is None or client_turn_id is None:
        raise _conflict("Server-owned autonomous turn provenance is required")
    lease = resident.get(_LEASE_KEY)
    if not isinstance(lease, dict) or not _same_lease(
        lease,
        turn_id=turn_id,
        lease_id=lease_id,
        client_turn_id=client_turn_id,
        task=task,
        session_id=session_id,
        generation=generation,
        incarnation=incarnation,
    ):
        raise _conflict("Autonomous turn lease is stale or mismatched")
    if lease.get("status") == "ended":
        return {"status": "duplicate", "autonomous_turn_id": str(turn_id)}
    lease = dict(lease)
    lease.update(status="ended", ended_at=_utcnow().isoformat(), updated_at=_utcnow().isoformat())
    await _store_lease(session, project, resident, lease)
    session.add(project)
    await session.commit()
    return {"status": "ended", "autonomous_turn_id": str(turn_id)}


async def handle_autonomous_handoff_operation(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    payload: Any,
) -> dict[str, Any]:
    """Dispatch one already-authenticated autonomous handoff operation."""
    if payload.op == "begin_autonomous_turn":
        return await _begin(session, project=project, task=task, payload=payload)
    if payload.op == "report_autonomous_finding":
        return await _report(session, project=project, task=task, payload=payload)
    if payload.op == "end_autonomous_turn":
        return await _end(session, project=project, task=task, payload=payload)
    raise _bad_request(f"Unsupported autonomous handoff op: {payload.op}")


__all__ = ["AUTONOMOUS_HANDOFF_OPS", "handle_autonomous_handoff_operation"]
