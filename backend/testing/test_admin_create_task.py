"""Tests for the hotfix guard in admin.create_task_manually.

POST /api/admin/tasks accepts a ``hotfix`` payload flag (Phase 5 / automain-LTS).
A hotfix is only meaningful for a ``plan`` task: the back-merge trigger in
``task_lifecycle.py`` (``is_rollup_merge and base_branch == "main" and hotfix``)
only fires for a hotfix ROLLUP merge of a ``plan`` task → main. A ``create`` or
``fix`` hotfix has no plan and therefore no rollup, so its PR would merge
straight to main with NO main → automain back-merge — silently diverging the
two branches and breaking the next promotion.

``create_task_manually`` must reject ``hotfix=true`` for any non-plan task_type
with an HTTP 400.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.models import (
    Direction,
    Project,
    ProjectMember,
    ProjectStatus,
    ReleaseVersion,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)

# Code-producing tasks now require an acceptance_evidence declaration at
# validation (validate_task_payload). These tests exercise the operator
# create-task surface, so every create/fix body that must reach a 200 carries a
# minimal valid observable_behavior declaration.
_CODE_EVIDENCE = {
    "kind": "observable_behavior",
    "description": "operator-spawned task ships a real, testable change",
}


async def _make_project_with_member(db_session, repo="acme/widget", role="owner"):
    """Project for the given owner/repo with test-user-123 as a member.

    ``role`` defaults to ``owner``; pass ``viewer``/``member`` to exercise the
    role floor on the existing-project creation path.
    """
    owner, name = repo.split("/", 1)
    project = Project(
        name=repo,
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role=role)
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_hotfix_create_task_rejected_with_400(client: AsyncClient, db_session):
    """hotfix=true on a create task → 400 (no plan → no rollup → no back-merge)."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Hotfix via create",
            "prompt": "patch the bug",
            "base_branch": "main",
            "task_type": "create",
            "hotfix": True,
        },
    )

    assert resp.status_code == 400, resp.text
    # The app wraps HTTPException as {"error": {"code", "message"}} (main.py
    # http_exception_handler) — there is no top-level "detail" key.
    detail = resp.json()["error"]["message"].lower()
    assert "hotfix" in detail
    assert "plan" in detail


@pytest.mark.asyncio
async def test_hotfix_fix_task_rejected_with_400(client: AsyncClient, db_session):
    """hotfix=true on a fix task → 400. pr_number/branch are supplied so the
    request clears Pydantic validation and actually reaches the hotfix guard."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Hotfix via fix",
            "prompt": "patch the bug",
            "base_branch": "main",
            "task_type": "fix",
            "pr_number": 123,
            "branch": "blueprint/task-abc",
            "hotfix": True,
        },
    )

    assert resp.status_code == 400, resp.text
    # See note above: HTTPException detail is at error.message, not "detail".
    assert "hotfix" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_hotfix_plan_task_accepted(client: AsyncClient, db_session):
    """hotfix=true on a plan task is allowed — the documented hotfix path —
    and the hotfix flag is persisted in the stored task payload so the agent
    handler and the Phase 5 back-merge trigger can see it."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Real hotfix",
            "prompt": "expedited fix off main",
            "base_branch": "main",
            "task_type": "plan",
            "hotfix": True,
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    # The hotfix flag must round-trip into the stored payload.
    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    payloads = get_resp.json()["payloads"]
    assert payloads[0]["type"] == "plan"
    assert payloads[0]["hotfix"] is True


