"""Project ownership checks for typed durable Resident context references."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Artifact, ArtifactRef, ProjectMessage, Task
from app.strategic_operation_authority_validation import InactiveTurnAuthority


async def validate_durable_ref_ownership(
    session: AsyncSession,
    refs: list[dict[str, Any]],
    *,
    project_id: uuid.UUID,
) -> None:
    """Resolve typed durable IDs instead of trusting a missing project scope."""

    targets = (
        ("artifact_id", Artifact),
        ("artifact_ref_id", ArtifactRef),
        ("task_id", Task),
        ("message_id", ProjectMessage),
    )
    for field, model in targets:
        identities: set[uuid.UUID] = set()
        for ref in refs:
            raw = ref.get(field)
            if raw is None:
                continue
            try:
                identities.add(uuid.UUID(str(raw)))
            except (TypeError, ValueError):
                raise InactiveTurnAuthority(
                    "Resident turn contains an invalid durable reference",
                    reference_field=field,
                    reference_id=str(raw),
                ) from None
        if not identities:
            continue
        rows = (
            await session.execute(
                select(model.id, model.project_id).where(model.id.in_(identities))
            )
        ).all()
        resolved = {row[0]: row[1] for row in rows}
        for identity in identities:
            owner = resolved.get(identity)
            if owner is None:
                raise InactiveTurnAuthority(
                    "Resident turn contains an unknown durable reference",
                    reference_field=field,
                    reference_id=str(identity),
                )
            if owner != project_id:
                raise InactiveTurnAuthority(
                    "Resident turn contains a cross-project reference",
                    reference_field=field,
                    reference_id=str(identity),
                    referenced_project_id=str(owner),
                )


__all__ = ["validate_durable_ref_ownership"]
