"""Security and tenant-boundary tests for project-operator credentials.

These tests exercise the real ServiceCredential authentication branch wherever
route-level behavior matters.  They intentionally treat the scoped operator API
as a capability boundary: a project-operator token may use the narrow versioned
contracts, but it may never fall through to generic admin mutation surfaces.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import func, select

from app.auth import get_current_user, get_current_user_optional, require_admin
from app.main import app
from app.models import (
    Direction,
    Project,
    ProjectMember,
    ProjectMessage,
    ReleaseVersion,
    ServiceCredential,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)
from app.service_credentials import (
    hash_service_token,
    mint_service_token,
    request_allowed,
)
from testing.factories import make_membership, make_project, make_task


OPERATOR_SCOPE = "project-operator"
MGMT_URL = "/api/admin/service-credentials"
OPERATOR_URL = "/api/operator-access/v1"
AGENT_URL = "/api/agent-access/v1"


@asynccontextmanager
async def _real_auth_client() -> AsyncIterator[AsyncClient]:
    """Temporarily remove blanket test auth overrides and run real bearer auth."""

    saved_user = app.dependency_overrides.pop(get_current_user, None)
    saved_optional = app.dependency_overrides.pop(get_current_user_optional, None)
    saved_admin = app.dependency_overrides.pop(require_admin, None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        if saved_user is not None:
            app.dependency_overrides[get_current_user] = saved_user
        if saved_optional is not None:
            app.dependency_overrides[get_current_user_optional] = saved_optional
        if saved_admin is not None:
            app.dependency_overrides[require_admin] = saved_admin


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _persist_project(
    db_session,
    *,
    name: str,
    owner: str = "project-owner",
    archived: bool = False,
    directions: list[dict] | None = None,
) -> Project:
    project = make_project(
        name=name,
        user_id=owner,
        github_owner="example",
        github_repo=name.lower().replace(" ", "-"),
        archived=archived,
        directions=directions or [],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _seed_operator(
    db_session,
    project: Project,
    *,
    role: str = "member",
    name: str | None = None,
) -> tuple[ServiceCredential, str]:
    token = mint_service_token()
    credential_id = uuid.uuid4()
    credential = ServiceCredential(
        id=credential_id,
        name=name or f"operator-{credential_id.hex[:10]}",
        principal_user_id=f"service:credential:{credential_id}",
        token_hash=hash_service_token(token),
        token_prefix=token[:12],
        scopes=[OPERATOR_SCOPE],
        created_by="test-harness",
    )
    db_session.add(credential)
    db_session.add(
        make_membership(
            project.id,
            user_id=credential.principal_user_id,
            role=role,
        )
    )
    await db_session.commit()
    await db_session.refresh(credential)
    return credential, token


def _operator_task_body(**overrides) -> dict:
    body = {
        "title": "Scoped operator task",
        "prompt": "Inspect the selected project and report the result.",
        "task_type": "plan",
        "idempotency_key": f"operator-test-{uuid.uuid4().hex}",
    }
    body.update(overrides)
    return body


def test_project_operator_scope_has_exact_positive_and_negative_matrix():
    project_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    direction_id = "d-operator_1"

    allowed = {
        ("GET", f"{AGENT_URL}/me"),
        ("GET", f"{AGENT_URL}/projects/{project_id}/snapshot"),
        ("GET", f"{AGENT_URL}/tasks"),
        ("GET", f"{AGENT_URL}/tasks/{task_id}"),
        ("GET", f"{AGENT_URL}/messages"),
        ("GET", f"{AGENT_URL}/directions"),
        ("GET", f"{OPERATOR_URL}/task-capabilities"),
        ("GET", f"{OPERATOR_URL}/tasks/{task_id}/cancel-impact"),
        (
            "GET",
            f"{OPERATOR_URL}/projects/{project_id}/strategic-conversation",
        ),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/tasks"),
        ("POST", f"{OPERATOR_URL}/tasks/{task_id}/requeue"),
        ("POST", f"{OPERATOR_URL}/tasks/{task_id}/cancel"),
        ("POST", f"{OPERATOR_URL}/tasks/{task_id}/request-changes"),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/directions"),
        (
            "PATCH",
            f"{OPERATOR_URL}/projects/{project_id}/directions/{direction_id}",
        ),
        (
            "POST",
            f"{OPERATOR_URL}/projects/{project_id}/directions/{direction_id}/activate",
        ),
        (
            "POST",
            f"{OPERATOR_URL}/projects/{project_id}/directions/{direction_id}/retarget",
        ),
        (
            "POST",
            f"{OPERATOR_URL}/projects/{project_id}/directions/{direction_id}/cancel",
        ),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/messages"),
        (
            "POST",
            f"{OPERATOR_URL}/projects/{project_id}/messages/{message_id}/answer",
        ),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/strategic-cycle"),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/strategic/start"),
        ("POST", f"{OPERATOR_URL}/projects/{project_id}/strategic/messages"),
    }
    for method, path in allowed:
        assert request_allowed([OPERATOR_SCOPE], method, path), (method, path)
        assert request_allowed([OPERATOR_SCOPE], method, path + "/"), (method, path)

    denied = {
        ("POST", f"{AGENT_URL}/tasks"),
        ("POST", "/api/admin/tasks"),
        ("PATCH", f"/api/admin/tasks/{task_id}"),
        ("POST", "/api/admin/tasks/bulk-action"),
        ("POST", "/api/admin/tasks/bulk-archive"),
        ("POST", f"/api/admin/projects/{project_id}/promote/force"),
        ("POST", "/api/deploy"),
        ("POST", "/api/admin/service-credentials"),
        ("GET", "/api/admin/settings"),
        ("POST", "/api/admin/runners"),
        ("PUT", f"/api/admin/projects/{project_id}/directions"),
        ("PATCH", f"/api/admin/projects/{project_id}/directions/{direction_id}"),
        ("POST", f"/api/admin/projects/{project_id}/strategic-session/start"),
        ("POST", f"{OPERATOR_URL}/tasks/{task_id}/complete"),
        ("PATCH", f"{OPERATOR_URL}/tasks/{task_id}/requeue"),
        ("DELETE", f"{OPERATOR_URL}/projects/{project_id}/directions/{direction_id}"),
        ("POST", f"{OPERATOR_URL}/projects/not-a-uuid/tasks"),
    }
    for method, path in denied:
        assert not request_allowed([OPERATOR_SCOPE], method, path), (method, path)


@pytest.mark.asyncio
async def test_project_operator_mint_owns_fresh_non_owner_principal(
    client: AsyncClient, db_session
):
    project = await _persist_project(db_session, name="Operator mint")
    response = await client.post(
        MGMT_URL,
        json={
            "name": "hermes-project-operator",
            "scopes": [OPERATOR_SCOPE],
            "project_memberships": [
                {"project_id": str(project.id), "role": "member"}
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["principal_user_id"] == f"service:credential:{body['id']}"
    assert body["principal_user_id"] != project.user_id

    membership = (
        await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == body["principal_user_id"],
            )
        )
    ).scalar_one()
    assert membership.role == "member"

    borrowed = await client.post(
        MGMT_URL,
        json={
            "name": "borrowed-project-operator",
            "scopes": [OPERATOR_SCOPE],
            "principal_user_id": "existing-human",
        },
    )
    assert borrowed.status_code == 422, borrowed.text

    owner_grant = await client.post(
        MGMT_URL,
        json={
            "name": "owner-project-operator",
            "scopes": [OPERATOR_SCOPE],
            "project_memberships": [
                {"project_id": str(project.id), "role": "owner"}
            ],
        },
    )
    assert owner_grant.status_code == 422, owner_grant.text


@pytest.mark.asyncio
async def test_task_create_is_explicit_member_scoped_strict_and_never_creates_shadow_project(
    override_get_session, db_session
):
    authorized = await _persist_project(db_session, name="Authorized create")
    foreign = await _persist_project(db_session, name="Foreign create", owner="other-owner")
    credential, token = await _seed_operator(db_session, authorized)
    headers = _auth(token)
    initial_project_count = int(
        (await db_session.execute(select(func.count(Project.id)))).scalar() or 0
    )

    async with _real_auth_client() as client:
        created = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(),
            headers=headers,
        )
        invalid_low_priority = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(priority=-1),
            headers=headers,
        )
        invalid_high_priority = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(priority=101),
            headers=headers,
        )
        forbidden_body = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(
                github_repo="attacker/shadow",
                project_id=str(foreign.id),
                hotfix=True,
                base_branch="main",
                model="provider-controlled",
            ),
            headers=headers,
        )
        research_fields_on_plan = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(
                task_type="plan",
                work_class="exploratory",
                stop_or_pivot_condition="Stop if the premise is disproved.",
            ),
            headers=headers,
        )
        incomplete_review = await client.post(
            f"{OPERATOR_URL}/projects/{authorized.id}/tasks",
            json=_operator_task_body(task_type="review"),
            headers=headers,
        )
        foreign_project = await client.post(
            f"{OPERATOR_URL}/projects/{foreign.id}/tasks",
            json=_operator_task_body(),
            headers=headers,
        )
        missing_project = await client.post(
            f"{OPERATOR_URL}/projects/{uuid.uuid4()}/tasks",
            json=_operator_task_body(),
            headers=headers,
        )

    assert created.status_code == 201, created.text
    created_task = await db_session.get(Task, uuid.UUID(created.json()["id"]))
    assert created_task is not None
    assert created_task.project_id == authorized.id
    assert created_task.created_by == "USER"
    assert created_task.priority == 50
    assert created_task.launched_by_user_id == credential.principal_user_id
    assert invalid_low_priority.status_code == 422, invalid_low_priority.text
    assert invalid_high_priority.status_code == 422, invalid_high_priority.text
    assert forbidden_body.status_code == 422, forbidden_body.text
    assert research_fields_on_plan.status_code == 422, research_fields_on_plan.text
    assert incomplete_review.status_code == 422, incomplete_review.text
    assert foreign_project.status_code == 404, foreign_project.text
    assert missing_project.status_code == 404, missing_project.text
    final_project_count = int(
        (await db_session.execute(select(func.count(Project.id)))).scalar() or 0
    )
    assert final_project_count == initial_project_count


@pytest.mark.asyncio
async def test_task_create_can_atomically_stage_exact_authority_while_paused(
    override_get_session, db_session
):
    project = await _persist_project(
        db_session,
        name="Paused exact admission",
        directions=[
            {"id": "d-exact", "text": "Exact task scope", "status": "active"}
        ],
    )
    project.paused = True
    project.integration_branch = "automain"
    project.promotion_target = "main"
    version = ReleaseVersion(
        project_id=project.id, version_number=54, status="open", phase="build"
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)
    criterion_ids = ["c-duration", "c-retention"]
    db_session.add_all(
        [
            Direction(
                project_id=project.id,
                direction_key="d-exact",
                status="active",
                data={
                    "id": "d-exact",
                    "status": "active",
                    "criterion_release_version_id": str(version.id),
                },
                criterion_keys=[*criterion_ids, "c-direction-only"],
            ),
            *[
                VersionAcceptanceCriterion(
                    project_id=project.id,
                    release_version_id=version.id,
                    criterion_key=key,
                    status="active",
                    text=key,
                )
                for key in [*criterion_ids, "c-direction-only"]
            ],
        ]
    )
    predecessor = make_task(
        project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "first", "base_branch": "automain"}],
    )
    db_session.add(predecessor)
    await db_session.commit()
    await db_session.refresh(predecessor)
    _credential, token = await _seed_operator(db_session, project)

    async with _real_auth_client() as client:
        response = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json=_operator_task_body(
                task_type="create",
                priority=100,
                prompt="Implement only the bounded budget consumers.",
                base_branch="automain",
                direction_id="d-exact",
                criterion_ids=criterion_ids,
                depends_on=[str(predecessor.id)],
                require_deps_landed_on="automain",
                release_base_branch="automain",
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_exact_admission.py::test_contract",
                },
            ),
            headers=_auth(token),
        )
        server_defaulted = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json=_operator_task_body(
                title="Server-selected integration base",
                prompt="Let the server select the current integration branch.",
            ),
            headers=_auth(token),
        )
        wrong_base = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json=_operator_task_body(base_branch="main"),
            headers=_auth(token),
        )

    assert response.status_code == 201, response.text
    assert server_defaulted.status_code == 201, server_defaulted.text
    assert server_defaulted.json()["base_branch"] == "automain"
    assert wrong_base.status_code == 400, wrong_base.text
    assert "project integration branch 'automain'" in wrong_base.text
    assert response.json()["direction_id"] == "d-exact"
    assert response.json()["criterion_ids"] == criterion_ids
    assert response.json()["criterion_ids_explicit"] is True
    assert response.json()["criterion_release_version_id"] == str(version.id)
    assert response.json()["priority"] == 100
    assert response.json()["base_branch"] == "automain"
    assert response.json()["depends_on"] == [str(predecessor.id)]
    assert response.json()["require_deps_landed_on"] == "automain"
    assert response.json()["release_base_branch"] == "automain"
    created = await db_session.get(Task, uuid.UUID(response.json()["id"]))
    assert created is not None
    assert created.status == TaskStatus.QUEUED
    assert created.priority == 100
    assert created.criterion_ids == criterion_ids
    assert created.depends_on == [str(predecessor.id)]
    assert created.context["direction_id"] == "d-exact"
    assert created.context["criterion_ids_explicit"] is True
    assert created.context["_operator_task_creation_authority"]["priority"] == 100
    assert created.context["_operator_task_creation_authority"]["base_branch"] == (
        "automain"
    )
    assert created.first_payload["base_branch"] == "automain"
    assert created.first_payload["require_deps_landed_on"] == "automain"
    await db_session.refresh(project)
    assert project.paused is True


@pytest.mark.asyncio
async def test_task_create_priority_is_exact_idempotency_authority(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Priority replay")
    _credential, token = await _seed_operator(db_session, project)
    body = _operator_task_body(
        priority=100,
        idempotency_key="operator-priority-replay",
    )

    async with _real_auth_client() as client:
        created = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json=body,
            headers=_auth(token),
        )
        duplicate = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json=body,
            headers=_auth(token),
        )
        mismatch = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/tasks",
            json={**body, "priority": 50},
            headers=_auth(token),
        )

    assert created.status_code == 201, created.text
    assert created.json()["priority"] == 100
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["is_duplicate"] is True
    assert duplicate.json()["priority"] == 100
    assert mismatch.status_code == 409, mismatch.text
    assert "priority" in mismatch.text
    assert (
        await db_session.execute(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_viewer_can_read_but_cannot_mutate_and_nonmembers_are_hidden(
    override_get_session, db_session
):
    visible = await _persist_project(
        db_session,
        name="Viewer visible",
        directions=[{"id": "d-visible", "text": "Read me", "status": "active"}],
    )
    foreign = await _persist_project(
        db_session,
        name="Viewer foreign",
        owner="foreign-owner",
        directions=[{"id": "d-foreign", "text": "Hidden", "status": "active"}],
    )
    foreign_task = make_task(
        foreign.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "foreign"}],
    )
    db_session.add(foreign_task)
    await db_session.commit()
    _credential, token = await _seed_operator(db_session, visible, role="viewer")
    headers = _auth(token)

    async with _real_auth_client() as client:
        own_read = await client.get(
            f"{AGENT_URL}/directions?project_id={visible.id}", headers=headers
        )
        foreign_read = await client.get(
            f"{AGENT_URL}/directions?project_id={foreign.id}", headers=headers
        )
        create_denied = await client.post(
            f"{OPERATOR_URL}/projects/{visible.id}/tasks",
            json=_operator_task_body(),
            headers=headers,
        )
        direction_denied = await client.post(
            f"{OPERATOR_URL}/projects/{visible.id}/directions",
            json={"text": "Viewer write"},
            headers=headers,
        )
        message_denied = await client.post(
            f"{OPERATOR_URL}/projects/{visible.id}/messages",
            json={"content": "Viewer write"},
            headers=headers,
        )
        foreign_task_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{foreign_task.id}/requeue",
            json={"guidance": "Retry foreign work"},
            headers=headers,
        )

    assert own_read.status_code == 200, own_read.text
    assert foreign_read.status_code == 404, foreign_read.text
    assert create_denied.status_code == 403, create_denied.text
    assert direction_denied.status_code == 403, direction_denied.text
    assert message_denied.status_code == 403, message_denied.text
    assert foreign_task_denied.status_code == 404, foreign_task_denied.text


@pytest.mark.asyncio
async def test_task_requeue_has_strict_body_status_and_archive_fences(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Requeue boundaries")
    archived = await _persist_project(
        db_session, name="Archived requeue", archived=True
    )
    credential, token = await _seed_operator(db_session, project)
    db_session.add(
        make_membership(
            archived.id, user_id=credential.principal_user_id, role="member"
        )
    )
    retryable = make_task(
        project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "original prompt"}],
    )
    completed = make_task(
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "completed"}],
    )
    active = make_task(
        project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "prompt": "active"}],
    )
    archived_failed = make_task(
        archived.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "archived"}],
    )
    db_session.add_all([retryable, completed, active, archived_failed])
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        requeued = await client.post(
            f"{OPERATOR_URL}/tasks/{retryable.id}/requeue",
            json={"guidance": "Use the concrete failure evidence."},
            headers=headers,
        )
        completed_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{completed.id}/requeue",
            json={"guidance": "Do not revive completed work."},
            headers=headers,
        )
        active_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{active.id}/requeue",
            json={"guidance": "Do not replace an active attempt."},
            headers=headers,
        )
        archived_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{archived_failed.id}/requeue",
            json={"guidance": "Archived projects stay inert."},
            headers=headers,
        )
        smuggled = await client.post(
            f"{OPERATOR_URL}/tasks/{completed.id}/requeue",
            json={
                "guidance": "No arbitrary status.",
                "status": "QUEUED",
                "context": {"review_loop": {"approved": True}},
            },
            headers=headers,
        )

    assert requeued.status_code == 200, requeued.text
    await db_session.refresh(retryable)
    assert retryable.status == TaskStatus.QUEUED
    assert (
        retryable.first_payload["operator_retry_guidance"]
        == "Use the concrete failure evidence."
    )
    assert "Use the concrete failure evidence." in retryable.first_payload[
        "prompt_suffix"
    ]
    assert completed_denied.status_code == 409, completed_denied.text
    assert active_denied.status_code == 409, active_denied.text
    assert archived_denied.status_code == 409, archived_denied.text
    assert smuggled.status_code == 422, smuggled.text


@pytest.mark.asyncio
async def test_task_cancel_requires_visible_cascade_confirmation_and_stays_in_project(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Cancel boundary")
    _credential, token = await _seed_operator(db_session, project)
    parent = make_task(project.id, status=TaskStatus.QUEUED)
    child = make_task(
        project.id, status=TaskStatus.QUEUED, parent_task_id=parent.id
    )
    dependent = make_task(
        project.id, status=TaskStatus.QUEUED, depends_on=[str(parent.id)]
    )
    unrelated = make_task(project.id, status=TaskStatus.QUEUED)
    db_session.add_all([parent, child, dependent, unrelated])
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        hidden_cascade_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{parent.id}/cancel",
            json={"reason": "Cancel this task only."},
            headers=headers,
        )
        smuggled = await client.post(
            f"{OPERATOR_URL}/tasks/{parent.id}/cancel",
            json={"confirm_cascade": True, "force_kill": True},
            headers=headers,
        )
        confirmed = await client.post(
            f"{OPERATOR_URL}/tasks/{parent.id}/cancel",
            json={"confirm_cascade": True, "reason": "Impact reviewed."},
            headers=headers,
        )

    assert hidden_cascade_denied.status_code == 409, hidden_cascade_denied.text
    detail = hidden_cascade_denied.json()["error"]["message"]
    assert detail["error"] == "cancel_cascade_confirmation_required"
    assert detail["cascade_count"] == 2
    assert smuggled.status_code == 422, smuggled.text
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["cancel_impact"]["active_child_cascade_count"] == 1
    assert confirmed.json()["cancel_impact"]["waiting_dependent_count"] == 1

    for task in (parent, child, dependent, unrelated):
        await db_session.refresh(task)
    assert parent.status == TaskStatus.CANCELLED
    assert child.status == TaskStatus.CANCELLED
    assert dependent.status == TaskStatus.CANCELLED
    assert unrelated.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_task_cancel_refuses_cross_project_child_without_mutation(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Cancel isolation")
    foreign = await _persist_project(
        db_session, name="Foreign cancel isolation", owner="foreign-owner"
    )
    _credential, token = await _seed_operator(db_session, project)
    parent = make_task(project.id, status=TaskStatus.QUEUED)
    foreign_child = make_task(
        foreign.id,
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
    )
    db_session.add_all([parent, foreign_child])
    await db_session.commit()

    async with _real_auth_client() as client:
        preview = await client.get(
            f"{OPERATOR_URL}/tasks/{parent.id}/cancel-impact",
            headers=_auth(token),
        )
        response = await client.post(
            f"{OPERATOR_URL}/tasks/{parent.id}/cancel",
            json={"confirm_cascade": True},
            headers=_auth(token),
        )

    assert preview.status_code == 409, preview.text
    assert "cross-project child" in preview.json()["error"]["message"]
    assert str(foreign_child.id) not in preview.text
    assert "active_child_cascade_count" not in preview.text
    assert response.status_code == 409, response.text
    assert "cross-project child" in response.json()["error"]["message"]
    await db_session.refresh(parent)
    await db_session.refresh(foreign_child)
    assert parent.status == TaskStatus.QUEUED
    assert foreign_child.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_task_requeue_refuses_cross_project_child_without_mutation(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Requeue isolation")
    foreign = await _persist_project(
        db_session, name="Foreign requeue isolation", owner="foreign-owner"
    )
    _credential, token = await _seed_operator(db_session, project)
    parent = make_task(
        project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "plan", "prompt": "retry the plan"}],
    )
    foreign_child = make_task(
        foreign.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "foreign child"}],
    )
    db_session.add_all([parent, foreign_child])
    await db_session.commit()
    before_count = (
        await db_session.execute(select(func.count()).select_from(Task))
    ).scalar_one()

    async with _real_auth_client() as client:
        response = await client.post(
            f"{OPERATOR_URL}/tasks/{parent.id}/requeue",
            json={"guidance": "Retry without crossing the project boundary."},
            headers=_auth(token),
        )

    assert response.status_code == 409, response.text
    assert "cross-project child" in response.json()["error"]["message"]
    after_count = (
        await db_session.execute(select(func.count()).select_from(Task))
    ).scalar_one()
    assert after_count == before_count
    await db_session.refresh(parent)
    await db_session.refresh(foreign_child)
    assert parent.status == TaskStatus.FAILED
    assert foreign_child.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_request_changes_is_strict_member_scoped_and_requires_approval(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Request changes")
    foreign = await _persist_project(db_session, name="Foreign request changes")
    _credential, token = await _seed_operator(db_session, project)
    ordinary = make_task(project.id, requires_approval=False)
    foreign_task = make_task(foreign.id, requires_approval=True)
    db_session.add_all([ordinary, foreign_task])
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        not_awaiting = await client.post(
            f"{OPERATOR_URL}/tasks/{ordinary.id}/request-changes",
            json={"feedback": "Please fix the concrete defect.", "action": "fix"},
            headers=headers,
        )
        invalid_action = await client.post(
            f"{OPERATOR_URL}/tasks/{ordinary.id}/request-changes",
            json={"feedback": "Bypass", "action": "complete"},
            headers=headers,
        )
        smuggled = await client.post(
            f"{OPERATOR_URL}/tasks/{ordinary.id}/request-changes",
            json={
                "feedback": "No context authority.",
                "action": "fix",
                "requires_approval": False,
            },
            headers=headers,
        )
        foreign_denied = await client.post(
            f"{OPERATOR_URL}/tasks/{foreign_task.id}/request-changes",
            json={"feedback": "Cross-project", "action": "review"},
            headers=headers,
        )

    assert not_awaiting.status_code == 400, not_awaiting.text
    assert invalid_action.status_code == 422, invalid_action.text
    assert smuggled.status_code == 422, smuggled.text
    assert foreign_denied.status_code == 404, foreign_denied.text


@pytest.mark.asyncio
async def test_direction_status_fences_and_staged_cancel_do_not_cancel_tasks(
    override_get_session, db_session
):
    active_id = "d-active"
    staged_id = "d-staged"
    project = await _persist_project(
        db_session,
        name="Direction fences",
        directions=[
            {"id": active_id, "text": "Active", "priority": "high", "status": "active"},
            {
                "id": staged_id,
                "text": "Staged",
                "priority": "medium",
                "status": "staged",
                "staged_for_version": 2,
            },
        ],
    )
    _credential, token = await _seed_operator(db_session, project)
    db_session.add(
        ReleaseVersion(project_id=project.id, version_number=2, status="open")
    )
    staged_task = make_task(
        project.id,
        status=TaskStatus.QUEUED,
        context={"direction_id": staged_id},
    )
    db_session.add(staged_task)
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        active_activate = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{active_id}/activate",
            json={},
            headers=headers,
        )
        active_retarget = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{active_id}/retarget",
            json={"version": 3},
            headers=headers,
        )
        active_cancel = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{active_id}/cancel",
            json={},
            headers=headers,
        )
        active_update = await client.patch(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{active_id}",
            json={"text": "Do not rewrite an active direction."},
            headers=headers,
        )
        hidden_update = await client.patch(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{active_id}",
            json={"research_context": "erase history", "status": "completed"},
            headers=headers,
        )
        staged_cancel = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/directions/{staged_id}/cancel",
            json={},
            headers=headers,
        )

    assert active_activate.status_code == 409, active_activate.text
    assert active_retarget.status_code == 409, active_retarget.text
    assert active_cancel.status_code == 409, active_cancel.text
    assert active_update.status_code == 409, active_update.text
    assert hidden_update.status_code == 422, hidden_update.text
    assert staged_cancel.status_code == 200, staged_cancel.text
    assert staged_cancel.json()["cancelled_tasks"] == 0
    await db_session.refresh(staged_task)
    assert staged_task.status == TaskStatus.QUEUED
    await db_session.refresh(project)
    by_id = {direction["id"]: direction for direction in project.directions}
    assert by_id[active_id]["status"] == "active"
    assert by_id[staged_id]["status"] == "rejected"


@pytest.mark.asyncio
async def test_nonempty_bodies_are_rejected_on_empty_body_operator_actions(
    override_get_session, db_session
):
    project = await _persist_project(
        db_session,
        name="Empty body fences",
        directions=[
            {"id": "d-activate", "text": "A", "status": "staged"},
            {"id": "d-cancel", "text": "B", "status": "staged"},
        ],
    )
    _credential, token = await _seed_operator(db_session, project)
    headers = _auth(token)

    start_mock = AsyncMock(return_value={"project_id": str(project.id), "session": {}})
    with patch(
        "app.routers.operator_access.start_project_strategic_resident_session",
        new=start_mock,
    ):
        async with _real_auth_client() as client:
            activate = await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/directions/d-activate/activate",
                json={"status": "completed"},
                headers=headers,
            )
            cancel = await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/directions/d-cancel/cancel",
                json={"cancel_queued_tasks": True},
                headers=headers,
            )
            strategic_start = await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/strategic/start",
                json={"runner_id": "forbidden-runner", "allow_no_runner": True},
                headers=headers,
            )

    assert activate.status_code == 422, activate.text
    assert cancel.status_code == 422, cancel.text
    assert strategic_start.status_code == 422, strategic_start.text
    start_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_messages_reject_context_and_cross_project_answers(
    override_get_session, db_session
):
    project = await _persist_project(db_session, name="Message boundary")
    foreign = await _persist_project(db_session, name="Foreign message boundary")
    _credential, token = await _seed_operator(db_session, project)
    own_message = ProjectMessage(
        project_id=project.id,
        actor_user_id="questioner",
        from_type="system",
        content="Own project note",
        status="open",
    )
    foreign_message = ProjectMessage(
        project_id=foreign.id,
        actor_user_id="questioner",
        from_type="system",
        content="Foreign project note",
        status="open",
    )
    db_session.add_all([own_message, foreign_message])
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        smuggled_send = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/messages",
            json={
                "content": "No hidden context",
                "context": {"secrets": "do-not-store"},
                "workbench_context": {"interaction_mode": "continuous"},
            },
            headers=headers,
        )
        foreign_answer = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/messages/{foreign_message.id}/answer",
            json={"resolution": "Cross-project answer"},
            headers=headers,
        )
        own_answer = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/messages/{own_message.id}/answer",
            json={"resolution": "Handled in the authorized project."},
            headers=headers,
        )
        strategic_smuggle = await client.post(
            f"{OPERATOR_URL}/projects/{project.id}/strategic/messages",
            json={
                "content": "No attachments",
                "message_type": "NOTE",
                "idempotency_key": f"strict-{uuid.uuid4()}",
                "attachments": [{"storage_uri": "gs://secret/object"}],
                "operator_intent": {"command": "restart_runner"},
            },
            headers=headers,
        )

    assert smuggled_send.status_code == 422, smuggled_send.text
    assert foreign_answer.status_code == 404, foreign_answer.text
    assert own_answer.status_code == 200, own_answer.text
    assert strategic_smuggle.status_code == 422, strategic_smuggle.text
    await db_session.refresh(own_message)
    assert own_message.status == "resolved"
    assert own_message.resolution == "Handled in the authorized project."


def test_operator_message_receipt_carries_the_arming_verdict():
    """The scoped operator receipt must not drop WHY a message will (not) be read.

    ``arming`` is the server's serviceability verdict
    (``strategic_trigger.maybe_arm_operator_refresh``): ``not_armed`` means the
    message was durably stored but no strategic cycle will consume it and no
    resident is listening. Filtering it out of this receipt left the operator CLI
    structurally unable to tell a live steer from a dead one, so a send that goes
    nowhere looked exactly like one that landed.
    """
    from app.routers.operator_access import _operator_message_receipt

    project_id = uuid.uuid4()
    receipt = _operator_message_receipt(
        {
            "id": "msg-1",
            "status": "created",
            "arming": "not_armed",
            "wakeup_state": None,
            # Not part of the operator contract; must still be filtered out.
            "message": {"content": "unbounded projection"},
        },
        project_id=project_id,
    )

    assert receipt["arming"] == "not_armed"
    assert receipt["id"] == "msg-1"
    assert receipt["status"] == "created"
    assert receipt["project_id"] == str(project_id)
    # The allowlist still fences everything it fenced before.
    assert "message" not in receipt


def test_operator_message_receipt_omits_arming_when_the_source_has_none():
    """Absent stays absent -- the filter must not invent a verdict."""
    from app.routers.operator_access import _operator_message_receipt

    receipt = _operator_message_receipt(
        {"id": "msg-2", "status": "duplicate"}, project_id=uuid.uuid4()
    )

    assert "arming" not in receipt


@pytest.mark.asyncio
async def test_archived_project_rejects_every_work_starting_operator_action(
    override_get_session, db_session
):
    project = await _persist_project(
        db_session,
        name="Archived operator",
        archived=True,
        directions=[{"id": "d-archived", "text": "Dormant", "status": "active"}],
    )
    _credential, token = await _seed_operator(db_session, project)
    failed = make_task(
        project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "archived"}],
    )
    awaiting = make_task(project.id, requires_approval=True)
    db_session.add_all([failed, awaiting])
    await db_session.commit()
    headers = _auth(token)

    async with _real_auth_client() as client:
        responses = [
            await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/tasks",
                json=_operator_task_body(),
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/tasks/{failed.id}/requeue",
                json={"guidance": "Archived"},
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/tasks/{awaiting.id}/request-changes",
                json={"feedback": "Archived", "action": "fix"},
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/directions",
                json={"text": "Archived"},
                headers=headers,
            ),
            await client.patch(
                f"{OPERATOR_URL}/projects/{project.id}/directions/d-archived",
                json={"text": "Archived edit"},
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/messages",
                json={"content": "Archived"},
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/strategic-cycle",
                json={"phase": "design_doc"},
                headers=headers,
            ),
            await client.post(
                f"{OPERATOR_URL}/projects/{project.id}/strategic/start",
                json={},
                headers=headers,
            ),
        ]

    for response in responses:
        assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_direction_reads_are_paginated_bounded_and_secret_redacted(
    override_get_session, db_session
):
    secret = "direction-client-secret-must-not-escape"
    directions = [
        {
            "id": f"d-bounded-{index}",
            "text": f"Direction {index}",
            "priority": "medium",
            "status": "active",
            "context": {"clientSecret": secret, "tokenCount": index},
            "research_context": "x" * 30_000,
            "research_history": [
                {"cycle": item, "authorizationHeader": secret}
                for item in range(80)
            ],
        }
        for index in range(3)
    ]
    project = await _persist_project(
        db_session, name="Bounded directions", directions=directions
    )
    _credential, token = await _seed_operator(db_session, project, role="viewer")

    async with _real_auth_client() as client:
        response = await client.get(
            f"{AGENT_URL}/directions?project_id={project.id}&limit=2&offset=0",
            headers=_auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    serialized = json.dumps(body, ensure_ascii=False)
    assert body["schema_version"] == "blueprint.agent-directions.v1"
    assert body["total"] == 3
    assert body["returned"] <= 2
    assert body["has_more"] is True
    assert body["next_offset"] == body["returned"]
    assert len(serialized.encode("utf-8")) <= body["limits"]["response_bytes"]
    assert body["redaction"]["applied"] is True
    assert secret not in serialized
    assert body["directions"][0]["context"]["tokenCount"] == 0
