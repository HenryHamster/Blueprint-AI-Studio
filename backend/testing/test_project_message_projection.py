import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    Task,
)
from app.project_message_projection import (
    MESSAGE_CONTEXT_PROJECTION_MAX_BYTES,
    MESSAGE_EVENT_MAX_BYTES,
    WAKEUP_STATE_CONTEXT_KEY,
    admin_message_dict,
    admin_message_list_dict,
    agent_fetch_message_dict,
    agent_thread_message_dict,
    legacy_direction_message_dict,
    message_event_payload,
    message_preview_dict,
)
from app.conversation_streaming import (
    build_streaming_reply_message,
    select_live_fast_claim,
    select_max_conversation_sequence,
    streaming_reply_event_payload,
    streaming_reply_message_id,
)
from app.project_message_creation import reserve_project_message_sequences
from testing.factories import make_task

TEST_USER_ID = "test-user-123"


def test_streaming_reply_projection_is_stable_and_uses_message_contract():
    project_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    task_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    claim_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    key = "typed-fast:claim-1:reply-1"
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    first = build_streaming_reply_message(
        project_id=project_id, task_id=task_id, session_id="session-1",
        claim_id=claim_id, idempotency_key=key, content="First", sequence=8, now=now,
    )
    second = build_streaming_reply_message(
        project_id=project_id, task_id=task_id, session_id="session-1",
        claim_id=claim_id, idempotency_key=key, content="First sentence.", sequence=9, now=now,
    )
    event = message_event_payload(second)

    assert first.id == second.id == streaming_reply_message_id(project_id, key)
    assert event["schema_version"] == 1
    assert event["message"]["id"] == str(first.id)
    assert event["message"]["content"] == "First sentence."
    assert event["message"]["client_idempotency_key"] == key
    assert event["message"]["context"]["_conversation_streaming_preview"] is True


def test_streaming_reply_delta_projection_stays_bounded_without_refetch():
    project_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    message = build_streaming_reply_message(
        project_id=project_id,
        task_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        session_id="session-1",
        claim_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        idempotency_key="typed-fast:claim-1:long-reply",
        content="x" * 1_200,
        sequence=9,
    )

    event = streaming_reply_event_payload(message, stream_seq=4)
    encoded = json.dumps(event, separators=(",", ":"), default=str).encode("utf-8")

    assert len(encoded) <= MESSAGE_EVENT_MAX_BYTES
    assert event.get("canonical_refetch_required") is not True
    assert event["message"]["context"]["_conversation_streaming_delta"] is True
    assert event["message"]["context"]["_conversation_streaming_seq"] == 4


