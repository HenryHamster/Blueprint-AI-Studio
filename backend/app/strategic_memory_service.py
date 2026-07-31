"""Deterministic admission and temporal resolution for Strategic Memory V2.

This service is deliberately the only semantic write boundary.  Consolidation
workers submit a bounded proposal, but all identities, source spans, authority
fences, status transitions, and present-time projections are checked locally
before anything is admitted.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.strategic_memory_sources import (
    SourceResolutionError,
    canonical_json_bytes,
    resolve_source_receipt,
    verify_exact_span,
)
from app.time_utils import _utcnow


INSTALLED_PROPOSAL_SCHEMA_VERSION = 1
ITEM_KINDS = frozenset({
    "goal", "preference", "constraint", "definition", "notation", "assumption",
    "claim", "decision", "result", "counterexample", "open_question", "procedure", "lesson",
})
ITEM_STATUSES = frozenset({"active", "challenged", "superseded", "retracted", "forgotten"})
EPISTEMIC_STATUSES = frozenset({"operator_explicit", "verified_evidence", "agent_conclusion", "agent_hypothesis"})
TRANSITIONS = frozenset({"activate", "challenge", "supersede", "retract", "forget"})
EDGE_RELATIONS = frozenset({"supports", "contradicts", "derived_from", "depends_on"})
SOURCE_ROLES = frozenset({"assertion", "evidence", "correction", "verification"})
SPAN_KINDS = frozenset({"text", "inline_math", "display_math", "code", "identifier", "artifact_region"})
TERMINAL_DISPOSITIONS = frozenset({"incorporated", "no_memory", "quarantined"})
MAX_PROPOSAL_BYTES = 256 * 1024
_SHA256_HEX_LENGTH = 64


class LedgerAdmissionError(RuntimeError):
    """A proposed semantic write cannot be safely admitted."""


class SourceSpanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: uuid.UUID
    source_role: str
    span_index: int = Field(ge=0)
    span_kind: str
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    span_hash: str

    @field_validator("span_hash")
    @classmethod
    def _span_hash_is_digest(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("span hash must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _span_is_complete(self) -> "SourceSpanProposal":
        has_offsets = self.span_start is not None or self.span_end is not None
        if self.source_role not in SOURCE_ROLES:
            raise ValueError("unknown source role")
        if self.span_kind not in SPAN_KINDS:
            raise ValueError("unknown source span kind")
        if has_offsets and (self.span_start is None or self.span_end is None):
            raise ValueError("exact source span requires both bounds")
        if self.span_end is not None and self.span_start is not None and self.span_end <= self.span_start:
            raise ValueError("source span must be non-empty")
        return self


class ItemMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: uuid.UUID | None = None
    scope_type: Literal["project", "research_topic"] = "project"
    # The database uses an empty-string project-scope sentinel so the unique
    # identity fence remains effective on PostgreSQL (where NULLs are distinct).
    scope_key: str = ""
    kind: str
    logical_key: str
    canonical_text: str
    structured_value: dict[str, Any] = Field(default_factory=dict)
    epistemic_status: str
    confidence: float = Field(ge=0, le=1)
    asserted_effective_from: datetime | None = None
    asserted_effective_until: datetime | None = None
    exact_terms: tuple[str, ...] = ()
    formula_fingerprints: tuple[str, ...] = ()
    extractor_version: str
    sources: tuple[SourceSpanProposal, ...]

    @model_validator(mode="after")
    def _semantic_shape(self) -> "ItemMutation":
        if self.kind not in ITEM_KINDS or self.epistemic_status not in EPISTEMIC_STATUSES:
            raise ValueError("unknown item kind or epistemic status")
        if not self.logical_key.strip() or not self.canonical_text.strip() or not self.sources:
            raise ValueError("item mutation needs logical key, text, and provenance")
        if (self.scope_type == "project" and self.scope_key) or (
            self.scope_type == "research_topic" and not self.scope_key.strip()
        ):
            raise ValueError("item scope type and key are inconsistent")
        if self.asserted_effective_from and self.asserted_effective_until and self.asserted_effective_until <= self.asserted_effective_from:
            raise ValueError("invalid asserted effective interval")
        return self


class TransitionMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: uuid.UUID
    item_version: int = Field(ge=1)
    transition: str
    source_id: uuid.UUID
    target_item_id: uuid.UUID | None = None
    target_version: int | None = Field(default=None, ge=1)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _transition_shape(self) -> "TransitionMutation":
        if self.transition not in TRANSITIONS:
            raise ValueError("unknown transition")
        if (self.target_item_id is None) != (self.target_version is None):
            raise ValueError("transition target id/version must be supplied together")
        if self.transition == "supersede" and self.target_item_id is None:
            raise ValueError("supersede requires a target")
        if (
            self.transition == "supersede"
            and self.target_item_id == self.item_id
            and self.target_version == self.item_version
        ):
            raise ValueError("supersede cannot target itself")
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError("invalid transition effective interval")
        return self


class EdgeMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    from_item_id: uuid.UUID
    from_version: int = Field(ge=1)
    relation: str
    to_item_id: uuid.UUID
    to_version: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    source_ids: tuple[uuid.UUID, ...]

    @model_validator(mode="after")
    def _edge_shape(self) -> "EdgeMutation":
        if self.relation not in EDGE_RELATIONS or not self.source_ids:
            raise ValueError("edge needs an allowed relation and provenance")
        if self.from_item_id == self.to_item_id and self.from_version == self.to_version:
            raise ValueError("self-edge is invalid")
        return self


class SourceDispositionMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: uuid.UUID
    source_hash: str
    status: str
    reason: str = ""
    classifier_version: str | None = None
    extractor_model: str | None = None
    prompt_version: str | None = None

    @field_validator("source_hash")
    @classmethod
    def _source_hash_is_digest(cls, value: str) -> str:
        if len(value) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source hash must be a lowercase SHA-256 digest")
        return value

    @field_validator("status")
    @classmethod
    def _terminal(cls, value: str) -> str:
        if value not in TERMINAL_DISPOSITIONS:
            raise ValueError("source disposition must be terminal")
        return value


class LedgerProposal(BaseModel):
    """Installed V1 worker proposal; unknown fields fail closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int
    job_id: uuid.UUID
    base_ledger_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=255)
    dispositions: tuple[SourceDispositionMutation, ...]
    items: tuple[ItemMutation, ...] = ()
    transitions: tuple[TransitionMutation, ...] = ()
    edges: tuple[EdgeMutation, ...] = ()

    @model_validator(mode="after")
    def _installed_and_unique(self) -> "LedgerProposal":
        if self.schema_version != INSTALLED_PROPOSAL_SCHEMA_VERSION:
            raise ValueError("proposal schema version is not installed")
        if len({entry.source_id for entry in self.dispositions}) != len(self.dispositions):
            raise ValueError("proposal repeats a source disposition")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class LedgerAdmissionResult:
    job_id: uuid.UUID
    ledger_revision: int
    projection_project_id: uuid.UUID | None
    replayed: bool


