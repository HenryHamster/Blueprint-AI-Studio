"""Owner-gated per-project engine-ownership switch (cutover / rollback).

These two endpoints are the M5 rollout trigger: they make the already-tested
``cutover_to_strategic_resident`` / ``rollback_to_legacy_resident`` transitions
operator-invokable in prod. The switch provisions the Resident on first cutover
(so it works on any project), is idempotent, reversible, and requires ``owner``.

Runs standalone-friendly: a single-threaded cutover/rollback exercises the
transition on the shared SQLite connection (the concurrent-serialization proofs
that need real row locks live in test_strategic_resident_ownership.py).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import Project, ProjectMember, StrategicResident

TEST_USER_ID = "test-user-123"


async def _project_with_role(
    db_session,
    *,
    role: str = "owner",
    user_id: str = TEST_USER_ID,
    owner_user_id: str | None = None,
) -> Project:
    """Create a bare project (no Resident row yet) with one membership grant."""
    project = Project(
        name=f"Engine switch {uuid.uuid4()}", user_id=owner_user_id or user_id
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id=user_id, role=role)
    )
    await db_session.commit()
    return project


async def _resident(db_session, project_id: uuid.UUID) -> StrategicResident:
    return (
        await db_session.execute(
            select(StrategicResident).where(
                StrategicResident.project_id == project_id
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_cutover_endpoint_provisions_and_flips_to_resident(
    client: AsyncClient, db_session
):
    # A project that never had a Resident row — the switch must provision it.
    project = await _project_with_role(db_session)

    res = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/cutover"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["previous_owner"] == "legacy"
    assert body["new_owner"] == "strategic_resident"
    assert body["new_generation"] > body["previous_generation"]
    assert body["already_owned"] is False
    assert body["checkpoint_id"]
    assert body["checkpoint_version"] >= 1

    # DB truth: the Resident row was provisioned and now owns the engine.
    await db_session.rollback()
    resident = await _resident(db_session, project.id)
    assert resident.engine_owner == "strategic_resident"


@pytest.mark.asyncio
async def test_cutover_endpoint_is_idempotent(client: AsyncClient, db_session):
    project = await _project_with_role(db_session)

    first = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/cutover"
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/cutover"
    )
    assert second.status_code == 200, second.text

    assert first.json()["already_owned"] is False
    assert second.json()["already_owned"] is True
    # A repeat flip must NOT advance the generation again.
    assert first.json()["new_generation"] == second.json()["new_generation"]


@pytest.mark.asyncio
async def test_rollback_endpoint_restores_legacy(client: AsyncClient, db_session):
    project = await _project_with_role(db_session)

    await client.post(f"/api/admin/projects/{project.id}/resident-engine/cutover")
    res = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/rollback"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["previous_owner"] == "strategic_resident"
    assert body["new_owner"] == "legacy"

    await db_session.rollback()
    resident = await _resident(db_session, project.id)
    assert resident.engine_owner == "legacy"


@pytest.mark.asyncio
async def test_rollback_without_resident_is_409(client: AsyncClient, db_session):
    # A project that was never cut over has no Resident to roll back → 409, not 500.
    project = await _project_with_role(db_session)
    res = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/rollback"
    )
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_engine_switch_requires_owner_role(client: AsyncClient, db_session):
    # TEST_USER_ID is only a member (not owner) of a project owned by someone else.
    project = await _project_with_role(
        db_session, role="member", owner_user_id="another-owner"
    )
    cutover = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/cutover"
    )
    assert cutover.status_code == 403, cutover.text
    rollback = await client.post(
        f"/api/admin/projects/{project.id}/resident-engine/rollback"
    )
    assert rollback.status_code == 403, rollback.text
    # The gate ran BEFORE any provisioning: no Resident row was created.
    await db_session.rollback()
    assert (
        await db_session.execute(
            select(StrategicResident).where(
                StrategicResident.project_id == project.id
            )
        )
    ).scalar_one_or_none() is None
