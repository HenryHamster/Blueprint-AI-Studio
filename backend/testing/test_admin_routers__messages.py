# AUTO-SPLIT from test_admin_routers.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared helpers (_make_project, _assert_message_event_payload) are
# duplicated here to keep this file self-contained. This file holds the admin_notifications
# domain slice: messages, noise-prefs, workbench-state, saved-views, mute-window.
"""Domain slice: admin_notifications.py — Messages

Split from test_admin_routers.py. Covers message CRUD, last-seen, noise prefs,
workbench state, saved views, temp mutes, and mute-window endpoints."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    LocalRunner,
    Project,
    ProjectMember,
    ProjectMessage,
    MessageSurface,
    MessageType,
    MessagesLastSeen,
    MessagesNoisePrefs,
    MessagesSavedViews,
    _utcnow,
)
from app.workbench_events import SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _assert_message_event_payload(
    payload,
    *,
    project_id,
    message_id: str,
    message_type: str,
    from_type: str,
    action: str,
    status: str = "open",
    excerpt: str | None = None,
):
    assert payload["message_id"] == message_id
    assert payload["type"] == message_type
    assert payload["from"] == from_type
    if action == "resolved":
        assert payload["status"] == "resolved"
    else:
        assert "status" not in payload
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project_id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == action
    assert payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}
    assert payload["message"]["id"] == message_id
    assert payload["message"]["type"] == message_type
    assert payload["message"]["from"] == from_type
    assert payload["message"]["status"] == status
    if excerpt is not None:
        assert payload["message"]["excerpt"] == excerpt
    assert len(json.dumps(payload, default=str).encode("utf-8")) < 16_384


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project", role="owner"):
    """Create a project with the given user as a member, plus the membership row."""
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role=role)
    session.add(member)
    await session.commit()
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# admin_notifications.py — Messages
# ═══════════════════════════════════════════════════════════════════════════════

class TestMessages:
    @pytest.mark.asyncio
    async def test_list_messages_empty(self, client: AsyncClient, db_session):
        """New project has no messages."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/messages?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_messages_excludes_strategist_internal_audience(
        self, client: AsyncClient, db_session
    ):
        """Ledger read path filters out non-operator-audience rows.

        operator_visible_message_audience_filter is fail-closed: only rows with
        an explicit operator audience (operator-fyi, operator-action-needed, etc.)
        are returned.  Strategist-internal rows and NULL-audience (legacy untagged)
        rows are both excluded — the reaper sweep backfills any surviving NULL-
        audience rows so they never accumulate in production.
        """
        project = await _make_project(db_session)
        # Strategist-internal bookkeeping row — must be hidden from the operator.
        hidden = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="strategist bookkeeping",
            status="open",
            context={"event_type": "pr_merged", "audience": "strategist-internal"},
        )
        # Operator-visible row — must be returned.
        # NOTE: the event_type is deliberately a NON-high-signal operator-fyi
        # event. On Postgres the ``projectmessage_0567_compat_insert`` BEFORE-INSERT
        # trigger (migration 0567/0608) routes any system NOTE whose event_type is
        # high-signal (e.g. ``failure_alert``) onto the CONVERSATION surface, so it
        # would never appear on the ACTIVITY messages list this endpoint queries —
        # a surface concern orthogonal to the audience filter under test here.
        # ``operator_command`` is operator-fyi but not high-signal, so it stays on
        # the ACTIVITY surface and correctly exercises the audience gate.
        visible = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="operator fyi",
            status="open",
            context={"event_type": "operator_command", "audience": "operator-fyi"},
        )
        # NULL-audience (legacy untagged) row — excluded by fail-closed filter.
        # In production the reaper sweep stamps these with the correct audience,
        # so legitimate operator messages are never lost; the filter rejects the
        # raw un-stamped row defensively.
        legacy = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="legacy untagged note",
            status="open",
            context={"event_type": "some_old_event"},
        )
        db_session.add_all([hidden, visible, legacy])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=all"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        contents = {m["content"] for m in data["messages"]}
        assert "operator fyi" in contents
        assert "legacy untagged note" not in contents
        assert "strategist bookkeeping" not in contents
        # total count excludes both the strategist-internal and the NULL-audience rows.
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_create_message(self, client: AsyncClient, db_session):
        """User can post a message to a project they belong to."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "This is a test note.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "created"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_message_viewer_forbidden(
        self, client: AsyncClient, db_session
    ):
        """A viewer may not post to the project message board (read-only role)."""
        project = await _make_project(db_session, name="Viewer Board", role="viewer")

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "Viewers must not be able to write this.",
            },
        )
        assert resp.status_code == 403, resp.text

        # Nothing persisted.
        from sqlalchemy import func as sa_func

        count = (
            await db_session.execute(
                select(sa_func.count(ProjectMessage.id)).where(
                    ProjectMessage.project_id == project.id
                )
            )
        ).scalar_one()
        assert count == 0

    @pytest.mark.asyncio
    async def test_create_message_member_allowed(
        self, client: AsyncClient, db_session
    ):
        """The member floor must not over-reject: a member (not owner) can post."""
        project = await _make_project(db_session, name="Member Board", role="member")

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "Members can post.",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "created"

    @pytest.mark.asyncio
    async def test_resolve_message_viewer_forbidden(
        self, client: AsyncClient, db_session
    ):
        """A viewer may not resolve someone else's message."""
        project = await _make_project(db_session, name="Viewer Resolve", role="viewer")
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Bug stays open",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}", json={"resolution": "wontfix"}
        )
        assert resp.status_code == 403, resp.text

        await db_session.refresh(msg)
        assert msg.status == "open"

    @pytest.mark.asyncio
    async def test_delete_message_viewer_forbidden(
        self, client: AsyncClient, db_session
    ):
        """A viewer may not delete a message off the board."""
        project = await _make_project(db_session, name="Viewer Delete", role="viewer")
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Must survive",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp.status_code == 403, resp.text

        # The message must still exist.
        still_there = await db_session.get(ProjectMessage, msg.id)
        assert still_there is not None

    @pytest.mark.asyncio
    async def test_create_message_broadcasts_created_once_for_idempotent_send(
        self, client: AsyncClient, db_session
    ):
        """A durable operator message wakes open workbenches exactly once."""
        project = await _make_project(db_session)
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "Keep this visible in the Strategic workbench.",
            "idempotency_key": "message-broadcast-idempotent-1",
        }

        with patch(
            "app.routers.admin_notifications.broadcast_to_project",
            new_callable=AsyncMock,
        ) as broadcast:
            first = await client.post("/api/admin/messages", json=body)
            second = await client.post("/api/admin/messages", json=body)

        assert first.status_code == 200, first.text
        assert first.json()["status"] == "created"
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "duplicate"
        assert second.json()["id"] == first.json()["id"]
        broadcast.assert_awaited_once()
        assert broadcast.await_args.args[:2] == (
            str(project.id),
            "message_created",
        )
        _assert_message_event_payload(
            broadcast.await_args.args[2],
            project_id=project.id,
            message_id=first.json()["id"],
            message_type="NOTE",
            from_type="user",
            action="created",
            excerpt="Keep this visible in the Strategic workbench.",
        )

    @pytest.mark.asyncio
    async def test_create_message_idempotency_key_dedupes(
        self, client: AsyncClient, db_session
    ):
        """Two sends sharing one idempotency key collapse to a single row."""
        from sqlalchemy import func as sa_func
        from app.models import ProjectMessage

        project = await _make_project(db_session)
        body = {
            "project_id": str(project.id),
            "message_type": "FEEDBACK",
            "content": "Steer toward X.",
            "idempotency_key": "key-double-submit-1",
        }

        first = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200
        assert first.json()["status"] == "created"
        first_id = first.json()["id"]
        assert first.json()["message"]["id"] == first_id

        # Identical retry with the same key — idempotent success, same row.
        second = await client.post("/api/admin/messages", json=body)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["id"] == first_id
        assert second.json()["message"]["id"] == first_id
        assert second.json()["message"]["message_type"] == "FEEDBACK"

        # Exactly one row landed for this project.
        count = await db_session.scalar(
            select(sa_func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
        )
        assert count == 1

        # A different key on the same content does create a second message.
        third = await client.post(
            "/api/admin/messages",
            json={**body, "idempotency_key": "key-double-submit-2"},
        )
        assert third.status_code == 200
        assert third.json()["status"] == "created"
        assert third.json()["id"] != first_id

    @pytest.mark.asyncio
    async def test_create_message_snapshots_current_strategic_session_context(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        workbench_resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                    "selected_refs": [
                        {"kind": "task", "id": "task-1", "label": "Watch task"},
                        {"kind": "task", "id": "task-1", "label": "Duplicate"},
                    ],
                },
            },
        )
        assert workbench_resp.status_code == 200

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "Keep this task in view.",
            },
        )

        assert resp.status_code == 200, resp.text
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        session = (msg.context or {})["strategic_session"]
        assert session["interaction_mode"] == "continuous"
        assert session["active_surface"] == "pipeline"
        assert session["selected_refs"] == [
            {"kind": "task", "id": "task-1", "label": "Watch task"}
        ]
        assert session["captured_at"]

    @pytest.mark.asyncio
    async def test_create_message_sanitizes_explicit_strategic_session_context(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        selected_refs = [
            {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
            {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Duplicate"},
            {"kind": "artifact", "id": "artifact-1", "label": "x" * 500, "storage_uri": "gs://secret"},
            *[
                {"kind": "task", "id": f"task-{index}", "label": f"Task {index}"}
                for index in range(20)
            ],
        ]

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "FEEDBACK",
                "content": "Use these refs.",
                "context": {"reply_to": "question-1"},
                "workbench_context": {
                    "interaction_mode": "invalid",
                    "active_surface": "runtime",
                    "session_started_at": "2026-06-29T12:00:00Z",
                    "context_package_id": "strategic-context:proj-1",
                    "selected_refs": selected_refs,
                    "storage_uri": "gs://secret",
                },
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"]["id"] == body["id"]
        assert body["message"]["message_type"] == "FEEDBACK"
        assert body["message"]["context"]["reply_to"] == "question-1"
        msg = (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.id == uuid.UUID(body["id"]))
            )
        ).scalar_one()
        context = msg.context or {}
        assert context["reply_to"] == "question-1"
        session = context["strategic_session"]
        assert session["interaction_mode"] == "chat"
        assert session["active_surface"] == "runtime"
        assert session["session_started_at"] == "2026-06-29T12:00:00Z"
        assert session["context_package_id"] == "strategic-context:proj-1"
        assert len(session["selected_refs"]) == 12
        assert session["selected_refs"][:2] == [
            {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
            {"kind": "artifact", "id": "artifact-1", "label": "x" * 160},
        ]
        assert "storage_uri" not in session

        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        stored_workbench = prefs_row.prefs["_strategic_workbench"]
        assert stored_workbench["interaction_mode"] == "chat"
        assert stored_workbench["active_surface"] == "runtime"
        assert stored_workbench["session_started_at"] == "2026-06-29T12:00:00Z"
        assert stored_workbench["last_interaction_at"]
        assert stored_workbench["selected_refs"][:2] == [
            {"kind": "canvas", "id": "strategic-canvas:proj-1", "label": "Context map"},
            {"kind": "artifact", "id": "artifact-1", "label": "x" * 160},
        ]
        assert "context_package_id" not in stored_workbench

    @pytest.mark.asyncio
    async def test_create_message_rejects_oversized_workbench_context(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "This should not scan a huge workbench context.",
                "workbench_context": {
                    "selected_refs": [
                        {"kind": "task", "id": "x" * 60_000}
                    ],
                },
            },
        )

        assert resp.status_code == 400
        assert "Workbench context too large" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_create_message_no_access_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Cannot post to a project the user is not a member of."""
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(uuid.uuid4()),
                "message_type": "NOTE",
                "content": "Sneaky note",
            },
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_message_invalid_type_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Unknown message_type is rejected."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "INVALID_TYPE",
                "content": "Test",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_message_empty_type_returns_400(
        self, client: AsyncClient, db_session
    ):
        """The canonical message endpoint does not default an empty type to NOTE."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "",
                "content": "Test",
            },
        )
        assert resp.status_code == 400
        assert "Invalid message_type" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_create_message_uses_canonical_context_size_limit(
        self, client: AsyncClient, db_session
    ):
        """The canonical endpoint preserves its default-json 50KB context limit."""
        project = await _make_project(db_session)
        context = {f"k{i}": "x" * 52 for i in range(800)}
        assert len(json.dumps(context, separators=(",", ":"), default=str)) <= 50_000
        assert len(json.dumps(context, default=str)) > 50_000

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "This context is too large for the canonical route.",
                "context": context,
            },
        )

        assert resp.status_code == 400
        assert "Context too large" in resp.json()["error"]["message"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_create_message_context_limit_counts_utf8_bytes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        context = {"operator_note": "🙂" * 13_000}
        assert len(json.dumps(context, ensure_ascii=False)) < 50_000
        assert len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 50_000

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "Reject a multibyte context over the byte budget.",
                "context": context,
            },
        )

        assert resp.status_code == 400
        assert "Context too large" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_create_message_empty_content_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Whitespace-only content is rejected."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "   ",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_message_over_limit_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Over-limit content is rejected (not silently truncated)."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "x" * (MESSAGE_CONTENT_MAX + 1),
            },
        )
        assert resp.status_code == 400
        assert str(MESSAGE_CONTENT_MAX) in resp.json()["error"]["message"]

        # Nothing should have been persisted for this project.
        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_create_message_bounds_operator_workbench_context_keys(
        self, client: AsyncClient, db_session
    ):
        """Workbench-owned context keys are bounded, while legacy extras survive."""
        project = await _make_project(db_session)
        selected_refs = [
            {"kind": "task", "id": "task-1", "label": "Release blocker"},
            {"kind": "task", "id": "task-1", "label": "Duplicate"},
        ]
        payload_context = {
            "event_type": "x" * 150,
            "user_event_type": "y" * 200,
            "reply_to": " reply-1 " * 80,
            "reply_preview": " preview " * 80,
            "answer": " answer " * 500,
            "resolution": " resolution " * 500,
            "dedupe_key": " dedupe " * 100,
            "strategic_session": {
                "interaction_mode": "continuous",
                "selected_refs": [{"kind": "task", "id": "attacker"}],
            },
            "operator_command": {"status": "succeeded"},
            "check_in": {"kind": "spoofed"},
            "legacy_extra": {"kept": True},
        }

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "FEEDBACK",
                "content": "Attach the current context without expanding it inline.",
                "context": payload_context,
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                    "session_started_at": "2026-06-29T12:00:00Z",
                    "context_package_id": "strategic-context:test",
                    "selected_refs": selected_refs,
                },
            },
        )

        assert resp.status_code == 200, resp.text
        body_context = resp.json()["message"]["context"]
        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        stored_context = msg.context or {}
        for context in (body_context, stored_context):
            assert "event_type" not in context
            assert "operator_command" not in context
            assert "check_in" not in context
            assert context["user_event_type"] == "x" * 120
            assert len(context["reply_to"]) == 160
            assert len(context["reply_preview"]) == 240
            assert len(context["answer"]) == 2_000
            assert len(context["resolution"]) == 2_000
            assert len(context["dedupe_key"]) == 240
            assert context["legacy_extra"] == {"kept": True}
            session = context["strategic_session"]
            assert session["interaction_mode"] == "continuous"
            assert session["active_surface"] == "pipeline"
            assert session["context_package_id"] == "strategic-context:test"
            assert session["selected_refs"] == [
                {"kind": "task", "id": "task-1", "label": "Release blocker"}
            ]

    @pytest.mark.asyncio
    async def test_create_message_drops_raw_strategic_session_without_workbench_context(
        self, client: AsyncClient, db_session
    ):
        """User context cannot inject a trusted Strategic session snapshot."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "Raw session context should not become trusted context.",
                "context": {
                    "strategic_session": {
                        "interaction_mode": "continuous",
                        "selected_refs": [{"kind": "task", "id": "attacker"}],
                    },
                    "legacy_extra": "kept",
                },
            },
        )

        assert resp.status_code == 200, resp.text
        body_context = resp.json()["message"]["context"]
        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        stored_context = msg.context or {}
        for context in (body_context, stored_context):
            assert "strategic_session" not in context
            assert context["legacy_extra"] == "kept"

    @pytest.mark.asyncio
    async def test_create_message_at_limit_succeeds(
        self, client: AsyncClient, db_session
    ):
        """Content of exactly the limit is accepted and stored in full."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)
        content = "x" * MESSAGE_CONTENT_MAX

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": content,
            },
        )
        assert resp.status_code == 200
        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        assert len(msg.content) == MESSAGE_CONTENT_MAX

    @pytest.mark.asyncio
    async def test_user_message_event_type_is_namespaced_not_routine_digest(
        self, client: AsyncClient, db_session
    ):
        """User context cannot claim the trusted system event_type contract."""
        project = await _make_project(db_session)

        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "PR #123 merged into main",
                "context": {"event_type": "pr_merged"},
            },
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        assert project.running_update is None

        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        assert msg.context.get("event_type") is None
        assert msg.context.get("user_event_type") == "pr_merged"

    async def _reload_vision(self, db_session, project_id) -> dict:
        """Re-read the project's vision JSON after an endpoint commit.

        The endpoint arms the marker on its own request-session/connection. We
        must close the test session first so the subsequent read happens in a
        fresh transaction — expiring the identity map alone keeps the stale
        pre-commit snapshot and would miss the just-committed marker.
        """
        await db_session.close()
        project = (
            await db_session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        return project.vision or {}

    @pytest.mark.asyncio
    async def test_canonical_message_arms_strategic_refresh(
        self, client: AsyncClient, db_session
    ):
        """The live-chat endpoint wakes the strategist on a steering message.

        Regression guard for the bug where the canonical MessagesPanel endpoint
        (POST /api/admin/messages) created the message but never armed the
        operator-origin refresh marker when the project row was contended, so
        live-chat steering could silently wait for the next cadence instead of
        waking the strategist immediately.
        """
        from app.strategic_trigger import strategic_refresh_pending
        import app.task_locking as task_locking

        project = await _make_project(db_session)

        lock_calls: list[tuple[bool, float]] = []

        @asynccontextmanager
        async def _fake_project_lock(
            session, project_id, *, skip_locked=True, timeout=30.0, load_columns=None
        ):
            lock_calls.append((skip_locked, timeout))
            if skip_locked:
                yield None
                return
            locked_project = await session.get(Project, project_id)
            assert locked_project is not None
            yield locked_project

        # The operator path routes its vision write through with_vision_locked(),
        # which acquires the row via task_locking.project_lock — patch it there.
        with patch.object(task_locking, "project_lock", _fake_project_lock):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "Please refocus on the auth bug",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "created"
        # The response reports the real arming decision so the UI can tell the
        # operator the message actually steered the strategist.
        assert resp.json()["arming"] == "steering"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["reason"] == "operator_message"
        assert marker["count"] == 1
        # Operator origin is what activates the breaker-bypass and deferral
        # consumer paths — the whole point of live steering.
        assert marker["origin"] == "operator"
        # The message text is persisted on the marker so the steering directive
        # survives the recent-messages snapshot aging out before the deferred
        # cycle reaches its review phase.
        assert marker["excerpt"] == "Please refocus on the auth bug"
        assert lock_calls == [(False, 1.0)]

    @pytest.mark.asyncio
    async def test_canonical_passive_note_does_not_arm_refresh(
        self, client: AsyncClient, db_session
    ):
        """A passive NOTE on the canonical endpoint must NOT wake the strategist."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "NOTE",
                "content": "FYI, just a note",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["arming"] == "passive_note"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is False

    @pytest.mark.asyncio
    async def test_user_feedback_with_reserved_event_type_still_arms_refresh(
        self, client: AsyncClient, db_session
    ):
        """A user message cannot suppress arming by spoofing a routine event_type."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        resp = await client.post(
            "/api/admin/messages",
            json={
                "project_id": str(project.id),
                "message_type": "FEEDBACK",
                "content": "review feedback digest",
                "context": {"event_type": "review_feedback"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["arming"] == "steering"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True

        msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
        assert msg is not None
        assert msg.context.get("event_type") is None
        assert msg.context.get("user_event_type") == "review_feedback"

    @pytest.mark.asyncio
    async def test_canonical_message_arm_failure_reports_not_armed(
        self, client: AsyncClient, db_session
    ):
        """When a genuine steering message cannot persist its wake marker (the
        bounded project-row acquire is exhausted under contention), the endpoint
        must surface the truthful ``not_armed`` outcome — never a false
        ``steering`` promise — and write no marker. Regression guard for the
        false operator receipt this task fixes.
        """
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)

        # Simulate the contended-row path: request_strategic_refresh exhausts its
        # bounded lock acquire and persists nothing (returns False).
        with patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(return_value=False),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "Please refocus on the auth bug",
                },
            )
        assert resp.status_code == 200, resp.text
        # Message still saved, but the receipt is truthful about the failed wake.
        assert resp.json()["status"] == "created"
        assert resp.json()["arming"] == "not_armed"

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is False

    @pytest.mark.asyncio
    async def test_canonical_message_arm_rollback_does_not_500(
        self, client: AsyncClient, db_session, expire_fidelity
    ):
        """Under real row contention the arm path rolls the session back to
        clear the aborted bounded-lock acquire, which EXPIRES the
        already-committed message. The endpoint must still return 200 with the
        persisted id — not 500 on a sync lazy-load (MissingGreenlet) of an
        expired ``msg.id``. Regression for the operator-message 500 where the
        message committed but the request still errored (prompting a double-post
        on retry). Distinct from ``..._reports_not_armed``, whose mock returns
        False WITHOUT rolling back, so it never expired the instance.
        """
        project = await _make_project(db_session)

        async def _rollback_then_fail(session, *args, **kwargs):
            # Mirror request_strategic_refresh's OperationalError handler: roll
            # the session back to keep the txn usable, which expires the
            # committed msg in the caller's session.
            await session.rollback()
            return False

        with patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(side_effect=_rollback_then_fail),
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "FEEDBACK",
                    "content": "steer me while a cycle is running",
                    "workbench_context": {
                        "interaction_mode": "continuous",
                        "active_surface": "strategic",
                        "selected_refs": [
                            {"kind": "task", "id": "task-rollback", "label": "Rollback task"}
                        ],
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["arming"] == "not_armed"
        # The id is the scalar snapshotted before the expire, not a lazy-load.
        assert isinstance(body["id"], str) and len(body["id"]) == 36
        assert body["message"]["id"] == body["id"]
        assert body["message"]["content"] == "steer me while a cycle is running"
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        assert prefs_row.prefs["_strategic_workbench"]["interaction_mode"] == "continuous"
        assert prefs_row.prefs["_strategic_workbench"]["selected_refs"] == [
            {"kind": "task", "id": "task-rollback", "label": "Rollback task"}
        ]

    @pytest.mark.asyncio
    async def test_canonical_message_burst_coalesces(
        self, client: AsyncClient, db_session
    ):
        """A burst of operator messages collapses into a single coalesced slot."""
        from app.strategic_trigger import (
            refresh_breaker_applies,
            strategic_refresh_pending,
        )

        project = await _make_project(db_session)
        for message_type in ("QUESTION", "BLOCKER"):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": message_type,
                    "content": f"steer {message_type}",
                },
            )
            assert resp.status_code == 200, resp.text

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["count"] == 2
        assert marker["reason"] == "operator_message"
        assert marker["origin"] == "operator"
        # Operator origin bypasses the failure breaker — exactly when an
        # operator needs to steer.
        assert refresh_breaker_applies(vision) is False

    @pytest.mark.asyncio
    async def test_user_message_reserved_event_type_skips_project_lock(
        self, client: AsyncClient, db_session
    ):
        """User-supplied event_type is not a trusted routine digest and does not lock."""
        project = await _make_project(db_session)

        import app.routers.admin_notifications as notif

        with patch.object(notif, "lock_project_or_429", AsyncMock()) as spy:
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "PR #7 merged",
                    "context": {"event_type": "pr_merged"},
                },
            )
        assert resp.status_code == 200
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_non_routine_message_skips_project_lock(
        self, client: AsyncClient, db_session
    ):
        """A plain NOTE (no routine event_type) does not pay the lock/429 cost."""
        project = await _make_project(db_session)

        import app.routers.admin_notifications as notif

        with patch.object(notif, "lock_project_or_429", AsyncMock()) as spy:
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "just a note",
                },
            )
        assert resp.status_code == 200
        spy.assert_not_awaited()

    # ── Idempotent steering-message creation ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_idempotent_double_submit_creates_one_row(
        self, client: AsyncClient, db_session
    ):
        """Posting the same intent twice with one idempotency_key dedupes to a
        single row, and both responses point at that same row."""
        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "duplicate steering intent",
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        # Same logical message: the first send creates, the retry is recognized
        # as a duplicate and returns the existing row's id.
        assert first.json()["status"] == "created"
        assert second.json()["status"] == "duplicate"
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["wakeup_state"] == second.json()["wakeup_state"]
        assert "_message_request_fingerprint" not in first.json()["message"]["context"]
        assert "_message_request_fingerprint" not in second.json()["message"]["context"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].id) == first.json()["id"]
        assert rows[0].idempotency_key == key
        assert (rows[0].context or {}).get("_message_request_fingerprint")

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_arming_coalesces(
        self, client: AsyncClient, db_session
    ):
        """A steering message POSTed twice with the same key arms a SINGLE
        refresh slot — the first send arms the wake, and the duplicate is
        recognized as such (``arming == "duplicate"``) rather than opening a
        second independent wake."""
        from app.strategic_trigger import strategic_refresh_pending

        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "FEEDBACK",
            "content": "Please refocus on the auth bug",
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        # The first send arms the strategist; the duplicate short-circuits before
        # the wake gate and reports the truthful "duplicate" outcome.
        assert first.json()["arming"] == "steering"
        assert second.json()["arming"] == "duplicate"
        assert first.json()["id"] == second.json()["id"]

        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["origin"] == "operator"
        # Only the first send armed a wake; the duplicate did not re-arm, so the
        # strategist has a SINGLE pending wake to consume.
        assert marker["count"] == 1

        # And the dedup still held: exactly one row backs both responses.
        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_idempotent_different_key_creates_new_message(
        self, client: AsyncClient, db_session
    ):
        """Identical content under two distinct keys is two distinct intents."""
        project = await _make_project(db_session)
        base = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "same words, different intent",
        }

        first = await client.post(
            "/api/admin/messages",
            json={**base, "idempotency_key": str(uuid.uuid4())},
        )
        second = await client.post(
            "/api/admin/messages",
            json={**base, "idempotency_key": str(uuid.uuid4())},
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_no_idempotency_key_creates_new_row_each_time(
        self, client: AsyncClient, db_session
    ):
        """Legacy path: omitting idempotency_key never dedupes (back-compat)."""
        project = await _make_project(db_session)
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "legacy unconditional insert",
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        # Both rows persisted with a NULL key (exempt from the partial index).
        assert all(r.idempotency_key is None for r in rows)

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_with_reserved_event_type_skips_digest(
        self, client: AsyncClient, db_session
    ):
        """Duplicate user sends converge, and spoofed event_type still writes no digest."""
        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        body = {
            "project_id": str(project.id),
            "message_type": "NOTE",
            "content": "PR #864 merged into main",
            "context": {"event_type": "pr_merged"},
            "idempotency_key": key,
        }

        first = await client.post("/api/admin/messages", json=body)
        second = await client.post("/api/admin/messages", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["id"] == second.json()["id"]

        await db_session.refresh(project)
        assert project.running_update is None

        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].context.get("event_type") is None
        assert rows[0].context.get("user_event_type") == "pr_merged"

    @pytest.mark.asyncio
    async def test_idempotent_concurrent_insert_rejects_mismatched_winner(
        self, client: AsyncClient, db_session
    ):
        """TOCTOU: a competitor commits the same-key row AFTER this request's
        idempotency pre-check passed but BEFORE its own commit, so the INSERT
        trips ux_projectmessage_project_idempotency_key. The handler must
        recover by re-selecting the winner, then reject it because its actor and
        request fingerprint do not match this request. A reused key must never
        silently adopt an unrelated committed message.

        Patch AsyncSession.commit to inject a winning row just before the route
        commits its pending message, exactly modelling an in-flight duplicate.
        """
        from conftest import TestingSessionLocal
        from sqlalchemy.ext.asyncio import AsyncSession as SQLAlchemyAsyncSession

        project = await _make_project(db_session)
        key = str(uuid.uuid4())
        injected = {"done": False}
        original_commit = SQLAlchemyAsyncSession.commit

        async def _commit_conflict_before_pending_message_commit(session):
            # Stand in for a concurrent request that won the race: persist the
            # same-key row in a separate transaction so this request's pending
            # INSERT violates the unique index on commit.
            #
            # The winner must be an OUT-OF-BAND, non-conversation row: at this
            # point the route already holds the exclusive projectmessagesequence
            # row lock (lock_project_and_next_message_sequence, taken before the
            # message INSERT). A conversation winner would trip the 0608
            # compat-insert trigger's sequence-allocator upsert (should_project
            # -> INSERT ... ON CONFLICT on projectmessagesequence) and block on
            # that held lock while this request synchronously awaits the winner's
            # commit — an asyncio/DB deadlock Postgres cannot detect (one side is
            # a Python await, not a DB lock wait). from_type="system" keeps
            # should_project False (see trigger), so the winner does NOT touch the
            # sequence allocator; it still shares (project_id, idempotency_key) so
            # the request's INSERT trips ux_projectmessage_project_idempotency_key.
            # This is exactly the raw/legacy same-key insert the recovery path
            # (re-select winner, 409 on mismatched actor/fingerprint) defends
            # against — a same-key row that never went through the advisory-locked
            # operator path. (On SQLite FOR UPDATE is a no-op, which is why the
            # original conversation winner only "passed" there.)
            if not injected["done"] and any(
                isinstance(obj, ProjectMessage) and obj.idempotency_key == key
                for obj in session.new
            ):
                injected["done"] = True
                async with TestingSessionLocal() as other:
                    other.add(
                        ProjectMessage(
                            project_id=project.id,
                            message_type=MessageType.NOTE,
                            from_type="system",
                            content="winner of the race",
                            context={"user_event_type": "pr_merged"},
                            idempotency_key=key,
                        )
                    )
                    await original_commit(other)
            return await original_commit(session)

        with patch.object(
            SQLAlchemyAsyncSession,
            "commit",
            _commit_conflict_before_pending_message_commit,
        ):
            resp = await client.post(
                "/api/admin/messages",
                json={
                    "project_id": str(project.id),
                    "message_type": "NOTE",
                    "content": "loser of the race",
                    "context": {"event_type": "pr_merged"},
                    "idempotency_key": key,
                },
            )

        assert resp.status_code == 409, resp.text

        # Exactly one row survived, and the response points at the winner.
        rows = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.idempotency_key == key,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].content == "winner of the race"

    @pytest.mark.asyncio
    async def test_resolve_message(self, client: AsyncClient, db_session):
        """An open message can be resolved."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_resolve_message_broadcasts_updated_event(
        self, client: AsyncClient, db_session
    ):
        """Resolving a message refreshes other open workbenches."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Close me everywhere.",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        with patch(
            "app.routers.admin_notifications.broadcast_to_project",
            new_callable=AsyncMock,
        ) as broadcast:
            resp = await client.patch(
                f"/api/admin/messages/{msg.id}",
                json={"resolution": "Done."},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resolved"
        broadcast.assert_awaited_once()
        assert broadcast.await_args.args[:2] == (
            str(project.id),
            "message_updated",
        )
        _assert_message_event_payload(
            broadcast.await_args.args[2],
            project_id=project.id,
            message_id=str(msg.id),
            message_type="NOTE",
            from_type="user",
            action="resolved",
            status="resolved",
            excerpt="Close me everywhere.",
        )
        canonical = broadcast.await_args.args[2]["message"]
        assert canonical["handling_state"] == "responded"
        assert canonical["resolution"] == "Done."
        assert canonical["updated_at"]

    @pytest.mark.asyncio
    async def test_resolve_linked_message(self, client: AsyncClient, db_session):
        """A linked message can be closed from the operator workbench."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="Owned by a direction, now done",
            status="linked",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={
                "resolution": "Direction finished.",
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                    "selected_refs": [
                        {"kind": "task", "id": "task-linked", "label": "Linked blocker"}
                    ],
                },
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["message"]["id"] == str(msg.id)
        assert body["message"]["status"] == "resolved"
        assert body["message"]["resolution"] == "Direction finished."
        await db_session.refresh(msg)
        assert msg.status == "resolved"
        assert msg.resolution == "Direction finished."
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        assert prefs_row.prefs["_strategic_workbench"]["interaction_mode"] == "continuous"
        assert prefs_row.prefs["_strategic_workbench"]["active_surface"] == "pipeline"
        assert prefs_row.prefs["_strategic_workbench"]["selected_refs"] == [
            {"kind": "task", "id": "task-linked", "label": "Linked blocker"}
        ]

    @pytest.mark.asyncio
    async def test_resolve_message_over_limit_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Over-limit resolution is rejected; the message stays unresolved."""
        from app.models import MESSAGE_CONTENT_MAX

        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "x" * (MESSAGE_CONTENT_MAX + 1)},
        )
        assert resp.status_code == 400
        assert str(MESSAGE_CONTENT_MAX) in resp.json()["error"]["message"]

        await db_session.refresh(msg)
        assert msg.status == "open"
        assert msg.resolution is None

    @pytest.mark.asyncio
    async def test_resolve_already_resolved_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Resolving an already-resolved message returns 'already_resolved'."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Done",
            status="resolved",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "project",
                    "selected_refs": [
                        {"kind": "message", "id": str(msg.id), "label": "Resolved note"}
                    ],
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_resolved"
        assert body["message"]["id"] == str(msg.id)
        assert body["message"]["status"] == "resolved"
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        assert prefs_row.prefs["_strategic_workbench"]["interaction_mode"] == "continuous"
        assert prefs_row.prefs["_strategic_workbench"]["active_surface"] == "project"
        assert prefs_row.prefs["_strategic_workbench"]["selected_refs"] == [
            {"kind": "message", "id": str(msg.id), "label": "Resolved note"}
        ]

    @pytest.mark.asyncio
    async def test_resolve_message_concurrent_requests_are_safe(
        self, client: AsyncClient, db_session
    ):
        """Concurrent resolves are safe: one succeeds, the other sees already_resolved."""
        import asyncio

        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # Simulate two concurrent resolve requests with different resolutions
        async def resolve_with(resolution_text):
            return await client.patch(
                f"/api/admin/messages/{msg.id}",
                json={"resolution": resolution_text},
            )

        resp1, resp2 = await asyncio.gather(
            resolve_with("First resolution"),
            resolve_with("Second resolution"),
        )
        # Both should succeed (200)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # One should be "resolved", the other "already_resolved"
        statuses = {resp1.json()["status"], resp2.json()["status"]}
        assert statuses == {"resolved", "already_resolved"}

        # Verify the message is resolved with the first resolution that won
        await db_session.refresh(msg)
        assert msg.status == "resolved"
        assert msg.resolution is not None
        for resp in (resp1, resp2):
            body = resp.json()
            assert body["message"]["id"] == str(msg.id)
            assert body["message"]["status"] == "resolved"
            assert body["message"]["resolution"] == msg.resolution

    @pytest.mark.asyncio
    async def test_resolve_reconciles_legacy_resolved_question_missing_reply(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        question = ProjectMessage(
            project_id=project.id,
            sequence=1,
            surface=MessageSurface.CONVERSATION,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Which path?",
            status="resolved",
            resolution="Take path A.",
            resolved_at=_utcnow(),
        )
        db_session.add(question)
        await db_session.commit()

        first = await client.patch(f"/api/admin/messages/{question.id}", json={})
        second = await client.patch(f"/api/admin/messages/{question.id}", json={})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["reply_message_id"] == second.json()["reply_message_id"]
        assert first.json()["wakeup_state"] == second.json()["wakeup_state"]
        replies = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.reply_to_message_id == question.id
                )
            )
        ).scalars().all()
        assert len(replies) == 1
        assert replies[0].content == "Take path A."
        assert (
            replies[0].context["_operator_wakeup_state"]
            == first.json()["wakeup_state"]
        )

    @pytest.mark.asyncio
    async def test_resolve_message_retried_request_is_safe(
        self, client: AsyncClient, db_session
    ):
        """Retrying a resolve request is safe (idempotent)."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="Help me",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # First resolve
        resp1 = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "resolved"

        # Retry the same request
        resp2 = await client.patch(
            f"/api/admin/messages/{msg.id}",
            json={"resolution": "Fixed!"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_resolved"

        # Message should still be resolved
        await db_session.refresh(msg)
        assert msg.status == "resolved"
        assert msg.resolution == "Fixed!"

    @pytest.mark.asyncio
    async def test_resolve_question_posts_question_answer_note(
        self, client: AsyncClient, db_session
    ):
        """Resolving a QUESTION via PATCH /messages/{id} creates a question_answer NOTE."""
        from app.strategic_trigger import strategic_refresh_pending
        from sqlmodel import select

        project = await _make_project(db_session)
        question = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="agent",
            content="What should I do?",
            status="open",
        )
        db_session.add(question)
        await db_session.commit()
        await db_session.refresh(question)

        resp = await client.patch(
            f"/api/admin/messages/{question.id}",
            json={"resolution": "Do X instead."},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["arming"] == "steering"
        assert body["wakeup_state"] == "queued"
        assert body["message"]["id"] == str(question.id)
        assert body["message"]["status"] == "resolved"
        assert body["message"]["resolution"] == "Do X instead."

        # Exactly one question_answer NOTE should exist, reply_to set to the question id
        notes = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                    ProjectMessage.from_type == "user",
                )
            )
        ).scalars().all()
        assert len(notes) == 1
        note = notes[0]
        assert note.context is not None
        assert note.context.get("event_type") == "question_answer"
        assert note.context.get("reply_to") == str(question.id)
        assert note.context.get("answer") == "Do X instead."
        assert body["reply_message_id"] == str(note.id)
        vision = await self._reload_vision(db_session, project.id)
        assert strategic_refresh_pending(vision) is True
        marker = vision["_strategic_refresh"]
        assert marker["origin"] == "operator"
        assert marker["reason"] == "operator_message"
        assert marker["excerpt"] == "Do X instead."

    @pytest.mark.asyncio
    async def test_operator_reply_resolves_open_question(
        self, client: AsyncClient, db_session
    ):
        """Operator replying to an open QUESTION via PATCH resolves it and posts a question_answer NOTE.

        This exercises the frontend reply path: when the operator types an answer
        to a system QUESTION and submits, the UI calls PATCH /messages/{id} with the
        answer as the resolution. The backend marks the question resolved and creates
        a question_answer NOTE linked back to it so the strategist sees it answered.
        """
        from sqlmodel import select

        project = await _make_project(db_session)
        question = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Should I use approach A or B?",
            status="open",
        )
        db_session.add(question)
        await db_session.commit()
        await db_session.refresh(question)

        with patch(
            "app.routers.admin_notifications.broadcast_to_project",
            new_callable=AsyncMock,
        ) as broadcast:
            resp = await client.patch(
                f"/api/admin/messages/{question.id}",
                json={
                    "resolution": "Use approach A.",
                    "workbench_context": {
                        "version": 1,
                        "interaction_mode": "continuous",
                        "active_surface": "strategic",
                        "selected_refs": [
                            {
                                "kind": "knowledge_document",
                                "id": "knowledge:kdoc-1",
                                "label": "Operator notes",
                            },
                        ],
                        "selected_ref_descriptors": [
                            {
                                "kind": "knowledge_document",
                                "id": "knowledge:kdoc-1",
                                "label": "Operator notes",
                                "summary": "Decision notes from the current review.",
                                "target": {
                                    "surface": "project",
                                    "section": "problem-model",
                                    "document_id": "kdoc-1",
                                },
                            },
                        ],
                        "session_started_at": "2026-06-29T01:00:00Z",
                        "context_package_id": "strategic-context:test",
                        "context_package_descriptor": {
                            "kind": "context_package",
                            "id": "strategic-context:test",
                            "label": "Current session context",
                            "target": {
                                "surface": "project",
                                "section": "problem-model",
                            },
                        },
                    },
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["arming"] == "steering"

        # Question row is now resolved with the operator's answer stored
        await db_session.refresh(question)
        assert question.status == "resolved"
        assert question.resolution == "Use approach A."

        # A question_answer NOTE was created so the strategist can read it
        notes = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                    ProjectMessage.from_type == "user",
                )
            )
        ).scalars().all()
        assert len(notes) == 1, "Exactly one answer note should be created"
        note = notes[0]
        assert note.context is not None
        assert note.context.get("event_type") == "question_answer"
        assert note.context.get("reply_to") == str(question.id)
        assert note.context.get("answer") == "Use approach A."
        assert note.context.get("_operator_wakeup_state") == body["wakeup_state"]
        assert "strategic_session" not in note.context
        assert body["reply_message_id"] == str(note.id)
        assert body["reply_message"]["id"] == str(note.id)
        assert body["reply_message"]["message_type"] == "NOTE"
        assert "strategic_session" not in body["reply_message"]["context"]
        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        stored_workbench = prefs_row.prefs["_strategic_workbench"]
        assert stored_workbench["interaction_mode"] == "continuous"
        assert stored_workbench["active_surface"] == "strategic"
        assert stored_workbench["selected_refs"] == [
            {
                "kind": "knowledge_document",
                "id": "knowledge:kdoc-1",
                "label": "Operator notes",
            }
        ]
        assert "context_package_id" not in stored_workbench
        assert broadcast.await_count == 2
        events = [
            (call.args[1], call.args[2])
            for call in broadcast.await_args_list
        ]
        assert events[0][0] == "message_updated"
        _assert_message_event_payload(
            events[0][1],
            project_id=project.id,
            message_id=str(question.id),
            message_type="QUESTION",
            from_type="system",
            action="resolved",
            status="resolved",
            excerpt="Should I use approach A or B?",
        )
        assert events[1][0] == "message_created"
        _assert_message_event_payload(
            events[1][1],
            project_id=project.id,
            message_id=str(note.id),
            message_type="NOTE",
            from_type="user",
            action="created",
            excerpt="Use approach A.",
        )

    @pytest.mark.asyncio
    async def test_operator_question_answer_rolls_back_atomically_when_arming_rolls_back(
        self, client: AsyncClient, db_session
    ):
        """A poisoned wake transaction leaves no partial answer and is retryable."""

        project = await _make_project(db_session)
        question = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Which path should Strategic take?",
            status="open",
        )
        db_session.add(question)
        await db_session.commit()
        await db_session.refresh(question)

        async def _rollback_then_fail(session, *args, **kwargs):
            await session.rollback()
            return False

        with patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(side_effect=_rollback_then_fail),
        ):
            resp = await client.patch(
                f"/api/admin/messages/{question.id}",
                json={"resolution": "Take the simpler path."},
            )

        assert resp.status_code == 503, resp.text

        await db_session.refresh(question)
        assert question.status == "open"
        assert question.resolution is None
        notes = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                    ProjectMessage.from_type == "user",
                )
            )
        ).scalars().all()
        assert notes == []

        retry = await client.patch(
            f"/api/admin/messages/{question.id}",
            json={"resolution": "Take the simpler path."},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "resolved"
        assert retry.json()["wakeup_state"] == "queued"

    @pytest.mark.asyncio
    async def test_resolve_feedback_posts_operator_reply_note(
        self, client: AsyncClient, db_session
    ):
        """Resolving FEEDBACK creates a durable acknowledgment linked to the original."""
        project = await _make_project(db_session)
        feedback = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.FEEDBACK,
            from_type="user",
            content="Please make the deploy summary clearer.",
            status="open",
        )
        db_session.add(feedback)
        await db_session.commit()
        await db_session.refresh(feedback)

        resp = await client.patch(
            f"/api/admin/messages/{feedback.id}",
            json={
                "resolution": "Deploy summary now cites the active release.",
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                    "selected_refs": [
                        {
                            "kind": "release",
                            "id": "release:v2",
                            "label": "v2 release",
                        }
                    ],
                },
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["arming"] == "steering"
        assert body["wakeup_state"] == "queued"
        assert body["message"]["id"] == str(feedback.id)
        assert body["message"]["status"] == "resolved"
        assert body["message"]["resolution"] == "Deploy summary now cites the active release."
        await db_session.refresh(feedback)
        assert feedback.status == "resolved"
        assert feedback.resolution == "Deploy summary now cites the active release."

        notes = (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                    ProjectMessage.from_type == "user",
                )
            )
        ).scalars().all()
        assert len(notes) == 1
        note = notes[0]
        assert note.actor_user_id == TEST_USER_ID
        assert note.context.get("event_type") == "feedback_reply"
        assert note.context.get("_operator_wakeup_state") == body["wakeup_state"]
        assert note.context.get("reply_to") == str(feedback.id)
        assert (
            note.context.get("resolution")
            == "Deploy summary now cites the active release."
        )
        assert "strategic_session" not in note.context
        assert body["reply_message_id"] == str(note.id)
        assert body["reply_message"]["id"] == str(note.id)
        assert body["reply_message"]["message_type"] == "NOTE"
        assert body["reply_message"]["context"]["event_type"] == "feedback_reply"
        assert body["reply_message"]["content"] == "Deploy summary now cites the active release."

    @pytest.mark.asyncio
    async def test_delete_message(self, client: AsyncClient, db_session):
        """Can delete an existing message."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Bug found",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        resp = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == str(msg.id)
        assert body["status"] == "deleted"
        assert body["message"]["id"] == str(msg.id)
        assert body["message"]["message_type"] == "BUG_REPORT"
        assert body["message"]["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_message_not_found_is_idempotent(self, client: AsyncClient):
        """Deleting a non-existent message returns idempotent success."""
        missing_id = uuid.uuid4()
        resp = await client.delete(f"/api/admin/messages/{missing_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == str(missing_id)
        assert body["status"] == "not_found"
        assert "message" not in body

    @pytest.mark.asyncio
    async def test_delete_message_twice_is_idempotent(
        self, client: AsyncClient, db_session
    ):
        """Deleting a message twice (retry) is idempotent."""
        project = await _make_project(db_session)
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            from_type="user",
            content="Bug found",
            status="open",
        )
        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        # First delete
        resp1 = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp1.status_code == 200
        assert resp1.json()["deleted"] is True
        assert resp1.json()["status"] == "deleted"

        # Retry delete
        resp2 = await client.delete(f"/api/admin/messages/{msg.id}")
        assert resp2.status_code == 200
        assert resp2.json()["deleted"] is True
        assert resp2.json()["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_list_messages_status_filter(self, client: AsyncClient, db_session):
        """Status filter separates open from resolved messages."""
        project = await _make_project(db_session)
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="Open msg",
                status="open",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="Resolved msg",
                status="resolved",
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open"
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["messages"][0]["status"] == "open"

    @pytest.mark.asyncio
    async def test_list_messages_invalid_status_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Invalid status filter value returns 400."""
        project = await _make_project(db_session)

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=pending"
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_messages_needs_action_scope(
        self, client: AsyncClient, db_session
    ):
        """needs_action scope returns only QUESTION/BLOCKER with non-resolved status."""
        project = await _make_project(db_session)
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.QUESTION,
                from_type="user",
                content="Open question",
                status="open",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.BLOCKER,
                from_type="system",
                content="Open blocker",
                status="open",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.BLOCKER,
                from_type="system",
                content="Resolved blocker",
                status="resolved",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="Open note",
                status="open",
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=needs_action"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        returned_types = {m["message_type"] for m in data["messages"]}
        assert returned_types == {"QUESTION", "BLOCKER"}
        returned_contents = {m["content"] for m in data["messages"]}
        assert "Resolved blocker" not in returned_contents
        assert "Open note" not in returned_contents

    @pytest.mark.asyncio
    async def test_list_messages_needs_action_linked_included(
        self, client: AsyncClient, db_session
    ):
        """needs_action scope includes BLOCKER in 'linked' status (linked != resolved)."""
        project = await _make_project(db_session)
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.BLOCKER,
                from_type="system",
                content="Linked blocker",
                status="linked",
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=needs_action"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["messages"][0]["content"] == "Linked blocker"
        assert data["messages"][0]["status"] == "linked"

    @pytest.mark.asyncio
    async def test_list_messages_needs_action_invalid_still_rejects(
        self, client: AsyncClient, db_session
    ):
        """Unrecognized status values still return 400 after needs_action was added."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=foobar"
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_messages_guarantees_high_signal_with_routine_flood(
        self, client: AsyncClient, db_session
    ):
        """BLOCKER messages are always returned even when routine messages fill the paged limit."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        # BLOCKER is the OLDEST message: with order_by(created_at.desc()) and limit=3 the
        # five newer routine rows would evict it entirely unless the guaranteed-query logic
        # in list_messages explicitly pulls it back in. This ordering is what makes the test
        # actually exercise the guarantee — a newest BLOCKER would survive on recency alone.
        blocker = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="critical blocker",
            context={},
        )
        blocker.created_at = now - timedelta(hours=10)
        db_session.add(blocker)
        for i in range(5):
            routine = ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"routine {i}",
                context={"event_type": "pr_merged"},
            )
            routine.created_at = now - timedelta(minutes=5 * (5 - i))
            db_session.add(routine)
        await db_session.commit()

        # limit=3 would normally cut off the BLOCKER if 5 newer routine messages exist
        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open&limit=3"
        )
        assert resp.status_code == 200
        returned_ids = {m["id"] for m in resp.json()["messages"]}
        assert str(blocker.id) in returned_ids

    @pytest.mark.parametrize(
        "high_signal_event_type",
        [
            "failure_alert",
            "rollback_recommended",
            "fix_loop_divergence_regressed",
            "release_calcifying",
            "release_build_stalled",
            "release_stabilize_stalled",
            "release_ci_unresolved_stall",
            "strategic_steering_deferred",
        ],
    )
    async def test_list_messages_guarantees_high_signal_notes_with_routine_flood(
        self, client: AsyncClient, db_session, high_signal_event_type: str
    ):
        """System NOTE alerts classified as high-signal always survive a routine flood."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        # High-signal NOTE is the OLDEST message so it would be evicted by paging
        # alone without the guaranteed-query logic.
        alert = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=f"system alert: {high_signal_event_type}",
            context={"event_type": high_signal_event_type},
        )
        alert.created_at = now - timedelta(hours=10)
        db_session.add(alert)
        for i in range(5):
            routine = ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content=f"routine {i}",
                context={"event_type": "pr_merged"},
            )
            routine.created_at = now - timedelta(minutes=5 * (5 - i))
            db_session.add(routine)
        await db_session.commit()

        # limit=3 would normally cut off the alert if 5 newer routine messages exist
        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=open&limit=3"
        )
        assert resp.status_code == 200
        returned_ids = {m["id"] for m in resp.json()["messages"]}
        assert str(alert.id) in returned_ids, (
            f"high-signal NOTE with event_type={high_signal_event_type!r} was evicted "
            "by routine flood — guaranteed-query logic did not include it"
        )


    @pytest.mark.asyncio
    async def test_list_messages_server_side_search_scoped_to_projects(
        self, client: AsyncClient, db_session
    ):
        """Server-side search (q=) filters by content and is scoped to the user's projects.

        Verifies that:
        - Only messages whose content contains the search term are returned.
        - Messages from a project the caller does not belong to are never returned,
          even if their content matches the search term.
        """
        project = await _make_project(db_session)

        # Two messages in the user's project — only one matches the search term.
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="deploy the authentication service",
                status="open",
            )
        )
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="unrelated routine update",
                status="open",
            )
        )

        # A message in a different project (user is NOT a member) — must never appear.
        other_project = Project(name="Other Project", user_id="other-user-999")
        db_session.add(other_project)
        await db_session.flush()
        db_session.add(
            ProjectMessage(
                project_id=other_project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content="deploy the authentication service in other project",
                status="open",
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages?project_id={project.id}&status=all&q=authentication"
        )
        assert resp.status_code == 200
        data = resp.json()
        contents = [m["content"] for m in data["messages"]]
        # Only the matching message from the user's project is returned.
        assert len(contents) == 1
        assert "authentication" in contents[0]
        assert all(m["project_id"] == str(project.id) for m in data["messages"])

    @pytest.mark.asyncio
    async def test_strategic_session_message_idempotency_preserves_response_shape_and_broadcast(
        self, client: AsyncClient, db_session
    ):
        """Strategic-session message retries return the existing ProjectMessage."""
        from sqlalchemy import func as sa_func

        project = await _make_project(db_session)
        body = {
            "message_type": "NOTE",
            "content": "Keep this visible in the Strategic workbench.",
            "idempotency_key": "strategic-session-message-idem-1",
        }

        with patch(
            "app.routers.admin_project_strategic_session.broadcast_to_project",
            new_callable=AsyncMock,
        ) as broadcast:
            first = await client.post(
                f"/api/admin/projects/{project.id}/strategic-session/messages",
                json=body,
            )
            second = await client.post(
                f"/api/admin/projects/{project.id}/strategic-session/messages",
                json=body,
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_body = first.json()
        second_body = second.json()
        assert first_body["status"] == "created"
        assert first_body["arming"] == "passive_note"
        assert first_body["message"]["id"] == first_body["id"]
        assert first_body["message"]["message_type"] == "NOTE"
        assert first_body["message"]["content"] == body["content"]
        assert second_body["status"] == "duplicate"
        assert second_body["arming"] == "duplicate"
        assert second_body["id"] == first_body["id"]
        assert second_body["message"]["id"] == first_body["id"]

        count = await db_session.scalar(
            select(sa_func.count())
            .select_from(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
        )
        assert count == 1
        broadcast.assert_awaited_once()
        assert broadcast.await_args.args[:2] == (
            str(project.id),
            "message_created",
        )
        _assert_message_event_payload(
            broadcast.await_args.args[2],
            project_id=project.id,
            message_id=first_body["id"],
            message_type="NOTE",
            from_type="user",
            action="created",
            excerpt="Keep this visible in the Strategic workbench.",
        )

    @pytest.mark.asyncio
    async def test_strategic_session_message_persists_workbench_snapshot_and_arms(
        self, client: AsyncClient, db_session
    ):
        """Strategic messages persist workbench state without hiding it in chat."""
        project = await _make_project(db_session)
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/messages",
            json={
                "message_type": "FEEDBACK",
                "content": "Prioritize the release blocker.",
                "context": {"event_type": "operator_supplied", "reply_to": "m-1"},
                "workbench_context": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                    "session_started_at": "2026-06-29T12:00:00Z",
                    "context_package_id": "strategic-context:test",
                    "selected_refs": [
                        {"kind": "task", "id": "task-1", "label": "Release blocker"},
                        {"kind": "task", "id": "task-1", "label": "Duplicate"},
                    ],
                },
                "idempotency_key": "strategic-session-message-workbench-1",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["arming"] == "steering"
        assert body["message"]["id"] == body["id"]
        assert body["message"]["message_type"] == "FEEDBACK"
        assert "strategic_session" not in body["message"]["context"]

        msg = await db_session.get(ProjectMessage, uuid.UUID(body["id"]))
        assert msg is not None
        context = msg.context or {}
        assert "reply_to" not in context
        assert "user_event_type" not in context
        assert "event_type" not in context
        assert "strategic_session" not in context

        prefs_row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        stored_workbench = prefs_row.prefs["_strategic_workbench"]
        assert stored_workbench["interaction_mode"] == "continuous"
        assert stored_workbench["active_surface"] == "pipeline"
        assert stored_workbench["selected_refs"] == [
            {"kind": "task", "id": "task-1", "label": "Release blocker"}
        ]
        assert "context_package_id" not in stored_workbench

        vision = await self._reload_vision(db_session, project.id)
        marker = vision.get("_strategic_refresh")
        assert marker["origin"] == "operator"
        assert marker["reason"] == "operator_message"
        assert marker["excerpt"] == "Prioritize the release blocker."

    @pytest.mark.asyncio
    async def test_strategic_session_message_allows_attachment_only_content(
        self, client: AsyncClient, db_session
    ):
        """Attachment-only strategic chat turns persist with a small placeholder."""
        from app.models import ProjectMessageAttachmentUpload
        from app.project_message_projection import project_message_base

        project = await _make_project(db_session)
        attachment_id = uuid.uuid4()
        attachment = {
            "attachment_id": str(attachment_id),
            "type": "image",
            "mime_type": "image/png",
            "name": "screenshot.png",
            "size_bytes": 4,
            "storage_uri": (
                "gs://blueprint-agent-artifacts/message-attachments/"
                f"{project.id}/{attachment_id}/screenshot.png"
            ),
        }
        db_session.add(ProjectMessageAttachmentUpload(
            attachment_id=attachment_id,
            project_id=project.id,
            actor_user_id=TEST_USER_ID,
            storage_uri=attachment["storage_uri"],
            filename="screenshot.png",
            content_type="image/png",
            expected_size_bytes=4,
            expires_at=_utcnow() + timedelta(minutes=15),
        ))
        await db_session.commit()

        async def _preview(_session, _project_id, _state_response, message):
            return project_message_base(message)

        session_response = {
            "project_id": str(project.id),
            "status": "idle",
            "location": "none",
            "status_reason": None,
            "session": {},
            "workbench": None,
            "strategic": {},
            "runtime": {},
            "context_package": None,
            "conversation": {"items": [], "limit": 20},
            "recent_outputs": [],
        }

        with (
            patch(
                "app.routers.admin_project_strategic_session.get_project_strategic_state",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.routers.admin_project_strategic_session._message_preview_for_session_state",
                new=AsyncMock(side_effect=_preview),
            ),
            patch(
                "app.routers.admin_project_strategic_session._persist_message_preview_strategic_session_descriptors",
                new=AsyncMock(),
            ),
            patch(
                "app.routers.admin_project_strategic_session._build_strategic_session_response",
                new=AsyncMock(return_value=session_response),
            ),
            patch(
                "app.routers.admin_project_strategic_session.broadcast_to_project",
                new=AsyncMock(),
            ),
        ):
            resp = await client.post(
                f"/api/admin/projects/{project.id}/strategic-session/messages",
                json={
                    "message_type": "NOTE",
                    "content": "",
                    "attachments": [attachment],
                    "idempotency_key": "strategic-session-message-attachment-only-1",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["message"]["content"] == "Sent attachment."
        assert body["message"]["context"]["attachments"] == [{
            "attachment_id": str(attachment_id),
            "type": "image",
            "mime_type": "image/png",
            "name": "screenshot.png",
            "filename": "screenshot.png",
            "size_bytes": 4,
        }]

        msg = await db_session.get(ProjectMessage, uuid.UUID(body["id"]))
        assert msg is not None
        assert msg.content == "Sent attachment."
        assert (msg.context or {})["attachments"] == [{
            **attachment,
            "filename": "screenshot.png",
        }]

    @pytest.mark.asyncio
    async def test_get_message_counts_empty(self, client: AsyncClient, db_session):
        """New project returns zero counts for all types."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocker"] == 0
        assert data["question"] == 0
        assert data["bug_report"] == 0
        assert data["feedback"] == 0
        assert data["unread"] == 0

    @pytest.mark.asyncio
    async def test_get_message_counts_by_type(self, client: AsyncClient, db_session):
        """Counts return correct per-type totals for open high-signal messages."""
        project = await _make_project(db_session)
        for mtype in (MessageType.BLOCKER, MessageType.BLOCKER, MessageType.QUESTION):
            db_session.add(ProjectMessage(
                project_id=project.id,
                message_type=mtype,
                from_type="system",
                content="test",
                status="open",
            ))
        # User-authored message should be excluded from counts (from_type="user")
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="user",
            content="user msg",
            status="open",
        ))
        # Resolved message should be excluded
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="resolved",
            status="resolved",
        ))
        await db_session.commit()

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocker"] == 2
        assert data["question"] == 1
        assert data["bug_report"] == 0
        assert data["feedback"] == 0

    @pytest.mark.asyncio
    async def test_get_message_counts_excludes_internal_rows_from_counts_and_unread(
        self, client: AsyncClient, db_session
    ):
        """Internal high-signal rows cannot inflate the operator badge."""
        from app.notification_dispatch import HIGH_SIGNAL_NOTE_EVENT_TYPES

        project = await _make_project(db_session)
        internal_context = {"audience": "strategist-internal"}
        for message_type in (
            MessageType.QUESTION,
            MessageType.BLOCKER,
            MessageType.BUG_REPORT,
            MessageType.FEEDBACK,
        ):
            db_session.add(ProjectMessage(
                project_id=project.id,
                message_type=message_type,
                from_type="system",
                content=f"internal {message_type.value}",
                status="open",
                context=internal_context,
            ))
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="internal high-signal note",
            status="open",
            context={
                "audience": "strategist-internal",
                "event_type": next(iter(HIGH_SIGNAL_NOTE_EVENT_TYPES)),
            },
        ))
        visible_question = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="legacy visible question",
            status="open",
        )
        db_session.add(visible_question)
        await db_session.commit()

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "blocker": 0,
            "question": 1,
            "bug_report": 0,
            "feedback": 0,
            "high_signal_note": 0,
            "unread": 1,
            "needs_operator": 1,
            "active_alerts": 0,
        }

    @pytest.mark.asyncio
    async def test_get_message_counts_high_signal_note(self, client: AsyncClient, db_session):
        """NOTE messages with HIGH_SIGNAL_NOTE_EVENT_TYPES event_type are counted in high_signal_note."""
        from app.notification_dispatch import HIGH_SIGNAL_NOTE_EVENT_TYPES
        from app.message_audience import route_event

        project = await _make_project(db_session)
        hs_event = next(iter(HIGH_SIGNAL_NOTE_EVENT_TYPES))  # pick any valid high-signal event type

        # High-signal NOTE — should be counted; stamp audience so nonactionable filter passes.
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="high signal note",
            status="open",
            context={"event_type": hs_event, "audience": route_event(hs_event)},
        ))
        # NOTE with non-high-signal event_type — should NOT be counted
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="routine note",
            status="open",
            context={"event_type": "some_low_signal_event", "audience": "operator-fyi"},
        ))
        # Resolved high-signal NOTE — should NOT be counted
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="resolved high signal note",
            status="resolved",
            context={"event_type": hs_event, "audience": route_event(hs_event)},
        ))
        await db_session.commit()

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["high_signal_note"] == 1, f"expected 1 high_signal_note, got {data['high_signal_note']}"

    @pytest.mark.asyncio
    async def test_get_message_counts_active_alerts(self, client: AsyncClient, db_session):
        """active_alerts counts undismissed ERROR and WARNING alert rows."""
        from app.models import Alert, AlertSeverity

        project = await _make_project(db_session)
        # Use an operator-visible event_type so operator_visible_alert_filter passes.
        _op_event = "failure_alert"

        # ERROR alert — should be counted
        db_session.add(Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="test error",
            message="test error alert",
            event_type=_op_event,
        ))
        # WARNING alert — should be counted
        db_session.add(Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title="test warning",
            message="test warning alert",
            event_type=_op_event,
        ))
        # Dismissed ERROR alert — should NOT be counted
        db_session.add(Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="dismissed error",
            message="dismissed error alert",
            event_type=_op_event,
            dismissed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        # INFO alert — should NOT be counted (only ERROR and WARNING)
        db_session.add(Alert(
            project_id=project.id,
            severity=AlertSeverity.INFO,
            title="test info",
            message="test info alert",
            event_type=_op_event,
        ))
        await db_session.commit()

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_alerts"] == 2, (
            f"expected 2 active_alerts (ERROR + WARNING), got {data['active_alerts']}"
        )

    @pytest.mark.asyncio
    async def test_get_message_counts_noise_prefs_muted_event_excluded(self, client: AsyncClient, db_session):
        """high_signal_note excludes NOTE messages whose event_type is muted in operator noise prefs."""
        from app.notification_dispatch import HIGH_SIGNAL_NOTE_EVENT_TYPES
        from app.message_audience import route_event

        project = await _make_project(db_session)
        event_types = list(HIGH_SIGNAL_NOTE_EVENT_TYPES)
        muted_event = event_types[0]
        visible_event = event_types[1] if len(event_types) > 1 else event_types[0]

        # Create noise prefs row muting muted_event for the test user
        db_session.add(MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={muted_event: "mute"},
        ))
        # NOTE whose event_type is muted — should be excluded
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="muted high signal note",
            status="open",
            context={"event_type": muted_event, "audience": route_event(muted_event)},
        ))
        if visible_event != muted_event:
            # NOTE whose event_type is NOT muted — should be included
            db_session.add(ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="system",
                content="visible high signal note",
                status="open",
                context={"event_type": visible_event, "audience": route_event(visible_event)},
            ))
        await db_session.commit()

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        expected = 0 if muted_event == visible_event else 1
        assert data["high_signal_note"] == expected, (
            f"muted event {muted_event!r} should be excluded; got high_signal_note={data['high_signal_note']}"
        )

    @pytest.mark.asyncio
    async def test_get_message_counts_cross_device_last_seen(self, client: AsyncClient, db_session):
        """When last_seen_ms is omitted, server-stored MessagesLastSeen drives the unread count."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old_ts = now - timedelta(hours=2)
        new_ts = now - timedelta(minutes=5)
        one_hour_ago = now - timedelta(hours=1)

        # Store server-side last-seen as 1 hour ago
        db_session.add(MessagesLastSeen(
            user_id=TEST_USER_ID,
            project_id=project.id,
            last_seen_at=one_hour_ago,
        ))
        # Old message — before server last_seen, should NOT be unread
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="old blocker",
            status="open",
            created_at=old_ts,
        ))
        # New message — after server last_seen, SHOULD be unread
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="new question",
            status="open",
            created_at=new_ts,
        ))
        await db_session.commit()

        # No last_seen_ms param — server-stored value should be used
        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["unread"] == 1, (
            f"server-stored last_seen should exclude old message; got unread={data['unread']}"
        )

    @pytest.mark.asyncio
    async def test_get_message_counts_unread_with_last_seen_ms(self, client: AsyncClient, db_session):
        """unread count returns only messages newer than last_seen_ms."""
        from datetime import datetime, timedelta, timezone

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old_ts = now - timedelta(hours=2)
        new_ts = now - timedelta(minutes=5)

        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="old blocker",
            status="open",
            created_at=old_ts,
        ))
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="new question",
            status="open",
            created_at=new_ts,
        ))
        await db_session.commit()

        # last_seen_ms set to 1 hour ago — only the new question is unread
        one_hour_ago_ms = int((now - timedelta(hours=1)).timestamp() * 1000)
        resp = await client.get(
            f"/api/admin/messages/counts?project_id={project.id}&last_seen_ms={one_hour_ago_ms}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocker"] == 1
        assert data["question"] == 1
        assert data["unread"] == 1  # only the new question

    @pytest.mark.asyncio
    async def test_get_message_counts_invalid_project_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Non-existent project returns 404."""
        resp = await client.get(
            f"/api/admin/messages/counts?project_id={uuid.uuid4()}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_message_counts_invalid_project_id_format_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Invalid UUID format returns 400."""
        resp = await client.get("/api/admin/messages/counts?project_id=not-a-uuid")
        assert resp.status_code == 400


