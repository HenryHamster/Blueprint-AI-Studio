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
    Task,
    TaskStatus,
)
from app.conversation_coordinator import claim_operator_messages
from app.reaper_bug_message_sweeps import (
    EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES,
    EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES,
    STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES,
    STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES,
    sweep_fail_stranded_pending_conversation_messages,
    sweep_release_expired_activity_claims,
    sweep_release_expired_conversation_claims,
)
from app.project_message_projection import WAKEUP_STATE_CONTEXT_KEY
from app.session_message_handoff import (
    RESIDENT_HANDOFF_AT_CONTEXT_KEY,
    RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
)
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
    """Step 25 never touches a LIVE claim -- its owner can still answer.

    Strengthened after the operator-message claim-deadlock incident. This test
    previously used a claim with NO lease at all and asserted, in its own
    docstring, that such a row was "left to conversation_coordinator's
    lease-expiry path". That path does not exist: expiry only grants re-claim
    eligibility and releases nothing, so the row this test constructed was in
    reality unreachable by every code path in the system -- the exact defect
    that left operator messages 'claimed' and unanswered forever. The case is
    now pinned as what it should always have been (a live lease, which really
    does belong to its owner); dead claims are covered by the Step 25a tests
    below, which prove they are released rather than abandoned.
    """
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 30
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="claimed by a worker whose lease is still live",
        status="open",
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.CLAIMED,
        conversation_claim_id=uuid.uuid4(),
        conversation_claim_expires_at=_utcnow() + timedelta(minutes=4),
        sequence=1,
        context={},
        created_at=old,
        updated_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_fail_stranded_pending_conversation_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.CLAIMED
    # ...and Step 25a leaves it alone too, so a live turn is owned by exactly
    # one sweep-free path: its claimant.
    assert await sweep_release_expired_conversation_claims(sweep_db, _utcnow()) == 0
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


# --- Step 25a: expired conversation-claim release -------------------------
#
# Incident (operator-message claim deadlock): operator messages sat at
# handling_state='claimed' with no reply forever, held by a Resident task that
# simultaneously reported "waiting for operator input". Lease expiry only made
# the turn ELIGIBLE for re-claim; it released nothing, every completion route
# refused an expired lease, and Step 25 skipped claimed rows -- so both sides
# deferred to the other and 0 of the operator's messages ever reached
# 'responded'. Step 25a is the release that closes that gap.


async def _add_claimed_message(
    db: AsyncSession, project: Project, **overrides
) -> ProjectMessage:
    """Insert a CLAIMED conversation message with claim fields populated."""
    fields = {
        "handling_state": MessageHandlingState.CLAIMED,
        "conversation_claim_id": uuid.uuid4(),
        "conversation_claim_generation": 1,
        "conversation_claim_incarnation": uuid.uuid4(),
        "conversation_claim_attempt": 2,
        **overrides,
    }
    return await _add_message(db, project, **fields)


