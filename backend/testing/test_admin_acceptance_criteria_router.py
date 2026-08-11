"""Tests for the admin acceptance-criteria endpoints.

Exercises:
  GET    /api/admin/projects/{project_id}/acceptance-criteria
  POST   /api/admin/projects/{project_id}/acceptance-criteria
  DELETE /api/admin/projects/{project_id}/acceptance-criteria/{criterion_key}
  POST   /api/admin/projects/{project_id}/acceptance-criteria/link-direction

Role-enforcement, happy path, 400/404/409 branches.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app import acceptance_criteria as ac
from app.models import (
    CriterionEvidence,
    Direction,
    Project,
    ProjectMember,
    ReleaseVersion,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)

TEST_USER_ID = "test-user-123"

_BASE = "/api/admin"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_project_member(session, role: str = "owner") -> Project:
    project = Project(name="AC-Router-Test", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role))
    await session.commit()
    return project


async def _make_version(session, project: Project, status: str = "open") -> ReleaseVersion:
    version = ReleaseVersion(project_id=project.id, version_number=1, status=status)
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def _make_direction(session, project: Project, key: str = "d-test1") -> Direction:
    direction = Direction(
        project_id=project.id,
        direction_key=key,
        data={"id": key, "text": "Test direction", "status": "active"},
        status="active",
    )
    session.add(direction)
    await session.commit()
    await session.refresh(direction)
    return direction


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/acceptance-criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_criteria_no_open_version(client: AsyncClient, db_session):
    """Returns empty list when project has no open release version."""
    project = await _make_project_member(db_session)

    resp = await client.get(f"{_BASE}/projects/{project.id}/acceptance-criteria")
    assert resp.status_code == 200
    data = resp.json()
    assert data["release_version_id"] is None
    assert data["criteria"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_criteria_with_open_version(client: AsyncClient, db_session):
    """Returns criteria summary when an open version exists."""
    project = await _make_project_member(db_session)
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="All tests pass",
        created_by="strategist",
    )

    resp = await client.get(f"{_BASE}/projects/{project.id}/acceptance-criteria")
    assert resp.status_code == 200
    data = resp.json()
    assert data["release_version_id"] == str(version.id)
    assert data["total"] == 1
    assert data["covered"] == 0
    assert data["uncovered"] == 1
    assert len(data["criteria"]) == 1
    assert data["criteria"][0]["criterion_key"] == criterion.criterion_key


@pytest.mark.asyncio
async def test_update_existing_evidence_contract_is_canonical_audited_and_scoped(
    client: AsyncClient, db_session
):
    """The live-shaped row binds strictly and leaves unrelated criteria untouched."""
    project = await _make_project_member(db_session, role="owner")
    version = await _make_version(db_session, project)
    target = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-production1",
        text="Release authorship is clean",
        status="active",
        created_by="strategist",
    )
    unrelated = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-unrelated1",
        text="Unrelated production criterion",
        status="active",
        created_by="operator",
    )
    db_session.add_all([target, unrelated])
    await db_session.commit()

    invalid = await client.patch(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{target.criterion_key}",
        json={
            "evidence_contract": {
                "type": "operator_authored_commits",
                "predicate": {"max_count": True},
            }
        },
    )
    assert invalid.status_code == 422

    contract = {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    }
    updated = await client.patch(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{target.criterion_key}/evidence-contract",
        json={"evidence_contract": contract},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["evidence_contract"] == contract
    assert body["evidence_contract_updated_by"] == TEST_USER_ID
    assert body["evidence_contract_updated_at"]

    readback = await client.get(
        f"{_BASE}/projects/{project.id}/acceptance-criteria"
    )
    assert readback.status_code == 200
    by_key = {item["criterion_key"]: item for item in readback.json()["criteria"]}
    assert by_key[target.criterion_key]["evidence_contract"] == contract
    assert by_key[unrelated.criterion_key]["evidence_contract"] is None


@pytest.mark.asyncio
async def test_list_criteria_requires_viewer_role(client: AsyncClient, db_session):
    """Project non-member receives 404 (project appears not found)."""
    other_project = Project(name="Other", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.get(f"{_BASE}/projects/{other_project.id}/acceptance-criteria")
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_member_can_write_text_but_cannot_activate_or_clear_machine_policy(
    client: AsyncClient, db_session
):
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)
    target = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-owner-policy",
        text="Trusted CI must verify the candidate",
        status="active",
        created_by="operator",
        evidence_contract={
            "type": "prepromotion_ci_build_smoke",
            "predicate": {"required": True},
        },
    )
    authorship_target = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-owner-authorship-policy",
        text="Operator authorship must remain clean",
        status="active",
        created_by="strategist",
        evidence_contract={
            "type": "operator_authored_commits",
            "predicate": {"max_count": 0},
        },
        evidence_contract_updated_by=TEST_USER_ID,
    )
    db_session.add_all([target, authorship_target])
    await db_session.commit()

    ordinary = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Member-authored prose criterion"},
    )
    assert ordinary.status_code == 200

    machine = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={
            "text": "Member must not spend Actions authority",
            "evidence_contract": {
                "type": "prepromotion_ci_build_smoke",
                "predicate": {"required": True},
            },
        },
    )
    assert machine.status_code == 403

    clear = await client.patch(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/"
        f"{target.criterion_key}/evidence-contract",
        json={"evidence_contract": None},
    )
    assert clear.status_code == 403

    delete = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{target.criterion_key}"
    )
    assert delete.status_code == 403

    target.status = "retracted"
    db_session.add(target)
    await db_session.commit()
    reinstate = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/"
        f"{target.criterion_key}/reinstate"
    )
    assert reinstate.status_code == 403

    delete_authorship = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/"
        f"{authorship_target.criterion_key}"
    )
    assert delete_authorship.status_code == 403

    authorship_target.status = "retracted"
    db_session.add(authorship_target)
    await db_session.commit()
    reinstate_authorship = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/"
        f"{authorship_target.criterion_key}/reinstate"
    )
    assert reinstate_authorship.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_reinstate_then_delete_external_ci_policy(
    client: AsyncClient, db_session
):
    project = await _make_project_member(db_session, role="owner")
    version = await _make_version(db_session, project)
    target = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-owner-external-policy",
        text="Trusted CI verifies the candidate",
        status="retracted",
        created_by="strategist",
        evidence_contract={
            "type": "prepromotion_ci_build_smoke",
            "predicate": {"required": True},
        },
        evidence_contract_updated_by=TEST_USER_ID,
    )
    db_session.add(target)
    await db_session.commit()

    reinstate = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/"
        f"{target.criterion_key}/reinstate"
    )
    assert reinstate.status_code == 200
    await db_session.refresh(target)
    assert target.status == "active"

    delete = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{target.criterion_key}"
    )
    assert delete.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria (create)
# DELETE /api/admin/projects/{project_id}/acceptance-criteria/{key} (delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_delete_acceptance_criterion(client: AsyncClient, db_session):
    """Creates and deletes criteria; exercises cap, de-dup, role, and open-version guards."""
    project = await _make_project_member(db_session, role="owner")
    version = await _make_version(db_session, project)

    # --- Create: happy path ---
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={
            "text": "All tests must pass",
            "evidence_contract": {
                "type": "operator_authored_commits",
                "predicate": {"max_count": 0},
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["criterion_key"].startswith("c-")
    assert data["text"] == "All tests must pass"
    assert data["evidence_contract"]["predicate"]["max_count"] == 0
    assert data["evidence_contract_updated_by"] == TEST_USER_ID
    assert data["evidence_contract_updated_at"]
    assert data["release_version_id"] == str(version.id)
    key1 = data["criterion_key"]
    created = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.criterion_key == key1
            )
        )
    ).scalar_one()
    assert created.created_by == "operator"
    assert created.evidence_contract_updated_at is not None
    assert created.evidence_contract_updated_by == TEST_USER_ID

    # Create a second distinct criterion.
    resp2 = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Coverage must be >= 80%"},
    )
    assert resp2.status_code == 200
    key2 = resp2.json()["criterion_key"]
    assert key2 != key1

    # --- link-direction de-dup: duplicate key in request stored only once ---
    direction = await _make_direction(db_session, project)
    resp_link = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": [key1, key1, key2]},
    )
    assert resp_link.status_code == 200
    linked_keys = resp_link.json()["criterion_keys"]
    assert linked_keys.count(key1) == 1, "duplicate key must be de-duped"
    assert key2 in linked_keys

    # --- Delete: happy path ---
    resp_del = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key1}",
    )
    assert resp_del.status_code == 200
    assert resp_del.json()["status"] == "deleted"
    assert resp_del.json()["criterion_key"] == key1

    # --- Delete: non-existent key => 404 ---
    resp_del2 = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key1}",
    )
    assert resp_del2.status_code == 404

    # --- Guard: empty text => 422 ---
    resp_empty = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "  "},
    )
    assert resp_empty.status_code == 422

    # --- Guard: no open version => 409 on create and delete ---
    version.status = "closed"
    db_session.add(version)
    await db_session.commit()

    resp_noversion = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Should fail"},
    )
    assert resp_noversion.status_code == 409

    resp_del_noversion = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key2}",
    )
    assert resp_del_noversion.status_code == 409

    # --- Guard: role enforcement (non-member => 403/404) ---
    other_project = Project(name="OtherProj", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp_role = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria",
        json={"text": "Blocked"},
    )
    assert resp_role.status_code in (403, 404)


@pytest.mark.asyncio
async def test_create_criterion_per_version_cap(client: AsyncClient, db_session):
    """Returns 409 once the per-version criterion cap is reached."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    # Fill up to the cap using the helper directly (avoid 50 HTTP calls).
    for i in range(ac.MAX_CRITERIA_PER_VERSION):
        await ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text=f"Criterion {i}",
        )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "One too many"},
    )
    assert resp.status_code == 409, f"expected 409 cap enforcement, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_cap_counts_only_active_criteria(client: AsyncClient, db_session):
    """Retracted criteria do not count toward the cap; a version at cap with some
    retracted rows can still accept new active criteria via the operator endpoint."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    # Fill to (cap - 1) with active criteria, plus 1 extra that we then retract.
    # Total rows = cap, but active rows = cap - 1, so there is still one slot.
    for i in range(ac.MAX_CRITERIA_PER_VERSION - 1):
        await ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text=f"Active {i}",
        )
    to_retract = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Will be retracted",
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=to_retract.criterion_key,
        reason="superseded by new work",
        retracted_by="test",
    )

    # Total rows = MAX_CRITERIA_PER_VERSION; active rows = MAX_CRITERIA_PER_VERSION - 1.
    # The endpoint should allow one more active criterion.
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Replacement criterion"},
    )
    assert resp.status_code == 200, (
        f"expected 200 (retracted rows should not block cap), got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert data["criterion_key"].startswith("c-")

    # Now we are exactly at the active cap — a further create should be rejected.
    resp2 = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "One too many"},
    )
    assert resp2.status_code == 409, (
        f"expected 409 after active cap reached, got {resp2.status_code}: {resp2.text}"
    )


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria/link-direction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_direction_happy_path(client: AsyncClient, db_session):
    """Links criterion keys to a direction and returns ok status."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="CI must pass",
    )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": [criterion.criterion_key],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["direction_key"] == direction.direction_key
    assert criterion.criterion_key in data["criterion_keys"]

    # Verify the DB row was updated
    await db_session.refresh(direction)
    assert criterion.criterion_key in direction.criterion_keys


