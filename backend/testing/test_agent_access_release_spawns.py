"""Focused release-window Strategic spawn evidence tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.acceptance_criteria import CRITERION_RELEASE_VERSION_CONTEXT_KEY
from app.auth import get_current_user, get_current_user_optional, require_admin
from app.main import app
from app.models import (
    DiscoveryCycle,
    Project,
    ServiceCredential,
    StrategicSpawnRequest,
    TASK_ORIGINATION_STRATEGIST,
    Task,
    TaskStatus,
)
from app.release_models import ReleaseVersion, VersionAcceptanceCriterion
from app.service_credentials import (
    hash_service_token,
    mint_service_token,
    request_allowed,
)
from app.strategic_spawn_requests import stable_replay_specification_hash
from app.time_utils import _utcnow, utc_iso
from testing.factories import make_membership


_WORKDAY_PACKET_ID = uuid.UUID("00000000-0000-0000-0000-000000000111")
_WORKDAY_CLAIM_ID = uuid.UUID("00000000-0000-0000-0000-000000000222")


@asynccontextmanager
async def _real_auth_client() -> AsyncIterator[AsyncClient]:
    saved_user = app.dependency_overrides.pop(get_current_user, None)
    saved_optional = app.dependency_overrides.pop(get_current_user_optional, None)
    saved_admin = app.dependency_overrides.pop(require_admin, None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if saved_user is not None:
            app.dependency_overrides[get_current_user] = saved_user
        if saved_optional is not None:
            app.dependency_overrides[get_current_user_optional] = saved_optional
        if saved_admin is not None:
            app.dependency_overrides[require_admin] = saved_admin


async def _seed_reader(
    db_session,
    project_id: uuid.UUID,
    *,
    scopes: list[str] | None = None,
) -> str:
    token = mint_service_token()
    credential = ServiceCredential(
        name=f"release-evidence-{uuid.uuid4().hex[:8]}",
        principal_user_id=f"service:release-evidence:{uuid.uuid4()}",
        token_hash=hash_service_token(token),
        token_prefix=token[:12],
        scopes=list(scopes or ["coding-agent:read"]),
        created_by="test-harness",
    )
    db_session.add(credential)
    db_session.add(
        make_membership(
            project_id,
            user_id=credential.principal_user_id,
            role="viewer",
        )
    )
    await db_session.commit()
    return token


def _request_specification(
    *,
    version_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    task_type: str,
    criterion_ids: list[str],
    depends_on: list[str],
) -> dict:
    return {
        "task": {
            "title": f"Resident-authored {task_type}",
            "description": "private specification must not be projected",
            "payloads": [
                {
                    "type": task_type,
                    "prompt": "Authorization: Bearer never-project-this",
                }
            ],
            "context": {
                CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version_id),
                "spawned_by": "strategic_direct",
                "parent_strategic_task_id": str(parent_task_id),
                "_workday_consultation_provenance": {
                    "packet_id": str(_WORKDAY_PACKET_ID),
                    "workday_claim_id": str(_WORKDAY_CLAIM_ID),
                },
            },
            "criterion_ids": list(criterion_ids),
            "depends_on": list(depends_on),
        }
    }


def _spawn_request(
    *,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    child: Task,
    created_at,
    admission_reason: str = "immediate",
    dispatched_at=None,
) -> StrategicSpawnRequest:
    task_type = str(child.payloads[0]["type"])
    specification = _request_specification(
        version_id=version_id,
        parent_task_id=parent_task_id,
        task_type=task_type,
        criterion_ids=list(child.criterion_ids),
        depends_on=list(child.depends_on),
    )
    return StrategicSpawnRequest(
        project_id=project_id,
        parent_task_id=parent_task_id,
        idempotency_key=f"release-evidence-{uuid.uuid4()}",
        task_type=task_type,
        specification=specification,
        specification_hash=stable_replay_specification_hash(specification),
        direction_id="d-1234abcd",
        state="dispatched",
        admission_reason=admission_reason,
        task_id=child.id,
        created_at=created_at,
        updated_at=dispatched_at or created_at,
        dispatched_at=dispatched_at,
    )


def _child(
    *,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    task_type: str,
    criterion_ids: list[str],
    created_at,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        project_id=project_id,
        title=f"Resident child {task_type}",
        payloads=[{"type": task_type, "prompt": "bounded work"}],
        context={
            CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version_id),
            "spawned_by": "strategic_direct",
            "parent_strategic_task_id": str(parent_task_id),
            "_workday_consultation_provenance": {
                "packet_id": str(_WORKDAY_PACKET_ID),
                "workday_claim_id": str(_WORKDAY_CLAIM_ID),
            },
        },
        criterion_ids=criterion_ids,
        depends_on=list(depends_on or []),
        created_by="STRATEGIC_AGENT",
        origination=TASK_ORIGINATION_STRATEGIST,
        status=TaskStatus.QUEUED,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_release_spawn_evidence_is_exact_private_and_fail_closed(
    override_get_session,
    db_session,
    monkeypatch,
):
    """A real scoped credential gets exact two-child proof and bounded failures."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = Project(name="Release spawn evidence", user_id="release-owner")
    foreign_project = Project(name="Foreign release evidence", user_id="foreign-owner")
    db_session.add_all([project, foreign_project])
    await db_session.flush()

    opened_at = _utcnow() - timedelta(minutes=10)
    successor_opened_at = _utcnow() + timedelta(minutes=10)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=57,
        status="promoted",
        opened_at=opened_at,
    )
    successor = ReleaseVersion(
        project_id=project.id,
        version_number=58,
        status="promoted",
        opened_at=successor_opened_at,
    )
    empty_version = ReleaseVersion(
        project_id=project.id,
        version_number=59,
        status="open",
        opened_at=successor_opened_at + timedelta(minutes=10),
    )
    foreign_version = ReleaseVersion(
        project_id=foreign_project.id,
        version_number=57,
        status="open",
        opened_at=opened_at,
    )
    parent = Task(
        project_id=project.id,
        title="Resident v57",
        payloads=[{"type": "strategic_review"}],
        created_by="ORCHESTRATOR",
        status=TaskStatus.IN_PROGRESS,
        created_at=opened_at + timedelta(seconds=1),
    )
    db_session.add_all(
        [version, successor, empty_version, foreign_version, parent]
    )
    await db_session.flush()
    db_session.add_all(
        [
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=version.id,
                criterion_key=criterion_key,
                text=f"Release evidence for {criterion_key}",
                created_by="strategist",
            )
            for criterion_key in ("c-00000001", "c-00000002", "c-deadbeef")
        ]
    )

    first = _child(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        task_type="create",
        criterion_ids=["c-00000001"],
        created_at=opened_at + timedelta(seconds=3),
    )
    second = _child(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        task_type="fix",
        criterion_ids=["c-00000002"],
        created_at=opened_at + timedelta(seconds=6),
        depends_on=[str(first.id)],
    )
    db_session.add_all([first, second])
    await db_session.flush()
    first_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=first,
        created_at=opened_at + timedelta(seconds=2),
        dispatched_at=opened_at + timedelta(seconds=4),
    )
    second_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=second,
        created_at=opened_at + timedelta(seconds=5),
        dispatched_at=opened_at + timedelta(seconds=7),
    )
    before_window = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=first,
        created_at=opened_at - timedelta(microseconds=1),
        dispatched_at=opened_at - timedelta(microseconds=1),
    )
    at_successor_boundary = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=first,
        created_at=successor_opened_at,
        dispatched_at=successor_opened_at,
    )
    db_session.add_all(
        [
            first_request,
            second_request,
            before_window,
            at_successor_boundary,
        ]
    )
    await db_session.commit()

    token = await _seed_reader(db_session, project.id)
    observer_token = await _seed_reader(
        db_session,
        project.id,
        scopes=["observer:read"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"/api/agent-access/v1/projects/{project.id}/release-versions/"
        f"{version.id}/strategic-spawn-requests"
    )
    assert request_allowed(["coding-agent:read"], "GET", url)
    assert request_allowed(["project-operator"], "GET", url)
    assert request_allowed(["coding-agent:read"], "GET", f"{url}/")
    assert not request_allowed(["observer:read"], "GET", url)
    assert not request_allowed(["coding-agent:read"], "POST", url)
    empty_url = (
        f"/api/agent-access/v1/projects/{project.id}/release-versions/"
        f"{empty_version.id}/strategic-spawn-requests"
    )
    async with _real_auth_client() as client:
        profile = await client.get("/api/agent-access/v1/me", headers=headers)
        unauthenticated = await client.get(url)
        observer_denied = await client.get(
            url,
            headers={"Authorization": f"Bearer {observer_token}"},
        )
        response = await client.get(url, headers=headers)
        first_page = await client.get(f"{url}?limit=1", headers=headers)
        second_page = await client.get(f"{url}?limit=1&offset=1", headers=headers)
        beyond_page = await client.get(f"{url}?limit=1&offset=99", headers=headers)
        foreign = await client.get(
            f"/api/agent-access/v1/projects/{foreign_project.id}/release-versions/"
            f"{foreign_version.id}/strategic-spawn-requests",
            headers=headers,
        )
        missing = await client.get(
            f"/api/agent-access/v1/projects/{project.id}/release-versions/"
            f"{uuid.uuid4()}/strategic-spawn-requests",
            headers=headers,
        )
        empty = await client.get(empty_url, headers=headers)

    assert profile.status_code == 200, profile.text
    assert "release.spawn_requests.list" in profile.json()["capabilities"]
    assert unauthenticated.status_code == 403, unauthenticated.text
    assert observer_denied.status_code == 403, observer_denied.text
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["validated_task_total"] == 2
    assert body["distinct_task_total"] == 2
    assert body["all_child_tasks_distinct"] is True
    assert body["two_task_dependency_chain_complete"] is True
    assert body["complete"] is True
    assert body["two_task_spawn_chain_complete"] is True
    assert body["release_version"]["window_end"] == utc_iso(successor_opened_at)
    assert [item["request_id"] for item in body["requests"]] == [
        str(first_request.id),
        str(second_request.id),
    ]
    assert [item["task_id"] for item in body["requests"]] == [
        str(first.id),
        str(second.id),
    ]
    serialized = json.dumps(body)
    assert str(before_window.id) not in serialized
    assert str(at_successor_boundary.id) not in serialized
    assert all(item["parent_task_id"] == str(parent.id) for item in body["requests"])
    assert all(item["evidence_complete"] is True for item in body["requests"])
    assert all(
        item["specification_hash_format_valid"] is True
        and item["specification_hash_matches_replay_specification"] is True
        for item in body["requests"]
    )
    assert all(item["workday_provenance_match"] is True for item in body["requests"])
    assert all(
        item["dependency_declaration_match"] is True
        for item in body["requests"]
    )
    assert all(
        item["discovery_cycle_link_match"] is True for item in body["requests"]
    )
    assert all(
        item["parent_created_by_orchestrator"] is True
        and item["parent_task_type"] == "strategic_review"
        and item["parent_task_type_recognized"] is True
        for item in body["requests"]
    )
    assert all(
        item["request_task_type_matches_child"] is True
        for item in body["requests"]
    )
    assert all(
        item["acceptance_criteria"]["declared_match"] is True
        and item["acceptance_criteria"]["canonical_active_match"] is True
        for item in body["requests"]
    )
    assert all(
        item["new_child_evidence_complete"] is True for item in body["requests"]
    )
    assert "private specification must not be projected" not in serialized
    assert "never-project-this" not in serialized
    assert "idempotency_key" not in serialized
    assert str(_WORKDAY_PACKET_ID) not in serialized
    assert str(_WORKDAY_CLAIM_ID) not in serialized

    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["total"] == 2
    assert first_page.json()["returned"] == 1
    assert first_page.json()["has_more"] is True
    assert first_page.json()["next_offset"] == 1
    assert first_page.json()["complete"] is False
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["returned"] == 1
    assert second_page.json()["has_more"] is False
    assert second_page.json()["complete"] is False
    assert second_page.json()["next_offset"] is None
    assert [
        first_page.json()["requests"][0]["request_id"],
        second_page.json()["requests"][0]["request_id"],
    ] == [str(first_request.id), str(second_request.id)]
    assert beyond_page.status_code == 200, beyond_page.text
    assert beyond_page.json()["total"] == 2
    assert beyond_page.json()["returned"] == 0
    assert beyond_page.json()["has_more"] is False
    assert beyond_page.json()["complete"] is False
    assert foreign.status_code == 404, foreign.text
    assert missing.status_code == 404, missing.text
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0
    assert empty.json()["complete"] is True
    assert empty.json()["two_task_spawn_chain_complete"] is False

    secret_criterion = "Authorization: Bearer criterion-secret"
    malformed = _child(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        task_type="create",
        criterion_ids=[
            "c-deadbeef",
            "c-deadbeef",
            "c-ffffffff",
            secret_criterion,
        ],
        created_at=opened_at + timedelta(seconds=6),
    )
    db_session.add(malformed)
    await db_session.flush()
    malformed_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=malformed,
        created_at=opened_at + timedelta(seconds=7),
        dispatched_at=opened_at + timedelta(seconds=7),
    )
    db_session.add(malformed_request)
    await db_session.commit()

    async with _real_auth_client() as client:
        invalid_response = await client.get(url, headers=headers)
    assert invalid_response.status_code == 200, invalid_response.text
    invalid_body = invalid_response.json()
    assert invalid_body["total"] == 3
    assert invalid_body["distinct_task_total"] == 3
    assert invalid_body["complete"] is False
    malformed_row = next(
        item
        for item in invalid_body["requests"]
        if item["request_id"] == str(malformed_request.id)
    )
    assert malformed_row["dispatch_timestamp_state"] == "present"
    assert malformed_row["dispatch_receipt_consistent"] is True
    assert malformed_row["evidence_complete"] is False
    assert malformed_row["acceptance_criteria"]["ids"] == ["c-deadbeef"]
    assert malformed_row["acceptance_criteria"]["invalid"] is True
    assert malformed_row["acceptance_criteria"]["invalid_count"] == 2
    assert malformed_row["acceptance_criteria"]["declared_match"] is False
    assert malformed_row["acceptance_criteria"]["canonical_active_match"] is False
    invalid_serialized = json.dumps(invalid_body)
    assert secret_criterion not in invalid_serialized
    assert "c-ffffffff" not in invalid_serialized

    deduplicated_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=second,
        created_at=opened_at + timedelta(seconds=8),
        admission_reason="active_work_deduplicated",
        dispatched_at=None,
    )
    db_session.add(deduplicated_request)
    await db_session.commit()

    async with _real_auth_client() as client:
        duplicate_response = await client.get(url, headers=headers)
    assert duplicate_response.status_code == 200, duplicate_response.text
    duplicate_body = duplicate_response.json()
    assert duplicate_body["total"] == 4
    assert duplicate_body["validated_task_total"] == 4
    assert duplicate_body["distinct_task_total"] == 3
    assert duplicate_body["all_child_tasks_distinct"] is False
    assert duplicate_body["complete"] is False
    assert duplicate_body["two_task_spawn_chain_complete"] is False
    duplicate_row = next(
        item
        for item in duplicate_body["requests"]
        if item["request_id"] == str(deduplicated_request.id)
    )
    assert duplicate_row["dispatch_timestamp_state"] == (
        "active_work_deduplicated_unstamped"
    )
    assert duplicate_row["dispatch_receipt_consistent"] is True
    assert duplicate_row["evidence_complete"] is True
    assert duplicate_row["new_child_evidence_complete"] is False

    invalid_timestamp_child = _child(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        task_type="review",
        criterion_ids=["c-00000001"],
        created_at=opened_at + timedelta(seconds=10),
    )
    db_session.add(invalid_timestamp_child)
    await db_session.flush()
    invalid_timestamp_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=invalid_timestamp_child,
        created_at=opened_at + timedelta(seconds=9),
        dispatched_at=opened_at + timedelta(seconds=11),
    )
    invalid_timestamp_request.updated_at = opened_at + timedelta(seconds=10)
    invalid_timestamp_request.specification_hash = "0" * 64
    db_session.add(invalid_timestamp_request)
    await db_session.commit()

    async with _real_auth_client() as client:
        invalid_timestamp_response = await client.get(url, headers=headers)
    assert invalid_timestamp_response.status_code == 200, (
        invalid_timestamp_response.text
    )
    invalid_timestamp_row = next(
        item
        for item in invalid_timestamp_response.json()["requests"]
        if item["request_id"] == str(invalid_timestamp_request.id)
    )
    assert invalid_timestamp_row["dispatch_timestamp_state"] == "invalid_order"
    assert invalid_timestamp_row["dispatch_receipt_consistent"] is False
    assert invalid_timestamp_row["specification_hash_format_valid"] is True
    assert invalid_timestamp_row[
        "specification_hash_matches_replay_specification"
    ] is False
    assert invalid_timestamp_row["specification_hash"] is None
    assert invalid_timestamp_row["new_child_dispatch"] is False
    assert invalid_timestamp_row["evidence_complete"] is False
    assert invalid_timestamp_row["new_child_evidence_complete"] is False

    foreign_cycle = DiscoveryCycle(
        project_id=foreign_project.id,
        direction_key="d-foreign1",
        outcome_kind="empirical",
        phase="probe",
        state="open",
        intent_snapshot={},
        bet_snapshot={},
    )
    invalid_parent = Task(
        project_id=project.id,
        title="Invalid non-Resident parent",
        payloads=[{"type": "research"}],
        created_by="STRATEGIC_AGENT",
        status=TaskStatus.IN_PROGRESS,
        created_at=opened_at + timedelta(seconds=11),
    )
    authority_mismatch_child = _child(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=invalid_parent.id,
        task_type="review",
        criterion_ids=["c-00000001"],
        created_at=opened_at + timedelta(seconds=13),
        depends_on=[str(first.id)],
    )
    authority_mismatch_child.discovery_cycle_id = foreign_cycle.id
    db_session.add_all(
        [foreign_cycle, invalid_parent, authority_mismatch_child]
    )
    await db_session.flush()
    authority_mismatch_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=invalid_parent.id,
        child=authority_mismatch_child,
        created_at=opened_at + timedelta(seconds=12),
        dispatched_at=opened_at + timedelta(seconds=14),
    )
    authority_mismatch_request.discovery_cycle_id = foreign_cycle.id
    authority_mismatch_request.task_type = "fix"
    mismatch_task = authority_mismatch_request.specification["task"]
    mismatch_task["context"][CRITERION_RELEASE_VERSION_CONTEXT_KEY] = str(
        successor.id
    )
    mismatch_packet_id = uuid.uuid4()
    mismatch_task["context"]["_workday_consultation_provenance"][
        "packet_id"
    ] = str(mismatch_packet_id)
    mismatch_task["criterion_ids"] = ["c-00000002"]
    mismatch_task["depends_on"] = []
    authority_mismatch_request.specification_hash = stable_replay_specification_hash(
        authority_mismatch_request.specification
    )
    db_session.add(authority_mismatch_request)
    await db_session.commit()

    async with _real_auth_client() as client:
        authority_mismatch_response = await client.get(url, headers=headers)
    assert authority_mismatch_response.status_code == 200, (
        authority_mismatch_response.text
    )
    authority_mismatch_body = authority_mismatch_response.json()
    authority_mismatch_row = next(
        item
        for item in authority_mismatch_body["requests"]
        if item["request_id"] == str(authority_mismatch_request.id)
    )
    assert authority_mismatch_row["declared_release_version_match"] is False
    assert authority_mismatch_row["task_release_version_match"] is True
    assert authority_mismatch_row["workday_provenance_match"] is False
    assert authority_mismatch_row["dependency_declaration_match"] is False
    assert authority_mismatch_row["discovery_cycle_link_match"] is False
    assert authority_mismatch_row["discovery_cycle_id"] is None
    assert authority_mismatch_row["parent_created_by_orchestrator"] is False
    assert authority_mismatch_row["parent_task_type"] is None
    assert authority_mismatch_row["parent_task_type_recognized"] is False
    assert authority_mismatch_row["request_task_type_matches_child"] is False
    assert authority_mismatch_row["acceptance_criteria"]["declared_match"] is False
    assert authority_mismatch_row["acceptance_criteria"][
        "canonical_active_match"
    ] is True
    assert authority_mismatch_row["evidence_complete"] is False
    assert authority_mismatch_body["two_task_spawn_chain_complete"] is False
    authority_mismatch_serialized = json.dumps(authority_mismatch_body)
    assert str(foreign_cycle.id) not in authority_mismatch_serialized
    assert str(mismatch_packet_id) not in authority_mismatch_serialized

    foreign_child = Task(
        project_id=foreign_project.id,
        title="Foreign child",
        payloads=[{"type": "create"}],
        context={},
        criterion_ids=["c-ffffffff"],
        created_by="STRATEGIC_AGENT",
        origination=TASK_ORIGINATION_STRATEGIST,
    )
    db_session.add(foreign_child)
    await db_session.flush()
    cross_tenant_request = _spawn_request(
        project_id=project.id,
        version_id=version.id,
        parent_task_id=parent.id,
        child=foreign_child,
        created_at=opened_at + timedelta(seconds=9),
        dispatched_at=opened_at + timedelta(seconds=9),
    )
    db_session.add(cross_tenant_request)
    await db_session.commit()

    async with _real_auth_client() as client:
        cross_tenant_response = await client.get(url, headers=headers)
    assert cross_tenant_response.status_code == 200, cross_tenant_response.text
    cross_tenant_body = cross_tenant_response.json()
    assert cross_tenant_body["total"] == 7
    assert cross_tenant_body["validated_task_total"] == 6
    cross_tenant_row = next(
        item
        for item in cross_tenant_body["requests"]
        if item["request_id"] == str(cross_tenant_request.id)
    )
    assert cross_tenant_row["task_id"] is None
    assert cross_tenant_row["evidence_complete"] is False
    assert str(foreign_child.id) not in json.dumps(cross_tenant_body)