@pytest.mark.asyncio
async def test_expired_conversation_claim_is_released_to_pending(sweep_db):
    """A provably dead claim returns to PENDING carrying its release reason."""
    project = await _make_project(sweep_db)
    task = Task(project_id=project.id, title="dead resident worker", payloads=[])
    sweep_db.add(task)
    await sweep_db.commit()
    await sweep_db.refresh(task)
    stale = _minutes_ago(EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES + 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        content="operator question the claimant never answered",
        handled_by_task_id=task.id,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        context={
            **ARMED_CONTEXT,
            "_conversation_claim_idempotency_key": "stale-claim-key",
        },
    )

    acted = await sweep_release_expired_conversation_claims(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert msg.status == "open"
    assert msg.conversation_claim_id is None
    assert msg.conversation_claim_expires_at is None
    assert msg.handled_by_task_id is None
    # The stale client identity must not survive, or the replacement worker
    # cannot bind a fresh claim to the same immutable membership.
    assert "_conversation_claim_idempotency_key" not in msg.context
    # Attempt history is PRESERVED for diagnostics.
    assert msg.conversation_claim_attempt == 2
    # The release is a recorded DEATH, which is what the coordinator's bound
    # counts; without it the release/re-claim loop would never terminate,
    # because each release also resets the claim id and the coordinator then
    # sees an ordinary fresh handout rather than a reclaim over a corpse.
    assert msg.context.get("_conversation_turn_dead_claims") == 1
    # Surface reasons, not states.
    assert msg.context.get("_conversation_claim_released_at") is not None
    reason = msg.context.get("_conversation_claim_release_reason") or ""
    assert "expired at" in reason
    assert "attempt=2" in reason
    assert "dead_claims=1" in reason
    assert str(task.id) in reason


@pytest.mark.asyncio
async def test_repeated_release_accumulates_dead_claims(sweep_db):
    """The death count must ACCUMULATE, or the bound can never be reached.

    Each release also clears the claim id, so the coordinator's next handout
    looks like an ordinary fresh claim rather than a reclaim over a corpse.
    The durable counter is the only thing that carries the failure history
    across that boundary.
    """
    project = await _make_project(sweep_db)
    stale = _minutes_ago(EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES + 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        context={**ARMED_CONTEXT, "_conversation_turn_dead_claims": 3},
    )

    assert await sweep_release_expired_conversation_claims(sweep_db, _utcnow()) == 1
    await sweep_db.refresh(msg)
    assert msg.context.get("_conversation_turn_dead_claims") == 4
    assert "dead_claims=4" in (
        msg.context.get("_conversation_claim_release_reason") or ""
    )


@pytest.mark.asyncio
async def test_live_conversation_claim_lease_untouched(sweep_db):
    """A claim whose lease is still in the future belongs to a live worker."""
    project = await _make_project(sweep_db)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        conversation_claim_expires_at=_utcnow() + timedelta(minutes=4),
        created_at=_minutes_ago(600),
        updated_at=_minutes_ago(600),
    )

    acted = await sweep_release_expired_conversation_claims(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.CLAIMED
    assert msg.conversation_claim_id is not None


@pytest.mark.asyncio
async def test_just_expired_conversation_claim_within_grace_untouched(sweep_db):
    """Inside the grace window a replacement generation may still reclaim."""
    project = await _make_project(sweep_db)
    recent = _minutes_ago(EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES - 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        conversation_claim_expires_at=recent,
        created_at=recent,
        updated_at=recent,
    )

    acted = await sweep_release_expired_conversation_claims(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.CLAIMED


@pytest.mark.asyncio
async def test_leaseless_claim_is_released(sweep_db):
    """A legacy claim with no recorded lease is dead on arrival: no completion
    route accepts a NULL expiry, so nothing but this sweep can ever free it."""
    project = await _make_project(sweep_db)
    stale = _minutes_ago(EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES + 60)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        conversation_claim_expires_at=None,
        created_at=stale,
        updated_at=stale,
    )

    acted = await sweep_release_expired_conversation_claims(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    reason = msg.context.get("_conversation_claim_release_reason") or ""
    assert "legacy lease-less claim" in reason


@pytest.mark.asyncio
async def test_released_claim_then_ages_out_through_step_25(sweep_db):
    """End-to-end termination: Step 25a releases, Step 25 is then the terminal
    backstop. Before Step 25a existed, neither sweep could touch the row and
    the operator's message stayed 'claimed' forever with no explanation."""
    project = await _make_project(sweep_db)
    stale = _minutes_ago(EXPIRED_CONVERSATION_CLAIM_RELEASE_AFTER_MINUTES + 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        context=dict(ARMED_CONTEXT),
    )

    assert await sweep_release_expired_conversation_claims(sweep_db, _utcnow()) == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING

    # Nobody re-claims it; the armed window then expires.
    later = _utcnow() + timedelta(
        minutes=STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES + 1
    )
    assert await sweep_fail_stranded_pending_conversation_messages(sweep_db, later) == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.FAILED
    assert msg.resolution == "stranded (no runner claim within the window)"


# --- Step 25b: expired Activity-claim release -----------------------------


async def _add_resident_task(
    db: AsyncSession,
    project: Project,
    *,
    session_id: str,
    generation: int,
    status: TaskStatus,
) -> Task:
    task = Task(
        project_id=project.id,
        title=f"Resident {session_id}",
        payloads=[],
        status=status,
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": generation,
        },
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


def _set_resident_authority(
    project: Project,
    task: Task,
    *,
    session_id: str,
    generation: int,
) -> None:
    project.vision = {
        **dict(project.vision or {}),
        "_resident_strategic_session": {
            "status": "active",
            "stop_requested": False,
            "task_id": str(task.id),
            "session_id": session_id,
            "worker_generation": generation,
        },
    }


@pytest.mark.asyncio
async def test_dead_activity_claim_release_recovers_mixed_membership(sweep_db):
    """A dead claimed Activity head can join its unclaimed sibling again."""
    project = await _make_project(sweep_db)
    dead_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-old",
        generation=1,
        status=TaskStatus.FAILED,
    )
    live_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-current",
        generation=2,
        status=TaskStatus.IN_PROGRESS,
    )
    _set_resident_authority(
        project,
        live_task,
        session_id="resident-current",
        generation=2,
    )
    sweep_db.add(project)
    await sweep_db.commit()

    stale = _minutes_ago(EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES + 5)
    claimed = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        message_type=MessageType.QUESTION,
        content="old claimed Activity question",
        handled_by_task_id=dead_task.id,
        resident_session_id="resident-old",
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=1,
    )
    sibling = await _add_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        message_type=MessageType.NOTE,
        content="unclaimed Activity sibling",
        resident_session_id=None,
        sequence=2,
    )

    assert await sweep_release_expired_activity_claims(sweep_db, _utcnow()) == 1
    await sweep_db.refresh(claimed)
    assert claimed.handling_state == MessageHandlingState.PENDING
    assert claimed.conversation_claim_id is None
    assert claimed.handled_by_task_id is None
    assert claimed.resident_session_id is None
    assert (
        claimed.context.get(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY)
        == "resident-old"
    )
    assert "terminal (failed)" in (
        claimed.context.get("_conversation_claim_release_reason") or ""
    )

    replacement_claim = await claim_operator_messages(
        sweep_db,
        project_id=project.id,
        session_id="resident-current",
        task_id=live_task.id,
        worker_generation=2,
        incarnation_id=uuid.uuid4(),
        message_ids=[claimed.id, sibling.id],
        lease_seconds=45,
    )
    assert replacement_claim is not None
    assert {row.id for row in replacement_claim.rows} == {claimed.id, sibling.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["resolved", "open"])
