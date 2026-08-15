"""Tests for the general service-credential (machine token) seam.

Covers spec section 6 (``service_credential_spec.md``): the admin management
router (mint/list/revoke), the ``authenticate_service_token`` auth branch
wired into ``get_current_user``, and the scope allowlist in
``app.service_credentials``. The credential is a revocable, non-admin, bearer
token that authenticates as a configurable non-human ``principal_user_id``
and is only accepted on a per-scope allowlist of endpoints (Hermes observer
is the first consumer).

Two auth postures are exercised:
  * The ``client`` fixture (admin persona via conftest's blanket override) —
    used for the platform-admin-only management endpoints (mint/list/revoke).
  * ``_real_auth_client()`` — pops the auth overrides so the REAL
    ``get_current_user`` dependency chain runs, including the new service-
    credential branch, exactly as ``testing/test_auth.py`` does for its
    real-token-path tests.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import inspect
from sqlmodel import select
from starlette.requests import Request

from app.auth import get_current_user, get_current_user_optional, require_admin
from app.main import app
from app.models import (
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    ServiceCredential,
    Task,
    TaskStatus,
    _utcnow,
)
from app.service_credentials import (
    TOKEN_PREFIX,
    authenticate_service_token,
    hash_service_token,
    mint_service_token,
    request_allowed,
)
from app.routers.agent_access import (
    _project_agent_message_list,
    _project_agent_task_detail,
    _project_agent_task_list,
)
from testing.factories import make_membership

MGMT_URL = "/api/admin/service-credentials"


@asynccontextmanager
async def _real_auth_client() -> AsyncIterator[AsyncClient]:
    """Run requests through the REAL ``get_current_user`` dependency chain.

    The conftest ``client`` fixture always overrides ``get_current_user``,
    ``get_current_user_optional`` and ``require_admin`` with an admin
    persona (fine for the management-endpoint tests), which means it can
    never exercise the actual service-credential auth branch. This pops all
    three overrides, hands back a fresh raw ``AsyncClient``, and restores
    the overrides afterward so later use of ``client`` in the same test (or
    later tests) is unaffected — mirroring the pop/finally pattern in
    ``testing/test_auth.py``.
    """
    saved_user = app.dependency_overrides.pop(get_current_user, None)
    saved_user_optional = app.dependency_overrides.pop(get_current_user_optional, None)
    saved_admin = app.dependency_overrides.pop(require_admin, None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        if saved_user is not None:
            app.dependency_overrides[get_current_user] = saved_user
        if saved_user_optional is not None:
            app.dependency_overrides[get_current_user_optional] = saved_user_optional
        if saved_admin is not None:
            app.dependency_overrides[require_admin] = saved_admin


async def _make_project(db_session, name: str, owner: str = "svc-cred-owner") -> Project:
    project = Project(name=name, user_id=owner)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _seed_credential(
    db_session,
    *,
    name: str,
    principal_user_id: str = "service:test-observer",
    scopes: list[str] | tuple[str, ...] = ("observer:read",),
    expires_at=None,
    revoked_at=None,
) -> tuple[ServiceCredential, str]:
    """Persist a ``ServiceCredential`` row directly via the ORM, bypassing the
    mint endpoint — for tests exercising the auth path in isolation, per spec
    section 6 ("construct ServiceCredential rows directly via ORM +
    mint_service_token/hash_service_token helpers")."""
    token = mint_service_token()
    cred = ServiceCredential(
        name=name,
        principal_user_id=principal_user_id,
        token_hash=hash_service_token(token),
        token_prefix=token[:12],
        scopes=list(scopes),
        expires_at=expires_at,
        revoked_at=revoked_at,
        created_by="test-harness",
    )
    db_session.add(cred)
    await db_session.commit()
    await db_session.refresh(cred)
    return cred, token


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Mint round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_round_trip_creates_hashed_credential_and_membership(
    client: AsyncClient, db_session
):
    """POST mint returns a plaintext bpsc_ token, stores only its SHA256 hash,
    and idempotently creates the requested project membership."""
    project = await _make_project(db_session, "Mint round-trip project")

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "observer-bot",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token"].startswith(TOKEN_PREFIX)
    assert body.get("token_notice")
    assert body["name"] == "observer-bot"

    row = (
        await db_session.execute(
            select(ServiceCredential).where(ServiceCredential.name == "observer-bot")
        )
    ).scalar_one()
    assert row.token_hash == hash_service_token(body["token"])
    assert row.token_hash != body["token"]

    members = (
        await db_session.execute(
            select(ProjectMember).where(ProjectMember.project_id == project.id)
        )
    ).scalars().all()
    assert len(members) == 1
    assert members[0].user_id == row.principal_user_id
    assert members[0].role == "viewer"


# ---------------------------------------------------------------------------
# 2. Authorized read works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorized_scoped_endpoints_return_200_for_real_token(
    override_get_session, db_session, monkeypatch
):
    """A real service token with membership on the target project succeeds on
    all three observer:read-scoped, membership-checked GET endpoints."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Authorized read project")
    cred, token = await _seed_credential(db_session, name="observer-authorized")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        attention = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
        resource_summary = await rc.get(
            f"/api/admin/projects/{project.id}/resource-summary",
            headers=_auth_header(token),
        )
        diagnostics = await rc.get(
            f"/api/admin/projects/{project.id}/diagnostics",
            headers=_auth_header(token),
        )

    assert attention.status_code == 200, attention.text
    assert resource_summary.status_code == 200, resource_summary.text
    assert diagnostics.status_code == 200, diagnostics.text


@pytest.mark.asyncio
async def test_service_credential_observer_read_authorized_endpoints(
    override_get_session, db_session, monkeypatch
):
    """GET /api/admin/projects — the fourth observer:read allowlisted path — is
    exercised with a real service token. Unlike the three per-project endpoints
    (which 404 via _get_user_project), the list endpoint scopes via a
    ProjectMember join: the credential's own project appears, a foreign project
    it holds no membership on is silently excluded, and a viewer-role
    credential gets build_config env_vars redacted. The trailing-slash form of
    the list path is asserted at the allowlist seam (request_allowed) so the
    slash normalization in request_allowed is exercised here too."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    from sqlalchemy.orm.attributes import flag_modified

    authorized = await _make_project(db_session, "Observer list authorized project")
    authorized.build_config = {
        "env_vars": {"PRIVATE_TOKEN": "s3cr3t"},
        "node_version": "20",
    }
    flag_modified(authorized, "build_config")
    db_session.add(authorized)
    foreign = await _make_project(db_session, "Observer list foreign project")
    cred, token = await _seed_credential(db_session, name="observer-list-read")
    db_session.add(
        make_membership(authorized.id, user_id=cred.principal_user_id, role="viewer")
    )
    await db_session.commit()

    # Slash-insensitive allowlist: the bare and trailing-slash list paths are
    # both authorized once request_allowed strips the single trailing slash.
    assert request_allowed(["observer:read"], "GET", "/api/admin/projects")
    assert request_allowed(["observer:read"], "GET", "/api/admin/projects/")

    async with _real_auth_client() as rc:
        resp = await rc.get("/api/admin/projects", headers=_auth_header(token))

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    item_ids = {item["id"] for item in items}
    assert str(authorized.id) in item_ids
    assert str(foreign.id) not in item_ids

    authorized_item = next(item for item in items if item["id"] == str(authorized.id))
    assert authorized_item["my_role"] == "viewer"
    assert "env_vars" not in authorized_item["build_config"]
    assert authorized_item["build_config"].get("node_version") == "20"


