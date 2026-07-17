"""Ownership-neutral Resident successor promotion facade."""

from __future__ import annotations

import uuid

from app.models import StrategicResident, Task
from app.strategic_resident_runtime import promote_authoritative_successor


def promote_resident_successor(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
    *,
    mutation_authorized: bool,
    checkpoint_version: int | None = None,
    checkpoint_hash: str | None = None,
    authority_generation: int | None = None,
    engine_owner: str = "strategic_resident",
) -> Task | None:
    if mutation_authorized:
        return promote_authoritative_successor(
            resident,
            tasks,
            checkpoint_version=checkpoint_version,
            checkpoint_hash=checkpoint_hash,
            authority_generation=authority_generation,
            engine_owner=engine_owner,
        )
    from app.strategic_resident_turns import _promote_successor

    return _promote_successor(resident, tasks)


__all__ = ["promote_resident_successor"]
