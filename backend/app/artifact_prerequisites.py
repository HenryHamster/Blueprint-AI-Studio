"""Producer-side delivery for downstream artifact prerequisites."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app import storage
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    KnowledgeDocument,
    SourceKind,
    Task,
    TaskStatus,
)
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


async def compute_result_pointer_for_knowledge(
    task: Task,
    doc: KnowledgeDocument,
    session,
) -> dict[str, Any] | None:
    """Resolve the exact compute artifact represented by ``doc``.

    A compute knowledge document and its result bundle are two views of the same
    execution.  This helper bridges them without widening artifact access: the
    document, job, artifact, and producing task must all agree on project and
    producer identity.  Missing or legacy linkage is a fail-soft no-op.
    """
    if (
        doc.source_kind != SourceKind.COMPUTE_EXPERIMENT.value
        or doc.source_compute_job_id is None
        or doc.project_id != task.project_id
        or doc.source_task_id != task.id
    ):
        return None

    job = (
        (
            await session.execute(
                select(ComputeJob).where(
                    ComputeJob.id == doc.source_compute_job_id,
                    ComputeJob.project_id == task.project_id,
                    ComputeJob.task_id == task.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if job is None or job.artifact_id is None:
        return None

    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.id == job.artifact_id,
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.COMPUTE_RESULT,
                    Artifact.storage_uri.isnot(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if artifact is None or not (artifact.storage_uri or "").strip():
        return None
    if storage.compute_job_id_for_artifact_uri(artifact.storage_uri) != job.id:
        return None

    pointer = _artifact_pointer(artifact, str(task.id))
    pointer["source_compute_job_id"] = str(job.id)
    return pointer


async def backfill_artifact_prerequisites(task: Task, session) -> None:
    """Hydrate artifact_prerequisites from already-completed direct dependencies.

    Called at claim time so a dependent that was queued after its producer
    completed still receives artifact pointers before launch.  Idempotent:
    skips any pointer whose artifact_id is already present in the list.
    """
    if not task.depends_on:
        return
    try:
        dep_ids = list(task.depends_on)
        query = select(Task).where(
            Task.id.in_(dep_ids),
            Task.status == TaskStatus.COMPLETED,
            Task.project_id == task.project_id,
        )
        producers = (await session.execute(query)).scalars().all()
        if not producers:
            return

        artifact_query = (
            select(Artifact)
            .where(
                Artifact.task_id.in_([p.id for p in producers]),
                Artifact.logical_key.isnot(None),
            )
        )
        artifacts = (await session.execute(artifact_query)).scalars().all()
        compute_docs = (
            (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.project_id == task.project_id,
                        KnowledgeDocument.source_task_id.in_(
                            [p.id for p in producers]
                        ),
                        KnowledgeDocument.source_kind
                        == SourceKind.COMPUTE_EXPERIMENT.value,
                        KnowledgeDocument.source_compute_job_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        ctx = dict(task.context or {})
        existing = ctx.get(ARTIFACT_PREREQUISITES_KEY)
        prereqs = list(existing) if isinstance(existing, list) else []
        existing_ids = {
            p.get("artifact_id")
            for p in prereqs
            if isinstance(p, dict)
        }

        added = 0
        for artifact in artifacts:
            aid = str(artifact.id)
            if aid in existing_ids:
                continue
            pointer = _artifact_pointer(artifact, str(artifact.task_id))
            prereqs.append(pointer)
            existing_ids.add(aid)
            added += 1

        # A compute result delivered through its KnowledgeDocument is canonical
        # even when the Artifact has no plan-time logical_key. This covers the
        # document-before-child ordering: the dependent did not exist when the
        # producer completed, so claim-time backfill must follow the durable
        # document -> job -> artifact identity rather than selecting every
        # COMPUTE_RESULT row left by retries.
        producer_by_id = {producer.id: producer for producer in producers}
        for doc in compute_docs:
            producer = producer_by_id.get(doc.source_task_id)
            if producer is None:
                continue
            pointer = await compute_result_pointer_for_knowledge(
                producer, doc, session
            )
            if pointer is None or pointer["artifact_id"] in existing_ids:
                continue
            prereqs.append(pointer)
            existing_ids.add(pointer["artifact_id"])
            added += 1

        if added:
            ctx[ARTIFACT_PREREQUISITES_KEY] = prereqs
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)
            logger.info(
                "[ARTIFACT] Backfilled %d artifact prerequisite(s) onto task %s",
                added,
                task.id,
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ARTIFACT] backfill_artifact_prerequisites failed for task %s",
            task.id,
            exc_info=True,
        )


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
    pending_dependents = []
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
                pending_dependents.append(dep)

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

    if scan_failed:
        return

    for dep in pending_dependents:
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

    if touched:
        if commit:
            await session.commit()
        logger.info(
            "[ARTIFACT] Delivered artifact %s to %d dependent task(s)",
            artifact.id,
            touched,
        )
