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


class TestStrategistSpawnSequencedDeclaration:
    """spawn_task sequenced-spawn declaration (require_deps_landed_on /
    release_base_branch): a declared landing gate admits cross-base depends_on
    edges (the deterministic gate governs them instead of the _same_base_branch
    heuristic) and both keys are mirrored verbatim into payloads[0]."""

    async def _make_strategic_task(self, db_session, project, vm="agent-vm-seq-spawn"):
        task = Task(
            project_id=project.id,
            title="Strategic review for sequenced spawn",
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
    async def test_spawn_task_landing_gate_admits_cross_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A cross-base depends_on edge is ACCEPTED when the spawn declares
        require_deps_landed_on — the landing gate holds dispatch until the dep
        has landed on the named branch and release_base_branch re-cuts the base,
        so the edge is neither silently skipped nor dispatched against a stale
        base. Both declared keys must land verbatim in payloads[0]."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        dep = Task(
            project_id=project.id,
            title="Cross-base prerequisite (rung A)",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": "plan/rung-a"}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        integration = get_project_integration_branch(project)
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Rung B builds on rung A",
                    "prompt": "Sequenced cross-base spawn must be accepted.",
                    "depends_on": [str(dep.id)],
                    "require_deps_landed_on": integration,
                    "release_base_branch": integration,
                },
            },
        )
        assert resp.status_code == 200, (
            f"declared landing gate must admit the cross-base edge; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("depends_on") == [str(dep.id)], (
            f"spawn must echo the accepted edge; got {body!r}"
        )
        import uuid as _uuid

        spawned = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert spawned is not None
        payload = spawned.first_payload or {}
        assert payload.get("require_deps_landed_on") == integration, (
            f"landing gate must be mirrored into payloads[0]; got {payload!r}"
        )
        assert payload.get("release_base_branch") == integration, (
            f"release base must be mirrored into payloads[0]; got {payload!r}"
        )

    @pytest.mark.asyncio
    async def test_spawn_task_rejects_cross_base_depends_on_without_gate(
        self, client: AsyncClient, db_session
    ):
        """Declaring only release_base_branch (no landing gate) does NOT admit a
        cross-base edge: without require_deps_landed_on the dispatch loop still
        applies the heuristic path, so the ungated edge stays rejected."""
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
                    "prompt": "Ungated cross-base edge stays rejected.",
                    "depends_on": [str(dep.id)],
                    "release_base_branch": "release/v29",
                },
            },
        )
        assert resp.status_code == 400, (
            f"ungated cross-base depends_on must stay rejected; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Cross-base dependency" in msg

    @pytest.mark.asyncio
    async def test_spawn_task_sequenced_keys_must_be_branch_names(
        self, client: AsyncClient, db_session
    ):
        """Blank or non-string sequenced keys are rejected up front, and a
        reserved branch shape is rejected by the payload schema."""
        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        for bad in ("", "   ", 123):
            resp = await client.post(
                "/api/agent/strategic",
                headers={"x-agent-token": token},
                json={
                    "task_id": str(strategic.id),
                    "op": "spawn_task",
                    "data": {
                        "type": "create",
                        "title": "Must not spawn",
                        "prompt": "Invalid sequenced key.",
                        "require_deps_landed_on": bad,
                    },
                },
            )
            assert resp.status_code == 400, (
                f"require_deps_landed_on={bad!r} must be rejected; got "
                f"{resp.status_code}: {resp.text}"
            )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Must not spawn",
                    "prompt": "Reserved branch component.",
                    "require_deps_landed_on": "feat/..sneaky",
                },
            },
        )
        assert resp.status_code == 400, (
            f"reserved branch shape must be rejected by the payload schema; "
            f"got {resp.status_code}: {resp.text}"
        )