@pytest.mark.asyncio
async def test_coding_agent_read_scope_is_bounded_introspectable_and_non_mutating(
    override_get_session, db_session, monkeypatch
):
    """The coding-agent scope can drill into its own tasks/messages but cannot
    cross the ProjectMember boundary or call a mutating task route."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    authorized = await _make_project(db_session, "Coding agent authorized project")
    authorized_later = await _make_project(db_session, "ZZ " + ("\x01" * 8_000))
    foreign = await _make_project(db_session, "Coding agent foreign project")
    cred, token = await _seed_credential(
        db_session,
        name="coding-agent-read",
        principal_user_id="service:credential:00000000-0000-0000-0000-000000000001",
        scopes=["coding-agent:read"],
    )
    db_session.add(
        make_membership(authorized.id, user_id=cred.principal_user_id, role="viewer")
    )
    db_session.add(
        make_membership(authorized_later.id, user_id=cred.principal_user_id, role="viewer")
    )
    own_task = Task(
        project_id=authorized.id,
        title="Own project failure",
        payloads=[{"type": "research"}],
        status=TaskStatus.FAILED,
    )
    foreign_task = Task(
        project_id=foreign.id,
        title="Foreign project failure",
        payloads=[{"type": "research"}],
        status=TaskStatus.FAILED,
        # Deliberately corrupt cross-project linkage: the external projection
        # must not inherit the admin detail reader's parent-only child query.
        parent_task_id=own_task.id,
    )
    db_session.add_all([own_task, foreign_task])
    await db_session.commit()

    attention_projection = AsyncMock(
        return_value={
            "level": "watch",
            "summary": "One item",
            "last_seen_at": None,
            "counts": {"unread": 1, "blockers": 1},
            "items": [{"id": "message:test", "unread": True}],
        }
    )
    diagnostics_projection = AsyncMock(
        return_value={"queued_count": 0, "in_progress_count": 0, "stuck_tasks": []}
    )
    resource_projection = AsyncMock(
        return_value={"runners": {"total": 1, "online": 1, "offline": 0}}
    )
    message_projection = AsyncMock(
        return_value={"unread": 1, "needs_operator": 1, "blocker": 1}
    )
    monkeypatch.setattr(
        "app.routers.agent_access.build_operator_attention", attention_projection
    )
    monkeypatch.setattr(
        "app.routers.agent_access.get_project_diagnostics", diagnostics_projection
    )
    monkeypatch.setattr(
        "app.routers.agent_access.get_project_resource_summary", resource_projection
    )
    monkeypatch.setattr(
        "app.routers.agent_access.get_message_counts", message_projection
    )

    async with _real_auth_client() as rc:
        profile = await rc.get(
            "/api/agent-access/v1/me?limit=1", headers=_auth_header(token)
        )
        profile_next = await rc.get(
            "/api/agent-access/v1/me?limit=1&offset=1", headers=_auth_header(token)
        )
        snapshot = await rc.get(
            f"/api/agent-access/v1/projects/{authorized.id}/snapshot",
            headers=_auth_header(token),
        )
        task_list = await rc.get(
            f"/api/agent-access/v1/tasks?project_id={authorized.id}&limit=20",
            headers=_auth_header(token),
        )
        raw_task_list = await rc.get(
            f"/api/admin/tasks?project_id={authorized.id}&limit=20",
            headers=_auth_header(token),
        )
        task_detail = await rc.get(
            f"/api/agent-access/v1/tasks/{own_task.id}",
            headers=_auth_header(token),
        )
        raw_task_detail = await rc.get(
            f"/api/admin/tasks/{own_task.id}", headers=_auth_header(token)
        )
        messages = await rc.get(
            f"/api/agent-access/v1/messages?project_id={authorized.id}&status=open",
            headers=_auth_header(token),
        )
        raw_messages = await rc.get(
            f"/api/admin/messages?project_id={authorized.id}&status=open",
            headers=_auth_header(token),
        )
        message_counts = await rc.get(
            f"/api/admin/messages/counts?project_id={authorized.id}",
            headers=_auth_header(token),
        )
        foreign_detail = await rc.get(
            f"/api/agent-access/v1/tasks/{foreign_task.id}",
            headers=_auth_header(token),
        )
        foreign_snapshot = await rc.get(
            f"/api/agent-access/v1/projects/{foreign.id}/snapshot",
            headers=_auth_header(token),
        )
        foreign_task_list = await rc.get(
            f"/api/agent-access/v1/tasks?project_id={foreign.id}",
            headers=_auth_header(token),
        )
        foreign_messages = await rc.get(
            f"/api/agent-access/v1/messages?project_id={foreign.id}",
            headers=_auth_header(token),
        )
        mutation = await rc.patch(
            f"/api/admin/tasks/{own_task.id}",
            headers=_auth_header(token),
            json={"status": "QUEUED"},
        )

    assert profile.status_code == 200, profile.text
    access = profile.json()
    assert access["schema_version"] == "blueprint.agent-access.v1"
    assert access["credential"]["name"] == "coding-agent-read"
    assert access["credential"]["scopes"] == ["coding-agent:read"]
    assert access["total"] == 2
    assert access["has_more"] is True
    assert access["next_offset"] == 1
    assert access["projects"] == [
        {
            "id": str(authorized.id),
            "name": authorized.name,
            "github_owner": authorized.github_owner,
            "github_repo": authorized.github_repo,
            "role": "viewer",
        }
    ]
    assert profile_next.status_code == 200, profile_next.text
    assert profile_next.json()["projects"][0]["id"] == str(authorized_later.id)
    assert profile_next.json()["has_more"] is False
    assert profile_next.json()["complete"] is False
    assert profile_next.json()["truncation"]["item_fields"] is True
    assert "tasks.read" in access["capabilities"]
    assert "messages.list" in access["capabilities"]

    assert snapshot.status_code == 200, snapshot.text
    snapshot_body = snapshot.json()
    assert snapshot_body["schema_version"] == "blueprint.project-snapshot.v1"
    assert snapshot_body["complete"] is True
    assert snapshot_body["project"]["id"] == str(authorized.id)
    assert snapshot_body["attention"]["level"] == "watch"
    assert snapshot_body["resources"]["runners"]["online"] == 1
    assert snapshot_body["message_counts"] == {
        "needs_operator": 1,
        "blocker": 1,
    }
    assert snapshot_body["attention"]["counts"] == {"blockers": 1}
    assert snapshot_body["attention"]["items"] == [{"id": "message:test"}]
    assert "last_seen_at" not in snapshot_body["attention"]
    assert snapshot_body["read_state"]["tracked"] is False
    assert foreign_snapshot.status_code == 404, foreign_snapshot.text

    assert task_list.status_code == 200, task_list.text
    task_list_body = task_list.json()
    assert task_list_body["schema_version"] == "blueprint.agent-tasks.v1"
    assert task_list_body["project"]["id"] == str(authorized.id)
    assert task_list_body["limit"] == 20
    assert task_list_body["offset"] == 0
    task_ids = {item["id"] for item in task_list_body["tasks"]}
    assert str(own_task.id) in task_ids
    assert str(foreign_task.id) not in task_ids
    assert raw_task_list.status_code == 403, raw_task_list.text
    assert foreign_task_list.status_code == 404, foreign_task_list.text
    assert task_detail.status_code == 200, task_detail.text
    assert task_detail.json()["schema_version"] == "blueprint.agent-task.v1"
    assert task_detail.json()["task"]["id"] == str(own_task.id)
    assert str(foreign_task.id) not in task_detail.json()["children"]["ids"]
    assert raw_task_detail.status_code == 403, raw_task_detail.text
    assert messages.status_code == 200, messages.text
    assert messages.json()["schema_version"] == "blueprint.agent-messages.v1"
    assert messages.json()["project"]["id"] == str(authorized.id)
    assert raw_messages.status_code == 403, raw_messages.text
    assert foreign_messages.status_code == 404, foreign_messages.text
    assert message_counts.status_code == 403, message_counts.text
    assert foreign_detail.status_code == 404, foreign_detail.text
    assert mutation.status_code == 403, mutation.text

    attention_projection.assert_awaited_once()
    diagnostics_projection.assert_awaited_once()
    resource_projection.assert_awaited_once()
    message_projection.assert_awaited_once()

    assert not request_allowed(
        ["observer:read"], "GET", f"/api/admin/tasks/{own_task.id}"
    )
    assert request_allowed(
        ["coding-agent:read"],
        "GET",
        f"/api/agent-access/v1/tasks/{own_task.id}",
    )
    assert not request_allowed(
        ["coding-agent:read"], "GET", f"/api/admin/tasks/{own_task.id}"
    )
    assert request_allowed(
        ["coding-agent:read"], "GET", "/api/agent-access/v1/tasks"
    )
    assert request_allowed(
        ["coding-agent:read"], "GET", "/api/agent-access/v1/messages"
    )
    assert not request_allowed(
        ["coding-agent:read"], "GET", "/api/admin/tasks"
    )
    assert not request_allowed(
        ["coding-agent:read"], "GET", "/api/admin/messages"
    )
    assert not request_allowed(
        ["coding-agent:read"], "GET", "/api/admin/messages/counts"
    )
    assert not request_allowed(
        ["coding-agent:read"], "PATCH", f"/api/admin/tasks/{own_task.id}"
    )


def test_coding_agent_task_projection_is_bounded_and_secret_redacted():
    """The external task contract has hard collection/body limits and applies
    defense-in-depth redaction beyond the admin payload's top-level filtering."""
    bearer_secret = "opaque-bearer-value-that-must-not-escape"
    github_secret = "ghp_" + ("a" * 40)
    nested_secret = "do-not-expose-nested-secret"
    registry_secret = "do-not-expose-registry-password"
    raw_detail = {
        "id": "00000000-0000-0000-0000-000000000101",
        "project_id": "00000000-0000-0000-0000-000000000102",
        "title": "Bounded projection",
        "description": "Authorization: Bearer " + bearer_secret,
        "status": "FAILED",
        "task_type": "fix",
        "attempt_count": 3,
        "attempt_number": 8,
        "payloads": [
            {
                "type": "fix",
                "prompt": f"inspect {github_secret}",
                "nested": {"api_key": nested_secret},
                "container_registry_auth": {"password": registry_secret},
            }
            for _ in range(8)
        ],
        "plan_output": {
            "summary": "x" * 100_000,
            "client_secret": nested_secret,
        },
        "latest_status_message": "Authorization: Bearer " + bearer_secret,
        "status_log": [
            {
                "index": index,
                "message": "status " + ("s" * 2_000),
                "password": nested_secret,
            }
            for index in range(90)
        ],
        "history": [
            {
                "timestamp": f"2026-08-10T00:{index:02d}:00Z",
                "action": "STATUS_CHANGE",
                "details": {
                    "message": "history " + ("h" * 2_000),
                    "authorization": bearer_secret,
                },
            }
            for index in range(90)
        ],
        "artifacts": [
            {
                "artifact_type": "ERROR_LOG",
                "pr_url": (
                    f"https://example.test/pull/{index}?token={bearer_secret}"
                ),
                "commit_hash": f"commit-{index}",
                "repo_url": "https://example.test/repo.git",
                "storage_uri": "gs://private-bucket/not-in-projection",
                "created_at": f"2026-08-10T01:{index:02d}:00Z",
            }
            for index in range(80)
        ],
        "child_task_ids": [f"child-{index}" for index in range(150)],
        "child_task_ids_truncated": True,
        "blocker_kind": "code_failure",
        "blocked_reason": "Authorization: Bearer " + bearer_secret,
        "error_logs": ("log\n" * 40_000) + github_secret,
        "evaluation_summary": {
            "reason": "Authorization: Bearer " + bearer_secret,
            "private_key": nested_secret,
        },
    }

    projected = _project_agent_task_detail(raw_detail)
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["schema_version"] == "blueprint.agent-task.v1"
    assert projected["task"]["attempt_count"] == 3
    assert projected["task"]["attempt_number"] == 8
    assert len(serialized.encode("utf-8")) <= projected["limits"]["response_bytes"]
    assert len(projected["payloads"]) <= projected["limits"]["payload_entries"]
    assert len(projected["status"]["log"]) <= projected["limits"][
        "status_log_entries"
    ]
    assert len(projected["history"]) <= projected["limits"]["history_entries"]
    assert len(projected["artifacts"]) <= projected["limits"]["artifact_entries"]
    assert projected["children"]["returned"] <= projected["limits"][
        "child_task_ids"
    ]
    assert projected["status"]["log"][0]["index"] == 40
    assert projected["artifacts"][0]["commit_hash"] == "commit-30"
    assert projected["complete"] is False
    assert all(
        projected["truncation"][key]
        for key in (
            "payloads",
            "plan_output",
            "status_log",
            "history",
            "artifacts",
            "children",
            "failure_evidence",
        )
    )
    assert projected["redaction"]["applied"] is True
    assert projected["redaction"]["fields_or_values"] > 0
    for secret in (bearer_secret, github_secret, nested_secret, registry_secret):
        assert secret not in serialized
    for sensitive_key in (
        "api_key",
        "client_secret",
        "container_registry_auth",
        "password",
        "private_key",
        "storage_uri",
    ):
        assert sensitive_key not in serialized