@pytest.mark.asyncio
async def test_link_direction_empty_criterion_keys(client: AsyncClient, db_session):
    """Empty criterion_keys list clears existing links without error."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_link_direction_409_no_open_version(client: AsyncClient, db_session):
    """Returns 409 when the project has no open release version."""
    project = await _make_project_member(db_session)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": []},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_link_direction_400_unknown_criterion_key(client: AsyncClient, db_session):
    """Returns 400 when any supplied criterion_key does not exist for the project."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": ["c-nonexistent"],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_link_direction_400_dangling_key_returns_structured_error(
    client: AsyncClient, db_session
):
    """A dangling criterion key returns a structured 400 with active_criterion_keys.

    check_criterion_key_write normalises both dangling and inactive failures
    into the same structured error shape so callers can inspect
    error='criterion_key_invalid', classification, keys, and active_criterion_keys.
    """
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    # Create one active criterion so the active_criterion_keys list is non-empty.
    active = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Active criterion",
    )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": ["c-does-not-exist"],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body.get("error", {}).get("message", body)
    assert detail.get("error") == "criterion_key_invalid"
    assert detail.get("classification") == "dangling"
    assert "c-does-not-exist" in detail.get("keys", [])
    assert active.criterion_key in detail.get("active_criterion_keys", [])


