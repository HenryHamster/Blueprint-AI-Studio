from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.strategic_memory_corrections import (
    CorrectionBarrierError,
    CorrectionCandidate,
    build_open_event,
    build_resolution_event,
    dismiss_correction_barrier,
    detect_correction,
    open_correction_barriers,
    process_operator_message_correction_barriers,
    resolve_correction_barrier,
    should_suppress_current_item,
)
from app.models import (
    Project,
    ProjectMessage,
    StrategicMemoryCorrectionBarrier,
    StrategicMemoryItem,
    StrategicMemoryItemSource,
    StrategicMemoryItemVersion,
    StrategicMemoryProjectState,
)
from app.strategic_memory_sources import SourceReceiptInput, record_source, sha256_hex
from app.time_utils import _utcnow
from sqlmodel import select


class _OneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ManyResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self.values

    def all(self):
        return self.values


def _session_with(*results):
    session = Mock()
    session.execute = AsyncMock(side_effect=results)
    session.flush = AsyncMock()
    return session


def _candidate(
    project_id: uuid.UUID,
    *,
    item_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    exact_terms: tuple[str, ...] = ("η", "0.1"),
    status: str = "active",
) -> CorrectionCandidate:
    return CorrectionCandidate(
        project_id=project_id,
        item_id=item_id or uuid.uuid4(),
        item_version=1,
        current_status=status,
        exact_terms=exact_terms,
        source_ids=(source_id,) if source_id else (),
    )


def test_exact_natural_correction_suppresses_matching_active_item() -> None:
    project_id = uuid.uuid4()
    candidate = _candidate(project_id)

    detection = detect_correction(
        project_id=project_id,
        content="No, η was 0.2.",
        candidates=[candidate],
    )

    assert detection.correction_intent
    assert [(match.item_id, match.match_kind) for match in detection.matches] == [
        (candidate.item_id, "exact")
    ]
    assert detection.suppression.excluded_item_ids == (candidate.item_id,)
    assert not detection.suppression.requires_clarification
    assert should_suppress_current_item(candidate.item_id, detection.suppression)


def test_reply_link_is_deterministic_and_beats_exact_score() -> None:
    project_id = uuid.uuid4()
    reply_source_id = uuid.uuid4()
    candidate = _candidate(project_id, source_id=reply_source_id)

    detection = detect_correction(
        project_id=project_id,
        content="Actually, change that value.",
        candidates=[candidate],
        reply_to_source_id=reply_source_id,
    )

    assert detection.matches[0].match_kind == "reply_link"
    assert detection.matches[0].match_score == 1.0


def test_ambiguous_targets_are_all_noncurrent_and_require_clarification() -> None:
    project_id = uuid.uuid4()
    first = _candidate(project_id, exact_terms=("η", "0.1"))
    second = _candidate(project_id, exact_terms=("η", "0.3"))

    detection = detect_correction(
        project_id=project_id,
        content="Correction: η should be 0.2.",
        candidates=[first, second],
    )

    assert {match.item_id for match in detection.matches} == {first.item_id, second.item_id}
    assert set(detection.suppression.excluded_item_ids) == {first.item_id, second.item_id}
    assert detection.suppression.requires_clarification


def test_non_correction_and_nonactive_candidates_do_not_open_barriers() -> None:
    project_id = uuid.uuid4()
    active = _candidate(project_id)
    challenged = _candidate(project_id, status="challenged")

    ordinary = detect_correction(
        project_id=project_id,
        content="η was 0.2 in the last experiment.",
        candidates=[active],
    )
    stale = detect_correction(
        project_id=project_id,
        content="No, η was 0.2.",
        candidates=[challenged],
    )

    assert not ordinary.correction_intent and not ordinary.matches
    assert stale.correction_intent and not stale.matches


def test_detector_fails_closed_for_cross_project_or_unbounded_input() -> None:
    project_id = uuid.uuid4()
    other_project = uuid.uuid4()

    with pytest.raises(CorrectionBarrierError, match="project"):
        detect_correction(
            project_id=project_id,
            content="No, η was 0.2.",
            candidates=[_candidate(other_project)],
        )
    with pytest.raises(CorrectionBarrierError, match="bounded"):
        detect_correction(
            project_id=project_id,
            content="x" * 8_193,
            candidates=[],
        )