def _message(**overrides) -> ProjectMessage:
    base = {
        "id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "project_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "message_type": MessageType.BLOCKER,
        "from_type": "system",
        "from_task_id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
        "content": "Investigate the stalled rollout.",
        "context": None,
        "status": "linked",
        "resolution": "Owned by direction d-1",
        "resolved_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "created_at": datetime(2026, 1, 1, 3, 4, 5, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return ProjectMessage(**base)


async def _make_project(session) -> Project:
    project = Project(name="Message Contract", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


def test_admin_message_dict_normalizes_enum_timestamp_and_empty_context():
    msg = _message()

    data = admin_message_dict(msg)

    assert data["id"] == str(msg.id)
    assert data["project_id"] == str(msg.project_id)
    assert data["message_type"] == "BLOCKER"
    assert data["from_type"] == "system"
    assert data["from_task_id"] == str(msg.from_task_id)
    assert data["actor_user_id"] is None
    assert data["sequence"] is None
    assert data["surface"] == "activity"
    assert data["handling_state"] == "pending"
    assert data["handled_by_task_id"] is None
    assert data["reply_to_message_id"] is None
    assert data["idempotency_key"] is None
    assert data["context"] == {}
    assert data["status"] == "linked"
    assert data["resolution"] == "Owned by direction d-1"
    assert data["created_at"] == "2026-01-01T03:04:05+00:00"
    assert data["resolved_at"] == "2026-01-02T03:04:05+00:00"
    assert data["updated_at"] == msg.updated_at.replace(tzinfo=timezone.utc).isoformat()


def test_compatibility_wrappers_keep_existing_public_aliases():
    handled_by = uuid.UUID("44444444-4444-4444-8444-444444444444")
    reply_to = uuid.UUID("55555555-5555-4555-8555-555555555555")
    msg = _message(
        message_type=MessageType.FEEDBACK,
        from_type="user",
        actor_user_id="operator-7",
        sequence=17,
        surface=MessageSurface.CONVERSATION,
        handling_state="responded",
        handled_by_task_id=handled_by,
        reply_to_message_id=reply_to,
        idempotency_key="client-send-17",
        status="open",
    )

    legacy = legacy_direction_message_dict(msg)
    thread = agent_thread_message_dict(msg)
    fetched = agent_fetch_message_dict(msg, convergence_link={"kind": "task", "id": "t-1"})

    assert legacy["type"] == "FEEDBACK"
    assert legacy["from"] == "user"
    assert legacy["context"] == {}
    assert "message_type" not in legacy

    assert thread["type"] == "FEEDBACK"
    assert thread["from"] == "user"
    assert thread["resolved_at"] == "2026-01-02T03:04:05+00:00"

    assert fetched["type"] == "FEEDBACK"
    assert fetched["from_type"] == "user"
    assert fetched["project_id"] == str(msg.project_id)
    assert fetched["from_task_id"] == str(msg.from_task_id)
    assert fetched["actor_user_id"] == "operator-7"
    assert fetched["sequence"] == 17
    assert fetched["surface"] == "conversation"
    assert fetched["handling_state"] == "responded"
    assert fetched["handled_by_task_id"] == str(handled_by)
    assert fetched["reply_to_message_id"] == str(reply_to)
    assert fetched["idempotency_key"] == "client-send-17"
    assert fetched["client_idempotency_key"] == "client-send-17"
    assert fetched["updated_at"] == msg.updated_at.replace(tzinfo=timezone.utc).isoformat()
    assert fetched["resolved_at"] == "2026-01-02T03:04:05+00:00"
    assert "context" not in fetched
    assert fetched["convergence_link"] == {"kind": "task", "id": "t-1"}


def test_message_preview_compacts_content_and_context_subset():
    msg = _message(
        content="A" * 400,
        context={
            "event_type": "blocker_linked",
            "direction_id": "d-" + ("x" * 200),
            "huge": "not returned",
        },
    )

    data = message_preview_dict(msg)

    assert data["message_type"] == "BLOCKER"
    assert len(data["content"]) <= 280
    assert data["content"].endswith("…")
    assert data["context"]["event_type"] == "blocker_linked"
    assert len(data["context"]["direction_id"]) <= 120
    assert data["context"]["direction_id"].endswith("…")
    assert "huge" not in data["context"]


def test_message_preview_preserves_bounded_workday_chat_provenance():
    result_packet_id = "packet-" + ("a" * 200)
    requested = _message(
        from_type="strategic",
        surface=MessageSurface.CONVERSATION,
        context={
            "event_type": "question_answer",
            "message_role": "operator_reply",
            "_workday_consultation_requested": True,
            "_conversation_public_reply_owner": "fast",
        },
    )
    fast_only = _message(
        from_type="strategic",
        surface=MessageSurface.CONVERSATION,
        context={
            "event_type": "question_answer",
            "message_role": "operator_reply",
            "_workday_consultation_requested": False,
            "_conversation_public_reply_owner": "fast",
        },
    )
    continuation = _message(
        from_type="strategic",
        surface=MessageSurface.CONVERSATION,
        context={
            "event_type": "question_answer",
            "message_role": "workday_consultation_continuation",
            "_workday_result_packet_id": result_packet_id,
            "_resident_activation_prompt": True,
            "_conversation_public_reply_owner": "fast",
        },
    )

    requested_data = message_preview_dict(requested)
    requested_event = message_event_payload(requested)
    fast_only_data = message_preview_dict(fast_only)
    fast_only_event = message_event_payload(fast_only)
    data = message_preview_dict(continuation)
    event = message_event_payload(continuation)

    assert requested_data["context"] == {
        "event_type": "question_answer",
        "message_role": "operator_reply",
        "_workday_consultation_requested": True,
    }
    assert requested_event["message"]["context"] == requested_data["context"]
    assert fast_only_data["context"] == {
        "event_type": "question_answer",
        "message_role": "operator_reply",
        "_workday_consultation_requested": False,
    }
    assert fast_only_event["message"]["context"] == fast_only_data["context"]
    assert data["context"]["message_role"] == "workday_consultation_continuation"
    assert data["context"]["_workday_result_packet_id"].endswith("…")
    assert len(data["context"]["_workday_result_packet_id"]) <= 120
    assert "_workday_consultation_requested" not in data["context"]
    assert data["context"]["_resident_activation_prompt"] is True
    assert "_conversation_public_reply_owner" not in data["context"]
    assert event["message"]["context"] == data["context"]


def test_message_preview_preserves_bounded_strategic_session_snapshot():
    msg = _message(
        context={
            "event_type": "blocker_linked",
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "pipeline",
                "session_started_at": "2026-06-29T12:00:00Z",
                "captured_at": "2026-06-29T12:05:00Z",
                "context_package_id": "strategic-context:proj-1",
                "selected_refs": [
                    {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
                    {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Duplicate"},
                    {"kind": "artifact", "id": "artifact-1", "label": "x" * 500, "storage_uri": "gs://secret"},
                    {"kind": "", "id": "ignored"},
                ],
                "storage_uri": "gs://secret",
            },
            "huge": "not returned",
        },
    )

    data = message_preview_dict(msg)

    assert data["context"]["event_type"] == "blocker_linked"
    assert "huge" not in data["context"]
    session = data["context"]["strategic_session"]
    assert session["interaction_mode"] == "continuous"
    assert session["active_surface"] == "pipeline"
    assert session["context_package_id"] == "strategic-context:proj-1"
    assert session["captured_at"] == "2026-06-29T12:05:00Z"
    assert session["selected_refs"] == [
        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
        {"kind": "artifact", "id": "artifact-1", "label": "x" * 160},
    ]
    assert "storage_uri" not in session


def test_message_preview_preserves_visible_sources_as_bounded_structured_data():
    msg = _message(
        context={
            "attachments": [
                {
                    "id": "att-1",
                    "type": "text/plain",
                    "filename": "evidence.txt",
                    "revision": 7,
                }
            ],
            "source_refs": [
                {"kind": "note", "id": "note-1", "revision": 4, "label": "Runbook"}
            ],
            "artifact_refs": [
                {"kind": "whiteboard", "id": "board-1", "checksum": "abc123"}
            ],
            "operator_intent": {
                "kind": "verify",
                "lane_owner": "operator",
                "artifact_ref_id": "artifact-ref-1",
                "expected_revision": 7,
            },
            "huge": "not returned",
        }
    )

    data = message_preview_dict(msg)

    assert data["context"]["attachments"][0]["filename"] == "evidence.txt"
    assert data["context"]["attachments"][0]["type"] == "text/plain"
    assert data["context"]["source_refs"][0]["revision"] == 4
    assert data["context"]["artifact_refs"][0]["checksum"] == "abc123"
    assert data["context"]["operator_intent"] == {
        "kind": "verify",
        "lane_owner": "operator",
        "artifact_ref_id": "artifact-ref-1",
        "expected_revision": 7,
    }
    assert "huge" not in data["context"]


def test_visible_source_projection_strips_secrets_uris_and_unknown_metadata():
    msg = _message(
        context={
            "attachments": [{
                "id": "att-1",
                "filename": "evidence.txt",
                "storage_uri": "gs://private-bucket/object",
                "signed_url": "https://signed.example/secret",
                "url": "https://attachment.example/secret",
                "thumbnail_url": "https://thumb.example/secret",
                "access_token": "bearer-secret",
                "metadata": {"credential": "nested-secret"},
            }],
            "source_refs": [{
                "kind": "note",
                "id": "note-1",
                "revision": 4,
                "download_url": "https://download.example/private",
                "api_token": "token-secret",
            }],
            "artifact_refs": [{
                "kind": "whiteboard",
                "id": "board-1",
                "checksum": "abc123",
                "uri": "gs://private-board",
                "credentials": "private",
            }],
            "operator_intent": {
                "kind": "verify",
                "lane_owner": "operator",
                "authorization": "Bearer private",
            },
        }
    )

    serialized = json.dumps(message_preview_dict(msg), default=str)

    assert "evidence.txt" in serialized
    assert "abc123" in serialized
    for forbidden in (
        "private-bucket", "signed.example", "bearer-secret", "nested-secret",
        "download.example", "token-secret", "private-board", "Bearer private",
        "attachment.example", "thumb.example",
    ):
        assert forbidden not in serialized


def test_all_full_and_agent_projections_strip_internal_receipts_and_typed_secrets():
    msg = _message(
        context={
            "_message_request_fingerprint": "private-fingerprint",
            WAKEUP_STATE_CONTEXT_KEY: "queued",
            "attachments": [{
                "attachment_id": "att-1",
                "filename": "evidence.txt",
                "storage_uri": "gs://private-bucket/object",
                "url": "https://signed.example/secret",
                "metadata": {"authorization": "nested-secret"},
            }],
            "operator_intent": {
                "kind": "verify",
                "target": {"authorization": "Bearer nested"},
            },
        },
    )

    for projection in (
        admin_message_dict(msg),
        admin_message_list_dict(msg),
        agent_thread_message_dict(msg),
        message_preview_dict(msg),
        message_event_payload(msg)["message"],
    ):
        serialized = json.dumps(projection, separators=(",", ":"), default=str)
        assert "private-fingerprint" not in serialized
        assert WAKEUP_STATE_CONTEXT_KEY not in serialized
        assert "private-bucket" not in serialized
        assert "signed.example" not in serialized
        assert "nested-secret" not in serialized
        assert "Bearer nested" not in serialized
        assert projection["context"]["attachments"] == [{
            "attachment_id": "att-1",
            "filename": "evidence.txt",
        }]
        assert projection["context"]["operator_intent"] == {"kind": "verify"}


def test_full_and_agent_context_projections_have_a_strict_total_byte_cap():
    msg = _message(context={
        "event_type": "operator_message",
        "source_refs": [{"kind": "note", "id": "note-1", "label": "Visible"}],
        **{f"legacy_{index}": "🙂" * 2_000 for index in range(200)},
    })

    for projection in (admin_message_dict(msg), agent_thread_message_dict(msg)):
        encoded = json.dumps(
            projection["context"], separators=(",", ":"), default=str
        ).encode("utf-8")
        assert len(encoded) <= MESSAGE_CONTEXT_PROJECTION_MAX_BYTES
        assert projection["context"]["event_type"] == "operator_message"
        assert projection["context"]["source_refs"][0]["id"] == "note-1"
        assert projection["context"]["projection_truncated"] is True


def test_message_event_omits_nested_objects_from_typed_scalar_fields():
    msg = _message(
        idempotency_key="nested-secret-probe",
        context={
            "attachments": [{
                "id": "att-1",
                "author": {
                    "api_token": "NESTED-API-TOKEN",
                    "storage_uri": "gs://nested-private-object",
                },
            }],
            "operator_intent": {
                "kind": "verify",
                "target": {
                    "authorization": "Bearer nested-private-token",
                    "storage_uri": "gs://nested-private-target",
                },
            },
        },
    )

    payload = message_event_payload(msg)
    serialized = json.dumps(payload, separators=(",", ":"), default=str)

    assert len(serialized.encode("utf-8")) <= MESSAGE_EVENT_MAX_BYTES
    assert payload["message"]["id"] == str(msg.id)
    assert payload["message"]["idempotency_key"] == "nested-secret-probe"
    assert payload["message"]["context"]["attachments"] == [{"id": "att-1"}]
    assert payload["message"]["context"]["operator_intent"] == {"kind": "verify"}
    for forbidden in (
        "NESTED-API-TOKEN",
        "nested-private-object",
        "nested-private-token",
        "nested-private-target",
    ):
        assert forbidden not in serialized


def test_message_event_carries_canonical_projection_for_gap_reconciliation():
    msg = _message(
        content="Canonical realtime message",
        context={"source_refs": [{"kind": "note", "id": "note-1", "revision": 2}]},
    )

    payload = message_event_payload(msg)

    assert payload["message"]["id"] == str(msg.id)
    assert payload["message"]["content"] == "Canonical realtime message"
    assert payload["message"]["sequence"] is None
    assert payload["message"]["surface"] == "activity"
    assert payload["message"]["handling_state"] == "pending"
    assert payload["message"]["context"]["source_refs"][0]["revision"] == 2


def test_message_event_preserves_bounded_audience_for_realtime_admission():
    msg = _message(
        surface=MessageSurface.CONVERSATION,
        from_type="strategic",
        context={
            "event_type": "resident_internal_bookkeeping",
            "audience": "strategist-internal",
            "private_detail": "not transported",
        },
    )

    preview = message_preview_dict(msg)
    payload = message_event_payload(msg)

    assert preview["context"]["audience"] == "strategist-internal"
    assert payload["message"]["context"]["audience"] == "strategist-internal"
    assert "private_detail" not in payload["message"]["context"]
    assert (
        len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
        <= MESSAGE_EVENT_MAX_BYTES
    )


def test_message_event_enforces_hard_serialized_budget():
    oversized_items = [
        {
            "id": f"item-{index}",
            "kind": "note",
            "label": "x" * 20_000,
            "excerpt": "y" * 20_000,
            "revision": index,
        }
        for index in range(12)
    ]
    msg = _message(
        content="z" * 20_000,
        resolution="r" * 200_000,
        idempotency_key="client-send-123",
        sequence=42,
        surface="conversation",
        reply_to_message_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        context={
            "attachments": oversized_items,
            "source_refs": oversized_items,
            "artifact_refs": oversized_items,
            "operator_intent": {"kind": "verify", "summary": "q" * 20_000},
        },
    )
    # Legacy rows predate current enum/length checks; the transport budget must
    # remain strict even when aliases are pathological.
    msg.message_type = "M" * 200_000
    msg.from_type = "F" * 200_000
    msg.status = "S" * 200_000

    payload = message_event_payload(msg)
    encoded = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

    assert len(encoded) <= MESSAGE_EVENT_MAX_BYTES
    assert payload["message"]["projection_truncated"] is True
    assert payload["canonical_refetch_required"] is True
    assert payload["message"]["content_truncated"] is True
    assert payload["message"]["resolution_truncated"] is True
    assert payload["message"]["idempotency_key"] == "client-send-123"
    assert payload["message"]["client_idempotency_key"] == "client-send-123"
    assert payload["message"]["id"] == str(msg.id)
    assert payload["message"]["sequence"] == 42
    assert payload["message"]["surface"] == "conversation"
    assert payload["message"]["reply_to_message_id"] == str(msg.reply_to_message_id)
    assert len(payload["type"]) <= 64
    assert len(payload["from"]) <= 32


@pytest.mark.asyncio
async def test_admin_messages_endpoint_keeps_full_message_contract(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    task_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    db_session.add(
        make_task(
            id=task_id,
            project_id=project.id,
            title="Message owner task",
        )
    )
    await db_session.flush()
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        from_task_id=task_id,
        content="Full admin route contract",
        context={"audience": "operator-action-needed"},
        status="linked",
        resolution="Owned by task",
        resolved_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/messages?project_id={project.id}&status=all"
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    item = data["messages"][0]
    assert set(item) == {
        "id",
        "project_id",
        "message_type",
        "from_type",
        "from_task_id",
        "actor_user_id",
        "sequence",
        "surface",
        "handling_state",
        "handled_by_task_id",
        "reply_to_message_id",
        "idempotency_key",
        "content",
        "context",
        "status",
        "resolution",
        "resolved_at",
        "delivered_at",
        "created_at",
        "updated_at",
    }
    assert item["message_type"] == "BUG_REPORT"
    assert item["from_type"] == "agent"
    assert item["from_task_id"] == "44444444-4444-4444-8444-444444444444"
    assert item["context"] == {}
    assert item["status"] == "linked"
    assert item["resolution"] == "Owned by task"
    assert item["resolved_at"].startswith("2026-02-03T04:05:06")
    assert item["delivered_at"] is None


@pytest.mark.asyncio
async def test_admin_messages_endpoint_eager_loads_delivered_at_under_raiseload(
    client: AsyncClient,
    db_session,
):
    """The list serializer must not touch a column omitted by its raiseload projection."""
    project = await _make_project(db_session)
    delivered_at = datetime(2026, 2, 4, 5, 6, 7, tzinfo=timezone.utc)
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="Delivered projection receipt",
            status="open",
            context={"audience": "operator-fyi"},
            delivered_at=delivered_at,
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/messages?project_id={project.id}&status=all"
    )

    assert response.status_code == 200, response.text
    [message] = response.json()["messages"]
    assert message["delivered_at"].startswith("2026-02-04T05:06:07")


@pytest.mark.asyncio
async def test_activity_endpoint_excludes_conversation_projection(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    activity_sequence, conversation_sequence = (
        await reserve_project_message_sequences(
            db_session, project.id, count=2
        )
    )
    db_session.add_all([
        ProjectMessage(
            project_id=project.id,
            sequence=activity_sequence,
            surface=MessageSurface.ACTIVITY,
            content="activity only",
            context={"audience": "operator-fyi"},
        ),
        # Non-operator (strategic) conversation projection: the activity list
        # excludes it. Operator-signal rows (user / FEEDBACK-BLOCKER-BUG_REPORT /
        # high-signal system NOTEs) are intentionally surfaced regardless of
        # surface (see _operator_signal_predicate in admin_notifications), so a
        # user-authored conversation message WOULD appear; strategic replies do not.
        ProjectMessage(
            project_id=project.id,
            sequence=conversation_sequence,
            surface=MessageSurface.CONVERSATION,
            from_type="strategic",
            content="conversation only",
        ),
    ])
    await db_session.commit()

    response = await client.get(
        f"/api/admin/messages?project_id={project.id}&status=all"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert [row["content"] for row in payload["messages"]] == ["activity only"]


@pytest.mark.asyncio
async def test_legacy_project_messages_endpoint_keeps_type_from_aliases(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Legacy alias contract",
        context=None,
        status="open",
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/messages")

    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    assert set(item) == {
        "id",
        "type",
        "content",
        "from",
        "status",
        "context",
        "created_at",
    }
    assert item["type"] == "QUESTION"
    assert item["from"] == "strategic"
    assert "message_type" not in item
    assert "from_type" not in item
    assert item["context"] == {}


@pytest.mark.asyncio
async def test_legacy_project_message_create_snapshots_strategic_session(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    with patch(
        "app.routers.directions.broadcast_to_project",
        new_callable=AsyncMock,
    ) as broadcast:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/messages",
            json={
                "message_type": "NOTE",
                "content": "Keep this canvas around.",
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "project",
                    "selected_refs": [
                        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"}
                    ],
                },
            },
        )

    assert resp.status_code == 200, resp.text
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[:2] == (str(project.id), "message_created")
    event_payload = broadcast.await_args.args[2]
    assert event_payload["schema_version"] == 1
    assert event_payload["project_id"] == str(project.id)
    assert event_payload["resource"] == "project_message"
    assert event_payload["action"] == "created"
    assert event_payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}
    assert event_payload["message_id"] == resp.json()["id"]
    assert event_payload["type"] == "NOTE"
    assert event_payload["from"] == "user"
    assert event_payload["message"]["id"] == resp.json()["id"]
    assert event_payload["message"]["status"] == "open"
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == uuid.UUID(resp.json()["id"]))
        )
    ).scalar_one()
    session = (msg.context or {})["strategic_session"]
    assert session["interaction_mode"] == "continuous"
    assert session["active_surface"] == "project"
    assert session["selected_refs"] == [
        {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"}
    ]


@pytest.mark.asyncio
async def test_legacy_project_message_create_normalizes_message_type_case(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={
            "message_type": "feedback",
            "content": "Lowercase type should match the canonical message route.",
        },
    )

    assert resp.status_code == 200, resp.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.id == uuid.UUID(resp.json()["id"])
            )
        )
    ).scalar_one()
    assert msg.message_type == MessageType.FEEDBACK
    assert msg.content == "Lowercase type should match the canonical message route."