@pytest.mark.asyncio
async def test_link_direction_404_unknown_direction(client: AsyncClient, db_session):
    """Returns 404 when direction_key does not exist in the project."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": "d-does-not-exist", "criterion_keys": []},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_direction_requires_member_role(client: AsyncClient, db_session):
    """Project non-member receives 403/404 on the link-direction endpoint."""
    other_project = Project(name="Other2", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria/link-direction",
        json={"direction_key": "d-x", "criterion_keys": []},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_link_direction_400_rejects_closed_version_criterion_key(
    client: AsyncClient, db_session
):
    """A criterion key from a closed/older version is rejected with 400.

    criterion_key is only unique within a release_version_id, so the validation
    must be scoped to the open version — not the whole project.
    """
    project = await _make_project_member(db_session, role="member")

    # Create a closed version with a criterion (version_number=1 so it sorts first).
    closed_version = ReleaseVersion(
        project_id=project.id, version_number=1, status="closed"
    )
    db_session.add(closed_version)
    await db_session.commit()
    await db_session.refresh(closed_version)
    closed_criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=closed_version.id,
        text="Old closed criterion",
    )

    # Create the open version (version_number=2, no criteria yet).
    open_version = ReleaseVersion(
        project_id=project.id, version_number=2, status="open"
    )
    db_session.add(open_version)
    await db_session.commit()
    direction = await _make_direction(db_session, project)

    # Supplying the closed-version criterion key must be rejected.
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": [closed_criterion.criterion_key],
        },
    )
    assert resp.status_code == 400, (
        "criterion key from a closed version must be rejected with 400; "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_link_direction_400_rejects_inactive_criterion_key(
    client: AsyncClient, db_session
):
    """A retracted criterion key in the open version is rejected with structured 400.

    Even when the key exists in the open version, an inactive (retracted or
    superseded) criterion must not be linked to a direction — the guard returns
    a structured error with error='criterion_key_invalid' and
    classification='inactive' so callers can distinguish inactive from unknown.
    """
    from app.models import VersionAcceptanceCriterion
    from sqlmodel import select as _select

    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    # Create a criterion and retract it in the open version.
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Criterion to retract",
    )
    row = (
        await db_session.execute(
            _select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.id == criterion.id
            )
        )
    ).scalar_one()
    row.status = "retracted"
    db_session.add(row)
    await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": [criterion.criterion_key],
        },
    )
    assert resp.status_code == 400, (
        "retracted criterion key must be rejected with 400; "
        f"got {resp.status_code}: {resp.text}"
    )
    # FastAPI wraps dict details under {"error": {"code": ..., "message": {...}}}.
    body = resp.json()
    detail = body.get("error", {}).get("message", body)
    assert detail.get("error") == "criterion_key_invalid"
    assert detail.get("classification") == "inactive"
    assert criterion.criterion_key in detail.get("keys", [])


# ---------------------------------------------------------------------------
# Supersession-target validation via the strategic agent op
# ---------------------------------------------------------------------------


async def _make_strategic_task_and_token(session, project):
    """Create a strategic_review Task and return (task, agent_token)."""
    from app.auth import create_agent_token
    from app.models import Task

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id=f"agent-vm-{project.id}",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    token = create_agent_token(
        str(task.id),
        f"agent-vm-{project.id}",
        task_type="strategic_review",
    )
    return task, token


@pytest.mark.asyncio
async def test_supersession_target_unknown_key_rejected(client: AsyncClient, db_session):
    """set_acceptance_criteria returns 400 when superseded_by_key does not exist on the open version."""
    project = await _make_project_member(db_session, role="owner")
    version = await _make_version(db_session, project)
    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Criterion to retract",
    )
    task, token = await _make_strategic_task_and_token(db_session, project)

    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": criterion.criterion_key,
                        "reason": "Replacing with something",
                        "superseded_by_key": "c-does-not-exist",
                    }
                ]
            },
        },
    )
    assert resp.status_code == 400, (
        f"Expected 400 for unknown superseded_by_key, got {resp.status_code}: {resp.text}"
    )
    assert "does not refer to an active criterion" in resp.json()["error"]["message"]

    # Source criterion must remain active — the validation rejected the op before retraction.
    await db_session.refresh(criterion)
    assert criterion.status == "active"


@pytest.mark.asyncio
async def test_supersession_target_valid_active_key_accepted(client: AsyncClient, db_session):
    """set_acceptance_criteria returns 200 when superseded_by_key references a real active criterion."""
    project = await _make_project_member(db_session, role="owner")
    version = await _make_version(db_session, project)
    source = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Old criterion",
    )
    successor = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="New replacement criterion",
    )
    task, token = await _make_strategic_task_and_token(db_session, project)

    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "retract": [
                    {
                        "criterion_key": source.criterion_key,
                        "reason": "Superseded by new criterion",
                        "superseded_by_key": successor.criterion_key,
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, (
        f"Expected 200 for valid active superseded_by_key, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["retracted"][0]["status"] == "superseded"
    assert body["retracted"][0]["found"] is True

    await db_session.refresh(source)
    assert source.status == "superseded"
    assert source.superseded_by_key == successor.criterion_key


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria/{key}/reinstate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinstate_criterion_happy_path(client: AsyncClient, db_session):
    """Reinstating a retracted criterion flips it back to active and clears retraction fields."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Criterion to retract then reinstate",
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion.criterion_key,
        reason="loop retracted this",
        retracted_by="strategist",
    )
    await db_session.refresh(criterion)
    assert criterion.status == "retracted"

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/reinstate"
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"
    assert data["criterion_key"] == criterion.criterion_key

    await db_session.refresh(criterion)
    assert criterion.status == "active"
    assert criterion.retraction_reason is None
    assert criterion.retracted_at is None
    assert criterion.retracted_by is None
    assert criterion.superseded_by_key is None


