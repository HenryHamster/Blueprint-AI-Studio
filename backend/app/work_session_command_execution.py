"""Server-side execution for Strategic work-session command descriptors."""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.messages_last_seen import (
    naive_utc_to_epoch_ms,
    timestamp_ms_to_naive_utc,
    upsert_messages_last_seen,
)
from app.messages_noise_prefs_store import get_messages_noise_prefs_row
from app.message_audience import stamp_message_audience
from app.models import Alert, MESSAGE_CONTENT_MAX, MessageType, Project, ProjectMessage, _utcnow
from app.project_message_creation import build_project_message
from app.strategic_trigger import (
    ARMING_NOT_ARMED,
    ARMING_RESIDENT_SESSION,
    ARMING_STEERING,
    ORIGIN_OPERATOR,
    request_strategic_refresh,
)
from app.workbench_contracts import (
    ALERT_DISMISS_COMMAND_PREFIX,
    COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN,
    COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED,
    SURFACE_STRATEGIC_SESSION,
    WorkSessionExecutableCommand,
    alert_id_from_command_id,
    compact_text,
    executable_work_session_command_contract,
    normalize_refetch_surfaces,
    normalize_target,
    runner_id_from_command_id,
    work_session_command_kind,
)
from app.workbench_state import (
    WORKBENCH_STATE_KEY,
    WORKBENCH_STATE_MAX_BYTES,
    persist_strategic_workbench_update,
    workbench_mode_refresh_excerpt,
    workbench_mode_should_arm_strategic_refresh,
    workbench_state_size_bytes,
)

logger = logging.getLogger(__name__)

OPERATOR_COMMAND_EVENT_TYPE = "operator_command"
WORK_SESSION_COMMAND_JOB_LIMIT = 8


def _compact_text(value: Any, *, max_chars: int = 240) -> str | None:
    return compact_text(value, max_chars=max_chars)


def _bounded_mapping(value: Any, *, max_items: int = 16) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if len(result) >= max_items:
            break
        key_text = _compact_text(key, max_chars=80)
        if not key_text:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key_text] = item
        else:
            text = _compact_text(item, max_chars=160)
            if text:
                result[key_text] = text
    return result or None


def _command_label(command: dict[str, Any]) -> str:
    return _compact_text(command.get("label"), max_chars=120) or "Workbench command"


def _command_id(command: dict[str, Any]) -> str:
    return str(command.get("id") or "")


def _safe_command_target(command: dict[str, Any]) -> dict[str, Any] | None:
    target = command.get("target") if isinstance(command.get("target"), dict) else None
    return normalize_target(target)


def _command_refetch_surfaces(command: dict[str, Any]) -> list[str]:
    return normalize_refetch_surfaces(
        command.get("refetch_surfaces")
        if isinstance(command.get("refetch_surfaces"), list)
        else None,
        default=(SURFACE_STRATEGIC_SESSION,),
    )


def _receipt_from_message(message: ProjectMessage) -> dict[str, Any]:
    context = message.context if isinstance(message.context, dict) else {}
    receipt = (
        context.get(OPERATOR_COMMAND_EVENT_TYPE)
        if isinstance(context.get(OPERATOR_COMMAND_EVENT_TYPE), dict)
        else {}
    )
    return {
        "id": str(message.id),
        "command_id": _compact_text(receipt.get("command_id"), max_chars=180),
        "label": _compact_text(receipt.get("label"), max_chars=120),
        "status": _compact_text(receipt.get("status"), max_chars=40) or "succeeded",
        "summary": _compact_text(message.content, max_chars=280),
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "completed_at": (
            message.resolved_at.isoformat()
            if message.resolved_at
            else message.created_at.isoformat()
            if message.created_at
            else None
        ),
        "target": normalize_target(
            receipt.get("target") if isinstance(receipt.get("target"), dict) else None
        ),
        "refetch_surfaces": normalize_refetch_surfaces(
            receipt.get("refetch_surfaces")
            if isinstance(receipt.get("refetch_surfaces"), list)
            else None,
            default=(SURFACE_STRATEGIC_SESSION,),
        ),
        "result": receipt.get("result") if isinstance(receipt.get("result"), dict) else None,
    }


