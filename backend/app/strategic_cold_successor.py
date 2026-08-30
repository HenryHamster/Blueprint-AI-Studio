"""Exact durable-authority packet for a transcript-free strategic successor.

This module deliberately stops at producing a validated task payload.  Launch,
claim, and attestation are separate authority transitions; keeping packet
construction read-only makes the source and byte-budget contract independently
testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeDocument, Project, SourceKind
from app.payload_schemas import validate_internal_payload
from app.strategic_memory_sources import canonical_json_bytes, sha256_hex


COLD_SUCCESSOR_SCHEMA_VERSION = 1
_HANDOFF_MEMORY_TAG = "handoff"
_PAYLOAD_CONTEXT = "strategic_cold_successor:strategic_review"


class ColdSuccessorContractError(ValueError):
    """The durable sources cannot safely authorize a cold successor."""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ColdSuccessorContractError(f"strategic memory {field} must be text")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _memory_document(document: KnowledgeDocument) -> dict[str, Any]:
    if document.source_kind != SourceKind.STRATEGIC_MEMORY.value:
        raise ColdSuccessorContractError(
            "cold successor memory must have source_kind=strategic_memory"
        )
    if document.lifecycle_status != "consumed":
        raise ColdSuccessorContractError(
            "cold successor memory must have lifecycle_status=consumed"
        )
    if document.id is None:
        raise ColdSuccessorContractError("strategic memory id is required")
    if not isinstance(document.created_at, datetime):
        raise ColdSuccessorContractError("strategic memory created_at is required")
    tags = document.tags
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ColdSuccessorContractError("strategic memory tags must be text")
    return {
        "id": str(document.id),
        "title": _require_text(document.title, "title"),
        "summary": _require_text(document.summary, "summary"),
        "body": _require_text(document.body, "body"),
        "topic": _optional_text(document.topic, "topic"),
        "direction_id": _optional_text(document.direction_id, "direction_id"),
        "tags": list(tags),
        "created_at": document.created_at.isoformat(),
    }


def _section(payload: bytes, *, encoding: str) -> dict[str, Any]:
    return {
        "encoding": encoding,
        "byte_count": len(payload),
        "sha256": sha256_hex(payload),
    }


def serialize_cold_successor_contract(
    project: Project,
    memory_documents: Iterable[KnowledgeDocument],
) -> dict[str, Any]:
    """Return one exact, non-degrading ``strategic_review`` task payload.

    Problem Model and strategic memory travel as canonical JSON *text*, so a
    JSONB round trip cannot change the bytes whose hashes authorize the
    successor.  Running Update remains raw text and preserves ``None`` versus
    the empty string.  No conversation, session, cursor, summary, or decision
    state is consulted or serialized.
    """

    if project.id is None:
        raise ColdSuccessorContractError("project id is required")
    if project.paused is not True:
        raise ColdSuccessorContractError("project must be paused")
    if project.autonomous_enabled is not False:
        raise ColdSuccessorContractError("project autonomy must be disabled")
    if project.archived is not False:
        raise ColdSuccessorContractError("archived project cannot launch a successor")
    if not isinstance(project.problem_model, dict):
        raise ColdSuccessorContractError("problem_model must be a JSON object")
    if project.running_update is not None and not isinstance(
        project.running_update, str
    ):
        raise ColdSuccessorContractError("running_update must be text or null")

    active_memory = [_memory_document(document) for document in memory_documents]
    memory = [
        item for item in active_memory if _HANDOFF_MEMORY_TAG in item["tags"]
    ]
    if len(memory) != 1:
        raise ColdSuccessorContractError(
            "cold successor requires exactly one active strategic memory tagged "
            f"'handoff'; found {len(memory)}"
        )
    memory.sort(key=lambda item: (item["created_at"], item["id"]))

    problem_model_bytes = canonical_json_bytes(project.problem_model)
    memory_bytes = canonical_json_bytes(memory)
    running_update_bytes = (
        canonical_json_bytes(None)
        if project.running_update is None
        else project.running_update.encode("utf-8")
    )

    core = {
        "schema_version": COLD_SUCCESSOR_SCHEMA_VERSION,
        "project_id": str(project.id),
        "safety": {
            "paused": True,
            "autonomous_enabled": False,
            "archived": False,
        },
        "problem_model_canonical_json": problem_model_bytes.decode("utf-8"),
        "running_update": {
            "present": project.running_update is not None,
            "text": project.running_update,
        },
        "strategic_memory_canonical_json": memory_bytes.decode("utf-8"),
    }
    core_bytes = canonical_json_bytes(core)
    contract = {
        "core": core,
        "manifest": {
            "problem_model": _section(problem_model_bytes, encoding="canonical-json"),
            "running_update": _section(
                running_update_bytes,
                encoding=("utf-8" if project.running_update is not None else "json-null"),
            ),
            "strategic_memory": {
                **_section(memory_bytes, encoding="canonical-json"),
                "document_count": len(memory),
            },
            "core": _section(core_bytes, encoding="canonical-json"),
        },
    }
    return validate_internal_payload(
        {"type": "strategic_review", "cold_successor_contract": contract},
        _PAYLOAD_CONTEXT,
    )


async def load_locked_cold_successor_contract(
    session: AsyncSession,
    project_id: UUID,
) -> dict[str, Any]:
    """Build the contract from one Project-locked durable-source snapshot.

    Canonical Problem Model, Running Update, and ``set_memory`` writers acquire
    the Project row first.  Holding that same lock across the memory read keeps
    the three sources from forming a mixed-revision packet.  This function does
    not flush or commit.
    """

    project = (
        await session.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None:
        raise ColdSuccessorContractError("project not found")

    memory_documents = (
        await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.project_id == project.id,
                KnowledgeDocument.source_kind == SourceKind.STRATEGIC_MEMORY.value,
                KnowledgeDocument.lifecycle_status == "consumed",
            )
            .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
        )
    ).scalars().all()
    return serialize_cold_successor_contract(project, memory_documents)