@pytest.mark.asyncio
async def test_reinstate_already_active_idempotent(client: AsyncClient, db_session):
    """Reinstating an already-active criterion is a no-op returning 200."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Active criterion",
    )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{criterion.criterion_key}/reinstate"
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"

    await db_session.refresh(criterion)
    assert criterion.status == "active"


@pytest.mark.asyncio
async def test_reinstate_not_found_404(client: AsyncClient, db_session):
    """Returns 404 when the criterion key does not exist in the open version."""
    project = await _make_project_member(db_session, role="member")
    await _make_version(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/c-doesnotexist/reinstate"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reinstate_409_no_open_version(client: AsyncClient, db_session):
    """Returns 409 when there is no open release version."""
    project = await _make_project_member(db_session, role="member")

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/c-any/reinstate"
    )
    assert resp.status_code == 409, f"expected 409 (no open version), got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_reinstate_409_cap_exceeded(client: AsyncClient, db_session):
    """Returns 409 when reinstating would exceed the active-criteria cap."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    # Create a criterion, retract it, then fill the cap with other criteria.
    to_reinstate = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Will be retracted",
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=to_reinstate.criterion_key,
        reason="test retraction",
        retracted_by="test",
    )

    # Fill up active slots to the cap.
    for i in range(ac.MAX_CRITERIA_PER_VERSION):
        await ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text=f"Filler {i}",
        )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{to_reinstate.criterion_key}/reinstate"
    )
    assert resp.status_code == 409, f"expected 409 (cap exceeded), got {resp.status_code}: {resp.text}"
    assert "cap" in resp.json().get("error", {}).get("message", resp.text).lower()