@pytest.mark.asyncio
async def test_omitted_base_branch_uses_project_promotion_target(
    client: AsyncClient, db_session
):
    """When base_branch is omitted, the resolved payload uses the project's promotion_target."""
    owner, name = "acme", "trunk-project"
    project = Project(
        name=f"{owner}/{name}",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": f"{owner}/{name}",
            "title": "Task without base_branch",
            "prompt": "do something useful",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            # base_branch intentionally omitted
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    stored_base_branch = get_resp.json()["payloads"][0]["base_branch"]
    assert stored_base_branch == "trunk", (
        f"Expected 'trunk' (project promotion_target) but got {stored_base_branch!r}"
    )


@pytest.mark.asyncio
async def test_explicit_base_branch_preserved(client: AsyncClient, db_session):
    """When base_branch is explicitly supplied, it overrides the project promotion_target."""
    owner, name = "acme", "explicit-branch-project"
    project = Project(
        name=f"{owner}/{name}",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": f"{owner}/{name}",
            "title": "Task with explicit base_branch",
            "prompt": "work on main",
            "task_type": "create",
            "base_branch": "main",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    stored_base_branch = get_resp.json()["payloads"][0]["base_branch"]
    assert stored_base_branch == "main", (
        f"Expected explicit 'main' but got {stored_base_branch!r}"
    )


@pytest.mark.asyncio
async def test_paused_create_atomically_persists_exact_task_authority(
    client: AsyncClient, db_session
):
    """A paused project can stage one exact, dependency-gated task row."""
    project = await _make_project_with_member(db_session, "acme/atomic-admission")
    project.paused = True
    project.integration_branch = "automain"
    project.promotion_target = "main"
    project.directions = [
        {"id": "d-atomic", "text": "Atomic admission", "status": "active"}
    ]
    version = ReleaseVersion(
        project_id=project.id,
        version_number=54,
        status="open",
        phase="build",
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)

    criterion_ids = ["c-duration", "c-retention"]
    db_session.add_all(
        [
            Direction(
                project_id=project.id,
                direction_key="d-atomic",
                status="active",
                text="Atomic admission",
                data={
                    "id": "d-atomic",
                    "status": "active",
                    "criterion_release_version_id": str(version.id),
                },
                # The direction owns a third key; an explicit task assignment
                # must remain the requested two-key subset.
                criterion_keys=[*criterion_ids, "c-unrelated"],
            ),
            *[
                VersionAcceptanceCriterion(
                    project_id=project.id,
                    release_version_id=version.id,
                    criterion_key=key,
                    status="active",
                    text=key,
                )
                for key in [*criterion_ids, "c-unrelated"]
            ],
        ]
    )
    predecessor = Task(
        project_id=project.id,
        title="Corpus prerequisite",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "freeze", "base_branch": "automain"}],
    )
    db_session.add(predecessor)
    await db_session.commit()
    await db_session.refresh(predecessor)

    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/atomic-admission",
            "project_id": str(project.id),
            "title": "Budget consumer",
            "prompt": "Enforce the selected-test budget after the corpus lands.",
            "task_type": "create",
            "base_branch": "automain",
            "direction_id": "d-atomic",
            "criterion_ids": criterion_ids,
            "depends_on": [str(predecessor.id)],
            "require_deps_landed_on": "automain",
            "release_base_branch": "automain",
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_atomic_admission.py::test_budget",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["direction_id"] == "d-atomic"
    assert response.json()["criterion_ids"] == criterion_ids
    assert response.json()["criterion_ids_explicit"] is True
    assert response.json()["base_branch"] == "automain"
    assert response.json()["depends_on"] == [str(predecessor.id)]
    assert response.json()["criterion_release_version_id"] == str(version.id)
    created = await db_session.get(Task, response.json()["id"])
    assert created is not None
    assert created.status == TaskStatus.QUEUED
    assert created.depends_on == [str(predecessor.id)]
    assert created.criterion_ids == criterion_ids
    assert created.context["direction_id"] == "d-atomic"
    assert created.context["criterion_ids_explicit"] is True
    assert created.context["criterion_release_version_id"] == str(version.id)
    creation_authority = created.context["_operator_task_creation_authority"]
    assert {
        key: creation_authority[key]
        for key in (
            "direction_id",
            "criterion_ids",
            "criterion_ids_explicit",
            "criterion_release_version_id",
            "base_branch",
            "depends_on",
            "require_deps_landed_on",
            "release_base_branch",
        )
    } == {
        "direction_id": "d-atomic",
        "criterion_ids": criterion_ids,
        "criterion_ids_explicit": True,
        "criterion_release_version_id": str(version.id),
        "base_branch": "automain",
        "depends_on": [str(predecessor.id)],
        "require_deps_landed_on": "automain",
        "release_base_branch": "automain",
    }
    assert len(creation_authority["canonical_request_sha256"]) == 64
    assert set(creation_authority["canonical_request_field_sha256"]) >= {
        "task_type",
        "base_branch",
        "title",
        "prompt",
        "acceptance_evidence",
        "direction_id",
        "criterion_ids",
        "depends_on",
    }
    assert created.context["_structural_depends_on_ids"] == [str(predecessor.id)]
    assert created.first_payload["require_deps_landed_on"] == "automain"
    assert created.first_payload["release_base_branch"] == "automain"
    assert created.first_payload["base_branch"] == "automain"
    await db_session.refresh(project)
    assert project.paused is True

    # Exercise the final cold-dispatch claim fence, not merely the row state:
    # the fully-authored QUEUED task cannot be claimed while paused. Once the
    # same project is explicitly unpaused, the pause fence releases it (the
    # dependency gate remains a separate downstream admission check).
    from app.orchestrator import _lock_dispatchable_task_for_mutation

    project_id = project.id
    created_task_id = created.id
    assert (
        await _lock_dispatchable_task_for_mutation(db_session, created_task_id) is None
    )
    await db_session.rollback()
    project = await db_session.get(Project, project_id)
    assert project is not None
    project.paused = False
    db_session.add(project)
    await db_session.commit()
    claimable_after_unpause = await _lock_dispatchable_task_for_mutation(
        db_session, created_task_id
    )
    assert claimable_after_unpause is not None
    await db_session.commit()