@pytest.mark.asyncio
async def test_legacy_project_message_create_rejects_oversized_workbench_context(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/messages",
        json={
            "message_type": "NOTE",
            "content": "Too much context.",
            "workbench_context": {
                "selected_refs": [{"kind": "task", "id": "x" * 60_000}],
            },
        },
    )

    assert resp.status_code == 400
    assert "Workbench context too large" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_strategic_open_message_payload_keeps_agent_aliases(db_session):
    from app.strategic_spawning import _fetch_open_messages

    project = await _make_project(db_session)
    linked_msg = ProjectMessage(
        project_id=project.id,
        sequence=6,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="Already owned",
        status="linked",
        created_at=datetime(2026, 2, 1, 4, 6, 6, tzinfo=timezone.utc),
    )
    db_session.add(linked_msg)
    await db_session.commit()
    open_msg = ProjectMessage(
        project_id=project.id,
        actor_user_id="operator-7",
        sequence=7,
        surface=MessageSurface.CONVERSATION,
        reply_to_message_id=linked_msg.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="Open payload contract",
        context={
            "source_refs": [{
                "kind": "task",
                "id": "task:11111111-1111-4111-8111-111111111111",
                "revision": "rev-7",
            }],
            "attachments": [{
                "attachment_id": "22222222-2222-4222-8222-222222222222",
                "filename": "evidence.txt",
                "size_bytes": 12,
                "storage_uri": "gs://must-not-reach/private",
            }],
        },
        status="open",
        created_at=datetime(2026, 2, 1, 4, 5, 6, tzinfo=timezone.utc),
    )
    db_session.add(open_msg)
    await db_session.commit()

    payload = await _fetch_open_messages(db_session, project.id)

    assert len(payload) == 1
    item = payload[0]
    assert item["type"] == "BLOCKER"
    assert item["from"] == "user"
    assert item["actor_user_id"] == "operator-7"
    assert item["sequence"] == 7
    assert item["surface"] == "conversation"
    assert item["reply_to_message_id"] == str(linked_msg.id)
    assert item["context"]["source_refs"] == [{
        "kind": "task",
        "id": "task:11111111-1111-4111-8111-111111111111",
        "revision": "rev-7",
    }]
    assert item["context"]["attachments"] == [{
        "attachment_id": "22222222-2222-4222-8222-222222222222",
        "filename": "evidence.txt",
        "size_bytes": 12,
    }]
    assert item["status"] == "open"
    assert item["resolved_at"] is None
    assert "message_type" not in item
    assert "from_type" not in item