def _models() -> Any:
    """Late import keeps this module independently importable while 0714 lands."""

    from app import models
    return models


async def _load_claimed_sources(session: AsyncSession, models: Any, job: Any) -> list[tuple[Any, Any]]:
    query = (
        select(models.StrategicMemorySource, models.StrategicMemorySourceDisposition)
        .join(models.StrategicMemorySourceDisposition, models.StrategicMemorySourceDisposition.source_id == models.StrategicMemorySource.id)
        .where(models.StrategicMemorySource.project_id == job.project_id)
    )
    if job.mode == "contiguous":
        query = query.where(models.StrategicMemorySource.source_sequence >= job.source_start, models.StrategicMemorySource.source_sequence <= job.source_end)
    else:
        query = query.where(models.StrategicMemorySource.id == job.target_source_id)
    return (await session.execute(query.order_by(models.StrategicMemorySource.source_sequence).with_for_update())).all()


async def _validate_claim_and_proposal(session: AsyncSession, models: Any, proposal: LedgerProposal, *, lease_owner: str, lease_generation: int) -> tuple[Any, Any, list[tuple[Any, Any]]]:
    from app.strategic_memory_control import SourceControlInvariantError, SourceJobLeaseError, _require_job_lease, lock_source_project_state

    job_project = await session.scalar(select(models.StrategicMemoryConsolidationJob.project_id).where(models.StrategicMemoryConsolidationJob.id == proposal.job_id))
    if job_project is None:
        raise LedgerAdmissionError("consolidation job not found")
    state = await lock_source_project_state(session, job_project)
    job = await session.scalar(select(models.StrategicMemoryConsolidationJob).where(models.StrategicMemoryConsolidationJob.id == proposal.job_id).with_for_update())
    _require_job_lease(job, lease_owner, lease_generation, _utcnow())
    assert job is not None
    if int(job.base_ledger_revision) != proposal.base_ledger_revision or int(state.ledger_revision) != proposal.base_ledger_revision:
        raise LedgerAdmissionError("base ledger revision changed")
    if int(job.context_authority_generation) != int(state.context_authority_generation) or int(job.privacy_generation) != int(state.privacy_generation):
        raise LedgerAdmissionError("authority or privacy generation changed")
    rows = await _load_claimed_sources(session, models, job)
    if job.mode == "contiguous":
        expected = list(range(int(job.source_start), int(job.source_end) + 1))
        if [int(source.source_sequence) for source, _ in rows] != expected:
            raise SourceControlInvariantError("claimed source range has a gap")
        if int(job.source_start) != int(state.incorporated_source_sequence) + 1:
            raise SourceControlInvariantError("claimed job no longer begins at watermark")
        if any(disposition.status != "pending" for _, disposition in rows):
            raise SourceControlInvariantError("claimed source is no longer pending")
    elif len(rows) != 1 or rows[0][1].status != "quarantined":
        raise SourceControlInvariantError("reconsolidation target is not quarantined")
    proposal_by_id = {entry.source_id: entry for entry in proposal.dispositions}
    expected_ids = {source.id for source, _ in rows}
    if set(proposal_by_id) != expected_ids:
        raise LedgerAdmissionError("source hashes/dispositions must exactly cover claimed sources")
    for source, _ in rows:
        entry = proposal_by_id[source.id]
        if source.content_hash != entry.source_hash or source.payload_state != "active" or source.tombstoned_at is not None:
            raise LedgerAdmissionError("claimed source hash or privacy state changed")
    return state, job, rows


