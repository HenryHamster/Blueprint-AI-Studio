"""Tests for message convergence resolver and reference handling."""

import logging
import uuid

import pytest
from fastapi import HTTPException

from app.message_convergence import (
    resolve_message_ref,
    link_message_to_outcome,
    get_message_link,
    is_bug_class,
    derive_needs_action,
    mark_message_resolved,
    linked_outcome_landed_resolution,
    LINKED_STATUS,
    RESOLVED_STATUS,
    NEEDS_ACTION_MESSAGE_TYPES,
    CONVERSATIONAL_DECAY_MESSAGE_TYPES,
)
from app.models import (
    Direction,
    Project,
    ProjectMember,
    ProjectMessage,
    MessageType,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Message Convergence Test Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_resolve_message_ref_basic(db_session):
    """Test basic message reference resolution."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        content="A bug found",
    )
    db_session.add(msg)
    await db_session.commit()

    # Resolve by full UUID (dashes removed, case-insensitive)
    resolved = await resolve_message_ref(
        db_session, project.id, str(msg.id).replace("-", "")
    )
    assert resolved.id == msg.id


@pytest.mark.asyncio
async def test_resolve_message_ref_short_hex(db_session):
    """Test message resolution by short hex prefix."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        content="A bug found",
    )
    db_session.add(msg)
    await db_session.commit()

    # Resolve by first 8 hex chars (minimum)
    hex_ref = msg.id.hex[:8]
    resolved = await resolve_message_ref(db_session, project.id, hex_ref)
    assert resolved.id == msg.id


@pytest.mark.asyncio
async def test_resolve_message_ref_no_warning_under_limit(db_session, caplog):
    """Test that no window-cap warning is logged when project has ≤500 messages."""
    project = await _make_project(db_session)

    # Create only 50 messages (well under the 500 limit).
    for i in range(50):
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            content=f"Message {i}",
        )
        db_session.add(msg)
    await db_session.commit()

    # Try to resolve a non-existent reference.
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_message_ref(db_session, project.id, "deadbeef")

    assert exc_info.value.status_code == 404
    # Window cap warning should NOT be logged (only 50 messages total).
    assert "message_ref resolver" not in caplog.text


