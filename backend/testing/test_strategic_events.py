"""Tests for app.strategic_events — the realtime strategist-event helpers.

These verify the event-name CONTRACT the admin UI depends on:
  - the three event types (strategic_cycle_started / _completed / strategic_updated)
  - the tiny trigger-a-refetch payload shapes
  - that every emitter routes through broadcast_to_project (local WS + NOTIFY
    fanout) and stringifies the project id.
"""

import uuid

import pytest
from unittest.mock import AsyncMock, patch

import app.strategic_events as se
from app.strategic_events import (
    STRATEGIC_CYCLE_STARTED,
    STRATEGIC_CYCLE_COMPLETED,
    STRATEGIC_UPDATED,
    STRATEGIC_KIND_DIRECTIONS,
    STRATEGIC_KIND_PROBLEM_MODEL,
    STRATEGIC_KIND_RUNNING_UPDATE,
    emit_strategic_cycle_started,
    emit_strategic_cycle_completed,
    emit_strategic_updated,
)


def test_event_name_contract_is_stable():
    """The three event names are a frontend contract — pin them literally."""
    assert STRATEGIC_CYCLE_STARTED == "strategic_cycle_started"
    assert STRATEGIC_CYCLE_COMPLETED == "strategic_cycle_completed"
    assert STRATEGIC_UPDATED == "strategic_updated"
    assert STRATEGIC_KIND_DIRECTIONS == "directions"
    assert STRATEGIC_KIND_RUNNING_UPDATE == "running_update"
    assert STRATEGIC_KIND_PROBLEM_MODEL == "problem_model"


@pytest.mark.asyncio
async def test_emit_cycle_started_payload():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_started(project_id, task_id, 1)

    mock_bcast.assert_awaited_once_with(
        str(project_id),
        "strategic_cycle_started",
        {"task_id": str(task_id), "phase": 1},
    )


@pytest.mark.asyncio
async def test_emit_cycle_completed_payload():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_completed(project_id, task_id, 2)

    mock_bcast.assert_awaited_once_with(
        str(project_id),
        "strategic_cycle_completed",
        {"task_id": str(task_id), "phase": 2},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        STRATEGIC_KIND_DIRECTIONS,
        STRATEGIC_KIND_RUNNING_UPDATE,
        STRATEGIC_KIND_PROBLEM_MODEL,
    ],
)
async def test_emit_strategic_updated_payload(kind):
    project_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_updated(project_id, kind)

    mock_bcast.assert_awaited_once_with(
        str(project_id),
        "strategic_updated",
        {"kind": kind},
    )


@pytest.mark.asyncio
async def test_emit_payloads_stay_tiny_for_notify_ceiling():
    """Payloads must encode well under the 7KB NOTIFY ceiling (they're ids only)."""
    import json

    captured = {}

    async def _capture(pid, event, data):
        captured["data"] = data

    with patch.object(se, "broadcast_to_project", new=_capture):
        await emit_strategic_cycle_started(uuid.uuid4(), uuid.uuid4(), 1)
        assert len(json.dumps(captured["data"]).encode("utf-8")) < 200
        await emit_strategic_updated(uuid.uuid4(), STRATEGIC_KIND_DIRECTIONS)
        assert len(json.dumps(captured["data"]).encode("utf-8")) < 200