@pytest.mark.asyncio
async def test_atomic_create_rejects_invalid_criterion_and_cross_project_dependency(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(db_session, "acme/admission-guards")
    foreign = await _make_project_with_member(db_session, "acme/admission-foreign")
    version = ReleaseVersion(project_id=project.id, version_number=54, status="open")
    direction = Direction(
        project_id=project.id,
        direction_key="d-guards",
        status="active",
        data={
            "id": "d-guards",
            "status": "active",
            "criterion_release_version_id": str(version.id),
        },
        criterion_keys=[],
    )
    other_direction = Direction(
        project_id=project.id,
        direction_key="d-other",
        status="active",
        data={"id": "d-other", "status": "active"},
        criterion_keys=["c-other-direction"],
    )
    staged_direction = Direction(
        project_id=project.id,
        direction_key="d-staged",
        status="staged",
        data={"id": "d-staged", "status": "staged"},
    )
    foreign_task = Task(
        project_id=foreign.id,
        title="Foreign prerequisite",
        payloads=[{"type": "create", "prompt": "foreign", "base_branch": "main"}],
    )
    cancelled_task = Task(
        project_id=project.id,
        title="Cancelled prerequisite",
        status=TaskStatus.CANCELLED,
        payloads=[{"type": "create", "prompt": "cancelled", "base_branch": "main"}],
    )
    db_session.add_all(
        [
            version,
            direction,
            other_direction,
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=version.id,
                criterion_key="c-other-direction",
                status="active",
                text="Owned by another direction",
            ),
            staged_direction,
            foreign_task,
            cancelled_task,
        ]
    )
    await db_session.commit()
    await db_session.refresh(foreign_task)
    cycle_a = Task(
        project_id=project.id,
        title="Cycle A",
        payloads=[{"type": "create", "prompt": "a", "base_branch": "main"}],
    )
    cycle_b = Task(
        project_id=project.id,
        title="Cycle B",
        payloads=[{"type": "create", "prompt": "b", "base_branch": "main"}],
    )
    db_session.add_all([cycle_a, cycle_b])
    await db_session.commit()
    await db_session.refresh(cycle_a)
    await db_session.refresh(cycle_b)
    cycle_a.depends_on = [str(cycle_b.id)]
    cycle_b.depends_on = [str(cycle_a.id)]
    await db_session.commit()
    initial_project_task_count = await _count_tasks(db_session, project.id)

    body = {
        "github_repo": "acme/admission-guards",
        "project_id": str(project.id),
        "title": "Rejected task",
        "prompt": "Must not persist.",
        "task_type": "create",
        "direction_id": "d-guards",
        "criterion_ids": ["c-missing"],
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_atomic_admission.py",
        },
    }
    invalid_criterion = await client.post("/api/admin/tasks", json=body)
    assert invalid_criterion.status_code == 400, invalid_criterion.text

    body["criterion_ids"] = ["c-other-direction"]
    wrong_direction_criterion = await client.post("/api/admin/tasks", json=body)
    assert wrong_direction_criterion.status_code == 400
    assert "criterion_ids_outside_direction" in wrong_direction_criterion.text

    staged_body = {
        **body,
        "direction_id": "d-staged",
    }
    staged_body.pop("criterion_ids")
    staged_direction_response = await client.post(
        "/api/admin/tasks", json=staged_body
    )
    assert staged_direction_response.status_code == 400
    assert "requires an active direction" in staged_direction_response.text

    body.pop("criterion_ids")
    body["depends_on"] = [str(foreign_task.id)]
    foreign_dependency = await client.post("/api/admin/tasks", json=body)
    assert foreign_dependency.status_code == 400, foreign_dependency.text

    body["depends_on"] = [str(cancelled_task.id)]
    cancelled_dependency = await client.post("/api/admin/tasks", json=body)
    assert cancelled_dependency.status_code == 400, cancelled_dependency.text
    assert "terminal non-success" in cancelled_dependency.text

    body["depends_on"] = [str(cycle_a.id)]
    cyclic_dependency = await client.post("/api/admin/tasks", json=body)
    assert cyclic_dependency.status_code == 400, cyclic_dependency.text
    assert "cyclic prerequisite set" in cyclic_dependency.text
    assert await _count_tasks(db_session, project.id) == initial_project_task_count


