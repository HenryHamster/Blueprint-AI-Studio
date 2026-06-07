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
from typing import NamedTuple

from sqlalchemy import String, cast, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import KnowledgeDocument


class KnowledgeSubstance(NamedTuple):
    """Lightweight record for knowledge-doc-based need satisfaction.

    ``finding_hash`` is taken from the source Task's ``context["finding_hash"]``;
    ``topic`` is the document's own topic field. Either or both may be None when
    absent from the source or document.
    """

    finding_hash: str | None
    topic: str | None


async def fetch_knowledge_substance_index(
    session,
    project_id: uuid.UUID,
) -> list[KnowledgeSubstance]:
    """Build a lightweight substance index for knowledge-doc-based need resolution.

    For each KnowledgeDocument in the project, returns a (finding_hash, topic)
    record where finding_hash comes from the source Task's context["finding_hash"].
    The mere presence of a KnowledgeDocument row is the 'durable/shipped' signal —
    no extra gate needed (the forward-quality gate fires at write time).

    Used by blocked-need resolution to satisfy a dangling kind='task' prerequisite
    whose originating task is CANCELLED but whose substance persists as a durable
    KnowledgeDocument, preventing escalation-parking forever.
    """
    from app.models import Task

    rows = (
        await session.execute(
            select(KnowledgeDocument.topic, Task.context)
            .join(Task, Task.id == KnowledgeDocument.source_task_id, isouter=True)
            .where(KnowledgeDocument.project_id == project_id)
        )
    ).all()
    result: list[KnowledgeSubstance] = []
    for topic, context in rows:
        fh = (context or {}).get("finding_hash") if context is not None else None
        result.append(
            KnowledgeSubstance(
                finding_hash=str(fh).strip() if fh else None,
                topic=str(topic).strip() if topic else None,
            )
        )
    return result


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


# Maximum number of entries carried in the compact knowledge index shipped to
# the strategist / research agent. The index is navigation-level: enough to know
# what knowledge already exists (and fetch the full body on demand), never the
# bodies themselves. Bounded so the field cannot dominate the prompt budget as
# the store grows.
_KNOWLEDGE_INDEX_KEEP = 20
# Maximum length of the per-entry one-line summary carried in the index. Keeps
# each entry recognition-level; the full summary + body are fetched on demand via
# the agent knowledge-document endpoint.
_KNOWLEDGE_INDEX_SUMMARY_CHARS = 200


def _knowledge_index_entry(doc: KnowledgeDocument, summary_chars: int) -> dict:
    """Build one compact, body-omitted index entry for a knowledge document.

    Carries only navigation-level metadata: id, title, char-bounded summary,
    topic, direction_id, source_kind, created_at. NEVER the markdown body — the
    full document is fetched on demand via the agent knowledge-document endpoint.
    """
    summary = doc.summary or ""
    collapsed = " ".join(summary.split())
    if len(collapsed) > summary_chars:
        collapsed = collapsed[: summary_chars - 1].rstrip() + "…"
    return {
        "id": str(doc.id),
        "title": doc.title,
        "summary": collapsed,
        "topic": doc.topic,
        "direction_id": doc.direction_id,
        "source_kind": doc.source_kind,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


async def build_knowledge_index(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    direction_id: str | None = None,
    limit: int = _KNOWLEDGE_INDEX_KEEP,
    summary_chars: int = _KNOWLEDGE_INDEX_SUMMARY_CHARS,
) -> list[dict]:
    """Build a compact, bounded, newest-first index of a project's knowledge docs.

    Returns a list of lightweight, body-omitted entries (id, title, truncated
    summary, topic, direction_id, source_kind, created_at) so the strategist /
    research agent knows what knowledge already exists and can fetch full bodies
    on demand. Scoped to ``project_id``; cross-project documents are never
    included.

    When ``direction_id`` is given, that direction's documents are listed first
    (newest-first), then project-wide documents fill the remainder up to
    ``limit`` — so the decision-maker sees the most relevant knowledge first
    without losing visibility of the broader store. The total never exceeds
    ``limit`` and per-entry summaries are truncated to ``summary_chars``.
    """
    seen: set[str] = set()
    entries: list[dict] = []

    async def _collect(direction: str | None) -> None:
        if len(entries) >= limit:
            return
        stmt = (
            build_knowledge_documents_stmt(project_id, direction=direction)
            .order_by(KnowledgeDocument.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        for doc in rows:
            doc_id = str(doc.id)
            if doc_id in seen:
                continue
            seen.add(doc_id)
            entries.append(_knowledge_index_entry(doc, summary_chars))
            if len(entries) >= limit:
                break

    if direction_id:
        await _collect(direction_id)
    # Project-wide fill (also the sole pass when no direction is scoped).
    await _collect(None)
    return entries


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