async def _resolve_provenance(session: AsyncSession, models: Any, *, project_id: uuid.UUID, source_map: Mapping[uuid.UUID, Any], span: SourceSpanProposal) -> None:
    source = source_map.get(span.source_id)
    if source is None:
        raise LedgerAdmissionError("item provenance references a source outside the claimed job")
    resolved = await resolve_source_receipt(session, source, expected_project_id=project_id)
    if span.span_start is not None:
        try:
            verify_exact_span(resolved.canonical_payload, byte_start=span.span_start, byte_end=span.span_end or 0, span_hash=span.span_hash)
        except SourceResolutionError as exc:
            raise LedgerAdmissionError("item provenance span does not verify") from exc
    elif span.span_hash != resolved.content_hash:
        raise LedgerAdmissionError("whole-source provenance hash does not verify")


def _enforce_admission_policy(item: ItemMutation, source_map: Mapping[uuid.UUID, Any]) -> None:
    supporting = [source_map[span.source_id] for span in item.sources]
    if item.epistemic_status == "operator_explicit" and not any(source.actor_kind in {"user", "operator"} for source in supporting):
        raise LedgerAdmissionError("operator-explicit memory requires operator provenance")
    if item.epistemic_status == "verified_evidence" and not any(source.source_type == "task_result" for source in supporting):
        raise LedgerAdmissionError("verified-evidence memory requires verified task evidence")
    if item.kind in {"preference", "constraint"} and not any(source.actor_kind in {"user", "operator"} for source in supporting):
        raise LedgerAdmissionError("external evidence cannot establish operator preferences or constraints")


def _assert_acyclic_supersession(edges: Mapping[tuple[uuid.UUID, int], tuple[uuid.UUID, int]]) -> None:
    """Reject a stable-item/version cycle before any semantic row is staged."""

    visiting: set[tuple[uuid.UUID, int]] = set()
    visited: set[tuple[uuid.UUID, int]] = set()

    def visit(node: tuple[uuid.UUID, int]) -> None:
        if node in visiting:
            raise LedgerAdmissionError("supersession cycle is not admissible")
        if node in visited:
            return
        visiting.add(node)
        target = edges.get(node)
        if target is not None:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


