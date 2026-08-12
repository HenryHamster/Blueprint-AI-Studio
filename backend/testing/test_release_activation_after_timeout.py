"""Regression coverage for re-arming promoted versions after confirmation timeout."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember, ReleaseVersion


async def _make_project(db_session):
    project = Project(name="Activation timeout project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()
    return project


async def _make_version(db_session, project_id, *, status="promoted", serving_status):
    version = ReleaseVersion(
        project_id=project_id,
        version_number=1,
        phase="stabilize",
        status=status,
        promoted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        if status == "promoted"
        else None,
        promoted_sha="a" * 40 if status == "promoted" else None,
        serving_status=serving_status,
        deploy_failure_reason=(
            "no_new_revision"
            if serving_status == "confirmation_timed_out"
            else None
        ),
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


@pytest.mark.asyncio
async def test_approve_activation_allowed_after_confirmation_timed_out(
    client: AsyncClient, db_session
):
    """A promoted timeout is a re-armable report, not a terminal rejection."""
    project = await _make_project(db_session)
    version = await _make_version(
        db_session,
        project.id,
        serving_status="confirmation_timed_out",
    )

    response = await client.post(
        f"/api/admin/projects/{project.id}/approve-activation"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version_number"] == version.version_number
    assert body["serving_status"] == "confirmation_timed_out"
    assert body["activation_requested_at"] is not None
    assert body["activation_requested_by"] == "test@example.com"

    await db_session.refresh(version)
    assert version.activation_requested_at is not None
    assert (
        version.last_qualification["activation_approval"]["approved_sha"]
        == "a" * 40
    )


@pytest.mark.asyncio
async def test_reapproval_after_timeout_gets_a_fresh_attempt_token(
    client: AsyncClient, db_session
):
    """A timed-out approval can be retried and cannot reuse its old marker."""
    project = await _make_project(db_session)
    version = await _make_version(
        db_session,
        project.id,
        serving_status="unconfirmed",
    )

    first = await client.post(
        f"/api/admin/projects/{project.id}/approve-activation"
    )
    assert first.status_code == 200, first.text
    first_at = first.json()["activation_requested_at"]

    version.serving_status = "confirmation_timed_out"
    db_session.add(version)
    await db_session.commit()

    second = await client.post(
        f"/api/admin/projects/{project.id}/approve-activation"
    )
    assert second.status_code == 200, second.text
    assert second.json()["activation_requested_at"] != first_at


@pytest.mark.asyncio
async def test_approve_activation_rejects_never_promoted_version(
    client: AsyncClient, db_session
):
    """A version that never reached promoted remains outside the activation path."""
    project = await _make_project(db_session)
    await _make_version(db_session, project.id, status="open", serving_status=None)

    response = await client.post(
        f"/api/admin/projects/{project.id}/approve-activation"
    )

    assert response.status_code == 404, response.text
    assert "No promoted version" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_approve_activation_rejects_promoted_deploy_failure(
    client: AsyncClient, db_session
):
    """A genuine deploy failure still requires failure recovery first."""
    project = await _make_project(db_session)
    await _make_version(
        db_session,
        project.id,
        serving_status="deploy_failed",
    )

    response = await client.post(
        f"/api/admin/projects/{project.id}/approve-activation"
    )

    assert response.status_code == 409, response.text
    assert "deploy failed" in response.json()["error"]["message"]