@pytest.mark.asyncio
async def test_reinstate_requires_member_role(client: AsyncClient, db_session):
    """Project non-member receives 403/404 on the reinstate endpoint."""
    other_project = Project(name="OtherReinstate", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria/c-any/reinstate"
    )
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria/reconcile-backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_backfill_single_direction(client: AsyncClient, db_session):
    """Single-direction branch: returns ok with per-direction summary (0 rows when no tasks)."""
    project = await _make_project_member(db_session, role="member")
    direction = await _make_direction(db_session, project, key="d-backfill1")

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/reconcile-backfill",
        json={"direction_key": direction.direction_key},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"
    assert isinstance(data["results"], list)
    assert len(data["results"]) == 1
    assert data["results"][0]["direction_key"] == direction.direction_key
    assert data["results"][0]["evidence_recorded"] == 0
    assert data["total_evidence_recorded"] == 0


@pytest.mark.asyncio
async def test_reconcile_backfill_all_directions(client: AsyncClient, db_session):
    """All-directions branch: iterates every Direction row for the project and returns aggregate."""
    project = await _make_project_member(db_session, role="member")
    dir_a = await _make_direction(db_session, project, key="d-all-a")
    dir_b = await _make_direction(db_session, project, key="d-all-b")

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/reconcile-backfill",
        json={},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"
    # Results list omits directions with 0 new rows, but total is the sum.
    assert data["total_evidence_recorded"] == 0
    assert isinstance(data["results"], list)
    # Verify that both directions were scanned (they produced 0 rows, so results list is empty).
    assert data["results"] == []
    _ = dir_a, dir_b  # referenced above; silence linter