class TestMessagesLastSeen:
    @pytest.mark.asyncio
    async def test_get_last_seen_empty(self, client: AsyncClient, db_session):
        """Returns null when no last-seen record exists for the operator."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] is None

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores the timestamp; subsequent GET returns the same value."""
        project = await _make_project(db_session)
        ts = 1_700_000_000_000  # arbitrary ms epoch

        put_resp = await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["last_seen_at"] == ts

        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["last_seen_at"] == ts

    @pytest.mark.asyncio
    async def test_get_last_seen_treats_persisted_naive_datetime_as_utc(
        self, client: AsyncClient, db_session
    ):
        """NaiveUTCDateTime rows are UTC, not host-local wall-clock values."""
        project = await _make_project(db_session)
        db_session.add(
            MessagesLastSeen(
                user_id=TEST_USER_ID,
                project_id=project.id,
                last_seen_at=datetime(1970, 1, 1),
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )

        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] == 0

    @pytest.mark.asyncio
    async def test_update_advances_timestamp(self, client: AsyncClient, db_session):
        """A second PUT overwrites the previous value."""
        project = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": ts2},
        )
        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )
        assert get_resp.json()["last_seen_at"] == ts2

    @pytest.mark.asyncio
    async def test_stale_update_does_not_regress_timestamp(
        self, client: AsyncClient, db_session
    ):
        """A delayed older PUT cannot move last-seen backwards and re-mark read messages unread."""
        project = await _make_project(db_session)
        older, newer = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": newer},
        )
        stale_resp = await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(project.id), "timestamp": older},
        )
        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={project.id}"
        )

        assert stale_resp.status_code == 200
        assert stale_resp.json()["last_seen_at"] == newer
        assert get_resp.json()["last_seen_at"] == newer

    @pytest.mark.asyncio
    async def test_stale_identity_map_update_does_not_regress_timestamp(
        self, db_session
    ):
        """A stale session cannot overwrite a newer committed last-seen value."""
        from conftest import TestingSessionLocal
        from app.messages_last_seen import upsert_messages_last_seen

        project = await _make_project(db_session)
        initial = datetime(2026, 1, 1, 12, 0, 0)
        stale_ts = initial + timedelta(minutes=5)
        newer_ts = initial + timedelta(minutes=10)
        db_session.add(
            MessagesLastSeen(
                user_id=TEST_USER_ID,
                project_id=project.id,
                last_seen_at=initial,
            )
        )
        await db_session.commit()

        async with TestingSessionLocal() as stale_session:
            stale_row = (
                await stale_session.execute(
                    select(MessagesLastSeen).where(
                        MessagesLastSeen.user_id == TEST_USER_ID,
                        MessagesLastSeen.project_id == project.id,
                    )
                )
            ).scalar_one()
            assert stale_row.last_seen_at == initial
            await stale_session.commit()

            async with TestingSessionLocal() as fresh_session:
                stored = await upsert_messages_last_seen(
                    fresh_session, TEST_USER_ID, project.id, newer_ts
                )
                await fresh_session.commit()
                assert stored == newer_ts

            stored = await upsert_messages_last_seen(
                stale_session, TEST_USER_ID, project.id, stale_ts
            )
            await stale_session.commit()

        async with TestingSessionLocal() as verify_session:
            final_ts = (
                await verify_session.execute(
                    select(MessagesLastSeen.last_seen_at).where(
                        MessagesLastSeen.user_id == TEST_USER_ID,
                        MessagesLastSeen.project_id == project.id,
                    )
                )
            ).scalar_one()
        assert stored == newer_ts
        assert final_ts == newer_ts

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot read or write last-seen for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/last-seen?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": random_pid, "timestamp": 1_700_000_000_000},
        )
        assert put_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_without_project_id_returns_null_when_empty(
        self, client: AsyncClient, db_session
    ):
        """GET without project_id returns null when no last-seen records exist."""
        resp = await client.get("/api/admin/messages/last-seen")
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] is None

    @pytest.mark.asyncio
    async def test_get_without_project_id_returns_max_across_projects(
        self, client: AsyncClient, db_session
    ):
        """GET without project_id returns max last-seen across all operator projects."""
        p1 = await _make_project(db_session)
        p2 = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(p1.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/messages/last-seen",
            json={"project_id": str(p2.id), "timestamp": ts2},
        )

        resp = await client.get("/api/admin/messages/last-seen")
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] == ts2


