"""Producer-side delivery for downstream artifact prerequisites."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Artifact, Task, TaskStatus
from app.payload_schemas import read_acceptance_evidence

logger = logging.getLogger(__name__)

ARTIFACT_PREREQUISITES_KEY = "artifact_prerequisites"
_DEPENDENT_SCAN_CAP = 100


def downstream_artifact_logical_key(task: Task | None) -> str | None:
    """Return the plan-time downstream_artifact slug declared by ``task``."""
    if task is None:
        return None
    evidence = read_acceptance_evidence(task.first_payload)
    if evidence is None or evidence.kind != "downstream_artifact":
        return None
    logical_key = (evidence.artifact_id or "").strip()
    return logical_key or None


def stamp_artifact_logical_key(artifact: Artifact, task: Task | None) -> str | None:
    """Stamp ``artifact.logical_key`` from the producer's acceptance evidence."""
    logical_key = downstream_artifact_logical_key(task)
    if logical_key:
        artifact.logical_key = logical_key
    return logical_key


def _artifact_pointer(artifact: Artifact, source_task_id: str) -> dict[str, Any]:
    pointer: dict[str, Any] = {
        "artifact_id": str(artifact.id),
        "logical_key": artifact.logical_key,
        "artifact_type": (
            artifact.artifact_type.value
            if hasattr(artifact.artifact_type, "value")
            else artifact.artifact_type
        ),
        "source_task_id": source_task_id,
    }
    if artifact.storage_uri:
        pointer["storage_uri"] = artifact.storage_uri
    return pointer


async def deliver_artifact_to_dependents(
    task: Task,
    artifact: Artifact,
    session,
    *,
    commit: bool = False,
) -> None:
    """Best-effort stamp an artifact pointer onto queued dependent tasks.

    Delivery mirrors ``knowledge_prerequisites``: queued tasks that directly
    depend on the producer receive compact, deduped artifact pointers in context.
    This does not record consumption and does not make a legacy artifact without
    a logical key count as proof; it only makes a real Artifact row discoverable
    to a dependent when it later launches.
    """
    task_id_str = str(task.id)
    pointer = _artifact_pointer(artifact, task_id_str)
    touched = 0
    last_created_at = None
    last_id = None
    scan_failed = False
    try:
        while True:
            query = (
                select(Task)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    Task.project_id == task.project_id,
                    Task.depends_on.isnot(None),
                )
                .order_by(Task.created_at, Task.id)
                .limit(_DEPENDENT_SCAN_CAP)
            )
            if last_created_at is not None and last_id is not None:
                query = query.where(
                    or_(
                        Task.created_at > last_created_at,
                        and_(Task.created_at == last_created_at, Task.id > last_id),
                    )
                )
            dependents = (await session.execute(query)).scalars().all()
            if not dependents:
                break

            for dep in dependents:
                if task_id_str not in (dep.depends_on or []):
                    continue
                ctx = dict(dep.context or {})
                existing = ctx.get(ARTIFACT_PREREQUISITES_KEY)
                prereqs = list(existing) if isinstance(existing, list) else []
                if any(
                    isinstance(p, dict) and p.get("artifact_id") == pointer["artifact_id"]
                    for p in prereqs
                ):
                    continue
                prereqs.append(pointer)
                ctx[ARTIFACT_PREREQUISITES_KEY] = prereqs
                dep.context = ctx
                flag_modified(dep, "context")
                session.add(dep)
                touched += 1

            last = dependents[-1]
            if last.created_at is None or last.id is None:
                break
            last_created_at = last.created_at
            last_id = last.id
            if len(dependents) < _DEPENDENT_SCAN_CAP:
                break
    except Exception:  # noqa: BLE001
        scan_failed = True
        logger.warning(
            "[ARTIFACT] dependent scan failed for task %s; artifact not delivered",
            task.id,
            exc_info=True,
        )

    if touched:
        if commit:
            await session.commit()
        logger.info(
            "[ARTIFACT] Delivered artifact %s to %d dependent task(s)",
            artifact.id,
            touched,
        )
    elif scan_failed:
        return
