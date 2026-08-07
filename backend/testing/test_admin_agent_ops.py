"""
Tests for admin_agent_ops.py — the strategist-facing /api/agent/strategic ops.

Focused on the spawn_task depends_on base-branch gate: a cross-base edge must be
rejected (the dispatch loop only waits on a dependency matching
``_same_base_branch``, so a cross-base edge would be silently skipped), while a
same-base edge stays accepted. Mirrors the operator-facing depends_on gate in
admin_task_mutations.py.
"""

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Project, ProjectMember, Task, TaskStatus

TEST_USER_ID = "u-admin-agent-ops"


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project"):
    """Create a project with the given user as an owner member."""
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    session.add(member)
    await session.commit()
    return project


class TestStrategistSpawnDependsOnBaseBranch:
    """spawn_task depends_on edges must reject cross-base-branch dependencies."""

    async def _make_strategic_task(self, db_session, project, vm="agent-vm-dep-base"):
        task = Task(
            project_id=project.id,
            title="Strategic review for dependency base gating",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id=vm,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        token = create_agent_token(
            str(task.id), vm, task_type="strategic_review"
        )
        return task, token

    @pytest.mark.asyncio
    async def test_spawn_task_rejects_cross_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A strategist depends_on edge across base branches is rejected with a
        clear error naming the dependency and both base branches — the same
        guard the operator-facing depends_on path enforces (a cross-base edge is
        silently skipped by the dispatch loop's _same_base_branch wait)."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        dep = Task(
            project_id=project.id,
            title="Cross-base prerequisite",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": "plan/other"}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Must not spawn",
                    "prompt": "Cross-base depends_on must be rejected.",
                    "depends_on": [str(dep.id)],
                },
            },
        )
        assert resp.status_code == 400, (
            f"cross-base depends_on must be rejected; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Cross-base dependency" in msg, f"error must name the guard; got {msg!r}"
        assert str(dep.id) in msg, f"error must name the dependency; got {msg!r}"
        assert "plan/other" in msg, f"error must name the dep base; got {msg!r}"
        integration = get_project_integration_branch(project)
        assert integration in msg, (
            f"error must name the spawn base '{integration}'; got {msg!r}"
        )

    @pytest.mark.asyncio
    async def test_spawn_task_accepts_same_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A strategist depends_on edge onto a sibling on the same integration
        base is still accepted — the guard must reject cross-base edges only,
        not every dependency."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        integration = get_project_integration_branch(project)
        dep = Task(
            project_id=project.id,
            title="Same-base prerequisite",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": integration}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Spawn on the integration base",
                    "prompt": "Same-base depends_on must be accepted.",
                    "depends_on": [str(dep.id)],
                },
            },
        )
        assert resp.status_code == 200, (
            f"same-base depends_on must be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("depends_on") == [str(dep.id)], (
            f"spawn must echo the accepted edge; got {body!r}"
        )