def _existing_receipt_matches(
    message: ProjectMessage,
    *,
    user_id: str,
    command_id: str,
) -> bool:
    context = message.context if isinstance(message.context, dict) else {}
    receipt = (
        context.get(OPERATOR_COMMAND_EVENT_TYPE)
        if isinstance(context.get(OPERATOR_COMMAND_EVENT_TYPE), dict)
        else {}
    )
    return (
        context.get("event_type") == OPERATOR_COMMAND_EVENT_TYPE
        and receipt.get("operator_user_id") == user_id
        and receipt.get("command_id") == command_id
    )


async def _receipt_for_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
) -> ProjectMessage | None:
    return (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


def _validate_idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")
    if len(key) > 255:
        raise HTTPException(
            status_code=400,
            detail="idempotency_key exceeds 255 characters",
        )
    return key


def _mutation(command: dict[str, Any]) -> dict[str, Any]:
    mutation = command.get("mutation")
    return mutation if isinstance(mutation, dict) else {}


def _mutation_body(command: dict[str, Any]) -> dict[str, Any]:
    body = _mutation(command).get("body")
    return body if isinstance(body, dict) else {}


def _alert_id_from_command_id(command_id: str) -> str | None:
    return alert_id_from_command_id(command_id)


def _runner_id_from_command_id(command_id: str) -> str | None:
    return runner_id_from_command_id(command_id)


def _allowed_command_kind(command_id: str) -> str | None:
    return work_session_command_kind(command_id)


def _can_persist_failed_receipt(command: dict[str, Any], exc: HTTPException) -> bool:
    if exc.status_code == 409 and exc.detail == "Command is no longer executable":
        return False
    command_id = _command_id(command)
    kind = _allowed_command_kind(command_id)
    if kind is None or _safe_command_target(command) is None:
        return False
    if not bool(command.get("enabled", True)) or not _mutation(command):
        return False
    if command_id == COMMAND_ID_ATTENTION_MESSAGES_MARK_SEEN:
        return isinstance(_mutation_body(command).get("timestamp"), int)
    if _runner_id_from_command_id(command_id):
        target = _safe_command_target(command) or {}
        return target.get("runner_id") == _runner_id_from_command_id(command_id)
    if command_id.startswith(ALERT_DISMISS_COMMAND_PREFIX):
        alert_id = _alert_id_from_command_id(command_id)
        target = _safe_command_target(command) or {}
        return alert_id is not None and target.get("alert_id") == alert_id
    return True


def _failed_command_result(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail if isinstance(exc.detail, str) else "Command failed"
    return {
        "kind": "command_failed",
        "status_code": int(exc.status_code or 500),
        "error": _compact_text(detail, max_chars=240) or "Command failed",
    }


def _failed_receipt_content(label: str, result: dict[str, Any]) -> str:
    error = _compact_text(result.get("error"), max_chars=260) or "Command failed"
    content = f"Command failed: {label} - {error}"
    if len(content) > MESSAGE_CONTENT_MAX:
        content = content[:MESSAGE_CONTENT_MAX]
    return content


async def _execute_strategic_workbench_command(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: uuid.UUID,
    contract: WorkSessionExecutableCommand,
) -> dict[str, Any]:
    workbench = contract.workbench_patch
    if not isinstance(workbench, dict):
        raise HTTPException(status_code=409, detail="Command is no longer executable")
    if workbench_state_size_bytes(workbench) > WORKBENCH_STATE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Workbench state too large (max 50KB)",
        )
    if (
        contract.command_id == COMMAND_ID_SESSION_RUNNER_CLEAR_PREFERRED
        and contract.runner_id
    ):
        prefs_row = await get_messages_noise_prefs_row(
            session,
            user_id,
            project_id,
        )
        prefs = (
            prefs_row.prefs
            if prefs_row is not None and isinstance(prefs_row.prefs, dict)
            else {}
        )
        current_workbench = (
            prefs.get(WORKBENCH_STATE_KEY)
            if isinstance(prefs.get(WORKBENCH_STATE_KEY), dict)
            else {}
        )
        if current_workbench.get("preferred_runner_id") != contract.runner_id:
            raise HTTPException(status_code=409, detail="Command is no longer executable")
    write = await persist_strategic_workbench_update(
        session,
        user_id,
        project_id,
        workbench,
        commit=False,
    )
    should_arm = workbench_mode_should_arm_strategic_refresh(
        write.previous_state,
        write.state,
    )
    resident_active = False
    if should_arm:
        from app.strategic_resident_session import (  # local import avoids cycle
            read_resident_session,
            resident_session_is_active,
        )
        project = await session.get(Project, project_id)
        resident_active = resident_session_is_active(
            read_resident_session(project.vision if project else None)
        )
    return {
        "kind": "workbench_update",
        "workbench_keys": sorted(str(key) for key in workbench.keys())[:12],
        "arming": (
            ARMING_RESIDENT_SESSION
            if (should_arm and resident_active)
            else (ARMING_NOT_ARMED if should_arm else write.arming)
        ),
        "_arm_after_receipt": should_arm and not resident_active,
        "_arm_state": write.state if (should_arm and not resident_active) else None,
        "_arm_resident_active": should_arm and resident_active,
    }


async def _execute_mark_messages_seen(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: uuid.UUID,
    contract: WorkSessionExecutableCommand,
    observed_at_ms: int | None,
) -> dict[str, Any]:
    raw_timestamp = contract.messages_seen_timestamp
    if not isinstance(raw_timestamp, int):
        raise HTTPException(status_code=409, detail="Command is no longer executable")
    effective_timestamp = raw_timestamp
    if isinstance(observed_at_ms, int):
        effective_timestamp = min(raw_timestamp, observed_at_ms)
    stored_ts = await upsert_messages_last_seen(
        session,
        user_id,
        project_id,
        timestamp_ms_to_naive_utc(effective_timestamp),
    )
    return {
        "kind": "messages_last_seen",
        "last_seen_at": naive_utc_to_epoch_ms(stored_ts),
    }


async def _execute_alert_dismiss(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    contract: WorkSessionExecutableCommand,
) -> dict[str, Any]:
    alert_id = contract.alert_id or ""
    try:
        alert_uuid = uuid.UUID(alert_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=409, detail="Command is no longer executable") from exc
    alert = await session.get(Alert, alert_uuid)
    if alert is None or alert.project_id != project_id:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.dismissed_at is None:
        alert.dismissed_at = _utcnow()
        session.add(alert)
    return {
        "kind": "alert_dismissed",
        "alert_id": str(alert.id),
        "title": _compact_text(alert.title, max_chars=160),
    }


async def _apply_command(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: uuid.UUID,
    contract: WorkSessionExecutableCommand,
    observed_at_ms: int | None,
) -> dict[str, Any]:
    if contract.kind == "strategic_workbench":
        return await _execute_strategic_workbench_command(
            session,
            user_id=user_id,
            project_id=project_id,
            contract=contract,
        )
    if contract.kind == "messages_last_seen":
        return await _execute_mark_messages_seen(
            session,
            user_id=user_id,
            project_id=project_id,
            contract=contract,
            observed_at_ms=observed_at_ms,
        )
    if contract.kind == "alert_dismiss":
        return await _execute_alert_dismiss(
            session,
            project_id=project_id,
            contract=contract,
        )
    raise HTTPException(status_code=409, detail="Command is not executable")


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


async def _request_arming_after_receipt(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    state: dict[str, Any],
    resident_session_active: bool = False,
) -> str:
    if resident_session_active:
        return ARMING_RESIDENT_SESSION
    try:
        armed = await request_strategic_refresh(
            session,
            project_id,
            "workbench_interaction_mode",
            origin=ORIGIN_OPERATOR,
            steering_excerpt=workbench_mode_refresh_excerpt(state),
            skip_locked=False,
        )
        if armed:
            await session.commit()
            return ARMING_STEERING
    except Exception:
        # A refresh failure must not fail the already-persisted command receipt,
        # but it must stay traceable: log with context so a real DB/trigger
        # failure is distinguishable from a benign not-armed outcome.
        logger.exception(
            "[WORKBENCH] arming strategic refresh after command receipt failed "
            "for project %s",
            project_id,
        )
        with contextlib.suppress(Exception):
            await session.rollback()
        return ARMING_NOT_ARMED
    return ARMING_NOT_ARMED


async def _update_receipt_result(
    session: AsyncSession,
    *,
    receipt_id: uuid.UUID,
    result: dict[str, Any],
) -> ProjectMessage | None:
    receipt = await session.get(ProjectMessage, receipt_id)
    if receipt is None:
        return None
    context = dict(receipt.context or {})
    command_context = (
        dict(context.get(OPERATOR_COMMAND_EVENT_TYPE))
        if isinstance(context.get(OPERATOR_COMMAND_EVENT_TYPE), dict)
        else {}
    )
    command_context["result"] = _bounded_mapping(result) or {}
    context[OPERATOR_COMMAND_EVENT_TYPE] = command_context
    receipt.context = context
    session.add(receipt)
    await session.commit()
    await session.refresh(receipt)
    return receipt


async def _persist_failed_command_receipt(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: str,
    command: dict[str, Any],
    idempotency_key: str,
    exc: HTTPException,
) -> dict[str, Any]:
    command_id = _command_id(command)
    now = _utcnow()
    label = _command_label(command)
    receipt_result = _failed_command_result(exc)
    receipt = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.NOTE,
        from_type="system",
        content=_failed_receipt_content(label, receipt_result),
        context=stamp_message_audience(
            {
                "event_type": OPERATOR_COMMAND_EVENT_TYPE,
                OPERATOR_COMMAND_EVENT_TYPE: {
                    "version": 1,
                    "command_id": command_id,
                    "label": label,
                    "status": "failed",
                    "operator_user_id": user_id,
                    "executed_at": now.isoformat(),
                    "failed_at": now.isoformat(),
                    "target": _safe_command_target(command),
                    "refetch_surfaces": _command_refetch_surfaces(command),
                    "result": _bounded_mapping(receipt_result) or {},
                },
            },
            OPERATOR_COMMAND_EVENT_TYPE,
        ),
        status="resolved",
        resolved_at=now,
        idempotency_key=idempotency_key,
    )
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _receipt_for_idempotency_key(
            session,
            project_id,
            idempotency_key,
        )
        if existing is not None and _existing_receipt_matches(
            existing,
            user_id=user_id,
            command_id=command_id,
        ):
            return {
                "status": "duplicate",
                "applied": False,
                "command_id": command_id,
                "receipt": _receipt_from_message(existing),
            }
        raise
    await session.refresh(receipt)
    return {
        "status": "failed",
        "applied": False,
        "command_id": command_id,
        "receipt": _receipt_from_message(receipt),
    }


