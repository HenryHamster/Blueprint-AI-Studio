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

import logging
import uuid
from typing import Any, NamedTuple

from sqlalchemy import String, cast, func, or_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import KnowledgeDocument, SourceKind, Task, TaskStatus

logger = logging.getLogger(__name__)


COMPUTE_KNOWLEDGE_ACCEPTANCE_GRADE_ACCEPTANCE = "acceptance"
COMPUTE_KNOWLEDGE_ACCEPTANCE_GRADE_MEASURED_ONLY = "measured_only"
COMPUTE_KNOWLEDGE_ACCEPTANCE_GRADE_KEY = "compute_acceptance_grade"
KNOWLEDGE_ACCEPTANCE_EVIDENCE_KEY = "knowledge_acceptance_evidence"
ORIGIN_STATE_LEGACY_UNKNOWN = "legacy_unknown"
ORIGIN_STATE_RESEARCH_FINDING = "research_finding"
ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED = "not_research_finding_derived"
ORIGIN_CHALLENGE_UNCHALLENGED = "unchallenged"
ORIGIN_CHALLENGE_NOT_APPLICABLE = "not_applicable"
_LEGACY_UNKNOWN_SOURCE_KINDS = {
    SourceKind.DESIGN.value,
    SourceKind.WEB_RESEARCH.value,
    SourceKind.UNKNOWN.value,
}


def _stored_origin_state(doc: KnowledgeDocument) -> str:
    if doc.origin_state:
        return str(doc.origin_state)
    if doc.source_kind in _LEGACY_UNKNOWN_SOURCE_KINDS:
        return ORIGIN_STATE_LEGACY_UNKNOWN
    return ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED


def _stored_challenge_state(doc: KnowledgeDocument, origin_state: str) -> str:
    if origin_state == ORIGIN_STATE_LEGACY_UNKNOWN:
        return ORIGIN_STATE_LEGACY_UNKNOWN
    if origin_state != ORIGIN_STATE_RESEARCH_FINDING:
        return ORIGIN_CHALLENGE_NOT_APPLICABLE
    return str(doc.origin_challenge_state or ORIGIN_CHALLENGE_UNCHALLENGED)


def serialize_origin_provenance(doc: KnowledgeDocument) -> dict[str, Any]:
    """Return compact, bounded origin provenance for list/get/index surfaces."""
    origin_state = _stored_origin_state(doc)
    challenge_state = _stored_challenge_state(doc, origin_state)
    return {
        "origin_state": origin_state,
        "research_task_id": doc.origin_research_task_id,
        "parent_research_task_id": doc.origin_parent_research_task_id,
        "finding_index": doc.origin_finding_index,
        "finding_hash": doc.origin_finding_hash,
        "direction_id": doc.direction_id,
        "challenge_state": challenge_state,
        "finding_challenge": (
            doc.origin_challenge_record
            if isinstance(doc.origin_challenge_record, dict)
            else None
        ),
    }


