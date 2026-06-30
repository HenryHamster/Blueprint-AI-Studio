"""Tests for app.strategic_events — the realtime strategist-event helpers.

These verify the event-name CONTRACT the admin UI depends on:
  - the three event types (strategic_cycle_started / _completed / strategic_updated)
  - the tiny trigger-a-refetch payload shapes
  - that every emitter routes through broadcast_to_project (local WS + NOTIFY
    fanout) and stringifies the project id.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

import app.strategic_events as se
from app.strategic_events import (
    STRATEGIC_CYCLE_COMPLETED,
    STRATEGIC_CYCLE_STARTED,
    STRATEGIC_KIND_DIRECTIONS,
    STRATEGIC_KIND_PROBLEM_MODEL,
    STRATEGIC_KIND_RUNNING_UPDATE,
    STRATEGIC_UPDATED,
    emit_strategic_cycle_completed,
    emit_strategic_cycle_started,
    emit_strategic_updated,
)


def _assert_v1_envelope(
    payload: dict,
    *,
    project_id: uuid.UUID,
    resource: str,
    action: str,
):
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project_id)
    assert payload["resource"] == resource
    assert payload["action"] == action
    assert payload["refetch"] == {
        "surfaces": ["strategic_session", "directions"],
    }


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

    mock_bcast.assert_awaited_once()
    assert mock_bcast.await_args.args[:2] == (
        str(project_id),
        "strategic_cycle_started",
    )
    payload = mock_bcast.await_args.args[2]
    assert payload["task_id"] == str(task_id)
    assert payload["phase"] == 1
    _assert_v1_envelope(
        payload,
        project_id=project_id,
        resource="strategic_cycle",
        action="started",
    )
    assert payload["strategic"]["kind"] == "cycle"
    assert payload["strategic"]["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_emit_cycle_completed_payload():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_completed(project_id, task_id, 2)

    mock_bcast.assert_awaited_once()
    assert mock_bcast.await_args.args[:2] == (
        str(project_id),
        "strategic_cycle_completed",
    )
    payload = mock_bcast.await_args.args[2]
    assert payload["task_id"] == str(task_id)
    assert payload["phase"] == 2
    _assert_v1_envelope(
        payload,
        project_id=project_id,
        resource="strategic_cycle",
        action="completed",
    )
    assert payload["strategic"]["kind"] == "cycle"
    assert payload["strategic"]["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_emit_cycle_started_payload_includes_phase_and_steering_context():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_started(
            project_id,
            task_id,
            1,
            cycle_id="sc-live123",
            steering_origin="operator",
            steering_excerpt="Focus on billing reliability. " * 20,
        )

    payload = mock_bcast.await_args.args[2]
    assert payload["task_id"] == str(task_id)
    assert payload["phase"] == 1
    assert payload["phase_number"] == 1
    assert payload["phase_label"] == "Architecture review"
    assert payload["phase_state"] == "phase1_running"
    assert payload["cycle_id"] == "sc-live123"
    assert payload["steering_origin"] == "operator"
    assert payload["steering_excerpt"].startswith("Focus on billing reliability.")
    assert len(payload["steering_excerpt"]) <= 240
    assert payload["strategic"] == {
        "kind": "cycle",
        "task_id": str(task_id),
        "phase": 1,
        "cycle_id": "sc-live123",
    }


@pytest.mark.asyncio
async def test_emit_cycle_completed_payload_includes_progress_and_outcome():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_completed(
            project_id,
            task_id,
            2,
            cycle_id="sc-done123",
            completion={
                "status": "completed",
                "created_direction_ids": ["d-new"],
                "retired_direction_ids": ["d-old"],
                "staged_direction_ids": ["d-next"],
                "updated_direction_ids": ["d-live"],
                "tasks_spawned": 3,
                "problem_model_changed": True,
                "running_update_changed": True,
                "status_message": "Strategic review complete",
            },
        )

    payload = mock_bcast.await_args.args[2]
    assert payload["task_id"] == str(task_id)
    assert payload["phase"] == 2
    assert payload["phase_number"] == 2
    assert payload["phase_label"] == "Strategic review"
    assert payload["phase_state"] == "cycle_done"
    assert payload["cycle_id"] == "sc-done123"
    completion = payload["completion"]
    assert completion["status"] == "completed"
    assert completion["created_direction_ids"] == ["d-new"]
    assert completion["retired_direction_ids"] == ["d-old"]
    assert completion["staged_direction_ids"] == ["d-next"]
    assert completion["updated_direction_ids"] == ["d-live"]
    assert completion["created_count"] == 1
    assert completion["retired_count"] == 1
    assert completion["staged_count"] == 1
    assert completion["updated_count"] == 1
    assert completion["tasks_spawned"] == 3
    assert completion["directions_changed"] is True
    assert completion["problem_model_changed"] is True
    assert completion["running_update_changed"] is True
    assert payload["strategic"] == {
        "kind": "cycle",
        "task_id": str(task_id),
        "phase": 2,
        "cycle_id": "sc-done123",
    }


@pytest.mark.asyncio
async def test_emit_cycle_completed_payload_includes_compact_challenge_summary():
    """A compact challenge summary on the completion dict rides into the payload."""
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_completed(
            project_id,
            task_id,
            2,
            cycle_id="sc-challenged",
            completion={
                "status": "completed",
                "strategic_judgment_challenge": {
                    "mode": "ok",
                    "verdict": "blocked",
                    "warned_count": 1,
                    "blocked_count": 2,
                },
            },
        )

    completion = mock_bcast.await_args.args[2]["completion"]
    assert completion["strategic_judgment_challenge"] == {
        "mode": "ok",
        "verdict": "blocked",
        "warned_count": 1,
        "blocked_count": 2,
    }


@pytest.mark.asyncio
async def test_emit_cycle_completed_payload_omits_challenge_when_absent():
    """Legacy cycles with no challenge record omit the key entirely."""
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_cycle_completed(
            project_id,
            task_id,
            2,
            cycle_id="sc-legacy",
            completion={"status": "completed"},
        )

    completion = mock_bcast.await_args.args[2]["completion"]
    assert "strategic_judgment_challenge" not in completion


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

    mock_bcast.assert_awaited_once()
    assert mock_bcast.await_args.args[:2] == (
        str(project_id),
        "strategic_updated",
    )
    payload = mock_bcast.await_args.args[2]
    assert payload["kind"] == kind
    _assert_v1_envelope(
        payload,
        project_id=project_id,
        resource="strategic_output",
        action="updated",
    )
    assert payload["strategic"] == {"kind": kind, "output_kind": kind}


@pytest.mark.asyncio
async def test_emit_strategic_updated_payload_includes_compact_linkage():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with patch.object(se, "broadcast_to_project", new=AsyncMock()) as mock_bcast:
        await emit_strategic_updated(
            project_id,
            STRATEGIC_KIND_DIRECTIONS,
            task_id=task_id,
            cycle_id="sc-live123",
            output_kind=STRATEGIC_KIND_DIRECTIONS,
            op="update_direction",
            changed_direction_ids=["d-live"],
            changed_count=1,
        )

    payload = mock_bcast.await_args.args[2]
    assert payload["kind"] == "directions"
    assert payload["task_id"] == str(task_id)
    assert payload["cycle_id"] == "sc-live123"
    assert payload["output_kind"] == "directions"
    assert payload["op"] == "update_direction"
    assert payload["changed_direction_ids"] == ["d-live"]
    assert payload["changed_count"] == 1
    _assert_v1_envelope(
        payload,
        project_id=project_id,
        resource="strategic_output",
        action="update_direction",
    )
    assert payload["strategic"] == {
        "kind": "directions",
        "output_kind": "directions",
        "task_id": str(task_id),
        "cycle_id": "sc-live123",
        "changed_count": 1,
    }


@pytest.mark.asyncio
async def test_emit_payloads_stay_tiny_for_notify_ceiling():
    """Payloads must encode well under the 7KB NOTIFY ceiling."""
    import json

    captured = {}

    async def _capture(pid, event, data):
        captured["data"] = data

    with patch.object(se, "broadcast_to_project", new=_capture):
        await emit_strategic_cycle_started(uuid.uuid4(), uuid.uuid4(), 1)
        assert len(json.dumps(captured["data"]).encode("utf-8")) < 500
        await emit_strategic_updated(uuid.uuid4(), STRATEGIC_KIND_DIRECTIONS)
        assert len(json.dumps(captured["data"]).encode("utf-8")) < 500
        await emit_strategic_cycle_completed(
            uuid.uuid4(),
            uuid.uuid4(),
            2,
            cycle_id="sc-size",
            completion={
                "status": "failed",
                "created_direction_ids": [f"d-{i}" for i in range(100)],
                "retired_direction_ids": [f"d-old-{i}" for i in range(100)],
                "staged_direction_ids": [f"d-next-{i}" for i in range(100)],
                "updated_direction_ids": [f"d-live-{i}" for i in range(100)],
                "failure_reason": "x" * 10_000,
                # Compact challenge summary stays four scalar fields regardless of
                # how large the underlying bounded record was.
                "strategic_judgment_challenge": {
                    "mode": "ok",
                    "verdict": "blocked",
                    "warned_count": 7,
                    "blocked_count": 11,
                },
            },
        )
        assert len(json.dumps(captured["data"]).encode("utf-8")) < 6_000
        # The compact challenge summary survived the soft-limit trim.
        assert captured["data"]["completion"]["strategic_judgment_challenge"] == {
            "mode": "ok",
            "verdict": "blocked",
            "warned_count": 7,
            "blocked_count": 11,
        }