class TestLastVisitTimestamp:
    @pytest.mark.asyncio
    async def test_get_empty_returns_null(self, client: AsyncClient, db_session):
        """Returns null when no MessagesLastSeen record exists for the operator."""
        resp = await client.get("/api/admin/last-visit-timestamp")
        assert resp.status_code == 200
        assert resp.json()["last_visit_at"] is None

    @pytest.mark.asyncio
    async def test_put_and_get_round_trip(self, client: AsyncClient, db_session):
        """PUT persists the timestamp; GET returns it as the cross-project max."""
        project = await _make_project(db_session)
        ts = 1_700_000_000_000

        put_resp = await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(project.id), "timestamp": ts},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["last_visit_at"] == ts

        get_resp = await client.get("/api/admin/last-visit-timestamp")
        assert get_resp.status_code == 200
        assert get_resp.json()["last_visit_at"] == ts

    @pytest.mark.asyncio
    async def test_get_returns_max_across_projects(
        self, client: AsyncClient, db_session
    ):
        """GET returns the most recent timestamp across all user's projects."""
        p1 = await _make_project(db_session)
        p2 = await _make_project(db_session)
        ts1, ts2 = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(p1.id), "timestamp": ts1},
        )
        await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(p2.id), "timestamp": ts2},
        )

        resp = await client.get("/api/admin/last-visit-timestamp")
        assert resp.json()["last_visit_at"] == ts2

    @pytest.mark.asyncio
    async def test_put_does_not_regress_existing_last_visit_timestamp(
        self, client: AsyncClient, db_session
    ):
        """The home-panel last-visit writer shares the monotonic MessagesLastSeen contract."""
        project = await _make_project(db_session)
        older, newer = 1_700_000_000_000, 1_700_000_060_000

        await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(project.id), "timestamp": newer},
        )
        stale_resp = await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": str(project.id), "timestamp": older},
        )
        get_resp = await client.get("/api/admin/last-visit-timestamp")

        assert stale_resp.status_code == 200
        assert stale_resp.json()["last_visit_at"] == newer
        assert get_resp.json()["last_visit_at"] == newer

    @pytest.mark.asyncio
    async def test_put_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot write last-visit-timestamp for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        resp = await client.put(
            "/api/admin/last-visit-timestamp",
            json={"project_id": random_pid, "timestamp": 1_700_000_000_000},
        )
        assert resp.status_code == 404


