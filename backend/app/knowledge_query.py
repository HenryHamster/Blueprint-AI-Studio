"""Shared query + serialization for the knowledge-document store.

Single source of truth for listing/searching/reading ``KnowledgeDocument``
rows so the operator-facing admin endpoints (``routers/admin_debug.py``) and
the agent-facing endpoints (``routers/agent.py``) behave identically. The only
difference between the two surfaces is auth + how the project scope is derived
(URL path vs. authenticated task); the search semantics, serialized field set,
pagination, and ordering live here so they cannot drift.

Search matches ``q`` against title/summary/body/topic via ILIKE
(trigram-GIN-backed on Postgres for title/body) plus a cast-to-text scan over
the JSON tag list. ``topic`` / ``tag`` apply exact filters. The list shape
omits the (potentially large) markdown body; only get-by-id returns it.
"""

import uuid

from sqlalchemy import String, cast, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import KnowledgeDocument


def serialize_knowledge_document(doc: KnowledgeDocument, *, include_body: bool) -> dict:
    """Serialize a KnowledgeDocument into a stable JSON-safe dict.

    Field set is the documented contract shared by the admin and agent
    surfaces: id, title, summary, topic, tags, source_citations,
    source_task_id, created_at, updated_at, forward_quality_verdict (plus
    body only when ``include_body``).
    """
    out = {
        "id": str(doc.id),
        "title": doc.title,
        "summary": doc.summary,
        "source_kind": doc.source_kind,
        "topic": doc.topic,
        "direction_id": doc.direction_id,
        "tags": doc.tags or [],
        "source_citations": doc.source_citations or [],
        "source_task_id": str(doc.source_task_id) if doc.source_task_id else None,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "forward_quality_verdict": doc.forward_quality_verdict,
        "authoring_branch": doc.authoring_branch,
        "head_sha": doc.head_sha,
    }
    if include_body:
        out["body"] = doc.body
    return out


def build_knowledge_documents_stmt(
    project_id: uuid.UUID,
    *,
    q: str | None = None,
    topic: str | None = None,
    tag: str | None = None,
    direction: str | None = None,
    source_kind: str | None = None,
):
    """Build the project-scoped ``select`` for knowledge documents.

    Applies the exact ``topic`` / ``tag`` / ``direction`` / ``source_kind``
    filters and the ``q`` ILIKE search over title/summary/body/topic plus a
    cast-to-text scan of the tag list. Ordering / pagination are applied by the
    caller (or by ``list_knowledge_documents``).
    """
    stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.project_id == project_id
    )
    if topic:
        stmt = stmt.where(KnowledgeDocument.topic == topic)
    if source_kind:
        stmt = stmt.where(KnowledgeDocument.source_kind == source_kind)
    if direction:
        stmt = stmt.where(KnowledgeDocument.direction_id == direction)
    if tag:
        # tags is a JSON list; match the exact element via a cast-to-text scan
        # so it works on both Postgres (JSONB) and the SQLite test backend.
        stmt = stmt.where(cast(KnowledgeDocument.tags, String).ilike(f'%"{tag}"%'))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                KnowledgeDocument.title.ilike(like),
                KnowledgeDocument.summary.ilike(like),
                KnowledgeDocument.body.ilike(like),
                KnowledgeDocument.topic.ilike(like),
                cast(KnowledgeDocument.tags, String).ilike(like),
            )
        )
    return stmt


async def list_knowledge_documents(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    q: str | None = None,
    topic: str | None = None,
    tag: str | None = None,
    direction: str | None = None,
    source_kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List / search a project's knowledge documents, newest-first, paginated.

    Returns ``{documents, total, limit, offset}`` where each document is the
    lean (body-omitted) serialization.
    """
    stmt = build_knowledge_documents_stmt(
        project_id,
        q=q,
        topic=topic,
        tag=tag,
        direction=direction,
        source_kind=source_kind,
    )

    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    rows = (
        (
            await session.execute(
                stmt.order_by(KnowledgeDocument.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "documents": [
            serialize_knowledge_document(d, include_body=False) for d in rows
        ],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


async def get_knowledge_document(
    session: AsyncSession,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
) -> KnowledgeDocument | None:
    """Fetch a single document scoped to ``project_id`` (None if missing/cross-project)."""
    return (
        await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
