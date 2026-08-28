"""Validation for operator-authored task authority attached at creation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.acceptance_criteria import (
    CRITERION_RELEASE_VERSION_CONTEXT_KEY,
    EXPLICIT_CRITERION_IDS_CONTEXT_KEY,
    OPERATOR_TASK_CREATION_AUTHORITY_CONTEXT_KEY,
    filter_active_criterion_keys,
)
from app.directions_repo import get_direction_row
from app.models import Project, Task, TASK_OUTCOME_TERMINAL_STATUSES, TaskStatus
from app.orchestrator_health import _same_base_branch_value
from app.release_version import current_open_version


def build_task_creation_authority_snapshot(
    authority: "TaskCreationAuthority",
    *,
    criterion_ids_explicit: bool,
    require_deps_landed_on: str | None,
    release_base_branch: str | None,
) -> dict[str, object]:
    """Return the immutable request authority used by idempotent replays."""

    return {
        "direction_id": authority.direction_id,
        "criterion_ids": list(authority.criterion_ids),
        "criterion_ids_explicit": criterion_ids_explicit,
        "criterion_release_version_id": (
            str(authority.criterion_release_version_id)
            if authority.criterion_release_version_id is not None
            else None
        ),
        "depends_on": list(authority.depends_on),
        "require_deps_landed_on": require_deps_landed_on,
        "release_base_branch": release_base_branch,
    }


def task_creation_authority_receipt(task: Task) -> dict[str, object]:
    """Return creation-time authority, falling back for pre-snapshot rows."""

    context = task.context or {}
    stored = context.get(OPERATOR_TASK_CREATION_AUTHORITY_CONTEXT_KEY)
    if isinstance(stored, dict):
        authority = dict(stored)
    else:
        first_payload = task.first_payload or {}
        authority = {
            "direction_id": context.get("direction_id")
            or first_payload.get("direction_id"),
            "criterion_ids": list(task.criterion_ids or []),
            "criterion_ids_explicit": (
                context.get(EXPLICIT_CRITERION_IDS_CONTEXT_KEY) is True
            ),
            "criterion_release_version_id": context.get(
                CRITERION_RELEASE_VERSION_CONTEXT_KEY
            ),
            "depends_on": list(task.depends_on or []),
            "require_deps_landed_on": first_payload.get(
                "require_deps_landed_on"
            ),
            "release_base_branch": first_payload.get("release_base_branch"),
        }
    return {
        "direction_id": authority.get("direction_id"),
        "criterion_ids": list(authority.get("criterion_ids") or []),
        "criterion_ids_explicit": authority.get("criterion_ids_explicit") is True,
        "criterion_release_version_id": authority.get(
            "criterion_release_version_id"
        ),
        "depends_on": list(authority.get("depends_on") or []),
        "require_deps_landed_on": authority.get("require_deps_landed_on"),
        "release_base_branch": authority.get("release_base_branch"),
    }


@dataclass(frozen=True)
class TaskCreationAuthority:
    """Canonical authority fields to persist with a newly queued task."""

    direction_id: str | None
    criterion_ids: list[str]
    criterion_release_version_id: uuid.UUID | None
    depends_on: list[str]


def reject_idempotent_task_authority_mismatch(
    existing: Task,
    *,
    direction_id: str | None,
    criterion_ids: list[str] | None,
    depends_on: list[uuid.UUID],
    require_deps_landed_on: str | None,
    release_base_branch: str | None,
) -> None:
    """Reject an idempotency replay that names different task authority."""

    authority = task_creation_authority_receipt(existing)
    expected_depends_on = [str(dep_id) for dep_id in depends_on]
    expected_criteria = list(criterion_ids or [])
    mismatches: list[str] = []
    if authority["direction_id"] != direction_id:
        mismatches.append("direction_id")
    if authority["criterion_ids"] != expected_criteria:
        mismatches.append("criterion_ids")
    if authority["criterion_ids_explicit"] != (criterion_ids is not None):
        mismatches.append("criterion_ids_mode")
    if authority["depends_on"] != expected_depends_on:
        mismatches.append("depends_on")
    if authority["require_deps_landed_on"] != require_deps_landed_on:
        mismatches.append("require_deps_landed_on")
    if authority["release_base_branch"] != release_base_branch:
        mismatches.append("release_base_branch")
    if mismatches:
        raise HTTPException(
            status_code=409,
            detail=(
                "idempotency_key already belongs to a task with different "
                "authority: " + ", ".join(mismatches)
            ),
        )


async def prepare_task_creation_authority(
    session: AsyncSession,
    *,
    project: Project,
    direction_id: str | None,
    criterion_ids: list[str] | None,
    depends_on: list[uuid.UUID],
    base_branch: str | None,
    require_deps_landed_on: str | None,
) -> TaskCreationAuthority:
    """Validate and normalize direction, criterion, and dependency authority.

    Explicit ``criterion_ids`` are deliberately exact.  They are not unioned
    with every key owned by ``direction_id``; this lets one bounded task prove a
    subset of a broader direction without claiming unrelated acceptance work.
    """

    direction = None
    if direction_id is not None:
        direction = await get_direction_row(session, project.id, direction_id)
        if direction is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown direction_id: {direction_id}"
            )
        direction_status = str(getattr(direction, "status", "") or "").lower()
        if direction_status != "active":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"direction_id {direction_id} is {direction_status or 'inactive'}; "
                    "operator task admission requires an active direction"
                ),
            )

    normalized_criterion_ids = list(criterion_ids or [])
    criterion_release_version_id: uuid.UUID | None = None
    if criterion_ids is not None:
        open_version = await current_open_version(session, project.id)
        if normalized_criterion_ids and open_version is None:
            raise HTTPException(
                status_code=409,
                detail="criterion_ids require an open release version",
            )
        if open_version is not None:
            criterion_release_version_id = open_version.id
        if normalized_criterion_ids:
            active = await filter_active_criterion_keys(
                session,
                project.id,
                normalized_criterion_ids,
                release_version_id=criterion_release_version_id,
            )
            active_set = set(active)
            invalid = [
                key for key in normalized_criterion_ids if key not in active_set
            ]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "criterion_ids_invalid",
                        "keys": invalid,
                        "reason": "criterion is unknown or inactive for the open version",
                    },
                )
            if direction is not None:
                direction_keys = set(direction.criterion_keys or [])
                outside_direction = [
                    key
                    for key in normalized_criterion_ids
                    if key not in direction_keys
                ]
                if outside_direction:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "criterion_ids_outside_direction",
                            "direction_id": direction_id,
                            "keys": outside_direction,
                        },
                    )

    normalized_depends_on = [str(dep_id) for dep_id in depends_on]
    if len(set(normalized_depends_on)) != len(normalized_depends_on):
        raise HTTPException(
            status_code=400, detail="depends_on must not contain duplicate task IDs"
        )
    if normalized_depends_on:
        dep_uuids = [uuid.UUID(dep_id) for dep_id in normalized_depends_on]
        dependency_rows = (
            (
                await session.execute(
                    select(Task).where(
                        Task.id.in_(dep_uuids),
                        Task.project_id == project.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        dependencies_by_id = {str(task.id): task for task in dependency_rows}
        missing = [
            dep_id
            for dep_id in normalized_depends_on
            if dep_id not in dependencies_by_id
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "depends_on_outside_project",
                    "task_ids": missing,
                },
            )
        for dep_id in normalized_depends_on:
            dependency = dependencies_by_id[dep_id]
            if (
                dependency.status in TASK_OUTCOME_TERMINAL_STATUSES
                and dependency.status != TaskStatus.COMPLETED
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"depends_on task {dep_id} is terminal non-success and cannot "
                        "be a prerequisite"
                    ),
                )
            dependency_base = (dependency.first_payload or {}).get("base_branch")
            if require_deps_landed_on is None and not _same_base_branch_value(
                base_branch, dependency_base
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Cross-base dependency rejected: this task's base branch "
                        f"'{base_branch or '(default)'}' differs from dependency "
                        f"{dep_id}'s base '{dependency_base or '(default)'}'. "
                        "Align the base branches or declare require_deps_landed_on."
                    ),
                )
        await _reject_dependency_cycles(
            session,
            project_id=project.id,
            foundations=[
                dependencies_by_id[dep_id] for dep_id in normalized_depends_on
            ],
        )

    return TaskCreationAuthority(
        direction_id=direction_id,
        criterion_ids=normalized_criterion_ids,
        criterion_release_version_id=criterion_release_version_id,
        depends_on=normalized_depends_on,
    )


async def _reject_dependency_cycles(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    foundations: list[Task],
) -> None:
    """Reject a dependency set that already contains a reachable cycle."""

    if not foundations:
        return
    cache: dict[str, Task] = {str(task.id): task for task in foundations}

    async def _has_cycle(task: Task, path: set[str]) -> bool:
        task_id = str(task.id)
        if task_id in path:
            return True
        path.add(task_id)
        for raw_dep_id in task.depends_on or []:
            try:
                dep_id = str(uuid.UUID(str(raw_dep_id)))
            except (ValueError, AttributeError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "depends_on prerequisite graph contains an invalid task "
                        f"ID: {raw_dep_id!r}"
                    ),
                ) from None
            dependency = cache.get(dep_id)
            if dependency is None:
                dependency = (
                    await session.execute(
                        select(Task).where(
                            Task.id == uuid.UUID(dep_id),
                            Task.project_id == project_id,
                        )
                    )
                ).scalar_one_or_none()
                if dependency is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "depends_on prerequisite graph contains a missing or "
                            f"cross-project task: {dep_id}"
                        ),
                    )
                cache[dep_id] = dependency
            if await _has_cycle(dependency, path):
                return True
        path.discard(task_id)
        return False

    for foundation in foundations:
        if await _has_cycle(foundation, set()):
            raise HTTPException(
                status_code=400,
                detail=(
                    "depends_on declares a cyclic prerequisite set (a "
                    "prerequisite transitively depends on itself); remove the "
                    "cyclic edges"
                ),
            )
