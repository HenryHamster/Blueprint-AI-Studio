"""Endpoint tests for the operator fresh-reviewer / SR-investigation routes.

Covers four endpoints added together for the operator-triggered review flow:

1. POST /api/admin/tasks/{task_id}/request-fresh-reviewer   (admin.py)
2. POST /api/agent/tasks/{original_task_id}/reviewer-merge   (routers/agent.py)
3. POST /api/admin/projects/{project_id}/sr-investigation    (routers/admin_digest.py)
4. GET  /api/admin/projects/{project_id}/sr-investigation-task-ids (routers/admin_digest.py)

The auth check, the 429 cooldown path, branch derivation per task type, and the
dedup path are all exercised here so the non-trivial logic (cooldown computation,
pr_url parsing, branch derivation) is guarded against regression.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth import create_agent_token
from app.models import Artifact, ArtifactType, Task, TaskStatus
from testing.factories import make_membership, make_project, make_task


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _project_with_member(session, role="owner", owner_id="test-user-123"):
    """Create a project plus a membership for ``owner_id`` at ``role``."""
    project = make_project(user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(make_membership(project.id, user_id="test-user-123", role=role))
    await session.commit()
    return project


async def _task_with_pr(session, project_id, payload_type="create", pr_number=4242):
    """Create a COMPLETED task with a PULL_REQUEST artifact."""
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": payload_type, "prompt": "do the thing"}],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/acme/repo/pull/{pr_number}",
        )
    )
    await session.commit()
    return task


def _hex8(task_id) -> str:
    return str(task_id).replace("-", "")[:8]


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/admin/tasks/{task_id}/request-fresh-reviewer
# ═══════════════════════════════════════════════════════════════════════════


class TestRequestFreshReviewer:
    @pytest.mark.asyncio
    async def test_success_spawns_reviewer_task(self, client, db_session):
        """A completed task with a PR spawns a reviewer task and stamps cooldown."""
        project = await _project_with_member(db_session)
        task = await _task_with_pr(db_session, project.id, "create", pr_number=4242)

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200
        spawned_id = uuid.UUID(resp.json()["spawned_task_id"])

        spawned = (
            await db_session.execute(select(Task).where(Task.id == spawned_id))
        ).scalar_one()
        payload = spawned.payloads[0]
        assert payload["pr_number"] == 4242
        assert payload["branch"] == f"blueprint/task-{_hex8(task.id)}"
        assert spawned.context["operator_requested_review_for"] == str(task.id)

        await db_session.refresh(task)
        assert task.context.get("_operator_fresh_reviewer_requested_at")

    @pytest.mark.asyncio
    async def test_branch_derivation_by_task_type(self, client, db_session):
        """A refactor task derives a 'refactor'-prefixed branch (iter-5 regression)."""
        project = await _project_with_member(db_session)
        task = await _task_with_pr(db_session, project.id, "refactor")

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200
        spawned_id = uuid.UUID(resp.json()["spawned_task_id"])
        spawned = (
            await db_session.execute(select(Task).where(Task.id == spawned_id))
        ).scalar_one()
        assert spawned.payloads[0]["branch"] == f"blueprint/refactor-{_hex8(task.id)}"

    @pytest.mark.asyncio
    async def test_explicit_review_loop_branch_takes_precedence(
        self, client, db_session
    ):
        """An explicit context.review_loop.branch overrides type-derived branch."""
        project = await _project_with_member(db_session)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "docs", "prompt": "x"}],
            context={"review_loop": {"branch": "blueprint/custom-deadbeef"}},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/repo/pull/7",
            )
        )
        await db_session.commit()

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200
        spawned_id = uuid.UUID(resp.json()["spawned_task_id"])
        spawned = (
            await db_session.execute(select(Task).where(Task.id == spawned_id))
        ).scalar_one()
        assert spawned.payloads[0]["branch"] == "blueprint/custom-deadbeef"

    @pytest.mark.asyncio
    async def test_cooldown_returns_429(self, client, db_session):
        """A request inside the cooldown window is rejected with 429."""
        project = await _project_with_member(db_session)
        task = await _task_with_pr(db_session, project.id)
        task.context = {
            "_operator_fresh_reviewer_requested_at": datetime.now(
                timezone.utc
            ).isoformat()
        }
        db_session.add(task)
        await db_session.commit()

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_cooldown_expired_allows_request(self, client, db_session):
        """A stale cooldown timestamp (older than the window) allows a new pass."""
        project = await _project_with_member(db_session)
        task = await _task_with_pr(db_session, project.id)
        task.context = {
            "_operator_fresh_reviewer_requested_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()
        }
        db_session.add(task)
        await db_session.commit()

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_pr_returns_400(self, client, db_session):
        """A task without a PR artifact is rejected with 400."""
        project = await _project_with_member(db_session)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "x"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_member_returns_404(self, client, db_session):
        """A user with no membership on the task's project gets 404 (auth check)."""
        project = make_project(user_id="someone-else")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = await _task_with_pr(db_session, project.id)

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_per_project_namespace_used_for_branch(self, client, db_session):
        """Branch derivation uses project build_config.task_branch_namespace, not 'blueprint'."""
        project = make_project(
            user_id="test-user-123",
            build_config={"task_branch_namespace": "acme"},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()
        task = await _task_with_pr(db_session, project.id, "create")

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200
        spawned_id = uuid.UUID(resp.json()["spawned_task_id"])
        spawned = (
            await db_session.execute(select(Task).where(Task.id == spawned_id))
        ).scalar_one()
        assert spawned.payloads[0]["branch"] == f"acme/task-{_hex8(task.id)}"

    @pytest.mark.asyncio
    async def test_per_project_integration_branch_used_as_base(
        self, client, db_session
    ):
        """base_branch falls back to project.integration_branch, not hardcoded 'main'."""
        project = make_project(
            user_id="test-user-123",
            integration_branch="develop",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()
        # Task payload intentionally omits base_branch to exercise the fallback
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "do the thing"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/repo/pull/99",
            )
        )
        await db_session.commit()

        resp = await client.post(f"/api/admin/tasks/{task.id}/request-fresh-reviewer")
        assert resp.status_code == 200
        spawned_id = uuid.UUID(resp.json()["spawned_task_id"])
        spawned = (
            await db_session.execute(select(Task).where(Task.id == spawned_id))
        ).scalar_one()
        assert spawned.payloads[0]["base_branch"] == "develop"


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/agent/tasks/{original_task_id}/reviewer-merge
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewerMerge:
    @pytest.mark.asyncio
    async def test_success_merges_findings(self, client, db_session):
        """An authorized reviewer task merges findings into the original SR outcome."""
        project = await _project_with_member(db_session)
        original = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": True}}
                }
            },
        )
        db_session.add(original)
        await db_session.commit()
        await db_session.refresh(original)

        reviewer = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="agent-vm-rev",
            context={"operator_requested_review_for": str(original.id)},
        )
        db_session.add(reviewer)
        await db_session.commit()
        await db_session.refresh(reviewer)

        token = create_agent_token(str(reviewer.id), "agent-vm-rev")
        resp = await client.post(
            f"/api/agent/tasks/{original.id}/reviewer-merge",
            headers={"x-agent-token": token},
            json={
                "reviewer_findings": [
                    {
                        "category": "correctness",
                        "description": "missed null check",
                        "severity": "HIGH",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.json()["finding_count"] == 1

        await db_session.refresh(original)
        sr = original.context["_self_review_outcome"]
        assert len(sr["reviewer_findings"]) == 1
        assert sr["reviewer_recovery_origin"] == "operator_requested_recovery"
        # A passing self-attestation is overridden by the reviewer finding.
        assert sr["check_results"]["correctness"]["passed"] is False
        assert (
            sr["check_results"]["correctness"]["origin"]
            == "operator_requested_recovery"
        )

    @pytest.mark.asyncio
    async def test_wrong_target_returns_403(self, client, db_session):
        """A reviewer task not scoped to the target task is rejected with 403."""
        project = await _project_with_member(db_session)
        original = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(original)
        await db_session.commit()
        await db_session.refresh(original)

        reviewer = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="agent-vm-rev2",
            context={"operator_requested_review_for": str(uuid.uuid4())},
        )
        db_session.add(reviewer)
        await db_session.commit()
        await db_session.refresh(reviewer)

        token = create_agent_token(str(reviewer.id), "agent-vm-rev2")
        resp = await client.post(
            f"/api/agent/tasks/{original.id}/reviewer-merge",
            headers={"x-agent-token": token},
            json={"reviewer_findings": []},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# /api/admin/projects/{project_id}/sr-investigation[-task-ids]
# ═══════════════════════════════════════════════════════════════════════════


class TestSrInvestigation:
    @pytest.mark.asyncio
    async def test_spawns_investigation_task(self, client, db_session):
        """A first request for a category spawns a new investigation task."""
        project = await _project_with_member(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/sr-investigation",
            json={"category": "correctness", "signals": ["miss_rate_up"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] is True

        spawned = (
            await db_session.execute(
                select(Task).where(Task.id == uuid.UUID(body["task_id"]))
            )
        ).scalar_one()
        assert spawned.context["spawned_by"] == "operator_sr_investigation"
        assert spawned.context["sr_category"] == "correctness"

    @pytest.mark.asyncio
    async def test_dedup_returns_existing_task(self, client, db_session):
        """A second request for the same open category returns the existing task."""
        project = await _project_with_member(db_session)

        first = await client.post(
            f"/api/admin/projects/{project.id}/sr-investigation",
            json={"category": "security"},
        )
        assert first.status_code == 200
        assert first.json()["created"] is True

        second = await client.post(
            f"/api/admin/projects/{project.id}/sr-investigation",
            json={"category": "security"},
        )
        assert second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["task_id"] == first.json()["task_id"]

    @pytest.mark.asyncio
    async def test_task_ids_map(self, client, db_session):
        """The task-ids endpoint returns a category -> task_id map of open work."""
        project = await _project_with_member(db_session)
        spawn = await client.post(
            f"/api/admin/projects/{project.id}/sr-investigation",
            json={"category": "performance"},
        )
        task_id = spawn.json()["task_id"]

        resp = await client.get(
            f"/api/admin/projects/{project.id}/sr-investigation-task-ids"
        )
        assert resp.status_code == 200
        assert resp.json()["task_ids"]["performance"] == task_id

    @pytest.mark.asyncio
    async def test_non_member_returns_404(self, client, db_session):
        """A user with no membership on the project gets 404 (auth check)."""
        project = make_project(user_id="someone-else")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/sr-investigation",
            json={"category": "correctness"},
        )
        assert resp.status_code == 404
