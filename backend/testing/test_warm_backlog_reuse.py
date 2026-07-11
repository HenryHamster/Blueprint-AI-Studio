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

from app.auth import create_agent_token, create_vm_scoped_token, verify_agent_jwt
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
    defaults = {
        "project_id": project_id,
        "title": "Queued backlog work",
        "payloads": [{"type": "create", "prompt": "do X", "base_branch": "automain"}],
        "status": TaskStatus.QUEUED,
        "context": {"scope": "small"},
    }
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

    def __init__(self, backlog=False, continuity=False, retarget=False):
        self.backlog = backlog
        self.continuity = continuity
        self.retarget = retarget

    def __enter__(self):
        import app.shared_state as ss
        from app.shared_state import runtime_config as rc

        self._ss = ss
        self._rc = rc
        self._prior_backlog = rc.get("warm_backlog_reuse_enabled")
        self._prior_continuity = rc.get("warm_continuity_enabled")
        self._prior_retarget = rc.get("warm_retarget_enabled")
        self._prior_refusal = dict(ss.warm_backlog_first_refusal)
        rc["warm_backlog_reuse_enabled"] = self.backlog
        rc["warm_continuity_enabled"] = self.continuity
        rc["warm_retarget_enabled"] = self.retarget
        return self

    def __exit__(self, *exc):
        for key, prior in (
            ("warm_backlog_reuse_enabled", self._prior_backlog),
            ("warm_continuity_enabled", self._prior_continuity),
            ("warm_retarget_enabled", self._prior_retarget),
        ):
            if prior is None:
                self._rc.pop(key, None)
            else:
                self._rc[key] = prior
        self._ss.warm_backlog_first_refusal.clear()
        self._ss.warm_backlog_first_refusal.update(self._prior_refusal)
        # Reset circuit-breaker state so tests don't bleed into each other.
        self._ss.warm_backlog_reuse_failure_events.clear()
        self._ss.warm_backlog_reuse_suppressed_until = None
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
        # Both context flag and column must be synchronized.
        assert backlog.vm_warm_start is True
        # Warm claim must NOT stamp a cold-start sample.
        assert backlog.vm_cold_start_ms is None
        assert "claim_latency_secs" not in (backlog.context or {})


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
async def test_pool_vm_scoped_token_excluded_from_backlog_reuse(
    client: AsyncClient, db_session, mocker
):
    """An IDLE pool VM (VM-scoped token, no warm_pool_pin task) must NOT claim a
    backlog task via the credential-agnostic backlog-reuse branch — that branch
    bypasses the pool's account/provider/model lease gate. It gets 204 and the
    backlog task stays QUEUED; only the orchestrator's strict pool lease may hand
    a pool VM work."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Pool Token Excluded")
        vm_id = "agent-vm-pool-token"
        backlog = await _make_backlog_task(db_session, project.id)

        # VM-scoped (taskless) token — what a freshly-booted pool VM carries.
        token = create_vm_scoped_token(vm_id, str(project.id))
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204, resp.text

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
async def test_eligibility_excludes_compute_experiment(db_session):
    """A QUEUED compute_experiment task is never returned by the warm-reuse
    eligibility scan.

    compute_experiment has a dedicated runner-aware direct-dispatch path that
    selects an eligible GPU runner before launch.  Warm backlog reuse never
    consults runner eligibility, so claiming it warm would either silently route
    the GPU job to the cloud backend or raise ComputeLaunchPreconditionFailed
    when require_local_runner=true.
    """
    project = await _make_project(db_session, name="ComputeExperiment Exclusion")
    vm_id = "agent-vm-compute-excl"

    # A QUEUED compute_experiment task — must never be warm-claimed.
    await _make_backlog_task(
        db_session,
        project.id,
        payloads=[{"type": "compute_experiment", "prompt": "run GPU experiment"}],
    )

    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None

    # Add a regular create task in the same project — it IS eligible.
    create_task = await _make_backlog_task(db_session, project.id)
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == create_task.id


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
    # Candidate that conflicts on src/app.py (large scope: any overlap defers) -> ineligible.
    await _make_backlog_task(
        db_session,
        project.id,
        context={"scope": "large", "related_files": ["src/app.py"]},
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
async def test_eligibility_read_only_conflict_semantics_match_dispatch(db_session):
    """Warm pickup must mirror filter_file_conflicts' read-only isolation:

      - an IN_PROGRESS REVIEW (inherits the PR's full related_files but pushes no
        diff) must NOT block a warm candidate that overlaps its files;
      - an IN_PROGRESS WRITE task on the same files STILL blocks; and
      - a read-only CANDIDATE (e.g. a review) is exempt from the overlap defer
        even against an in-flight write (it produces no diff to collide).
    """
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="ReadOnly Conflict")
    vm_id = "agent-vm-readonly-conflict"

    # 1) IN_PROGRESS review editing src/app.py — must NOT count as a claim.
    review_ip = await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "review"}],
        context={"related_files": ["src/app.py"]},
    )
    write_candidate = await _make_backlog_task(
        db_session,
        project.id,
        payloads=[{"type": "fix"}],
        # large scope: any overlap defers (matches cold dispatch behavior)
        context={"scope": "large", "related_files": ["src/app.py"]},
    )
    # The review contributes nothing to the in-progress claim set, so the
    # overlapping write candidate is eligible.
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == write_candidate.id

    # 2) Flip the in-flight task to a WRITE type: now the same overlap DOES block.
    review_ip.payloads = [{"type": "fix"}]
    db_session.add(review_ip)
    await db_session.commit()
    # Re-fetch eligibility: the only candidate (large scope) now conflicts with
    # an in-flight write on src/app.py, so it is ineligible.
    eligible = await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
    assert write_candidate.id not in eligible

    # 3) A read-only CANDIDATE overlapping the in-flight write is still exempt
    #    from the overlap defer (it pushes no diff), so it is eligible.
    review_candidate = await _make_backlog_task(
        db_session,
        project.id,
        payloads=[{"type": "review"}],
        context={"related_files": ["src/app.py"]},
    )
    eligible = await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
    assert review_candidate.id in eligible
    assert write_candidate.id not in eligible


@pytest.mark.asyncio
async def test_warm_cross_parent_large_scope_hotspot_not_skipped(db_session):
    """A large-scope candidate sharing ONLY a hotspot file with cross-parent
    in-progress tasks is eligible for warm reuse (hotspot exemption parity
    with filter_file_conflicts cold path)."""
    project = await _make_project(db_session, name="Hotspot Cross-Parent")
    vm_id = "agent-vm-hotspot-cp"

    # Create real parent tasks (required by FK constraint on parent_task_id).
    parent_a = await _make_backlog_task(
        db_session, project.id, status=TaskStatus.EVALUATING
    )
    parent_b = await _make_backlog_task(
        db_session, project.id, status=TaskStatus.EVALUATING
    )
    parent_c = await _make_backlog_task(
        db_session, project.id, status=TaskStatus.EVALUATING
    )

    # Two IN_PROGRESS tasks under different parents touching schemas.py.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/schemas.py"]},
        parent_task_id=parent_b.id,
    )
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/schemas.py"]},
        parent_task_id=parent_c.id,
    )
    # Candidate under a different parent — schemas.py overlap is hotspot-only.
    candidate = await _make_backlog_task(
        db_session,
        project.id,
        payloads=[{"type": "create", "prompt": "fix schemas", "base_branch": "automain"}],
        context={"scope": "large", "related_files": ["backend/app/schemas.py"]},
        parent_task_id=parent_a.id,
    )
    # schemas.py now appears in 3 tasks → hotspot → cross-parent candidate eligible.
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == candidate.id


@pytest.mark.asyncio
async def test_warm_same_parent_hotspot_still_skipped(db_session):
    """Same-parent tasks still defer on a hotspot file — the exemption only
    applies when tasks are confidently cross-parent."""
    project = await _make_project(db_session, name="Hotspot Same-Parent")
    vm_id = "agent-vm-hotspot-sp"

    # Create real parent tasks (required by FK constraint on parent_task_id).
    shared_parent = await _make_backlog_task(
        db_session, project.id, status=TaskStatus.EVALUATING
    )
    other_parent = await _make_backlog_task(
        db_session, project.id, status=TaskStatus.EVALUATING
    )

    # One IN_PROGRESS task with the SAME parent as the candidate.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/schemas.py"]},
        parent_task_id=shared_parent.id,
    )
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/schemas.py"]},
        parent_task_id=other_parent.id,
    )
    # Candidate shares parent with ip1 → NOT confidently cross-parent → skipped.
    await _make_backlog_task(
        db_session,
        project.id,
        payloads=[{"type": "create", "prompt": "fix schemas", "base_branch": "automain"}],
        context={"scope": "large", "related_files": ["backend/app/schemas.py"]},
        parent_task_id=shared_parent.id,
    )
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None


@pytest.mark.asyncio
async def test_paused_project_has_no_warm_claimable_backlog(db_session):
    """A paused project's QUEUED backlog must not be warm-claimable on the
    idle-VM next-role reuse path; resuming restores eligibility."""
    project = await _make_project(db_session, name="Paused Backlog")
    vm_id = "agent-vm-paused-backlog"

    clean = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )
    # Baseline: eligible while the project is running.
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True

    # Pause: the same backlog task is now ineligible on every reuse entry point.
    project.paused = True
    db_session.add(project)
    await db_session.commit()
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert (
        await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None
    )

    # Resume: eligibility is restored with no other change.
    project.paused = False
    db_session.add(project)
    await db_session.commit()
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == clean.id


@pytest.mark.asyncio
async def test_final_claim_rechecks_project_pause_with_stale_eligible_ids(
    db_session, mocker
):
    """The final warm-claim lock re-checks Project.paused even if eligibility
    was computed before the project was paused."""
    project = await _make_project(db_session, name="Paused Claim Race")
    vm_id = "agent-vm-paused-claim-race"
    task = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )

    project.paused = True
    db_session.add(project)
    await db_session.commit()

    mocker.patch(
        "app.warm_backlog_reuse._eligible_backlog_ids",
        new_callable=mocker.AsyncMock,
        return_value=[task.id],
    )

    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_dependency_ready_task_is_eligible_with_marker(db_session):
    """A dependency-bearing task whose deps the orchestrator already resolved
    (``context['_deps_satisfied']`` stamped) is warm-claimable, while an
    otherwise-identical task WITHOUT the marker stays ineligible and still
    fires the depends_on skip funnel."""
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="Dep Ready")
    vm_id = "agent-vm-dep-ready"

    # Dependency-bearing task, deps NOT yet resolved (no marker) -> skipped.
    await _make_backlog_task(
        db_session,
        project.id,
        depends_on=["00000000-0000-0000-0000-000000000123"],
        context={"scope": "small"},
    )
    wbr._scan_skipped_depends_on_events.clear()
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None
    # The unsatisfied-dep skip funnel still fires for the unmarked task.
    assert len(wbr._scan_skipped_depends_on_events) >= 1

    # Same shape but deps resolved + stamped by the orchestrator -> eligible.
    ready = await _make_backlog_task(
        db_session,
        project.id,
        depends_on=["00000000-0000-0000-0000-000000000123"],
        context={"scope": "small", "_deps_satisfied": True},
    )
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == ready.id


@pytest.mark.asyncio
async def test_dep_ready_task_still_respects_other_exclusions(db_session):
    """The ``_deps_satisfied`` marker only lifts the dependency gate — a
    dep-ready task pinned to a different VM or conflicting on files stays
    ineligible exactly as a dependency-free task would."""
    project = await _make_project(db_session, name="Dep Ready Exclusions")
    vm_id = "agent-vm-mine"

    # Dep-ready but pinned to another VM -> still excluded.
    await _make_backlog_task(
        db_session,
        project.id,
        depends_on=["00000000-0000-0000-0000-000000000123"],
        context={"_deps_satisfied": True, "warm_target_vm_id": "agent-vm-other"},
    )
    # Dep-ready but file-conflicting with an IN_PROGRESS task -> still excluded
    # (large scope: any single-file overlap defers).
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["src/app.py"]},
    )
    await _make_backlog_task(
        db_session,
        project.id,
        depends_on=["00000000-0000-0000-0000-000000000123"],
        context={"_deps_satisfied": True, "scope": "large", "related_files": ["src/app.py"]},
    )
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None


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


# ---------------------------------------------------------------------------
# Provider-aware backlog reuse (a reused VM is locked to its boot manifest's
# provider/account/model — never warm-claim a task pinned/recorded elsewhere)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_mismatch_task_is_ineligible(db_session):
    """A task recorded to a different provider than the reusing VM's boot
    account is excluded and fires the provider-mismatch scan funnel; a task on
    the matching provider remains claimable."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Provider Mismatch")
        vm_id = "agent-vm-provider"
        vm_boot = {"provider": "anthropic"}

        # Recorded to gemini -> incompatible with an anthropic-booted VM.
        await _make_backlog_task(db_session, project.id, provider="gemini")
        # Explicitly pinned to gemini via context -> also incompatible.
        await _make_backlog_task(
            db_session, project.id, context={"provider_preference": ["gemini"]}
        )

        assert (
            await has_eligible_backlog_task(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is False
        )
        assert (
            await select_backlog_reuse_candidate(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is None
        )
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_provider_mismatch_skip"] >= 1

        # A matching-provider candidate is claimable on the same VM.
        match = await _make_backlog_task(db_session, project.id, provider="anthropic")
        picked = await select_backlog_reuse_candidate(
            db_session, project.id, vm_id, vm_boot=vm_boot
        )
        assert picked is not None and picked.id == match.id
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_single_provider_project_still_warm_reuses(db_session):
    """REGRESSION: the provider guard is a no-op for single-provider projects.

    Even with ``vm_boot`` supplied, a task carrying no divergent constraint
    (no recorded provider, or one matching the boot account) stays claimable so
    single-provider projects keep warm-reusing exactly as before."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Single Provider")
        vm_id = "agent-vm-single"
        vm_boot = {"provider": "anthropic", "account_id": "acct-1", "model": "opus"}

        # Fresh task with no recorded/pinned provider — fungible.
        await _make_backlog_task(db_session, project.id)
        # Task recorded to the same provider/account/model as the boot manifest.
        await _make_backlog_task(
            db_session,
            project.id,
            provider="anthropic",
            account_id="acct-1",
            model="opus",
        )

        assert (
            await has_eligible_backlog_task(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is True
        )
        assert (
            await select_backlog_reuse_candidate(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is not None
        )
        # No task was provider-skipped — the guard stayed inert.
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_provider_mismatch_skip"] == 0
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_provider_guard_noop_when_vm_boot_absent(db_session):
    """Omitting ``vm_boot`` disables the guard entirely — a task recorded to a
    different provider is still eligible (pre-feature behavior preserved)."""
    project = await _make_project(db_session, name="No Boot Identity")
    vm_id = "agent-vm-noboot"
    await _make_backlog_task(db_session, project.id, provider="gemini")
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    assert (
        await select_backlog_reuse_candidate(db_session, project.id, vm_id) is not None
    )


@pytest.mark.asyncio
async def test_provider_match_with_divergent_account_is_ineligible(db_session):
    """Same provider but a different recorded account is still incompatible —
    the boot manifest freezes the account, not just the provider."""
    project = await _make_project(db_session, name="Account Mismatch")
    vm_id = "agent-vm-acct"
    vm_boot = {"provider": "anthropic", "account_id": "acct-boot"}
    await _make_backlog_task(
        db_session, project.id, provider="anthropic", account_id="acct-other"
    )
    assert (
        await select_backlog_reuse_candidate(
            db_session, project.id, vm_id, vm_boot=vm_boot
        )
        is None
    )


@pytest.mark.asyncio
async def test_retarget_disabled_keeps_boot_compat_gate(db_session):
    """REGRESSION (flag off): with warm_retarget_enabled OFF the boot-compat gate
    is byte-for-byte unchanged — an account/provider-divergent task stays
    ineligible and cold-dispatches as before."""
    with _FlagGuard(backlog=True, retarget=False):
        project = await _make_project(db_session, name="Retarget Off")
        vm_id = "agent-vm-retarget-off"
        vm_boot = {"provider": "anthropic", "account_id": "acct-boot", "model": "opus"}
        await _make_backlog_task(
            db_session, project.id, provider="gemini", account_id="acct-other"
        )
        assert (
            await has_eligible_backlog_task(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is False
        )
        assert (
            await select_backlog_reuse_candidate(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is None
        )


@pytest.mark.asyncio
async def test_retarget_enabled_relaxes_boot_compat_gate(db_session):
    """Flag on: warm_retarget_enabled makes account/provider/model divergence
    re-targetable, so a same-project task that diverges from the VM's boot
    manifest becomes claimable (the finalizer re-materializes its credentials)."""
    with _FlagGuard(backlog=True, retarget=True):
        project = await _make_project(db_session, name="Retarget On")
        vm_id = "agent-vm-retarget-on"
        vm_boot = {"provider": "anthropic", "account_id": "acct-boot", "model": "opus"}
        divergent = await _make_backlog_task(
            db_session,
            project.id,
            provider="gemini",
            account_id="acct-other",
            model="gemini-2.5-pro",
        )
        assert (
            await has_eligible_backlog_task(
                db_session, project.id, vm_id, vm_boot=vm_boot
            )
            is True
        )
        picked = await select_backlog_reuse_candidate(
            db_session, project.id, vm_id, vm_boot=vm_boot
        )
        assert picked is not None and picked.id == divergent.id


def _pool_account(account_id, user_id="test-user-123", provider="anthropic"):
    """Minimal in-memory pool account dict select_max_plan_account treats as
    healthy (runtime-ready OAuth, enabled, no disabled models, zero load)."""
    return {
        "id": account_id,
        "user_id": user_id,
        "name": f"acct-{account_id}",
        "provider": provider,
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-retarget-token",
        "runtime_secret": "sk-ant-retarget-token",
        "runtime_ready": True,
        "claude_config_json": None,
        "provider_metadata": {},
        "enabled": True,
        "active_tasks": 0,
        "daily_task_count": 0,
        "disabled_models": {},
    }


def _seed_pool(mocker, accounts, user_id="test-user-123"):
    """Patch the in-memory account pool with ``accounts`` for ``user_id``."""
    import app.max_plan_pool as pool

    mocker.patch.object(pool, "_max_plan_pools", {user_id: list(accounts)})
    mocker.patch.object(pool, "_account_by_id", {a["id"]: a for a in accounts})


@pytest.mark.asyncio
async def test_retarget_claim_emits_secret_manifest_when_account_differs(
    client: AsyncClient, db_session, mocker
):
    """Flag on + resolved account != VM boot account: the claim response carries
    secret_manifest for the resolved account, the task is stamped/acquired to
    that account, and the bootstrap token is NOT duplicated into the manifest."""
    _patch_broadcast(mocker)
    retarget_acct = _pool_account("acct-retarget")
    _seed_pool(mocker, [retarget_acct])
    with _FlagGuard(backlog=True, retarget=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-retarget-diff"
        prior = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            vm_instance_id=vm_id,
            account_id="acct-boot",
            provider="anthropic",
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
        manifest = body.get("secret_manifest")
        assert manifest is not None, "expected secret_manifest for divergent account"
        assert manifest["metadata"]["provider"] == "anthropic"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-retarget-token"
        # The authoritative JWT is the rotated agent_token, not the manifest.
        assert "AGENT_SECRET_TOKEN" not in manifest["env"]

        # Slot acquired against the resolved account and durably stamped.
        assert retarget_acct["active_tasks"] == 1
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert (backlog.context or {}).get("llm_account_id") == "acct-retarget"


@pytest.mark.asyncio
async def test_retarget_claim_omits_manifest_when_account_matches(
    client: AsyncClient, db_session, mocker
):
    """Flag on but resolved account == VM boot account: response stays
    byte-identical (no secret_manifest), while the slot is still acquired and
    stamped so accounting follows the account the task runs on."""
    _patch_broadcast(mocker)
    boot_acct = _pool_account("acct-boot")
    _seed_pool(mocker, [boot_acct])
    with _FlagGuard(backlog=True, retarget=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-retarget-match"
        prior = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            vm_instance_id=vm_id,
            account_id="acct-boot",
            provider="anthropic",
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
        assert "secret_manifest" not in body
        assert boot_acct["active_tasks"] == 1
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert (backlog.context or {}).get("llm_account_id") == "acct-boot"


@pytest.mark.asyncio
async def test_retarget_releases_slot_on_finalize_failure(
    client: AsyncClient, db_session, mocker
):
    """If finalization fails after the re-target slot was acquired, the slot is
    released (acquire/release balanced) and the task is rolled back to QUEUED."""
    _patch_broadcast(mocker)
    retarget_acct = _pool_account("acct-retarget-fail")
    _seed_pool(mocker, [retarget_acct])
    mocker.patch(
        "app.routers.agent_warm_continuity.create_agent_token",
        side_effect=RuntimeError("mint boom"),
    )
    with _FlagGuard(backlog=True, retarget=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-retarget-fail"
        prior = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            vm_instance_id=vm_id,
            account_id="acct-boot",
            provider="anthropic",
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
        assert resp.status_code == 500
        # Acquired slot was released — net zero.
        assert retarget_acct["active_tasks"] == 0
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_retarget_scopes_pool_to_launcher_not_project_owner(
    client: AsyncClient, db_session, mocker
):
    """A user-launched backlog task (launcher != project owner) must re-target
    against the LAUNCHER's account pool, mirroring cold dispatch (the launcher
    pays). Resolving against the project owner would mis-attribute cost and, when
    the owner's pool has no match, strand the task on the VM's boot creds."""
    _patch_broadcast(mocker)
    # Pool belongs to the LAUNCHER, not the project owner ("test-user-123").
    launcher_acct = _pool_account("acct-launcher", user_id="launcher-user-999")
    _seed_pool(mocker, [launcher_acct], user_id="launcher-user-999")
    with _FlagGuard(backlog=True, retarget=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-retarget-launcher"
        prior = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            vm_instance_id=vm_id,
            account_id="acct-boot",
            provider="anthropic",
        )
        backlog = await _make_backlog_task(
            db_session, project.id, launched_by_user_id="launcher-user-999"
        )

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
        manifest = body.get("secret_manifest")
        assert manifest is not None, "expected manifest from launcher's pool account"
        # Slot acquired against the launcher's account and stamped onto the task.
        assert launcher_acct["active_tasks"] == 1
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert (backlog.context or {}).get("llm_account_id") == "acct-launcher"


@pytest.mark.asyncio
async def test_in_progress_scan_computed_once_when_reused(db_session, mocker):
    """A caller pairing the eligibility gate with the claim can compute the
    project's IN_PROGRESS file set once and pass it to both, so the file-conflict
    scan runs a single time instead of once per call."""
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="Scan Reuse")
    vm_id = "agent-vm-scan"
    # One IN_PROGRESS task (file-conflict source) + one clean candidate.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["src/app.py"]},
    )
    clean = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )

    # Compute the IN_PROGRESS file set once, then reuse it across both calls.
    in_progress_files = await wbr._project_in_progress_files(db_session, project.id)
    spy = mocker.spy(wbr, "_project_in_progress_files")

    assert (
        await has_eligible_backlog_task(
            db_session, project.id, vm_id, in_progress_files=in_progress_files
        )
        is True
    )
    picked = await select_backlog_reuse_candidate(
        db_session, project.id, vm_id, in_progress_files=in_progress_files
    )
    assert picked is not None and picked.id == clean.id
    # Neither call re-scanned IN_PROGRESS — the precomputed set was reused.
    assert spy.call_count == 0


@pytest.mark.asyncio
async def test_in_progress_scan_runs_when_not_provided(db_session, mocker):
    """When no precomputed set is passed, the scan still runs (lazy default),
    preserving the standalone behavior of each helper."""
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="Scan Lazy")
    vm_id = "agent-vm-lazy"
    await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )
    spy = mocker.spy(wbr, "_project_in_progress_files")
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    assert spy.call_count == 1


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


# ---------------------------------------------------------------------------
# Circuit-breaker tests
# ---------------------------------------------------------------------------


def test_backlog_reuse_circuit_breaker_suppresses_after_threshold():
    """Injecting >= threshold failures trips the breaker and suppresses reuse."""
    import app.shared_state as ss

    ss.warm_backlog_reuse_failure_events.clear()
    ss.warm_backlog_reuse_suppressed_until = None
    threshold = ss.runtime_config.get("warm_backlog_reuse_failure_rate_threshold", 5)
    try:
        for _ in range(threshold):
            ss.record_backlog_reuse_failure("task_failed")
        assert ss.count_recent_backlog_reuse_failures() >= threshold
        assert ss.is_warm_backlog_reuse_suppressed() is True
        assert ss.warm_backlog_reuse_suppressed_until is not None
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


def test_backlog_reuse_circuit_breaker_clears_after_cooldown():
    """With an expired suppression deadline the breaker clears on the next check."""
    from datetime import timedelta

    import app.shared_state as ss
    from app.time_utils import _utcnow

    ss.warm_backlog_reuse_failure_events.clear()
    # Set suppression deadline in the past so the breaker is "expired".
    ss.warm_backlog_reuse_suppressed_until = _utcnow() - timedelta(seconds=1)
    try:
        # No recent failures — below threshold — so breaker should clear.
        result = ss.is_warm_backlog_reuse_suppressed()
        assert result is False
        assert ss.warm_backlog_reuse_suppressed_until is None
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


def test_backlog_reuse_finalize_error_records_failure():
    """record_backlog_reuse_failure increments the event count (used from finalize error path)."""
    import app.shared_state as ss

    ss.warm_backlog_reuse_failure_events.clear()
    ss.warm_backlog_reuse_suppressed_until = None
    try:
        assert ss.count_recent_backlog_reuse_failures() == 0
        ss.record_backlog_reuse_failure("finalize_error")
        assert ss.count_recent_backlog_reuse_failures() == 1
        ss.record_backlog_reuse_failure("task_failed")
        assert ss.count_recent_backlog_reuse_failures() == 2
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


# ---------------------------------------------------------------------------
# Funnel observability counter tests
# ---------------------------------------------------------------------------


def _clear_funnel_events(wbr):
    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()
    wbr._scan_skipped_depends_on_events.clear()
    wbr._scan_skipped_pinned_vm_events.clear()
    wbr._scan_skipped_file_conflict_events.clear()
    wbr._scan_skipped_provider_mismatch_events.clear()
    wbr._scan_saturated_events.clear()
    wbr._completion_dependents_marked_events.clear()


def test_funnel_counters_and_snapshot():
    """Each record_* function increments its stage counter; get_reuse_funnel_snapshot
    returns accurate per-stage and per-reason totals."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        wbr.record_vm_with_eligible_backlog()
        wbr.record_vm_with_eligible_backlog()
        wbr.record_teardown_defer()
        wbr.record_next_role_poll_during_defer()
        wbr.record_warm_claim_succeeded()
        wbr.record_claim_skipped("no_eligible")
        wbr.record_claim_skipped("lost_claim_race")
        wbr.record_claim_skipped("no_eligible")

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["window_hours"] == 24
        assert snap["completed_vms_with_eligible_backlog"] == 2
        assert snap["teardown_defers_scheduled"] == 1
        assert snap["next_role_polls_during_defer"] == 1
        assert snap["warm_claims_succeeded"] == 1
        assert snap["claim_skipped_total"] == 3
        assert snap["claim_skipped_by_reason"]["no_eligible"] == 2
        assert snap["claim_skipped_by_reason"]["lost_claim_race"] == 1
    finally:
        _clear_funnel_events(wbr)


def test_funnel_completion_dependents_marked():
    """record_completion_dependents_marked sums the dependents proactively marked
    by completion-side triggers within the window; non-positive counts no-op."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        wbr.record_completion_dependents_marked(2)
        wbr.record_completion_dependents_marked(1)
        # Non-positive counts must not register an event.
        wbr.record_completion_dependents_marked(0)
        wbr.record_completion_dependents_marked(-3)

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["completion_dependents_marked"] == 3
        assert len(wbr._completion_dependents_marked_events) == 2
    finally:
        _clear_funnel_events(wbr)


def test_funnel_claim_skipped_circuit_breaker():
    """record_claim_skipped('circuit_breaker_tripped') is tracked in the snapshot."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        wbr.record_claim_skipped("circuit_breaker_tripped")
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["claim_skipped_by_reason"].get("circuit_breaker_tripped") == 1
        assert snap["claim_skipped_total"] == 1
    finally:
        _clear_funnel_events(wbr)


def _clear_all_dropout_events(wbr):
    _clear_funnel_events(wbr)
    wbr._grace_idle_release_events.clear()
    wbr._grace_ceiling_release_events.clear()
    wbr._grace_window_extension_events.clear()


def test_dominant_dropout_names_clear_leader():
    """A reason that meets the volume/share/runner-up gate is named dominant,
    with its share of the non-converting population; below the gate it is None."""
    import app.warm_backlog_reuse as wbr

    _clear_all_dropout_events(wbr)
    try:
        # 4 lost-claim-race vs 1 ceiling-release: race covers 4/5 (>=50%),
        # >=2 events, and strictly outranks the runner-up -> dominant.
        for _ in range(4):
            wbr.record_claim_skipped("lost_claim_race")
        wbr.record_grace_ceiling_release()

        result = wbr.compute_dominant_reuse_dropout()
        assert result["dropout_total"] == 5
        assert result["dropout_by_reason"]["claim_lost_claim_race"] == 4
        assert result["dropout_by_reason"]["grace_ceiling_release"] == 1
        dom = result["dominant_dropout"]
        assert dom is not None
        assert dom["reason"] == "claim_lost_claim_race"
        assert dom["count"] == 4
        assert dom["total"] == 5
        assert dom["share"] == 0.8
        assert "per-VM" in result["attribution_note"]
    finally:
        _clear_all_dropout_events(wbr)


def test_dominant_dropout_none_on_even_split_and_empty():
    """No leader is named on an even split (no clear dominant) or empty window."""
    import app.warm_backlog_reuse as wbr

    _clear_all_dropout_events(wbr)
    try:
        # Empty window -> total 0, no dominant.
        empty = wbr.compute_dominant_reuse_dropout()
        assert empty["dropout_total"] == 0
        assert empty["dominant_dropout"] is None

        # Even 2-vs-2 split: top does not strictly outrank runner-up -> None.
        wbr.record_claim_skipped("lost_claim_race")
        wbr.record_claim_skipped("lost_claim_race")
        wbr.record_grace_idle_release()
        wbr.record_grace_idle_release()
        tied = wbr.compute_dominant_reuse_dropout()
        assert tied["dropout_total"] == 4
        assert tied["dominant_dropout"] is None
    finally:
        _clear_all_dropout_events(wbr)


@pytest.mark.asyncio
async def test_funnel_warm_claim_increments_succeeded(
    client: AsyncClient, db_session, mocker
):
    """A successful /next-role backlog claim increments warm_claims_succeeded
    and leaves claim_skipped_total at zero."""
    import app.warm_backlog_reuse as wbr

    _patch_broadcast(mocker)
    _clear_funnel_events(wbr)
    try:
        with _FlagGuard(backlog=True):
            project = await _make_project(db_session, name="Funnel Claim")
            vm_id = "agent-vm-funnel-claim"
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            await _make_backlog_task(db_session, project.id)
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 200, resp.text

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["warm_claims_succeeded"] >= 1
        assert snap["claim_skipped_total"] == 0
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_funnel_no_eligible_increments_skipped(
    client: AsyncClient, db_session, mocker
):
    """A /next-role poll with an empty backlog increments claim_skipped no_eligible."""
    import app.warm_backlog_reuse as wbr

    _patch_broadcast(mocker)
    _clear_funnel_events(wbr)
    try:
        with _FlagGuard(backlog=True):
            project = await _make_project(db_session, name="Funnel No Eligible")
            vm_id = "agent-vm-funnel-noelig"
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            # No QUEUED backlog task — queue is empty.
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 204

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["warm_claims_succeeded"] == 0
        assert snap["claim_skipped_by_reason"].get("no_eligible", 0) >= 1
    finally:
        _clear_funnel_events(wbr)


# ---------------------------------------------------------------------------
# Eligibility-scan skip-reason funnel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_skip_reasons_increment_each_stage_once(db_session):
    """A single scan that triggers each structural skip reason at least once
    increments each new snapshot field by exactly 1."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Mixed")
        vm_id = "agent-vm-scan-mixed"

        # IN_PROGRESS task editing src/app.py — file-conflict source.
        await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.IN_PROGRESS,
            context={"related_files": ["src/app.py"]},
        )
        # File-conflict candidate (large scope: any single-file overlap defers).
        await _make_backlog_task(
            db_session,
            project.id,
            context={"scope": "large", "related_files": ["src/app.py"]},
        )
        # Depends_on candidate.
        await _make_backlog_task(
            db_session,
            project.id,
            depends_on=["00000000-0000-0000-0000-000000000123"],
        )
        # Pinned to a different VM.
        await _make_backlog_task(
            db_session,
            project.id,
            context={"warm_target_vm_id": "agent-vm-other"},
        )
        # One clean candidate so the scan still includes non-skip rows.
        await _make_backlog_task(
            db_session, project.id, context={"related_files": ["docs/readme.md"]}
        )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 1
        assert snap["scan_polls_with_pinned_vm_skip"] == 1
        assert snap["scan_polls_with_file_conflict_skip"] == 1
        # 5 QUEUED rows fits under the default _backlog_scan_limit() (10).
        assert snap["scan_polls_saturated"] == 0
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_skip_aggregates_per_poll_not_per_row(db_session):
    """A poll that skips multiple rows for the same reason increments the
    counter by 1 (per-poll aggregation), not by the row count."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Per Poll")
        vm_id = "agent-vm-scan-poll"

        # Three depends_on-skipped candidates.
        for _ in range(3):
            await _make_backlog_task(
                db_session,
                project.id,
                depends_on=["00000000-0000-0000-0000-000000000999"],
            )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        # 3 skipped rows in one poll == exactly 1 event.
        assert snap["scan_polls_with_depends_on_skip"] == 1

        # A second poll with the same backlog: counter advances by exactly 1.
        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 2
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_saturated_increments_when_at_limit(db_session, monkeypatch):
    """When rows_fetched hits _backlog_scan_limit(), scan_polls_saturated increments."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Saturated")
        vm_id = "agent-vm-scan-sat"

        # Lower the scan limit so the test stays cheap.
        monkeypatch.setitem(wbr._runtime_config, "warm_backlog_scan_limit", 3)
        for _ in range(3):
            await _make_backlog_task(db_session, project.id)

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_saturated"] == 1
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_skip_no_events_when_all_eligible(db_session):
    """A scan where every candidate is eligible records zero skip events."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Clean")
        vm_id = "agent-vm-scan-clean"
        await _make_backlog_task(
            db_session, project.id, context={"related_files": ["docs/x.md"]}
        )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 0
        assert snap["scan_polls_with_pinned_vm_skip"] == 0
        assert snap["scan_polls_with_file_conflict_skip"] == 0
        assert snap["scan_polls_saturated"] == 0
    finally:
        _clear_funnel_events(wbr)