async def test_dead_activity_claim_release_preserves_terminal_sibling(
    sweep_db, terminal_status
):
    """A terminal member cannot strand an unresolved member of its old claim."""
    project = await _make_project(sweep_db)
    dead_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-old",
        generation=1,
        status=TaskStatus.FAILED,
    )
    responder_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-responder",
        generation=1,
        status=TaskStatus.COMPLETED,
    )
    live_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-current",
        generation=2,
        status=TaskStatus.IN_PROGRESS,
    )
    _set_resident_authority(
        project,
        live_task,
        session_id="resident-current",
        generation=2,
    )
    sweep_db.add(project)
    await sweep_db.commit()

    stale = _minutes_ago(EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES + 5)
    claim_id = uuid.uuid4()
    incarnation_id = uuid.uuid4()
    claimed = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        message_type=MessageType.QUESTION,
        content="unanswered member",
        handled_by_task_id=dead_task.id,
        resident_session_id="resident-old",
        conversation_claim_id=claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=incarnation_id,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=1,
    )
    resolved_at = _utcnow()
    terminal = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        message_type=MessageType.BUG_REPORT,
        content="completed member",
        status=terminal_status,
        handling_state=MessageHandlingState.RESPONDED,
        handled_by_task_id=responder_task.id,
        resident_session_id="resident-old",
        conversation_claim_id=claim_id,
        conversation_claim_generation=1,
        conversation_claim_incarnation=incarnation_id,
        conversation_claim_expires_at=stale,
        created_at=stale + timedelta(seconds=1),
        updated_at=resolved_at,
        sequence=2,
    )
    terminal_snapshot = (
        terminal.status,
        terminal.handling_state,
        terminal.handled_by_task_id,
        terminal.conversation_claim_id,
        terminal.conversation_claim_generation,
        terminal.conversation_claim_incarnation,
        terminal.conversation_claim_expires_at,
        terminal.updated_at,
    )

    assert await sweep_release_expired_activity_claims(sweep_db, _utcnow()) == 1
    await sweep_db.refresh(claimed)
    await sweep_db.refresh(terminal)
    assert claimed.handling_state == MessageHandlingState.PENDING
    assert claimed.conversation_claim_id is None
    assert claimed.handled_by_task_id is None
    assert (
        terminal.status,
        terminal.handling_state,
        terminal.handled_by_task_id,
        terminal.conversation_claim_id,
        terminal.conversation_claim_generation,
        terminal.conversation_claim_incarnation,
        terminal.conversation_claim_expires_at,
        terminal.updated_at,
    ) == terminal_snapshot

    replacement_claim = await claim_operator_messages(
        sweep_db,
        project_id=project.id,
        session_id="resident-current",
        task_id=live_task.id,
        worker_generation=2,
        incarnation_id=uuid.uuid4(),
        message_ids=[claimed.id],
        lease_seconds=45,
    )
    assert replacement_claim is not None
    assert [row.id for row in replacement_claim.rows] == [claimed.id]


