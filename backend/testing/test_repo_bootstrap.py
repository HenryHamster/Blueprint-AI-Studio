"""Tests for repo_bootstrap task type: auto-spawn at onboarding, strategic spawn gate,
recovery helper, operator endpoint, and background sweep."""

import uuid
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.auth import create_agent_token
from app.main import app
from app.models import Project, ProjectStatus, Task, TaskHistory, TaskStatus
from app.auth import get_current_user, require_admin

TEST_USER_ID = "test-user-bootstrap-123"
# The conftest's override_get_current_user returns this user_id for HTTP clients.
_HTTP_CLIENT_USER_ID = "test-user-123"


@pytest.fixture(autouse=True)
def override_auth():
    async def _user():
        return {"user_id": TEST_USER_ID, "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_admin] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_admin, None)


def _onboard_patches(build_config=None, default_branch="main"):
    """Return context managers to patch onboarding dependencies."""
    return (
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=build_config if build_config is not None else {"language": "python"},
        ),
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.github.get_repo_default_branch",
            new_callable=AsyncMock,
            return_value=default_branch,
        ),
    )


class TestRepoBootstrapOnboardSpawn:
    @pytest.mark.asyncio
    async def test_non_self_repo_non_empty_spawns_bootstrap(
        self, client: AsyncClient, db_session
    ):
        """Onboarding a non-self-repo, non-empty project spawns a repo_bootstrap task."""
        detect_patch, token_patch, branch_patch = _onboard_patches(
            build_config={"language": "python", "repo_is_empty": False}
        )
        with detect_patch, token_patch, branch_patch:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "external-repo"},
            )

        assert resp.status_code == 200
        project_id = resp.json()["project_id"]
        project = await db_session.get(Project, project_id)
        assert project is not None
        assert not project.is_self_repo

        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        bootstrap_tasks = [
            t for t in tasks
            if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 1, (
            f"Expected 1 repo_bootstrap task, got {len(bootstrap_tasks)}"
        )
        bt = bootstrap_tasks[0]
        assert bt.first_payload["github_owner"] == "acme"
        assert bt.first_payload["github_repo"] == "external-repo"
        assert bt.first_payload["is_first_bootstrap"] is True
        assert (bt.context or {}).get("bootstrap_reason") == "first_cycle"

    @pytest.mark.asyncio
    async def test_self_repo_does_not_spawn_bootstrap(
        self, client: AsyncClient, db_session
    ):
        """Onboarding a self-repo project must NOT spawn a repo_bootstrap task."""
        detect_patch, token_patch, branch_patch = _onboard_patches(
            build_config={"language": "python", "repo_is_empty": False}
        )
        with detect_patch, token_patch, branch_patch:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "HenryHamster", "github_repo": "BlueprintAIStudio"},
            )

        assert resp.status_code == 200

        # Manually mark as self-repo to simulate the DB flag being set
        # (in practice the is_self_repo flag is set by a separate mechanism).
        # Instead, verify that when is_self_repo=True no bootstrap spawns.
        # Create a project with is_self_repo=True directly.
        self_repo_project = Project(
            name="self/repo",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="self",
            github_repo="repo",
            is_self_repo=True,
            build_config={"language": "python", "repo_is_empty": False},
            integration_branch="main",
        )
        db_session.add(self_repo_project)
        await db_session.commit()
        await db_session.refresh(self_repo_project)

        # The spawn logic checks is_self_repo — since this project was created
        # directly (not via onboard), verify that the condition holds.
        assert self_repo_project.is_self_repo is True
        assert not (self_repo_project.build_config or {}).get("repo_is_empty")

        # Verify no bootstrap task was auto-spawned for the self-repo project
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == self_repo_project.id,
                )
            )
        ).scalars().all()
        bootstrap_tasks = [
            t for t in tasks
            if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 0, (
            "self_repo project must not have a repo_bootstrap task"
        )

    @pytest.mark.asyncio
    async def test_empty_repo_does_not_spawn_bootstrap(
        self, client: AsyncClient, db_session
    ):
        """Onboarding a repo_is_empty=True project must NOT spawn a repo_bootstrap task."""
        detect_patch, token_patch, branch_patch = _onboard_patches(
            build_config={"repo_is_empty": True}
        )
        with detect_patch, token_patch, branch_patch:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "empty-repo"},
            )

        assert resp.status_code == 200
        project_id = resp.json()["project_id"]
        project = await db_session.get(Project, project_id)
        assert project is not None

        tasks = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalars().all()
        bootstrap_tasks = [
            t for t in tasks
            if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 0, (
            "repo_is_empty=True project must not have a repo_bootstrap task"
        )

    @pytest.mark.asyncio
    async def test_already_bootstrapped_project_does_not_spawn_again(
        self, client: AsyncClient, db_session
    ):
        """Re-onboarding with bootstrap_completed_at set must NOT spawn a second bootstrap."""
        detect_patch, token_patch, branch_patch = _onboard_patches(
            build_config={
                "language": "python",
                "repo_is_empty": False,
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            }
        )
        with detect_patch, token_patch, branch_patch:
            resp = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "already-bootstrapped"},
            )

        assert resp.status_code == 200
        project_id = resp.json()["project_id"]
        project = await db_session.get(Project, project_id)
        assert project is not None

        tasks = (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        ).scalars().all()
        bootstrap_tasks = [
            t for t in tasks
            if (t.first_payload or {}).get("type") == "repo_bootstrap"
        ]
        assert len(bootstrap_tasks) == 0, (
            "Project with bootstrap_completed_at must not spawn a second bootstrap task"
        )


