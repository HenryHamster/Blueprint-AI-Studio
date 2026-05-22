"""Tests for warm-VM backlog reuse (efficiency direction d-91d8dede).

Covers the /api/agent/next-role backlog-claim branch and its module-level
eligibility/selection helpers:

  - claim path: an idle VM claims an eligible same-project QUEUED backlog task
    and the task is stamped warm (warm_start=True, warm_claim_origin=backlog_reuse)
  - no-double-claim: a task already grabbed (IN_PROGRESS/PROVISIONING) is never
    re-claimed; two idle VMs racing the same QUEUED task yield exactly one claim
  - project scoping: a VM never claims another project's backlog task
  - doomed-VM guard: a teardown-scheduled VM never wins a backlog claim
  - config gate: with the flag OFF behavior is unchanged (no backlog claim)
  - eligibility: dependency-bearing and file-conflicting tasks are skipped
  - telemetry: a backlog-reused task lifts vm_reuse_rate and adds no cold-start
    sample to the efficiency snapshot
"""

import time

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token, verify_agent_jwt
from app.efficiency_snapshot import (
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    compute_current_axes,
)
from app.models import Project, Task, TaskStatus
from app.warm_backlog_reuse import (
    has_eligible_backlog_task,
    select_backlog_reuse_candidate,
)


async def _make_project(db_session, name="Backlog Reuse"):
    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_backlog_task(db_session, project_id, **overrides):
    defaults = dict(
        project_id=project_id,
        title="Queued backlog work",
        payloads=[{"type": "create", "prompt": "do X", "base_branch": "automain"}],
        status=TaskStatus.QUEUED,
        context={"scope": "small"},
    )
    defaults.update(overrides)
    task = Task(**defaults)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _patch_broadcast(mocker):
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )


class _FlagGuard:
    """Snapshot/restore runtime_config flags + the first-refusal registry."""

    def __init__(self, backlog=False, continuity=False):
        self.backlog = backlog
        self.continuity = continuity

    def __enter__(self):
        import app.shared_state as ss
        from app.shared_state import runtime_config as rc

        self._ss = ss
        self._rc = rc
        self._prior_backlog = rc.get("warm_backlog_reuse_enabled")
        self._prior_continuity = rc.get("warm_continuity_enabled")
        self._prior_refusal = dict(ss.warm_backlog_first_refusal)
        rc["warm_backlog_reuse_enabled"] = self.backlog
        rc["warm_continuity_enabled"] = self.continuity
        return self

    def __exit__(self, *exc):
        for key, prior in (
            ("warm_backlog_reuse_enabled", self._prior_backlog),
            ("warm_continuity_enabled", self._prior_continuity),
        ):
            if prior is None:
                self._rc.pop(key, None)
            else:
                self._rc[key] = prior
        self._ss.warm_backlog_first_refusal.clear()
        self._ss.warm_backlog_first_refusal.update(self._prior_refusal)
        return False


@pytest.mark.asyncio
async def test_backlog_reuse_claims_same_project_task(
    client: AsyncClient, db_session, mocker
):
    """An idle VM claims an eligible QUEUED backlog task in its project and the
    task is stamped warm with warm_claim_origin='backlog_reuse'."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-backlog-001"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(backlog.id)
        assert body["warm_start"] is True
        new_claims = verify_agent_jwt(body["agent_token"])
        assert new_claims["sub"] == str(backlog.id)
        assert new_claims["project_id"] == str(project.id)

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.IN_PROGRESS
        assert backlog.vm_instance_id == vm_id
        timing = (backlog.context or {}).get("_timing", {})
        assert timing.get("warm_start") is True
        assert timing.get("warm_claim_origin") == "backlog_reuse"
        # Warm claim must NOT stamp a cold-start sample.
        assert backlog.vm_cold_start_ms is None


@pytest.mark.asyncio
async def test_backlog_reuse_config_gate_off_is_unchanged(
    client: AsyncClient, db_session, mocker
):
    """With both warm flags OFF, an idle VM with no chain pin gets 204 and the
    backlog task is left untouched — behavior identical to pre-feature."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=False, continuity=False):
        project = await _make_project(db_session, name="Gate Off")
        vm_id = "agent-vm-gate-off"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.QUEUED
        assert backlog.vm_instance_id is None