class TestMessagesNoisePrefs:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns empty dict when no noise-prefs record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["prefs"] == {}

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores event-visibility prefs; GET returns them."""
        project = await _make_project(db_session)
        prefs = {"pr_merged": "mute", "cycle_summary": "routine"}
        put_resp = await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": str(project.id), "prefs": prefs},
        )
        assert put_resp.status_code == 200
        get_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert get_resp.json()["prefs"] == prefs

    @pytest.mark.asyncio
    async def test_noise_prefs_save_preserves_temp_mutes_and_mute_window(
        self, client: AsyncClient, db_session
    ):
        """A visibility-only noise-prefs save must NOT clobber an operator's
        server-side temp-mutes or mute-window stored in the same prefs column."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 3_600_000

        # Operator sets a DND window and a temp-mute first.
        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        await client.put(
            "/api/admin/messages/temp-mutes",
            json={
                "project_id": str(project.id),
                "temp_mutes": {"pr_merged": future_ms},
            },
        )

        # Then toggles an event-visibility pref (sends only the visibility map).
        await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": str(project.id), "prefs": {"cycle_summary": "routine"}},
        )

        # The DND window and temp-mute must survive the visibility save.
        mw_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert mw_resp.json()["mute_window"] == {"expires_at": future_ms}
        tm_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert tm_resp.json()["temp_mutes"] == {"pr_merged": future_ms}

        # And the visibility pref itself round-trips.
        np_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert np_resp.json()["prefs"].get("cycle_summary") == "routine"

    @pytest.mark.asyncio
    async def test_noise_prefs_payload_cannot_write_reserved_keys(
        self, client: AsyncClient, db_session
    ):
        """Reserved keys in a noise-prefs payload are ignored, not persisted as
        a fake mute-window/temp-mutes that the dedicated endpoints would honor."""
        project = await _make_project(db_session)
        await client.put(
            "/api/admin/messages/noise-prefs",
            json={
                "project_id": str(project.id),
                "prefs": {
                    "pr_merged": "mute",
                    "_mute_window": {"expires_at": 9_999_999_999_999},
                    "_temp_mutes": {"cycle_summary": 9_999_999_999_999},
                },
            },
        )
        mw_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert mw_resp.json()["mute_window"] is None
        tm_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert tm_resp.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={random_pid}"
        )
        assert get_resp.status_code == 404
        put_resp = await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": random_pid, "prefs": {}},
        )
        assert put_resp.status_code == 404