def test_events_carry_source_actor_sequence_and_generation() -> None:
    source_id = uuid.uuid4()
    opened = build_open_event(
        correction_source_id=source_id,
        actor_kind="operator",
        actor_id="user-1",
        reason="natural correction",
        recorded_source_sequence=8,
        context_generation=3,
    )
    resolved = build_resolution_event(
        event_type="resolved",
        source_id=source_id,
        actor_kind="consolidator",
        actor_id="job-1",
        reason="version transition appended",
        recorded_source_sequence=9,
        context_generation=3,
    )

    assert opened.event_type == "opened"
    assert opened.source_id == source_id
    assert (opened.recorded_source_sequence, opened.context_generation) == (8, 3)
    assert resolved.event_type == "resolved"
    assert resolved.actor_kind == "consolidator"


@pytest.mark.asyncio
async def test_open_barrier_persists_project_scoped_append_only_event() -> None:
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    candidate = _candidate(project_id)
    detection = detect_correction(
        project_id=project_id,
        content="No, η was 0.2.",
        candidates=[candidate],
    )
    opened = build_open_event(
        correction_source_id=source_id,
        actor_kind="operator",
        actor_id="user-1",
        reason="natural correction",
        recorded_source_sequence=8,
        context_generation=3,
    )
    session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=3)),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=8)),
        _OneResult(None),
        _OneResult(SimpleNamespace(id=candidate.item_id, current_status="active", current_version=1)),
        _OneResult(SimpleNamespace(item_id=candidate.item_id, version=1)),
    )

    barriers = await open_correction_barriers(
        session,
        project_id=project_id,
        correction_source_id=source_id,
        matches=detection.matches,
        opened_event=opened,
    )

    assert len(barriers) == 1
    event = session.add.call_args_list[-1].args[0]
    assert event.project_id == project_id
    assert event.source_id == source_id
    assert (event.event_type, event.recorded_source_sequence, event.context_generation) == (
        "opened", 8, 3
    )


@pytest.mark.asyncio
async def test_resolution_and_dismissal_only_append_their_matching_event() -> None:
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    barrier = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, context_generation=4)
    resolved = build_resolution_event(
        event_type="resolved", source_id=source_id, actor_kind="consolidator", actor_id="job",
        reason="ledger transition appended", recorded_source_sequence=9, context_generation=4,
    )
    dismissed = build_resolution_event(
        event_type="dismissed", source_id=source_id, actor_kind="operator", actor_id="user",
        reason="false positive", recorded_source_sequence=10, context_generation=4,
    )

    opened_row = SimpleNamespace(
        id=uuid.uuid4(), event_type="opened", recorded_source_sequence=8,
        created_at=datetime.now(timezone.utc),
    )
    resolved_session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=4)),
        _OneResult(barrier),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=9)),
        _ManyResult([opened_row]),
    )
    dismissed_session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=4)),
        _OneResult(barrier),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=10)),
        _ManyResult([opened_row]),
    )

    resolved_event = await resolve_correction_barrier(resolved_session, barrier=barrier, event=resolved)
    dismissed_event = await dismiss_correction_barrier(dismissed_session, barrier=barrier, event=dismissed)

    assert (resolved_event.event_type, dismissed_event.event_type) == ("resolved", "dismissed")
    assert resolved_session.add.call_args.args[0].event_type == "resolved"
    assert dismissed_session.add.call_args.args[0].event_type == "dismissed"


@pytest.mark.asyncio
async def test_stale_generation_and_terminal_barrier_fail_closed() -> None:
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    candidate = _candidate(project_id)
    stale_open = build_open_event(
        correction_source_id=source_id, actor_kind="operator", actor_id="user", reason="correction",
        recorded_source_sequence=8, context_generation=3,
    )
    session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=4)),
    )
    with pytest.raises(CorrectionBarrierError, match="stale authority"):
        await open_correction_barriers(
            session, project_id=project_id, correction_source_id=source_id,
            matches=[
                type("Match", (), {
                    "project_id": project_id, "item_id": candidate.item_id, "item_version": 1,
                    "match_kind": "exact", "match_score": 1.0,
                })()
            ],
            opened_event=stale_open,
        )

    barrier = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, context_generation=4)
    terminal = SimpleNamespace(
        id=uuid.uuid4(), event_type="resolved", recorded_source_sequence=8,
        created_at=datetime.now(timezone.utc),
    )
    terminal_session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=4)),
        _OneResult(barrier),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=9)),
        _ManyResult([SimpleNamespace(
            id=uuid.uuid4(), event_type="opened", recorded_source_sequence=7,
            created_at=datetime.now(timezone.utc),
        ), terminal]),
    )
    dismissal = build_resolution_event(
        event_type="dismissed", source_id=source_id, actor_kind="operator", actor_id="user",
        reason="late dismissal", recorded_source_sequence=9, context_generation=4,
    )
    with pytest.raises(CorrectionBarrierError, match="terminal"):
        await dismiss_correction_barrier(terminal_session, barrier=barrier, event=dismissal)