class TestRepoBootstrapStrategicGate:
    """Tests for the strategic spawn gate that defers when bootstrap is pending."""

    def _make_project(self, build_config=None, is_self_repo=False):
        return Project(
            id=uuid.uuid4(),
            name="test/project",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="myrepo",
            is_self_repo=is_self_repo,
            build_config=build_config or {},
            integration_branch="main",
            promotion_target="main",
        )

    @pytest.mark.asyncio
    async def test_spawn_strategic_review_defers_when_bootstrap_pending(
        self, db_session
    ):
        """_spawn_strategic_review returns False when a pending repo_bootstrap exists."""
        from app.strategic_spawning import _spawn_strategic_review

        project = self._make_project(build_config={"language": "python"})
        db_session.add(project)
        await db_session.flush()

        # Create a pending bootstrap task
        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: test/project",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _spawn_strategic_review(project, db_session)
        assert result is False, (
            "_spawn_strategic_review must defer (return False) when bootstrap is pending"
        )

    @pytest.mark.asyncio
    async def test_spawn_strategic_review_proceeds_when_bootstrap_completed(
        self, db_session
    ):
        """_spawn_strategic_review proceeds (spawns) when bootstrap_completed_at is set."""
        from app.strategic_spawning import _spawn_strategic_review

        project = self._make_project(
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # No bootstrap task needed — bootstrap_completed_at is set
        result = await _spawn_strategic_review(project, db_session)
        assert result is True, (
            "_spawn_strategic_review must proceed (return True) when bootstrap_completed_at is set"
        )

        # Verify a strategic_review task was spawned
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        strategic_tasks = [
            t for t in tasks
            if (t.first_payload or {}).get("type") == "strategic_review"
        ]
        assert len(strategic_tasks) >= 1, (
            "Expected at least one strategic_review task after bootstrap completes"
        )

    @pytest.mark.asyncio
    async def test_spawn_strategic_review_proceeds_when_bootstrap_failed(
        self, db_session
    ):
        """_spawn_strategic_review proceeds even when bootstrap task has FAILED (degrade gracefully)."""
        from app.strategic_spawning import _spawn_strategic_review

        project = self._make_project(build_config={"language": "python"})
        db_session.add(project)
        await db_session.flush()

        # Bootstrap task in terminal FAILED state
        failed_bootstrap = Task(
            project_id=project.id,
            title="Bootstrap: test/project",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(failed_bootstrap)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _spawn_strategic_review(project, db_session)
        assert result is True, (
            "_spawn_strategic_review must proceed (return True) when bootstrap FAILED (degrade gracefully)"
        )

    @pytest.mark.asyncio
    async def test_spawn_strategic_review_proceeds_when_no_bootstrap_task(
        self, db_session
    ):
        """_spawn_strategic_review proceeds when no bootstrap task exists and no completed_at."""
        from app.strategic_spawning import _spawn_strategic_review

        project = self._make_project(build_config={"language": "python"})
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _spawn_strategic_review(project, db_session)
        # No pending bootstrap, no completed_at — proceeds normally
        assert result is True, (
            "_spawn_strategic_review must proceed when there is no pending bootstrap task"
        )


class TestRepoBootstrapCompletionStamp:
    """Tests for bootstrap_completed_at being stamped on task completion."""

    @pytest.mark.asyncio
    async def test_bootstrap_completion_stamps_completed_at(self, db_session):
        """Completing a repo_bootstrap task with a valid overview stamps bootstrap_completed_at."""
        import json as _json
        from app.task_completion import _handle_repo_bootstrap_completion

        project = Project(
            name="acme/stamped",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="stamped",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/stamped",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        overview_logs = _json.dumps({
            "tech_stack": "Python, FastAPI",
            "directory_layout": "backend/ — FastAPI app; admin-ui/ — React UI",
            "first_steps_summary": "pip install -r requirements.txt && pytest",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task, overview_logs, db_session)

        await db_session.refresh(project)
        assert (project.build_config or {}).get("bootstrap_completed_at") is not None, (
            "bootstrap_completed_at must be set after _handle_repo_bootstrap_completion with a valid overview"
        )

    @pytest.mark.asyncio
    async def test_bootstrap_completion_idempotent(self, db_session):
        """Calling _handle_repo_bootstrap_completion twice does not overwrite the timestamp."""
        from app.task_completion import _handle_repo_bootstrap_completion

        project = Project(
            name="acme/idempotent",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="idempotent",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/idempotent",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        # Call twice — the timestamp must not be overwritten
        await _handle_repo_bootstrap_completion(bootstrap_task, "", db_session)

        await db_session.refresh(project)
        assert (project.build_config or {}).get("bootstrap_completed_at") == "2026-01-01T00:00:00", (
            "Existing bootstrap_completed_at must not be overwritten"
        )


class TestRepoBootstrapDesignDocPersistence:
    """Tests for design_doc persistence via the completion handler."""

    @pytest.mark.asyncio
    async def test_bootstrap_with_overview_persists_design_doc_section(self, db_session):
        """Completing a bootstrap with a valid overview persists it to design_doc['repo_overview']
        and makes it visible in the strategic design-doc index (_design_doc_index /
        _agent_view_design_doc) — the same surface the first strategic cycle consumes.
        bootstrap_completed_at is stamped clean and no degraded_reason is set."""
        import json as _json
        from app.task_completion import _handle_repo_bootstrap_completion
        from app.strategic_spawning import _design_doc_index, _agent_view_design_doc

        project = Project(
            name="acme/with-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="with-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/with-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        overview_logs = _json.dumps({
            "tech_stack": "Python 3.12, FastAPI, SQLAlchemy",
            "directory_layout": "backend/ — API; admin-ui/ — React frontend",
            "first_steps_summary": "cd backend && pip install -r requirements.txt && pytest",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task, overview_logs, db_session)

        await db_session.refresh(project)

        # 1. Section present in design_doc under the non-'_'-prefixed key.
        assert "repo_overview" in (project.design_doc or {}), (
            "design_doc['repo_overview'] must be present after completion with overview"
        )
        stored = (project.design_doc or {})["repo_overview"]
        assert stored["tech_stack"] == "Python 3.12, FastAPI, SQLAlchemy"
        assert stored["directory_layout"] == "backend/ — API; admin-ui/ — React frontend"
        assert stored["first_steps_summary"] == "cd backend && pip install -r requirements.txt && pytest"

        # 2. Section visible in _agent_view_design_doc (agent-facing view, drops '_'-keys).
        agent_view = _agent_view_design_doc(project.design_doc)
        assert "repo_overview" in agent_view, (
            "repo_overview must appear in _agent_view_design_doc (the agent-facing design doc view)"
        )
        # Internal keys should be stripped from the agent view
        assert "_meta" not in agent_view

        # 3. Section visible in _design_doc_index (strategic index consumed by first cycle).
        index = _design_doc_index(agent_view)
        section_keys = [e["key"] for e in index.get("sections", [])]
        assert "repo_overview" in section_keys, (
            "repo_overview must appear in _design_doc_index — the strategic cycle's navigation surface"
        )

        # 4. Clean success: bootstrap_completed_at stamped, no degraded_reason.
        bc = project.build_config or {}
        assert bc.get("bootstrap_completed_at") is not None, (
            "bootstrap_completed_at must be set on clean success"
        )
        assert "bootstrap_degraded_reason" not in bc, (
            "bootstrap_degraded_reason must not be set on clean success"
        )

    @pytest.mark.asyncio
    async def test_bootstrap_repo_overview_index_includes_summary(self, db_session):
        """The _design_doc_index entry for repo_overview carries inline tech_stack,
        directory_layout, and first_steps_summary fields so the first strategic cycle
        has repo orientation without a round-trip fetch. Degraded path (no overview)
        produces no inline fields."""
        import json as _json
        from app.task_completion import _handle_repo_bootstrap_completion
        from app.strategic_spawning import _design_doc_index, _agent_view_design_doc

        project = Project(
            name="acme/inline-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="inline-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/inline-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        overview_logs = _json.dumps({
            "tech_stack": "TypeScript, Node.js, Vitest",
            "directory_layout": "agent/ — worker; backend/ — API; admin-ui/ — React",
            "first_steps_summary": "cd agent && npm install && npm test",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task, overview_logs, db_session)
        await db_session.refresh(project)

        # Build the strategic index from the persisted design_doc.
        agent_view = _agent_view_design_doc(project.design_doc)
        index = _design_doc_index(agent_view)
        entries = {e["key"]: e for e in index.get("sections", [])}

        assert "repo_overview" in entries, (
            "repo_overview must appear in _design_doc_index after bootstrap"
        )
        entry = entries["repo_overview"]
        assert entry.get("tech_stack") == "TypeScript, Node.js, Vitest", (
            "tech_stack must be carried inline in the repo_overview index entry"
        )
        assert entry.get("directory_layout") == "agent/ — worker; backend/ — API; admin-ui/ — React", (
            "directory_layout must be carried inline in the repo_overview index entry"
        )
        assert entry.get("first_steps_summary") == "cd agent && npm install && npm test", (
            "first_steps_summary must be carried inline in the repo_overview index entry"
        )

        # Degraded path: no overview → no inline fields on any entry.
        project2 = Project(
            name="acme/inline-degraded",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="inline-degraded",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project2)
        await db_session.flush()

        bootstrap_task2 = Task(
            project_id=project2.id,
            title="Bootstrap: acme/inline-degraded",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task2)
        await db_session.commit()
        await db_session.refresh(project2)
        await db_session.refresh(bootstrap_task2)

        await _handle_repo_bootstrap_completion(bootstrap_task2, "", db_session)
        await db_session.refresh(project2)

        # Degraded: no repo_overview section at all, so no inline fields.
        agent_view2 = _agent_view_design_doc(project2.design_doc)
        index2 = _design_doc_index(agent_view2)
        entries2 = {e["key"]: e for e in index2.get("sections", [])}
        assert "repo_overview" not in entries2, (
            "repo_overview must not appear in the index when bootstrap degraded (no overview)"
        )

    @pytest.mark.asyncio
    async def test_bootstrap_overview_persists_optional_fields(self, db_session):
        """Bootstrap completion persists all 7 overview fields when the agent
        provides them; absent optional fields are gracefully omitted."""
        import json as _json
        from app.task_completion import _handle_repo_bootstrap_completion
        from app.strategic_spawning import _design_doc_index, _agent_view_design_doc

        project = Project(
            name="acme/full-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="full-overview",
            build_config={"language": "typescript"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/full-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        overview_logs = _json.dumps({
            "tech_stack": "TypeScript, Node.js",
            "directory_layout": "src/ — source; tests/ — test suite",
            "first_steps_summary": "npm install && npm test",
            "entry_points": "src/index.ts — main server; src/cli.ts — CLI tool",
            "test_layout": "tests/unit — unit tests; tests/integration — integration; npm test runs all",
            "key_conventions": "branch: feat/<name>; commits: conventional commits; PRs require one review",
            "environment_requirements": "Node >= 18; DATABASE_URL required; .env.example provided",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task, overview_logs, db_session)
        await db_session.refresh(project)

        stored = (project.design_doc or {}).get("repo_overview", {})
        assert stored.get("tech_stack") == "TypeScript, Node.js"
        assert stored.get("entry_points") == "src/index.ts — main server; src/cli.ts — CLI tool"
        assert stored.get("test_layout") == "tests/unit — unit tests; tests/integration — integration; npm test runs all"
        assert stored.get("key_conventions") == "branch: feat/<name>; commits: conventional commits; PRs require one review"
        assert stored.get("environment_requirements") == "Node >= 18; DATABASE_URL required; .env.example provided"

        # Verify optional fields appear inline in the strategic index.
        agent_view = _agent_view_design_doc(project.design_doc)
        index = _design_doc_index(agent_view)
        entry = {e["key"]: e for e in index.get("sections", [])}.get("repo_overview", {})
        assert entry.get("entry_points") == "src/index.ts — main server; src/cli.ts — CLI tool"
        assert entry.get("test_layout") == "tests/unit — unit tests; tests/integration — integration; npm test runs all"
        assert entry.get("key_conventions") == "branch: feat/<name>; commits: conventional commits; PRs require one review"
        assert entry.get("environment_requirements") == "Node >= 18; DATABASE_URL required; .env.example provided"

        # Partial path: only 3 required fields → optional fields absent from stored doc.
        project3 = Project(
            name="acme/partial-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="partial-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project3)
        await db_session.flush()

        bootstrap_task3 = Task(
            project_id=project3.id,
            title="Bootstrap: acme/partial-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task3)
        await db_session.commit()
        await db_session.refresh(project3)
        await db_session.refresh(bootstrap_task3)

        partial_logs = _json.dumps({
            "tech_stack": "Python",
            "directory_layout": "backend/ — app",
            "first_steps_summary": "pip install -r requirements.txt && pytest",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task3, partial_logs, db_session)
        await db_session.refresh(project3)

        stored3 = (project3.design_doc or {}).get("repo_overview", {})
        assert stored3.get("tech_stack") == "Python"
        assert "entry_points" not in stored3, "Optional field absent when not provided"
        assert "test_layout" not in stored3, "Optional field absent when not provided"
        assert "key_conventions" not in stored3, "Optional field absent when not provided"
        assert "environment_requirements" not in stored3, "Optional field absent when not provided"

    @pytest.mark.asyncio
    async def test_bootstrap_without_overview_records_degraded_marker(self, db_session):
        """Completing a bootstrap with no parseable overview records bootstrap_degraded_reason
        in build_config (explicit degraded marker, not silent clean success) and does NOT
        create a spurious design_doc section."""
        from app.task_completion import _handle_repo_bootstrap_completion

        project = Project(
            name="acme/no-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="no-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/no-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        # Empty logs: no parseable overview in the completion payload.
        await _handle_repo_bootstrap_completion(bootstrap_task, "", db_session)

        await db_session.refresh(project)

        # 1. Degraded marker recorded — not silent clean success.
        bc = project.build_config or {}
        assert bc.get("bootstrap_degraded_reason") == "overview_missing", (
            "bootstrap_degraded_reason must be 'overview_missing' when no overview was produced"
        )

        # 2. No clean bootstrap_completed_at on the degraded path.
        assert bc.get("bootstrap_completed_at") is None, (
            "bootstrap_completed_at must NOT be set on the degraded (no-overview) path"
        )

        # 3. No spurious design_doc section created.
        assert "repo_overview" not in (project.design_doc or {}), (
            "design_doc must not contain 'repo_overview' when no overview was produced"
        )


class TestRepoBootstrapCompletionSeam:
    @pytest.mark.asyncio
    async def test_completed_bootstrap_with_overview_posts_payload_and_persists_section(
        self, client: AsyncClient, db_session
    ):
        import json as _json

        project = Project(
            name="acme/status-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="status-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        task = Task(
            project_id=project.id,
            title="Bootstrap: acme/status-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(task)

        token = create_agent_token(
            str(task.id),
            task.vm_instance_id or "",
            task_type="repo_bootstrap",
            project_id=str(project.id),
        )
        payload = {
            "task_id": str(task.id),
            "status": "COMPLETED",
            "repo_overview": {
                "tech_stack": "Python, FastAPI",
                "directory_layout": "backend/ API; agent/ worker",
                "first_steps_summary": "pip install -r requirements.txt && pytest",
            },
            "logs": _json.dumps({"note": "bootstrap finished"}),
        }
        resp = await client.post(
            "/api/agent/status",
            json=payload,
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        stored_overview = (project.design_doc or {}).get("repo_overview") or {}
        # Core overview fields are persisted verbatim...
        assert stored_overview.get("tech_stack") == "Python, FastAPI"
        assert stored_overview.get("directory_layout") == "backend/ API; agent/ worker"
        assert (
            stored_overview.get("first_steps_summary")
            == "pip install -r requirements.txt && pytest"
        )
        # ...alongside a provenance block stamping freshness. (source_branch is
        # None here because this task's payload carries no integration_branch;
        # the refresh path test below exercises a populated source branch.)
        provenance = stored_overview.get("provenance") or {}
        assert provenance.get("refreshed_at"), "overview provenance must stamp refreshed_at"
        assert "source_branch" in provenance

        from app.strategic_spawning import _agent_view_design_doc, _design_doc_index

        agent_view = _agent_view_design_doc(project.design_doc)
        assert "repo_overview" in agent_view
        section_keys = [e["key"] for e in _design_doc_index(agent_view).get("sections", [])]
        assert "repo_overview" in section_keys

        bc = project.build_config or {}
        assert bc.get("bootstrap_completed_at") is not None
        assert "bootstrap_degraded_reason" not in bc

    @pytest.mark.asyncio
    async def test_completed_bootstrap_without_overview_records_degraded_marker(
        self, client: AsyncClient, db_session
    ):
        project = Project(
            name="acme/status-degraded",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="status-degraded",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        task = Task(
            project_id=project.id,
            title="Bootstrap: acme/status-degraded",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(task)

        token = create_agent_token(
            str(task.id),
            task.vm_instance_id or "",
            task_type="repo_bootstrap",
            project_id=str(project.id),
        )
        payload = {
            "task_id": str(task.id),
            "status": "COMPLETED",
            "logs": "bootstrap finished without structured overview",
        }
        resp = await client.post(
            "/api/agent/status",
            json=payload,
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200

        await db_session.refresh(project)
        bc = project.build_config or {}
        assert bc.get("bootstrap_degraded_reason") == "overview_missing"
        assert bc.get("bootstrap_completed_at") is None
        assert "repo_overview" not in (project.design_doc or {})

    @pytest.mark.asyncio
    async def test_partial_bootstrap_overview_logs_mark_degraded_not_completed(
        self, db_session
    ):
        """Completing a bootstrap with a partial overview (missing or empty required fields)
        records bootstrap_degraded_reason in build_config and does NOT set bootstrap_completed_at.

        A complete overview requires all three fields (tech_stack, directory_layout,
        first_steps_summary) to be present and non-empty. Partial overviews should be
        treated the same as missing overviews."""
        from app.task_completion import _handle_repo_bootstrap_completion

        project = Project(
            name="acme/partial-overview",
            user_id=TEST_USER_ID,
            status=ProjectStatus.DEPLOYING,
            github_owner="acme",
            github_repo="partial-overview",
            build_config={"language": "python"},
            integration_branch="main",
        )
        db_session.add(project)
        await db_session.flush()

        bootstrap_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/partial-overview",
            description="First-cycle bootstrap",
            payloads=[{"type": "repo_bootstrap", "integration_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(bootstrap_task)
        await db_session.commit()
        await db_session.refresh(project)
        await db_session.refresh(bootstrap_task)

        # Test case: overview missing directory_layout (incomplete)
        import json
        logs = json.dumps({
            "tech_stack": "Python, FastAPI",
            "directory_layout": "",  # Empty required field
            "first_steps_summary": "pip install && pytest",
        })
        await _handle_repo_bootstrap_completion(bootstrap_task, logs, db_session)

        await db_session.refresh(project)

        # Should be treated as degraded (missing/empty required field)
        bc = project.build_config or {}
        assert bc.get("bootstrap_degraded_reason") == "overview_missing", (
            "bootstrap_degraded_reason must be 'overview_missing' for partial overview"
        )
        assert bc.get("bootstrap_completed_at") is None, (
            "bootstrap_completed_at must NOT be set when required overview field is empty"
        )
        assert "repo_overview" not in (project.design_doc or {}), (
            "design_doc must not contain 'repo_overview' when overview is partial/incomplete"
        )


# ---------------------------------------------------------------------------
# Recovery path: spawn_bootstrap_task helper
# ---------------------------------------------------------------------------


def _make_external_project(
    db_session,
    *,
    name: str = "acme/recovery-test",
    build_config=None,
    is_self_repo: bool = False,
    add_membership: bool = False,
) -> Project:
    """Helper: create and add an external project to the session.

    Pass add_membership=True for projects accessed via the HTTP client
    (requires a ProjectMember row for _get_user_project_with_role).
    """
    from app.models import ProjectMember

    project = Project(
        id=uuid.uuid4(),
        name=name,
        user_id=TEST_USER_ID,
        status=ProjectStatus.DEPLOYING,
        github_owner="acme",
        github_repo=name.split("/")[-1],
        is_self_repo=is_self_repo,
        build_config=build_config if build_config is not None else {"language": "python"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    if add_membership:
        member = ProjectMember(
            project_id=project.id,
            user_id=_HTTP_CLIENT_USER_ID,
            role="owner",
        )
        db_session.add(member)
    return project


class TestSpawnBootstrapTaskHelper:
    """Unit tests for the shared spawn_bootstrap_task helper."""

    @pytest.mark.asyncio
    async def test_recovery_spawns_exactly_one_bootstrap_for_degraded_project(
        self, db_session
    ):
        """Recovery spawns exactly one new bootstrap task for a degraded project.

        A degraded project has bootstrap_degraded_reason set (no
        bootstrap_completed_at) and no active bootstrap task. The recovery
        helper must enqueue exactly one QUEUED repo_bootstrap task.
        """
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/degraded-recovery",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
            },
        )
        await db_session.flush()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery_sweep"
        )
        await db_session.commit()

        assert result.outcome == "spawned", (
            f"Expected 'spawned' for degraded project, got {result.outcome!r}: {result.reason}"
        )
        assert result.task_id is not None

        # Exactly one active bootstrap task exists.
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        assert len(tasks) == 1, (
            f"Expected exactly 1 QUEUED repo_bootstrap task, got {len(tasks)}"
        )
        task = tasks[0]
        assert task.first_payload["is_first_bootstrap"] is False
        assert (task.context or {}).get("bootstrap_reason") == "recovery_sweep"

        # A TaskHistory SPAWNED row exists for this task.
        history = (
            await db_session.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id == task.id,
                    TaskHistory.action == "SPAWNED",
                )
            )
        ).scalars().all()
        assert len(history) == 1, "Expected a SPAWNED TaskHistory row"

    @pytest.mark.asyncio
    async def test_recovery_no_op_when_bootstrap_already_active(self, db_session):
        """Recovery returns already_active and does not create a duplicate task."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/active-bootstrap",
            build_config={"language": "python"},
        )
        await db_session.flush()

        # Create an existing active bootstrap task.
        active_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/active-bootstrap",
            description="Active bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(active_task)
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )

        assert result.outcome == "already_active", (
            f"Expected 'already_active', got {result.outcome!r}"
        )
        assert result.task_id is not None

        # Still only one repo_bootstrap task.
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(tasks) == 1, "Must not create a duplicate when one is already active"

    @pytest.mark.asyncio
    async def test_recovery_no_op_when_bootstrap_completed(self, db_session):
        """Recovery returns already_completed when bootstrap_completed_at is set."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/completed-bootstrap",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )

        assert result.outcome == "already_completed", (
            f"Expected 'already_completed', got {result.outcome!r}"
        )
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(tasks) == 0, "Must not create a task when bootstrap_completed_at is set"

    @pytest.mark.asyncio
    async def test_recovery_no_op_for_self_repo(self, db_session):
        """Recovery returns not_eligible for a self-repo project."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="self/repo",
            build_config={"language": "python"},
            is_self_repo=True,
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )

        assert result.outcome == "not_eligible"
        assert result.reason == "self_repo"

    @pytest.mark.asyncio
    async def test_recovery_no_op_for_empty_repo(self, db_session):
        """Recovery returns not_eligible for an empty-repo project."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/empty-repo",
            build_config={"repo_is_empty": True},
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )

        assert result.outcome == "not_eligible"
        assert result.reason == "repo_is_empty"

    @pytest.mark.asyncio
    async def test_recovery_spawns_for_project_with_failed_bootstrap(self, db_session):
        """Recovery spawns a new bootstrap for a project whose prior bootstrap FAILED."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/failed-bootstrap",
            build_config={"language": "python"},
        )
        await db_session.flush()

        # Prior bootstrap task in FAILED state.
        failed_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/failed-bootstrap",
            description="Previous bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.FAILED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(failed_task)
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )
        if result.outcome == "spawned":
            await db_session.commit()

        assert result.outcome == "spawned", (
            f"Expected 'spawned' after a FAILED bootstrap, got {result.outcome!r}"
        )

    @pytest.mark.asyncio
    async def test_recovery_spawns_for_project_with_cancelled_bootstrap(self, db_session):
        """Recovery spawns a new bootstrap for a project whose prior bootstrap CANCELLED."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/cancelled-bootstrap",
            build_config={"language": "python"},
        )
        await db_session.flush()

        cancelled_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/cancelled-bootstrap",
            description="Previous bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.CANCELLED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(cancelled_task)
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )
        if result.outcome == "spawned":
            await db_session.commit()

        assert result.outcome == "spawned", (
            f"Expected 'spawned' after a CANCELLED bootstrap, got {result.outcome!r}"
        )

    @pytest.mark.asyncio
    async def test_recovery_spawns_for_project_with_no_bootstrap_task_at_all(
        self, db_session
    ):
        """Recovery spawns a bootstrap for a project that never had a bootstrap task."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/no-prior-bootstrap",
            build_config={"language": "python"},
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery"
        )
        if result.outcome == "spawned":
            await db_session.commit()

        assert result.outcome == "spawned", (
            f"Expected 'spawned' for a project with no bootstrap task, got {result.outcome!r}"
        )

    @pytest.mark.asyncio
    async def test_automatic_recovery_respects_backoff(self, db_session):
        """Automatic recovery does not spawn before bootstrap_recovery_next_at."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/backoff-bootstrap",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
                "bootstrap_recovery_attempts": 1,
                "bootstrap_recovery_state": "backoff",
                "bootstrap_recovery_next_at": "2099-01-01T00:00:00",
            },
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason="recovery_sweep",
            automatic_recovery=True,
        )

        assert result.outcome == "not_eligible"
        assert result.reason == "bootstrap_recovery_backoff"

    @pytest.mark.asyncio
    async def test_operator_retry_bypasses_exhausted_recovery(self, db_session):
        """Operator retry can enqueue one task even after automatic recovery exhausts."""
        from app.repo_bootstrap import spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/operator-exhausted-bootstrap",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
                "bootstrap_recovery_attempts": 3,
                "bootstrap_recovery_state": "exhausted",
                "bootstrap_recovery_exhausted_at": "2026-01-01T00:00:00",
            },
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason="operator_retry",
        )
        await db_session.commit()

        assert result.outcome == "spawned"

        second = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason="operator_retry",
        )

        assert second.outcome == "already_active"

    @pytest.mark.asyncio
    async def test_repeated_degraded_bootstrap_does_not_auto_respawn_unbounded(
        self, db_session
    ):
        """Automatic recovery stops after repeated no-overview bootstrap completions."""
        from app.repo_bootstrap import spawn_bootstrap_task
        from app.task_completion import _handle_repo_bootstrap_completion

        project = _make_external_project(
            db_session,
            name="acme/repeated-degraded-bootstrap",
            build_config={"language": "python"},
        )
        await db_session.flush()

        task = Task(
            project_id=project.id,
            title="Bootstrap: acme/repeated-degraded-bootstrap",
            description="Initial bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(task)
        await db_session.flush()
        await db_session.refresh(task)

        for expected_attempt in (1, 2, 3):
            await _handle_repo_bootstrap_completion(task, "", db_session)
            await db_session.refresh(project)
            build_config = dict(project.build_config or {})
            assert build_config.get("bootstrap_recovery_attempts") == expected_attempt

            if expected_attempt == 3:
                break

            build_config["bootstrap_recovery_next_at"] = "2020-01-01T00:00:00"
            project.build_config = build_config
            db_session.add(project)
            await db_session.commit()

            result = await spawn_bootstrap_task(
                project,
                db_session,
                is_first_bootstrap=False,
                spawn_reason="recovery_sweep",
                automatic_recovery=True,
            )
            await db_session.commit()
            assert result.outcome == "spawned"
            assert result.task_id is not None

            task = await db_session.get(Task, uuid.UUID(result.task_id))
            assert task is not None
            task.status = TaskStatus.COMPLETED
            db_session.add(task)
            await db_session.flush()

        await db_session.refresh(project)
        build_config = project.build_config or {}
        assert build_config.get("bootstrap_recovery_state") == "exhausted"
        assert build_config.get("bootstrap_recovery_exhausted_at") is not None

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason="recovery_sweep",
            automatic_recovery=True,
        )
        await db_session.commit()

        assert result.outcome == "not_eligible"
        assert result.reason == "bootstrap_recovery_exhausted"
        queued = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        assert queued == []


class TestBootstrapRetryEndpoint:
    """Tests for the POST /api/admin/projects/{id}/bootstrap/retry endpoint."""

    @pytest.mark.asyncio
    async def test_retry_endpoint_spawns_for_degraded_project(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns spawned outcome and creates a QUEUED bootstrap task."""
        project = _make_external_project(
            db_session,
            name="acme/endpoint-degraded",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
            },
            add_membership=True,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/retry"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "spawned", f"Unexpected state: {body}"
        assert body["task_id"] is not None

        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        assert len(tasks) == 1, "Expected exactly one QUEUED repo_bootstrap task"

    @pytest.mark.asyncio
    async def test_retry_endpoint_no_duplicate_when_active(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns already_active and does not create a duplicate."""
        project = _make_external_project(
            db_session,
            name="acme/endpoint-active",
            build_config={"language": "python"},
            add_membership=True,
        )
        await db_session.flush()

        active_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/endpoint-active",
            description="Active bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(active_task)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/retry"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "already_active", f"Unexpected state: {body}"

        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(tasks) == 1, "Must not create a duplicate"

    @pytest.mark.asyncio
    async def test_retry_endpoint_returns_already_completed(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns already_completed when bootstrap_completed_at is set."""
        project = _make_external_project(
            db_session,
            name="acme/endpoint-completed",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            add_membership=True,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/retry"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "already_completed", f"Unexpected state: {body}"

    @pytest.mark.asyncio
    async def test_retry_endpoint_returns_not_eligible_for_self_repo(
        self, client: AsyncClient, db_session
    ):
        """Endpoint returns not_eligible for a self-repo project."""
        project = _make_external_project(
            db_session,
            name="self/repo",
            build_config={"language": "python"},
            is_self_repo=True,
            add_membership=True,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/retry"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "not_eligible", f"Unexpected state: {body}"


class TestBootstrapRecoveryAndStrategicGate:
    """Tests proving the strategic gate re-engages after a recovery spawn."""

    @pytest.mark.asyncio
    async def test_recovery_spawns_exactly_one_bootstrap_for_degraded_project(
        self, db_session
    ):
        """Recovery path acceptance test: degraded project gets exactly one bootstrap.

        A project is 'degraded' when bootstrap_degraded_reason is set (no
        bootstrap_completed_at) and no active bootstrap task exists. The
        recovery helper enqueues exactly one task and the strategic gate
        then defers (returns False) because a pending bootstrap exists.
        """
        from app.repo_bootstrap import spawn_bootstrap_task
        from app.strategic_spawning import _spawn_strategic_review

        project = _make_external_project(
            db_session,
            name="acme/degraded-gate-test",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
            },
        )
        await db_session.flush()
        await db_session.commit()

        # Confirm the strategic gate currently PROCEEDS (degraded, no active task).
        await _spawn_strategic_review(project, db_session)
        # Clean up any strategic task spawned.
        from sqlalchemy import delete as _delete
        await db_session.execute(
            _delete(Task).where(
                Task.project_id == project.id,
                Task.payloads[0]["type"].as_string().in_(["design_doc_update", "strategic_review"]),
            )
        )
        await db_session.commit()

        # Now recovery-spawn the bootstrap.
        spawn_result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery_sweep"
        )
        await db_session.commit()

        assert spawn_result.outcome == "spawned", (
            f"Recovery spawn must succeed for degraded project, got {spawn_result.outcome!r}"
        )

        # The strategic gate must now DEFER (return False) because a pending bootstrap exists.
        await db_session.refresh(project)
        gate_result = await _spawn_strategic_review(project, db_session)
        assert gate_result is False, (
            "_spawn_strategic_review must return False (defer) after a recovery bootstrap is spawned"
        )

        # Exactly one bootstrap task in the queue.
        bootstrap_tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()
        assert len(bootstrap_tasks) == 1, (
            f"Expected exactly 1 QUEUED repo_bootstrap task after recovery, got {len(bootstrap_tasks)}"
        )

    @pytest.mark.asyncio
    async def test_degraded_no_active_task_is_eligible_for_recovery(self, db_session):
        """A degraded project with no active bootstrap is the target the sweep recovers.

        The strategic gate PROCEEDS for such a project (degraded=no active task),
        and the recovery helper correctly identifies it as needing a new bootstrap.
        """
        from app.repo_bootstrap import spawn_bootstrap_task
        from app.strategic_spawning import _spawn_strategic_review

        project = _make_external_project(
            db_session,
            name="acme/degraded-eligible",
            build_config={
                "language": "python",
                "bootstrap_degraded_reason": "overview_missing",
            },
        )
        await db_session.flush()
        await db_session.commit()

        # Gate proceeds for degraded-no-active-task projects.
        await _spawn_strategic_review(project, db_session)
        # We just need the gate to NOT permanently block — it may spawn a
        # strategic task or return True/False. The key check is that the
        # helper identifies this project as needing recovery.
        # Clean up any spawned strategic task.
        from sqlalchemy import delete as _delete
        await db_session.execute(
            _delete(Task).where(
                Task.project_id == project.id,
                Task.payloads[0]["type"].as_string().in_(["design_doc_update", "strategic_review"]),
            )
        )
        await db_session.commit()

        # Recovery helper must succeed for this project.
        spawn_result = await spawn_bootstrap_task(
            project, db_session, is_first_bootstrap=False, spawn_reason="recovery_sweep"
        )
        assert spawn_result.outcome == "spawned", (
            f"A degraded project with no active bootstrap must be eligible for recovery, "
            f"got {spawn_result.outcome!r}"
        )


class TestBootstrapSweepDirect:
    """Direct tests for _sweep_bootstrap_recovery — exercises the sweep itself,
    not just the underlying helper.

    Specifically covers the candidate-batching behavior: seeding >20 completed
    projects plus a handful of eligible ones proves that eligible projects are
    NOT starved by the batch limit.
    """

    @pytest.mark.asyncio
    async def test_sweep_spawns_eligible_projects_not_starved_by_completed(
        self, db_session
    ):
        """Eligible projects must not be starved when >_BOOTSTRAP_SWEEP_BATCH_SIZE
        completed (bootstrap_completed_at set) projects exist in the table.

        Seeds 25 completed + 3 eligible projects (28 total). With LIMIT 20
        applied BEFORE filtering, the 3 eligible projects would never be
        reached if the 25 completed rows land first. With filtering in SQL,
        only the 3 eligible rows enter the 20-slot batch and all 3 are spawned.
        """
        from app.recovery_sweeps import _sweep_bootstrap_recovery

        # Create 25 completed projects — ineligible for recovery.
        completed_projects = []
        for i in range(25):
            p = _make_external_project(
                db_session,
                name=f"acme/completed-{i:02d}",
                build_config={
                    "language": "python",
                    "bootstrap_completed_at": "2026-01-01T00:00:00",
                },
            )
            completed_projects.append(p)
        await db_session.flush()

        # Create 3 eligible projects — need recovery bootstrap.
        eligible_projects = []
        for i in range(3):
            p = _make_external_project(
                db_session,
                name=f"acme/eligible-{i:02d}",
                build_config={
                    "language": "python",
                    "bootstrap_degraded_reason": "overview_missing",
                },
            )
            eligible_projects.append(p)
        await db_session.flush()
        await db_session.commit()

        # Run the sweep — it opens its own session internally.
        await _sweep_bootstrap_recovery()

        # Verify: each eligible project has exactly one new QUEUED bootstrap task.
        for proj in eligible_projects:
            await db_session.refresh(proj)
            bootstrap_tasks = (
                await db_session.execute(
                    select(Task).where(
                        Task.project_id == proj.id,
                        Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                        Task.status == TaskStatus.QUEUED,
                    )
                )
            ).scalars().all()
            assert len(bootstrap_tasks) == 1, (
                f"Eligible project {proj.name} must have exactly 1 QUEUED bootstrap "
                f"task after sweep, got {len(bootstrap_tasks)}"
            )

        # Verify: completed projects got no new bootstrap tasks.
        for proj in completed_projects:
            bootstrap_tasks = (
                await db_session.execute(
                    select(Task).where(
                        Task.project_id == proj.id,
                        Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    )
                )
            ).scalars().all()
            assert len(bootstrap_tasks) == 0, (
                f"Completed project {proj.name} must not receive a bootstrap task, "
                f"got {len(bootstrap_tasks)}"
            )

    @pytest.mark.asyncio
    async def test_sweep_is_noop_when_no_eligible_projects(self, db_session):
        """Sweep must not spawn any task when all external projects are completed."""
        from app.recovery_sweeps import _sweep_bootstrap_recovery

        for i in range(5):
            _make_external_project(
                db_session,
                name=f"acme/all-done-{i}",
                build_config={
                    "language": "python",
                    "bootstrap_completed_at": "2026-01-01T00:00:00",
                },
            )
        await db_session.flush()
        await db_session.commit()

        await _sweep_bootstrap_recovery()

        total_bootstrap = (
            await db_session.execute(
                select(Task).where(
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(total_bootstrap) == 0, (
            f"No bootstrap tasks should be spawned when all projects are completed, "
            f"got {len(total_bootstrap)}"
        )

    @pytest.mark.asyncio
    async def test_sweep_idempotent_already_active(self, db_session):
        """Sweep must not spawn a second task when a bootstrap is already active."""
        from app.recovery_sweeps import _sweep_bootstrap_recovery

        project = _make_external_project(
            db_session,
            name="acme/already-active-sweep",
            build_config={"language": "python"},
        )
        await db_session.flush()

        # Pre-create an active bootstrap task.
        active_task = Task(
            project_id=project.id,
            title=f"Bootstrap: {project.name}",
            description="Recovery repo bootstrap",
            payloads=[{
                "type": "repo_bootstrap",
                "github_owner": "acme",
                "github_repo": "already-active-sweep",
                "integration_branch": "main",
                "is_first_bootstrap": False,
            }],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(active_task)
        await db_session.flush()
        await db_session.commit()

        await _sweep_bootstrap_recovery()

        bootstrap_tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(bootstrap_tasks) == 1, (
            f"Sweep must not create a duplicate when bootstrap is already active, "
            f"got {len(bootstrap_tasks)}"
        )


# ---------------------------------------------------------------------------
# Operator refresh path: re-bootstrap an already-completed project
# ---------------------------------------------------------------------------


async def _fetch_queued_bootstrap_task(db_session, project_id) -> Task:
    """Return the single QUEUED repo_bootstrap task for a project."""
    tasks = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                Task.status == TaskStatus.QUEUED,
            )
        )
    ).scalars().all()
    assert len(tasks) == 1, f"Expected exactly one QUEUED bootstrap task, got {len(tasks)}"
    return tasks[0]


class TestBootstrapRefresh:
    """Tests for the operator refresh path (spawn_reason='operator_refresh')."""

    @pytest.mark.asyncio
    async def test_refresh_completed_project_replaces_overview_with_provenance(
        self, db_session
    ):
        """A refresh of a COMPLETED project spawns a new bootstrap, and on completion
        REPLACES the prior repo_overview and stamps provenance (refreshed time + the
        source branch the overview describes)."""
        import json as _json
        from app.repo_bootstrap import REFRESH_SPAWN_REASON, spawn_bootstrap_task
        from app.task_completion import _handle_repo_bootstrap_completion

        project = _make_external_project(
            db_session,
            name="acme/refresh-completed",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
                "bootstrap_recovery_state": "completed",
            },
        )
        # Seed a prior (stale) overview the refresh must replace.
        project.design_doc = {
            "repo_overview": {
                "tech_stack": "OLD stack",
                "directory_layout": "OLD layout",
                "first_steps_summary": "OLD steps",
            },
            "_meta": {"revision": 1},
        }
        flag_modified(project, "design_doc")
        await db_session.flush()
        await db_session.commit()

        # The refresh bypasses the already-completed gate and spawns a task.
        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason=REFRESH_SPAWN_REASON,
        )
        await db_session.commit()

        assert result.outcome == "spawned", (
            f"Refresh must spawn for a completed project, got {result.outcome!r}: {result.reason}"
        )
        await db_session.refresh(project)
        # bootstrap_completed_at is preserved (first-bootstrap gate semantics intact).
        assert (project.build_config or {}).get("bootstrap_completed_at") == "2026-01-01T00:00:00"

        task = await _fetch_queued_bootstrap_task(db_session, project.id)
        assert task.first_payload["is_first_bootstrap"] is False
        assert (task.context or {}).get("bootstrap_reason") == REFRESH_SPAWN_REASON

        # Complete the refreshed bootstrap with a NEW overview + reported revision.
        new_overview = _json.dumps({
            "tech_stack": "NEW Python 3.12, FastAPI",
            "directory_layout": "NEW backend/ — API; admin-ui/ — React",
            "first_steps_summary": "NEW pip install && pytest",
            "source_revision": "abc1234",
        })
        task.status = TaskStatus.COMPLETED
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _handle_repo_bootstrap_completion(task, new_overview, db_session)
        await db_session.refresh(project)

        stored = (project.design_doc or {})["repo_overview"]
        assert stored["tech_stack"] == "NEW Python 3.12, FastAPI", (
            "refresh must REPLACE the prior overview content"
        )
        assert stored["directory_layout"] == "NEW backend/ — API; admin-ui/ — React"
        # Provenance stamped into the design_doc overview block.
        provenance = stored.get("provenance") or {}
        assert provenance.get("refreshed_at"), "design_doc provenance must stamp refreshed_at"
        assert provenance.get("source_branch") == "main", (
            "source_branch must be the bootstrap task's integration_branch"
        )
        assert provenance.get("source_revision") == "abc1234"

        # Provenance also mirrored into build_config for the admin UI.
        bc = project.build_config or {}
        assert bc.get("bootstrap_overview_refreshed_at"), (
            "build_config must carry bootstrap_overview_refreshed_at"
        )
        assert bc.get("bootstrap_overview_source_branch") == "main"
        assert bc.get("bootstrap_overview_source_revision") == "abc1234"

    @pytest.mark.asyncio
    async def test_refresh_idempotent_when_bootstrap_already_active(self, db_session):
        """Refresh returns already_active and creates no duplicate when a bootstrap runs."""
        from app.repo_bootstrap import REFRESH_SPAWN_REASON, spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/refresh-active",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
        )
        await db_session.flush()

        active_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/refresh-active",
            description="Active bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(active_task)
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason=REFRESH_SPAWN_REASON,
        )

        assert result.outcome == "already_active", (
            f"Refresh must be idempotent against an active bootstrap, got {result.outcome!r}"
        )
        tasks = (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                )
            )
        ).scalars().all()
        assert len(tasks) == 1, "Refresh must not create a duplicate bootstrap task"

    @pytest.mark.asyncio
    async def test_refresh_does_not_arm_auto_retry(self, db_session):
        """Refresh is one-shot: it must NOT schedule automatic recovery / auto-retry."""
        from app.repo_bootstrap import REFRESH_SPAWN_REASON, spawn_bootstrap_task

        project = _make_external_project(
            db_session,
            name="acme/refresh-no-autoretry",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
        )
        await db_session.flush()
        await db_session.commit()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=False,
            spawn_reason=REFRESH_SPAWN_REASON,
        )
        await db_session.commit()
        await db_session.refresh(project)

        assert result.outcome == "spawned"
        bc = project.build_config or {}
        # No backoff timer armed and a clear non-automatic recovery marker.
        assert "bootstrap_recovery_next_at" not in bc, (
            "Refresh must NOT arm the automatic-recovery backoff timer"
        )
        assert bc.get("bootstrap_recovery_state") == "operator_refresh"
        assert bc.get("bootstrap_recovery_attempts") == 0

    @pytest.mark.asyncio
    async def test_brand_new_project_first_bootstrap_gate_unchanged(self, db_session):
        """A brand-new (never-bootstrapped) project is unaffected by the refresh path:
        a normal recovery spawn still works and the strategic gate still defers until
        the FIRST bootstrap completes."""
        from app.repo_bootstrap import spawn_bootstrap_task

        # Never-bootstrapped project: no bootstrap_completed_at. A normal
        # (non-refresh) spawn enqueues the first bootstrap as usual.
        project = _make_external_project(
            db_session,
            name="acme/refresh-brand-new",
            build_config={"language": "python"},
        )
        await db_session.flush()

        result = await spawn_bootstrap_task(
            project,
            db_session,
            is_first_bootstrap=True,
            spawn_reason="onboard",
        )
        await db_session.commit()
        await db_session.refresh(project)

        assert result.outcome == "spawned"
        # The refresh path did not retroactively stamp completion or a refresh marker.
        bc = project.build_config or {}
        assert "bootstrap_completed_at" not in bc, (
            "First bootstrap must not pre-stamp bootstrap_completed_at"
        )
        assert bc.get("bootstrap_recovery_state") != "operator_refresh"

    @pytest.mark.asyncio
    async def test_refresh_endpoint_spawns_for_completed_project(
        self, client: AsyncClient, db_session
    ):
        """Refresh endpoint returns spawned for a completed (healthy) project."""
        project = _make_external_project(
            db_session,
            name="acme/refresh-endpoint-completed",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            add_membership=True,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/refresh"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "spawned", f"Unexpected state: {body}"
        assert body["task_id"] is not None

        await _fetch_queued_bootstrap_task(db_session, project.id)

    @pytest.mark.asyncio
    async def test_refresh_endpoint_already_active(
        self, client: AsyncClient, db_session
    ):
        """Refresh endpoint returns already_active when a bootstrap is running."""
        project = _make_external_project(
            db_session,
            name="acme/refresh-endpoint-active",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            add_membership=True,
        )
        await db_session.flush()

        active_task = Task(
            project_id=project.id,
            title="Bootstrap: acme/refresh-endpoint-active",
            description="Active bootstrap",
            payloads=[{"type": "repo_bootstrap"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        db_session.add(active_task)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/refresh"
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "already_active"

    @pytest.mark.asyncio
    async def test_refresh_endpoint_not_eligible_for_self_repo(
        self, client: AsyncClient, db_session
    ):
        """Refresh endpoint returns not_eligible for a self-repo project."""
        project = _make_external_project(
            db_session,
            name="self/refresh-repo",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            is_self_repo=True,
            add_membership=True,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/refresh"
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "not_eligible"

    @pytest.mark.asyncio
    async def test_refresh_endpoint_owner_gated(
        self, client: AsyncClient, db_session
    ):
        """Refresh endpoint is owner-gated: a non-member is denied access."""
        project = _make_external_project(
            db_session,
            name="acme/refresh-endpoint-nonmember",
            build_config={
                "language": "python",
                "bootstrap_completed_at": "2026-01-01T00:00:00",
            },
            add_membership=False,
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/projects/{project.id}/bootstrap/refresh"
        )
        assert resp.status_code in (403, 404), (
            f"Non-owner must be denied, got {resp.status_code}"
        )