async def execute_work_session_command(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: str,
    command: dict[str, Any],
    idempotency_key: str | None,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    command_id = str(command.get("id") or "")
    if not command_id:
        raise HTTPException(status_code=404, detail="Command not found")
    idem_key = _validate_idempotency_key(idempotency_key)

    existing = await _receipt_for_idempotency_key(session, project_id, idem_key)
    if existing is not None:
        if not _existing_receipt_matches(existing, user_id=user_id, command_id=command_id):
            raise HTTPException(
                status_code=409,
                detail="idempotency_key already belongs to another operation",
            )
        return {
            "status": "duplicate",
            "applied": False,
            "command_id": command_id,
            "receipt": _receipt_from_message(existing),
        }

    if not bool(command.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Command is disabled")
    if not _mutation(command):
        raise HTTPException(status_code=409, detail="Command is not executable")
    if _allowed_command_kind(command_id) is None:
        raise HTTPException(status_code=409, detail="Command is not executable")
    contract = executable_work_session_command_contract(command, project_id=project_id)
    if contract is None:
        raise HTTPException(status_code=409, detail="Command is no longer executable")

    try:
        result = await _apply_command(
            session,
            user_id=user_id,
            project_id=project_id,
            contract=contract,
            observed_at_ms=observed_at_ms,
        )
    except HTTPException as exc:
        with contextlib.suppress(Exception):
            await session.rollback()
        if not _can_persist_failed_receipt(command, exc):
            raise
        return await _persist_failed_command_receipt(
            session,
            project_id=project_id,
            user_id=user_id,
            command=command,
            idempotency_key=idem_key,
            exc=exc,
        )
    receipt_result = _public_result(result)

    now = _utcnow()
    label = _command_label(command)
    target = _safe_command_target(command)
    refetch_surfaces = _command_refetch_surfaces(command)
    content = f"Command completed: {label}"
    if len(content) > MESSAGE_CONTENT_MAX:
        content = content[:MESSAGE_CONTENT_MAX]
    receipt = await build_project_message(
        session,
        project_id=project_id,
        message_type=MessageType.NOTE,
        from_type="system",
        content=content,
        context=stamp_message_audience(
            {
                "event_type": OPERATOR_COMMAND_EVENT_TYPE,
                OPERATOR_COMMAND_EVENT_TYPE: {
                    "version": 1,
                    "command_id": command_id,
                    "label": label,
                    "status": "succeeded",
                    "operator_user_id": user_id,
                    "executed_at": now.isoformat(),
                    "target": target,
                    "refetch_surfaces": refetch_surfaces,
                    "result": _bounded_mapping(receipt_result) or {},
                },
            },
            OPERATOR_COMMAND_EVENT_TYPE,
        ),
        status="resolved",
        resolved_at=now,
        idempotency_key=idem_key,
    )
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _receipt_for_idempotency_key(session, project_id, idem_key)
        if existing is not None and _existing_receipt_matches(
            existing,
            user_id=user_id,
            command_id=command_id,
        ):
            return {
                "status": "duplicate",
                "applied": False,
                "command_id": command_id,
                "receipt": _receipt_from_message(existing),
            }
        raise
    await session.refresh(receipt)
    if result.get("_arm_after_receipt") and isinstance(result.get("_arm_state"), dict):
        receipt_result["arming"] = await _request_arming_after_receipt(
            session,
            project_id=project_id,
            state=result["_arm_state"],
        )
        updated_receipt = await _update_receipt_result(
            session,
            receipt_id=receipt.id,
            result=receipt_result,
        )
        if updated_receipt is not None:
            receipt = updated_receipt
    return {
        "status": "succeeded",
        "applied": True,
        "command_id": command_id,
        "receipt": _receipt_from_message(receipt),
    }


async def existing_work_session_command_result_for_idempotency_key(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: str,
    command_id: str,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    idem_key = _validate_idempotency_key(idempotency_key)
    existing = await _receipt_for_idempotency_key(session, project_id, idem_key)
    if existing is None:
        return None
    if not _existing_receipt_matches(
        existing,
        user_id=user_id,
        command_id=command_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="idempotency_key already belongs to another operation",
        )
    return {
        "status": "duplicate",
        "applied": False,
        "command_id": command_id,
        "receipt": _receipt_from_message(existing),
    }


async def build_work_session_command_jobs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    user_id: str | None = None,
    limit: int = WORK_SESSION_COMMAND_JOB_LIMIT,
) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or WORK_SESSION_COMMAND_JOB_LIMIT), 20))
    query = select(ProjectMessage).where(
        ProjectMessage.project_id == project_id,
        ProjectMessage.context["event_type"].as_string()
        == OPERATOR_COMMAND_EVENT_TYPE,
    )
    if user_id is not None:
        query = query.where(
            ProjectMessage.context[OPERATOR_COMMAND_EVENT_TYPE]["operator_user_id"]
            .as_string()
            == user_id
        )
    rows = (
        await session.execute(
            query
            .order_by(ProjectMessage.created_at.desc())
            .limit(bounded_limit + 1)
        )
    ).scalars().all()
    return {
        "version": 1,
        "limit": bounded_limit,
        "has_more": len(rows) > bounded_limit,
        "items": [_receipt_from_message(row) for row in rows[:bounded_limit]],
    }