# ---------------------------------------------------------------------------
# Bounded, activity-gated grace-window teardown (_deferred_backlog_teardown)
# ---------------------------------------------------------------------------


def _set_grace_config(monkeypatch, wbr, *, defer, window, idle, interval):
    monkeypatch.setitem(wbr._runtime_config, "warm_backlog_teardown_defer_secs", defer)
    monkeypatch.setitem(wbr._runtime_config, "warm_backlog_grace_window_secs", window)
    monkeypatch.setitem(wbr._runtime_config, "warm_backlog_grace_idle_secs", idle)
    monkeypatch.setitem(
        wbr._runtime_config, "warm_backlog_grace_poll_interval_secs", interval
    )


@pytest.mark.asyncio
async def test_grace_window_claim_skips_teardown(monkeypatch, mocker):
    """A claim landing during the window returns without deleting the VM."""
    import app.vm_lifecycle as vml
    import app.warm_backlog_reuse as wbr

    _set_grace_config(monkeypatch, wbr, defer=20, window=60, idle=30, interval=5)
    mocker.patch("app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock)
    delete_mock = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)
    # Not claimed on the first check, claimed on the second.
    mocker.patch(
        "app.vm_lifecycle._vm_claimed_backlog_work",
        new_callable=mocker.AsyncMock,
        side_effect=[False, True],
    )

    await vml._deferred_backlog_teardown("agent-vm-grace-claim", "agent_completion")

    delete_mock.assert_not_called()