class TestMessagesWorkbenchState:
    @pytest.mark.asyncio
    async def test_get_empty_returns_defaults(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["workbench"] == {
            "version": 1,
            "interaction_mode": "chat",
            "active_surface": "strategic",
            "selected_refs": [],
            "session_started_at": None,
            "last_interaction_at": None,
        }

    @pytest.mark.asyncio
    async def test_round_trip_sanitizes_and_timestamps(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        payload = {
            "interaction_mode": "continuous",
            "active_surface": "pipeline",
            "selected_refs": [
                {"kind": "task", "id": "task-1", "label": "Implement work session"},
                {"kind": "message", "id": "msg-1", "label": "Operator steer"},
                {"kind": "", "id": "ignored"},
            ],
        }
        with patch("app.workbench_events.broadcast_to_project", new=AsyncMock()) as bcast:
            put_resp = await client.put(
                "/api/admin/messages/workbench-state",
                json={"project_id": str(project.id), "workbench": payload},
            )
        assert put_resp.status_code == 200
        bcast.assert_awaited_once()
        assert bcast.await_args.args[:2] == (str(project.id), "workbench_refetch")
        event_payload = bcast.await_args.args[2]
        assert event_payload["resource"] == "strategic_workbench"
        assert event_payload["action"] == "updated"
        assert event_payload["refetch"] == {
            "surfaces": [SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME],
        }
        assert event_payload["workbench"]["interaction_mode"] == "continuous"
        assert event_payload["workbench"]["active_surface"] == "pipeline"
        assert event_payload["workbench"]["arming"] == "steering"
        assert put_resp.json()["arming"] == "steering"
        stored = put_resp.json()["workbench"]
        assert stored["interaction_mode"] == "continuous"
        assert stored["active_surface"] == "pipeline"
        assert stored["selected_refs"] == [
            {"kind": "task", "id": "task-1", "label": "Implement work session"},
            {"kind": "message", "id": "msg-1", "label": "Operator steer"},
        ]
        assert stored["session_started_at"]
        assert stored["last_interaction_at"]

        get_resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["workbench"] == stored

    @pytest.mark.asyncio
    async def test_entering_active_mode_arms_strategic_refresh(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                    "selected_refs": [
                        {
                            "kind": "direction",
                            "id": "d-workbench",
                            "label": "Operator workbench",
                        }
                    ],
                },
            },
        )

        assert resp.status_code == 200
        assert resp.json()["arming"] == "steering"
        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker["origin"] == "operator"
        assert marker["reason"] == "workbench_interaction_mode"
        assert marker["operator_count"] == 1
        assert "workday mode" in marker["excerpt"]
        assert "Operator workbench" in marker["excerpt"]

    # ── Role floor ────────────────────────────────────────────────────────────
    #
    # PUT /messages/workbench-state looks like the five sibling PUT /messages/*
    # routes (last-seen, noise-prefs, saved-views, temp-mutes, mute-window) that
    # ARE per-user personal UI state — each keyed by a
    # UniqueConstraint("user_id","project_id") — and stay viewer-writable.
    #
    # It is not one of them. persist_strategic_workbench_update takes
    # with_vision_locked() to read-modify-write the PROJECT-GLOBAL Project.vision
    # column, and on a client-supplied interaction_mode transition arms a sticky,
    # breaker-bypassing, cooldown-bypassing strategic refresh with
    # origin=ORIGIN_OPERATOR (see test_entering_active_mode_arms_strategic_refresh
    # directly above). Without the member+ floor, a VIEWER can steer the
    # autonomous strategist.

    @pytest.mark.asyncio
    async def test_viewer_cannot_persist_workbench_state(
        self, client: AsyncClient, db_session
    ):
        """Viewer → 403, and no operator-origin refresh armed in Project.vision."""
        project = await _make_project(
            db_session, name="Viewer Workbench", role="viewer"
        )

        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                    "selected_refs": [],
                },
            },
        )

        assert resp.status_code == 403, resp.text
        assert "member" in resp.json()["error"]["message"].lower()

        # The project-global vision column must be untouched: no viewer-supplied
        # steering excerpt, no operator-origin refresh armed.
        await db_session.refresh(project)
        assert (project.vision or {}).get("_strategic_refresh") is None

    @pytest.mark.asyncio
    async def test_member_can_persist_workbench_state(
        self, client: AsyncClient, db_session
    ):
        """The floor must not over-reject: a member (not owner) can still steer."""
        project = await _make_project(
            db_session, name="Member Workbench", role="member"
        )

        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                    "selected_refs": [],
                },
            },
        )

        assert resp.status_code == 200, resp.text
        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker is not None
        assert marker["origin"] == "operator"

    @pytest.mark.asyncio
    async def test_viewer_can_still_read_workbench_state(
        self, client: AsyncClient, db_session
    ):
        """The GET is a read — the floor must not break viewer read access."""
        project = await _make_project(
            db_session, name="Viewer Workbench Read", role="viewer"
        )

        resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={project.id}"
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_repeating_same_active_mode_does_not_rearm(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        first = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                },
            },
        )
        second = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "pipeline",
                },
            },
        )

        assert first.status_code == 200
        assert first.json()["arming"] == "steering"
        assert second.status_code == 200
        assert second.json()["arming"] == "passive_note"
        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker["reason"] == "workbench_interaction_mode"
        assert marker["operator_count"] == 1

    @pytest.mark.asyncio
    async def test_surface_only_update_after_active_mode_does_not_rearm(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        first = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                },
            },
        )
        second = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "active_surface": "runtime",
                },
            },
        )

        assert first.status_code == 200
        assert first.json()["arming"] == "steering"
        assert second.status_code == 200
        assert second.json()["arming"] == "passive_note"
        stored = second.json()["workbench"]
        assert stored["interaction_mode"] == "continuous"
        assert stored["active_surface"] == "runtime"
        await db_session.refresh(project)
        marker = (project.vision or {}).get("_strategic_refresh")
        assert marker["reason"] == "workbench_interaction_mode"
        assert marker["operator_count"] == 1

    @pytest.mark.asyncio
    async def test_continuous_mode_writes_local_continuation_marker(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="m" * 64,
            hostname="desk-mini",
            status="active",
            last_seen_at=_utcnow(),
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)
        preferred_runner_id = str(runner.id)
        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "runtime",
                    "preferred_runner_id": preferred_runner_id,
                },
            },
        )

        assert resp.status_code == 200
        assert resp.json()["arming"] == "steering"
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["active"] is True
        assert marker["operator_user_id"] == TEST_USER_ID
        assert marker["preferred_runner_id"] == preferred_runner_id
        assert marker["last_seen_at"]
        assert marker["updated_at"]

    @pytest.mark.asyncio
    async def test_continuous_mode_drops_unknown_preferred_runner_id(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

        project = await _make_project(db_session)
        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "runtime",
                    "preferred_runner_id": str(uuid.uuid4()),
                },
            },
        )

        assert resp.status_code == 200
        assert "preferred_runner_id" not in resp.json()["workbench"]
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["active"] is True
        assert "preferred_runner_id" not in marker

    @pytest.mark.asyncio
    async def test_list_runners_returns_all_with_explicit_limit(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runners = [
            LocalRunner(
                project_id=project.id,
                token_hash=f"{i:064x}",
                hostname=f"runner-{i:03d}",
                status="active",
                created_at=_utcnow() - timedelta(seconds=i),
            )
            for i in range(205)
        ]
        db_session.add_all(runners)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners?limit=1000"
        )
        assert resp.status_code == 200
        listed = resp.json()
        assert len(listed) == 205
        assert listed[0]["hostname"] == "runner-000"
        assert listed[-1]["hostname"] == "runner-204"

    @pytest.mark.asyncio
    async def test_list_runners_default_limit(
        self, client: AsyncClient, db_session
    ):
        """Call without limit param; should return at most 50 runners by default."""
        project = await _make_project(db_session)
        runners = [
            LocalRunner(
                project_id=project.id,
                token_hash=f"{i:064x}",
                hostname=f"runner-{i:03d}",
                status="active",
                created_at=_utcnow() - timedelta(seconds=i),
            )
            for i in range(205)
        ]
        db_session.add_all(runners)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners"
        )
        assert resp.status_code == 200
        listed = resp.json()
        # Default limit is 50; with 205 seeded runners we expect exactly 50
        assert len(listed) == 50, f"expected 50 (default limit), got {len(listed)}"
        # Runners are ordered by created_at DESC; the most-recently-created
        # (lowest i) should appear first
        assert listed[0]["hostname"] == "runner-000"
        assert listed[-1]["hostname"] == "runner-049"
        # token_age_days is None when token_created_at is not set
        assert listed[0]["token_age_days"] is None

    @pytest.mark.asyncio
    async def test_list_runners_includes_token_age_days(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="abcd" + "f" * 60,
            hostname="old-token-runner",
            status="active",
            token_created_at=_utcnow() - timedelta(days=100),
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/runners"
        )
        assert resp.status_code == 200
        listed = resp.json()
        assert len(listed) == 1
        assert listed[0]["hostname"] == "old-token-runner"
        # token_age_days must reflect the 100-day-old token
        assert listed[0]["token_age_days"] == 100

    @pytest.mark.asyncio
    async def test_leaving_continuous_mode_clears_local_continuation_marker(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

        project = await _make_project(db_session)
        first = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "runtime",
                },
            },
        )
        second = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "chat",
                    "active_surface": "runtime",
                },
            },
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["arming"] == "passive_note"
        await db_session.refresh(project)
        assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (project.vision or {})

    @pytest.mark.asyncio
    async def test_non_continuous_update_clears_stale_foreign_continuation_marker(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import (
            STRATEGIC_LOCAL_CONTINUATION_KEY,
            STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
        )

        project = await _make_project(db_session)
        stale_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS + 60)
        ).isoformat()
        project.vision = {
            **(project.vision or {}),
            STRATEGIC_LOCAL_CONTINUATION_KEY: {
                "version": 1,
                "active": True,
                "operator_user_id": "other-operator",
                "updated_at": stale_at,
                "last_seen_at": stale_at,
            },
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "chat",
                    "active_surface": "runtime",
                },
            },
        )

        assert resp.status_code == 200
        assert resp.json()["workbench"]["interaction_mode"] == "chat"
        await db_session.refresh(project)
        assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (project.vision or {})

    @pytest.mark.parametrize(
        "marker_overrides",
        [
            {"active": "false"},
            {"active": None},
            {"operator_user_id": ""},
            {"operator_user_id": 123},
            {"version": "1"},
        ],
    )
    async def test_non_continuous_update_clears_malformed_foreign_continuation_marker(
        self, client: AsyncClient, db_session, marker_overrides: dict[str, object]
    ):
        from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

        project = await _make_project(db_session)
        fresh_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        project.vision = {
            **(project.vision or {}),
            STRATEGIC_LOCAL_CONTINUATION_KEY: {
                "version": 1,
                "active": True,
                "operator_user_id": "other-operator",
                "updated_at": fresh_at,
                "last_seen_at": fresh_at,
                **marker_overrides,
            },
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "chat",
                    "active_surface": "runtime",
                },
            },
        )

        assert resp.status_code == 200
        await db_session.refresh(project)
        assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (project.vision or {})

    @pytest.mark.asyncio
    async def test_non_continuous_update_preserves_fresh_foreign_continuation_marker(
        self, client: AsyncClient, db_session
    ):
        from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

        project = await _make_project(db_session)
        fresh_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        project.vision = {
            **(project.vision or {}),
            STRATEGIC_LOCAL_CONTINUATION_KEY: {
                "version": 1,
                "active": True,
                "operator_user_id": "other-operator",
                "updated_at": fresh_at,
                "last_seen_at": fresh_at,
                "preferred_runner_id": str(uuid.uuid4()),
            },
        }
        db_session.add(project)
        await db_session.commit()

        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "chat",
                    "active_surface": "runtime",
                },
            },
        )

        assert resp.status_code == 200
        await db_session.refresh(project)
        marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
        assert marker["operator_user_id"] == "other-operator"
        assert marker["last_seen_at"] == fresh_at

    @pytest.mark.asyncio
    async def test_arm_rollback_does_not_discard_workbench_state(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)

        async def _rollback_then_fail(session, *args, **kwargs):
            await session.rollback()
            return False

        with patch(
            "app.workbench_state.request_strategic_refresh",
            new=AsyncMock(side_effect=_rollback_then_fail),
        ):
            resp = await client.put(
                "/api/admin/messages/workbench-state",
                json={
                    "project_id": str(project.id),
                    "workbench": {
                        "interaction_mode": "continuous",
                        "active_surface": "strategic",
                    },
                },
            )

        assert resp.status_code == 200
        assert resp.json()["arming"] == "not_armed"
        assert resp.json()["workbench"]["interaction_mode"] == "continuous"

        get_resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["workbench"]["interaction_mode"] == "continuous"
        await db_session.refresh(project)
        assert not (project.vision or {}).get("_strategic_refresh")

    @pytest.mark.asyncio
    async def test_chat_mode_does_not_arm_strategic_refresh(
        self, client: AsyncClient, db_session
    ):
        # Chat is the sole non-arming mode after the collapse; entering it (or
        # staying in it) must not arm a strategic refresh.
        project = await _make_project(db_session)
        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "chat",
                    "active_surface": "pipeline",
                },
            },
        )

        assert resp.status_code == 200
        assert resp.json()["arming"] == "passive_note"
        await db_session.refresh(project)
        assert not (project.vision or {}).get("_strategic_refresh")

    @pytest.mark.asyncio
    async def test_workbench_state_preserves_noise_pref_reserved_data(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                },
            },
        )
        await client.put(
            "/api/admin/messages/noise-prefs",
            json={"project_id": str(project.id), "prefs": {"cycle_summary": "routine"}},
        )
        await client.put(
            "/api/admin/messages/temp-mutes",
            json={
                "project_id": str(project.id),
                "temp_mutes": {"pr_merged": 4_102_444_800_000},
            },
        )
        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": 4_102_444_800_000},
        )

        workbench_resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={project.id}"
        )
        assert workbench_resp.json()["workbench"]["interaction_mode"] == "continuous"

        noise_resp = await client.get(
            f"/api/admin/messages/noise-prefs?project_id={project.id}"
        )
        assert noise_resp.json()["prefs"] == {"cycle_summary": "routine"}

        row = (
            await db_session.execute(
                select(MessagesNoisePrefs).where(
                    MessagesNoisePrefs.project_id == project.id,
                    MessagesNoisePrefs.user_id == TEST_USER_ID,
                )
            )
        ).scalar_one()
        assert "_strategic_workbench" in row.prefs
        assert row.prefs["cycle_summary"] == "routine"
        assert row.prefs["_temp_mutes"] == {"pr_merged": 4_102_444_800_000}
        assert row.prefs["_mute_window"] == {"expires_at": 4_102_444_800_000}

    @pytest.mark.asyncio
    async def test_payload_too_large_returns_400(self, client: AsyncClient, db_session):
        project = await _make_project(db_session)
        resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={
                "project_id": str(project.id),
                "workbench": {"selected_refs": [{"kind": "task", "id": "x" * 60_000}]},
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/workbench-state?project_id={random_pid}"
        )
        assert get_resp.status_code == 404
        put_resp = await client.put(
            "/api/admin/messages/workbench-state",
            json={"project_id": random_pid, "workbench": {}},
        )
        assert put_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_workbench_state_from_prefs_decays_stale_session(
        self, client: AsyncClient, db_session
    ):
        """workbench_state_from_prefs clears session_started_at when idle > 4 hours."""
        from app.workbench_state import workbench_state_from_prefs, SESSION_IDLE_TTL_SECONDS, WORKBENCH_STATE_KEY

        stale_ts = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=SESSION_IDLE_TTL_SECONDS + 60)
        ).isoformat()
        prefs = {
            WORKBENCH_STATE_KEY: {
                "version": 1,
                "interaction_mode": "check_in",
                "active_surface": "strategic",
                "selected_refs": [],
                "session_started_at": stale_ts,
                "last_interaction_at": stale_ts,
            }
        }
        result = workbench_state_from_prefs(prefs)
        assert result["session_started_at"] is None, (
            "Stale session must decay: session_started_at should be None when last_interaction_at exceeds TTL"
        )
        assert result["interaction_mode"] == "chat"
        assert result["active_surface"] == "strategic"
        assert result["selected_refs"] == []

    @pytest.mark.asyncio
    async def test_workbench_state_from_prefs_keeps_recent_session(
        self, client: AsyncClient, db_session
    ):
        """workbench_state_from_prefs preserves session_started_at for fresh sessions."""
        from app.workbench_state import workbench_state_from_prefs, SESSION_IDLE_TTL_SECONDS, WORKBENCH_STATE_KEY

        recent_ts = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=SESSION_IDLE_TTL_SECONDS - 60)
        ).isoformat()
        started_ts = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=2)
        ).isoformat()
        prefs = {
            WORKBENCH_STATE_KEY: {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "pipeline",
                "selected_refs": [],
                "session_started_at": started_ts,
                "last_interaction_at": recent_ts,
            }
        }
        result = workbench_state_from_prefs(prefs)
        assert result["session_started_at"] == started_ts, (
            "Recent session must not decay: session_started_at should be preserved"
        )

    @pytest.mark.asyncio
    async def test_workbench_state_from_prefs_legacy_fallback_decays_stale_session_started_at(
        self, client: AsyncClient, db_session
    ):
        """Legacy rows with no last_interaction_at fall back to session_started_at for TTL."""
        from app.workbench_state import workbench_state_from_prefs, SESSION_IDLE_TTL_SECONDS, WORKBENCH_STATE_KEY

        stale_ts = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=SESSION_IDLE_TTL_SECONDS + 3600)
        ).isoformat()
        prefs = {
            WORKBENCH_STATE_KEY: {
                "version": 1,
                "interaction_mode": "check_in",
                "active_surface": "strategic",
                "selected_refs": [],
                "session_started_at": stale_ts,
                "last_interaction_at": None,
            }
        }
        result = workbench_state_from_prefs(prefs)
        assert result["session_started_at"] is None, (
            "Legacy session with stale session_started_at (no last_interaction_at) must decay"
        )