def test_admin_message_list_dict_returns_compact_context():
    """admin_message_list_dict preserves UI-needed context keys and strips large keys."""
    msg_with_context = _message(
        context={"event_type": "task_failed", "severity": "error", "strategic_session": {"version": 1}}
    )
    msg_no_context = _message(context=None)

    data_with = admin_message_list_dict(msg_with_context)
    data_none = admin_message_list_dict(msg_no_context)

    # UI-needed keys are preserved
    assert data_with["context"]["event_type"] == "task_failed"
    assert data_with["context"]["severity"] == "error"
    # Large keys not in the compact set are stripped
    assert "strategic_session" not in data_with["context"]
    # No context → empty dict
    assert data_none["context"] == {}
    # All other fields are still present and correct
    assert data_with["message_type"] == "BLOCKER"
    assert data_with["from_type"] == "system"
    assert data_with["status"] == "linked"


def test_admin_message_projection_strips_internal_request_fingerprint():
    msg = _message(
        context={
            "event_type": "operator_message",
            "_message_request_fingerprint": "server-only-secret",
        }
    )

    projected = admin_message_dict(msg)
    compact = admin_message_list_dict(msg)

    assert msg.context["_message_request_fingerprint"] == "server-only-secret"
    assert "_message_request_fingerprint" not in projected["context"]
    assert "_message_request_fingerprint" not in compact["context"]


