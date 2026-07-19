"""Revision-safe handoff between human-owned project tools and the Resident."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    ArtifactRef,
    ArtifactRefPromotionIntent,
    ProjectMember,
    ProjectMessageAttachmentUpload,
    ProjectTool,
    Task,
    _utcnow,
)
from app.task_locking import lock_project_or_429


NOTES_SNAPSHOT_CHAR_CAP = 16_000
ARTIFACT_REF_KINDS = frozenset({"notes", "whiteboard"})
SAFE_RENDERED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
RESIDENT_ARTIFACT_REF_GRANTS_KEY = "_resident_artifact_ref_grants"
RESIDENT_ARTIFACT_REF_GRANTS_MAX = 256


@dataclass(frozen=True)
class ArtifactRefConflict(RuntimeError):
    expected_revision: int
    current_revision: int

    def __str__(self) -> str:
        return (
            "Project tool revision changed "
            f"(expected={self.expected_revision}, current={self.current_revision})"
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def content_checksum(content: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(_canonical_json(dict(content or {}))).hexdigest()


def _bounded_notes_snapshot(content: Mapping[str, Any]) -> dict[str, Any]:
    text = content.get("text")
    text = text if isinstance(text, str) else ""
    truncated = len(text) > NOTES_SNAPSHOT_CHAR_CAP
    return {
        "text": text[:NOTES_SNAPSHOT_CHAR_CAP],
        "truncated": truncated,
        "original_char_count": len(text),
    }


def _whiteboard_snapshot(content: Mapping[str, Any]) -> dict[str, Any]:
    strokes = content.get("strokes")
    nodes = content.get("nodes")
    nodes = nodes if isinstance(nodes, list) else []
    return {
        "stroke_count": len(strokes) if isinstance(strokes, list) else 0,
        "node_count": len(nodes),
        "text_node_count": sum(
            1
            for node in nodes
            if isinstance(node, dict) and node.get("type") == "text"
        ),
        "image_node_count": sum(
            1
            for node in nodes
            if isinstance(node, dict) and node.get("type") == "image"
        ),
        "document_node_count": sum(
            1
            for node in nodes
            if isinstance(node, dict) and node.get("type") == "document"
        ),
    }


def artifact_ref_projection(ref: ArtifactRef) -> dict[str, Any]:
    snapshot = ref.content_snapshot if isinstance(ref.content_snapshot, dict) else {}
    retrieval = (
        ref.retrieval_metadata if isinstance(ref.retrieval_metadata, dict) else {}
    )
    projection: dict[str, Any] = {
        "id": str(ref.id),
        "artifact_ref_id": str(ref.id),
        "tool_id": str(ref.tool_id),
        "project_id": str(ref.project_id),
        "kind": ref.kind,
        "label": "Notes" if ref.kind == "notes" else "Whiteboard",
        "title": "Notes" if ref.kind == "notes" else "Whiteboard",
        "revision": ref.revision,
        "checksum": ref.checksum,
        "author": ref.author_user_id,
        "timestamp": ref.created_at.isoformat(),
        "retrieval": {
            "capability": str(
                retrieval.get("capability") or "project_tool_revision"
            ),
            "href": f"/api/agent/resident-turn/artifact?artifact_ref_id={ref.id}",
        },
    }
    if ref.kind == "notes":
        projection["excerpt"] = str(snapshot.get("text") or "")[:2_000]
    return projection


def resident_artifact_ref_grants(task: Task) -> frozenset[uuid.UUID]:
    context = task.context if isinstance(task.context, dict) else {}
    raw = context.get(RESIDENT_ARTIFACT_REF_GRANTS_KEY)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or len(raw) > RESIDENT_ARTIFACT_REF_GRANTS_MAX:
        raise ValueError("Resident ArtifactRef grants are malformed")
    try:
        return frozenset(uuid.UUID(str(value)) for value in raw)
    except (TypeError, ValueError):
        raise ValueError("Resident ArtifactRef grants are malformed") from None


def artifact_ref_grants_from_message_context(
    message_context: Mapping[str, Any] | None,
) -> frozenset[uuid.UUID]:
    """Extract exact tool-ref identities from already-canonical message context."""

    raw_refs = (message_context or {}).get("artifact_refs")
    raw_refs = raw_refs if isinstance(raw_refs, list) else []
    identities: set[uuid.UUID] = set()
    for ref in raw_refs:
        if not isinstance(ref, dict) or ref.get("artifact_ref_id") is None:
            continue
        try:
            identities.add(uuid.UUID(str(ref["artifact_ref_id"])))
        except (TypeError, ValueError):
            raise ValueError("canonical message contains malformed ArtifactRef") from None
    if len(identities) > RESIDENT_ARTIFACT_REF_GRANTS_MAX:
        raise ValueError("Resident ArtifactRef grant limit exceeded")
    return frozenset(identities)


def union_resident_artifact_ref_grants(
    task: Task,
    artifact_ref_grants: frozenset[uuid.UUID],
) -> None:
    """Union canonical grants into a new or still-mutable Resident turn."""

    merged = resident_artifact_ref_grants(task) | artifact_ref_grants
    if len(merged) > RESIDENT_ARTIFACT_REF_GRANTS_MAX:
        raise ValueError("Resident ArtifactRef grant limit exceeded")
    if not merged:
        return
    context = dict(task.context or {})
    context[RESIDENT_ARTIFACT_REF_GRANTS_KEY] = sorted(str(value) for value in merged)
    task.context = context


def merge_resident_artifact_ref_grants(target: Task, source: Task) -> None:
    union_resident_artifact_ref_grants(target, resident_artifact_ref_grants(source))


async def create_tool_artifact_ref(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tool_id: uuid.UUID,
    actor_user_id: str,
    expected_revision: int,
    rendered_image: Mapping[str, Any] | None = None,
    promotion_intent_id: uuid.UUID | None = None,
    promotion_claim_id: uuid.UUID | None = None,
) -> ArtifactRef:
    """Freeze exactly ``expected_revision`` or fail without sharing stale work."""

    await lock_project_or_429(session, project_id)
    member = (
        await session.execute(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_user_id,
                ProjectMember.role.in_(["member", "owner"]),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        raise PermissionError("actor no longer has member access to this project")
    tool = (
        await session.execute(
            select(ProjectTool)
            .where(ProjectTool.id == tool_id, ProjectTool.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if tool is None:
        raise LookupError("project tool not found")
    if tool.kind not in ARTIFACT_REF_KINDS:
        raise ValueError("only Notes and Whiteboard tools can be shared")
    if expected_revision != tool.content_revision:
        raise ArtifactRefConflict(expected_revision, tool.content_revision)

    content = tool.content if isinstance(tool.content, dict) else {}
    checksum = content_checksum(content)
    existing = (
        await session.execute(
            select(ArtifactRef).where(
                ArtifactRef.project_id == project_id,
                ArtifactRef.tool_id == tool_id,
                ArtifactRef.revision == expected_revision,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.checksum != checksum:
            raise RuntimeError("immutable ArtifactRef checksum mismatch")
        if tool.kind == "whiteboard" and rendered_image:
            supplied_receipt = str(
                rendered_image.get("attachment_id")
                or rendered_image.get("id")
                or ""
            )
            existing_receipt = str(
                (existing.retrieval_metadata or {}).get("attachment_id") or ""
            )
            if supplied_receipt != existing_receipt:
                try:
                    redundant_id = uuid.UUID(supplied_receipt)
                except (TypeError, ValueError):
                    raise ValueError("invalid redundant rendered image receipt") from None
                redundant = (
                    await session.execute(
                        select(ProjectMessageAttachmentUpload)
                        .where(
                            ProjectMessageAttachmentUpload.attachment_id
                            == redundant_id
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    redundant is None
                    or redundant.project_id != project_id
                    or redundant.actor_user_id != actor_user_id
                    or redundant.consumed_at is not None
                    or redundant.message_id is not None
                    or redundant.artifact_ref_id is not None
                    or redundant.expires_at <= _utcnow()
                    or redundant.storage_uri
                    != str(rendered_image.get("storage_uri") or "").strip()
                    or str(redundant.content_type or "").lower()
                    not in SAFE_RENDERED_IMAGE_TYPES
                ):
                    raise ValueError("invalid redundant rendered image receipt")
                now = _utcnow()
                redundant.expires_at = max(
                    now, redundant.created_at + timedelta(microseconds=1)
                )
                session.add(redundant)
        return existing

    retrieval: dict[str, Any]
    if tool.kind == "notes":
        snapshot = _bounded_notes_snapshot(content)
        retrieval = {
            "capability": "inline_bounded_text",
            "content_truncated": snapshot["truncated"],
        }
    else:
        image = dict(rendered_image or {})
        try:
            attachment_id = uuid.UUID(
                str(image.get("attachment_id") or image.get("id") or "")
            )
        except (TypeError, ValueError):
            raise ValueError("whiteboard handoff requires a rendered image upload") from None
        receipt = (
            await session.execute(
                select(ProjectMessageAttachmentUpload)
                .where(ProjectMessageAttachmentUpload.attachment_id == attachment_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            receipt is None
            or receipt.project_id != project_id
            or receipt.actor_user_id != actor_user_id
            or receipt.consumed_at is not None
            or receipt.message_id is not None
            or receipt.artifact_ref_id is not None
            or receipt.storage_uri != str(image.get("storage_uri") or "").strip()
        ):
            raise ValueError("invalid rendered image upload receipt")
        if str(receipt.content_type or "").lower() not in SAFE_RENDERED_IMAGE_TYPES:
            raise ValueError("rendered image must be PNG, JPEG, or WebP")
        declared_checksum = str(image.get("checksum") or "").strip().lower()
        if len(declared_checksum) != 64 or any(
            char not in "0123456789abcdef" for char in declared_checksum
        ):
            raise ValueError("rendered image checksum must be lowercase SHA-256")
        from app.storage import artifact_ref_render_uri

        expected_storage_uri = artifact_ref_render_uri(
            project_id,
            tool_id,
            expected_revision,
            attachment_id,
        )
        durable_storage_uri = str(
            image.get("_durable_storage_uri") or ""
        ).strip()
        if durable_storage_uri != expected_storage_uri:
            raise ValueError("whiteboard handoff requires a promoted rendered image")
        if promotion_intent_id is None or promotion_claim_id is None:
            raise ValueError("whiteboard handoff requires a durable promotion claim")
        promotion_intent = (
            await session.execute(
                select(ArtifactRefPromotionIntent)
                .where(ArtifactRefPromotionIntent.id == promotion_intent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = _utcnow()
        if (
            promotion_intent is None
            or promotion_intent.project_id != project_id
            or promotion_intent.tool_id != tool_id
            or promotion_intent.revision != expected_revision
            or promotion_intent.receipt_id != attachment_id
            or promotion_intent.actor_user_id != actor_user_id
            or promotion_intent.candidate_storage_uri != expected_storage_uri
            or promotion_intent.source_storage_uri != receipt.storage_uri
            or promotion_intent.expected_size_bytes != receipt.expected_size_bytes
            or promotion_intent.expected_sha256 != declared_checksum
            or promotion_intent.render_width
            != (image.get("width") if isinstance(image.get("width"), int) else None)
            or promotion_intent.render_height
            != (image.get("height") if isinstance(image.get("height"), int) else None)
            or promotion_intent.state != "promoted"
            or promotion_intent.claim_id != promotion_claim_id
            or promotion_intent.lease_expires_at is None
            or promotion_intent.lease_expires_at <= now
            or promotion_intent.destination_generation is None
        ):
            raise PermissionError("render promotion authority expired or changed")
        snapshot = _whiteboard_snapshot(content)
        retrieval = {
            "capability": "project_tool_rendered_image",
            "storage_uri": durable_storage_uri,
            "attachment_id": str(receipt.attachment_id),
            "image_checksum": declared_checksum,
            "mime_type": receipt.content_type or "image/png",
            "size_bytes": receipt.expected_size_bytes,
            "width": image.get("width") if isinstance(image.get("width"), int) else None,
            "height": image.get("height") if isinstance(image.get("height"), int) else None,
        }

    ref = ArtifactRef(
        project_id=project_id,
        tool_id=tool_id,
        kind=tool.kind,
        revision=expected_revision,
        checksum=checksum,
        author_user_id=actor_user_id,
        content_snapshot=snapshot,
        retrieval_metadata=retrieval,
    )
    session.add(ref)
    await session.flush()
    if tool.kind == "whiteboard":
        receipt.consumed_at = _utcnow()
        receipt.artifact_ref_id = ref.id
        session.add(receipt)
        promotion_intent.state = "owned"
        promotion_intent.artifact_ref_id = ref.id
        promotion_intent.completed_at = _utcnow()
        promotion_intent.updated_at = promotion_intent.completed_at
        promotion_intent.claim_id = None
        promotion_intent.lease_expires_at = None
        promotion_intent.last_error = None
        session.add(promotion_intent)
    return ref


async def get_project_artifact_ref(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    artifact_ref_id: uuid.UUID,
) -> ArtifactRef | None:
    return (
        await session.execute(
            select(ArtifactRef).where(
                ArtifactRef.id == artifact_ref_id,
                ArtifactRef.project_id == project_id,
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "ARTIFACT_REF_KINDS",
    "ArtifactRefConflict",
    "artifact_ref_grants_from_message_context",
    "artifact_ref_projection",
    "content_checksum",
    "create_tool_artifact_ref",
    "get_project_artifact_ref",
    "merge_resident_artifact_ref_grants",
    "resident_artifact_ref_grants",
    "union_resident_artifact_ref_grants",
]
