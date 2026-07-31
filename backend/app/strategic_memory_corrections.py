"""Bounded, deterministic correction barriers for Strategic memory.

This is intentionally a *pre-consolidation* guard.  It is not an extractor
and it does not decide what the corrected assertion should be.  Its only job
is to prevent a plausibly stale current item from being presented as current
between an operator correction and the later append-only ledger commit.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.strategic_memory_sources import parse_exact_spans
from app.time_utils import _utcnow


CorrectionMatchKind = Literal["exact", "reply_link", "classifier"]
CorrectionBarrierEventType = Literal[
    "opened", "target_confirmed", "resolved", "dismissed"
]

_MAX_MESSAGE_BYTES = 8_192
_MAX_CANDIDATES = 64
_MAX_MATCHES = 16
_NATURAL_CORRECTION_CUE = re.compile(
    r"(?:^|[\s,;:])(?:no|nope|actually|correction|correcting|instead|"
    r"rather|wrong)(?:[\s,;:.!]|$)|\bi\s+(?:meant|mean)\b|"
    r"\bnot\b.{0,120}\bbut\b",
    re.IGNORECASE,
)


class CorrectionBarrierError(ValueError):
    """A caller tried to create an unsafe or non-auditable barrier."""


@dataclass(frozen=True)
class CorrectionCandidate:
    """The bounded active-candidate shape needed by the synchronous detector.

    ``source_ids`` is deliberately source-receipt identity only; it is used
    for a native reply link and never for cross-project content lookup.
    """

    project_id: uuid.UUID
    item_id: uuid.UUID
    item_version: int
    current_status: str
    exact_terms: tuple[str, ...] = ()
    source_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class CorrectionMatch:
    project_id: uuid.UUID
    item_id: uuid.UUID
    item_version: int
    match_kind: CorrectionMatchKind
    match_score: float


@dataclass(frozen=True)
class CorrectionSuppression:
    """The retrieval-safe outcome of a detector run.

    All IDs here must be excluded from the current-view candidate set.  A
    single target is a deterministic stale-target suppression; multiple
    targets are intentionally ambiguous and require a clarification rather
    than selecting one based on a confidence score.
    """

    excluded_item_ids: tuple[uuid.UUID, ...]
    requires_clarification: bool


@dataclass(frozen=True)
class CorrectionDetection:
    correction_intent: bool
    matches: tuple[CorrectionMatch, ...]
    suppression: CorrectionSuppression


@dataclass(frozen=True)
class BarrierEventInput:
    """Append-only event data shared by open, resolve, and dismiss paths."""

    event_type: CorrectionBarrierEventType
    source_id: uuid.UUID
    actor_kind: str
    actor_id: str | None
    reason: str
    recorded_source_sequence: int
    context_generation: int


def _normal_form(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _message_exact_terms(content: str) -> set[str]:
    """Return bounded deterministic lookup forms from the existing parser."""

    terms: set[str] = set()
    for span in parse_exact_spans(content):
        terms.add(_normal_form(span.raw_text))
        terms.update(_normal_form(form) for form in span.normalized_forms)
    return {term for term in terms if term}


def _validate_candidate(candidate: CorrectionCandidate, project_id: uuid.UUID) -> None:
    if candidate.project_id != project_id:
        raise CorrectionBarrierError("correction candidates must belong to the project")
    if candidate.item_version < 1:
        raise CorrectionBarrierError("candidate item version must be positive")


def detect_correction(
    *,
    project_id: uuid.UUID,
    content: str,
    candidates: Sequence[CorrectionCandidate],
    reply_to_source_id: uuid.UUID | None = None,
    structured_correction: bool = False,
) -> CorrectionDetection:
    """Detect exact/reply-linked correction targets without an LLM call.

    This detector refuses unbounded input and limits its candidate set.  It
    only targets present-time ``active`` items; challenged or historical items
    are already non-current and must not be turned into a new stale claim.
    A later classifier may add ``classifier`` matches, but it must obey this
    same output/suppression contract.
    """

    if not isinstance(content, str):
        raise TypeError("correction content must be text")
    if len(content.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise CorrectionBarrierError("correction content exceeds bounded detector limit")
    if len(candidates) > _MAX_CANDIDATES:
        raise CorrectionBarrierError("correction candidate set exceeds bounded detector limit")

    for candidate in candidates:
        _validate_candidate(candidate, project_id)

    intent = structured_correction or bool(_NATURAL_CORRECTION_CUE.search(content))
    if not intent:
        return CorrectionDetection(
            correction_intent=False,
            matches=(),
            suppression=CorrectionSuppression((), False),
        )

    message_terms = _message_exact_terms(content)
    matches_by_target: dict[tuple[uuid.UUID, int], CorrectionMatch] = {}
    for candidate in candidates:
        if candidate.current_status != "active":
            continue
        target = (candidate.item_id, candidate.item_version)
        if reply_to_source_id is not None and reply_to_source_id in candidate.source_ids:
            matches_by_target[target] = CorrectionMatch(
                project_id=project_id,
                item_id=candidate.item_id,
                item_version=candidate.item_version,
                match_kind="reply_link",
                match_score=1.0,
            )
            continue
        candidate_terms = {_normal_form(term) for term in candidate.exact_terms if term.strip()}
        overlap = candidate_terms & message_terms
        if overlap:
            # Score is bounded and reproducible; it is ordering metadata, not
            # authority.  Ambiguous matches remain ambiguous regardless of it.
            score = min(1.0, len(overlap) / max(1, len(candidate_terms)))
            matches_by_target[target] = CorrectionMatch(
                project_id=project_id,
                item_id=candidate.item_id,
                item_version=candidate.item_version,
                match_kind="exact",
                match_score=score,
            )

    matches = tuple(
        sorted(
            matches_by_target.values(),
            key=lambda match: (-match.match_score, str(match.item_id), match.item_version),
        )[:_MAX_MATCHES]
    )
    implicated_items = tuple(sorted({match.item_id for match in matches}, key=str))
    return CorrectionDetection(
        correction_intent=True,
        matches=matches,
        suppression=CorrectionSuppression(
            excluded_item_ids=implicated_items,
            requires_clarification=len(implicated_items) > 1,
        ),
    )


def should_suppress_current_item(
    item_id: uuid.UUID,
    suppression: CorrectionSuppression,
) -> bool:
    """The retrieval contract: never present an implicated item as current."""

    return item_id in suppression.excluded_item_ids


def build_open_event(
    *,
    correction_source_id: uuid.UUID,
    actor_kind: str,
    actor_id: str | None,
    reason: str,
    recorded_source_sequence: int,
    context_generation: int,
) -> BarrierEventInput:
    return _build_event_input(
        event_type="opened",
        source_id=correction_source_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
        recorded_source_sequence=recorded_source_sequence,
        context_generation=context_generation,
    )


def build_resolution_event(
    *,
    event_type: Literal["target_confirmed", "resolved", "dismissed"],
    source_id: uuid.UUID,
    actor_kind: str,
    actor_id: str | None,
    reason: str,
    recorded_source_sequence: int,
    context_generation: int,
) -> BarrierEventInput:
    return _build_event_input(
        event_type=event_type,
        source_id=source_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
        recorded_source_sequence=recorded_source_sequence,
        context_generation=context_generation,
    )


def _build_event_input(
    *,
    event_type: CorrectionBarrierEventType,
    source_id: uuid.UUID,
    actor_kind: str,
    actor_id: str | None,
    reason: str,
    recorded_source_sequence: int,
    context_generation: int,
) -> BarrierEventInput:
    if not actor_kind.strip() or not reason.strip():
        raise CorrectionBarrierError("barrier events require an actor and reason")
    if recorded_source_sequence < 1 or context_generation < 0:
        raise CorrectionBarrierError("barrier event sequence/generation is invalid")
    return BarrierEventInput(
        event_type=event_type,
        source_id=source_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
        recorded_source_sequence=recorded_source_sequence,
        context_generation=context_generation,
    )


async def _phase4_barrier_models() -> tuple[type[Any], type[Any]]:
    """Resolve Phase-4 models lazily to preserve migration landing order."""

    from app.models import StrategicMemoryCorrectionBarrier, StrategicMemoryCorrectionBarrierEvent

    return StrategicMemoryCorrectionBarrier, StrategicMemoryCorrectionBarrierEvent


async def _phase4_correction_models() -> tuple[type[Any], ...]:
    """Resolve all persistence dependencies lazily in schema landing order."""

    from app.models import (
        Project,
        StrategicMemoryItem,
        StrategicMemoryItemVersion,
        StrategicMemoryProjectState,
        StrategicMemorySource,
    )

    return Project, StrategicMemoryProjectState, StrategicMemorySource, StrategicMemoryItem, StrategicMemoryItemVersion


async def _lock_project_state(
    session: AsyncSession, *, project_id: uuid.UUID
) -> tuple[Any, Any]:
    """Lock order is project, then state; barrier rows are locked later."""

    Project, ProjectState, _, _, _ = await _phase4_correction_models()
    project = (
        await session.execute(select(Project).where(Project.id == project_id).with_for_update())
    ).scalar_one_or_none()
    if project is None:
        raise CorrectionBarrierError("correction project does not exist")
    state = (
        await session.execute(
            select(ProjectState)
            .where(ProjectState.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state is None:
        raise CorrectionBarrierError("correction project state does not exist")
    return project, state


async def _require_active_source(
    session: AsyncSession, *, project_id: uuid.UUID, source_id: uuid.UUID
) -> Any:
    _, _, Source, _, _ = await _phase4_correction_models()
    source = (
        await session.execute(
            select(Source).where(Source.project_id == project_id, Source.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None or source.payload_state != "active":
        raise CorrectionBarrierError("barrier source is absent, cross-project, or unavailable")
    return source


def derive_barrier_state(events: Sequence[Any]) -> str | None:
    """Return the latest valid state or ``None`` for an invalid event stream."""

    def field(row: Any, name: str) -> Any:
        return row.get(name) if isinstance(row, Mapping) else getattr(row, name)

    state: str | None = None
    for event in sorted(
        events,
        key=lambda row: (
            field(row, "recorded_source_sequence"),
            field(row, "created_at"),
            str(field(row, "id")),
        ),
    ):
        event_type = field(event, "event_type")
        if state is None:
            if event_type != "opened":
                return None
        elif state == "opened":
            if event_type not in {"target_confirmed", "resolved", "dismissed"}:
                return None
        elif state == "target_confirmed":
            if event_type not in {"resolved", "dismissed"}:
                return None
        else:
            return None
        state = event_type
    return state


async def _locked_barrier_and_state(
    session: AsyncSession, *, project_id: uuid.UUID, barrier_id: uuid.UUID
) -> tuple[Any, Any]:
    """Apply the project -> state -> barrier lock order for event appends."""

    _, state = await _lock_project_state(session, project_id=project_id)
    Barrier, _ = await _phase4_barrier_models()
    barrier = (
        await session.execute(
            select(Barrier)
            .where(Barrier.project_id == project_id, Barrier.id == barrier_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if barrier is None:
        raise CorrectionBarrierError("barrier does not exist in this project")
    return barrier, state


async def open_correction_barriers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correction_source_id: uuid.UUID,
    matches: Iterable[CorrectionMatch],
    opened_event: BarrierEventInput,
) -> list[Any]:
    """Append one barrier and its immutable ``opened`` event per candidate.

    The caller owns the authority-generation increment, source receipt, and
    transaction; this helper locks project then state then existing barrier.
    It deliberately never commits, updates, or expires a barrier.
    """

    if opened_event.event_type != "opened" or opened_event.source_id != correction_source_id:
        raise CorrectionBarrierError("opened event must be anchored to the correction source")
    Barrier, BarrierEvent = await _phase4_barrier_models()
    _, state = await _lock_project_state(session, project_id=project_id)
    if state.context_authority_generation != opened_event.context_generation:
        raise CorrectionBarrierError("barrier open uses a stale authority generation")
    correction_source = await _require_active_source(
        session, project_id=project_id, source_id=correction_source_id
    )
    if opened_event.recorded_source_sequence != correction_source.source_sequence:
        raise CorrectionBarrierError("opened event source sequence does not match its receipt")
    match_list = list(matches)
    seen: set[tuple[uuid.UUID, int]] = set()
    barriers: list[Any] = []
    _, _, _, Item, ItemVersion = await _phase4_correction_models()
    for match in match_list:
        if match.project_id != project_id:
            raise CorrectionBarrierError("correction match crosses project boundary")
        target = (match.item_id, match.item_version)
        if target in seen:
            continue
        seen.add(target)
        existing = (
            await session.execute(
                select(Barrier)
                .where(
                    Barrier.project_id == project_id,
                    Barrier.correction_source_id == correction_source_id,
                    Barrier.candidate_item_id == match.item_id,
                    Barrier.candidate_version == match.item_version,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            barriers.append(existing)
            continue
        candidate = (
            await session.execute(
                select(Item)
                .where(Item.project_id == project_id, Item.id == match.item_id)
            )
        ).scalar_one_or_none()
        version = (
            await session.execute(
                select(ItemVersion).where(
                    ItemVersion.project_id == project_id,
                    ItemVersion.item_id == match.item_id,
                    ItemVersion.version == match.item_version,
                )
            )
        ).scalar_one_or_none()
        if (
            candidate is None
            or version is None
            or candidate.current_status != "active"
            or candidate.current_version != match.item_version
        ):
            raise CorrectionBarrierError("barrier candidate is not a current active project item")
        barrier = Barrier(
            project_id=project_id,
            correction_source_id=correction_source_id,
            candidate_item_id=match.item_id,
            candidate_version=match.item_version,
            match_kind=match.match_kind,
            match_score=match.match_score,
            context_generation=opened_event.context_generation,
            created_at=_utcnow(),
        )
        session.add(barrier)
        await session.flush()
        session.add(
            BarrierEvent(
                project_id=project_id,
                barrier_id=barrier.id,
                event_type=opened_event.event_type,
                source_id=opened_event.source_id,
                actor_kind=opened_event.actor_kind,
                actor_id=opened_event.actor_id,
                reason=opened_event.reason,
                recorded_source_sequence=opened_event.recorded_source_sequence,
                context_generation=opened_event.context_generation,
                created_at=_utcnow(),
            )
        )
        barriers.append(barrier)
    return barriers


async def append_correction_barrier_event(
    session: AsyncSession,
    *,
    barrier: Any,
    event: BarrierEventInput,
) -> Any:
    """Append, never mutate, a barrier event for the supplied barrier."""

    if event.event_type == "opened":
        raise CorrectionBarrierError("opened events are created only by the barrier-open API")
    locked_barrier, state = await _locked_barrier_and_state(
        session, project_id=barrier.project_id, barrier_id=barrier.id
    )
    if event.context_generation < locked_barrier.context_generation:
        raise CorrectionBarrierError("barrier event cannot move authority generation backward")
    if event.context_generation != state.context_authority_generation:
        raise CorrectionBarrierError("barrier event uses a stale or future authority generation")
    source = await _require_active_source(
        session, project_id=locked_barrier.project_id, source_id=event.source_id
    )
    if event.recorded_source_sequence != source.source_sequence:
        raise CorrectionBarrierError("barrier event source sequence does not match its receipt")
    _, BarrierEvent = await _phase4_barrier_models()
    events = list(
        (
            await session.execute(
                select(BarrierEvent).where(
                    BarrierEvent.project_id == locked_barrier.project_id,
                    BarrierEvent.barrier_id == locked_barrier.id,
                )
            )
        ).scalars()
    )
    current_state = derive_barrier_state(events)
    if current_state is None:
        raise CorrectionBarrierError("barrier event history is invalid")
    if current_state in {"resolved", "dismissed"}:
        raise CorrectionBarrierError("terminal barrier cannot receive further events")
    if current_state == "opened" and event.event_type not in {"target_confirmed", "resolved", "dismissed"}:
        raise CorrectionBarrierError("barrier event is not a legal state transition")
    if current_state == "target_confirmed" and event.event_type not in {"resolved", "dismissed"}:
        raise CorrectionBarrierError("barrier event is not a legal state transition")
    appended = BarrierEvent(
        project_id=locked_barrier.project_id,
        barrier_id=locked_barrier.id,
        event_type=event.event_type,
        source_id=event.source_id,
        actor_kind=event.actor_kind,
        actor_id=event.actor_id,
        reason=event.reason,
        recorded_source_sequence=event.recorded_source_sequence,
        context_generation=event.context_generation,
        created_at=_utcnow(),
    )
    session.add(appended)
    return appended


async def resolve_correction_barrier(
    session: AsyncSession,
    *,
    barrier: Any,
    event: BarrierEventInput,
) -> Any:
    """Append a final resolution event; it never updates the barrier row."""

    if event.event_type != "resolved":
        raise CorrectionBarrierError("resolution API requires a resolved event")
    return await append_correction_barrier_event(session, barrier=barrier, event=event)


async def dismiss_correction_barrier(
    session: AsyncSession,
    *,
    barrier: Any,
    event: BarrierEventInput,
) -> Any:
    """Append an audited false-positive dismissal; barriers never expire."""

    if event.event_type != "dismissed":
        raise CorrectionBarrierError("dismissal API requires a dismissed event")
    return await append_correction_barrier_event(session, barrier=barrier, event=event)


async def process_operator_message_correction_barriers(
    session: AsyncSession,
    message: Any,
) -> CorrectionDetection:
    """Open deterministic barriers for one just-flushed operator message.

    The canonical message writer calls this after its native message and
    source receipt are flushed, in the same transaction.  It does not commit.
    Non-operator messages and ordinary (no-cue) operator messages are strict
    no-ops: neither bumps the authority generation nor creates a barrier.
    """

    if getattr(message, "from_type", None) != "user":
        return CorrectionDetection(False, (), CorrectionSuppression((), False))
    # The detector itself remains strict for callers that provide an unbounded
    # payload. The normal message-write path accepts larger messages, so those
    # inputs simply opt out of synchronous correction detection.
    content = getattr(message, "content", "")
    if isinstance(content, str) and len(content.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        return CorrectionDetection(False, (), CorrectionSuppression((), False))
    project_id = message.project_id
    message_id = message.id
    _, state = await _lock_project_state(session, project_id=project_id)
    _, _, Source, Item, ItemVersion = await _phase4_correction_models()
    source = (
        await session.execute(
            select(Source).where(
                Source.project_id == project_id,
                Source.source_type == "project_message",
                Source.source_key == str(message_id),
                Source.payload_state == "active",
                Source.tombstoned_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise CorrectionBarrierError("operator message has no active source receipt")

    candidate_rows = list(
        (
            await session.execute(
                select(Item, ItemVersion)
                .join(
                    ItemVersion,
                    (ItemVersion.project_id == Item.project_id)
                    & (ItemVersion.item_id == Item.id)
                    & (ItemVersion.version == Item.current_version),
                )
                .where(Item.project_id == project_id, Item.current_status == "active")
                .order_by(Item.updated_at.desc(), Item.id)
                .limit(_MAX_CANDIDATES)
            )
        ).all()
    )
    candidate_keys = {(item.id, version.version) for item, version in candidate_rows}
    provenance_rows = []
    if candidate_keys:
        from app.models import StrategicMemoryItemSource

        provenance_rows = list(
            (
                await session.execute(
                    select(StrategicMemoryItemSource).where(
                        StrategicMemoryItemSource.project_id == project_id,
                        StrategicMemoryItemSource.item_id.in_([key[0] for key in candidate_keys]),
                    )
                )
            ).scalars()
        )
    source_ids_by_candidate: dict[tuple[uuid.UUID, int], list[uuid.UUID]] = {}
    for provenance in provenance_rows:
        key = (provenance.item_id, provenance.item_version)
        if key in candidate_keys:
            source_ids_by_candidate.setdefault(key, []).append(provenance.source_id)
    candidates = [
        CorrectionCandidate(
            project_id=project_id,
            item_id=item.id,
            item_version=version.version,
            current_status=item.current_status,
            exact_terms=tuple(version.exact_terms),
            source_ids=tuple(source_ids_by_candidate.get((item.id, version.version), [])),
        )
        for item, version in candidate_rows
    ]
    reply_to_source_id: uuid.UUID | None = None
    if getattr(message, "reply_to_message_id", None) is not None:
        reply_source = (
            await session.execute(
                select(Source.id).where(
                    Source.project_id == project_id,
                    Source.source_type == "project_message",
                    Source.source_key == str(message.reply_to_message_id),
                    Source.payload_state == "active",
                    Source.tombstoned_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        reply_to_source_id = reply_source
    detection = detect_correction(
        project_id=project_id,
        content=message.content,
        candidates=candidates,
        reply_to_source_id=reply_to_source_id,
    )
    if not detection.matches:
        return detection
    state.context_authority_generation = int(state.context_authority_generation) + 1
    session.add(state)
    await session.flush()
    opened_event = build_open_event(
        correction_source_id=source.id,
        actor_kind="operator",
        actor_id=getattr(message, "actor_user_id", None),
        reason="deterministic operator correction candidate",
        recorded_source_sequence=source.source_sequence,
        context_generation=state.context_authority_generation,
    )
    await open_correction_barriers(
        session,
        project_id=project_id,
        correction_source_id=source.id,
        matches=detection.matches,
        opened_event=opened_event,
    )
    return detection
