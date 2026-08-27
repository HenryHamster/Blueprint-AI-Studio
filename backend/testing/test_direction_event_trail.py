"""Tests for the direction lifecycle event trail writer (direction_event_trail.py).

Covers:
  - created event when prior_row is None
  - status_changed event when status changes
  - no event when status is unchanged
  - pruned event via record_direction_pruned
  - reason and actor extraction from direction dict
"""

import uuid
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.direction_event_trail import (
    maybe_record_direction_event,
    record_direction_pruned,
    _extract_reason,
    _extract_actor,
)
from app.models import DirectionEvent


# ── helper fixtures ─────────────────────────────────────────────────────────

def _make_direction(status="active", **extra):
    base = {"id": "d-test01", "status": status, "text": "test direction"}
    base.update(extra)
    return base


def _make_prior_row(status="active"):
    row = MagicMock()
    row.status = status
    return row


def _make_session():
    session = AsyncMock()
    session.add = MagicMock()
    return session


# ── _extract_reason ──────────────────────────────────────────────────────────

def test_extract_reason_paused_reason():
    d = _make_direction(paused_reason="too many failures")
    assert _extract_reason(d, "active", "paused") == "too many failures"


def test_extract_reason_retirement_reason():
    d = _make_direction(retirement_reason="work completed")
    assert _extract_reason(d, "active", "retired") == "work completed"


def test_extract_reason_notes_are_not_transition_reasons():
    d = _make_direction(notes=[{"text": "unrelated note"}])
    assert _extract_reason(d, "active", "paused") is None


def test_extract_reason_none():
    d = _make_direction()
    assert _extract_reason(d, "active", "paused") is None


# ── _extract_actor ───────────────────────────────────────────────────────────

def test_extract_actor_retired_by():
    d = _make_direction(retired_by="agent-007")
    assert _extract_actor(d) == "agent-007"


def test_extract_actor_paused_by():
    d = _make_direction(paused_by="operator")
    assert _extract_actor(d) == "operator"


def test_extract_actor_none():
    d = _make_direction()
    assert _extract_actor(d) is None


# ── maybe_record_direction_event ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_created_when_no_prior_row():
    session = _make_session()
    d = _make_direction(status="active")
    pid = uuid.uuid4()
    await maybe_record_direction_event(session, pid, d, prior_row=None)
    assert session.add.called
    added = session.add.call_args[0][0]
    assert isinstance(added, DirectionEvent)
    assert added.event_kind == "created"
    assert added.prior_status is None
    assert added.new_status == "active"
    assert added.project_id == pid
    assert added.direction_key == "d-test01"


@pytest.mark.asyncio
async def test_event_status_changed():
    session = _make_session()
    d = _make_direction(status="paused", paused_reason="churn")
    pid = uuid.uuid4()
    prior = _make_prior_row(status="active")
    await maybe_record_direction_event(session, pid, d, prior_row=prior)
    assert session.add.called
    added = session.add.call_args[0][0]
    assert added.event_kind == "status_changed"
    assert added.prior_status == "active"
    assert added.new_status == "paused"
    assert added.reason == "churn"


@pytest.mark.asyncio
async def test_bare_pause_event_reports_no_reason_not_the_activation_note():
    """End-to-end pin for the production shape that motivated this fix.

    The strategist's update_direction pause path wrote status only - no
    paused_reason - so the event row fell back to whatever note was last on the
    direction.  For a direction previously resumed by staged activation that
    note said "Auto-activated ... staged work resumed", so the trail recorded an
    active->paused transition whose stated reason was an ACTIVATION.  The row
    must now carry no reason rather than a contradictory one.
    """
    session = _make_session()
    d = _make_direction(
        status="paused",
        notes=[
            {
                "text": "Auto-activated: v32 promoted - staged work resumed",
                "added_at": "2026-08-20T12:00:00",
            }
        ],
    )
    await maybe_record_direction_event(
        session, uuid.uuid4(), d, prior_row=_make_prior_row(status="active")
    )
    added = session.add.call_args[0][0]
    assert added.event_kind == "status_changed"
    assert added.prior_status == "active"
    assert added.new_status == "paused"
    assert added.reason is None
    # The snapshot still carries the note, so nothing is lost - only the
    # false CLAIM that the note explains this transition.
    assert added.snapshot_data["notes"][-1]["text"].startswith("Auto-activated")


@pytest.mark.asyncio
async def test_no_event_when_status_unchanged():
    session = _make_session()
    d = _make_direction(status="active")
    prior = _make_prior_row(status="active")
    await maybe_record_direction_event(session, uuid.uuid4(), d, prior_row=prior)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_event_terminal_status_change():
    session = _make_session()
    d = _make_direction(status="expired", retirement_reason="success evidence met")
    prior = _make_prior_row(status="active")
    await maybe_record_direction_event(session, uuid.uuid4(), d, prior_row=prior)
    added = session.add.call_args[0][0]
    assert added.new_status == "expired"
    assert added.reason == "success evidence met"