async def _validate_and_admit_proposal_without_savepoint(session: AsyncSession, proposal: LedgerProposal, *, lease_owner: str, lease_generation: int) -> LedgerAdmissionResult:
    """Atomically validate and admit one leased V1 proposal; never commits."""

    models = _models()
    proposal_hash = proposal.content_hash()
    # Exact replay is allowed only for the exact completed job/key/hash.
    existing = await session.scalar(select(models.StrategicMemoryConsolidationJob).where(models.StrategicMemoryConsolidationJob.id == proposal.job_id))
    if existing is not None and existing.status == "complete":
        if existing.accepted_proposal_idempotency_key == proposal.idempotency_key and existing.accepted_proposal_hash == proposal_hash:
            if existing.resulting_ledger_revision is None:
                raise LedgerAdmissionError("completed job lacks its semantic receipt")
            return LedgerAdmissionResult(existing.id, int(existing.resulting_ledger_revision), existing.project_id, True)
        raise LedgerAdmissionError("completed job accepts only its exact proposal replay")
    state, job, rows = await _validate_claim_and_proposal(session, models, proposal, lease_owner=lease_owner, lease_generation=lease_generation)
    source_map = {source.id: source for source, _ in rows}
    if len(proposal.canonical_bytes()) > MAX_PROPOSAL_BYTES:
        raise LedgerAdmissionError("proposal exceeds bounded admission size")
    semantic_source_ids = {
        span.source_id for item in proposal.items for span in item.sources
    } | {transition.source_id for transition in proposal.transitions} | {
        source_id for edge in proposal.edges for source_id in edge.source_ids
    }
    disposition_by_id = {entry.source_id: entry.status for entry in proposal.dispositions}
    if any(disposition_by_id[source_id] != "incorporated" for source_id in semantic_source_ids):
        raise LedgerAdmissionError("semantic ledger rows require incorporated source receipts")
    supersession_edges = {
        (transition.item_id, int(transition.item_version)): (
            transition.target_item_id,
            int(transition.target_version),
        )
        for transition in (
            await session.execute(
                select(models.StrategicMemoryTransition).where(
                    models.StrategicMemoryTransition.project_id == job.project_id,
                    models.StrategicMemoryTransition.transition == "supersede",
                )
            )
        ).scalars()
        if transition.target_item_id is not None and transition.target_version is not None
    }
    for transition in proposal.transitions:
        if transition.transition != "supersede":
            continue
        assert transition.target_item_id is not None and transition.target_version is not None
        origin = (transition.item_id, int(transition.item_version))
        target = (transition.target_item_id, int(transition.target_version))
        previous = supersession_edges.get(origin)
        if previous is not None and previous != target:
            raise LedgerAdmissionError("item version already has a different supersession target")
        supersession_edges[origin] = target
    _assert_acyclic_supersession(supersession_edges)

    for mutation in proposal.items:
        for span in mutation.sources:
            await _resolve_provenance(session, models, project_id=job.project_id, source_map=source_map, span=span)
        _enforce_admission_policy(mutation, source_map)
        item_id = mutation.item_id or uuid.uuid4()
        item = await session.get(models.StrategicMemoryItem, item_id)
        if item is None:
            item = models.StrategicMemoryItem(id=item_id, project_id=job.project_id, scope_type=mutation.scope_type, scope_key=mutation.scope_key, kind=mutation.kind, logical_key=mutation.logical_key, current_version=0, current_status="active")
            session.add(item)
        elif (
            item.project_id != job.project_id
            or item.kind != mutation.kind
            or item.scope_type != mutation.scope_type
            or item.scope_key != mutation.scope_key
            or item.logical_key != mutation.logical_key
        ):
            raise LedgerAdmissionError("item mutation changes immutable item identity")
        next_version = int(item.current_version) + 1
        content_hash = hashlib.sha256(canonical_json_bytes({"canonical_text": mutation.canonical_text, "structured_value": mutation.structured_value})).hexdigest()
        version = models.StrategicMemoryItemVersion(item_id=item.id, version=next_version, project_id=job.project_id, canonical_text=mutation.canonical_text, structured_value=mutation.structured_value, epistemic_status=mutation.epistemic_status, confidence=mutation.confidence, asserted_effective_from=mutation.asserted_effective_from, asserted_effective_until=mutation.asserted_effective_until, recorded_at=_utcnow(), recorded_source_sequence=max(source_map[span.source_id].source_sequence for span in mutation.sources), exact_terms=list(mutation.exact_terms), formula_fingerprints=list(mutation.formula_fingerprints), content_hash=content_hash, created_by_job_id=job.id, extractor_version=mutation.extractor_version)
        session.add(version)
        # The provenance table has a composite FK to the immutable version.
        # Do not rely on ORM insertion ordering here: ItemSource has no mapper
        # relationship to ItemVersion, so PostgreSQL can otherwise see its row
        # before the referenced version during the same unit-of-work flush.
        await session.flush()
        for span in mutation.sources:
            session.add(models.StrategicMemoryItemSource(item_id=item.id, item_version=next_version, source_id=span.source_id, source_role=span.source_role, span_index=span.span_index, project_id=job.project_id, span_kind=span.span_kind, span_start=span.span_start, span_end=span.span_end, span_hash=span.span_hash))
        item.current_version = next_version
        item.current_status = "active"
        item.updated_at = _utcnow()
        session.add(item)
    await session.flush()

    for mutation in proposal.transitions:
        item = await session.get(models.StrategicMemoryItem, mutation.item_id)
        if item is None or item.project_id != job.project_id or mutation.source_id not in source_map:
            raise LedgerAdmissionError("transition crosses project or source scope")
        if mutation.target_item_id is not None:
            target = await session.get(models.StrategicMemoryItem, mutation.target_item_id)
            if target is None or target.project_id != job.project_id:
                raise LedgerAdmissionError("transition target crosses project")
        version = await session.get(models.StrategicMemoryItemVersion, (mutation.item_id, mutation.item_version))
        if version is None or version.project_id != job.project_id:
            raise LedgerAdmissionError("transition references an unknown item version")
        if mutation.target_item_id is not None:
            target_version = await session.get(models.StrategicMemoryItemVersion, (mutation.target_item_id, mutation.target_version))
            if target_version is None or target_version.project_id != job.project_id:
                raise LedgerAdmissionError("transition target references an unknown item version")
        session.add(models.StrategicMemoryTransition(project_id=job.project_id, item_id=mutation.item_id, item_version=mutation.item_version, transition=mutation.transition, target_item_id=mutation.target_item_id, target_version=mutation.target_version, effective_from=mutation.effective_from, effective_until=mutation.effective_until, recorded_source_sequence=source_map[mutation.source_id].source_sequence, source_id=mutation.source_id, reason=mutation.reason))
        now = _utcnow()
        applies_now = (
            (mutation.effective_from is None or mutation.effective_from <= now)
            and (mutation.effective_until is None or now < mutation.effective_until)
        )
        if mutation.item_version == item.current_version and applies_now:
            item.current_status = {
                "activate": "active",
                "challenge": "challenged",
                "supersede": "superseded",
                "retract": "retracted",
                "forget": "forgotten",
            }[mutation.transition]
            item.forgotten_at = now if mutation.transition == "forget" else None
            item.updated_at = now
            session.add(item)

    for mutation in proposal.edges:
        for item_id, version in ((mutation.from_item_id, mutation.from_version), (mutation.to_item_id, mutation.to_version)):
            item = await session.get(models.StrategicMemoryItem, item_id)
            item_version = await session.get(models.StrategicMemoryItemVersion, (item_id, version))
            if item is None or item.project_id != job.project_id or item_version is None or item_version.project_id != job.project_id:
                raise LedgerAdmissionError("edge endpoint crosses project or version")
        if any(source_id not in source_map for source_id in mutation.source_ids):
            raise LedgerAdmissionError("edge provenance is outside claimed job")
        edge = models.StrategicMemoryEdge(project_id=job.project_id, from_node_type="item_version", from_node_id=mutation.from_item_id, from_version=mutation.from_version, relation=mutation.relation, to_node_type="item_version", to_node_id=mutation.to_item_id, to_version=mutation.to_version, confidence=mutation.confidence, version=1)
        session.add(edge)
        await session.flush()
        for source_id in mutation.source_ids:
            session.add(models.StrategicMemoryEdgeSource(edge_id=edge.id, source_id=source_id, project_id=job.project_id))
    await session.flush()

    # One durable revision covers semantic rows and terminal control disposition.
    state.ledger_revision = int(state.ledger_revision) + 1
    job.accepted_proposal_idempotency_key = proposal.idempotency_key
    job.accepted_proposal_hash = proposal_hash
    job.proposal_schema_version = str(proposal.schema_version)
    job.resulting_ledger_revision = int(state.ledger_revision)
    session.add(state)
    session.add(job)
    await session.flush()
    from app.strategic_memory_control import SourceDispositionDecision, complete_consolidation_job
    decisions = {entry.source_id: SourceDispositionDecision(status=entry.status, reason=entry.reason, classifier_version=entry.classifier_version, extractor_model=entry.extractor_model, prompt_version=entry.prompt_version) for entry in proposal.dispositions}
    completed = await complete_consolidation_job(session, job_id=job.id, lease_owner=lease_owner, lease_generation=lease_generation, decisions=decisions)
    projection = await rebuild_present_projection(session, project_id=job.project_id, ledger_revision=int(state.ledger_revision), through_source_sequence=int(state.incorporated_source_sequence))
    return LedgerAdmissionResult(completed.id, int(state.ledger_revision), projection.project_id, False)