def test_coding_agent_task_projection_redacts_bearer_and_jwt_key_variants():
    raw_detail = {
        "id": "00000000-0000-0000-0000-000000000103",
        "project_id": "00000000-0000-0000-0000-000000000104",
        "status": "FAILED",
        "task_type": "fix",
        "plan_output": {
            "bearer": "opaque-super-secret",
            "jwt": "eyJopaque",
            "nested_bearer": "nested-bearer-secret",
        },
        "history": [
            {
                "details": {
                    "bearer": "history-secret",
                    "jwt_claims": "claims-secret",
                }
            }
        ],
    }

    projected = _project_agent_task_detail(raw_detail)
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["redaction"]["applied"] is True
    for secret in (
        "opaque-super-secret",
        "eyJopaque",
        "nested-bearer-secret",
        "history-secret",
        "claims-secret",
    ):
        assert secret not in serialized


def test_coding_agent_task_projection_caps_json_escaped_control_text():
    control_text = "\x01" * 8_000
    raw_detail = {
        "id": "00000000-0000-0000-0000-000000000101",
        "project_id": "00000000-0000-0000-0000-000000000102",
        "title": "Escaped control-character budget",
        "description": control_text,
        "status": "FAILED",
        "task_type": "create",
        "pr_url": control_text,
        "commit_hash": control_text,
        "repo_url": control_text,
        "payloads": [],
        "status_log": [],
        "latest_status_message": control_text,
        "history": [],
        "artifacts": [],
        "child_task_ids": [],
        "blocker_kind": control_text,
        "blocked_reason": control_text,
        "park_reason": control_text,
        "movement_explanation": control_text,
        "error_logs": control_text,
        "evaluation_summary": control_text,
    }

    projected = _project_agent_task_detail(raw_detail)
    serialized_bytes = len(
        json.dumps(projected, ensure_ascii=False).encode("utf-8")
    )

    assert serialized_bytes <= projected["limits"]["response_bytes"]
    assert projected["truncation"]["response_bytes"] is True
    assert projected["complete"] is False
    assert projected["task"]["id"] == raw_detail["id"]