@pytest.mark.asyncio
async def test_atomic_create_rejects_stale_direction_criterion_version(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(db_session, "acme/stale-direction")
    stale_version = ReleaseVersion(
        project_id=project.id,
        version_number=53,
        status="promoted",
    )
    open_version = ReleaseVersion(
        project_id=project.id,
        version_number=54,
        status="open",
        phase="build",
    )
    db_session.add_all([stale_version, open_version])
    await db_session.commit()
    criterion_key = "c-current"
    db_session.add_all(
        [
            Direction(
                project_id=project.id,
                direction_key="d-stale",
                status="active",
                data={
                    "id": "d-stale",
                    "status": "active",
                    "criterion_release_version_id": str(stale_version.id),
                },
                criterion_keys=[criterion_key],
            ),
            VersionAcceptanceCriterion(
                project_id=project.id,
                release_version_id=open_version.id,
                criterion_key=criterion_key,
                status="active",
                text="Current-version criterion",
            ),
        ]
    )
    await db_session.commit()

    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/stale-direction",
            "project_id": str(project.id),
            "title": "Do not admit stale direction authority",
            "prompt": "This direction is bound to the previous release.",
            "task_type": "create",
            "direction_id": "d-stale",
            "criterion_ids": [criterion_key],
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_stale_direction.py::test_current",
            },
        },
    )

    assert response.status_code == 400, response.text
    assert "criterion_direction_version_mismatch" in response.text
    assert str(stale_version.id) in response.text
    assert str(open_version.id) in response.text