async def validate_and_admit_proposal(
    session: AsyncSession,
    proposal: LedgerProposal,
    *,
    lease_owner: str,
    lease_generation: int,
) -> LedgerAdmissionResult:
    """Admit exactly one proposal without poisoning the caller transaction.

    The caller owns the outer transaction and may intentionally catch a
    ``LedgerAdmissionError``.  A savepoint therefore contains every non-replay
    flush/control update so the failure cannot later leak through an outer
    commit.
    """

    models = _models()
    proposal_hash = proposal.content_hash()
    existing = await session.scalar(
        select(models.StrategicMemoryConsolidationJob).where(
            models.StrategicMemoryConsolidationJob.id == proposal.job_id
        )
    )
    if existing is not None and existing.status == "complete":
        if (
            existing.accepted_proposal_idempotency_key == proposal.idempotency_key
            and existing.accepted_proposal_hash == proposal_hash
        ):
            if existing.resulting_ledger_revision is None:
                raise LedgerAdmissionError("completed job lacks its semantic receipt")
            return LedgerAdmissionResult(
                existing.id,
                int(existing.resulting_ledger_revision),
                existing.project_id,
                True,
            )
        raise LedgerAdmissionError("completed job accepts only its exact proposal replay")
    async with session.begin_nested():
        return await _validate_and_admit_proposal_without_savepoint(
            session,
            proposal,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )


async def rebuild_present_projection(session: AsyncSession, *, project_id: uuid.UUID, ledger_revision: int, through_source_sequence: int) -> Any:
    """Rebuild a present-time cache from immutable ledger rows, never truth."""

    models = _models()
    items = (await session.execute(select(models.StrategicMemoryItem).where(models.StrategicMemoryItem.project_id == project_id))).scalars().all()
    item_metadata = {item.id: item for item in items}
    resolved = await as_of_memory_items(
        session,
        project_id=project_id,
        source_sequence=through_source_sequence,
        event_time=_utcnow(),
    )
    projection_items = []
    for entry in resolved:
        item = item_metadata.get(entry["item_id"])
        if item is not None:
            projection_items.append({
                "id": str(item.id),
                "logical_key": item.logical_key,
                "kind": item.kind,
                "status": entry["status"],
                "version": entry["version"],
                "canonical_text": entry["canonical_text"],
            })
    projection_items.sort(key=lambda entry: (entry["logical_key"], entry["id"]))
    projection_value = {"schema_version": 1, "ledger_revision": ledger_revision, "items": projection_items}
    payload = canonical_json_bytes(projection_value)
    projection = await session.scalar(
        select(models.StrategicMemoryProjection)
        .where(models.StrategicMemoryProjection.project_id == project_id)
        .with_for_update()
    )
    projection_fields = {
        "ledger_revision": ledger_revision,
        "through_source_sequence": through_source_sequence,
        "structured_state": projection_value,
        "canonical_text": payload.decode("utf-8"),
        "canonical_sha256": hashlib.sha256(payload).hexdigest(),
        "rebuilt_at": _utcnow(),
    }
    if projection is None:
        projection = models.StrategicMemoryProjection(
            project_id=project_id, **projection_fields
        )
    else:
        for name, value in projection_fields.items():
            setattr(projection, name, value)
    session.add(projection)
    await session.flush()
    return projection