@pytest.mark.asyncio
async def test_event_must_use_the_current_authority_generation() -> None:
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    barrier = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, context_generation=3)
    stale_event = build_resolution_event(
        event_type="resolved", source_id=source_id, actor_kind="consolidator", actor_id="job",
        reason="stale completion", recorded_source_sequence=9, context_generation=3,
    )
    session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=4)),
        _OneResult(barrier),
    )

    with pytest.raises(CorrectionBarrierError, match="stale or future"):
        await resolve_correction_barrier(session, barrier=barrier, event=stale_event)


@pytest.mark.asyncio
async def test_barrier_events_must_use_their_source_receipt_sequence() -> None:
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    candidate = _candidate(project_id)
    opened = build_open_event(
        correction_source_id=source_id, actor_kind="operator", actor_id="user", reason="correction",
        recorded_source_sequence=8, context_generation=3,
    )
    open_session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=3)),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=9)),
    )
    with pytest.raises(CorrectionBarrierError, match="source sequence"):
        await open_correction_barriers(
            open_session, project_id=project_id, correction_source_id=source_id,
            matches=[type("Match", (), {
                "project_id": project_id, "item_id": candidate.item_id, "item_version": 1,
                "match_kind": "exact", "match_score": 1.0,
            })()],
            opened_event=opened,
        )

    barrier = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, context_generation=3)
    resolution = build_resolution_event(
        event_type="resolved", source_id=source_id, actor_kind="consolidator", actor_id="job",
        reason="complete", recorded_source_sequence=8, context_generation=3,
    )
    append_session = _session_with(
        _OneResult(SimpleNamespace(id=project_id)),
        _OneResult(SimpleNamespace(context_authority_generation=3)),
        _OneResult(barrier),
        _OneResult(SimpleNamespace(id=source_id, payload_state="active", source_sequence=9)),
    )
    with pytest.raises(CorrectionBarrierError, match="source sequence"):
        await resolve_correction_barrier(append_session, barrier=barrier, event=resolution)


@pytest.mark.asyncio
async def test_operator_message_helper_opens_exact_barrier_and_bumps_generation() -> None:
    project_id = uuid.uuid4()
    message_id = uuid.uuid4()
    source_id = uuid.uuid4()
    item_id = uuid.uuid4()
    state = SimpleNamespace(context_authority_generation=2)
    source = SimpleNamespace(id=source_id, source_sequence=8)
    item = SimpleNamespace(id=item_id, current_status="active")
    version = SimpleNamespace(version=1, exact_terms=["η", "0.1"])
    session = _session_with(
        _OneResult(source), _ManyResult([(item, version)]),
        _ManyResult([SimpleNamespace(item_id=item_id, item_version=1, source_id=uuid.uuid4())]),
    )
    message = SimpleNamespace(
        id=message_id, project_id=project_id, from_type="user", actor_user_id="operator",
        reply_to_message_id=None, content="No, η was 0.2.",
    )
    with patch(
        "app.strategic_memory_corrections._lock_project_state",
        new=AsyncMock(return_value=(SimpleNamespace(id=project_id), state)),
    ), patch(
        "app.strategic_memory_corrections.open_correction_barriers", new=AsyncMock()
    ) as open_barriers:
        detection = await process_operator_message_correction_barriers(session, message)

    assert detection.matches[0].match_kind == "exact"
    assert state.context_authority_generation == 3
    assert open_barriers.await_args.kwargs["opened_event"].recorded_source_sequence == 8