@pytest.mark.asyncio
async def test_backlog_reuse_project_scoping(client: AsyncClient, db_session, mocker):
    """A VM never claims a backlog task belonging to a different project."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project_a = await _make_project(db_session, name="Project A")
        project_b = await _make_project(db_session, name="Project B")
        vm_id = "agent-vm-scoping"
        prior = await _make_backlog_task(
            db_session, project_a.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # Only project B has backlog work — the VM (scoped to A) must not take it.
        b_task = await _make_backlog_task(db_session, project_b.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project_a.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204

        db_session.expire(b_task)
        await db_session.refresh(b_task)
        assert b_task.status == TaskStatus.QUEUED
        assert b_task.vm_instance_id is None


@pytest.mark.asyncio
async def test_backlog_reuse_doomed_vm_guard(client: AsyncClient, db_session, mocker):
    """A VM scheduled for orchestrator-initiated teardown never wins a backlog
    claim — the doomed-VM guard returns 204 before the backlog branch runs."""
    _patch_broadcast(mocker)
    import app.main as main

    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Doomed")
        vm_id = "agent-vm-doomed-backlog"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        main._orchestrator_deleted_vms[vm_id] = (time.time(), "test_doomed")
        try:
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 204
        finally:
            main._orchestrator_deleted_vms.pop(vm_id, None)

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_backlog_reuse_no_double_claim_after_grabbed(
    client: AsyncClient, db_session, mocker
):
    """Once the orchestrator has grabbed a task (status != QUEUED), the backlog
    branch will not double-claim it — the VM gets 204."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="No Double")
        vm_id = "agent-vm-no-double"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # Simulate the orchestrator already cold-dispatching it.
        await _make_backlog_task(db_session, project.id, status=TaskStatus.PROVISIONING)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_backlog_reuse_two_vms_single_claim(
    client: AsyncClient, db_session, mocker
):
    """Two idle VMs in the same project, one QUEUED backlog task: exactly one
    claims it, the other gets 204 (the QUEUED->IN_PROGRESS flip is the race
    arbiter)."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Two VMs")
        backlog = await _make_backlog_task(db_session, project.id)

        results = []
        for vm_id in ("agent-vm-race-a", "agent-vm-race-b"):
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            results.append(resp.status_code)

        assert sorted(results) == [200, 204]
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_eligibility_skips_dependency_and_file_conflict(db_session):
    """select_backlog_reuse_candidate skips tasks with dependencies and tasks
    whose files overlap an IN_PROGRESS task in the same project."""
    project = await _make_project(db_session, name="Eligibility")
    vm_id = "agent-vm-eligibility"

    # IN_PROGRESS task editing src/app.py — file-conflict source.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["src/app.py"]},
    )
    # Candidate that conflicts on src/app.py -> ineligible.
    await _make_backlog_task(
        db_session, project.id, context={"related_files": ["src/app.py"]}
    )
    # Candidate with an unmet dependency -> ineligible.
    await _make_backlog_task(
        db_session, project.id, depends_on=["00000000-0000-0000-0000-000000000123"]
    )

    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None

    # Add a clean, dependency-free, non-conflicting candidate -> eligible.
    clean = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == clean.id


@pytest.mark.asyncio
async def test_eligibility_skips_other_vm_pin(db_session):
    """A task pinned to a different VM (chain pin) is not claimable as backlog
    by this VM."""
    project = await _make_project(db_session, name="Other Pin")
    vm_id = "agent-vm-mine"
    await _make_backlog_task(
        db_session,
        project.id,
        context={"warm_target_vm_id": "agent-vm-someone-else"},
    )
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None


@pytest.mark.asyncio
async def test_backlog_reused_task_lifts_efficiency_axes(
    client: AsyncClient, db_session, mocker
):
    """A backlog-reused task contributes to vm_reuse_rate (warm_start=True) and
    adds no cold-start sample (vm_cold_start_ms stays NULL), so the snapshot
    computation needs no change."""
    _patch_broadcast(mocker)
    from datetime import timedelta

    from testing.factories import _utcnow

    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Efficiency")
        vm_id = "agent-vm-eff"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # A genuinely cold, merged task to populate the cold-start sample.
        now = _utcnow()
        cold = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True, "_timing": {"warm_start": False}},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            vm_cold_start_ms=12000,
        )
        # The backlog task to be warm-claimed.
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200, resp.text

        # Mark the warm-claimed task merged so it participates as a warm sample.
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        ctx = dict(backlog.context or {})
        ctx["pr_merged"] = True
        backlog.context = ctx
        backlog.status = TaskStatus.COMPLETED
        db_session.add(backlog)
        await db_session.commit()

        axes = await compute_current_axes(db_session, project.id)
        # cold (warm_start False) + backlog (warm_start True) participate -> 0.5.
        assert axes[AXIS_VM_REUSE_RATE] == 0.5
        # Only the cold task supplies a cold-start sample; the warm task is NULL.
        assert axes[AXIS_VM_COLD_START_P50_MS] == 12000.0
        assert cold.vm_cold_start_ms == 12000
        assert backlog.vm_cold_start_ms is None
