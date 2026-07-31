"""Read-only, content-safe integrity checks for the Strategic memory ledger.

The audit deliberately reports only identifiers, revisions, and invariant
codes.  It is safe to run before an authority cutover because it neither
repairs nor mutates the append-only ledger it is inspecting.
"""

from __future__ import annotations

import uuid
import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.strategic_memory_corrections import derive_barrier_state


_TRANSITION_STATUS = {
    "activate": "active",
    "challenge": "challenged",
    "supersede": "superseded",
    "retract": "retracted",
    "forget": "forgotten",
}
_CURRENT_STATUSES = frozenset(_TRANSITION_STATUS.values())
_ALLOWED_EDGE_RELATIONS = frozenset({"supports", "contradicts", "depends_on", "derived_from"})


@dataclass(frozen=True)
class LedgerInvariantViolation:
    """A non-content diagnostic suitable for logs and cutover decisions."""

    code: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class LedgerInvariantReport:
    project_id: uuid.UUID
    violations: tuple[LedgerInvariantViolation, ...]

    @property
    def is_clean(self) -> bool:
        return not self.violations


def _field(record: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _record_id(record: Mapping[str, Any] | Any, *names: str) -> str:
    values = [_field(record, name) for name in names]
    return ":".join(str(value) for value in values if value is not None) or "unknown"


def _version_key(record: Mapping[str, Any] | Any, item_id_field: str = "item_id") -> tuple[Any, Any]:
    return (_field(record, item_id_field), _field(record, "version", _field(record, "item_version")))


def audit_ledger_records(
    *,
    project_id: uuid.UUID,
    state: Mapping[str, Any] | Any | None,
    sources: Sequence[Mapping[str, Any] | Any],
    items: Sequence[Mapping[str, Any] | Any],
    versions: Sequence[Mapping[str, Any] | Any],
    transitions: Sequence[Mapping[str, Any] | Any],
    item_sources: Sequence[Mapping[str, Any] | Any],
    edges: Sequence[Mapping[str, Any] | Any],
    edge_sources: Sequence[Mapping[str, Any] | Any] = (),
    barriers: Sequence[Mapping[str, Any] | Any] = (),
    barrier_events: Sequence[Mapping[str, Any] | Any] = (),
    projection: Mapping[str, Any] | Any | None,
) -> LedgerInvariantReport:
    """Check a project-scoped snapshot without exposing content.

    Inputs may be ORM rows or small mapping fixtures.  That makes the core
    auditor independently testable while the database adapter remains a thin,
    read-only query layer.  Callers must pass the complete project snapshot;
    cross-project rows are intentionally treated as violations rather than
    silently filtered away.
    """

    violations: list[LedgerInvariantViolation] = []

    def violation(code: str, *record_ids: str) -> None:
        violations.append(LedgerInvariantViolation(code, tuple(record_ids)))

    source_by_id: dict[Any, Mapping[str, Any] | Any] = {}
    sequences: dict[int, str] = {}
    for source in sources:
        source_id = _field(source, "id")
        record_id = _record_id(source, "id")
        source_by_id[source_id] = source
        if _field(source, "project_id") != project_id:
            violation("foreign_project_source", record_id)
        sequence = _field(source, "source_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            violation("invalid_source_sequence", record_id)
        elif sequence in sequences:
            violation("duplicate_source_sequence", sequences[sequence], record_id)
        else:
            sequences[sequence] = record_id

    item_by_id: dict[Any, Mapping[str, Any] | Any] = {}
    for item in items:
        item_id = _field(item, "id")
        record_id = _record_id(item, "id")
        if item_id in item_by_id:
            violation("duplicate_item_id", _record_id(item_by_id[item_id], "id"), record_id)
        item_by_id[item_id] = item
        if _field(item, "project_id") != project_id:
            violation("foreign_project_item", record_id)
        if _field(item, "current_status") not in _CURRENT_STATUSES:
            violation("invalid_item_current_status", record_id)

    version_by_key: dict[tuple[Any, Any], Mapping[str, Any] | Any] = {}
    versions_by_item: dict[Any, list[int]] = defaultdict(list)
    for version in versions:
        key = _version_key(version)
        record_id = _record_id(version, "item_id", "version")
        if key in version_by_key:
            violation("duplicate_item_version", _record_id(version_by_key[key], "item_id", "version"), record_id)
        version_by_key[key] = version
        if isinstance(key[1], int):
            versions_by_item[key[0]].append(key[1])
        item = item_by_id.get(key[0])
        if _field(version, "project_id") != project_id:
            violation("foreign_project_version", record_id)
        if item is None:
            violation("version_missing_item", record_id)
        elif _field(version, "project_id") != _field(item, "project_id"):
            violation("version_item_project_mismatch", record_id, _record_id(item, "id"))

    transitions_by_item: dict[Any, list[Mapping[str, Any] | Any]] = defaultdict(list)
    supersession_edges: dict[tuple[Any, Any], list[tuple[tuple[Any, Any], str]]] = defaultdict(list)
    for transition in transitions:
        item_id = _field(transition, "item_id")
        key = (_field(transition, "item_id"), _field(transition, "item_version"))
        record_id = _record_id(transition, "id", "item_id", "item_version")
        transitions_by_item[item_id].append(transition)
        if _field(transition, "project_id") != project_id:
            violation("foreign_project_transition", record_id)
        item = item_by_id.get(item_id)
        if item is None:
            violation("transition_missing_item", record_id)
        elif _field(transition, "project_id") != _field(item, "project_id"):
            violation("transition_item_project_mismatch", record_id, _record_id(item, "id"))
        if key not in version_by_key:
            violation("transition_missing_version", record_id)
        source = source_by_id.get(_field(transition, "source_id"))
        if source is None:
            violation("transition_missing_source", record_id)
        elif _field(source, "project_id") != _field(transition, "project_id"):
            violation("transition_source_project_mismatch", record_id, _record_id(source, "id"))
        elif _field(transition, "recorded_source_sequence") != _field(source, "source_sequence"):
            violation("transition_source_sequence_mismatch", record_id, _record_id(source, "id"))
        target_id = _field(transition, "target_item_id")
        target_version = _field(transition, "target_version")
        if (target_id is None) != (target_version is None):
            violation("transition_incomplete_target", record_id)
        elif target_id is not None and (target_id, target_version) not in version_by_key:
            violation("transition_missing_target_version", record_id)
        transition_type = _field(transition, "transition")
        if transition_type not in _TRANSITION_STATUS:
            violation("invalid_transition_type", record_id)
        elif transition_type == "supersede" and target_id is None:
            violation("supersede_missing_target", record_id)
        elif transition_type != "supersede" and target_id is not None:
            violation("nonsupersede_transition_has_target", record_id)
        elif transition_type == "supersede":
            source_key = (_field(transition, "item_id"), _field(transition, "item_version"))
            target_key = (target_id, target_version)
            if source_key == target_key:
                violation("supersede_self_target", record_id)
            else:
                supersession_edges[source_key].append((target_key, record_id))

    visited: set[tuple[Any, Any]] = set()
    active_path: set[tuple[Any, Any]] = set()

    def visit_supersession(node: tuple[Any, Any]) -> None:
        visited.add(node)
        active_path.add(node)
        for target, transition_id in supersession_edges.get(node, []):
            if target in active_path:
                violation("cyclic_supersession", transition_id)
            elif target not in visited:
                visit_supersession(target)
        active_path.remove(node)

    for node in sorted(supersession_edges, key=lambda key: (str(key[0]), str(key[1]))):
        if node not in visited:
            visit_supersession(node)

    active_provenance: set[tuple[Any, Any]] = set()
    for item_source in item_sources:
        key = _version_key(item_source)
        record_id = _record_id(item_source, "item_id", "item_version", "source_id", "span_index")
        source = source_by_id.get(_field(item_source, "source_id"))
        version = version_by_key.get(key)
        if _field(item_source, "project_id") != project_id:
            violation("foreign_project_item_source", record_id)
        if version is None:
            violation("item_source_missing_version", record_id)
        if source is None:
            violation("item_source_missing_source", record_id)
            continue
        if _field(source, "project_id") != project_id:
            violation("item_source_source_project_mismatch", record_id, _record_id(source, "id"))
        if version is not None and _field(source, "project_id") == _field(version, "project_id"):
            if _field(source, "payload_state", "active") == "active":
                active_provenance.add(key)

    for item in items:
        item_id = _field(item, "id")
        current_version = _field(item, "current_version")
        item_record_id = _record_id(item, "id")
        key = (item_id, current_version)
        if key not in version_by_key:
            violation("item_head_missing_version", item_record_id)
            continue
        if versions_by_item[item_id] and current_version != max(versions_by_item[item_id]):
            violation("item_head_not_latest_version", item_record_id)
        if _field(item, "current_status") in {"active", "challenged"} and key not in active_provenance:
            violation("current_item_without_active_provenance", item_record_id, _record_id(version_by_key[key], "item_id", "version"))
        item_transitions = [
            transition
            for transition in transitions_by_item.get(item_id, [])
            if _field(transition, "item_version") == current_version
        ]
        if item_transitions:
            latest = max(
                item_transitions,
                key=lambda row: (
                    _field(row, "recorded_source_sequence", -1),
                    str(_field(row, "id", "")),
                ),
            )
            expected_status = _TRANSITION_STATUS.get(_field(latest, "transition"))
            if expected_status and _field(item, "current_status") != expected_status:
                violation("item_head_status_transition_mismatch", item_record_id, _record_id(latest, "id"))

    edge_by_id: dict[Any, Mapping[str, Any] | Any] = {}
    for edge in edges:
        from_item_id = _field(edge, "from_node_id", _field(edge, "from_item_id"))
        to_item_id = _field(edge, "to_node_id", _field(edge, "to_item_id"))
        record_id = _record_id(edge, "id", "from_node_id", "to_node_id")
        if _field(edge, "project_id") != project_id:
            violation("foreign_project_edge", record_id)
        edge_by_id[_field(edge, "id")] = edge
        from_key = (from_item_id, _field(edge, "from_version"))
        to_key = (to_item_id, _field(edge, "to_version"))
        if from_key not in version_by_key or to_key not in version_by_key:
            violation("edge_missing_endpoint_version", record_id)
        if _field(edge, "relation") not in _ALLOWED_EDGE_RELATIONS:
            violation("invalid_edge_relation", record_id)

    active_edge_provenance: set[Any] = set()
    for edge_source in edge_sources:
        edge_id = _field(edge_source, "edge_id")
        record_id = _record_id(edge_source, "edge_id", "source_id")
        edge = edge_by_id.get(edge_id)
        source = source_by_id.get(_field(edge_source, "source_id"))
        if _field(edge_source, "project_id") != project_id:
            violation("foreign_project_edge_source", record_id)
        if edge is None:
            violation("edge_source_missing_edge", record_id)
        if source is None:
            violation("edge_source_missing_source", record_id)
        elif _field(source, "project_id") != _field(edge_source, "project_id"):
            violation("edge_source_project_mismatch", record_id, _record_id(source, "id"))
        elif _field(source, "payload_state", "active") == "active":
            active_edge_provenance.add(edge_id)
    for edge_id, edge in edge_by_id.items():
        if edge_id not in active_edge_provenance:
            violation("edge_without_active_provenance", _record_id(edge, "id"))

    barrier_by_id: dict[Any, Mapping[str, Any] | Any] = {}
    for barrier in barriers:
        barrier_id = _field(barrier, "id")
        record_id = _record_id(barrier, "id")
        barrier_by_id[barrier_id] = barrier
        if _field(barrier, "project_id") != project_id:
            violation("foreign_project_barrier", record_id)
        source = source_by_id.get(_field(barrier, "correction_source_id"))
        if source is None:
            violation("barrier_missing_correction_source", record_id)
        elif _field(source, "project_id") != _field(barrier, "project_id"):
            violation("barrier_source_project_mismatch", record_id, _record_id(source, "id"))
        candidate_key = (_field(barrier, "candidate_item_id"), _field(barrier, "candidate_version"))
        if candidate_key not in version_by_key:
            violation("barrier_missing_candidate_version", record_id)
        if state is not None and _field(barrier, "context_generation", -1) > _field(state, "context_authority_generation", 0):
            violation("barrier_future_authority_generation", record_id)

    events_by_barrier: dict[Any, list[Mapping[str, Any] | Any]] = defaultdict(list)
    for event in barrier_events:
        barrier_id = _field(event, "barrier_id")
        record_id = _record_id(event, "id", "barrier_id")
        events_by_barrier[barrier_id].append(event)
        barrier = barrier_by_id.get(barrier_id)
        source = source_by_id.get(_field(event, "source_id"))
        if _field(event, "project_id") != project_id:
            violation("foreign_project_barrier_event", record_id)
        if barrier is None:
            violation("barrier_event_missing_barrier", record_id)
        elif _field(event, "project_id") != _field(barrier, "project_id"):
            violation("barrier_event_project_mismatch", record_id, _record_id(barrier, "id"))
        if source is None:
            violation("barrier_event_missing_source", record_id)
        elif _field(source, "project_id") != _field(event, "project_id"):
            violation("barrier_event_source_project_mismatch", record_id, _record_id(source, "id"))
        elif _field(source, "source_sequence") != _field(event, "recorded_source_sequence"):
            violation("barrier_event_source_sequence_mismatch", record_id, _record_id(source, "id"))
        if _field(event, "recorded_source_sequence", 0) < 1:
            violation("invalid_barrier_event_source_sequence", record_id)
        if barrier is not None and _field(event, "context_generation", -1) < _field(barrier, "context_generation", 0):
            violation("barrier_event_authority_generation_regression", record_id)
        if state is not None and _field(event, "context_generation", -1) > _field(state, "context_authority_generation", 0):
            violation("barrier_event_future_authority_generation", record_id)
    for barrier_id, barrier in barrier_by_id.items():
        ordered_events = sorted(
            events_by_barrier.get(barrier_id, []),
            key=lambda row: (
                _field(row, "recorded_source_sequence", -1),
                _field(row, "created_at"),
                str(_field(row, "id")),
            ),
        )
        prior_generation = -1
        for event in ordered_events:
            generation = _field(event, "context_generation", -1)
            if generation < prior_generation:
                violation("barrier_event_generation_nonmonotonic", _record_id(event, "id", "barrier_id"))
            prior_generation = generation
        event_state = derive_barrier_state(ordered_events)
        if event_state is None:
            violation("invalid_barrier_event_history", _record_id(barrier, "id"))

    if state is None:
        violation("missing_project_state", str(project_id))
    elif _field(state, "project_id") != project_id:
        violation("foreign_project_state", _record_id(state, "project_id"))
    elif sequences and _field(state, "source_high_watermark", max(sequences)) < max(sequences):
        violation("source_high_watermark_behind_receipts", _record_id(state, "project_id"))

    if projection is None:
        violation("missing_projection", str(project_id))
    else:
        projection_id = _record_id(projection, "project_id")
        if _field(projection, "project_id") != project_id:
            violation("foreign_project_projection", projection_id)
        ledger_revision = _field(state, "ledger_revision", 0) if state is not None else 0
        projection_revision = _field(projection, "ledger_revision")
        if not isinstance(projection_revision, int) or projection_revision < 0:
            violation("invalid_projection_revision", projection_id)
        elif projection_revision > ledger_revision:
            violation("projection_ahead_of_ledger", projection_id)
        projection_hash = _field(projection, "canonical_sha256")
        if not isinstance(projection_hash, str) or len(projection_hash) != 64:
            violation("invalid_projection_hash", projection_id)
        else:
            canonical_text = _field(projection, "canonical_text")
            if not isinstance(canonical_text, str):
                violation("missing_projection_canonical_text", projection_id)
            elif hashlib.sha256(canonical_text.encode("utf-8")).hexdigest() != projection_hash:
                violation("projection_hash_mismatch", projection_id)

    return LedgerInvariantReport(project_id=project_id, violations=tuple(violations))


async def _phase4_ledger_models() -> tuple[type[Any], ...]:
    """Resolve Phase-4 models lazily so migration/schema landing order is safe."""

    from app.models import (
        StrategicMemoryEdge,
        StrategicMemoryEdgeSource,
        StrategicMemoryCorrectionBarrier,
        StrategicMemoryCorrectionBarrierEvent,
        StrategicMemoryItem,
        StrategicMemoryItemSource,
        StrategicMemoryItemVersion,
        StrategicMemoryProjection,
        StrategicMemoryProjectState,
        StrategicMemorySource,
        StrategicMemoryTransition,
    )

    return (
        StrategicMemorySource,
        StrategicMemoryItem,
        StrategicMemoryItemVersion,
        StrategicMemoryTransition,
        StrategicMemoryItemSource,
        StrategicMemoryEdge,
        StrategicMemoryEdgeSource,
        StrategicMemoryCorrectionBarrier,
        StrategicMemoryCorrectionBarrierEvent,
        StrategicMemoryProjectState,
        StrategicMemoryProjection,
    )


async def audit_project_memory_invariants(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> LedgerInvariantReport:
    """Load one project snapshot and run the non-mutating ledger audit."""

    (
        Source,
        Item,
        ItemVersion,
        Transition,
        ItemSource,
        Edge,
        EdgeSource,
        Barrier,
        BarrierEvent,
        ProjectState,
        Projection,
    ) = await _phase4_ledger_models()
    sources = list((await session.execute(select(Source).where(Source.project_id == project_id))).scalars())
    items = list((await session.execute(select(Item).where(Item.project_id == project_id))).scalars())
    versions = list((await session.execute(select(ItemVersion).where(ItemVersion.project_id == project_id))).scalars())
    transitions = list((await session.execute(select(Transition).where(Transition.project_id == project_id))).scalars())
    item_sources = list((await session.execute(select(ItemSource).where(ItemSource.project_id == project_id))).scalars())
    edges = list((await session.execute(select(Edge).where(Edge.project_id == project_id))).scalars())
    edge_sources = list((await session.execute(select(EdgeSource).where(EdgeSource.project_id == project_id))).scalars())
    barriers = list((await session.execute(select(Barrier).where(Barrier.project_id == project_id))).scalars())
    barrier_events = list((await session.execute(select(BarrierEvent).where(BarrierEvent.project_id == project_id))).scalars())
    state = (await session.execute(select(ProjectState).where(ProjectState.project_id == project_id))).scalar_one_or_none()
    projection = (await session.execute(select(Projection).where(Projection.project_id == project_id))).scalar_one_or_none()
    return audit_ledger_records(
        project_id=project_id,
        state=state,
        sources=sources,
        items=items,
        versions=versions,
        transitions=transitions,
        item_sources=item_sources,
        edges=edges,
        edge_sources=edge_sources,
        barriers=barriers,
        barrier_events=barrier_events,
        projection=projection,
    )
