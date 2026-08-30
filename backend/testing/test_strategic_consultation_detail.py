"""Operator read path for private Workday consultations.

These tests pin the two properties that make the feature safe to ship:

1. The projection reports the real lifecycle position of a sub-session --
   including the running/stalled distinction, which is invisible to anything
   that only reads ``handling_state``.
2. It does NOT achieve visibility by moving the packet or its result into the
   operator audience.  Both rows stay ``strategist-internal``; a diff that
   "fixes" visibility by flipping the audience map would dump coordination
   noise into the thread and is explicitly guarded against here.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.message_audience import STRATEGIST_INTERNAL, route_event
from app.models import (
    MessageHandlingState,
    MessageSurface,
    Project,
    ProjectMember,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.strategic_consultation_detail import (
    CONSULTATION_PREVIEW_MAX_CHARS,
    CONSULTATION_QUESTION_MAX_CHARS,
    CONSULTATION_SUMMARY_MAX_CHARS,
    project_consultation_detail,
    project_consultation_index,
)
from app.strategic_workday_mutation_rebind import (
    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
    WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
)
from app.time_utils import _utcnow


TEST_USER_ID = "test-user-123"
OTHER_USER_ID = "someone-else-456"
SESSION_ID = "resident-session"


# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_project(session, *, user_id: str = TEST_USER_ID) -> Project:
    project = Project(name="consultation-detail", user_id=user_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=user_id, role="viewer")
    )
    await session.commit()
    return project


async def _make_worker_task(session, project: Project) -> Task:
    """A real worker row: handled_by_task_id is FK-bound to the same project."""
    task = Task(
        project_id=project.id,
        payloads=[{}],
        status=TaskStatus.IN_PROGRESS,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _public_request(project_id, *, sequence: int = 2, **context_overrides):
    return ProjectMessage(
        id=uuid.uuid4(),
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="strategic",
        content="I asked Workday to look into that.",
        resident_session_id=SESSION_ID,
        context={
            "message_role": "operator_reply",
            "_workday_consultation_requested": True,
            "_conversation_public_reply_owner": "fast",
            **context_overrides,
        },
    )


def _packet(project_id, consultation_id, *, sequence: int = 3, content="why is the fleet idle?", **overrides):
    context = {
        "audience": STRATEGIST_INTERNAL,
        "message_role": "workday_consultation",
        "public_reply_owner": "fast",
        "_conversation_consultation_packet": True,
        "_conversation_consultation_continuation": "initial",
        "_workday_consultation_atomic_reply_id": str(consultation_id),
    }
    context.update(overrides.pop("context", {}))
    return ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content=content,
        resident_session_id=SESSION_ID,
        status="open",
        handling_state=MessageHandlingState.PENDING,
        context=context,
        **overrides,
    )


def _result(project_id, consultation_id, *, sequence: int = 4, content="Four directions were stranded.", **overrides):
    context = {
        "audience": STRATEGIST_INTERNAL,
        "message_role": "workday_consultation_result",
        "public_reply_owner": "fast",
        "_workday_consultation_result": True,
        "result_state": "completed",
        "_workday_consultation_atomic_reply_id": str(consultation_id),
    }
    context.update(overrides.pop("context", {}))
    return ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="system",
        content=content,
        resident_session_id=SESSION_ID,
        status="open",
        handling_state=MessageHandlingState.PENDING,
        context=context,
        **overrides,
    )


def _continuation(project_id, consultation_id, *, sequence: int = 5):
    return ProjectMessage(
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        from_type="strategic",
        content="Workday found four stranded directions.",
        resident_session_id=SESSION_ID,
        handling_state=MessageHandlingState.RESPONDED,
        context={
            "message_role": "workday_consultation_continuation",
            "_workday_result_packet_id": str(uuid.uuid4()),
            "_workday_consultation_atomic_reply_id": str(consultation_id),
        },
    )


def _live_claim_context(*, expires_in_seconds: int = 300) -> dict:
    return {
        "_workday_consultation_claim_id": str(uuid.uuid4()),
        "_workday_consultation_claim_generation": 2,
        "_workday_consultation_claim_incarnation": str(uuid.uuid4()),
        "_workday_consultation_claim_expires_at": (
            _utcnow() + timedelta(seconds=expires_in_seconds)
        ).isoformat(),
        "_workday_consultation_claim_attempt": 1,
    }


def _ledger_entry(op: str, entity_id: str, *, effect: str = "created") -> dict:
    """A durable operation receipt shaped exactly as the rebind writer emits it."""
    return {
        "version": 1,
        "packet_id": str(uuid.uuid4()),
        "workday_claim_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "fast_claim_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "worker_incarnation": str(uuid.uuid4()),
        "envelope_snapshot_id": str(uuid.uuid4()),
        "resident_session_id": SESSION_ID,
        "worker_generation": 2,
        "context_authority_generation_before": 4,
        "context_authority_generation_after": 5,
        "privacy_generation": 1,
        "op": op,
        "operation_fingerprint": "a" * 64,
        "recorded_at": _utcnow().isoformat(),
        "entity_kind": "direction",
        "entity_id": entity_id,
        "effect": effect,
        "accepted_at": _utcnow().isoformat(),
    }


def _attempt(
    attempt: int,
    outcome: str,
    predicate: str,
    *,
    output_tail: str | None = None,
) -> dict:
    started = _utcnow() - timedelta(seconds=attempt)
    return {
        "attempt": attempt,
        "workday_claim_id": str(uuid.uuid4()),
        "outcome": outcome,
        "terminal_predicate": predicate,
        "provider_terminal_reason": "aborted_streaming",
        "provider_output_tail": output_tail,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(milliseconds=250)).isoformat(),
        "turn_ms": 250,
        "ttft_ms": 20,
        "tool_call_count": attempt,
        "tool_call_duration_ms": 100,
        "result_chars": 0,
        "trace": [
            {"elapsed_ms": 10, "kind": "tool", "label": "Read"},
            {"elapsed_ms": 250, "kind": "result", "label": predicate},
        ],
        "trace_truncated": False,
    }


# ── lifecycle ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_walks_the_whole_consultation_lifecycle(db_session):
    """One consultation, read at every phase it actually passes through."""
    project = await _make_project(db_session)
    request = _public_request(project.id)
    db_session.add(request)
    await db_session.commit()

    async def read():
        return await project_consultation_detail(
            db_session, project_id=project.id, consultation_id=request.id
        )

    detail = await read()
    assert detail["state"] == "requested"
    assert detail["question"] is None

    packet = _packet(project.id, request.id)
    db_session.add(packet)
    await db_session.commit()
    detail = await read()
    assert detail["state"] == "queued"
    assert detail["question"]["text"] == "why is the fleet idle?"
    assert detail["continuation_key"] == "initial"

    worker = await _make_worker_task(db_session, project)
    packet.handling_state = MessageHandlingState.CLAIMED
    packet.handled_by_task_id = worker.id
    packet.context = {**packet.context, **_live_claim_context()}
    db_session.add(packet)
    await db_session.commit()
    detail = await read()
    assert detail["state"] == "running"
    assert detail["claim"]["expired"] is False
    assert detail["claim"]["attempt"] == 1
    assert detail["claim"]["generation"] == 2

    # A dead lease is the difference between "working" and "nobody is holding
    # this" -- handling_state alone reads CLAIMED for both.
    packet.context = {
        **packet.context,
        **_live_claim_context(expires_in_seconds=-30),
    }
    db_session.add(packet)
    await db_session.commit()
    detail = await read()
    assert detail["state"] == "stalled"
    assert detail["claim"]["expired"] is True

    packet.handling_state = MessageHandlingState.RESPONDED
    packet.status = "handled"
    db_session.add(packet)
    db_session.add(_result(project.id, request.id))
    await db_session.commit()
    detail = await read()
    assert detail["state"] == "delivering"
    assert detail["outcome"] == "completed"
    assert detail["result"]["summary"]["text"] == "Four directions were stranded."
    assert detail["delivered_at"] is None

    db_session.add(_continuation(project.id, request.id))
    await db_session.commit()
    detail = await read()
    assert detail["state"] == "delivered"
    assert detail["delivered_at"] is not None


@pytest.mark.asyncio
async def test_terminal_packet_without_a_result_is_named_not_guessed(db_session):
    """A parked packet must not read as 'queued' or 'running'."""
    project = await _make_project(db_session)
    request = _public_request(project.id)
    packet = _packet(project.id, request.id)
    packet.handling_state = MessageHandlingState.RESPONDED
    packet.status = "handled"
    db_session.add_all([request, packet])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert detail["state"] == "closed_without_result"
    assert detail["result"] is None


@pytest.mark.asyncio
async def test_failed_outcome_is_reported_alongside_delivery_position(db_session):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    packet = _packet(project.id, request.id)
    packet.handling_state = MessageHandlingState.RESPONDED
    packet.status = "handled"
    result = _result(
        project.id,
        request.id,
        content="The consultation could not produce a usable result.",
        context={"result_state": "failed"},
    )
    db_session.add_all([request, packet, result])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    # Position and outcome are separate readings: a failure still has to travel
    # back to the operator, and collapsing them hides whether it arrived.
    assert detail["state"] == "delivering"
    assert detail["outcome"] == "failed"


@pytest.mark.asyncio
async def test_every_attempt_and_terminal_predicate_survive_failed_delivery(db_session):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    attempts = [
        _attempt(1, "abandoned", "consultation_inactivity_timeout"),
        _attempt(
            2,
            "failed",
            "provider_failed",
            output_tail="terminal_reason=aborted_streaming",
        ),
    ]
    packet = _packet(
        project.id,
        request.id,
        context={"_workday_consultation_attempts": attempts},
    )
    packet.handling_state = MessageHandlingState.RESPONDED
    packet.status = "handled"
    result = _result(
        project.id,
        request.id,
        content="Generic bounded failure summary.",
        context={
            "result_state": "failed",
            "_workday_consultation_attempts": attempts,
        },
    )
    db_session.add_all([request, packet, result])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert [item["attempt"] for item in detail["attempts"]] == [1, 2]
    assert detail["attempts"][0]["terminal_predicate"] == (
        "consultation_inactivity_timeout"
    )
    assert detail["attempts"][1]["trace"][-1]["label"] == "provider_failed"
    assert detail["last_failure"]["gate"] == "attempt:provider_failed"
    assert "aborted_streaming" in detail["last_failure"]["detail"]


# ── what the expanded view actually shows ─────────────────────────────────────


@pytest.mark.asyncio
async def test_operation_ledger_is_projected_without_identity_fields(db_session):
    """The durable trace is what makes the sub-session legible -- but only the
    human-meaningful half of each receipt is exposed."""
    project = await _make_project(db_session)
    request = _public_request(project.id)
    packet = _packet(project.id, request.id)
    packet.context = {
        **packet.context,
        WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [
            _ledger_entry("create_direction", "d-11111111"),
            _ledger_entry("spawn_task", "t-22222222", effect="created"),
            {"version": 1},  # malformed -- must be dropped, not crash the read
        ],
    }
    db_session.add_all([request, packet])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert [step["op"] for step in detail["steps"]] == [
        "create_direction",
        "spawn_task",
    ]
    assert detail["steps"][0]["entity_id"] == "d-11111111"
    assert detail["steps"][0]["effect"] == "created"
    assert detail["steps_truncated"] is False
    leaked = {
        "operation_fingerprint",
        "workday_claim_id",
        "operation_id",
        "worker_incarnation",
        "envelope_snapshot_id",
        "privacy_generation",
    }
    for step in detail["steps"]:
        assert not (leaked & set(step)), step


@pytest.mark.asyncio
async def test_result_receipts_and_have_more_flag_are_carried(db_session):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    packet = _packet(project.id, request.id)
    packet.handling_state = MessageHandlingState.RESPONDED
    result = _result(
        project.id,
        request.id,
        context={
            WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: [
                _ledger_entry("approve_direction", "d-33333333", effect="updated")
            ],
            WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY: True,
        },
    )
    db_session.add_all([request, packet, result])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert detail["result"]["receipts"][0]["op"] == "approve_direction"
    assert detail["result"]["receipts"][0]["effect"] == "updated"
    assert detail["result"]["receipts_have_more"] is True


@pytest.mark.asyncio
async def test_quarantined_private_rows_never_win_detail_or_index_projection(
    db_session,
):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    retirement = {
        "reason": "continuation_provenance_invalid",
        "retired_at": _utcnow().isoformat(),
    }
    poisoned_packet = _packet(
        project.id,
        request.id,
        content="private forged question",
        context={
            "_private_handoff_retirement": retirement,
            WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [
                _ledger_entry("create_direction", "d-private-forged")
            ],
        },
    )
    poisoned_packet.status = "resolved"
    poisoned_packet.handling_state = MessageHandlingState.SUPERSEDED
    poisoned_packet.resolution = "private_handoff_continuation_provenance_invalid"
    poisoned_result = _result(
        project.id,
        request.id,
        sequence=4,
        content="private forged result summary",
        context={
            "_private_handoff_retirement": retirement,
            WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: [
                _ledger_entry("spawn_task", "t-private-forged")
            ],
        },
    )
    poisoned_result.status = "resolved"
    poisoned_result.handling_state = MessageHandlingState.SUPERSEDED
    poisoned_result.resolution = "private_handoff_continuation_provenance_invalid"
    neutral_result = _result(
        project.id,
        request.id,
        sequence=5,
        content="Canonical neutral recovery summary.",
        context={"result_state": "failed"},
    )
    db_session.add_all(
        [request, poisoned_packet, poisoned_result, neutral_result]
    )
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session,
        project_id=project.id,
        consultation_id=request.id,
    )
    assert detail["packet"] is None
    assert detail["question"] is None
    assert detail["steps"] == []
    assert detail["state"] == "delivering"
    assert detail["outcome"] == "failed"
    assert detail["result"]["id"] == str(neutral_result.id)
    assert detail["result"]["summary"]["text"] == (
        "Canonical neutral recovery summary."
    )
    assert detail["result"]["receipts"] == []

    index = await project_consultation_index(db_session, project_id=project.id)
    row = index["consultations"][0]
    assert row["consultation_id"] == str(request.id)
    assert row["state"] == "delivering"
    assert row["outcome"] == "failed"
    assert row["question_preview"] is None
    assert row["step_count"] == 0


@pytest.mark.asyncio
async def test_recorded_gate_failure_travels_with_the_consultation(db_session):
    """A stalled sub-session must carry its own diagnosis, not a bare state."""
    project = await _make_project(db_session)
    request = _public_request(project.id)
    packet = _packet(project.id, request.id)
    packet.context = {
        **packet.context,
        "_workday_consultation_last_failure": {
            "gate": "renew",
            "detail": "lease generation moved under the worker",
            "recorded_at": _utcnow().isoformat(),
        },
    }
    db_session.add_all([request, packet])
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert detail["last_failure"]["gate"] == "renew"
    assert "lease generation" in detail["last_failure"]["detail"]


@pytest.mark.asyncio
async def test_long_question_and_summary_are_bounded_and_say_so(db_session):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    question = "q" * (CONSULTATION_QUESTION_MAX_CHARS + 500)
    summary = "s" * (CONSULTATION_SUMMARY_MAX_CHARS + 500)
    packet = _packet(project.id, request.id, content=question)
    packet.handling_state = MessageHandlingState.RESPONDED
    db_session.add_all(
        [request, packet, _result(project.id, request.id, content=summary)]
    )
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    # Literal pins: these bounds sit on an operator read path and must not be
    # widened by editing the constant alone.
    assert CONSULTATION_QUESTION_MAX_CHARS == 4000
    assert CONSULTATION_SUMMARY_MAX_CHARS == 8000
    assert len(detail["question"]["text"]) == CONSULTATION_QUESTION_MAX_CHARS
    assert detail["question"]["truncated"] is True
    assert detail["question"]["chars"] == len(question)
    assert len(detail["result"]["summary"]["text"]) == CONSULTATION_SUMMARY_MAX_CHARS
    assert detail["result"]["summary"]["truncated"] is True


# ── access boundary ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_refuses_ids_the_operator_thread_does_not_refer_to(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session, user_id=OTHER_USER_ID)

    # Unknown id.
    assert (
        await project_consultation_detail(
            db_session, project_id=project.id, consultation_id=uuid.uuid4()
        )
        is None
    )

    # Another project's consultation, read through this project.
    foreign = _public_request(other.id)
    db_session.add(foreign)
    await db_session.commit()
    assert (
        await project_consultation_detail(
            db_session, project_id=project.id, consultation_id=foreign.id
        )
        is None
    )

    # An internal row is not a consultation handle, even with the right marker.
    internal = _public_request(project.id, audience=STRATEGIST_INTERNAL)
    db_session.add(internal)
    await db_session.commit()
    assert (
        await project_consultation_detail(
            db_session, project_id=project.id, consultation_id=internal.id
        )
        is None
    )

    # A redacted request stops being expandable.
    redacted = _public_request(project.id, privacy_state="redacted")
    db_session.add(redacted)
    await db_session.commit()
    assert (
        await project_consultation_detail(
            db_session, project_id=project.id, consultation_id=redacted.id
        )
        is None
    )


@pytest.mark.asyncio
async def test_related_rows_are_scoped_to_the_reading_project(db_session):
    """A packet in another project must never attach to this consultation."""
    project = await _make_project(db_session)
    other = await _make_project(db_session, user_id=OTHER_USER_ID)
    request = _public_request(project.id)
    db_session.add(request)
    await db_session.commit()
    db_session.add(_packet(other.id, request.id))
    await db_session.commit()

    detail = await project_consultation_detail(
        db_session, project_id=project.id, consultation_id=request.id
    )
    assert detail["state"] == "requested"
    assert detail["packet"] is None


# ── index ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_returns_one_collapsed_row_per_consultation(db_session):
    project = await _make_project(db_session)
    first = _public_request(project.id, sequence=2)
    second = _public_request(project.id, sequence=6)
    db_session.add_all([first, second])
    await db_session.commit()
    worker = await _make_worker_task(db_session, project)
    first_packet = _packet(project.id, first.id, sequence=3, content="first question")
    first_packet.handling_state = MessageHandlingState.CLAIMED
    first_packet.handled_by_task_id = worker.id
    first_packet.context = {
        **first_packet.context,
        **_live_claim_context(),
        WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [
            _ledger_entry("create_direction", "d-44444444")
        ],
    }
    db_session.add_all(
        [first_packet, _packet(project.id, second.id, sequence=7, content="second")]
    )
    await db_session.commit()

    index = await project_consultation_index(db_session, project_id=project.id)
    rows = {row["consultation_id"]: row for row in index["consultations"]}
    assert set(rows) == {str(first.id), str(second.id)}
    assert rows[str(first.id)]["state"] == "running"
    assert rows[str(first.id)]["step_count"] == 1
    assert rows[str(first.id)]["question_preview"]["text"] == "first question"
    assert rows[str(second.id)]["state"] == "queued"
    assert rows[str(second.id)]["step_count"] == 0
    # Newest first, so the collapsed list leads with the live sub-session.
    assert index["consultations"][0]["consultation_id"] == str(second.id)


@pytest.mark.asyncio
async def test_index_preview_is_shorter_than_the_expanded_question(db_session):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    db_session.add(request)
    await db_session.commit()
    db_session.add(
        _packet(project.id, request.id, content="p" * (CONSULTATION_PREVIEW_MAX_CHARS + 100))
    )
    await db_session.commit()

    index = await project_consultation_index(db_session, project_id=project.id)
    preview = index["consultations"][0]["question_preview"]
    assert CONSULTATION_PREVIEW_MAX_CHARS == 240
    assert len(preview["text"]) == CONSULTATION_PREVIEW_MAX_CHARS
    assert preview["truncated"] is True


@pytest.mark.asyncio
async def test_index_ignores_projects_and_rows_that_are_not_requests(db_session):
    project = await _make_project(db_session)
    other = await _make_project(db_session, user_id=OTHER_USER_ID)
    db_session.add_all(
        [
            _public_request(other.id),
            _public_request(project.id, audience=STRATEGIST_INTERNAL),
            ProjectMessage(
                project_id=project.id,
                sequence=9,
                surface=MessageSurface.CONVERSATION,
                from_type="strategic",
                content="an ordinary reply",
                context={"message_role": "operator_reply"},
            ),
        ]
    )
    await db_session.commit()

    index = await project_consultation_index(db_session, project_id=project.id)
    assert index["consultations"] == []


# ── the audience must not be what changed ─────────────────────────────────────


def test_consultation_rows_remain_strategist_internal():
    """This feature is a read path, not an audience change.

    If a future diff makes consultations visible by re-routing these roles into
    the operator audience, the packets land in the thread as noise and this
    module stops being the thing that gates the read.  Literal pin, on purpose.
    """
    # Called exactly as stamp_message_audience calls it at both write sites.
    assert (
        route_event("workday_consultation", message_type="NOTE")
        == STRATEGIST_INTERNAL
    )
    assert (
        route_event("workday_consultation_result", message_type="NOTE")
        == STRATEGIST_INTERNAL
    )


# ── HTTP surface ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoints_serve_the_projection_and_404_unknown_ids(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    request = _public_request(project.id)
    attempt = _attempt(
        1,
        "failed",
        "provider_failed",
        output_tail="terminal_reason=aborted_streaming",
    )
    db_session.add(request)
    await db_session.commit()
    db_session.add(
        _packet(
            project.id,
            request.id,
            context={"_workday_consultation_attempts": [attempt]},
        )
    )
    await db_session.commit()

    listing = await client.get(
        f"/api/admin/projects/{project.id}/strategic-session/consultations"
    )
    assert listing.status_code == 200
    assert listing.json()["consultations"][0]["consultation_id"] == str(request.id)

    detail = await client.get(
        f"/api/admin/projects/{project.id}"
        f"/strategic-session/consultations/{request.id}"
    )
    assert detail.status_code == 200
    assert detail.json()["state"] == "queued"
    serialized_attempt = detail.json()["attempts"][0]
    assert serialized_attempt["attempt"] == 1
    assert serialized_attempt["workday_claim_id"] == attempt["workday_claim_id"]
    assert serialized_attempt["terminal_predicate"] == "provider_failed"
    assert serialized_attempt["provider_terminal_reason"] == "aborted_streaming"
    assert "aborted_streaming" in serialized_attempt["provider_output_tail"]
    assert serialized_attempt["turn_ms"] == 250
    assert serialized_attempt["trace"][-1]["label"] == "provider_failed"

    missing = await client.get(
        f"/api/admin/projects/{project.id}"
        f"/strategic-session/consultations/{uuid.uuid4()}"
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_endpoints_enforce_project_access(client: AsyncClient, db_session):
    """A project the caller is not a member of is not readable through here."""
    other = await _make_project(db_session, user_id=OTHER_USER_ID)
    request = _public_request(other.id)
    db_session.add(request)
    await db_session.commit()

    listing = await client.get(
        f"/api/admin/projects/{other.id}/strategic-session/consultations"
    )
    assert listing.status_code in {403, 404}
    detail = await client.get(
        f"/api/admin/projects/{other.id}"
        f"/strategic-session/consultations/{request.id}"
    )
    assert detail.status_code in {403, 404}