@pytest.mark.asyncio
async def test_idempotency_replay_rejects_different_task_authority(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(db_session, "acme/idempotent-authority")
    body = {
        "github_repo": "acme/idempotent-authority",
        "project_id": str(project.id),
        "title": "Stable request",
        "prompt": "Create exactly once.",
        "task_type": "create",
        "idempotency_key": "exact-authority-replay",
        "acceptance_evidence": _CODE_EVIDENCE,
    }
    first = await client.post("/api/admin/tasks", json=body)
    assert first.status_code == 200, first.text

    changed = {
        **body,
        "depends_on": ["11111111-1111-4111-8111-111111111111"],
    }
    replay = await client.post("/api/admin/tasks", json=changed)

    assert replay.status_code == 409, replay.text
    assert "different authority" in replay.text
    assert await _count_tasks(db_session, project.id) == 1


@pytest.mark.parametrize(
    ("changed_fields", "mismatch_field"),
    [
        pytest.param({"task_type": "refactor"}, "task_type", id="task-type"),
        pytest.param({"base_branch": "automain"}, "base_branch", id="base-branch"),
        pytest.param({"title": "Changed title"}, "title", id="title"),
        pytest.param({"prompt": "Changed prompt"}, "prompt", id="prompt"),
        pytest.param(
            {
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_idempotency.py::test_changed",
                }
            },
            "acceptance_evidence",
            id="acceptance-evidence",
        ),
    ],
)
@pytest.mark.asyncio
async def test_idempotency_replay_rejects_changed_canonical_request_field(
    client: AsyncClient,
    db_session,
    changed_fields,
    mismatch_field,
):
    project = await _make_project_with_member(
        db_session, f"acme/idempotent-{mismatch_field.replace('_', '-')}"
    )
    body = {
        "github_repo": f"acme/idempotent-{mismatch_field.replace('_', '-')}",
        "project_id": str(project.id),
        "title": "Canonical request",
        "prompt": "Create this exact task.",
        "task_type": "create",
        "base_branch": "main",
        "idempotency_key": "canonical-request-replay",
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_idempotency.py::test_original",
        },
    }
    first = await client.post("/api/admin/tasks", json=body)
    assert first.status_code == 200, first.text

    replay = await client.post(
        "/api/admin/tasks",
        json={**body, **changed_fields},
    )

    assert replay.status_code == 409, replay.text
    assert mismatch_field in replay.text
    assert await _count_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_idempotency_replay_uses_immutable_creation_authority(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(db_session, "acme/idempotent-snapshot")
    original_dep = Task(
        project_id=project.id,
        title="Original dependency",
        payloads=[{"type": "create", "prompt": "original", "base_branch": "main"}],
    )
    replacement_dep = Task(
        project_id=project.id,
        title="Replacement dependency",
        payloads=[{"type": "create", "prompt": "replacement", "base_branch": "main"}],
    )
    db_session.add_all([original_dep, replacement_dep])
    await db_session.commit()
    body = {
        "github_repo": "acme/idempotent-snapshot",
        "project_id": str(project.id),
        "title": "Stable dependency request",
        "prompt": "Create exactly once.",
        "task_type": "create",
        "idempotency_key": "stable-dependency-authority",
        "depends_on": [str(original_dep.id)],
        "acceptance_evidence": _CODE_EVIDENCE,
    }
    first = await client.post("/api/admin/tasks", json=body)
    assert first.status_code == 200, first.text
    created = await db_session.get(Task, first.json()["id"])
    assert created is not None

    # Runtime dependency remapping must not change what an idempotency replay
    # compares against: the request identity is the admitted snapshot.
    created.depends_on = [str(replacement_dep.id)]
    await db_session.commit()

    replay = await client.post("/api/admin/tasks", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["is_duplicate"] is True
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["depends_on"] == [str(original_dep.id)]


def test_direct_review_reuse_keeps_authority_check_without_display_text_identity():
    from fastapi import HTTPException

    from app.admin_task_admission import (
        DIRECT_REVIEW_REUSE_AUTHORITY_REQUEST_FIELDS,
        TaskCreationAuthority,
        build_task_creation_authority_snapshot,
        canonical_task_creation_request,
        reject_idempotent_task_authority_mismatch,
    )
    from app.admin_task_contracts import CreateTaskPayload

    project_id = uuid.uuid4()
    payload = CreateTaskPayload(
        github_repo="acme/direct-review",
        project_id=project_id,
        title="Review PR 42",
        prompt="Review the exact pull request head.",
        task_type="review",
        pr_number=42,
        branch="feature/pr-42",
        base_branch="automain",
    )
    canonical = canonical_task_creation_request(
        payload,
        resolved_base_branch="automain",
    )
    authority = TaskCreationAuthority(
        direction_id=None,
        criterion_ids=[],
        criterion_release_version_id=None,
        depends_on=[],
    )
    existing = Task(
        project_id=project_id,
        title=payload.title,
        payloads=[
            {
                "type": "review",
                "prompt": payload.prompt,
                "pr_number": 42,
                "branch": "feature/pr-42",
                "base_branch": "automain",
            }
        ],
        context={
            "_operator_task_creation_authority": (
                build_task_creation_authority_snapshot(
                    authority,
                    canonical_request=canonical,
                    criterion_ids_explicit=False,
                    require_deps_landed_on=None,
                    release_base_branch=None,
                )
            )
        },
    )

    display_only_replay = canonical_task_creation_request(
        payload.model_copy(
            update={
                "title": "Different operator label",
                "prompt": "Different display prompt for the same review owner.",
            }
        ),
        resolved_base_branch="automain",
    )
    reject_idempotent_task_authority_mismatch(
        existing,
        canonical_request=display_only_replay,
        conflict_label="active review reuse",
        comparison_fields=DIRECT_REVIEW_REUSE_AUTHORITY_REQUEST_FIELDS,
    )

    wrong_base = {**display_only_replay, "base_branch": "main"}
    with pytest.raises(HTTPException) as mismatch:
        reject_idempotent_task_authority_mismatch(
            existing,
            canonical_request=wrong_base,
            conflict_label="active review reuse",
            comparison_fields=DIRECT_REVIEW_REUSE_AUTHORITY_REQUEST_FIELDS,
        )
    assert mismatch.value.status_code == 409
    assert "base_branch" in mismatch.value.detail


@pytest.mark.asyncio
async def test_non_hotfix_create_task_still_works(client: AsyncClient, db_session):
    """Regression guard: the hotfix check must not block an ordinary create
    task. A create task with no hotfix flag succeeds and stores NO hotfix key."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Ordinary create",
            "prompt": "build the feature",
            "base_branch": "main",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    payloads = get_resp.json()["payloads"]
    assert payloads[0]["type"] == "create"
    assert "hotfix" not in payloads[0]


# --- Non-launchable task-type guard (reject doomed compute_experiment) -------
#
# The manual creation path only carries the generic CreateTaskPayload fields; it
# cannot supply the launch contract a compute_experiment needs (non-empty
# command + positive spend_cap_usd + positive deadline_seconds). The endpoint
# must reject any task_type the capability registry declares non-launchable BEFORE
# any Task row is created, rather than enqueueing a task that only fails later on
# the agent. The gate is registry-driven (USER_LAUNCHABLE_TASK_TYPES), so it
# generalizes to every non-launchable type, not just compute_experiment.


async def _count_tasks(db_session, project_id) -> int:
    from sqlalchemy import func, select

    from app.models import Task

    return (
        await db_session.execute(
            select(func.count(Task.id)).where(Task.project_id == project_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_compute_experiment_rejected_at_creation(
    client: AsyncClient, db_session
):
    """compute_experiment is not user_launchable → 400, and no Task is created."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Doomed compute experiment",
            "prompt": "run the experiment",
            "base_branch": "main",
            "task_type": "compute_experiment",
        },
    )

    assert resp.status_code == 400, resp.text
    # HTTPException detail surfaces at error.message (main.py http_exception_handler).
    message = resp.json()["error"]["message"].lower()
    assert "compute_experiment" in message
    assert "launchable" in message
    # The doomed task must never have been persisted.
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_non_launchable_type_rejected(client: AsyncClient, db_session):
    """A different non-launchable type (memo) is rejected by the same registry
    gate — proving the guard is registry-driven, not a compute_experiment
    special case."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Memo via manual path",
            "prompt": "reason about a topic",
            "base_branch": "main",
            "task_type": "memo",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "memo" in resp.json()["error"]["message"].lower()
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_user_launchable_type_still_accepted(client: AsyncClient, db_session):
    """Regression guard: the non-launchable gate must not over-reject. A
    user_launchable type (research) is still accepted and persisted."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Codebase research",
            "prompt": "analyze the codebase",
            "base_branch": "main",
            "task_type": "research",
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    stored_payload = get_resp.json()["payloads"][0]
    assert stored_payload["type"] == "research"
    assert stored_payload["base_branch"] == "main"
    assert await _count_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_manual_research_gets_canonical_root_envelope(
    client: AsyncClient, db_session, monkeypatch
):
    """Omitted research fields use the same root contract as autonomous/direct work."""
    from app import admin
    from app.research_completion import effective_research_finding_headroom

    project = Project(
        name="acme/research-envelope",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner="acme",
        github_repo="research-envelope",
        integration_branch="automain",
        promotion_target="main",
        directions=[
            {
                "id": "direction-1",
                "text": "Improve research throughput",
                "status": "active",
                "category": "research",
                "priority": "high",
                "system_id": "research-runtime",
                "completion_criteria": "A bounded end-to-end proof exists",
                "research_context": {"decision_question": "Does the parent carry work?"},
                "paths": ["backend/app/research_completion.py"],
            }
        ],
        problem_model={
            "capabilities": [{"name": "Repository Mapping", "status": "working"}]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    monkeypatch.setitem(
        admin._runtime_config,
        "agent_prompt_suffixes",
        {"research": "Inspect the exact causal path."},
    )

    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/research-envelope",
            "title": "Research the parent",
            "prompt": "Find the remaining parent-quality gaps",
            "task_type": "research",
        },
    )

    assert response.status_code == 200, response.text
    stored = (
        await client.get(f"/api/admin/tasks/{response.json()['id']}")
    ).json()["payloads"][0]
    assert stored["base_branch"] == "automain"
    assert stored["work_headroom"] == effective_research_finding_headroom(0)
    assert stored["prompt_suffix"] == "Inspect the exact causal path."
    assert stored["available_capabilities"][0]["name"] == "repository-mapping"
    assert stored["directions"] == [
        {
            "id": "direction-1",
            "text": "Improve research throughput",
            "category": "research",
            "priority": "high",
            "system_id": "research-runtime",
            "completion_criteria": "A bounded end-to-end proof exists",
            "research_context": {"decision_question": "Does the parent carry work?"},
            "paths": ["backend/app/research_completion.py"],
        }
    ]