def classify_knowledge_acceptance(
    *,
    source_kind: str | None,
    forward_quality_verdict: dict[str, Any] | None,
    source_citations: list[Any] | None,
) -> dict[str, Any]:
    """Return the canonical acceptance signal for a KnowledgeDocument.

    Non-compute knowledge documents already passed their forward-quality gate, so
    they remain acceptable for strict knowledge-substance consumers. Compute
    knowledge is stricter: only the cited LLM analysis path, with a grounding
    probe resolving at least one citation and no grounding-miss flag, qualifies
    as acceptance evidence. Deterministic fallback and unresolved-citation
    documents remain durable measured knowledge, but classify as measured-only.
    """
    if source_kind != "compute_experiment":
        return {
            "knowledge_acceptance_evidence": True,
            "compute_acceptance_grade": None,
            "compute_acceptance_reason": "non_compute_forward_quality_gate",
        }

    verdict = (
        forward_quality_verdict if isinstance(forward_quality_verdict, dict) else {}
    )
    citations = source_citations if isinstance(source_citations, list) else []
    reason = None
    if str(verdict.get("analysis_mode") or "").strip().lower() != "llm":
        reason = "analysis_mode_not_llm"
    elif verdict.get("citation_grounding_miss") is True:
        reason = "citation_grounding_miss"
    elif not any(isinstance(c, str) and c.strip() for c in citations):
        reason = "no_source_citations"
    else:
        probes = (
            verdict.get("probes") if isinstance(verdict.get("probes"), dict) else {}
        )
        grounding = (
            probes.get("grounding")
            if isinstance(probes.get("grounding"), dict)
            else None
        )
        if grounding is None:
            reason = "missing_grounding_probe"
        elif int(grounding.get("grounded") or 0) <= 0:
            reason = "no_grounded_artifact_citation"

    if reason is None and isinstance(verdict.get("release_evidence"), dict):
        binding = verdict.get("release_binding") or {}
        binding_status = str(binding.get("status") or "absent")
        if binding_status == "mismatched":
            reason = "release_binding_mismatched"
        elif binding_status != "verified":
            reason = "release_binding_absent"

    accepted = reason is None
    return {
        "knowledge_acceptance_evidence": accepted,
        "compute_acceptance_grade": (
            COMPUTE_KNOWLEDGE_ACCEPTANCE_GRADE_ACCEPTANCE
            if accepted
            else COMPUTE_KNOWLEDGE_ACCEPTANCE_GRADE_MEASURED_ONLY
        ),
        "compute_acceptance_reason": reason or "llm_artifact_cited",
    }


class KnowledgeSubstance(NamedTuple):
    """Lightweight record for knowledge-doc-based need satisfaction.

    ``finding_hash`` is taken from the source Task's ``context["finding_hash"]``;
    ``topic`` is the document's own topic field. Either or both may be None when
    absent from the source or document.
    """

    finding_hash: str | None
    topic: str | None
    source_kind: str | None = None
    source_citations: tuple[str, ...] | None = None
    forward_quality_verdict: dict[str, Any] | None = None


def _is_durable_compute_citation(value: str) -> bool:
    """True when value is a durable storage URI or path-like reference.

    Rejects bare http(s):// URLs — ephemeral signed download URLs and bare result
    URLs are transient and cannot serve as durable grounding evidence. Accepts
    gs://, s3://, az://, file: URIs, path-like references (containing '/'), and
    metric fragment references of the form gs://...#metric:name=value anchored to
    a durable storage URI.
    """
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    if lowered.startswith(("gs://", "s3://", "az://", "file:")):
        return True
    return "/" in value


def _has_durable_compute_citation(citations: tuple[str, ...] | None) -> bool:
    """True when at least one compute citation is durably resolvable."""
    for citation in citations or ():
        if not isinstance(citation, str):
            continue
        text = citation.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered.startswith(("http://", "https://")):
            continue
        if lowered.startswith(("gs://", "s3://", "az://", "file:")) or "/" in text:
            return True
    return False