def test_coding_agent_task_list_caps_escaped_control_text_and_redacts_secrets():
    control_text = "\x01" * 8_000
    secret = "task-list-secret-must-not-escape"
    project = {
        "id": "00000000-0000-0000-0000-000000000201",
        "name": "Bounded task list",
        "github_owner": "owner",
        "github_repo": "repo",
    }
    raw_response = {
        "tasks": [
            {
                "id": f"00000000-0000-0000-0000-{index:012d}",
                "project_id": project["id"],
                "title": control_text,
                "description": control_text,
                "status": "FAILED",
                "task_type": "fix",
                "attempt_count": 1,
                "attempt_number": index + 4,
                "latest_status_message": control_text,
                "payloads": [{"type": "fix", "api_key": secret}],
            }
            for index in range(20)
        ],
        "total": 20,
        "scoped_active_count": 0,
        "scoped_starting_count": 2,
        "scoped_evaluating_count": 0,
        "scoped_queued_count": 0,
    }

    projected = _project_agent_task_list(
        raw_response,
        project,
        limit=20,
        offset=0,
        statuses=[TaskStatus.FAILED],
        task_type="fix",
        search=None,
        direction_id="direction-test",
        approval_needed=False,
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["schema_version"] == "blueprint.agent-tasks.v1"
    assert len(serialized.encode("utf-8")) <= projected["limits"]["response_bytes"]
    assert projected["truncation"]["response_bytes"] is True
    assert projected["complete"] is False
    assert projected["returned"] < projected["source_returned"]
    assert projected["next_offset"] == projected["returned"]
    assert projected["has_more"] is True
    assert projected["project"]["id"] == project["id"]
    assert projected["limit"] == 20
    assert projected["offset"] == 0
    assert projected["filters"]["status"] == ["FAILED"]
    assert projected["filters"]["direction_id"] == "direction-test"
    assert projected["counts"]["active"] == 0
    assert projected["counts"]["starting"] == 2
    assert all("attempt_number" in item for item in projected["tasks"])
    assert all(
        len(json.dumps(item["title"], ensure_ascii=False).encode("utf-8"))
        <= projected["limits"]["string_serialized_bytes"]
        for item in projected["tasks"]
    )
    assert secret not in serialized
    assert "api_key" not in serialized


def test_coding_agent_message_list_caps_escaped_control_text_and_redacts_secrets():
    control_text = "\x02" * 8_000
    secret = "message-list-secret-must-not-escape"
    project = {
        "id": "00000000-0000-0000-0000-000000000301",
        "name": "Bounded message list",
        "github_owner": "owner",
        "github_repo": "repo",
    }
    raw_response = {
        "messages": [
            {
                "id": f"10000000-0000-0000-0000-{index:012d}",
                "project_id": project["id"],
                "message_type": "BLOCKER",
                "from_type": "system",
                "content": control_text,
                "context": {"note": control_text, "client_secret": secret},
                "status": "open",
                "resolution": control_text,
            }
            for index in range(20)
        ],
        "total": 20,
    }

    projected = _project_agent_message_list(
        raw_response,
        project,
        limit=20,
        offset=0,
        message_status="open",
        query=None,
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["schema_version"] == "blueprint.agent-messages.v1"
    assert len(serialized.encode("utf-8")) <= projected["limits"]["response_bytes"]
    assert projected["truncation"]["response_bytes"] is True
    assert projected["complete"] is False
    assert projected["returned"] < projected["source_returned"]
    assert projected["project"]["id"] == project["id"]
    assert projected["limit"] == 20
    assert projected["offset"] == 0
    assert projected["filters"] == {"status": "open", "q": None}
    assert all(
        len(json.dumps(item["content"], ensure_ascii=False).encode("utf-8"))
        <= projected["limits"]["string_serialized_bytes"]
        for item in projected["messages"]
    )
    assert secret not in serialized
    assert "client_secret" not in serialized


@pytest.mark.asyncio
async def test_coding_agent_messages_pages_without_guaranteed_message_reinjection(
    override_get_session, db_session, monkeypatch
):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Coding agent message pagination")
    cred, token = await _seed_credential(
        db_session,
        name="coding-agent-message-pages",
        principal_user_id="service:credential:00000000-0000-0000-0000-000000000402",
        scopes=["coding-agent:read"],
    )
    db_session.add(
        make_membership(project.id, user_id=cred.principal_user_id, role="viewer")
    )
    now = _utcnow()
    for index in range(5):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                from_type="user",
                content=f"paged-message-{index}",
                status="open",
                created_at=now - timedelta(seconds=index),
            )
        )
    await db_session.commit()

    async with _real_auth_client() as rc:
        pages = [
            await rc.get(
                f"/api/agent-access/v1/messages?project_id={project.id}"
                f"&status=open&limit=2&offset={offset}",
                headers=_auth_header(token),
            )
            for offset in (0, 2, 4)
        ]
        excessive_offset = await rc.get(
            f"/api/agent-access/v1/messages?project_id={project.id}"
            "&status=open&limit=2&offset=1000001",
            headers=_auth_header(token),
        )

    assert all(page.status_code == 200 for page in pages), [
        page.text for page in pages
    ]
    bodies = [page.json() for page in pages]
    assert [body["offset"] for body in bodies] == [0, 2, 4]
    assert [body["returned"] for body in bodies] == [2, 2, 1]
    assert [body["next_offset"] for body in bodies] == [2, 4, None]
    assert [body["has_more"] for body in bodies] == [True, True, False]
    assert [body["total"] for body in bodies] == [5, 5, 5]
    assert excessive_offset.status_code == 422, excessive_offset.text
    assert [
        message["content"]
        for body in bodies
        for message in body["messages"]
    ] == [f"paged-message-{index}" for index in range(5)]


