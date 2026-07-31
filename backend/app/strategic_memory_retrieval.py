"""Deterministic, project-fenced lexical retrieval for Strategic Memory V2.

This is deliberately the non-vector retrieval baseline.  It is useful on its
own, and remains the safe degraded path if an eventual embedding service is
unavailable.  Every candidate is scoped and privacy-filtered before any score
is calculated; scores are selection signals, never truth or confidence.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from sqlmodel import select

from app.strategic_memory_corrections import derive_barrier_state
from app.strategic_memory_sources import (
    SourceResolutionError,
    parse_exact_spans,
    resolve_source_receipt,
    verify_exact_span,
)


MemoryPolicy = Literal["strict_history", "balanced", "fresh_start"]
MAX_CANDIDATES = 80
MAX_EVIDENCE_CARDS = 8
MAX_RAW_SUFFIX = 12
_RRF_K = 60
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_HISTORY = re.compile(r"\b(?:as\s+of|at\s+the\s+time|histor(?:y|ical)|previous(?:ly)?|before\s+we|old\s+(?:plan|approach|version))\b", re.I)
_FRESH = re.compile(r"\b(?:ignore\s+(?:our\s+)?prior|fresh\s+(?:start|perspective)|start\s+over)\b", re.I)
_CORRECTION = re.compile(r"\b(?:actually|correction|wrong|contradict|conflict|supersed|retract|instead)\b", re.I)
_EXACT = re.compile(r"\b(?:exact(?:ly)?|quote|verbatim|formula|notation|symbol)\b", re.I)


@dataclass(frozen=True)
class QueryAnalysis:
    query: str
    policy: MemoryPolicy
    historical: bool
    correction_or_conflict: bool
    exact_required: bool
    lexical_terms: tuple[str, ...]
    exact_terms: tuple[str, ...]
    formula_fingerprints: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalAccessPolicy:
    """Backend-owned disclosure policy for a retrieval invocation.

    Source provenance is the privacy authority.  Callers must opt in to raw
    evidence explicitly; arbitrary API inputs must never widen this policy.
    """

    allowed_visibilities: frozenset[str] = frozenset({"project"})
    allowed_sensitivities: frozenset[str] = frozenset({"normal"})
    include_raw_content: bool = False

    def permits(self, source: Any) -> bool:
        return (
            str(getattr(source, "visibility", "")) in self.allowed_visibilities
            and str(getattr(source, "sensitivity", "")) in self.allowed_sensitivities
        )


DEFAULT_RETRIEVAL_ACCESS_POLICY = RetrievalAccessPolicy()


@dataclass(frozen=True)
class EvidenceCard:
    item_id: uuid.UUID
    version: int
    canonical_text: str
    status: str
    epistemic_status: str
    confidence: float
    recorded_source_sequence: int
    asserted_effective_from: datetime | None
    asserted_effective_until: datetime | None
    source_id: uuid.UUID | None
    source_type: str | None
    source_key: str | None
    source_revision: str | None
    raw_quote: str | None
    raw_quote_available: bool
    relation_path: tuple[str, ...]
    selection_reasons: tuple[str, ...]
    score: float

    def as_envelope_dict(self) -> dict[str, Any]:
        """The bounded, provenance-first representation persisted in a core."""

        return {
            "item_id": str(self.item_id),
            "version": self.version,
            "canonical_text": self.canonical_text,
            "status": self.status,
            "epistemic_status": self.epistemic_status,
            "confidence": self.confidence,
            "recorded_source_sequence": self.recorded_source_sequence,
            "asserted_effective_from": self.asserted_effective_from,
            "asserted_effective_until": self.asserted_effective_until,
            "source": (
                None
                if self.source_id is None
                else {
                    "id": str(self.source_id),
                    "type": self.source_type,
                    "key": self.source_key,
                    "revision": self.source_revision,
                }
            ),
            "raw_quote": self.raw_quote,
            "raw_quote_available": self.raw_quote_available,
            "relation_path": list(self.relation_path),
            "selection_reasons": list(self.selection_reasons),
        }


@dataclass(frozen=True)
class RetrievalResult:
    analysis: QueryAnalysis
    cards: tuple[EvidenceCard, ...]
    answerable: bool
    abstain_reason: str | None
    conflict_present: bool
    raw_suffix: tuple[dict[str, Any], ...]
    memory_degraded: bool
    trace: Mapping[str, Any]

    def envelope_payload(self) -> dict[str, Any]:
        return {
            "evidence_cards": [card.as_envelope_dict() for card in self.cards],
            "answerability": {
                "answerable": self.answerable,
                "abstain_reason": self.abstain_reason,
                "conflict_present": self.conflict_present,
            },
            "raw_suffix": list(self.raw_suffix),
            "memory_degraded": self.memory_degraded,
            "retrieval_trace": dict(self.trace),
        }


@dataclass
class _Candidate:
    item: Any
    version: Any
    status: str
    provenance: list[tuple[Any, Any]] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    channels: dict[str, float] = field(default_factory=dict)

    @property
    def key(self) -> tuple[uuid.UUID, int]:
        return self.item.id, int(self.version.version)


def analyze_memory_query(query: str) -> QueryAnalysis:
    """Parse only bounded deterministic signals; no provider call is involved."""

    if not isinstance(query, str):
        raise TypeError("memory query must be text")
    normalized = query.strip()
    if len(normalized.encode("utf-8")) > 16_384:
        raise ValueError("memory query exceeds the bounded retrieval limit")
    spans = parse_exact_spans(normalized)
    exact = {span.raw_text.casefold() for span in spans if span.raw_text.strip()}
    fingerprints = {
        form for span in spans for form in span.normalized_forms if form.startswith("formula-v1:")
    }
    lexical = tuple(sorted({word.casefold() for word in _WORD.findall(normalized) if len(word) > 2}))
    policy: MemoryPolicy = "fresh_start" if _FRESH.search(normalized) else ("strict_history" if _HISTORY.search(normalized) else "balanced")
    return QueryAnalysis(
        query=normalized,
        policy=policy,
        historical=bool(_HISTORY.search(normalized)),
        correction_or_conflict=bool(_CORRECTION.search(normalized)),
        exact_required=bool(_EXACT.search(normalized)) or bool(fingerprints),
        lexical_terms=lexical,
        exact_terms=tuple(sorted(exact)),
        formula_fingerprints=tuple(sorted(fingerprints)),
    )


def _models() -> Any:
    # Keeps this retrieval baseline importable during the additive migration
    # landing order, as with the consolidation service.
    from app import models

    return models


def _latest_versions(versions: Sequence[Any], *, event_time: datetime | None) -> dict[uuid.UUID, Any]:
    latest: dict[uuid.UUID, Any] = {}
    for version in versions:
        if event_time is not None and (
            (version.asserted_effective_from and event_time < version.asserted_effective_from)
            or (version.asserted_effective_until and event_time >= version.asserted_effective_until)
        ):
            continue
        prior = latest.get(version.item_id)
        if prior is None or (version.recorded_source_sequence, version.version) > (
            prior.recorded_source_sequence,
            prior.version,
        ):
            latest[version.item_id] = version
    return latest


def _rank_channel(records: Sequence[_Candidate], score) -> list[_Candidate]:
    return sorted(
        (record for record in records if score(record) > 0),
        key=lambda record: (-score(record), str(record.item.id), record.version.version),
    )


def _lexical_score(candidate: _Candidate, analysis: QueryAnalysis) -> float:
    if not analysis.lexical_terms:
        return 0.0
    haystack = " ".join(
        [candidate.version.canonical_text, candidate.item.logical_key, *candidate.version.exact_terms]
    ).casefold()
    return float(sum(term in haystack for term in analysis.lexical_terms))


def _exact_score(candidate: _Candidate, analysis: QueryAnalysis) -> float:
    terms = {str(term).casefold() for term in candidate.version.exact_terms}
    formulas = set(candidate.version.formula_fingerprints)
    exact = len(terms & set(analysis.exact_terms))
    formula = len(formulas & set(analysis.formula_fingerprints))
    return float(exact + 3 * formula)


async def _raw_quote(session: AsyncSession, *, source: Any, provenance: Any, project_id: uuid.UUID) -> tuple[str | None, bool]:
    """Return only a verified stored span; never synthesize mathematical text."""

    try:
        resolved = await resolve_source_receipt(session, source, expected_project_id=project_id)
        if provenance.span_start is None:
            return None, False
        verify_exact_span(
            resolved.canonical_payload,
            byte_start=int(provenance.span_start),
            byte_end=int(provenance.span_end),
            span_hash=provenance.span_hash,
        )
        return (
            resolved.canonical_payload[int(provenance.span_start): int(provenance.span_end)].decode("utf-8"),
            True,
        )
    except (SourceResolutionError, UnicodeDecodeError, ValueError):
        return None, False


async def _raw_suffix(
    session: AsyncSession,
    *,
    models: Any,
    project_id: uuid.UUID,
    after_sequence: int,
    limit: int,
    access_policy: RetrievalAccessPolicy,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    # Raw native text is a separate disclosure class from canonical evidence.
    # Keep it absent unless a server-owned call site explicitly permits it.
    if not access_policy.include_raw_content:
        return (), False
    predicate = (
        models.StrategicMemorySource.project_id == project_id,
        models.StrategicMemorySource.source_sequence > after_sequence,
        models.StrategicMemorySource.payload_state == "active",
        models.StrategicMemorySource.tombstoned_at.is_(None),
        models.StrategicMemorySource.visibility.in_(access_policy.allowed_visibilities),
        models.StrategicMemorySource.sensitivity.in_(access_policy.allowed_sensitivities),
    )
    total = await session.scalar(select(func.count()).select_from(models.StrategicMemorySource).where(*predicate))
    degraded = int(total or 0) > limit
    if degraded:
        head_count = max(1, limit // 2)
        tail_count = limit - head_count
        head = (await session.execute(
            select(models.StrategicMemorySource).where(*predicate)
            .order_by(models.StrategicMemorySource.source_sequence).limit(head_count)
        )).scalars().all()
        tail = (await session.execute(
            select(models.StrategicMemorySource).where(*predicate)
            .order_by(models.StrategicMemorySource.source_sequence.desc()).limit(tail_count)
        )).scalars().all()
        sources = sorted({source.id: source for source in [*head, *tail]}.values(), key=lambda source: source.source_sequence)
    else:
        sources = (await session.execute(
            select(models.StrategicMemorySource).where(*predicate)
            .order_by(models.StrategicMemorySource.source_sequence)
        )).scalars().all()
    rows: list[dict[str, Any]] = []
    for source in sources:
        try:
            resolved = await resolve_source_receipt(session, source, expected_project_id=project_id)
            preview = resolved.canonical_payload[:2048].decode("utf-8", "ignore")
        except SourceResolutionError:
            continue
        rows.append({
            "source_id": str(source.id),
            "source_sequence": int(source.source_sequence),
            "source_type": source.source_type,
            "source_key": source.source_key,
            "content": preview,
            "content_truncated": len(resolved.canonical_payload) > 2048,
            "content_hash": source.content_hash,
        })
    return tuple(rows), degraded


async def retrieve_memory(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query: str,
    source_sequence: int | None = None,
    event_time: datetime | None = None,
    max_cards: int = 6,
    raw_suffix_limit: int = MAX_RAW_SUFFIX,
    access_policy: RetrievalAccessPolicy = DEFAULT_RETRIEVAL_ACCESS_POLICY,
) -> RetrievalResult:
    """Retrieve a bounded evidence pack without vectors or cross-project reads."""

    if not 1 <= max_cards <= MAX_EVIDENCE_CARDS:
        raise ValueError("max_cards is outside the bounded retrieval pack")
    if not 1 <= raw_suffix_limit <= MAX_RAW_SUFFIX:
        raise ValueError("raw_suffix_limit is outside the bounded retrieval pack")
    if not isinstance(access_policy, RetrievalAccessPolicy):
        raise TypeError("retrieval access policy must be backend-owned")
    analysis = analyze_memory_query(query)
    models = _models()
    state = await session.get(models.StrategicMemoryProjectState, project_id)
    if state is None:
        return RetrievalResult(analysis, (), False, "memory_state_unavailable", False, (), True, {"degraded": ["memory_state_unavailable"]})
    through = int(source_sequence if source_sequence is not None else state.incorporated_source_sequence)
    if through < 0:
        raise ValueError("source sequence must be nonnegative")

    # Scope and tombstones are applied before candidate assembly and ranking.
    active_sources = (
        await session.execute(
            select(models.StrategicMemorySource).where(
                models.StrategicMemorySource.project_id == project_id,
                models.StrategicMemorySource.payload_state == "active",
                models.StrategicMemorySource.tombstoned_at.is_(None),
                models.StrategicMemorySource.visibility.in_(access_policy.allowed_visibilities),
                models.StrategicMemorySource.sensitivity.in_(access_policy.allowed_sensitivities),
            )
        )
    ).scalars().all()
    source_by_id = {source.id: source for source in active_sources}
    raw_suffix, suffix_degraded = await _raw_suffix(
        session,
        models=models,
        project_id=project_id,
        after_sequence=through,
        limit=raw_suffix_limit,
        access_policy=access_policy,
    )
    if not source_by_id:
        return RetrievalResult(analysis, (), False, "no_authorized_evidence", False, raw_suffix, suffix_degraded, {"degraded": ["no_active_sources", "vector_disabled"]})

    versions = (
        await session.execute(
            select(models.StrategicMemoryItemVersion)
            .where(
                models.StrategicMemoryItemVersion.project_id == project_id,
                models.StrategicMemoryItemVersion.recorded_source_sequence <= through,
            )
            .order_by(models.StrategicMemoryItemVersion.recorded_source_sequence.desc(), models.StrategicMemoryItemVersion.version.desc())
            .limit(MAX_CANDIDATES * 4)
        )
    ).scalars().all()
    latest = _latest_versions(versions, event_time=event_time)
    if not latest:
        return RetrievalResult(analysis, (), False, "no_memory_evidence", False, raw_suffix, suffix_degraded, {"degraded": ["vector_disabled"]})
    item_ids = list(latest)
    items = (
        await session.execute(select(models.StrategicMemoryItem).where(
            models.StrategicMemoryItem.project_id == project_id,
            models.StrategicMemoryItem.id.in_(item_ids),
        ))
    ).scalars().all()
    item_by_id = {item.id: item for item in items}
    provenance_rows = (
        await session.execute(select(models.StrategicMemoryItemSource).where(
            models.StrategicMemoryItemSource.project_id == project_id,
            models.StrategicMemoryItemSource.item_id.in_(item_ids),
        ))
    ).scalars().all()
    provenance_by_key: dict[tuple[uuid.UUID, int], list[tuple[Any, Any]]] = defaultdict(list)
    for provenance in provenance_rows:
        source = source_by_id.get(provenance.source_id)
        if source is not None:
            provenance_by_key[(provenance.item_id, int(provenance.item_version))].append((provenance, source))

    transitions = (
        await session.execute(select(models.StrategicMemoryTransition).where(
            models.StrategicMemoryTransition.project_id == project_id,
            models.StrategicMemoryTransition.recorded_source_sequence <= through,
        ).order_by(models.StrategicMemoryTransition.recorded_source_sequence, models.StrategicMemoryTransition.id))
    ).scalars().all()
    # The item head is the present cache; transitions below replay the
    # requested as-of view over it.  Starting from the stored head also keeps
    # retrieval safe for legacy/imported rows whose terminal transition was
    # recorded before this bounded query window.
    # This is an as-of query.  ``current_status`` is a present-time cache and
    # can already contain a later retraction, so reconstruct from the immutable
    # transitions visible at ``through`` instead of leaking future status into a
    # historical answer.
    statuses = {item_id: "active" for item_id in latest if item_id in item_by_id}
    transition_by_key: dict[tuple[uuid.UUID, int], list[Any]] = defaultdict(list)
    for transition in transitions:
        if transition.source_id not in source_by_id:
            continue
        if event_time is not None and (
            (transition.effective_from and event_time < transition.effective_from)
            or (transition.effective_until and event_time >= transition.effective_until)
        ):
            continue
        transition_by_key[(transition.item_id, int(transition.item_version))].append(transition)
        if transition.item_id in latest and latest[transition.item_id].version == transition.item_version:
            statuses[transition.item_id] = {
                "activate": "active", "challenge": "challenged", "supersede": "superseded",
                "retract": "retracted", "forget": "forgotten",
            }[transition.transition]

    barriers = (await session.execute(select(models.StrategicMemoryCorrectionBarrier).where(
        models.StrategicMemoryCorrectionBarrier.project_id == project_id
    ))).scalars().all()
    barrier_events = (await session.execute(select(models.StrategicMemoryCorrectionBarrierEvent).where(
        models.StrategicMemoryCorrectionBarrierEvent.project_id == project_id,
        models.StrategicMemoryCorrectionBarrierEvent.recorded_source_sequence <= through,
    ))).scalars().all()
    events_by_barrier: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for event in barrier_events:
        if event.source_id in source_by_id:
            events_by_barrier[event.barrier_id].append(event)
    suppressed = {
        (barrier.candidate_item_id, int(barrier.candidate_version))
        for barrier in barriers
        if derive_barrier_state(events_by_barrier.get(barrier.id, ())) in {"opened", "target_confirmed"}
    }

    candidates: dict[tuple[uuid.UUID, int], _Candidate] = {}
    for item_id, version in latest.items():
        item = item_by_id.get(item_id)
        key = (item_id, int(version.version))
        if item is None or key not in provenance_by_key or key in suppressed:
            continue
        status = statuses[item_id]
        if (
            analysis.policy != "strict_history"
            and not analysis.correction_or_conflict
            and status not in {"active", "challenged"}
        ):
            continue
        candidates[key] = _Candidate(item=item, version=version, status=status, provenance=provenance_by_key[key])

    # Exact/entity seeds expand through typed project-scoped edges before pruning.
    seed_keys = {
        key for key, candidate in candidates.items()
        if _exact_score(candidate, analysis) > 0 or _lexical_score(candidate, analysis) > 0
    }
    if seed_keys:
        edges = (await session.execute(select(models.StrategicMemoryEdge).where(
            models.StrategicMemoryEdge.project_id == project_id,
        ))).scalars().all()
        for edge in edges:
            left = (edge.from_node_id, int(edge.from_version))
            right = (edge.to_node_id, int(edge.to_version))
            if left in seed_keys and right in candidates:
                candidates[right].relations.append(f"{edge.relation}:{left[0]}:{left[1]}")
                candidates[right].channels["edge"] = max(candidates[right].channels.get("edge", 0), 1.0)
                if edge.relation == "contradicts":
                    candidates[right].channels["conflict_pair"] = 1.0
            if right in seed_keys and left in candidates:
                candidates[left].relations.append(f"{edge.relation}:{right[0]}:{right[1]}")
                candidates[left].channels["edge"] = max(candidates[left].channels.get("edge", 0), 1.0)
                if edge.relation == "contradicts":
                    candidates[left].channels["conflict_pair"] = 1.0

    # Supersession links are a conflict/correction pair even absent a typed edge.
    for transitions_for_key in transition_by_key.values():
        for transition in transitions_for_key:
            if transition.transition != "supersede" or transition.target_item_id is None:
                continue
            origin = (transition.item_id, int(transition.item_version))
            target = (transition.target_item_id, int(transition.target_version))
            if origin in candidates and target in candidates:
                candidates[origin].relations.append(f"supersedes:{target[0]}:{target[1]}")
                candidates[target].relations.append(f"superseded_by:{origin[0]}:{origin[1]}")
                candidates[origin].channels["conflict_pair"] = 1.0
                candidates[target].channels["conflict_pair"] = 1.0

    records = list(candidates.values())
    channel_scores = {
        "explicit": lambda candidate: float(any(
            str(source.id) in analysis.query
            or bool(source.source_key and source.source_key in analysis.query)
            for _, source in candidate.provenance
        )),
        "exact": lambda candidate: _exact_score(candidate, analysis),
        "lexical": lambda candidate: _lexical_score(candidate, analysis),
        "recent": lambda candidate: float(candidate.version.recorded_source_sequence) / max(1, through),
        "current": lambda candidate: 1.0 if candidate.status == "active" else 0.4 if candidate.status == "challenged" else 0.1,
        "edge": lambda candidate: candidate.channels.get("edge", 0.0),
    }
    scores: dict[tuple[uuid.UUID, int], float] = defaultdict(float)
    reasons: dict[tuple[uuid.UUID, int], set[str]] = defaultdict(set)
    for name, fn in channel_scores.items():
        ranked = _rank_channel(records, fn)
        for rank, candidate in enumerate(ranked, 1):
            scores[candidate.key] += 1.0 / (_RRF_K + rank)
            reasons[candidate.key].add(name)
    for candidate in records:
        key = candidate.key
        if candidate.status == "active":
            scores[key] += 0.025
        if _exact_score(candidate, analysis):
            scores[key] += 0.04
        if candidate.channels.get("conflict_pair"):
            scores[key] += 0.03
            reasons[key].add("correction_pair")
        if analysis.policy == "fresh_start" and candidate.version.recorded_source_sequence < max(1, through - 8):
            scores[key] -= 0.01

    # Keep pair members before diversity pruning, then cap repeated source/item evidence.
    ranked = sorted(records, key=lambda candidate: (-scores[candidate.key], str(candidate.item.id), candidate.version.version))
    selected: list[_Candidate] = []
    source_counts: dict[uuid.UUID, int] = defaultdict(int)
    # A correction/conflict counterpart must survive selection even when its
    # individual lexical rank is lower than the first matching claim.
    for candidate in ranked:
        if len(selected) >= max_cards or not candidate.channels.get("conflict_pair"):
            continue
        primary_source = candidate.provenance[0][1].id
        if source_counts[primary_source] >= 2:
            continue
        selected.append(candidate)
        source_counts[primary_source] += 1
    for candidate in ranked:
        if len(selected) >= max_cards:
            break
        if candidate in selected:
            continue
        primary_source = candidate.provenance[0][1].id
        if source_counts[primary_source] >= 2:
            continue
        selected.append(candidate)
        source_counts[primary_source] += 1

    conflict_present = any("correction_pair" in reasons[candidate.key] or candidate.status == "challenged" for candidate in selected)
    cards: list[EvidenceCard] = []
    for candidate in selected:
        provenance, source = sorted(candidate.provenance, key=lambda pair: (pair[0].span_index, str(pair[1].id)))[0]
        if access_policy.include_raw_content:
            quote, quote_available = await _raw_quote(
                session, source=source, provenance=provenance, project_id=project_id
            )
        else:
            quote, quote_available = None, False
        if analysis.exact_required and not quote_available:
            reasons[candidate.key].add("exact_span_unavailable")
        cards.append(EvidenceCard(
            item_id=candidate.item.id,
            version=int(candidate.version.version),
            canonical_text=candidate.version.canonical_text,
            status=candidate.status,
            epistemic_status=candidate.version.epistemic_status,
            confidence=float(candidate.version.confidence),
            recorded_source_sequence=int(candidate.version.recorded_source_sequence),
            asserted_effective_from=candidate.version.asserted_effective_from,
            asserted_effective_until=candidate.version.asserted_effective_until,
            source_id=source.id,
            source_type=source.source_type,
            source_key=source.source_key,
            source_revision=source.source_revision,
            raw_quote=quote,
            raw_quote_available=quote_available,
            relation_path=tuple(sorted(set(candidate.relations))),
            selection_reasons=tuple(sorted(reasons[candidate.key])),
            score=scores[candidate.key],
        ))

    top_exact = max((_exact_score(candidate, analysis) for candidate in selected), default=0.0)
    top_lexical = max((_lexical_score(candidate, analysis) for candidate in selected), default=0.0)
    answerable = bool(cards) and (top_exact > 0 or top_lexical > 0 or not analysis.lexical_terms)
    abstain_reason: str | None = None
    if not cards:
        abstain_reason = "earlier_discussion_not_found"
    elif analysis.exact_required and not any(card.raw_quote_available for card in cards):
        answerable = False
        abstain_reason = "exact_source_span_unavailable"
    elif not answerable:
        abstain_reason = "no_evidence_above_lexical_threshold"
    trace = {
        "query_policy": analysis.policy,
        "channels": sorted(channel_scores),
        "candidate_count": len(records),
        "selected_count": len(cards),
        "through_source_sequence": through,
        "vector": "disabled_unqualified",
        "degraded": (
            (["raw_content_disabled"] if not access_policy.include_raw_content else [])
            + (["raw_suffix_truncated"] if suffix_degraded else [])
        ),
    }
    return RetrievalResult(analysis, tuple(cards), answerable, abstain_reason, conflict_present, raw_suffix, suffix_degraded, trace)


__all__ = [
    "DEFAULT_RETRIEVAL_ACCESS_POLICY", "EvidenceCard", "MemoryPolicy", "QueryAnalysis",
    "RetrievalAccessPolicy", "RetrievalResult",
    "analyze_memory_query", "retrieve_memory",
]