# --- Idempotency tests -------------------------------------------------------
#
# Manual task creation accepts an optional client-supplied idempotency_key.
# When provided, duplicate requests with the same key return the existing task
# instead of creating a new one. This protects against double-click, network
# retry, and proxy replay scenarios.


@pytest.mark.asyncio
async def test_idempotency_key_dedupes_duplicate_submit(
    client: AsyncClient, db_session
):
    """Two requests with the same idempotency_key return the same task."""
    await _make_project_with_member(db_session, "acme/widget")

    key = "my-unique-key-001"
    resp1 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Idempotent task",
            "prompt": "do something",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )

    assert resp1.status_code == 200, resp1.text
    task_id_1 = resp1.json()["id"]
    # First response should not have is_duplicate flag (or it's False)
    assert not resp1.json().get("is_duplicate")

    # Retry with the same key
    resp2 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Idempotent task",
            "prompt": "do something",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )

    assert resp2.status_code == 200, resp2.text
    task_id_2 = resp2.json()["id"]
    # Same task ID returned
    assert task_id_1 == task_id_2
    # Second response marked as duplicate
    assert resp2.json()["is_duplicate"] is True
    assert resp2.json()["task_type"] == "create"


@pytest.mark.asyncio
async def test_idempotent_retry_succeeds_after_active_task_cap(
    client: AsyncClient, db_session
):
    """A replay returns its original task even after the project reaches 200 active tasks."""
    project = await _make_project_with_member(db_session, "acme/widget")
    key = "retry-at-active-cap"
    body = {
        "github_repo": "acme/widget",
        "project_id": str(project.id),
        "title": "Already accepted",
        "prompt": "do something",
        "task_type": "create",
        "acceptance_evidence": _CODE_EVIDENCE,
        "idempotency_key": key,
    }
    first = await client.post("/api/admin/tasks", json=body)
    assert first.status_code == 200, first.text

    db_session.add_all([
        Task(
            project_id=project.id,
            title=f"filler {index}",
            description="fill the active queue",
            status=TaskStatus.QUEUED,
            payloads=[{"type": "plan", "prompt": "fill", "base_branch": "main"}],
            vm_instance_id=f"filler-vm-{index}",
        )
        for index in range(199)
    ])
    await db_session.commit()

    replay = await client.post("/api/admin/tasks", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["is_duplicate"] is True


@pytest.mark.asyncio
async def test_explicit_project_id_targets_exact_duplicate_repo_project(
    client: AsyncClient, db_session
):
    project_a = await _make_project_with_member(db_session, "acme/widget")
    project_b = await _make_project_with_member(db_session, "acme/widget")

    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "project_id": str(project_b.id),
            "title": "Target B",
            "prompt": "enqueue on the explicitly selected project",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == str(project_b.id)
    assert await _count_tasks(db_session, project_a.id) == 0
    assert await _count_tasks(db_session, project_b.id) == 1


@pytest.mark.asyncio
async def test_explicit_project_id_rejects_repo_mismatch(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(db_session, "acme/gadget")
    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "project_id": str(project.id),
            "title": "Mismatched identity",
            "prompt": "must not enqueue",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )
    assert response.status_code == 422, response.text
    assert "not acme/widget" in response.json()["error"]["message"]
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_explicit_project_id_enforces_member_role(
    client: AsyncClient, db_session
):
    project = await _make_project_with_member(
        db_session, "acme/widget", role="viewer"
    )
    response = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "project_id": str(project.id),
            "title": "Viewer cannot enqueue",
            "prompt": "must not enqueue",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )
    assert response.status_code == 403, response.text
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_idempotency_key_no_second_task_created(
    client: AsyncClient, db_session
):
    """When idempotency_key dedupes, only one task is persisted."""
    project = await _make_project_with_member(db_session, "acme/widget")

    key = "my-unique-key-002"
    await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "First request",
            "prompt": "do work",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )

    await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "First request",
            "prompt": "do work",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )

    # Exactly one task created for this project
    task_count = await _count_tasks(db_session, project.id)
    assert task_count == 1