@pytest.mark.asyncio
async def test_coding_agent_mint_owns_a_fresh_principal_on_every_grant(
    client: AsyncClient, db_session
):
    """A coding-agent token cannot borrow a human identity or retain the
    memberships of a revoked same-name credential when it is re-minted."""
    first_project = await _make_project(db_session, "First coding grant")
    second_project = await _make_project(db_session, "Second coding grant")

    borrowed = await client.post(
        MGMT_URL,
        json={
            "name": "borrowed-coding-agent",
            "scopes": ["coding-agent:read"],
            "principal_user_id": "existing-human-member",
            "project_memberships": [
                {"project_id": str(first_project.id), "role": "viewer"}
            ],
        },
    )
    assert borrowed.status_code == 422, borrowed.text

    first = await client.post(
        MGMT_URL,
        json={
            "name": "rotating-coding-agent",
            "scopes": ["coding-agent:read"],
            "project_memberships": [
                {"project_id": str(first_project.id), "role": "viewer"}
            ],
        },
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["principal_user_id"].startswith("service:credential:")

    revoked = await client.post(f"{MGMT_URL}/{first_body['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    second = await client.post(
        MGMT_URL,
        json={
            "name": "rotating-coding-agent",
            "scopes": ["coding-agent:read"],
            "project_memberships": [
                {"project_id": str(second_project.id), "role": "viewer"}
            ],
        },
    )
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["principal_user_id"].startswith("service:credential:")
    assert second_body["principal_user_id"] != first_body["principal_user_id"]

    memberships = (
        await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.user_id == second_body["principal_user_id"]
            )
        )
    ).scalars().all()
    assert [(str(row.project_id), row.role) for row in memberships] == [
        (str(second_project.id), "viewer")
    ]