def knowledge_substance_has_durable_compute_grounding(
    substance: KnowledgeSubstance,
) -> bool:
    """True when a compute_experiment substance carries durable grounded evidence.

    Fails closed: the write-time grounding probe is the ONLY positive proof. When
    the probe is absent — no forward_quality_verdict, no probes dict, no grounding
    sub-probe, or grounded count absent or zero — returns False. Additionally, at
    least one citation must be a durable reference (gs://, s3://, az://, file:, or
    path-like); bare http(s):// ephemeral/signed URLs are rejected even when the
    probe is present and grounded > 0.

    This is a consumer-side predicate over stored knowledge-document signals. It
    does not re-derive artifact durability; it only inspects the persisted
    compute-experiment fields that write-time already stamped.
    """
    if substance.source_kind != SourceKind.COMPUTE_EXPERIMENT.value:
        return False

    citations = [
        c
        for c in (substance.source_citations or ())
        if isinstance(c, str) and c.strip()
    ]
    if not citations or not _has_durable_compute_citation(tuple(citations)):
        return False

    # At least one citation must be a durable reference, not merely non-empty.
    if not any(_is_durable_compute_citation(c) for c in citations):
        return False

    verdict = substance.forward_quality_verdict
    if not isinstance(verdict, dict):
        return False
    if verdict.get("citation_grounding_miss"):
        return False
    probes = verdict.get("probes")
    if not isinstance(probes, dict):
        return False
    grounding = probes.get("grounding")
    if not isinstance(grounding, dict):
        return False
    try:
        return int(grounding.get("grounded") or 0) > 0
    except (TypeError, ValueError):
        return False


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
    rows_result = await session.execute(
        select(
            KnowledgeDocument.topic,
            Task.context,
            KnowledgeDocument.source_kind,
            KnowledgeDocument.source_citations,
            KnowledgeDocument.forward_quality_verdict,
        )
        .join(Task, Task.id == KnowledgeDocument.source_task_id, isouter=True)
        .where(KnowledgeDocument.project_id == project_id)
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(10000)
    )
    rows = rows_result.all()
    if len(rows) >= 10000:
        logger.warning(
            "[KNOWLEDGE] fetch_knowledge_substance_index truncated to 10000 rows "
            "for project_id=%s — substance index may be incomplete",
            project_id,
        )
    result: list[KnowledgeSubstance] = []
    for topic, context, source_kind, source_citations, verdict in rows:
        fh = (context or {}).get("finding_hash") if context is not None else None
        result.append(
            KnowledgeSubstance(
                finding_hash=str(fh).strip() if fh else None,
                topic=str(topic).strip() if topic else None,
                source_kind=str(source_kind).strip() if source_kind else None,
                source_citations=tuple(
                    c.strip()
                    for c in (source_citations or [])
                    if isinstance(c, str) and c.strip()
                ),
                forward_quality_verdict=verdict if isinstance(verdict, dict) else None,
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
    acceptance = classify_knowledge_acceptance(
        source_kind=doc.source_kind,
        forward_quality_verdict=doc.forward_quality_verdict,
        source_citations=doc.source_citations,
    )
    verdict = (
        doc.forward_quality_verdict
        if isinstance(doc.forward_quality_verdict, dict)
        else {}
    )
    analysis_fallback_reason = (
        verdict.get("fallback_reason")
        if doc.source_kind == SourceKind.COMPUTE_EXPERIMENT.value
        else None
    )
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
        "analysis_fallback_reason": analysis_fallback_reason,
        "knowledge_acceptance_evidence": acceptance["knowledge_acceptance_evidence"],
        "compute_acceptance_grade": acceptance["compute_acceptance_grade"],
        "compute_acceptance_reason": acceptance["compute_acceptance_reason"],
        "release_evidence": verdict.get("release_evidence"),
        "unavailable_cause": verdict.get("unavailable_cause"),
        "criterion_met": (
            verdict.get("criterion_met")
            if doc.source_kind == SourceKind.COMPUTE_EXPERIMENT.value
            else None
        ),
        "authoring_branch": doc.authoring_branch,
        "head_sha": doc.head_sha,
        "sweep_id": doc.sweep_id,
        "arm_label": doc.arm_label,
        "origin_provenance": serialize_origin_provenance(doc),
        "lifecycle_status": doc.lifecycle_status,
        "consumed_at": doc.consumed_at.isoformat() if doc.consumed_at else None,
        "dismissed_at": doc.dismissed_at.isoformat() if doc.dismissed_at else None,
        "dismissed_reason": doc.dismissed_reason,
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
    origin_state: str | None = None,
    challenge_state: str | None = None,
    finding_hash: str | None = None,
    sweep_id: str | None = None,
    lifecycle_status: str | None = None,
):
    """Build the project-scoped ``select`` for knowledge documents.

    Applies the exact ``topic`` / ``tag`` / ``direction`` / ``source_kind``
    filters and the ``q`` ILIKE search over title/summary/body/topic plus a
    cast-to-text scan of the tag list. Ordering / pagination are applied by the
    caller (or by ``list_knowledge_documents``).
    """
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.project_id == project_id)
    if topic:
        stmt = stmt.where(KnowledgeDocument.topic == topic)
    if source_kind:
        stmt = stmt.where(KnowledgeDocument.source_kind == source_kind)
    if direction:
        stmt = stmt.where(KnowledgeDocument.direction_id == direction)
    if origin_state:
        if origin_state == ORIGIN_STATE_LEGACY_UNKNOWN:
            stmt = stmt.where(
                KnowledgeDocument.origin_state.is_(None),
                KnowledgeDocument.source_kind.in_(_LEGACY_UNKNOWN_SOURCE_KINDS),
            )
        elif origin_state == ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED:
            stmt = stmt.where(
                or_(
                    KnowledgeDocument.origin_state
                    == ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED,
                    KnowledgeDocument.source_kind.not_in(_LEGACY_UNKNOWN_SOURCE_KINDS),
                )
            )
        else:
            stmt = stmt.where(KnowledgeDocument.origin_state == origin_state)
    if challenge_state:
        if challenge_state == ORIGIN_STATE_LEGACY_UNKNOWN:
            stmt = stmt.where(
                KnowledgeDocument.origin_state.is_(None),
                KnowledgeDocument.source_kind.in_(_LEGACY_UNKNOWN_SOURCE_KINDS),
            )
        elif challenge_state == ORIGIN_CHALLENGE_NOT_APPLICABLE:
            stmt = stmt.where(
                or_(
                    KnowledgeDocument.origin_state
                    == ORIGIN_STATE_NOT_RESEARCH_FINDING_DERIVED,
                    KnowledgeDocument.source_kind.not_in(_LEGACY_UNKNOWN_SOURCE_KINDS),
                )
            )
        else:
            stmt = stmt.where(
                KnowledgeDocument.origin_challenge_state == challenge_state
            )
    if finding_hash:
        stmt = stmt.where(KnowledgeDocument.origin_finding_hash == finding_hash)
    if sweep_id:
        stmt = stmt.where(KnowledgeDocument.sweep_id == sweep_id)
    if lifecycle_status:
        stmt = stmt.where(KnowledgeDocument.lifecycle_status == lifecycle_status)
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
    origin_state: str | None = None,
    challenge_state: str | None = None,
    finding_hash: str | None = None,
    sweep_id: str | None = None,
    lifecycle_status: str | None = None,
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
        origin_state=origin_state,
        challenge_state=challenge_state,
        finding_hash=finding_hash,
        sweep_id=sweep_id,
        lifecycle_status=lifecycle_status,
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