@pytest.mark.asyncio
async def test_expired_activity_claim_current_authority_is_preserved(sweep_db):
    """Clock staleness alone never steals from the authoritative Resident."""
    project = await _make_project(sweep_db)
    task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-current",
        generation=4,
        status=TaskStatus.IN_PROGRESS,
    )
    _set_resident_authority(
        project,
        task,
        session_id="resident-current",
        generation=4,
    )
    sweep_db.add(project)
    await sweep_db.commit()
    stale = _minutes_ago(EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES + 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        handled_by_task_id=task.id,
        conversation_claim_generation=4,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
    )

    assert await sweep_release_expired_activity_claims(sweep_db, _utcnow()) == 0
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.CLAIMED
    assert msg.conversation_claim_id is not None


@pytest.mark.asyncio
async def test_expired_activity_claim_superseded_generation_is_released(sweep_db):
    """Current Project authority fences an older still-IN_PROGRESS task."""
    project = await _make_project(sweep_db)
    old_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-old",
        generation=6,
        status=TaskStatus.IN_PROGRESS,
    )
    current_task = await _add_resident_task(
        sweep_db,
        project,
        session_id="resident-current",
        generation=7,
        status=TaskStatus.IN_PROGRESS,
    )
    _set_resident_authority(
        project,
        current_task,
        session_id="resident-current",
        generation=7,
    )
    sweep_db.add(project)
    await sweep_db.commit()
    stale = _minutes_ago(EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES + 5)
    msg = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        handled_by_task_id=old_task.id,
        conversation_claim_generation=6,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
    )

    assert await sweep_release_expired_activity_claims(sweep_db, _utcnow()) == 1
    await sweep_db.refresh(msg)
    assert msg.handling_state == MessageHandlingState.PENDING
    assert "superseded by" in (
        msg.context.get("_conversation_claim_release_reason") or ""
    )


@pytest.mark.asyncio
async def test_activity_sweep_preserves_live_nonresident_and_conversation_claims(
    sweep_db,
):
    """Ambiguous live owners and every conversation-surface group fail closed."""
    project = await _make_project(sweep_db)
    live_nonresident = Task(
        project_id=project.id,
        title="live non-Resident task",
        payloads=[],
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    dead_resident = Task(
        project_id=project.id,
        title="dead Resident task",
        payloads=[],
        status=TaskStatus.FAILED,
        context={"_resident_session": True},
    )
    sweep_db.add_all([live_nonresident, dead_resident])
    await sweep_db.commit()
    stale = _minutes_ago(EXPIRED_ACTIVITY_CLAIM_RELEASE_AFTER_MINUTES + 5)
    activity = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        handled_by_task_id=live_nonresident.id,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=1,
    )
    conversation = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.CONVERSATION,
        handled_by_task_id=dead_resident.id,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=2,
    )
    mixed_claim_id = uuid.uuid4()
    mixed_incarnation = uuid.uuid4()
    mixed_activity = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.ACTIVITY,
        handled_by_task_id=dead_resident.id,
        conversation_claim_id=mixed_claim_id,
        conversation_claim_incarnation=mixed_incarnation,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=3,
    )
    mixed_conversation = await _add_claimed_message(
        sweep_db,
        project,
        surface=MessageSurface.CONVERSATION,
        handled_by_task_id=dead_resident.id,
        conversation_claim_id=mixed_claim_id,
        conversation_claim_incarnation=mixed_incarnation,
        conversation_claim_expires_at=stale,
        created_at=stale,
        updated_at=stale,
        sequence=4,
    )

    assert await sweep_release_expired_activity_claims(sweep_db, _utcnow()) == 0
    await sweep_db.refresh(activity)
    await sweep_db.refresh(conversation)
    await sweep_db.refresh(mixed_activity)
    await sweep_db.refresh(mixed_conversation)
    assert activity.handling_state == MessageHandlingState.CLAIMED
    assert conversation.handling_state == MessageHandlingState.CLAIMED
    assert mixed_activity.handling_state == MessageHandlingState.CLAIMED
    assert mixed_conversation.handling_state == MessageHandlingState.CLAIMED