# ---------------------------------------------------------------------------
# 3. Project isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_isolation_denies_unauthorized_project(
    override_get_session, db_session, monkeypatch
):
    """The same credential, valid on its own project, is denied (404 — tenant
    boundary) on a second project it holds no membership on."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    authorized_project = await _make_project(db_session, "Isolation authorized project")
    unauthorized_project = await _make_project(db_session, "Isolation foreign project")
    cred, token = await _seed_credential(db_session, name="observer-isolated")
    db_session.add(
        make_membership(authorized_project.id, user_id=cred.principal_user_id, role="viewer")
    )
    await db_session.commit()

    async with _real_auth_client() as rc:
        attention = await rc.get(
            f"/api/admin/operator-attention?project_id={unauthorized_project.id}",
            headers=_auth_header(token),
        )
        resource_summary = await rc.get(
            f"/api/admin/projects/{unauthorized_project.id}/resource-summary",
            headers=_auth_header(token),
        )
        diagnostics = await rc.get(
            f"/api/admin/projects/{unauthorized_project.id}/diagnostics",
            headers=_auth_header(token),
        )

    assert attention.status_code == 404, attention.text
    assert resource_summary.status_code == 404, resource_summary.text
    assert diagnostics.status_code == 404, diagnostics.text


# ---------------------------------------------------------------------------
# 4. Endpoint allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_allowlist_rejects_out_of_scope_get_and_mutating_post(
    override_get_session, db_session, monkeypatch
):
    """The allowlist rejects BEFORE any handler runs: a membership-scoped GET
    outside observer:read's paths, and a mutating POST, are both 403 even
    though the credential genuinely holds membership on the target project."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Allowlist project")
    cred, token = await _seed_credential(db_session, name="observer-allowlist")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="member"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        # GET but not one of the four allowlisted paths.
        out_of_scope_get = await rc.get(
            f"/api/admin/projects/{project.id}/auto-remediation",
            headers=_auth_header(token),
        )
        # A mutating admin endpoint — method alone excludes it (allowlist is
        # GET-only for observer:read).
        mutating_post = await rc.post(
            f"/api/admin/projects/{project.id}/circuit-breaker/reset",
            headers=_auth_header(token),
        )

    assert out_of_scope_get.status_code == 403, out_of_scope_get.text
    assert mutating_post.status_code == 403, mutating_post.text