@pytest.mark.asyncio
async def test_without_idempotency_key_creates_duplicates(
    client: AsyncClient, db_session
):
    """Regression guard: without idempotency_key, identical requests create
    separate tasks (the old behavior that the fix addresses)."""
    project = await _make_project_with_member(db_session, "acme/widget")

    # First request without idempotency_key
    resp1 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Unprotected task",
            "prompt": "do something",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert resp1.status_code == 200, resp1.text
    task_id_1 = resp1.json()["id"]

    # Second identical request without idempotency_key
    resp2 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Unprotected task",
            "prompt": "do something",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert resp2.status_code == 200, resp2.text
    task_id_2 = resp2.json()["id"]

    # Different task IDs (separate tasks created)
    assert task_id_1 != task_id_2
    # No is_duplicate flag when idempotency_key not used
    assert not resp2.json().get("is_duplicate")

    # Two tasks persisted
    task_count = await _count_tasks(db_session, project.id)
    assert task_count == 2


@pytest.mark.asyncio
async def test_idempotency_key_scoped_per_project(client: AsyncClient, db_session):
    """A client key that collides across projects must NOT dedupe cross-project.

    The stored idempotency_key is namespaced with the project id, so the same
    user-supplied key in two different projects yields two distinct tasks and
    never leaks the foreign project's task fields (cross-project info disclosure).
    """
    project_a = await _make_project_with_member(db_session, "acme/widget")
    project_b = await _make_project_with_member(db_session, "acme/gadget")

    key = "shared-client-key-collision"
    resp_a = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Task in project A",
            "prompt": "do A",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )
    assert resp_a.status_code == 200, resp_a.text
    assert not resp_a.json().get("is_duplicate")

    resp_b = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/gadget",
            "title": "Task in project B",
            "prompt": "do B",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
            "idempotency_key": key,
        },
    )
    assert resp_b.status_code == 200, resp_b.text
    # Same client key but different project → fresh task, no cross-project dedup
    assert not resp_b.json().get("is_duplicate")
    assert resp_a.json()["id"] != resp_b.json()["id"]
    assert resp_b.json()["project_id"] == str(project_b.id)

    # Each project has exactly its own single task
    assert await _count_tasks(db_session, project_a.id) == 1
    assert await _count_tasks(db_session, project_b.id) == 1