@pytest.mark.asyncio
async def test_reconcile_backfill_requires_member_role(client: AsyncClient, db_session):
    """Project non-member receives 403/404 on the reconcile-backfill endpoint."""
    other_project = Project(name="OtherBackfill", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria/reconcile-backfill",
        json={"direction_key": "d-any"},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_reconcile_backfill_single_direction_with_merged_task(
    client: AsyncClient, db_session
):
    """Single-direction backfill records evidence for a pre-existing proven merged task.

    Exercises the non-zero path: a task that merged before the criterion-direction
    mapping was created should have evidence recorded on backfill.  Asserts both
    the per-direction result and that CriterionEvidence rows are actually persisted.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project, status="open")
    direction = await _make_direction(db_session, project, key="d-backfill-merged")

    # Create a criterion and link it to the direction so record_task_merge_evidence
    # can find it via direction.criterion_keys.
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-backfill-test",
        text="Backfill test criterion",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()
    await db_session.refresh(criterion)

    # Link criterion to direction via criterion_keys.
    direction.criterion_keys = ["c-backfill-test"]
    db_session.add(direction)
    await db_session.commit()

    # Create a task that already merged (proven) in the current version window.
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
        context={
            "direction_id": direction.direction_key,
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True},
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/reconcile-backfill",
        json={"direction_key": direction.direction_key},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"
    assert data["results"][0]["direction_key"] == direction.direction_key
    # Two evidence rows per criterion: task_merge + reachability.
    assert data["results"][0]["evidence_recorded"] == 2
    assert data["total_evidence_recorded"] == 2

    # Verify CriterionEvidence rows are actually persisted.
    ev_rows = (
        await db_session.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion.id
            )
        )
    ).scalars().all()
    source_kinds = {e.source_kind for e in ev_rows}
    assert "task_merge" in source_kinds
    assert "reachability" in source_kinds


@pytest.mark.asyncio
async def test_reconcile_backfill_all_directions_with_merged_task(
    client: AsyncClient, db_session
):
    """All-directions backfill aggregates evidence across directions with proven merged tasks."""
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project, status="open")
    dir_a = await _make_direction(db_session, project, key="d-all-proven-a")
    dir_b = await _make_direction(db_session, project, key="d-all-proven-b")

    for direction, ckey in [
        (dir_a, "c-all-proven-a"),
        (dir_b, "c-all-proven-b"),
    ]:
        criterion = VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=version.id,
            criterion_key=ckey,
            text=f"Criterion for {direction.direction_key}",
            status="active",
        )
        db_session.add(criterion)
        await db_session.commit()
        direction.criterion_keys = [ckey]
        db_session.add(direction)
        await db_session.commit()

        task = Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            completed_at=version.opened_at + timedelta(seconds=1),
            context={
                "direction_id": direction.direction_key,
                "pr_merged": True,
                CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True},
            },
        )
        db_session.add(task)
        await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/reconcile-backfill",
        json={},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "ok"
    # 2 evidence rows per direction (task_merge + reachability) x 2 directions = 4
    assert data["total_evidence_recorded"] == 4
    direction_keys_in_results = {r["direction_key"] for r in data["results"]}
    assert dir_a.direction_key in direction_keys_in_results
    assert dir_b.direction_key in direction_keys_in_results
