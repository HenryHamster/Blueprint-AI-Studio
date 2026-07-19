"""Final live-state checkpoint boundary for legacy-to-Resident cutover."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMessage,
    StrategicResident,
    StrategicResidentCheckpoint,
)
from app.project_message_sequence_fence import (
    lock_project_message_sequence_boundary,
)
from app.strategic_resident_checkpointing import (
    CHECKPOINT_NAMESPACE,
    build_initial_strategic_checkpoint,
)


CUTOVER_CHECKPOINT_AUTHORIZATION_KEY = "_resident_cutover_checkpoint_authorization"


@dataclass(frozen=True)
class CutoverBoundary:
    checkpoint: StrategicResidentCheckpoint
    handled_message_sequence: int
    handled_project_change_cursor: dict[str, Any]
    pending_message_sequence_start: int | None
    pending_message_sequence_end: int | None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


async def _conversation_boundary(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, int | None, int | None]:
    unresolved = (
        MessageHandlingState.PENDING,
        MessageHandlingState.CLAIMED,
    )
    first_unresolved, newest_conversation = (
        await session.execute(
            select(
                func.min(
                    case(
                        (
                            (ProjectMessage.from_type == "user")
                            & ProjectMessage.handling_state.in_(unresolved),
                            ProjectMessage.sequence,
                        )
                    )
                ),
                func.max(ProjectMessage.sequence),
            ).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.surface == MessageSurface.CONVERSATION,
                ProjectMessage.sequence.is_not(None),
            )
        )
    ).one()
    if first_unresolved is not None:
        return (
            max(0, int(first_unresolved) - 1),
            int(first_unresolved),
            int(newest_conversation or first_unresolved),
        )
    return max(0, int(newest_conversation or 0)), None, None


def _revision(value: Any) -> int | str | None:
    if not isinstance(value, dict):
        return None
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, (int, str)):
        return None
    return revision if isinstance(revision, int) else revision[:160]


def _project_change_baseline(
    project: Project,
    *,
    cutover_at: datetime,
) -> dict[str, Any]:
    directions = project.directions if isinstance(project.directions, list) else []
    running_update = str(project.running_update or "")
    updated_at = getattr(project, "updated_at", None)
    return {
        "baseline_kind": "legacy_cutover",
        "cutover_at": cutover_at.isoformat(),
        "project_updated_at": (
            updated_at.isoformat()
            if isinstance(updated_at, datetime)
            else None
        ),
        "vision_revision": _revision(project.vision),
        "problem_model_revision": _revision(project.problem_model),
        "directions_hash": hashlib.sha256(_canonical(directions)).hexdigest(),
        "running_update_hash": hashlib.sha256(
            running_update.encode("utf-8")
        ).hexdigest(),
    }


def _boundary_source_task_id(
    project: Project,
    *,
    version: int,
) -> uuid.UUID:
    vision = project.vision if isinstance(project.vision, dict) else {}
    blob = vision.get("_resident_strategic_session")
    if isinstance(blob, dict) and blob.get("task_id"):
        try:
            return uuid.UUID(str(blob["task_id"]))
        except (TypeError, ValueError):
            pass
    return uuid.uuid5(
        CHECKPOINT_NAMESPACE,
        f"{project.id}:cutover-boundary:{version}",
    )


async def append_live_cutover_checkpoint(
    session: AsyncSession,
    *,
    project: Project,
    resident: StrategicResident,
    current: StrategicResidentCheckpoint,
    next_generation: int,
    cutover_at: datetime,
) -> CutoverBoundary:
    """Append an exact live Project boundary while the Project is locked."""

    await lock_project_message_sequence_boundary(session, project.id)
    transition_id = uuid.uuid4()
    version = int(current.version) + 1
    source_turn_task_id = _boundary_source_task_id(project, version=version)
    body, _initial_hash = build_initial_strategic_checkpoint(
        project,
        source="ownership_cutover",
    )
    (
        handled_message_sequence,
        pending_message_sequence_start,
        pending_message_sequence_end,
    ) = await _conversation_boundary(session, project.id)
    project_cursor = _project_change_baseline(project, cutover_at=cutover_at)
    cursors = body.setdefault("cursors", {})
    cursors["handled_message_sequence"] = handled_message_sequence
    cursors["project_change"] = project_cursor
    migration = body.setdefault("migration", {})
    pending_project_change = (
        resident.scheduling_state.get("pending_project_change_cursor")
        if isinstance(resident.scheduling_state, dict)
        else None
    )
    migration["cutover_boundary"] = {
        "transition_id": str(transition_id),
        "previous_owner": "legacy",
        "previous_authority_generation": int(resident.authority_generation),
        "previous_checkpoint_id": str(current.id),
        "previous_checkpoint_version": int(current.version),
        "previous_checkpoint_hash": str(current.content_hash),
        "authority_generation": int(next_generation),
        "source_turn_task_id": str(source_turn_task_id),
        "pending_project_change_cursor": (
            pending_project_change
            if isinstance(pending_project_change, dict)
            else None
        ),
    }
    encoded = _canonical(body)
    content_hash = hashlib.sha256(encoded).hexdigest()
    checkpoint = StrategicResidentCheckpoint(
        id=uuid.uuid5(
            CHECKPOINT_NAMESPACE,
            (
                f"{project.id}:cutover:{version}:{next_generation}:"
                f"{content_hash}"
            ),
        ),
        project_id=project.id,
        version=version,
        schema_version=int(body.get("schema_version") or 1),
        content_hash=content_hash,
        checkpoint=body,
        authority_generation=int(next_generation),
        source_turn_task_id=source_turn_task_id,
    )
    authorization = {
        "transition_id": str(transition_id),
        "project_id": str(project.id),
        "previous_owner": "legacy",
        "previous_authority_generation": int(resident.authority_generation),
        "next_authority_generation": int(next_generation),
        "previous_checkpoint_id": str(current.id),
        "previous_checkpoint_version": int(current.version),
        "previous_checkpoint_hash": str(current.content_hash),
        "checkpoint_id": str(checkpoint.id),
        "checkpoint_version": int(checkpoint.version),
        "checkpoint_hash": str(checkpoint.content_hash),
        "source_turn_task_id": str(source_turn_task_id),
    }
    resident.rollout_state = {
        **dict(resident.rollout_state or {}),
        CUTOVER_CHECKPOINT_AUTHORIZATION_KEY: authorization,
    }
    session.add(resident)
    # The database trigger sees only flushed state. Stage this exact, private
    # authorization while the locked Resident is still legacy-owned, then append
    # the checkpoint. The enclosing ownership transaction removes the marker.
    await session.flush()
    session.add(checkpoint)
    await session.flush()
    return CutoverBoundary(
        checkpoint=checkpoint,
        handled_message_sequence=handled_message_sequence,
        handled_project_change_cursor=project_cursor,
        pending_message_sequence_start=pending_message_sequence_start,
        pending_message_sequence_end=pending_message_sequence_end,
    )


__all__ = [
    "CUTOVER_CHECKPOINT_AUTHORIZATION_KEY",
    "CutoverBoundary",
    "append_live_cutover_checkpoint",
]
