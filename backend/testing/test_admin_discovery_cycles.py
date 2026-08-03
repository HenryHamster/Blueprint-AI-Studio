"""Focused contracts for the mounted admin discovery-cycle resolution route."""

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
