"""Shared immutable verification for a Resident checkpoint boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import StrategicResident, StrategicResidentCheckpoint


class ResidentCheckpointIntegrityError(ValueError):
    """The locked checkpoint cannot be trusted as an immutable boundary."""


@dataclass(frozen=True)
class VerifiedResidentCheckpoint:
    """Canonical immutable snapshot of a verified locked checkpoint."""

    checkpoint_id: uuid.UUID
    project_id: uuid.UUID
    version: int
    schema_version: int
    content_hash: str
    authority_generation: int
    canonical_body: bytes

    def mutable_body(self) -> dict[str, Any]:
        """Return a detached JSON copy only after integrity was established."""

        value = json.loads(self.canonical_body)
        if not isinstance(value, dict):  # Defensive: verified snapshots are objects.
            raise ResidentCheckpointIntegrityError(
                "Resident checkpoint canonical body is malformed"
            )
        return value


def _canonical_body(value: Any) -> bytes:
    if not isinstance(value, dict):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint body is malformed"
        )
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


async def verify_locked_current_checkpoint(
    session: AsyncSession,
    resident: StrategicResident,
    checkpoint: StrategicResidentCheckpoint,
) -> VerifiedResidentCheckpoint:
    """Verify project, pointer, latest lineage, schema, and content under locks."""

    if (
        checkpoint.project_id != resident.project_id
        or resident.current_checkpoint_id != checkpoint.id
    ):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint project or pointer lineage is invalid"
        )
    latest = (
        await session.execute(
            select(StrategicResidentCheckpoint)
            .where(StrategicResidentCheckpoint.project_id == resident.project_id)
            .order_by(
                StrategicResidentCheckpoint.version.desc(),
                StrategicResidentCheckpoint.id.desc(),
            )
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        latest is None
        or latest.id != checkpoint.id
        or int(latest.version) != int(checkpoint.version)
    ):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint is not the latest checkpoint"
        )
    body_schema_version = checkpoint.checkpoint.get("schema_version") if isinstance(
        checkpoint.checkpoint, dict
    ) else None
    if (
        isinstance(body_schema_version, bool)
        or not isinstance(body_schema_version, int)
        or int(body_schema_version) != int(checkpoint.schema_version)
        or int(checkpoint.schema_version) < 1
    ):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint schema version is inconsistent"
        )
    if int(checkpoint.authority_generation) > int(resident.authority_generation):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint authority generation is from the future"
        )
    canonical_body = _canonical_body(checkpoint.checkpoint)
    observed_hash = hashlib.sha256(canonical_body).hexdigest()
    if not hmac.compare_digest(observed_hash, str(checkpoint.content_hash)):
        raise ResidentCheckpointIntegrityError(
            "Resident current checkpoint content hash is invalid"
        )
    return VerifiedResidentCheckpoint(
        checkpoint_id=checkpoint.id,
        project_id=checkpoint.project_id,
        version=int(checkpoint.version),
        schema_version=int(checkpoint.schema_version),
        content_hash=str(checkpoint.content_hash),
        authority_generation=int(checkpoint.authority_generation),
        canonical_body=canonical_body,
    )


__all__ = [
    "ResidentCheckpointIntegrityError",
    "VerifiedResidentCheckpoint",
    "verify_locked_current_checkpoint",
]
