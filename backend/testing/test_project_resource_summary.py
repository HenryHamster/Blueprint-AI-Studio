"""Viewer-safe project resource summary contract tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.auth import get_current_user
from app.main import app
from app.models import LocalRunner, Project, RunnerProjectBinding, _utcnow
from testing.factories import make_membership


@asynccontextmanager
async def _acting_as(user_id: str, role: str = "user") -> AsyncIterator[None]:
    original = app.dependency_overrides.get(get_current_user)

    async def _current_user() -> dict[str, str]:
        return {"user_id": user_id, "email": f"{user_id}@example.com", "role": role}

    app.dependency_overrides[get_current_user] = _current_user
    try:
        yield
    finally:
        if original is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = original


async def _project_with_personas(db_session) -> Project:
    project = Project(name="Resource summary", user_id="resource-owner")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add_all(
        [
            make_membership(project.id, user_id="resource-owner", role="owner"),
            make_membership(project.id, user_id="resource-member", role="member"),
            make_membership(project.id, user_id="resource-viewer", role="viewer"),
        ]
    )
    await db_session.commit()
    return project


def _sensitive_account_usage() -> list[dict[str, object]]:
    return [
        {
            "account_id": "acct-secret-id",
            "account_name": "private-owner-account",
            "provider": "secret-provider",
            "enabled": True,
            "project_active_tasks": 2,
            "project_daily_task_count": 7,
            "project_daily_task_cap": 10,
            "project_max_concurrent_tasks": 3,
            "at_project_cap": False,
            "global_active_tasks": 2,
            "global_daily_task_count": 99,
            "global_daily_task_cap": 99,
            "global_max_concurrent_tasks": 5,
        },
        {
            "account_id": "acct-unbounded-id",
            "account_name": "unbounded-private-account",
            "provider": "another-secret-provider",
            "enabled": True,
            "project_active_tasks": 1,
            "project_daily_task_count": 4,
            "project_daily_task_cap": None,
            "project_max_concurrent_tasks": None,
            "at_project_cap": False,
            "global_active_tasks": 0,
            "global_daily_task_count": 0,
            "global_daily_task_cap": None,
            "global_max_concurrent_tasks": None,
        },
        {
            "account_id": "disabled-secret-id",
            "account_name": "disabled-private-account",
            "provider": "disabled-secret-provider",
            "enabled": False,
            "project_active_tasks": 50,
            "project_daily_task_count": 50,
            "project_daily_task_cap": 50,
            "project_max_concurrent_tasks": 50,
            "at_project_cap": True,
            "global_active_tasks": 50,
            "global_daily_task_count": 50,
            "global_daily_task_cap": 50,
            "global_max_concurrent_tasks": 50,
        },
    ]


@pytest.mark.asyncio
async def test_resource_summary_uses_owner_pool_and_redacts_identity(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project_with_personas(db_session)
    enrolled = LocalRunner(
        project_id=project.id,
        token_hash="f" * 64,
        hostname="secret-owner-hostname",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={"private_host_detail": "never-return-this"},
    )
    other_project = Project(name="Other", user_id="other-owner")
    db_session.add_all([enrolled, other_project])
    await db_session.commit()
    await db_session.refresh(other_project)
    bound = LocalRunner(
        project_id=other_project.id,
        token_hash="e" * 64,
        hostname="secret-bound-hostname",
        status="inactive",
    )
    db_session.add(bound)
    await db_session.commit()
    await db_session.refresh(bound)
    db_session.add(RunnerProjectBinding(runner_id=bound.id, project_id=project.id))
    await db_session.commit()

    calls: list[tuple[str, str]] = []

    def _usage(project_id: str, user_id: str) -> list[dict[str, object]]:
        calls.append((project_id, user_id))
        return _sensitive_account_usage()

    monkeypatch.setattr(
        "app.routers.admin_project_config.get_project_account_usage", _usage
    )
    async with _acting_as("resource-viewer"):
        response = await client.get(
            f"/api/admin/projects/{project.id}/resource-summary"
        )

    assert response.status_code == 200, response.text
    assert calls == [(str(project.id), "resource-owner")]
    assert response.json() == {
        "project_id": str(project.id),
        "policy": {
            "execution_resources": "project owner",
            "attribution": "launcher",
            "managed_by": "owner",
        },
        "accounts": {
            "total": 3,
            "enabled": 2,
            "enabled_below_cap": 1,
            "at_cap": 1,
            # Historical/current activity remains visible even if the owner
            # later disables the account carrying it.
            "project_active_tasks": 53,
            "project_daily_task_count": 61,
            "project_daily_task_cap": None,
            "project_max_concurrent_tasks": None,
        },
        "runners": {"total": 2, "online": 1, "offline": 1},
    }
    serialized = response.text.lower()
    for secret in (
        "acct-secret-id",
        "private-owner-account",
        "secret-provider",
        str(enrolled.id),
        str(bound.id),
        "secret-owner-hostname",
        "secret-bound-hostname",
        "never-return-this",
        "token_hash",
        "capabilities",
        "provider",
        "account_id",
        "account_name",
        "hostname",
    ):
        assert secret.lower() not in serialized


@pytest.mark.asyncio
async def test_resource_summary_has_exact_project_persona_access(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project_with_personas(db_session)
    monkeypatch.setattr(
        "app.routers.admin_project_config.get_project_account_usage",
        lambda _project_id, _owner_id: [],
    )

    for user_id in ("resource-owner", "resource-member", "resource-viewer"):
        async with _acting_as(user_id):
            response = await client.get(
                f"/api/admin/projects/{project.id}/resource-summary"
            )
        assert response.status_code == 200, (user_id, response.text)

    async with _acting_as("unrelated-user"):
        unrelated = await client.get(
            f"/api/admin/projects/{project.id}/resource-summary"
        )
    assert unrelated.status_code == 404, unrelated.text

    async with _acting_as("platform-admin-without-membership", role="admin"):
        platform_admin = await client.get(
            f"/api/admin/projects/{project.id}/resource-summary"
        )
    assert platform_admin.status_code == 404, platform_admin.text


@pytest.mark.asyncio
async def test_resource_summary_is_read_only_for_viewer(
    client: AsyncClient, db_session
):
    project = await _project_with_personas(db_session)
    async with _acting_as("resource-viewer"):
        response = await client.patch(
            f"/api/admin/projects/{project.id}/resource-summary",
            json={
                "project_daily_task_cap": 999,
                "account_id": "replace-owner-account",
                "runner_id": "replace-runner-binding",
                "token": "replace-secret",
            },
        )
    assert response.status_code == 405, response.text


@pytest.mark.asyncio
async def test_resource_summary_runner_query_does_not_scale_with_runner_count(
    client: AsyncClient, db_session, monkeypatch
):
    project = await _project_with_personas(db_session)
    now = _utcnow()
    runners = [
        # Canonically online: administratively active with a recent heartbeat.
        LocalRunner(
            project_id=project.id,
            token_hash="1" * 64,
            hostname="recent-active",
            status="active",
            last_seen_at=now,
        ),
        # Every remaining nondeleted state belongs to the complete offline
        # partition, including active rows which have not proved liveness.
        LocalRunner(
            project_id=project.id,
            token_hash="2" * 64,
            hostname="stale-active",
            status="active",
            last_seen_at=now - timedelta(hours=1),
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="3" * 64,
            hostname="never-seen-active",
            status="active",
            last_seen_at=None,
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="4" * 64,
            hostname="inactive",
            status="inactive",
            last_seen_at=now,
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="5" * 64,
            hostname="paused",
            status="paused",
            last_seen_at=now,
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="6" * 64,
            hostname="draining",
            status="draining",
            last_seen_at=now,
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="7" * 64,
            hostname="deleted",
            status="deleted",
            last_seen_at=now,
        ),
    ]
    # Preserve a cardinality-pressure check while covering every status/liveness
    # persona above. These additional recent active rows are all online.
    runners.extend(
        LocalRunner(
            project_id=project.id,
            token_hash=f"{index:064x}",
            hostname=f"private-host-{index}",
            status="active",
            last_seen_at=now,
        )
        for index in range(8, 22)
    )
    db_session.add_all(runners)
    await db_session.commit()
    monkeypatch.setattr(
        "app.routers.admin_project_config.get_project_account_usage",
        lambda _project_id, _owner_id: [],
    )

    statements: list[str] = []

    def _record_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    request_engine = db_session.bind
    event.listen(request_engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        async with _acting_as("resource-viewer"):
            response = await client.get(
                f"/api/admin/projects/{project.id}/resource-summary"
            )
    finally:
        event.remove(
            request_engine.sync_engine, "before_cursor_execute", _record_statement
        )

    assert response.status_code == 200, response.text
    assert response.json()["runners"] == {
        "total": 20,
        "online": 15,
        "offline": 5,
    }
    # Project fetch + membership lookup + one aggregate runner query. The
    # precise runner cardinality cannot add SQL statements.
    assert len(statements) == 3, statements
