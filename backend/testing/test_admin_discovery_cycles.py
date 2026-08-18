"""Focused contracts for the mounted admin discovery-cycle resolution route."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from fastapi.routing import APIRoute
import pytest

import app.routers.admin_discovery_cycles as admin_discovery_cycles
from app.discovery_review import DiscoveryCycleBusy
from app.routers.directions import router as directions_router


def _payload(
    *, action: str = "park"
) -> admin_discovery_cycles.DiscoveryResolutionPayload:
    return admin_discovery_cycles.DiscoveryResolutionPayload(
        cycle_id=uuid.uuid4(),
        cycle_revision=3,
        action=action,
        next_bet={"claim": "Run the bounded follow-up"},
        note="Operator resolution",
    )


def _mock_dependencies(mocker, *, resolve_result=None, resolve_error=None):
    session = AsyncMock()
    require_member = mocker.patch.object(
        admin_discovery_cycles,
        "_get_user_project_with_role",
        new=AsyncMock(),
    )
    lock_project = mocker.patch.object(
        admin_discovery_cycles,
        "lock_project_or_429",
        new=AsyncMock(),
    )
    resolve_cycle = mocker.patch.object(
        admin_discovery_cycles,
        "resolve_discovery_cycle",
        new=AsyncMock(return_value=resolve_result, side_effect=resolve_error),
    )
    return session, require_member, lock_project, resolve_cycle


def test_discovery_resolution_route_is_mounted_once_under_admin_directions():
    expected_path = (
        "/api/admin/projects/{project_id}/directions/{direction_id}/"
        "discovery-resolution"
    )
    matches = [
        route
        for route in directions_router.routes
        if isinstance(route, APIRoute)
        and route.path == expected_path
        and "POST" in (route.methods or set())
    ]

    assert len(matches) == 1
    assert (
        matches[0].endpoint is admin_discovery_cycles.resolve_direction_discovery_cycle
    )


@pytest.mark.asyncio
async def test_discovery_resolution_route_preserves_resolution_contract(mocker):
    project_id = uuid.uuid4()
    direction_id = "direction-1"
    payload = _payload()
    resolved = SimpleNamespace(id=payload.cycle_id, generation=7)
    session, require_member, lock_project, resolve_cycle = _mock_dependencies(
        mocker,
        resolve_result=resolved,
    )

    response = await admin_discovery_cycles.resolve_direction_discovery_cycle(
        project_id,
        direction_id,
        payload,
        session=session,
        current_user={"user_id": "operator-1"},
    )

    require_member.assert_awaited_once_with(project_id, "operator-1", session, "member")
    lock_project.assert_awaited_once_with(session, project_id)
    resolve_cycle.assert_awaited_once_with(
        session,
        project_id=project_id,
        direction_key=direction_id,
        cycle_id=payload.cycle_id,
        cycle_revision=3,
        action="park",
        next_bet=payload.next_bet,
        note="Operator resolution",
        skip_locked=True,
    )
    session.commit.assert_awaited_once_with()
    assert response == {
        "status": "resolved",
        "action": "park",
        "cycle_id": str(payload.cycle_id),
        "generation": 7,
    }


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_headers"),
    [
        (ValueError("stale cycle revision"), 409, None),
        (DiscoveryCycleBusy("cycle is busy"), 429, {"Retry-After": "2"}),
    ],
)
@pytest.mark.asyncio
async def test_discovery_resolution_route_preserves_error_mapping(
    mocker,
    error,
    expected_status,
    expected_headers,
):
    session, _, _, _ = _mock_dependencies(mocker, resolve_error=error)

    with pytest.raises(HTTPException) as exc_info:
        await admin_discovery_cycles.resolve_direction_discovery_cycle(
            uuid.uuid4(),
            "direction-1",
            _payload(),
            session=session,
            current_user={"user_id": "operator-1"},
        )

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.detail == str(error)
    assert exc_info.value.headers == expected_headers
    session.commit.assert_not_awaited()


def _fake_cycle(
    *,
    state="reviewed",
    revision=2,
    generation=1,
    verdict="advance",
    review_result=None,
    bet_snapshot=None,
    review_task_id=None,
):
    now = datetime(2026, 8, 17, 12, 0, 0)
    return SimpleNamespace(
        id=uuid.uuid4(),
        state=state,
        revision=revision,
        generation=generation,
        verdict=verdict,
        review_result=review_result,
        bet_snapshot=bet_snapshot or {},
        review_task_id=review_task_id,
        created_at=now,
        updated_at=now,
        reviewed_at=now,
    )


def _fake_task(*, task_id, evidence_packet_manifest):
    return SimpleNamespace(
        id=task_id,
        first_payload={"evidence_packet_manifest": evidence_packet_manifest},
    )


def _execute_result(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def test_discovery_cycles_route_is_mounted_once_under_admin_directions():
    expected_path = (
        "/api/admin/projects/{project_id}/directions/{direction_id}/"
        "discovery-cycles"
    )
    matches = [
        route
        for route in directions_router.routes
        if isinstance(route, APIRoute)
        and route.path == expected_path
        and "GET" in (route.methods or set())
    ]

    assert len(matches) == 1
    assert (
        matches[0].endpoint is admin_discovery_cycles.list_direction_discovery_cycles
    )


@pytest.mark.asyncio
async def test_discovery_cycles_route_surfaces_operator_question_and_binding_authority(
    mocker,
):
    # The motivating incident: an overflowing receipt forced ask_operator on
    # a healthy cohort, and the operator had no read surface for the
    # resulting operator_question or WHY the decision packet stopped being
    # binding. This asserts both are now readable off the cycle history.
    project_id = uuid.uuid4()
    direction_id = "direction-1"
    review_task_id = uuid.uuid4()
    blocked_cycle = _fake_cycle(
        state="review_blocked",
        verdict=None,
        review_result={
            "status": "DECISION_FAILED",
            "operator_question": "Evidence overflowed the packet budget; advance anyway?",
            "summary": "5 merged PRs landed cleanly but the receipt could not fit.",
        },
        bet_snapshot={"mode": "build", "claim": "Ship the bounded rollout"},
        review_task_id=review_task_id,
    )
    reviewed_cycle = _fake_cycle(
        state="reviewed",
        verdict="advance",
        review_result={
            "status": "OK",
            "operator_question": None,
            "summary": "Advanced on complete evidence.",
        },
        review_task_id=None,
    )
    task = _fake_task(
        task_id=review_task_id,
        evidence_packet_manifest={
            "binding_decision_allowed": False,
            "required_evidence_omitted": True,
        },
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _execute_result([blocked_cycle, reviewed_cycle]),
            _execute_result([task]),
        ]
    )
    require_member = mocker.patch.object(
        admin_discovery_cycles,
        "_get_user_project_with_role",
        new=AsyncMock(),
    )

    response = await admin_discovery_cycles.list_direction_discovery_cycles(
        project_id,
        direction_id,
        limit=5,
        session=session,
        current_user={"user_id": "operator-1"},
    )

    require_member.assert_awaited_once_with(project_id, "operator-1", session, "member")
    assert len(response.cycles) == 2

    blocked = response.cycles[0]
    assert blocked.id == blocked_cycle.id
    assert blocked.state == "review_blocked"
    assert blocked.verdict is None
    assert (
        blocked.operator_question
        == "Evidence overflowed the packet budget; advance anyway?"
    )
    assert blocked.review_summary == (
        "5 merged PRs landed cleanly but the receipt could not fit."
    )
    assert blocked.bet_snapshot == {"mode": "build", "claim": "Ship the bounded rollout"}
    assert blocked.binding_decision_allowed is False

    reviewed = response.cycles[1]
    assert reviewed.verdict == "advance"
    assert reviewed.operator_question is None
    # No review_task_id on this cycle -- binding authority isn't derivable,
    # and that must read as "unknown", never as a false "still allowed".
    assert reviewed.binding_decision_allowed is None


@pytest.mark.asyncio
async def test_discovery_cycles_route_skips_task_lookup_with_no_review_task(mocker):
    project_id = uuid.uuid4()
    direction_id = "direction-1"
    cycle = _fake_cycle(state="open", verdict=None, review_task_id=None)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_execute_result([cycle])])
    mocker.patch.object(
        admin_discovery_cycles,
        "_get_user_project_with_role",
        new=AsyncMock(),
    )

    response = await admin_discovery_cycles.list_direction_discovery_cycles(
        project_id,
        direction_id,
        limit=5,
        session=session,
        current_user={"user_id": "operator-1"},
    )

    assert session.execute.await_count == 1
    assert len(response.cycles) == 1
    assert response.cycles[0].binding_decision_allowed is None
    assert response.cycles[0].operator_question is None
    assert response.cycles[0].review_result is None