def derive_compute_arm_label_from_payload(payload: dict[str, Any] | None) -> str | None:
    """Return the canonical planned arm label for a compute_experiment payload."""
    if not isinstance(payload, dict):
        return None
    raw_arm = payload.get("experiment") or payload.get("arm_label") or None
    return (str(raw_arm).strip()[:256] if raw_arm else None) or None


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
    acceptance = classify_knowledge_acceptance(
        source_kind=doc.source_kind,
        forward_quality_verdict=doc.forward_quality_verdict,
        source_citations=doc.source_citations,
    )
    verdict = (
        doc.forward_quality_verdict
        if isinstance(doc.forward_quality_verdict, dict)
        else {}
    )
    is_compute = doc.source_kind == SourceKind.COMPUTE_EXPERIMENT.value
    # criterion_met: tri-state (True/False/None) read from the persisted verdict.
    # True = criterion was met, False = not met, None = unmeasured/not evaluable.
    # Distinct from compute_acceptance_grade (a trustworthiness signal).
    criterion_met: bool | None = verdict.get("criterion_met") if is_compute else None
    return {
        "id": str(doc.id),
        "title": doc.title,
        "summary": collapsed,
        "topic": doc.topic,
        "direction_id": doc.direction_id,
        "source_kind": doc.source_kind,
        "sweep_id": doc.sweep_id,
        "arm_label": doc.arm_label,
        "analysis_fallback_reason": (
            verdict.get("fallback_reason") if is_compute else None
        ),
        "knowledge_acceptance_evidence": acceptance["knowledge_acceptance_evidence"],
        "compute_acceptance_grade": acceptance["compute_acceptance_grade"],
        "compute_acceptance_reason": acceptance["compute_acceptance_reason"],
        "unavailable_cause": (
            verdict.get("unavailable_cause") if is_compute else None
        ),
        "criterion_met": criterion_met,
        "criterion": verdict.get("criterion") if is_compute else None,
        "per_arm_verdicts": verdict.get("per_arm_verdicts") if is_compute else None,
        "origin_provenance": serialize_origin_provenance(doc),
        "lifecycle_status": doc.lifecycle_status,
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

    Documents are ranked by source-kind tier (durable kinds first, stubs last),
    then by creation date descending, so design/web_research/compute_experiment
    docs outrank newer research_audit stubs.
    """
    seen: set[str] = set()
    entries: list[dict] = []

    async def _collect(direction: str | None) -> None:
        if len(entries) >= limit:
            return
        stmt = build_knowledge_documents_stmt(project_id, direction=direction)
        # Tier 0: durable kinds (design, web_research, compute_experiment).
        # Tier 1: stub/metadata kinds (research_audit, repo_domain, direction_retirement).
        # Tier 2: unknown/legacy.
        durable = {
            SourceKind.DESIGN.value,
            SourceKind.WEB_RESEARCH.value,
            SourceKind.COMPUTE_EXPERIMENT.value,
        }
        stub = {
            SourceKind.RESEARCH_AUDIT.value,
            SourceKind.REPO_DOMAIN.value,
            SourceKind.DIRECTION_RETIREMENT.value,
        }
        stmt = (
            stmt.order_by(
                case(
                    (KnowledgeDocument.source_kind.in_(durable), 0),
                    (KnowledgeDocument.source_kind.in_(stub), 1),
                    else_=2,
                ),
                KnowledgeDocument.created_at.desc(),
            )
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


# Maximum pending follow-ups shipped to the strategist payload. Like the knowledge
# index, this is bounded so it cannot dominate the prompt budget as the store grows.
_PENDING_LIFECYCLE_ITEMS_KEEP = 20
# Per-entry summary character cap for the pending-followups section.
_PENDING_LIFECYCLE_ITEMS_SUMMARY_CHARS = 200


async def build_pending_followups(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int = _PENDING_LIFECYCLE_ITEMS_KEEP,
    summary_chars: int = _PENDING_LIFECYCLE_ITEMS_SUMMARY_CHARS,
) -> list[dict]:
    """Return compact entries for knowledge docs that need strategist follow-up.

    Only design and web_research docs participate in this lifecycle. Returns
    pending (not yet consumed/dismissed) entries, newest-first. Each entry
    carries id, title, summary (truncated), topic, source_kind, direction_id,
    created_at, and tags — enough for the strategist to decide whether to
    consume, dismiss, or spawn a direction.
    """
    stmt = (
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.project_id == project_id,
            KnowledgeDocument.source_kind.in_(
                [SourceKind.DESIGN.value, SourceKind.WEB_RESEARCH.value]
            ),
            KnowledgeDocument.lifecycle_status == "pending",
        )
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    entries: list[dict] = []
    for doc in rows:
        summary = doc.summary or ""
        collapsed = " ".join(summary.split())
        if len(collapsed) > summary_chars:
            collapsed = collapsed[: summary_chars - 1].rstrip() + "…"
        entries.append({
            "id": str(doc.id),
            "title": doc.title,
            "summary": collapsed,
            "topic": doc.topic,
            "source_kind": doc.source_kind,
            "direction_id": doc.direction_id,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "tags": doc.tags if isinstance(doc.tags, list) else [],
        })
    return entries


_DIRECTION_KNOWLEDGE_CAP = 5


async def fetch_direction_knowledge_index(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_ids: list[str],
    *,
    summary_chars: int = _KNOWLEDGE_INDEX_SUMMARY_CHARS,
) -> dict[str, list[dict]]:
    """Bulk-fetch compact knowledge entries grouped by direction_id.

    Returns ``{direction_id: [entry, ...]}`` (newest-first, capped at
    ``_DIRECTION_KNOWLEDGE_CAP`` per direction) for every direction that has at
    least one document.  Directions with no documents are absent from the result.
    Uses a single query to avoid N per-direction round-trips.
    """
    if not direction_ids:
        return {}
    stmt = (
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.project_id == project_id,
            KnowledgeDocument.direction_id.in_(direction_ids),
        )
        .order_by(KnowledgeDocument.direction_id, KnowledgeDocument.created_at.desc())
        .limit(5000)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if len(rows) >= 5000:
        logger.warning(
            "[KNOWLEDGE] fetch_direction_knowledge_index query truncated "
            "to 5000 rows for project_id=%s — some directions may have "
            "incomplete index entries",
            project_id,
        )
    by_dir: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for doc in rows:
        did = str(doc.direction_id)
        n = counts.get(did, 0)
        if n >= _DIRECTION_KNOWLEDGE_CAP:
            continue
        by_dir.setdefault(did, []).append(_knowledge_index_entry(doc, summary_chars))
        counts[did] = n + 1
    return by_dir


async def fetch_retirement_memo(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
    *,
    summary_chars: int = _KNOWLEDGE_INDEX_SUMMARY_CHARS,
) -> dict[str, str] | None:
    """Return the latest retirement memo knowledge row for a direction.

    Retirement memos are best-effort: dedup windows and sparse retire context
    mean a matching row may not exist, so callers should treat ``None`` as a
    normal absence rather than a grounding failure.
    """
    if not direction_id:
        return None
    stmt = (
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.project_id == project_id,
            KnowledgeDocument.direction_id == direction_id,
            KnowledgeDocument.source_kind == SourceKind.DIRECTION_RETIREMENT.value,
        )
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(1)
    )
    doc = (await session.execute(stmt)).scalars().first()
    if doc is None:
        return None
    return {
        "id": str(doc.id),
        "summary": (doc.summary or "")[:summary_chars],
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


async def build_sweep_knowledge_index(
    session: AsyncSession,
    project_id: uuid.UUID,
    sweep_id: str,
    *,
    summary_chars: int = _KNOWLEDGE_INDEX_SUMMARY_CHARS,
) -> list[dict]:
    """Return all compute_experiment knowledge docs for a sweep, oldest-first.

    Covers both per-arm docs (arm_label set) and the sweep-aggregate doc
    (arm_label null), ordered by created_at ascending so arms precede aggregate.
    """
    stmt = (
        build_knowledge_documents_stmt(
            project_id,
            source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
            sweep_id=sweep_id,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_knowledge_index_entry(doc, summary_chars) for doc in rows]


_SWEEP_GROUPS_LIMIT = 10
_SWEEP_MIN_DOCS = 2

_COMPUTE_SWEEP_TERMINAL_STATUSES = frozenset(
    (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    )
)


def _compute_sweep_task_is_terminal(task: Task) -> bool:
    try:
        status = TaskStatus(task.status)
    except (TypeError, ValueError):
        return False
    return status in _COMPUTE_SWEEP_TERMINAL_STATUSES


def _compute_sweep_doc_is_definitive(doc: KnowledgeDocument) -> bool:
    verdict = (
        doc.forward_quality_verdict
        if isinstance(doc.forward_quality_verdict, dict)
        else {}
    )
    if verdict.get("unavailable_cause"):
        return False
    if isinstance(verdict.get("criterion_met"), bool):
        return True
    return verdict.get("outcome") is not None


async def build_compute_sweep_groups(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    direction_id: str | None = None,
    limit: int = _SWEEP_GROUPS_LIMIT,
    summary_chars: int = _KNOWLEDGE_INDEX_SUMMARY_CHARS,
) -> dict[str, list[dict]]:
    """Group complete compute_experiment sweeps by their planned task roster.

    Returns ``{sweep_id: [entry, ...]}`` for sweeps that pass the completeness
    gate. Bounded to ``limit`` distinct sweeps, newest-first. Sweeps with no
    sweep_id are excluded. Two paths populate the result:

    1. Multi-doc path: sweeps with ``>= _SWEEP_MIN_DOCS`` KnowledgeDocuments.
    2. Single-doc arms-block path: sweeps with exactly one KnowledgeDocument
       whose ``per_arm_verdicts`` carries ``>= _SWEEP_MIN_DOCS`` arms, each
       with a boolean ``met`` value. Used for single-task arms-block experiments
       where all arm results are encoded in a single document.

    Completeness gate (both paths): when compute_experiment Task rows for this
    project carry a matching sweep_id in their first payload, every such task
    must have a usable KnowledgeDocument (source_task_id match, no
    unavailable_cause) before the sweep is returned. Sweeps that fail this
    check are excluded so the strategist cannot synthesise from partial
    evidence. When no Task manifest is discoverable for a sweep, the
    docs-only behavior is preserved unchanged.
    """
    docs_stmt = (
        build_knowledge_documents_stmt(
            project_id,
            source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        )
        .where(KnowledgeDocument.sweep_id.is_not(None))
        .order_by(KnowledgeDocument.sweep_id, KnowledgeDocument.created_at.desc())
        .limit(5000)
    )
    all_doc_rows = (await session.execute(docs_stmt)).scalars().all()
    if len(all_doc_rows) >= 5000:
        logger.warning(
            "[KNOWLEDGE] build_compute_sweep_groups doc query truncated "
            "to 5000 rows for project_id=%s — sweep completeness gate may "
            "exclude some sweeps",
            project_id,
        )

    all_sweep_ids: set[str] = set()
    for doc in all_doc_rows:
        sid = doc.sweep_id
        if sid is None:
            continue
        all_sweep_ids.add(sid)

    if not all_sweep_ids:
        return {}

    task_stmt = select(Task).where(
        Task.project_id == project_id,
        Task.payloads[0]["type"].as_string() == SourceKind.COMPUTE_EXPERIMENT.value,
        Task.payloads[0]["sweep_id"].as_string().in_(list(all_sweep_ids)),
    )
    if direction_id:
        task_stmt = task_stmt.where(
            Task.context["direction_id"].as_string() == direction_id
        )
    task_stmt = task_stmt.order_by(Task.created_at.desc()).limit(5000)
    tasks = (await session.execute(task_stmt)).scalars().all()
    if len(tasks) >= 5000:
        logger.warning(
            "[KNOWLEDGE] build_compute_sweep_groups task query truncated "
            "to 5000 rows for project_id=%s — sweep completeness gate may "
            "exclude tasks from some sweeps",
            project_id,
        )

    planned: dict[str, dict[str | None, list[Task]]] = {}
    task_to_arm: dict[uuid.UUID, str | None] = {}
    for task in tasks:
        payload = task.first_payload or {}
        raw_sweep_id = payload.get("sweep_id") or None
        sweep_id = (str(raw_sweep_id).strip()[:128] if raw_sweep_id else None) or None
        if sweep_id is None:
            continue
        arm_label = derive_compute_arm_label_from_payload(payload)
        planned.setdefault(sweep_id, {}).setdefault(arm_label, []).append(task)
        task_to_arm[task.id] = arm_label

    if direction_id:
        scoped_task_ids = set(task_to_arm)
        doc_rows = [
            doc
            for doc in all_doc_rows
            if doc.direction_id == direction_id or doc.source_task_id in scoped_task_ids
        ]
    else:
        doc_rows = all_doc_rows

    by_sweep_docs: dict[str, list[KnowledgeDocument]] = {}
    for doc in doc_rows:
        sid = doc.sweep_id
        if sid is None:
            continue
        by_sweep_docs.setdefault(sid, []).append(doc)

    if not by_sweep_docs:
        return {}

    multi_docs = {
        sid: docs
        for sid, docs in by_sweep_docs.items()
        if len(docs) >= _SWEEP_MIN_DOCS
    }
    single_doc_candidates: dict[str, KnowledgeDocument] = {
        sid: docs[0]
        for sid, docs in by_sweep_docs.items()
        if len(docs) == 1
    }

    docs_by_sweep_arm: dict[str, dict[str, KnowledgeDocument]] = {}
    for doc in doc_rows:
        if doc.source_task_id not in task_to_arm:
            continue
        arm_label = task_to_arm[doc.source_task_id]
        if arm_label is None or doc.arm_label != arm_label:
            continue
        sweep_id = doc.sweep_id
        if sweep_id is None:
            continue
        docs_by_sweep_arm.setdefault(sweep_id, {}).setdefault(arm_label, doc)

    complete: dict[str, list[dict]] = {}
    for sweep_id, docs in multi_docs.items():
        arms = planned.get(sweep_id)
        if not arms:
            complete[sweep_id] = [
                _knowledge_index_entry(doc, summary_chars) for doc in docs
            ]
            continue

        labels = [label for label in arms if label is not None]
        if len(labels) != len(arms) or len(set(labels)) < _SWEEP_MIN_DOCS:
            continue
        if any(
            not _compute_sweep_task_is_terminal(task)
            for arm_tasks in arms.values()
            for task in arm_tasks
        ):
            continue

        selected_docs: list[KnowledgeDocument] = []
        sweep_docs = docs_by_sweep_arm.get(sweep_id, {})
        for label in sorted(set(labels)):
            doc = sweep_docs.get(label)
            if doc is None or not _compute_sweep_doc_is_definitive(doc):
                selected_docs = []
                break
            selected_docs.append(doc)
        if not selected_docs:
            continue
        selected_docs.sort(
            key=lambda d: d.created_at.isoformat() if d.created_at else "",
            reverse=True,
        )
        complete[sweep_id] = [
            _knowledge_index_entry(doc, summary_chars) for doc in selected_docs
        ]

    for sweep_id, doc in single_doc_candidates.items():
        if sweep_id in complete:
            continue
        arms = planned.get(sweep_id)
        if arms and any(
            not _compute_sweep_task_is_terminal(task)
            for arm_tasks in arms.values()
            for task in arm_tasks
        ):
            continue
        if arms and not all(
            any(
                doc.source_task_id == task.id and _compute_sweep_doc_is_definitive(doc)
                for doc in by_sweep_docs.get(sweep_id, [])
            )
            for arm_tasks in arms.values()
            for task in arm_tasks
        ):
            continue
        fqv = (
            doc.forward_quality_verdict
            if isinstance(doc.forward_quality_verdict, dict)
            else {}
        )
        verdicts = fqv.get("per_arm_verdicts")
        if not isinstance(verdicts, dict):
            continue
        arm_items = list(verdicts.items())
        if len(arm_items) < _SWEEP_MIN_DOCS:
            continue
        if not all(
            isinstance(arm, dict) and arm.get("met") in (True, False)
            for _, arm in arm_items
        ):
            continue
        base_entry = _knowledge_index_entry(doc, summary_chars)
        complete[sweep_id] = [
            {**base_entry, "arm_label": str(arm_name), "criterion_met": arm.get("met")}
            for arm_name, arm in arm_items
        ]

    if not complete:
        return {}

    # Annotate the first entry of each sweep with the disposition task (act-on-verdict
    # sweep follow-on) when one exists, so the strategist can skip re-synthesizing sweeps
    # that already have a follow-on in progress or completed.
    _sweep_ids = list(complete.keys())
    _disp_stmt = (
        select(Task).where(
            Task.project_id == project_id,
            Task.context["act_on_verdict_sweep_id"].as_string().in_(_sweep_ids),
        )
        .order_by(Task.created_at.desc())
        .limit(10000)
    )
    _disp_rows = (await session.execute(_disp_stmt)).scalars().all()
    if len(_disp_rows) >= 10000:
        logger.warning(
            "[KNOWLEDGE] build_compute_sweep_groups disposition query truncated "
            "to 10000 rows for project_id=%s — some sweeps may lack "
            "disposition annotations",
            project_id,
        )
    _disposition: dict[str, dict] = {}
    for _t in _disp_rows:
        _sid = (_t.context or {}).get("act_on_verdict_sweep_id")
        if not _sid or _sid not in complete:
            continue
        _status = _t.status.value if hasattr(_t.status, "value") else str(_t.status)
        _status_norm = _status.strip().lower()
        if _status_norm not in {
            TaskStatus.QUEUED.value.lower(),
            TaskStatus.PROVISIONING.value.lower(),
            TaskStatus.IN_PROGRESS.value.lower(),
            TaskStatus.EVALUATING.value.lower(),
            TaskStatus.COMPLETED.value.lower(),
        }:
            continue
        _existing = _disposition.get(_sid)
        # Prefer any non-terminal disposition over a completed one when multiple tasks exist.
        if _existing is None or _existing.get("status") == TaskStatus.COMPLETED.value:
            _disposition[_sid] = {"task_id": str(_t.id), "status": _status}
    for _sid, _disp in _disposition.items():
        if complete[_sid]:
            complete[_sid][0]["sweep_disposition"] = _disp
    # Sort sweeps by the created_at of their newest entry (descending).
    ordered = sorted(
        complete.items(),
        key=lambda kv: (kv[1][0].get("created_at") or ""),
        reverse=True,
    )
    return dict(ordered[:limit])
