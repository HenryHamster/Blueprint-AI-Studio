"""Durable project control for fresh automatic release qualification starts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import ReleaseQualificationControlRevision
from app.qualification_state_resolver import active_qualification_attempt_id
from app.release_authority_lock import lock_release_project
from app import release_task_admission

_MODULE_SIZE_GOVERNANCE_CAP: int = 260
_KEY_MAX, _REASON_MAX = 255, 500


class QualificationControlError(ValueError):
    """Base class for rejected qualification-control operations."""


class QualificationControlConflict(QualificationControlError):
    """The idempotency key or expected revision does not match durable state."""


class QualificationControlProjectNotFound(QualificationControlError):
    """The target project does not exist."""


def _normalize_text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise QualificationControlError(
            f"{name} must be a non-empty canonical string"
        )
    if len(value) > limit:
        raise QualificationControlError(f"{name} must be at most {limit} characters")
    return value


def _specification_hash(
    *,
    project_id: uuid.UUID,
    expected_revision: int,
    automatic_starts_enabled: bool,
    idempotency_key: str,
    actor_user_id: str,
    reason: str,
) -> str:
    payload = {
        "schema_version": 1,
        "project_id": str(project_id),
        "expected_revision": expected_revision,
        "automatic_starts_enabled": automatic_starts_enabled,
        "idempotency_key": idempotency_key,
        "actor_user_id": actor_user_id,
        "reason": reason,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _projection(
    project_id: uuid.UUID,
    row: ReleaseQualificationControlRevision | None,
) -> dict[str, Any]:
    if row is None:
        return {
            "schema_version": 1,
            "project_id": str(project_id),
            "id": None,
            "revision": 0,
            "automatic_starts_enabled": True,
            "idempotency_key": None,
            "specification_hash": None,
            "actor_user_id": None,
            "reason": None,
            "created_at": None,
        }
    return {
        "schema_version": 1,
        "project_id": str(project_id),
        "id": str(row.id),
        "revision": int(row.revision),
        "automatic_starts_enabled": bool(row.automatic_starts_enabled),
        "idempotency_key": row.idempotency_key,
        "specification_hash": row.specification_hash,
        "actor_user_id": row.actor_user_id,
        "reason": row.reason,
        "created_at": row.created_at.isoformat(),
    }


async def _latest(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ReleaseQualificationControlRevision | None:
    return (
        await session.execute(
            select(ReleaseQualificationControlRevision)
            .where(ReleaseQualificationControlRevision.project_id == project_id)
            .order_by(ReleaseQualificationControlRevision.revision.desc())
            .limit(1)
        )
    ).scalars().first()


async def qualification_control_readback(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    key = None
    receipt = None
    if idempotency_key is not None:
        key = _normalize_text(
            idempotency_key, name="idempotency_key", limit=_KEY_MAX
        )
        receipt = (
            await session.execute(
                select(ReleaseQualificationControlRevision).where(
                    ReleaseQualificationControlRevision.project_id == project_id,
                    ReleaseQualificationControlRevision.idempotency_key == key,
                )
            )
        ).scalars().first()
    current = await _latest(session, project_id)
    return {
        "schema_version": 1,
        "project_id": str(project_id),
        "current": _projection(project_id, current),
        "receipt": _projection(project_id, receipt) if receipt is not None else None,
        "requested_idempotency_key": key,
    }


async def apply_qualification_control(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    expected_revision: int,
    automatic_starts_enabled: bool,
    idempotency_key: str,
    actor_user_id: str,
    reason: str,
) -> dict[str, Any]:
    if type(expected_revision) is not int or expected_revision < 0:
        raise QualificationControlError(
            "expected_revision must be a non-negative integer"
        )
    if type(automatic_starts_enabled) is not bool:
        raise QualificationControlError("automatic_starts_enabled must be boolean")
    key = _normalize_text(idempotency_key, name="idempotency_key", limit=_KEY_MAX)
    actor = _normalize_text(actor_user_id, name="actor_user_id", limit=255)
    why = _normalize_text(reason, name="reason", limit=_REASON_MAX)
    specification_hash = _specification_hash(
        project_id=project_id,
        expected_revision=expected_revision,
        automatic_starts_enabled=automatic_starts_enabled,
        idempotency_key=key,
        actor_user_id=actor,
        reason=why,
    )
    if await lock_release_project(session, project_id) is None:
        raise QualificationControlProjectNotFound("Project not found")
    existing = (
        await session.execute(
            select(ReleaseQualificationControlRevision).where(
                ReleaseQualificationControlRevision.project_id == project_id,
                ReleaseQualificationControlRevision.idempotency_key == key,
            )
        )
    ).scalars().first()
    current = await _latest(session, project_id)
    if existing is not None:
        if existing.specification_hash != specification_hash:
            raise QualificationControlConflict(
                "idempotency key belongs to a different "
                "qualification-control request"
            )
        return {
            **await qualification_control_readback(
                session, project_id, idempotency_key=key
            ),
            "idempotent_replay": True,
        }
    current_revision = int(current.revision) if current is not None else 0
    if current_revision != expected_revision:
        raise QualificationControlConflict(
            "qualification-control revision changed "
            f"(expected {expected_revision}, current {current_revision})"
        )
    row = ReleaseQualificationControlRevision(
        project_id=project_id,
        revision=current_revision + 1,
        automatic_starts_enabled=automatic_starts_enabled,
        idempotency_key=key,
        specification_hash=specification_hash,
        actor_user_id=actor,
        reason=why,
    )
    session.add(row)
    await session.flush()
    readback = await qualification_control_readback(
        session, project_id, idempotency_key=key
    )
    return {**readback, "idempotent_replay": False}


async def automatic_qualification_start_block(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any] | None:
    row = await _latest(session, project_id)
    if row is None or row.automatic_starts_enabled:
        return None
    return {
        "status": "automatic_qualification_disabled",
        "qualification_control_revision": int(row.revision),
        "qualification_control_reason": row.reason,
    }


def qualification_continuation_block(
    version: Any,
    requested_attempt_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    if requested_attempt_id is None:
        return None
    active_attempt_id = active_qualification_attempt_id(version)
    if active_attempt_id == requested_attempt_id:
        return None
    return {
        "status": "stale_qualification_attempt",
        "qualification_attempt_id": str(requested_attempt_id),
        "active_qualification_attempt_id": (
            str(active_attempt_id) if active_attempt_id is not None else None
        ),
    }


async def qualification_start_block(
    session: AsyncSession,
    project_id: uuid.UUID,
    version_number: int,
    *,
    fresh_automatic: bool,
) -> dict[str, Any] | None:
    block = await release_task_admission.start_block(
        session, project_id, version_number
    )
    if block is not None or not fresh_automatic:
        return block
    return await automatic_qualification_start_block(session, project_id)