def _message_saved_view(idx: int = 1, **overrides):
    view = {
        "id": f"view-{idx}",
        "name": f"View {idx}",
        "query": "",
        "prefs": {},
        "sort": "importance",
        "showResolved": False,
        "routineExpanded": False,
        "showOnlyNew": False,
        "createdAt": 1_700_000_000_000 + idx,
    }
    view.update(overrides)
    return view


class TestMessagesSavedViews:
    def test_sanitizer_handles_corrupt_non_list_rows(self):
        """Old/corrupt JSONB values read back as an empty saved-view list."""
        from app.routers.admin_notifications import _sanitize_message_saved_views

        assert _sanitize_message_saved_views({"views": [_message_saved_view()]}) == []

    @pytest.mark.asyncio
    async def test_get_saved_views_empty(self, client: AsyncClient, db_session):
        """Returns empty list when no saved-views record exists for the operator."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["saved_views"] == []

    @pytest.mark.asyncio
    async def test_get_sanitizes_corrupt_saved_views_row(self, client: AsyncClient, db_session):
        """GET sanitizes legacy/corrupt rows before returning them to the UI."""
        project = await _make_project(db_session)
        db_session.add(
            MessagesSavedViews(
                user_id=TEST_USER_ID,
                project_id=project.id,
                saved_views=[
                    {"id": "bad"},
                    _message_saved_view(1, prefs={"deploy": "surfaced", "list": [], "dict": {}}, extra="drop"),
                    *[_message_saved_view(i) for i in range(2, 13)],
                ],
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )

        assert resp.status_code == 200
        saved_views = resp.json()["saved_views"]
        assert len(saved_views) == 10
        assert saved_views[0] == {
            "id": "view-1",
            "name": "View 1",
            "query": "",
            "prefs": {"deploy": "surfaced"},
            "sort": "importance",
            "showResolved": False,
            "routineExpanded": False,
            "showOnlyNew": False,
            "createdAt": 1_700_000_000_001,
        }
        assert "extra" not in saved_views[0]
        assert saved_views[-1]["id"] == "view-10"

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores saved views; subsequent GET returns the same value."""
        project = await _make_project(db_session)
        views = [_message_saved_view(name="Blockers only", query="blocked", sort="recent")]

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["saved_views"] == views

    @pytest.mark.asyncio
    async def test_update_replaces_views(self, client: AsyncClient, db_session):
        """A second PUT replaces the previous saved views."""
        project = await _make_project(db_session)
        views1 = [_message_saved_view(1, name="First view")]
        views2 = [_message_saved_view(2, name="Second view"), _message_saved_view(3, name="Third view")]

        await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views1},
        )
        await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views2},
        )
        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert get_resp.json()["saved_views"] == views2

    @pytest.mark.asyncio
    async def test_put_sanitizes_and_caps_saved_views(self, client: AsyncClient, db_session):
        """PUT stores only valid saved-view fields and caps the list at ten."""
        project = await _make_project(db_session)
        views = [
            "not a view",
            {"id": "missing-name"},
            _message_saved_view(
                0,
                name="Primary",
                query=123,
                prefs={"deployment": "mute", "noise": "invalid", "list": [], "dict": {}},
                sort="recent",
                showResolved=True,
                showOnlyNew=True,
                extra="dropped",
            ),
            *[_message_saved_view(i) for i in range(1, 12)],
        ]

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": views},
        )
        assert put_resp.status_code == 200

        row = (
            await db_session.execute(
                select(MessagesSavedViews).where(
                    MessagesSavedViews.user_id == TEST_USER_ID,
                    MessagesSavedViews.project_id == project.id,
                )
            )
        ).scalar_one()
        saved_views = row.saved_views

        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={project.id}"
        )
        assert get_resp.json()["saved_views"] == saved_views
        assert len(saved_views) == 10
        assert saved_views[0] == {
            "id": "view-0",
            "name": "Primary",
            "query": "",
            "prefs": {"deployment": "mute"},
            "sort": "recent",
            "showResolved": True,
            "routineExpanded": False,
            "showOnlyNew": True,
            "createdAt": 1_700_000_000_000,
        }
        assert "extra" not in saved_views[0]
        assert saved_views[-1]["id"] == "view-9"

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Cannot read or write saved-views for a project the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/saved-views?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": random_pid, "saved_views": []},
        )
        assert put_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_oversize_payload_returns_400(self, client: AsyncClient, db_session):
        """Payload exceeding 50 KB is rejected with 400."""
        project = await _make_project(db_session)
        # Each view is ~1KB; 60 of them easily exceeds 50KB
        large_views = [{"name": f"view-{i}", "data": "x" * 900} for i in range(60)]

        put_resp = await client.put(
            "/api/admin/messages/saved-views",
            json={"project_id": str(project.id), "saved_views": large_views},
        )
        assert put_resp.status_code == 400
        assert "50" in put_resp.json()["error"]["message"]


