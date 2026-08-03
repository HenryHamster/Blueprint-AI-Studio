"""Regression matrix for the centralized project-role authority."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.admin_helpers import (
    PROJECT_VIEW_ROLES,
    _get_user_project_with_role,
    _project_role_allows,
)
from app.auth import get_current_user, require_admin
from app.main import app
from app.models import (
    ArtifactRef,
    MessageSurface,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    ProjectTool,
    Task,
    TaskStatus,
)
from app.routers.admin_project_strategic_session import ARTIFACT_BUCKET_NAME
from app.routers.admin_project_tools import _verify_tool_asset_token


ROLE_MATRIX = {
    "viewer": {"viewer": True, "member": False, "owner": False},
    "member": {"viewer": True, "member": True, "owner": False},
    "owner": {"viewer": True, "member": True, "owner": True},
    # Persisted values outside the canonical vocabulary fail closed, including
    # for the viewer floor. This is the regression the old ``dict.get(..., 0)``
    # comparison could not provide.
    "future-role": {"viewer": False, "member": False, "owner": False},
    None: {"viewer": False, "member": False, "owner": False},
}

_CSRF_TOKEN = "project-role-persona-test-token"


@asynccontextmanager
async def _persona_client(*, user_id: str, platform_role: str):
    """Exercise the real auth graph with only identity resolution replaced.

    In particular, do not use the standard ``client`` fixture here: that fixture
    overrides ``require_admin`` and would make an ordinary project member look
    like a platform administrator on operator-only routes.
    """

    async def actor_user():
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "role": platform_role,
        }

    assert require_admin not in app.dependency_overrides
    assert get_current_user not in app.dependency_overrides
    app.dependency_overrides[get_current_user] = actor_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-CSRF-Token": _CSRF_TOKEN},
            cookies={"csrf_token": _CSRF_TOKEN},
        ) as persona_client:
            yield persona_client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("actual_role", ROLE_MATRIX)
@pytest.mark.parametrize("minimum_role", PROJECT_VIEW_ROLES)
def test_project_role_lattice_is_explicit_and_fail_closed(
    actual_role: str | None,
    minimum_role: str,
):
    assert _project_role_allows(actual_role, minimum_role) is ROLE_MATRIX[actual_role][
        minimum_role
    ]


def test_unknown_policy_floor_is_a_programming_error():
    with pytest.raises(ValueError, match="Unknown minimum project role"):
        _project_role_allows("owner", "platform-admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership_role", "minimum_role", "expected_status"),
    [
        ("viewer", "viewer", 200),
        ("viewer", "member", 403),
        ("viewer", "owner", 403),
        ("member", "viewer", 200),
        ("member", "member", 200),
        ("member", "owner", 403),
        ("owner", "viewer", 200),
        ("owner", "member", 200),
        ("owner", "owner", 200),
        (None, "viewer", 404),
        (None, "member", 404),
        (None, "owner", 404),
    ],
)
async def test_project_access_helper_role_matrix(
    db_session,
    membership_role: str | None,
    minimum_role: str,
    expected_status: int,
):
    user_id = "role-matrix-user"
    project = Project(name="Role Matrix", user_id="project-owner")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    if membership_role is not None:
        db_session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role=membership_role,
            )
        )
        await db_session.commit()

    if expected_status == 200:
        resolved = await _get_user_project_with_role(
            project.id, user_id, db_session, minimum_role
        )
        assert resolved.id == project.id
        return

    with pytest.raises(HTTPException) as exc_info:
        await _get_user_project_with_role(
            project.id, user_id, db_session, minimum_role
        )
    assert exc_info.value.status_code == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "actor_name",
        "platform_role",
        "membership_role",
        "read_status",
        "write_status",
    ),
    [
        # Platform-admin authority remains separate from project membership.
        # An admin with no ProjectMember row must not bypass tenant isolation.
        ("platform_admin", "admin", None, 404, 404),
        ("owner", "user", "owner", 200, 200),
        ("member", "user", "member", 200, 200),
        ("viewer", "user", "viewer", 200, 403),
        ("unrelated", "user", None, 404, 404),
    ],
)
async def test_project_endpoint_actor_matrix(
    override_get_session,
    db_session,
    actor_name: str,
    platform_role: str,
    membership_role: str | None,
    read_status: int,
    write_status: int,
):
    user_id = f"role-actor-{actor_name}"
    original_name = f"Actor Matrix {actor_name}"
    project = Project(name=original_name, user_id="project-owner")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    if membership_role is not None:
        db_session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role=membership_role,
            )
        )
        await db_session.commit()

    async with _persona_client(
        user_id=user_id, platform_role=platform_role
    ) as persona_client:
        read_response = await persona_client.get(f"/api/admin/projects/{project.id}")
        assert read_response.status_code == read_status

        updated_name = f"Updated by {actor_name}"
        write_response = await persona_client.patch(
            f"/api/admin/projects/{project.id}",
            json={"name": updated_name},
        )
        assert write_response.status_code == write_status

        # These operator-only routes must be decided by the real require_admin
        # dependency, independently of the actor's project membership.
        expected_admin_status = 200 if platform_role == "admin" else 403
        for route in (
            "/api/admin/settings",
            "/api/admin/max-plan-accounts",
            "/api/admin/digest",
        ):
            response = await persona_client.get(route)
            assert response.status_code == expected_admin_status, route

    await db_session.refresh(project)
    if write_status == 200:
        assert project.name == updated_name
    else:
        # Authorization failures must happen before the mutation path. This is
        # especially important for viewer/unrelated personas and for a platform
        # admin who has no tenant membership.
        assert project.name == original_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor_name", "membership_role", "expected_status"),
    [
        ("owner", "owner", 200),
        ("member", "member", 200),
        ("viewer", "viewer", 200),
        ("unrelated", None, 404),
    ],
)
async def test_viewer_safe_task_and_strategic_get_actor_matrix(
    override_get_session,
    db_session,
    actor_name: str,
    membership_role: str | None,
    expected_status: int,
):
    user_id = f"viewer-read-actor-{actor_name}"
    project = Project(name=f"Viewer read matrix {actor_name}", user_id="project-owner")
    db_session.add(project)
    await db_session.flush()
    if membership_role is not None:
        db_session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role=membership_role,
            )
        )

    task = Task(
        project_id=project.id,
        title=f"Viewer-readable task {actor_name}",
        payloads=[{"type": "create", "prompt": "exercise task detail auth"}],
        status=TaskStatus.QUEUED,
    )
    attachment_id = uuid.uuid4()
    attachment_storage_uri = (
        f"gs://{ARTIFACT_BUCKET_NAME}/message-attachments/"
        f"{project.id}/{attachment_id}/readme.txt"
    )
    message = ProjectMessage(
        project_id=project.id,
        actor_user_id="project-owner",
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Viewer-readable conversation attachment",
        context={
            "attachments": [
                {
                    "attachment_id": str(attachment_id),
                    "filename": "readme.txt",
                    "storage_uri": attachment_storage_uri,
                }
            ]
        },
    )
    db_session.add(task)
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(message)

    routes = (
        f"/api/admin/tasks/{task.id}",
        f"/api/admin/projects/{project.id}/strategic-state",
        f"/api/admin/projects/{project.id}/strategic-liveness",
        f"/api/admin/projects/{project.id}/strategic-session",
        f"/api/admin/projects/{project.id}/strategic-conversation",
        (
            f"/api/admin/projects/{project.id}/strategic-conversation/"
            f"{message.id}/attachments/{attachment_id}/download-url"
        ),
    )
    signed_url = AsyncMock(return_value="https://storage.invalid/signed")
    persist_workbench = AsyncMock()
    sync_session = AsyncMock()
    issue_credentials = MagicMock()
    release_lease = AsyncMock()
    build_attention = AsyncMock(
        return_value={
            "level": "clear",
            "summary": "No operator action is currently flagged",
            "last_seen_at": None,
            "has_more": False,
            "limits": {},
            "counts": {},
            "items": [],
        }
    )
    with (
        patch(
            "app.routers.admin_project_strategic_session.generate_download_signed_url",
            signed_url,
        ),
        patch(
            "app.routers.admin_project_strategic_session.persist_strategic_workbench_update",
            persist_workbench,
        ),
        patch(
            "app.routers.admin_project_strategic_session.sync_resident_session_for_interaction_mode",
            sync_session,
        ),
        patch(
            "app.routers.admin_project_strategic_session.issue_browser_call_credentials",
            issue_credentials,
        ),
        patch(
            "app.routers.admin_project_strategic_session.release_resident_call_lease",
            release_lease,
        ),
        patch(
            "app.routers.admin_project_strategic_session.build_operator_attention",
            build_attention,
        ),
    ):
        async with _persona_client(
            user_id=user_id, platform_role="user"
        ) as persona_client:
            for route in routes:
                response = await persona_client.get(route)
                assert response.status_code == expected_status, (
                    route,
                    response.text,
                )

    if expected_status == 200:
        signed_url.assert_awaited_once_with(
            attachment_storage_uri, expiration_minutes=15
        )
    else:
        signed_url.assert_not_awaited()
    persist_workbench.assert_not_awaited()
    sync_session.assert_not_awaited()
    issue_credentials.assert_not_called()
    release_lease.assert_not_awaited()
    await db_session.refresh(project)
    await db_session.refresh(task)
    await db_session.refresh(message)
    assert project.name == f"Viewer read matrix {actor_name}"
    assert task.status == TaskStatus.QUEUED
    assert message.content == "Viewer-readable conversation attachment"


@pytest.mark.asyncio
async def test_task_detail_cross_project_is_not_disclosed(
    override_get_session,
    db_session,
):
    user_id = "task-cross-project-viewer"
    visible_project = Project(name="Visible project", user_id="visible-owner")
    foreign_project = Project(name="Foreign project", user_id="foreign-owner")
    db_session.add(visible_project)
    db_session.add(foreign_project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=visible_project.id,
            user_id=user_id,
            role="viewer",
        )
    )
    foreign_task = Task(
        project_id=foreign_project.id,
        title="Foreign task",
        payloads=[{"type": "create", "prompt": "must remain private"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(foreign_task)
    await db_session.commit()
    await db_session.refresh(foreign_task)

    async with _persona_client(
        user_id=user_id, platform_role="user"
    ) as persona_client:
        response = await persona_client.get(f"/api/admin/tasks/{foreign_task.id}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_viewer_denied_call_and_mutations_have_no_side_effects(
    override_get_session,
    db_session,
):
    user_id = "viewer-side-effect-guard"
    project = Project(name="Viewer side-effect guard", user_id="project-owner")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id=user_id,
            role="viewer",
        )
    )
    task = Task(
        project_id=project.id,
        title="Viewer may not mutate",
        payloads=[{"type": "create", "prompt": "remain queued"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(task)

    read_call = MagicMock()
    release_lease = AsyncMock()
    issue_credentials = MagicMock()
    persist_workbench = AsyncMock()
    sync_session = AsyncMock()
    with (
        patch(
            "app.routers.admin_project_strategic_session.read_resident_call",
            read_call,
        ),
        patch(
            "app.routers.admin_project_strategic_session.release_resident_call_lease",
            release_lease,
        ),
        patch(
            "app.routers.admin_project_strategic_session."
            "issue_browser_call_credentials",
            issue_credentials,
        ),
        patch(
            "app.routers.admin_project_strategic_session."
            "persist_strategic_workbench_update",
            persist_workbench,
        ),
        patch(
            "app.routers.admin_project_strategic_session."
            "sync_resident_session_for_interaction_mode",
            sync_session,
        ),
    ):
        async with _persona_client(
            user_id=user_id, platform_role="user"
        ) as persona_client:
            call_response = await persona_client.get(
                f"/api/admin/projects/{project.id}/strategic-call"
            )
            task_response = await persona_client.patch(
                f"/api/admin/tasks/{task.id}",
                json={"status": TaskStatus.CANCELLED.value},
            )
            strategic_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/strategic-session",
                json={"workbench": {"active_surface": "viewer-test"}},
            )

    assert call_response.status_code == 403
    assert task_response.status_code == 403
    assert strategic_response.status_code == 403
    read_call.assert_not_called()
    release_lease.assert_not_awaited()
    issue_credentials.assert_not_called()
    persist_workbench.assert_not_awaited()
    sync_session.assert_not_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_viewer_tool_reads_and_asset_token_are_pure_but_writes_stay_denied(
    override_get_session,
    db_session,
):
    user_id = "viewer-tool-read-safety"
    project = Project(name="Viewer tool read safety", user_id="project-owner")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, user_id=user_id, role="viewer"))
    tool = ProjectTool(
        project_id=project.id,
        kind="notes",
        slug="notes",
        title="Read-only notes",
        enabled=True,
        placement="project_section",
        content={"text": "viewer evidence"},
        content_revision=7,
    )
    db_session.add(tool)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(tool)

    manifest = {
        "tools": [
            {
                "slug": "read-tool",
                "title": "Read tool",
                "description": "viewer-safe static tool",
                "placement": "project_section",
                "entry": "index.html",
                "kind": "project",
                "source": "project",
            }
        ],
        "skills": [],
        "error": None,
    }
    discover_manifest = AsyncMock(return_value=manifest)
    with patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        discover_manifest,
    ):
        async with _persona_client(user_id=user_id, platform_role="user") as persona_client:
            list_response = await persona_client.get(
                f"/api/admin/projects/{project.id}/tools"
            )
            content_response = await persona_client.get(
                f"/api/admin/projects/{project.id}/tools/{tool.id}/content"
            )
            manifest_response = await persona_client.get(
                f"/api/admin/projects/{project.id}/tools/manifest"
            )
            token_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/tools/read-tool/asset-token"
            )
            save_response = await persona_client.put(
                f"/api/admin/projects/{project.id}/tools/{tool.id}/content",
                json={"content": {"text": "forbidden"}, "base_revision": 7},
            )
            handoff_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/tools/{tool.id}/artifact-refs",
                json={"expected_revision": 7},
            )
            manage_response = await persona_client.patch(
                f"/api/admin/projects/{project.id}/tools/{tool.id}",
                json={"title": "forbidden rename"},
            )

    assert list_response.status_code == 200
    assert content_response.status_code == 200
    assert content_response.json() == {
        "content": {"text": "viewer evidence"},
        "content_revision": 7,
    }
    assert manifest_response.status_code == 200
    assert token_response.status_code == 200
    token_claims = _verify_tool_asset_token(token_response.json()["token"])
    assert token_claims["scope"] == "tool-asset"
    assert token_claims["project_id"] == str(project.id)
    assert token_claims["tool_slug"] == "read-tool"
    assert save_response.status_code == 403
    assert handoff_response.status_code == 403
    assert manage_response.status_code == 403

    await db_session.refresh(project)
    await db_session.refresh(tool)
    assert project.name == "Viewer tool read safety"
    assert tool.title == "Read-only notes"
    assert tool.content == {"text": "viewer evidence"}
    assert tool.content_revision == 7
    created_refs = (
        await db_session.execute(
            select(ArtifactRef).where(ArtifactRef.project_id == project.id)
        )
    ).scalars().all()
    assert created_refs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor_name", "platform_role", "membership_role", "expected_status"),
    [
        ("owner", "user", "owner", 200),
        ("member", "user", "member", 200),
        ("viewer", "user", "viewer", 200),
        ("unrelated", "user", None, 404),
        ("platform_admin_without_membership", "admin", None, 404),
    ],
)
async def test_immutable_artifact_ref_get_actor_matrix_is_pure(
    override_get_session,
    db_session,
    actor_name: str,
    platform_role: str,
    membership_role: str | None,
    expected_status: int,
):
    user_id = f"artifact-ref-read-{actor_name}"
    project = Project(name=f"Artifact ref read {actor_name}", user_id="project-owner")
    db_session.add(project)
    await db_session.flush()
    if membership_role is not None:
        db_session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role=membership_role,
            )
        )
    artifact_ref = ArtifactRef(
        project_id=project.id,
        tool_id=uuid.uuid4(),
        kind="notes",
        revision=11,
        checksum="b" * 64,
        author_user_id="project-owner",
        content_snapshot={"text": "immutable viewer evidence"},
        retrieval_metadata={"capability": "inline_bounded_text"},
    )
    db_session.add(artifact_ref)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(artifact_ref)

    original_created_at = artifact_ref.created_at
    async with _persona_client(
        user_id=user_id, platform_role=platform_role
    ) as persona_client:
        response = await persona_client.get(
            f"/api/admin/projects/{project.id}/artifact-refs/{artifact_ref.id}"
        )

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        body = response.json()
        assert body["content"] == {"text": "immutable viewer evidence"}
        assert body["retrieval"] == {"capability": "inline_bounded_text"}
        assert body["artifact_ref"]["id"] == str(artifact_ref.id)

    await db_session.refresh(project)
    await db_session.refresh(artifact_ref)
    assert project.name == f"Artifact ref read {actor_name}"
    assert artifact_ref.revision == 11
    assert artifact_ref.checksum == "b" * 64
    assert artifact_ref.content_snapshot == {"text": "immutable viewer evidence"}
    assert artifact_ref.retrieval_metadata == {"capability": "inline_bounded_text"}
    assert artifact_ref.created_at == original_created_at


@pytest.mark.asyncio
async def test_ordinary_member_can_write_task_chat_and_strategic_but_not_owner_or_platform_controls(
    override_get_session,
    db_session,
):
    user_id = "ordinary-member-write-proof"
    project = Project(name="Member write proof", user_id="project-owner")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, user_id=user_id, role="member"))
    task = Task(
        project_id=project.id,
        title="Member-operable task",
        payloads=[{"type": "create", "prompt": "member integration proof"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    build_attention = AsyncMock(
        return_value={
            "level": "clear",
            "summary": "No operator action is currently flagged",
            "last_seen_at": None,
            "has_more": False,
            "limits": {},
            "counts": {},
            "items": [],
        }
    )
    with patch(
        "app.routers.admin_project_strategic_session.build_operator_attention",
        build_attention,
    ):
        async with _persona_client(user_id=user_id, platform_role="user") as persona_client:
            task_response = await persona_client.patch(
                f"/api/admin/tasks/{task.id}",
                json={"status": TaskStatus.CANCELLED.value},
            )
            chat_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/strategic-session/messages",
                json={
                    "content": "Member-authored project message",
                    "message_type": "NOTE",
                    "idempotency_key": "member-chat-write-proof",
                },
            )
            strategic_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/strategic-session",
                json={"workbench": {"active_surface": "member-proof"}},
            )
            owner_response = await persona_client.post(
                f"/api/admin/projects/{project.id}/tools",
                json={"kind": "notes", "title": "Owner-only registration"},
            )
            platform_response = await persona_client.get("/api/admin/settings")

    assert task_response.status_code == 200, task_response.text
    assert chat_response.status_code == 200, chat_response.text
    assert strategic_response.status_code == 200, strategic_response.text
    assert owner_response.status_code == 403
    assert platform_response.status_code == 403
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    written_message = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.actor_user_id == user_id,
                ProjectMessage.content == "Member-authored project message",
            )
        )
    ).scalar_one()
    assert written_message.id is not None