# ---------------------------------------------------------------------------
# 5. Never admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_credential_never_satisfies_require_admin(
    override_get_session, db_session, monkeypatch
):
    """A real service-credential request to a require_admin, platform-global
    endpoint (GET /api/admin/diagnostics) is rejected 403 — the credential's
    role is always "user", and the endpoint is outside observer:read's
    per-project allowlist regardless."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    _cred, token = await _seed_credential(db_session, name="observer-never-admin")

    async with _real_auth_client() as rc:
        resp = await rc.get("/api/admin/diagnostics", headers=_auth_header(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 6. Revocation immediate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_immediately_blocks_real_token(
    client: AsyncClient, db_session, monkeypatch
):
    """A token that authenticates successfully stops working the instant its
    credential is revoked via the management endpoint — no caching/TTL."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Revocation project")

    mint_resp = await client.post(
        MGMT_URL,
        json={
            "name": "observer-revocable",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )
    assert mint_resp.status_code == 201, mint_resp.text
    body = mint_resp.json()
    token = body["token"]
    cred_id = body["id"]

    async with _real_auth_client() as rc:
        before = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert before.status_code == 200, before.text

    revoke_resp = await client.post(f"{MGMT_URL}/{cred_id}/revoke")
    assert revoke_resp.status_code == 200, revoke_resp.text

    async with _real_auth_client() as rc:
        after = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert after.status_code == 401, after.text


# ---------------------------------------------------------------------------
# 7. Expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_credential_returns_401(
    override_get_session, db_session, monkeypatch
):
    """A credential whose expires_at is already in the past is rejected 401,
    even though it is not explicitly revoked."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Expiry project")
    cred, token = await _seed_credential(
        db_session,
        name="observer-expired",
        expires_at=_utcnow() - timedelta(days=1),
    )
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        resp = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )

    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 8. Unknown bpsc_ token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_bpsc_token_returns_401_without_reaching_firebase(
    override_get_session, db_session, monkeypatch
):
    """A syntactically valid bpsc_-prefixed token with no matching DB row is
    rejected 401 with the service-credential-specific detail message — proof
    it never fell through to Firebase verification (which would instead
    produce "Invalid authentication credentials")."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    unknown_token = mint_service_token()  # never persisted anywhere

    async with _real_auth_client() as rc:
        resp = await rc.get(
            "/api/admin/operator-attention?project_id=00000000-0000-0000-0000-000000000000",
            headers=_auth_header(unknown_token),
        )

    assert resp.status_code == 401, resp.text
    # All HTTPExceptions are wrapped by main.py's global handler into
    # {"error": {"code": ..., "message": ...}} — never bare {"detail": ...}
    # (see app/main.py http_exception_handler). The spec pins the literal
    # detail text "Invalid service credential"; if it reached Firebase
    # verification instead, the message would be "Invalid authentication
    # credentials" (app/auth.py's existing Firebase-branch failure text).
    detail = resp.json()["error"]["message"]
    assert "invalid" in detail.lower()
    assert "service credential" in detail.lower()
    assert "authentication credentials" not in detail.lower()


