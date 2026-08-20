"""Tests for the Step 25 stranded-pending-conversation-message failure sweep.

Covers the acceptance behavior: an operator chat message sent while the
selected runner is dead never gets a claim, and must reach a bounded,
operator-visible terminal outcome (handling_state='failed') instead of
remaining silently PENDING forever.
"""

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
)
from app.reaper_bug_message_sweeps import (
    STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES,
    STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES,
    sweep_fail_stranded_pending_conversation_messages,
)
from app.project_message_projection import WAKEUP_STATE_CONTEXT_KEY
from app.session_message_handoff import RESIDENT_HANDOFF_AT_CONTEXT_KEY
from app.strategic_resident_session import RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY
from app.time_utils import _utcnow


@pytest_asyncio.fixture()
async def sweep_db():
    """Isolated in-memory SQLite DB for sweep unit tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


ARMED_CONTEXT = {WAKEUP_STATE_CONTEXT_KEY: "queued"}


async def _make_project(db: AsyncSession) -> Project:
    project = Project(name="sweep-unit", user_id="user1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_stranded_pending_conversation_message_fails(sweep_db):
    """An unclaimed PENDING conversation message past the window is swept to FAILED."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 1
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="operator message sent while the selected runner was dead",
        status="open",
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        conversation_claim_id=None,
        sequence=1,
        # A runner WAS armed for this send (continuous/Workday path), so the
        # short window applies.
        context=dict(ARMED_CONTEXT),
        created_at=old,
        updated_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.FAILED
    assert msg.status == "resolved"
    assert msg.resolution == "stranded (no runner claim within the window)"
    assert msg.resolved_at is not None
    assert msg.context.get("_stranded_failed_at") is not None