@pytest.mark.asyncio
async def test_grace_window_idle_release_tears_down_early(monkeypatch, mocker):
    """When the agent stops heartbeating before the ceiling, the activity gate
    releases the hold early and the VM is torn down (idle release)."""
    import app.vm_lifecycle as vml
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    wbr._grace_idle_release_events.clear()
    wbr._grace_ceiling_release_events.clear()
    wbr._grace_window_extension_events.clear()
    try:
        _set_grace_config(monkeypatch, wbr, defer=20, window=120, idle=30, interval=5)
        mocker.patch("app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock)
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        # Agent never claims and is not alive (stopped polling).
        mocker.patch(
            "app.vm_lifecycle._vm_claimed_backlog_work",
            new_callable=mocker.AsyncMock,
            return_value=False,
        )
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=False)

        await vml._deferred_backlog_teardown("agent-vm-grace-idle", "agent_completion")

        delete_mock.assert_awaited_once()
        assert len(wbr._grace_idle_release_events) == 1
        assert len(wbr._grace_ceiling_release_events) == 0
    finally:
        wbr._grace_idle_release_events.clear()
        wbr._grace_ceiling_release_events.clear()
        wbr._grace_window_extension_events.clear()


@pytest.mark.asyncio
async def test_grace_window_extends_until_bounded_ceiling(monkeypatch, mocker):
    """A still-polling but never-claiming VM is extended each interval and torn
    down at the bounded ceiling (not held indefinitely)."""
    import app.vm_lifecycle as vml
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    wbr._grace_idle_release_events.clear()
    wbr._grace_ceiling_release_events.clear()
    wbr._grace_window_extension_events.clear()
    try:
        _set_grace_config(monkeypatch, wbr, defer=20, window=50, idle=30, interval=5)
        mocker.patch("app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock)
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        mocker.patch(
            "app.vm_lifecycle._vm_claimed_backlog_work",
            new_callable=mocker.AsyncMock,
            return_value=False,
        )
        # Agent keeps polling the whole time.
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)

        await vml._deferred_backlog_teardown("agent-vm-grace-ceil", "agent_completion")

        delete_mock.assert_awaited_once()
        assert len(wbr._grace_ceiling_release_events) == 1
        assert len(wbr._grace_idle_release_events) == 0
        # min_hold=20, ceiling=50, interval=5 -> extensions at 20,25,...,45 = 6.
        assert len(wbr._grace_window_extension_events) == 6
    finally:
        wbr._grace_idle_release_events.clear()
        wbr._grace_ceiling_release_events.clear()
        wbr._grace_window_extension_events.clear()


@pytest.mark.asyncio
async def test_grace_window_ceiling_floored_at_min_hold(monkeypatch, mocker):
    """A grace ceiling configured below the minimum hold is floored at the
    minimum hold, so the unconditional first-poll window is never undercut."""
    import app.vm_lifecycle as vml
    import app.warm_backlog_reuse as wbr

    # window (5) < defer (20): ceiling must floor to 20, so the loop runs the
    # min-hold sleep and then immediately hits the ceiling.
    _set_grace_config(monkeypatch, wbr, defer=20, window=5, idle=30, interval=5)
    sleep_mock = mocker.patch(
        "app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock
    )
    delete_mock = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch(
        "app.vm_lifecycle._vm_claimed_backlog_work",
        new_callable=mocker.AsyncMock,
        return_value=False,
    )
    alive_mock = mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)

    await vml._deferred_backlog_teardown("agent-vm-grace-floor", "agent_completion")

    # The unconditional min-hold sleep used 20 (the floored value), not 5.
    assert sleep_mock.await_args_list[0].args[0] == 20
    delete_mock.assert_awaited_once()
    # Ceiling hit immediately after min-hold; no activity-gate iteration ran.
    alive_mock.assert_not_called()