@pytest.mark.asyncio
async def test_event_recovery_paused_to_active():
    session = _make_session()
    d = _make_direction(status="active")
    prior = _make_prior_row(status="paused")
    await maybe_record_direction_event(session, uuid.uuid4(), d, prior_row=prior)
    added = session.add.call_args[0][0]
    assert added.event_kind == "status_changed"
    assert added.prior_status == "paused"
    assert added.new_status == "active"


@pytest.mark.asyncio
async def test_event_snapshot_data_captured():
    session = _make_session()
    d = {"id": "d-snap01", "status": "active", "priority": "high", "extra_field": "value"}
    await maybe_record_direction_event(session, uuid.uuid4(), d, prior_row=None)
    added = session.add.call_args[0][0]
    assert added.snapshot_data == d


@pytest.mark.asyncio
async def test_exception_swallowed(caplog):
    session = AsyncMock()
    session.add = MagicMock(side_effect=RuntimeError("db fail"))
    d = _make_direction(status="active")
    # Should not raise; exception should be logged
    await maybe_record_direction_event(session, uuid.uuid4(), d, prior_row=None)
    # function returns without raising


# ── record_direction_pruned ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pruned_event_written():
    session = _make_session()
    pid = uuid.uuid4()
    last_data = {"id": "d-prune01", "status": "active", "text": "pruned direction"}
    await record_direction_pruned(session, pid, "d-prune01", last_data)
    assert session.add.called
    added = session.add.call_args[0][0]
    assert isinstance(added, DirectionEvent)
    assert added.event_kind == "pruned"
    assert added.direction_key == "d-prune01"
    assert added.prior_status == "active"
    assert added.new_status is None
    assert added.snapshot_data == last_data


@pytest.mark.asyncio
async def test_pruned_event_with_none_data():
    session = _make_session()
    await record_direction_pruned(session, uuid.uuid4(), "d-gone01", None)
    added = session.add.call_args[0][0]
    assert added.event_kind == "pruned"
    assert added.prior_status is None
    assert added.snapshot_data is None


@pytest.mark.asyncio
async def test_pruned_exception_swallowed():
    session = AsyncMock()
    session.add = MagicMock(side_effect=RuntimeError("fail"))
    # Should not raise
    await record_direction_pruned(session, uuid.uuid4(), "d-x", {"status": "active"})


# ── upsert_direction integration — status_changed event via real write path ──

def _make_upsert_session(existing_status: str | None):
    """Build a mock session whose execute() returns a row with the given status."""
    session = AsyncMock()
    session.add = MagicMock()
    if existing_status is None:
        # Simulate no existing row (INSERT path)
        existing_row = None
    else:
        existing_row = MagicMock()
        existing_row.status = existing_status
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=existing_row)
    session.execute = AsyncMock(return_value=execute_result)
    return session, existing_row


@pytest.mark.asyncio
async def test_upsert_direction_records_status_changed_event():
    """upsert_direction must emit a status_changed event when the row status changes.

    Regression guard for the pre-mutation snapshot bug: previously, existing.status
    was overwritten before maybe_record_direction_event read it, so prior_status
    equalled new_status and no event was recorded.
    """
    from app import directions_repo

    session, _existing = _make_upsert_session(existing_status="active")
    pid = uuid.uuid4()
    direction = {"id": "d-evt01", "status": "paused", "text": "pause me", "paused_reason": "churn"}

    await directions_repo.upsert_direction(session, pid, direction)

    direction_events = [
        c[0][0] for c in session.add.call_args_list
        if isinstance(c[0][0], DirectionEvent)
    ]
    assert len(direction_events) == 1, "expected exactly one DirectionEvent added to session"
    evt = direction_events[0]
    assert evt.event_kind == "status_changed"
    assert evt.prior_status == "active"
    assert evt.new_status == "paused"
    assert evt.reason == "churn"


@pytest.mark.asyncio
async def test_upsert_direction_no_event_when_status_unchanged():
    """upsert_direction must not emit an event when the status is identical."""
    from app import directions_repo

    session, _existing = _make_upsert_session(existing_status="active")
    pid = uuid.uuid4()
    direction = {"id": "d-nochg01", "status": "active", "text": "no change"}

    await directions_repo.upsert_direction(session, pid, direction)

    direction_events = [
        c[0][0] for c in session.add.call_args_list
        if isinstance(c[0][0], DirectionEvent)
    ]
    assert len(direction_events) == 0, "no event should be emitted when status is unchanged"


@pytest.mark.asyncio
async def test_upsert_direction_records_created_event_for_new_row():
    """upsert_direction must emit a created event when there is no prior row."""
    from app import directions_repo

    session, _existing = _make_upsert_session(existing_status=None)
    pid = uuid.uuid4()
    direction = {"id": "d-new01", "status": "active", "text": "new direction"}

    await directions_repo.upsert_direction(session, pid, direction)

    direction_events = [
        c[0][0] for c in session.add.call_args_list
        if isinstance(c[0][0], DirectionEvent)
    ]
    assert len(direction_events) == 1
    assert direction_events[0].event_kind == "created"
    assert direction_events[0].prior_status is None
    assert direction_events[0].new_status == "active"