# --- Role floor on the existing-project path ---------------------------------
#
# Manual task creation resolves the target project by (owner, repo) scoped to the
# caller's memberships. Membership alone is NOT authorization to create work: a
# task executes code and consumes LLM/GPU budget, so the existing-project path
# requires the member+ floor (viewer is documented read-only). The auto-create
# branch is unaffected — a caller with no member-visible project for the repo
# becomes the owner of the brand-new project.


async def _count_projects_for_repo(db_session, owner, repo) -> int:
    from sqlalchemy import func, select

    return (
        await db_session.execute(
            select(func.count(Project.id)).where(
                Project.github_owner == owner, Project.github_repo == repo
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_viewer_cannot_create_task_on_existing_project(
    client: AsyncClient, db_session
):
    """A viewer on the target project gets 403 and no task is enqueued."""
    project = await _make_project_with_member(db_session, "acme/widget", role="viewer")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Viewer-enqueued work",
            "prompt": "spend the budget",
            "base_branch": "main",
            "task_type": "create",
        },
    )

    assert resp.status_code == 403, resp.text
    # HTTPException detail surfaces at error.message (main.py http_exception_handler).
    assert "member" in resp.json()["error"]["message"].lower()
    # No task enqueued...
    assert await _count_tasks(db_session, project.id) == 0
    # ...and the rejection must NOT have fallen through into the auto-create
    # branch, which would hand the viewer a shadow project they own.
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 1


@pytest.mark.asyncio
async def test_member_can_create_task_on_existing_project(
    client: AsyncClient, db_session
):
    """The member floor must not over-reject: a member (not owner) can create."""
    project = await _make_project_with_member(db_session, "acme/widget", role="member")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Member-enqueued work",
            "prompt": "do the work",
            "base_branch": "main",
            "task_type": "create",
            "acceptance_evidence": _CODE_EVIDENCE,
        },
    )

    assert resp.status_code == 200, resp.text
    assert await _count_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_ambiguous_repo_prefers_writable_project_over_viewer_one(
    client: AsyncClient, db_session
):
    """Two projects for one repo (viewer on A, owner of B) → create on B, not 403.

    ``ix_project_github_owner_repo`` is NOT unique and the auto-create branch
    deliberately mints a project per (caller, repo), so a user can legitimately
    be a viewer on one project for ``acme/widget`` and the owner of another. A
    membership-scoped ``.scalars().first()`` with no ORDER BY picks either row at
    random; checking the role only AFTER that pick would 403 the caller roughly
    half the time — randomly locking them out of creating tasks on their OWN
    project. The resolver must look for a member+ project FIRST.
    """
    viewer_project = await _make_project_with_member(
        db_session, "acme/widget", role="viewer"
    )
    owner_project = await _make_project_with_member(
        db_session, "acme/widget", role="owner"
    )
    assert viewer_project.id != owner_project.id
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 2

    # Run several times: a nondeterministic pick would flake, not fail outright.
    for i in range(4):
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/widget",
                "title": f"Work on my own project {i}",
                "prompt": "do the work",
                "base_branch": "main",
                "task_type": "create",
                "acceptance_evidence": _CODE_EVIDENCE,
            },
        )
        assert resp.status_code == 200, resp.text

    # Every task landed on the project the caller can WRITE to...
    assert await _count_tasks(db_session, owner_project.id) == 4
    # ...none on the viewer-only one...
    assert await _count_tasks(db_session, viewer_project.id) == 0
    # ...and no shadow project was minted.
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 2


@pytest.mark.asyncio
async def test_create_validation_reason_surfaces_in_422(
    client: AsyncClient, db_session
):
    """A schema-validation failure on the operator create surface returns the
    authored reason instead of the opaque 'Invalid request'.

    Regression for the payload-validation 422 opacity fix: submitting a
    code-producing task without an acceptance_evidence declaration must surface
    WHY it was rejected (the field/validator message), so the operator UI toast
    and an autonomously spawning agent can see the reason and self-correct.
    """
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Missing evidence",
            "prompt": "patch the bug",
            "base_branch": "main",
            "task_type": "create",
        },
    )

    assert resp.status_code == 422, resp.text
    # The app wraps HTTPException as {"error": {"code", "message"}} (main.py
    # http_exception_handler) — there is no top-level "detail" key.
    message = resp.json()["error"]["message"]
    assert "acceptance_evidence" in message
    assert "Invalid request" not in message
    # The reason must survive end-to-end (no multi-line pydantic block).
    assert "\n" not in message