@pytest.mark.asyncio
async def test_admin_messages_list_strips_large_context_keys(
    client: AsyncClient,
    db_session,
):
    """List endpoint returns compact context (UI-needed keys only), stripping large keys like strategic_session."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="Message with large context",
        context={
            "event_type": "task_failed",
            "audience": "operator-fyi",
            "strategic_session": {"version": 1, "huge": "x" * 10_000},
        },
        status="open",
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.get(f"/api/admin/messages?project_id={project.id}")

    assert resp.status_code == 200, resp.text
    items = resp.json()["messages"]
    assert len(items) == 1
    # event_type is a UI-needed key and must be present
    assert items[0]["context"]["event_type"] == "task_failed"
    # strategic_session is a large key excluded from the compact set
    assert "strategic_session" not in items[0]["context"]
    assert items[0]["content"] == "Message with large context"


async def _claimed_fast_turn(db_session):
    """A project whose one operator message is under a live FAST claim."""
    from app.conversation_coordinator import (
        ConversationPublicReplyOwner,
        claim_operator_messages,
    )

    project = await _make_project(db_session)
    message = ProjectMessage(
        project_id=project.id,
        # A BEFORE-INSERT trigger reallocates any conversation sequence that is
        # not already the project allocator's current value, so the number a
        # test hands in here is advisory. Read the persisted value back rather
        # than asserting on this literal.
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        from_type="user",
        content="Can you look into this?",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    task = Task(project_id=project.id, title="typed fast worker", payloads=[])
    db_session.add_all([message, task])
    await db_session.commit()
    claim = await claim_operator_messages(
        db_session,
        project_id=project.id,
        session_id="session-1",
        task_id=task.id,
        worker_generation=1,
        incarnation_id=uuid.uuid4(),
        message_ids=[message.id],
        lease_seconds=45,
        public_reply_owner=ConversationPublicReplyOwner.FAST,
    )
    assert claim is not None
    await db_session.commit()
    return project, message, task, claim


@pytest.mark.asyncio
async def test_streaming_relay_fence_runs_as_real_sql_against_the_claim_row(db_session):
    """The typed Fast relay's liveness fence must EXECUTE, not be mocked away.

    The websocket tests in ``test_agent.py`` stub the DB session, so every
    predicate in this query is invisible to them: a wrong JSON path, a wrong
    column, or a wrong enum comparison would leave streaming permanently dead in
    production with those tests green. This runs the exact query the relay
    issues against the real database and proves each predicate discriminates.
    """
    project, message, task, claim = await _claimed_fast_turn(db_session)
    now = datetime.now(timezone.utc)

    async def fence(**overrides):
        params = {
            "project_id": project.id,
            "claim_id": claim.claim_id,
            "task_id": task.id,
            "now": now,
        }
        params.update(overrides)
        return (
            await db_session.execute(select_live_fast_claim(**params))
        ).scalar_one_or_none()

    # A live Fast claim admits the relay. This alone exercises the JSON owner
    # path, the CLAIMED enum comparison and the expiry column as real SQL.
    live = await fence()
    assert live is not None
    assert live.id == message.id

    assert await fence(project_id=uuid.uuid4()) is None
    assert await fence(claim_id=uuid.uuid4()) is None
    assert await fence(task_id=uuid.uuid4()) is None
    # An expired lease is not a live claim: another worker is free to take the
    # turn, so this one must stop projecting into the operator's conversation.
    assert await fence(now=now + timedelta(hours=1)) is None


@pytest.mark.asyncio
async def test_streaming_relay_fence_rejects_standard_owner_and_settled_turn(db_session):
    """Only a live FAST-owned, still-CLAIMED turn may stream a public preview."""
    project, message, task, claim = await _claimed_fast_turn(db_session)
    now = datetime.now(timezone.utc)

    def query():
        return select_live_fast_claim(
            project_id=project.id, claim_id=claim.claim_id, task_id=task.id, now=now,
        )

    assert (await db_session.execute(query())).scalar_one_or_none() is not None

    # The Standard lane owning the public answer must fence the Fast relay out,
    # or two lanes paint the same turn.
    row = await db_session.get(ProjectMessage, message.id)
    row.context = {**(row.context or {}), "_conversation_public_reply_owner": "standard"}
    db_session.add(row)
    await db_session.commit()
    assert (await db_session.execute(query())).scalar_one_or_none() is None

    row = await db_session.get(ProjectMessage, message.id)
    row.context = {**(row.context or {}), "_conversation_public_reply_owner": "fast"}
    row.handling_state = MessageHandlingState.RESPONDED
    db_session.add(row)
    await db_session.commit()
    # Once the turn is answered the fence closes again, so a late frame cannot
    # reopen a settled conversation.
    assert (await db_session.execute(query())).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_streaming_preview_sequence_reads_only_the_conversation_surface(db_session):
    """The preview is placed after the last CONVERSATION message.

    Reading the whole message table instead would let a busy activity feed push
    the live preview to a sequence the conversation pane never reaches, so the
    operator watches a reply arrive nowhere. The activity row here sits far
    above the conversation row precisely so a missing surface filter changes
    the answer.
    """
    project, message, _task, _claim = await _claimed_fast_turn(db_session)
    activity = ProjectMessage(
        project_id=project.id,
        sequence=99,
        surface=MessageSurface.ACTIVITY,
        from_type="system",
        content="activity noise",
        status="open",
        handling_state=MessageHandlingState.PENDING,
    )
    db_session.add(activity)
    await db_session.commit()

    async def persisted_sequence(message_id):
        return await db_session.scalar(
            select(ProjectMessage.sequence).where(ProjectMessage.id == message_id)
        )

    operator_sequence = await persisted_sequence(message.id)
    activity_sequence = await persisted_sequence(activity.id)
    max_sequence = await db_session.scalar(select_max_conversation_sequence(project.id))

    # The activity row keeps its own far-ahead slot; only conversation rows are
    # renumbered onto the project allocator.
    assert activity_sequence == 99
    assert operator_sequence is not None
    assert operator_sequence < activity_sequence
    # Dropping the surface predicate would return 99 here.
    assert max_sequence == operator_sequence