@pytest.mark.asyncio
async def test_fresh_pending_conversation_message_untouched(sweep_db):
    """A PENDING conversation message within the window is left alone."""
    project = await _make_project(sweep_db)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="just sent",
        status="open",
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        conversation_claim_id=None,
        sequence=1,
        context={},
        created_at=_utcnow(),
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_claimed_conversation_message_untouched(sweep_db):
    """A claimed message is left to conversation_coordinator's lease-expiry path."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 30
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="claimed by a (possibly stalled) worker",
        status="open",
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.CLAIMED,
        conversation_claim_id=uuid.uuid4(),
        sequence=1,
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.CLAIMED
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_stale_activity_surface_message_untouched(sweep_db):
    """Non-conversation (ACTIVITY) messages are outside this sweep's scope."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 30
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="an activity-surface note, not a conversation send",
        status="open",
        surface=MessageSurface.ACTIVITY,
        handling_state=MessageHandlingState.PENDING,
        conversation_claim_id=None,
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_already_responded_conversation_message_untouched(sweep_db):
    """A message that already progressed past PENDING is never re-touched."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 30
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="already answered",
        status="resolved",
        resolution="answered",
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        conversation_claim_id=None,
        sequence=1,
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.RESPONDED
    assert msg.resolution == "answered"


async def _add_message(db: AsyncSession, project: Project, **overrides) -> ProjectMessage:
    """Insert a conversation-surface message with sweep-relevant defaults."""
    created_at = overrides.pop("created_at", _utcnow())
    updated_at = overrides.pop("updated_at", created_at)
    fields = {
        "project_id": project.id,
        "message_type": MessageType.NOTE,
        "from_type": "user",
        "content": "msg",
        "status": "open",
        "surface": MessageSurface.CONVERSATION,
        "handling_state": MessageHandlingState.PENDING,
        "conversation_claim_id": None,
        "sequence": 1,
        "context": {},
        **overrides,
    }
    msg = ProjectMessage(created_at=created_at, updated_at=updated_at, **fields)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


def _minutes_ago(minutes: int):
    return _utcnow() - timedelta(minutes=minutes)


@pytest.mark.asyncio
async def test_system_workday_consultation_packet_untouched(sweep_db):
    """A queued Workday consultation packet is system-authored and claim-in-context.

    Its ``conversation_claim_id`` is permanently NULL — the claim lives in
    ``context['_workday_consultation_claim_id']`` — so a from_type-blind
    predicate would terminally FAIL a packet whose Fast turn has simply not
    claimed it yet, making it unclaimable (claim gates require status=='open').
    """
    project = await _make_project(sweep_db)
    old = _minutes_ago(STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120)
    msg = await _add_message(
        sweep_db,
        project,
        from_type="system",
        content="consultation question for the Fast turn",
        context={
            "message_role": "workday_consultation",
            "_workday_consultation_packet": True,
        },
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_workday_consultation_result_row_untouched(sweep_db):
    """A consultation result row awaiting the next continuation is left alone."""
    project = await _make_project(sweep_db)
    old = _minutes_ago(STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120)
    msg = await _add_message(
        sweep_db,
        project,
        from_type="system",
        content="consultation result summary",
        context={
            "message_role": "workday_consultation_result",
            "_workday_consultation_claim_id": str(uuid.uuid4()),
        },
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_consultation_marker_on_user_row_still_excluded(sweep_db):
    """The consultation-marker guard holds even if the row is not system-authored."""
    project = await _make_project(sweep_db)
    old = _minutes_ago(STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120)
    msg = await _add_message(
        sweep_db,
        project,
        context={
            **ARMED_CONTEXT,
            "_workday_consultation_claim_id": str(uuid.uuid4()),
        },
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_session_handoff_released_row_untouched(sweep_db):
    """A handoff-released operator row is waiting to be rebound, not stranded.

    session_message_handoff resets arbitrarily old unanswered operator messages
    back to PENDING with the claim cleared so the NEXT resident can claim them.
    Ageing on created_at would destroy the message before rebinding.
    """
    project = await _make_project(sweep_db)
    released_at = _minutes_ago(STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 45)
    msg = await _add_message(
        sweep_db,
        project,
        content="operator message written days before the session ended",
        context={
            **ARMED_CONTEXT,
            RESIDENT_HANDOFF_AT_CONTEXT_KEY: released_at.isoformat(),
        },
        created_at=_utcnow() - timedelta(days=3),
        updated_at=released_at,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_session_handoff_released_row_fails_past_deferred_window(sweep_db):
    """A handoff-released row is still bounded — no permanent PENDING leak."""
    project = await _make_project(sweep_db)
    released_at = _minutes_ago(
        STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120
    )
    msg = await _add_message(
        sweep_db,
        project,
        context={
            **ARMED_CONTEXT,
            RESIDENT_HANDOFF_AT_CONTEXT_KEY: released_at.isoformat(),
        },
        created_at=released_at - timedelta(days=1),
        updated_at=released_at,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.FAILED


@pytest.mark.asyncio
async def test_chat_mode_paused_row_untouched_within_cadence_backstop(sweep_db):
    """Chat mode defers the answer to the cadence strategist (30-1440 min later)."""
    project = await _make_project(sweep_db)
    old = _minutes_ago(600)
    msg = await _add_message(
        sweep_db,
        project,
        content="chat-mode send answered by the next strategist cycle",
        context={WAKEUP_STATE_CONTEXT_KEY: "paused"},
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_chat_mode_waiting_for_capacity_row_untouched(sweep_db):
    """'waiting_for_capacity' models the same designed deferral as 'paused'."""
    project = await _make_project(sweep_db)
    old = _minutes_ago(600)
    msg = await _add_message(
        sweep_db,
        project,
        context={WAKEUP_STATE_CONTEXT_KEY: "waiting_for_capacity"},
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING


@pytest.mark.asyncio
async def test_chat_mode_paused_row_fails_past_deferred_window(sweep_db):
    """Past the cadence backstop, even a deferred chat row reaches a failed state."""
    project = await _make_project(sweep_db)
    old = _minutes_ago(STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120)
    msg = await _add_message(
        sweep_db,
        project,
        context={WAKEUP_STATE_CONTEXT_KEY: "paused"},
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.FAILED
    assert msg.status == "resolved"


@pytest.mark.asyncio
async def test_resident_activation_prompt_row_untouched(sweep_db):
    """The synthetic activation prompt must stay open+PENDING to be retired.

    ``_retire_unclaimed_resident_activation`` only consumes it while it is
    still open, PENDING and unclaimed; failing it strands a synthetic row in
    the operator's conversation forever.
    """
    project = await _make_project(sweep_db)
    old = _minutes_ago(STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES + 120)
    msg = await _add_message(
        sweep_db,
        project,
        context={**ARMED_CONTEXT, RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY: True},
        idempotency_key="resident-activation:sess-1",
        resident_session_id="sess-1",
        created_at=old,
        updated_at=old,
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_recently_rereleased_row_untouched(sweep_db):
    """Age is measured from the CURRENT unclaimed state, not from creation."""
    project = await _make_project(sweep_db)
    msg = await _add_message(
        sweep_db,
        project,
        context=dict(ARMED_CONTEXT),
        created_at=_utcnow() - timedelta(days=5),
        updated_at=_utcnow(),
    )

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