class TestMessagesTempMutes:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns empty dict when no temp-mutes record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores temp-mutes; GET returns only non-expired entries."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 3_600_000  # 1 hour from now
        mutes = {"pr_merged": future_ms}

        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": str(project.id), "temp_mutes": mutes},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["temp_mutes"] == mutes

    @pytest.mark.asyncio
    async def test_expired_mutes_filtered_on_get(self, client: AsyncClient, db_session):
        """Expired entries are filtered out on GET; active entries survive."""
        import time

        project = await _make_project(db_session)
        now_ms = int(time.time() * 1000)
        future_ms = now_ms + 3_600_000
        # Write one expired + one active mute (bypass filter by writing directly via PUT
        # with a past timestamp — PUT also filters, so use future for writing and verify active)
        await client.put(
            "/api/admin/messages/temp-mutes",
            json={
                "project_id": str(project.id),
                "temp_mutes": {"cycle_summary": future_ms},
            },
        )
        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert "cycle_summary" in get_resp.json()["temp_mutes"]

        # Overwrite with an already-expired entry: PUT filters it out before storing
        past_ms = now_ms - 1000
        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": str(project.id), "temp_mutes": {"pr_merged": past_ms}},
        )
        assert put_resp.status_code == 200
        get_resp2 = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={project.id}"
        )
        assert get_resp2.json()["temp_mutes"] == {}

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/temp-mutes?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/temp-mutes",
            json={"project_id": random_pid, "temp_mutes": {}},
        )
        assert put_resp.status_code == 404