async def as_of_memory_items(session: AsyncSession, *, project_id: uuid.UUID, source_sequence: int, event_time: datetime | None = None) -> list[dict[str, Any]]:
    """Resolve immutable item versions/transitions at system-time, optionally event-time."""

    models = _models()
    active_source_ids = set(
        (await session.execute(
            select(models.StrategicMemorySource.id).where(
                models.StrategicMemorySource.project_id == project_id,
                models.StrategicMemorySource.payload_state == "active",
                models.StrategicMemorySource.tombstoned_at.is_(None),
            )
        )).scalars()
    )
    active_provenance = set(
        (await session.execute(
            select(
                models.StrategicMemoryItemSource.item_id,
                models.StrategicMemoryItemSource.item_version,
            ).where(
                models.StrategicMemoryItemSource.project_id == project_id,
                models.StrategicMemoryItemSource.source_id.in_(active_source_ids),
            )
        )).all()
    ) if active_source_ids else set()
    versions = (await session.execute(select(models.StrategicMemoryItemVersion).where(models.StrategicMemoryItemVersion.project_id == project_id, models.StrategicMemoryItemVersion.recorded_source_sequence <= source_sequence).order_by(models.StrategicMemoryItemVersion.item_id, models.StrategicMemoryItemVersion.version))).scalars().all()
    transitions = (await session.execute(select(models.StrategicMemoryTransition).where(models.StrategicMemoryTransition.project_id == project_id, models.StrategicMemoryTransition.recorded_source_sequence <= source_sequence).order_by(models.StrategicMemoryTransition.recorded_source_sequence, models.StrategicMemoryTransition.id))).scalars().all()
    latest: dict[uuid.UUID, Any] = {}
    for version in versions:
        if event_time is not None and ((version.asserted_effective_from and event_time < version.asserted_effective_from) or (version.asserted_effective_until and event_time >= version.asserted_effective_until)):
            continue
        prior = latest.get(version.item_id)
        if prior is None or (version.recorded_source_sequence, version.version) > (prior.recorded_source_sequence, prior.version):
            latest[version.item_id] = version
    # Do not fall back to an older assertion if the selected head's receipts
    # became unavailable.  That would present stale content as current.
    latest = {
        item_id: version
        for item_id, version in latest.items()
        if (item_id, version.version) in active_provenance
    }
    statuses = {item_id: "active" for item_id in latest}
    for transition in transitions:
        selected_version = latest.get(transition.item_id)
        if (
            selected_version is None
            or selected_version.version != transition.item_version
            or transition.source_id not in active_source_ids
        ):
            continue
        if event_time is not None and ((transition.effective_from and event_time < transition.effective_from) or (transition.effective_until and event_time >= transition.effective_until)):
            continue
        statuses[transition.item_id] = {"activate": "active", "challenge": "challenged", "supersede": "superseded", "retract": "retracted", "forget": "forgotten"}[transition.transition]
    barriers = (await session.execute(
        select(models.StrategicMemoryCorrectionBarrier).where(
            models.StrategicMemoryCorrectionBarrier.project_id == project_id
        )
    )).scalars().all()
    barrier_events = (await session.execute(
        select(models.StrategicMemoryCorrectionBarrierEvent).where(
            models.StrategicMemoryCorrectionBarrierEvent.project_id == project_id,
            models.StrategicMemoryCorrectionBarrierEvent.recorded_source_sequence <= source_sequence,
        )
    )).scalars().all()
    events_by_barrier: dict[uuid.UUID, list[Any]] = {}
    for event in barrier_events:
        events_by_barrier.setdefault(event.barrier_id, []).append(event)
    suppressed_versions: set[tuple[uuid.UUID, int]] = set()
    for barrier in barriers:
        events = sorted(
            events_by_barrier.get(barrier.id, ()),
            key=lambda row: (row.recorded_source_sequence, row.created_at, str(row.id)),
        )
        if events and events[-1].event_type in {"opened", "target_confirmed"}:
            suppressed_versions.add((barrier.candidate_item_id, barrier.candidate_version))
    return [
        {
            "item_id": item_id,
            "version": version.version,
            "status": statuses[item_id],
            "canonical_text": version.canonical_text,
            "structured_value": version.structured_value,
            "recorded_source_sequence": version.recorded_source_sequence,
        }
        for item_id, version in latest.items()
        if (item_id, version.version) not in suppressed_versions
    ]