@pytest.mark.asyncio
async def test_operator_message_helper_uses_reply_receipt_and_no_cue_is_a_noop() -> None:
    project_id = uuid.uuid4()
    message_id = uuid.uuid4()
    reply_message_id = uuid.uuid4()
    source_id = uuid.uuid4()
    reply_source_id = uuid.uuid4()
    item_id = uuid.uuid4()
    source = SimpleNamespace(id=source_id, source_sequence=8)
    item = SimpleNamespace(id=item_id, current_status="active")
    version = SimpleNamespace(version=1, exact_terms=[])
    provenance = SimpleNamespace(item_id=item_id, item_version=1, source_id=reply_source_id)
    state = SimpleNamespace(context_authority_generation=2)
    reply_session = _session_with(
        _OneResult(source), _ManyResult([(item, version)]), _ManyResult([provenance]),
        _OneResult(reply_source_id),
    )
    reply_message = SimpleNamespace(
        id=message_id, project_id=project_id, from_type="user", actor_user_id="operator",
        reply_to_message_id=reply_message_id, content="Actually, change that.",
    )
    with patch(
        "app.strategic_memory_corrections._lock_project_state",
        new=AsyncMock(return_value=(SimpleNamespace(id=project_id), state)),
    ), patch(
        "app.strategic_memory_corrections.open_correction_barriers", new=AsyncMock()
    ) as open_barriers:
        detection = await process_operator_message_correction_barriers(reply_session, reply_message)
    assert detection.matches[0].match_kind == "reply_link"
    assert open_barriers.await_count == 1

    no_cue_state = SimpleNamespace(context_authority_generation=2)
    no_cue_session = _session_with(
        _OneResult(source), _ManyResult([(item, version)]), _ManyResult([provenance]),
    )
    no_cue_message = SimpleNamespace(
        id=uuid.uuid4(), project_id=project_id, from_type="user", actor_user_id="operator",
        reply_to_message_id=None, content="η was 0.2 in the last run.",
    )
    with patch(
        "app.strategic_memory_corrections._lock_project_state",
        new=AsyncMock(return_value=(SimpleNamespace(id=project_id), no_cue_state)),
    ), patch(
        "app.strategic_memory_corrections.open_correction_barriers", new=AsyncMock()
    ) as no_open:
        detection = await process_operator_message_correction_barriers(no_cue_session, no_cue_message)
    assert not detection.matches
    assert no_cue_state.context_authority_generation == 2
    no_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_message_helper_real_database_path(db_session) -> None:
    project = Project(name="correction-barrier", user_id="correction-barrier-user")
    db_session.add(project)
    await db_session.flush()
    original = ProjectMessage(project_id=project.id, from_type="user", content="eta was 0.1")
    db_session.add(original)
    await db_session.flush()
    original_receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id, source_type="project_message", source_key=str(original.id),
            source_revision="1", content_hash=sha256_hex(original.content), occurred_at=_utcnow(),
            actor_kind="user", actor_id="operator", native_sequence=1,
        ),
    )
    item = StrategicMemoryItem(
        project_id=project.id, scope_type="project", scope_key="", kind="notation",
        logical_key="eta", current_version=1, current_status="active",
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(
        StrategicMemoryItemVersion(
            item_id=item.id, version=1, project_id=project.id, canonical_text="eta was 0.1",
            structured_value={}, epistemic_status="operator_explicit", confidence=1,
            recorded_source_sequence=original_receipt.source.source_sequence, exact_terms=["η", "0.1"],
            formula_fingerprints=[], content_hash=sha256_hex("eta was 0.1"), extractor_version="test",
        )
    )
    # ItemSource's composite FK targets ItemVersion. The ledger models do not
    # use an ORM relationship for that edge, so flush the referenced version
    # before staging its provenance row (required on PostgreSQL).
    await db_session.flush()
    db_session.add(
        StrategicMemoryItemSource(
            project_id=project.id, item_id=item.id, item_version=1,
            source_id=original_receipt.source.id, source_role="assertion", span_index=0,
            span_kind="text", span_hash=original_receipt.source.content_hash,
        )
    )
    correction = ProjectMessage(project_id=project.id, from_type="user", content="No, η was 0.2.")
    db_session.add(correction)
    await db_session.flush()
    correction_receipt = await record_source(
        db_session,
        SourceReceiptInput(
            project_id=project.id, source_type="project_message", source_key=str(correction.id),
            source_revision="1", content_hash=sha256_hex(correction.content), occurred_at=_utcnow(),
            actor_kind="user", actor_id="operator", native_sequence=2,
        ),
    )

    detection = await process_operator_message_correction_barriers(db_session, correction)

    assert detection.matches and detection.matches[0].item_id == item.id
    barrier = await db_session.scalar(
        select(StrategicMemoryCorrectionBarrier).where(
            StrategicMemoryCorrectionBarrier.correction_source_id == correction_receipt.source.id
        )
    )
    state = await db_session.get(StrategicMemoryProjectState, project.id)
    assert barrier is not None
    assert state is not None and state.context_authority_generation == barrier.context_generation