class TestMessagesMuteWindow:
    @pytest.mark.asyncio
    async def test_get_empty(self, client: AsyncClient, db_session):
        """Returns null when no mute-window record exists."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert resp.status_code == 200
        assert resp.json()["mute_window"] is None

    @pytest.mark.asyncio
    async def test_round_trip(self, client: AsyncClient, db_session):
        """PUT stores mute-window; GET returns it while still active."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 7_200_000  # 2 hours from now

        put_resp = await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "ok"

        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["mute_window"] == {"expires_at": future_ms}

    @pytest.mark.asyncio
    async def test_clear_mute_window(self, client: AsyncClient, db_session):
        """PUT with expires_at=null clears the mute window."""
        import time

        project = await _make_project(db_session)
        future_ms = int(time.time() * 1000) + 7_200_000

        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": future_ms},
        )
        await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": str(project.id), "expires_at": None},
        )
        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={project.id}"
        )
        assert get_resp.json()["mute_window"] is None

    @pytest.mark.asyncio
    async def test_no_access_returns_404(self, client: AsyncClient, db_session):
        """Endpoints return 404 for projects the user is not a member of."""
        random_pid = str(uuid.uuid4())
        get_resp = await client.get(
            f"/api/admin/messages/mute-window?project_id={random_pid}"
        )
        assert get_resp.status_code == 404

        put_resp = await client.put(
            "/api/admin/messages/mute-window",
            json={"project_id": random_pid, "expires_at": None},
        )
        assert put_resp.status_code == 404



# ═══════════════════════════════════════════════════════════════════════════════
# admin_notifications.py — Auto-resolve answered messages (reaper sweep)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnsweredQuestionAutoResolves:
    """Auto-resolve sweep: OPEN QUESTION with a strategic reply → resolved."""

    @pytest.mark.asyncio
    async def test_answered_question_auto_resolves(self, client: AsyncClient, db_session):
        """An open QUESTION with a strategic reply auto-resolves and leaves needs-action."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages
        from app.message_convergence import derive_needs_action
        from app.message_audience import OPERATOR_ACTION_NEEDED

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        parent = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Please explain the failure.",
            status="open",
            context={"audience": OPERATOR_ACTION_NEEDED},
        )
        db_session.add(parent)
        await db_session.flush()

        reply = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="Here is the explanation.",
            status="open",
            reply_to_message_id=parent.id,
        )
        db_session.add(reply)
        await db_session.commit()

        # Confirm it appears in the counts before the sweep
        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        assert resp.json()["question"] == 1

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 1

        await db_session.refresh(parent)
        assert parent.status == "resolved"
        assert parent.resolution == "answered"
        assert not derive_needs_action(parent)

        # Verify it disappears from the counts endpoint
        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        assert resp.json()["question"] == 0

    @pytest.mark.asyncio
    async def test_answered_question_no_reply_stays_open(self, client: AsyncClient, db_session):
        """An open QUESTION with no reply must remain open."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages
        from app.message_audience import OPERATOR_ACTION_NEEDED

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        parent = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Unanswered question.",
            status="open",
            context={"audience": OPERATOR_ACTION_NEEDED},
        )
        db_session.add(parent)
        await db_session.commit()

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 0

        await db_session.refresh(parent)
        assert parent.status == "open"

        resp = await client.get(f"/api/admin/messages/counts?project_id={project.id}")
        assert resp.status_code == 200
        assert resp.json()["question"] == 1

    @pytest.mark.asyncio
    async def test_answered_blocker_not_auto_resolved(self, client: AsyncClient, db_session):
        """A BLOCKER with a strategic reply must NOT be auto-resolved."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        blocker = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="system",
            content="Critical blocker.",
            status="open",
        )
        db_session.add(blocker)
        await db_session.flush()

        reply = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="Working on it.",
            status="open",
            reply_to_message_id=blocker.id,
        )
        db_session.add(reply)
        await db_session.commit()

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 0

        await db_session.refresh(blocker)
        assert blocker.status == "open"

    @pytest.mark.asyncio
    async def test_answered_operator_action_needed_note_resolves(self, db_session):
        """An open NOTE with operator-action-needed audience and a strategic reply auto-resolves."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages
        from app.message_convergence import derive_needs_action
        from app.message_audience import OPERATOR_ACTION_NEEDED

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        parent = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="Action required.",
            status="open",
            context={"audience": OPERATOR_ACTION_NEEDED},
        )
        db_session.add(parent)
        await db_session.flush()

        reply = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="agent",
            content="Done.",
            status="open",
            reply_to_message_id=parent.id,
        )
        db_session.add(reply)
        await db_session.commit()

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 1

        await db_session.refresh(parent)
        assert parent.status == "resolved"
        assert not derive_needs_action(parent)

    @pytest.mark.asyncio
    async def test_answered_question_user_reply_does_not_resolve(self, db_session):
        """A reply from from_type='user' must NOT trigger auto-resolution."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        parent = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="system",
            content="Machine question.",
            status="open",
        )
        db_session.add(parent)
        await db_session.flush()

        reply = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="User reply that shouldn't trigger.",
            status="open",
            reply_to_message_id=parent.id,
        )
        db_session.add(reply)
        await db_session.commit()

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 0

        await db_session.refresh(parent)
        assert parent.status == "open"

    @pytest.mark.asyncio
    async def test_answered_question_already_resolved_is_idempotent(self, db_session):
        """Already-resolved QUESTION with a strategic reply → sweep returns 0 (idempotent)."""
        from app.reaper_sweeps import sweep_auto_resolve_answered_messages

        project = await _make_project(db_session)
        now = datetime.now(timezone.utc)

        parent = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="user",
            content="Already resolved.",
            status="resolved",
            resolution="manual",
        )
        db_session.add(parent)
        await db_session.flush()

        reply = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="Response.",
            status="open",
            reply_to_message_id=parent.id,
        )
        db_session.add(reply)
        await db_session.commit()

        count = await sweep_auto_resolve_answered_messages(db_session, now)
        assert count == 0

        await db_session.refresh(parent)
        assert parent.status == "resolved"
        assert parent.resolution == "manual"