# ---------------------------------------------------------------------------
# 9. last_used tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_used_tracking_populated_after_successful_call(
    override_get_session, db_session, monkeypatch
):
    """A successful authenticated call stamps last_used_at/last_used_path on
    the credential row."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Last-used project")
    cred, token = await _seed_credential(db_session, name="observer-last-used")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()
    assert cred.last_used_at is None
    assert cred.last_used_path is None

    async with _real_auth_client() as rc:
        resp = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert resp.status_code == 200, resp.text

    db_session.expunge_all()
    refreshed = (
        await db_session.execute(
            select(ServiceCredential).where(ServiceCredential.id == cred.id)
        )
    ).scalar_one()
    assert refreshed.last_used_at is not None
    assert refreshed.last_used_path is not None
    assert "operator-attention" in refreshed.last_used_path


@pytest.mark.asyncio
async def test_last_used_telemetry_issues_short_lock_timeout_literal_on_postgres(
    db_session,
):
    """Regression (2026-06-04 incident class): the last_used telemetry commit on
    the service-credential auth hot path must run behind a short per-statement
    lock_timeout — exactly like the admin-login metadata write in app.auth — so
    a contended row cannot block every machine-token request up to the 30s
    default. The timeout must be inlined as a literal (asyncpg cannot bind a
    parameter into SET LOCAL) and issued only on Postgres."""
    cred, token = await _seed_credential(db_session, name="observer-lock-timeout")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/projects/00000000-0000-0000-0000-000000000000/diagnostics",
            "query_string": b"",
            "headers": [],
        }
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = cred
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with patch("app.service_credentials.resolve_dialect_name", return_value="postgresql"):
        principal = await authenticate_service_token(token, session, request)

    set_local_calls = [
        call.args[0]
        for call in session.execute.call_args_list
        if "SET LOCAL" in str(call.args[0])
    ]
    assert set_local_calls, "expected a SET LOCAL lock_timeout on Postgres"
    stmt = set_local_calls[0]
    assert "'1s'" in str(stmt.text)
    assert ":t" not in str(stmt.text) and "$1" not in str(stmt.text)
    for call in session.execute.call_args_list:
        assert len(call.args) == 1, "SET LOCAL must carry no bind params"
    session.commit.assert_awaited_once()
    assert principal["user_id"] == cred.principal_user_id
    assert principal["service_credential_id"] == str(cred.id)


@pytest.mark.asyncio
async def test_last_used_telemetry_skips_lock_timeout_on_non_postgres(db_session):
    """On SQLite (and other test dialects) there is no lock_timeout GUC, so the
    SET LOCAL is skipped entirely — the telemetry write still commits and auth
    still resolves the principal."""
    cred, token = await _seed_credential(db_session, name="observer-lock-sqlite")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/projects/00000000-0000-0000-0000-000000000000/diagnostics",
            "query_string": b"",
            "headers": [],
        }
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = cred
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with patch("app.service_credentials.resolve_dialect_name", return_value="sqlite"):
        principal = await authenticate_service_token(token, session, request)

    assert not any(
        "SET LOCAL" in str(call.args[0]) for call in session.execute.call_args_list
    ), "SET LOCAL must not be issued on non-Postgres dialects"
    session.commit.assert_awaited_once()
    assert principal["user_id"] == cred.principal_user_id
    assert principal["service_credential_id"] == str(cred.id)


@pytest.mark.asyncio
async def test_authentication_survives_failed_last_used_telemetry_commit(
    db_session, monkeypatch, real_orm_expiry
):
    """A telemetry rollback must not invalidate an otherwise valid principal."""
    cred, token = await _seed_credential(db_session, name="observer-telemetry-failure")
    credential_id = str(cred.id)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/projects/00000000-0000-0000-0000-000000000000/diagnostics",
            "query_string": b"",
            "headers": [],
        }
    )

    async def commit_then_fail():
        await db_session.flush()
        raise RuntimeError("telemetry commit failure")

    monkeypatch.setattr(db_session, "commit", commit_then_fail)

    principal = await authenticate_service_token(token, db_session, request)

    assert inspect(cred).expired, "rollback must exercise real ORM expiry semantics"
    assert principal == {
        "user_id": "service:test-observer",
        "email": "observer-telemetry-failure@service.blueprint.local",
        "role": "user",
        "service_credential": "observer-telemetry-failure",
        "service_credential_id": credential_id,
    }


# ---------------------------------------------------------------------------
# 10. Management auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_management_mint_requires_authentication(unauthenticated_client: AsyncClient):
    """The mint endpoint is platform-admin-only; an unauthenticated caller
    (no Firebase credentials, no admin override) is rejected."""
    resp = await unauthenticated_client.post(
        MGMT_URL,
        json={"name": "unauthorized-mint", "scopes": ["observer:read"]},
    )
    assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# 11. Mint validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_rejects_unknown_scope(client: AsyncClient, db_session):
    resp = await client.post(
        MGMT_URL,
        json={"name": "bad-scope-bot", "scopes": ["not-a-real-scope"]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_rejects_owner_role_in_project_memberships(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, "Owner-rejected project")
    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-role-bot",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "owner"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_rejects_principal_user_id_matching_project_owner(
    client: AsyncClient, db_session
):
    """A caller-supplied principal_user_id that IS an existing project owner
    (Project.user_id) is rejected: the credential would authenticate as that
    owner and inherit owner-level membership, bypassing the no-owner rule."""
    owner_uid = "firebase-uid-project-owner"
    await _make_project(db_session, "Owner-principal project", owner=owner_uid)

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-principal-bot",
            "scopes": ["observer:read"],
            "principal_user_id": owner_uid,
        },
    )

    assert resp.status_code == 422, resp.text
    remaining = (
        await db_session.execute(
            select(ServiceCredential).where(
                ServiceCredential.name == "owner-principal-bot"
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_mint_rejects_principal_user_id_with_owner_membership(
    client: AsyncClient, db_session
):
    """A caller-supplied principal_user_id already holding an owner-role
    ProjectMember row is rejected for the same impersonation reason."""
    owner_uid = "firebase-uid-owner-member"
    project = await _make_project(db_session, "Owner-member project")
    db_session.add(make_membership(project.id, user_id=owner_uid, role="owner"))
    await db_session.commit()

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-member-bot",
            "scopes": ["observer:read"],
            "principal_user_id": owner_uid,
        },
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_accepts_custom_non_owner_principal_user_id(
    client: AsyncClient, db_session
):
    """A custom non-owner principal_user_id is honored and minted normally."""
    project = await _make_project(db_session, "Custom-principal project")
    resp = await client.post(
        MGMT_URL,
        json={
            "name": "custom-principal-bot",
            "scopes": ["observer:read"],
            "principal_user_id": "custom-machine-principal-1",
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["principal_user_id"] == "custom-machine-principal-1"


@pytest.mark.asyncio
async def test_mint_rejects_duplicate_unrevoked_name(client: AsyncClient, db_session):
    first = await client.post(
        MGMT_URL, json={"name": "dupe-name-bot", "scopes": ["observer:read"]}
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        MGMT_URL, json={"name": "dupe-name-bot", "scopes": ["observer:read"]}
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_mint_allows_name_reuse_after_revocation(
    client: AsyncClient, db_session
):
    """A revoked name may be re-minted: the uniqueness rule only guards ACTIVE
    (non-revoked) credentials, so reusing a revoked name must succeed instead
    of tripping the DB-level unique constraint (regression for migration 0723's
    full uq_servicecredential_name, which turned the intended reuse into a
    409 IntegrityError)."""
    first = await client.post(
        MGMT_URL, json={"name": "reusable-bot", "scopes": ["observer:read"]}
    )
    assert first.status_code == 201, first.text
    first_token = first.json()["token"]

    revoke = await client.post(f"{MGMT_URL}/{first.json()['id']}/revoke")
    assert revoke.status_code == 200, revoke.text

    second = await client.post(
        MGMT_URL, json={"name": "reusable-bot", "scopes": ["observer:read"]}
    )
    assert second.status_code == 201, second.text
    assert second.json()["token"] != first_token


# ---------------------------------------------------------------------------
# 12. List endpoint never returns token/hash fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_endpoint_never_returns_token_or_hash_fields(
    client: AsyncClient, db_session
):
    mint_resp = await client.post(
        MGMT_URL,
        json={"name": "list-safety-bot", "scopes": ["observer:read"]},
    )
    assert mint_resp.status_code == 201, mint_resp.text
    plaintext_token = mint_resp.json()["token"]
    token_hash = hash_service_token(plaintext_token)

    list_resp = await client.get(MGMT_URL)
    assert list_resp.status_code == 200, list_resp.text
    entries = list_resp.json()["service_credentials"]
    assert isinstance(entries, list)
    entry = next(e for e in entries if e["name"] == "list-safety-bot")
    assert "token" not in entry
    assert "token_hash" not in entry

    serialized = list_resp.text
    assert plaintext_token not in serialized
    assert token_hash not in serialized