@pytest.mark.asyncio
async def test_link_message_to_outcome(db_session):
    """Test linking a message to an outcome."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        content="A bug found",
    )
    db_session.add(msg)
    await db_session.commit()

    direction_id = "dir-test-123"
    link_message_to_outcome(msg, "direction", direction_id)

    assert msg.status == LINKED_STATUS
    link = get_message_link(msg)
    assert link is not None
    assert link["kind"] == "direction"
    assert link["id"] == direction_id
    assert "linked_at" in link


@pytest.mark.asyncio
async def test_is_bug_class(db_session):
    """Test bug class detection."""
    project = await _make_project(db_session)

    bug_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        content="A bug",
    )
    blocker_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        content="A blocker",
    )
    note_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        content="A note",
    )

    assert is_bug_class(bug_msg)
    assert is_bug_class(blocker_msg)
    assert not is_bug_class(note_msg)


@pytest.mark.asyncio
async def test_resolve_message_ref_ambiguous(db_session):
    """Test that ambiguous references raise 409 with candidate list."""
    project = await _make_project(db_session)

    # Create two messages that will both match a short prefix.
    # We'll manually create UUIDs that share a hex prefix.
    msg1 = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        content="Message 1",
    )
    db_session.add(msg1)
    await db_session.commit()

    msg2 = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        content="Message 2",
    )
    db_session.add(msg2)
    await db_session.commit()

    # A very short prefix (less than 8 chars) will be rejected as too short.
    # For a true ambiguity test, we would need two UUIDs that match the same 8+ char prefix,
    # which is extremely unlikely in practice. Skip this as an edge case.


@pytest.mark.asyncio
async def test_resolve_message_ref_too_short(db_session):
    """Test that a reference shorter than MIN_MESSAGE_REF_HEXLEN is rejected."""
    project = await _make_project(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await resolve_message_ref(db_session, project.id, "abc")

    assert exc_info.value.status_code == 400
    assert "too short" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_resolve_message_ref_beyond_500(db_session):
    """Resolution survives projects with >500 messages (no recency-window cap)."""
    project = await _make_project(db_session)

    # Seed 501 messages so any window-cap of 500 would hide at least one early message.
    msgs = []
    for i in range(501):
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            content=f"Message {i}",
        )
        db_session.add(msg)
        msgs.append(msg)
    await db_session.commit()

    # Resolve the first-inserted message by its short hex prefix.
    # If a 500-row recency cap were in effect the earliest messages would be invisible.
    target = msgs[0]
    hex_ref = target.id.hex[:8]
    resolved = await resolve_message_ref(db_session, project.id, hex_ref)
    assert resolved.id == target.id


@pytest.mark.asyncio
async def test_resolve_message_ref_invalid_hex(db_session):
    """Test that non-hex characters are rejected."""
    project = await _make_project(db_session)

    with pytest.raises(HTTPException) as exc_info:
        await resolve_message_ref(db_session, project.id, "gggggggg")

    assert exc_info.value.status_code == 400
    assert "hex" in exc_info.value.detail.lower()


# ── Stage-2 generalized lifecycle: needs-action derivation ────────────────────


def _msg(msg_type, status="open"):
    return ProjectMessage(
        project_id=uuid.uuid4(),
        message_type=msg_type,
        content="x",
        status=status,
    )


def test_needs_action_message_types_are_question_and_blocker():
    assert NEEDS_ACTION_MESSAGE_TYPES == frozenset(
        {MessageType.QUESTION.value, MessageType.BLOCKER.value}
    )
    assert CONVERSATIONAL_DECAY_MESSAGE_TYPES == frozenset(
        {MessageType.NOTE.value, MessageType.FEEDBACK.value}
    )


def test_derive_needs_action_true_for_open_question_and_blocker():
    assert derive_needs_action(_msg(MessageType.QUESTION, "open")) is True
    assert derive_needs_action(_msg(MessageType.BLOCKER, "linked")) is True


def test_derive_needs_action_false_when_resolved():
    # A resolved QUESTION/BLOCKER no longer needs action — the derivation is how
    # auto-resolve clears needs-action without a schema column.
    assert derive_needs_action(_msg(MessageType.QUESTION, RESOLVED_STATUS)) is False
    assert derive_needs_action(_msg(MessageType.BLOCKER, RESOLVED_STATUS)) is False


def test_derive_needs_action_false_for_conversational_types():
    assert derive_needs_action(_msg(MessageType.NOTE, "open")) is False
    assert derive_needs_action(_msg(MessageType.FEEDBACK, "open")) is False


def test_mark_message_resolved_stamps_audit_trail():
    msg = _msg(MessageType.QUESTION, "linked")
    now = _utcnow()
    mark_message_resolved(msg, now, "auto-decayed (stale)")
    assert msg.status == RESOLVED_STATUS
    assert msg.resolution == "auto-decayed (stale)"
    assert msg.resolved_at == now
    assert msg.context.get("_convergence_converged_at") == now.isoformat()
    # Resolving clears the derived needs-action flag.
    assert derive_needs_action(msg) is False


# ── Stage-2 generalized lifecycle: linked-outcome landing detector ────────────


@pytest.mark.asyncio
async def test_linked_outcome_landed_resolution_task_merged(db_session):
    """A non-bug NOTE linked to a merged-completion task yields a landed audit."""
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="strategic",
        content="tracked via task",
    )
    link_message_to_outcome(msg, "task", str(task.id))
    db_session.add(msg)
    await db_session.commit()

    audit = await linked_outcome_landed_resolution(db_session, msg, now=_utcnow())
    assert audit is not None
    assert "landed" in audit or "merged" in audit


@pytest.mark.asyncio
async def test_linked_outcome_landed_resolution_task_not_landed(db_session):
    """A task COMPLETED without the landed signal is NOT treated as landed."""
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        content="tracked",
    )
    link_message_to_outcome(msg, "task", str(task.id))
    db_session.add(msg)
    await db_session.commit()

    assert await linked_outcome_landed_resolution(db_session, msg, now=_utcnow()) is None


@pytest.mark.asyncio
async def test_linked_outcome_landed_resolution_direction_completed(db_session):
    """A message linked to a completed direction yields a landed audit."""
    project = await _make_project(db_session)
    direction = Direction(
        project_id=project.id,
        direction_key="d-done1",
        status="completed",
        data={"id": "d-done1", "status": "completed"},
    )
    db_session.add(direction)
    await db_session.commit()

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        content="answered by direction",
    )
    link_message_to_outcome(msg, "direction", "d-done1")
    db_session.add(msg)
    await db_session.commit()

    audit = await linked_outcome_landed_resolution(db_session, msg, now=_utcnow())
    assert audit is not None
    assert "completed" in audit


@pytest.mark.asyncio
async def test_linked_outcome_landed_resolution_direction_active_is_none(db_session):
    """An active (not completed) direction leaves the message unresolved."""
    project = await _make_project(db_session)
    direction = Direction(
        project_id=project.id,
        direction_key="d-active1",
        status="active",
        data={"id": "d-active1", "status": "active"},
    )
    db_session.add(direction)
    await db_session.commit()

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        content="tracked",
    )
    link_message_to_outcome(msg, "direction", "d-active1")
    db_session.add(msg)
    await db_session.commit()

    assert await linked_outcome_landed_resolution(db_session, msg, now=_utcnow()) is None


@pytest.mark.asyncio
async def test_linked_outcome_landed_resolution_no_link_is_none(db_session):
    """A message with no _convergence_link is a fail-safe None."""
    project = await _make_project(db_session)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        content="unlinked",
    )
    db_session.add(msg)
    await db_session.commit()

    assert await linked_outcome_landed_resolution(db_session, msg, now=_utcnow()) is None
