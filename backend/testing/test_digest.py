"""Integration tests for /api/admin/digest and /api/admin/activity endpoints."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.main import app
from app.models import (
    Artifact,
    ArtifactType,
    ImprovementLane,
    ImprovementLaneStatus,
    PoolVm,
    PoolVmState,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
    ResidualRemediationCase,
    ResidualCaseStatus,
    ReleaseVersion,
)
from app.capability_trial_verdict import LANE_PILOT_ASSIGNMENT_KEY, LANE_PILOT_CAP_NAME
from app.routers.admin_analytics import _derive_completed_at
from app.routers.admin_digest import _compute_digest
from app.digest_metrics import (
    compute_remediation_and_patterns,
    compute_remediation_funnel,
    compute_trial_arm_accumulation,
    compute_trial_arm_accrual,
    compute_trial_arm_cost_delta,
)
from app.sr_review_quality_metrics import (
    _stash_projected_task_fields,
)
from testing.factories import make_project, make_task, make_membership, _utcnow

from testing._digest_test_helpers import _curr_ts, _prev_ts


@pytest_asyncio.fixture(scope="function")
async def unauthenticated_client(override_get_session):
    """Client with no auth override — useful for testing 401/403 responses."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_digest_empty_window(client: AsyncClient):
    """No tasks in the DB → all counts are 0 and lists are empty."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["window_hours"] == 24
    assert data["tasks_truncated"] is False  # no tasks → well below the 5000-row cap
    assert data["completed_count"] == 0
    assert data["failed_count"] == 0
    assert data["pending_approval_count"] == 0
    assert data["research_cycles"] == 0
    assert data["prs_opened"] == 0
    assert data["active_count"] == 0
    assert data["active_tasks"] == []
    assert data["needs_attention"] == []
    assert data["recent_failures"] == []
    assert data["recent_completions"] == []
    assert "parallel_review_effectiveness" not in data
    assert data["stale_heartbeat_count"] == 0


@pytest.mark.asyncio
async def test_digest_stale_heartbeat_count_old_heartbeat(db_session):
    """IN_PROGRESS task with a >5min-old heartbeat increments stale_heartbeat_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    db_session.add(stale_task)
    await db_session.commit()

    data = await _compute_digest(hours=24, project_id=None, session=db_session)
    assert data["stale_heartbeat_count"] == 1


@pytest.mark.asyncio
async def test_digest_stale_heartbeat_count_fresh_heartbeat(db_session):
    """IN_PROGRESS task with a recent heartbeat does not increment stale_heartbeat_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fresh_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(minutes=1),
    )
    db_session.add(fresh_task)
    await db_session.commit()

    data = await _compute_digest(hours=24, project_id=None, session=db_session)
    assert data["stale_heartbeat_count"] == 0


@pytest.mark.asyncio
async def test_digest_stale_heartbeat_count_is_membership_scoped(db_session):
    """stale_heartbeat_count for a project_id only counts that project's stale tasks."""
    project_a = make_project(name="Project A")
    project_b = make_project(name="Project B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    stale_a = make_task(
        project_id=project_a.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    stale_b = make_task(
        project_id=project_b.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    db_session.add(stale_a)
    db_session.add(stale_b)
    await db_session.commit()

    # Scoped to project_a — should only count stale_a
    data_a = await _compute_digest(
        hours=24, project_id=project_a.id, session=db_session
    )
    assert data_a["stale_heartbeat_count"] == 1

    # Fleet-wide — should count both
    data_all = await _compute_digest(hours=24, project_id=None, session=db_session)
    assert data_all["stale_heartbeat_count"] == 2


@pytest.mark.asyncio
async def test_compute_digest_counts_active_tasks_without_display_row_cap(db_session):
    """active_count/running/queued use SQL aggregation, not the display row cap."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    active_tasks = [
        make_task(
            project_id=project.id,
            title=f"Queued task {idx}",
            status=TaskStatus.QUEUED,
            created_at=now - timedelta(minutes=idx % 60),
            payloads=[{"type": "create"}],
        )
        for idx in range(525)
    ]
    db_session.add_all(active_tasks)
    await db_session.commit()

    data = await _compute_digest(hours=24, project_id=None, session=db_session)

    assert data["active_count"] == 525
    assert data["queued_count"] == 525
    assert data["running_count"] == 0
    assert len(data["active_tasks"]) == 5


@pytest.mark.asyncio
async def test_lane_pilot_kill_survives_digest_session_rollback(db_session):
    """A digest-computed lane-pilot KILL is committed outside the digest session."""
    from app.shared_state import runtime_config

    previous_flag = runtime_config.get("review_fix_lane_pilot_enabled")
    runtime_config["review_fix_lane_pilot_enabled"] = True
    try:
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        lane = ImprovementLane(
            project_id=project.id,
            title="Digest KILL lane",
            goal="prove durable kill",
            branch_name="lane/digest-kill",
            base_branch="main",
            status=ImprovementLaneStatus.ACTIVE.value,
            pilot_arm="lane",
        )
        db_session.add(lane)
        await db_session.flush()
        db_session.add(
            PoolVm(
                vm_instance_id="digest-kill-reserved-vm",
                project_id=project.id,
                state=PoolVmState.IDLE,
                reserved_lane_id=lane.id,
                reservation_expires_at=_utcnow() + timedelta(hours=1),
            )
        )
        for idx in range(3):
            db_session.add(
                make_task(
                    project_id=project.id,
                    title=f"Lane review leaked context {idx}",
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "review"}],
                    context={
                        LANE_PILOT_ASSIGNMENT_KEY: "lane",
                        "improvement_lane_id": str(lane.id),
                    },
                )
            )
        await db_session.commit()

        Session = async_sessionmaker(
            db_session.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async with Session() as digest_session:
            result = await _compute_digest(
                hours=24, project_id=project.id, session=digest_session
            )
            assert (
                result["capability_adoption_verdicts"][LANE_PILOT_CAP_NAME]["verdict"]
                == "KILL"
            )
            await digest_session.rollback()

        async with Session() as fresh:
            stored_project = await fresh.get(Project, project.id)
            stored_verdict = stored_project.capability_adoption_verdicts[
                LANE_PILOT_CAP_NAME
            ]
            assert stored_verdict["verdict"] == "KILL"
            assert stored_verdict["verdict_reason"] == (
                "gate_integrity_violations_exceeded"
            )
            stored_lane = await fresh.get(ImprovementLane, lane.id)
            assert stored_lane.status == ImprovementLaneStatus.KILLED.value
            stored_vm = (
                (
                    await fresh.execute(
                        select(PoolVm).where(
                            PoolVm.vm_instance_id == "digest-kill-reserved-vm"
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert stored_vm.reserved_lane_id is None
            assert stored_vm.reservation_expires_at is None
            messages = (
                (
                    await fresh.execute(
                        select(ProjectMessage).where(
                            ProjectMessage.project_id == project.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(messages) == 2
            dedupe_keys = {m.context["dedupe_key"] for m in messages}
            assert "review_fix_lane_gate_integrity_violation" in dedupe_keys

        assert runtime_config["review_fix_lane_pilot_enabled"] is False

        async with Session() as digest_session:
            await _compute_digest(
                hours=24, project_id=project.id, session=digest_session
            )
            await digest_session.rollback()

        async with Session() as fresh:
            stored_lane = await fresh.get(ImprovementLane, lane.id)
            assert stored_lane.status == ImprovementLaneStatus.KILLED.value
            messages = (
                (
                    await fresh.execute(
                        select(ProjectMessage).where(
                            ProjectMessage.project_id == project.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(messages) == 2
    finally:
        if previous_flag is None:
            runtime_config.pop("review_fix_lane_pilot_enabled", None)
        else:
            runtime_config["review_fix_lane_pilot_enabled"] = previous_flag


@pytest.mark.asyncio
async def test_request_path_digest_persists_trial_verdict_and_alert_without_caller_commit(
    db_session, mocker
):
    """Request-path digest compute durably persists BOTH a capability trial
    verdict AND a gate-integrity alert even when the caller rolls back / closes
    the request session WITHOUT committing.

    Regression guard for the original bug: ``_compute_digest`` recorded verdicts
    + alerts onto the early-fetched request-session row and only ``flush()``ed,
    so the FastAPI session dependency closing without a commit silently rolled
    them back on every on-demand request-path compute. Durability must now come
    from ``persist_trial_verdicts_in_transaction``'s own write-owned session.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Force a DECIDED (adopt) verdict for the generic capability trials without
    # needing 30+ real per-arm task rows: the assignment-based arm composition
    # satisfies min_n so evaluate_trial_verdict returns adopt.
    mocker.patch(
        "app.routers.admin_digest.compute_trial_arm_accumulation",
        new_callable=AsyncMock,
        return_value={
            "lift": 0.25,
            "with_n": 40,
            "without_n": 40,
            "arm_composition": {
                "with_assigned": 40,
                "without_assigned": 40,
                "with_receipt": 0,
                "without_receipt": 0,
            },
        },
    )
    # Lane verdict: CONTINUE but immediate_alert → the gate-integrity alert is
    # routed through the durable write-owned helper (NOT the request session).
    mocker.patch(
        "app.review_fix_lane_pilot.compute_lane_pilot_aggregates",
        new_callable=AsyncMock,
        return_value={},
    )
    mocker.patch(
        "app.routers.admin_digest.evaluate_lane_pilot_verdict",
        return_value={
            "verdict": "CONTINUE",
            "verdict_reason": "treatment_not_engaging",
            "immediate_alert": True,
        },
    )

    Session = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    async with Session() as digest_session:
        result = await _compute_digest(
            hours=24, project_id=project.id, session=digest_session
        )
        # The decided verdict surfaces in the response payload...
        _resp_verdicts = result["capability_adoption_verdicts"]
        assert any(
            isinstance(v, dict) and v.get("verdict") in ("adopt", "reject")
            for k, v in _resp_verdicts.items()
            if k != LANE_PILOT_CAP_NAME
        )
        # ...and the caller closes WITHOUT committing.
        await digest_session.rollback()

    # Re-open a fresh session: both the trial verdict and the alert must survive.
    async with Session() as fresh:
        stored_project = await fresh.get(Project, project.id)
        stored = stored_project.capability_adoption_verdicts or {}
        decided = [
            (k, v)
            for k, v in stored.items()
            if k != LANE_PILOT_CAP_NAME
            and isinstance(v, dict)
            and v.get("verdict") in ("adopt", "reject")
        ]
        assert decided, f"no decided trial verdict durably persisted: {stored}"

        messages = (
            (
                await fresh.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            (m.context or {}).get("dedupe_key")
            == "review_fix_lane_gate_integrity_violation"
            for m in messages
        ), "gate-integrity alert was not durably persisted"


@pytest.mark.asyncio
async def test_precompute_persists_verdicts_when_l2_cache_write_fails(
    db_session, mocker
):
    """A precompute tick whose L2 cache write (write_shared) FAILS still durably
    persists the trial verdicts.

    Before the fix, verdict durability on the precompute path was a side effect
    of ``write_shared`` committing the request session for its DigestCacheEntry
    upsert — and write_shared rolls back on any L2 write failure, discarding the
    verdicts with it. The write-owned helper makes durability independent of L2.
    """
    from app.digest_precompute import _precompute_one

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.routers.admin_digest.compute_trial_arm_accumulation",
        new_callable=AsyncMock,
        return_value={
            "lift": 0.25,
            "with_n": 40,
            "without_n": 40,
            "arm_composition": {
                "with_assigned": 40,
                "without_assigned": 40,
                "with_receipt": 0,
                "without_receipt": 0,
            },
        },
    )
    # Force the L2 merge to raise so write_shared rolls back the request session
    # and returns False — exactly the failure mode that used to discard verdicts.
    mocker.patch.object(
        db_session,
        "merge",
        new=AsyncMock(side_effect=RuntimeError("forced L2 merge failure")),
    )

    # Snapshot the id: the forced L2 failure makes write_shared roll back
    # db_session inside _precompute_one, expiring ``project`` — a later
    # ``project.id`` lazy-load would raise MissingGreenlet under real ORM expiry.
    project_id = project.id

    # Should not raise: write_shared is fail-open.
    await _precompute_one(db_session, hours=24, project_id=project_id)

    Session = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    async with Session() as fresh:
        stored_project = await fresh.get(Project, project_id)
        stored = stored_project.capability_adoption_verdicts or {}
        decided = [
            (k, v)
            for k, v in stored.items()
            if k != LANE_PILOT_CAP_NAME
            and isinstance(v, dict)
            and v.get("verdict") in ("adopt", "reject")
        ]
        assert decided, (
            f"trial verdicts were lost when the L2 cache write failed: {stored}"
        )


@pytest.mark.asyncio
async def test_lane_pilot_immediate_alert_on_continue_is_not_dropped(
    db_session, mocker
):
    """A CONTINUE verdict with immediate_alert still raises the operator alert."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.review_fix_lane_pilot.compute_lane_pilot_aggregates",
        new_callable=AsyncMock,
        return_value={},
    )
    mocker.patch(
        "app.routers.admin_digest.evaluate_lane_pilot_verdict",
        return_value={
            "verdict": "CONTINUE",
            "verdict_reason": "treatment_not_engaging",
            "immediate_alert": True,
        },
    )
    raise_alert = mocker.patch(
        "app.review_fix_lane_pilot.raise_gate_integrity_alert",
        new_callable=AsyncMock,
    )
    kill_lane = mocker.patch(
        "app.review_fix_lane_pilot.apply_lane_pilot_kill_in_transaction",
        new_callable=AsyncMock,
    )

    await _compute_digest(hours=24, project_id=project.id, session=db_session)

    # The alert is now raised durably through persist_trial_verdicts_in_transaction's
    # OWN write-owned session (not the rollback-prone request session), so we assert
    # on the project_id + detail rather than binding to the request session object.
    raise_alert.assert_awaited_once()
    _alert_call = raise_alert.await_args
    assert _alert_call.args[1] == project.id
    assert _alert_call.kwargs["detail"] == "treatment_not_engaging"
    kill_lane.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_with_completed_tasks(client: AsyncClient, db_session):
    """Completed tasks inside the window appear in recent_completions with correct count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(
        project_id=project.id,
        title="Completed Task 1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    task2 = make_task(
        project_id=project.id,
        title="Completed Task 2",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 2
    assert data["failed_count"] == 0
    assert data["research_cycles"] == 1  # only the research-type task

    titles = {c["title"] for c in data["recent_completions"]}
    assert "Completed Task 1" in titles
    assert "Completed Task 2" in titles


@pytest.mark.asyncio
async def test_digest_recent_completions_include_scope_and_review_iterations(
    client: AsyncClient, db_session
):
    """recent_completions entries expose scope and review_iterations from task.context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Scoped Completed Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"scope": "medium", "review_loop": {"iteration": 3}},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    entries = [
        c for c in data["recent_completions"] if c["title"] == "Scoped Completed Task"
    ]
    assert len(entries) == 1
    assert entries[0]["scope"] == "medium"
    assert entries[0]["review_iterations"] == 3


@pytest.mark.asyncio
async def test_digest_recent_completions_include_unverified_merge_reason(
    client: AsyncClient, db_session
):
    """recent_completions exposes the no-CI gate fall-open reason KEY (None when verified)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    unverified_task = make_task(
        project_id=project.id,
        title="Unverified Merge Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "review_loop": {"unverified_merge_reason": "no_detectable_test_command"}
        },
    )
    verified_task = make_task(
        project_id=project.id,
        title="Verified Merge Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"review_loop": {"iteration": 1}},
    )
    db_session.add(unverified_task)
    db_session.add(verified_task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    by_title = {c["title"]: c for c in data["recent_completions"]}
    assert (
        by_title["Unverified Merge Task"]["unverified_merge_reason"]
        == "no_detectable_test_command"
    )
    assert by_title["Verified Merge Task"]["unverified_merge_reason"] is None


@pytest.mark.asyncio
async def test_digest_with_failed_tasks(client: AsyncClient, db_session):
    """Failed tasks appear in both needs_attention (reason='failed') and recent_failures."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failed_count"] == 1
    assert data["completed_count"] == 0

    assert len(data["needs_attention"]) == 1
    attention = data["needs_attention"][0]
    assert attention["task_id"] == str(task.id)
    assert attention["title"] == "Failed Task"
    assert attention["reason"] == "failed"

    assert len(data["recent_failures"]) == 1
    failure = data["recent_failures"][0]
    assert failure["task_id"] == str(task.id)
    assert failure["title"] == "Failed Task"
    assert "failed_at" in failure


@pytest.mark.asyncio
async def test_digest_merge_gate_fleet_health_verified(client: AsyncClient, db_session):
    """Fleet with verified merges: verified_count matches, verification_rate is
    correct, degraded_project_count is 0 (no fall-opens)."""
    now_iso = _utcnow().isoformat()
    p1 = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": True,
            "recent_outcomes": [
                {"at": now_iso, "verified": True, "reason": None},
                {"at": now_iso, "verified": True, "reason": None},
            ],
        }
    )
    p2 = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": True,
            "recent_outcomes": [
                {"at": now_iso, "verified": True, "reason": None},
            ],
        }
    )
    db_session.add_all([p1, p2])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    mg = response.json()["merge_gate_fleet_health"]
    assert mg["verified_count"] == 3
    assert mg["fell_open_count"] == 0
    assert mg["verification_rate"] == 1.0
    assert mg["most_common_reason"] is None
    assert mg["degraded_project_count"] == 0
    assert mg["project_count"] == 2


@pytest.mark.asyncio
async def test_digest_merge_gate_fleet_health_fell_open(
    client: AsyncClient, db_session
):
    """Fleet with fell-open merges: fell_open_count and most_common_reason are
    correct, degraded_project_count reflects projects with falls."""
    now_iso = _utcnow().isoformat()
    # Degraded project: 1 verified + 2 fell-open (probe_passed dominates).
    degraded = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": False,
            "recent_outcomes": [
                {"at": now_iso, "verified": True, "reason": None},
                {"at": now_iso, "verified": False, "reason": "probe_passed"},
                {"at": now_iso, "verified": False, "reason": "suite_all_skipped"},
            ],
            "unverified_reason_counts": {"probe_passed": 2, "suite_all_skipped": 1},
        }
    )
    # Healthy project: all verified, contributes no fall-opens.
    healthy = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": True,
            "recent_outcomes": [
                {"at": now_iso, "verified": True, "reason": None},
            ],
        }
    )
    db_session.add_all([degraded, healthy])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    mg = response.json()["merge_gate_fleet_health"]
    assert mg["verified_count"] == 2
    assert mg["fell_open_count"] == 2
    assert mg["verification_rate"] == 0.5
    assert mg["most_common_reason"] == "probe_passed"
    assert mg["degraded_project_count"] == 1
    assert mg["project_count"] == 2


@pytest.mark.asyncio
async def test_digest_merge_gate_fleet_health_empty(client: AsyncClient, db_session):
    """Empty fleet (no projects with gate data): signal is present with zero
    counts and does not crash; legacy/malformed records are tolerated."""
    # A project with no merge_gate_runtime at all, plus one with a legacy
    # four-key-only record (no recent_outcomes) and one with a malformed value.
    p_none = make_project()
    p_legacy = make_project(
        merge_gate_runtime={
            "last_run_at": _utcnow().isoformat(),
            "executed_real_tests": True,
        }
    )
    p_malformed = make_project(merge_gate_runtime={"recent_outcomes": "not-a-list"})
    db_session.add_all([p_none, p_legacy, p_malformed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    mg = response.json()["merge_gate_fleet_health"]
    assert mg == {
        "verified_count": 0,
        "fell_open_count": 0,
        "verification_rate": None,
        "most_common_reason": None,
        "degraded_project_count": 0,
        "project_count": 0,
    }


@pytest.mark.asyncio
async def test_digest_dependency_health_surfaces_finding_structural_give_up(
    client: AsyncClient, db_session
):
    """The admin digest wires through the finding-level structural give-up signal."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        title="research cycle with give-up",
        context={
            "finding_structural_give_up": {
                "at": "2026-06-04T12:00:00",
                "total": 1,
                "by_reason": {"prereq_deferred_same_cycle": 1},
                "by_direction": {"d-aaa": 1},
                "sample": [
                    {
                        "title": "Use shared helper",
                        "reason": "prereq_deferred_same_cycle",
                        "direction_id": "d-aaa",
                    }
                ],
            }
        },
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    give_up = response.json()["dependency_health_signals"]["finding_structural_give_up"]
    assert give_up["total"] == 1
    assert give_up["by_reason"] == {"prereq_deferred_same_cycle": 1}
    assert give_up["by_direction"] == {"d-aaa": 1}


@pytest.mark.asyncio
async def test_digest_merge_gate_fleet_health_window_filters_stale(
    client: AsyncClient, db_session
):
    """recent_outcomes entries older than the digest window are excluded; only
    in-window entries count toward verified/fell-open totals."""
    now_iso = _utcnow().isoformat()
    stale_iso = (_utcnow() - timedelta(days=30)).isoformat()
    project = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": True,
            "recent_outcomes": [
                {"at": stale_iso, "verified": False, "reason": "probe_passed"},
                {"at": now_iso, "verified": True, "reason": None},
            ],
        }
    )
    db_session.add(project)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    mg = response.json()["merge_gate_fleet_health"]
    # The stale fell-open entry is filtered out by the window; only the recent
    # verified entry remains.
    assert mg["verified_count"] == 1
    assert mg["fell_open_count"] == 0
    assert mg["project_count"] == 1
    assert mg["degraded_project_count"] == 0


@pytest.mark.asyncio
async def test_digest_merge_gate_fleet_health_reason_respects_window(
    client: AsyncClient, db_session
):
    """The most_common_reason reflects only in-window fall-opens, not stale
    all-time tallies. A project with an old fall-open for reason A and a new
    fall-open for reason B should report B as most_common when the old one is
    outside the window."""
    now_iso = _utcnow().isoformat()
    stale_iso = (_utcnow() - timedelta(days=30)).isoformat()
    project = make_project(
        merge_gate_runtime={
            "last_run_at": now_iso,
            "executed_real_tests": True,
            "recent_outcomes": [
                # Stale fall-open with reason A (outside window)
                {"at": stale_iso, "verified": False, "reason": "probe_passed"},
                # In-window fall-open with reason B (inside window)
                {"at": now_iso, "verified": False, "reason": "suite_all_skipped"},
            ],
            # All-time tally shows probe_passed dominates historically
            "unverified_reason_counts": {"probe_passed": 5, "suite_all_skipped": 1},
        }
    )
    db_session.add(project)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    mg = response.json()["merge_gate_fleet_health"]
    # The in-window entry shows 1 fall-open with suite_all_skipped reason.
    assert mg["verified_count"] == 0
    assert mg["fell_open_count"] == 1
    # most_common_reason must reflect the in-window reason (suite_all_skipped),
    # not the all-time dominant (probe_passed). This is the fix: the windowed
    # reason should not be masked by historical counts.
    assert mg["most_common_reason"] == "suite_all_skipped"
    assert mg["project_count"] == 1
    assert mg["degraded_project_count"] == 1


@pytest.mark.asyncio
async def test_digest_aggregates_merge_retries_saved(client: AsyncClient, db_session):
    """total_merge_retries_saved sums _merge_retries_saved across completed+failed tasks;
    merge_early_exit_count counts tasks with a positive saved value."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        context={"_merge_retries_saved": 3},
    )
    t2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"_merge_retries_saved": 2},
    )
    t3 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        context={"_merge_retries_saved": 0},
    )
    t4 = make_task(project_id=project.id, status=TaskStatus.FAILED, context={})
    for t in (t1, t2, t3, t4):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total_merge_retries_saved"] == 5
    assert data["merge_early_exit_count"] == 2


@pytest.mark.asyncio
async def test_digest_failed_task_direction_attribution(
    client: AsyncClient, db_session
):
    """Failed tasks with direction_id context expose direction_id/direction_text in needs_attention, recent_failures, and failure_groups."""
    project = make_project(
        directions=[{"id": "dir-f1", "text": "Harden reliability", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-f1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    attention = data["needs_attention"][0]
    assert attention["direction_id"] == "dir-f1"
    assert attention["direction_text"] == "Harden reliability"

    failure = data["recent_failures"][0]
    assert failure["direction_id"] == "dir-f1"
    assert failure["direction_text"] == "Harden reliability"
    assert failure["task_type"] == "create"

    assert data["failure_groups"]
    group_task = data["failure_groups"][0]["tasks"][0]
    assert group_task["direction_id"] == "dir-f1"
    assert group_task["direction_text"] == "Harden reliability"


@pytest.mark.asyncio
async def test_digest_retry_waste_by_direction(client: AsyncClient, db_session):
    """retry_waste_by_direction groups retry-overhead cost by direction_id, sorted descending."""
    project = make_project(
        directions=[
            {"id": "dir-A", "text": "Direction Alpha", "status": "active"},
            {"id": "dir-B", "text": "Direction Beta", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # dir-A: one retry, 2h on-demand → expensive
    t_a = make_task(
        project_id=project.id,
        title="A retry",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-A", "_attempt_number": 2},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    # dir-B: two retries, each 30min spot → cheaper
    t_b1 = make_task(
        project_id=project.id,
        title="B retry 1",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-B", "_attempt_number": 2},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    t_b2 = make_task(
        project_id=project.id,
        title="B retry 2",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-B", "_attempt_number": 3},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    db_session.add_all([t_a, t_b1, t_b2])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    breakdown = data["retry_waste_by_direction"]
    assert len(breakdown) == 2
    # Sorted descending by cost — dir-A (on-demand, 2h) > dir-B (spot, 2x30min)
    assert breakdown[0]["direction_id"] == "dir-A"
    assert breakdown[0]["direction_text"] == "Direction Alpha"
    assert breakdown[0]["retry_task_count"] == 1
    assert breakdown[0]["retry_cost_usd"] > 0

    assert breakdown[1]["direction_id"] == "dir-B"
    assert breakdown[1]["direction_text"] == "Direction Beta"
    assert breakdown[1]["retry_task_count"] == 2
    assert breakdown[1]["retry_cost_usd"] > 0
    assert breakdown[0]["retry_cost_usd"] > breakdown[1]["retry_cost_usd"]


@pytest.mark.asyncio
async def test_digest_provision_waste_metrics(client: AsyncClient, db_session):
    """provision_waste_cost_usd sums VM cost of PROVISION_FAILURE tasks;
    provision_waste_by_direction groups and sorts by cost descending."""
    project = make_project(
        directions=[
            {"id": "dir-A", "text": "Direction Alpha", "status": "active"},
            {"id": "dir-B", "text": "Direction Beta", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # dir-A: 2h on-demand provision failure (expensive)
    pf_a = make_task(
        project_id=project.id,
        title="A prov fail",
        status=TaskStatus.FAILED,
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        context={"direction_id": "dir-A"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    # dir-B: 30min spot provision failure
    pf_b = make_task(
        project_id=project.id,
        title="B prov fail",
        status=TaskStatus.FAILED,
        latest_status_message="IN_USE_ADDRESSES quota exceeded for zone us-central1-a",
        context={"direction_id": "dir-B"},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    # Non-provision failure — should NOT contribute
    other = make_task(
        project_id=project.id,
        title="Code error",
        status=TaskStatus.FAILED,
        latest_status_message="AssertionError: thing broke",
        context={"direction_id": "dir-A"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    db_session.add_all([pf_a, pf_b, other])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "provision_waste_cost_usd" in data
    assert data["provision_waste_cost_usd"] > 0
    # Should match cost_by_failure_class["PROVISION_FAILURE"]
    assert data["provision_waste_cost_usd"] == pytest.approx(
        data["cost_by_failure_class"].get("PROVISION_FAILURE", 0.0)
    )

    breakdown = data["provision_waste_by_direction"]
    assert len(breakdown) == 2
    assert breakdown[0]["direction_id"] == "dir-A"
    assert breakdown[0]["direction_text"] == "Direction Alpha"
    assert breakdown[0]["provision_task_count"] == 1
    assert breakdown[0]["provision_cost_usd"] > breakdown[1]["provision_cost_usd"]
    assert breakdown[1]["direction_id"] == "dir-B"
    assert breakdown[1]["provision_task_count"] == 1


@pytest.mark.asyncio
async def test_digest_analysis_overflow_summary(client: AsyncClient, db_session):
    """analysis_overflow_summary counts failed tasks with scope_gate_triggered=analysis_overflow,
    breaks down by scope, and sums VM cost using the standard rate calculation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    overflow_small = make_task(
        project_id=project.id,
        title="overflow small",
        status=TaskStatus.FAILED,
        context={"scope_gate_triggered": "analysis_overflow", "scope": "small"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    overflow_medium = make_task(
        project_id=project.id,
        title="overflow medium",
        status=TaskStatus.FAILED,
        context={"scope_gate_triggered": "analysis_overflow", "scope": "medium"},
        vm_provisioned_at=now - timedelta(minutes=30),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    # Non-overflow failure — should NOT contribute
    other = make_task(
        project_id=project.id,
        title="unrelated",
        status=TaskStatus.FAILED,
        context={"scope": "small"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    db_session.add_all([overflow_small, overflow_medium, other])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    summary = data["analysis_overflow_summary"]
    assert summary["total_count"] == 2
    assert summary["by_scope"] == {"small": 1, "medium": 1}
    assert summary["cost_usd"] > 0


@pytest.mark.asyncio
async def test_digest_recent_failures_includes_failure_class_and_scope(
    client: AsyncClient, db_session
):
    """recent_failures entries include failure_class and scope fields."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"scope": "small"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert len(data["recent_failures"]) == 1
    failure = data["recent_failures"][0]
    assert "failure_class" in failure
    assert failure["failure_class"] is not None
    assert failure["scope"] == "small"


@pytest.mark.asyncio
async def test_digest_recurring_error_clusters(client: AsyncClient, db_session):
    """Failed tasks sharing a normalized error are grouped into a recurring cluster (count >= 2)."""
    project = make_project(
        directions=[{"id": "dir-rc", "text": "Reliability", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two failures with the same root cause but differing PR numbers — should
    # normalize to the same fingerprint.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail A",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-rc"},
            latest_status_message="Merge conflict on PR #1234",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail B",
            status=TaskStatus.FAILED,
            payloads=[{"type": "fix"}],
            context={"direction_id": "dir-rc"},
            latest_status_message="Merge conflict on PR #5678",
        )
    )
    # Singleton — should be excluded from clusters.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail C",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            latest_status_message="Unique failure: disk full",
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    clusters = data["recurring_error_clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["count"] == 2
    assert len(cluster["fingerprint"]) == 12
    assert "Merge conflict" in cluster["error_snippet"]
    assert cluster["direction_ids"] == ["dir-rc"]
    assert sorted(cluster["task_types"]) == ["create", "fix"]


@pytest.mark.asyncio
async def test_digest_recurring_error_clusters_empty_when_all_unique(
    client: AsyncClient, db_session
):
    """If every failed task has a distinct fingerprint, recurring_error_clusters is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i, msg in enumerate(
        [
            "TypeError: foo is undefined",
            "Rate limit exceeded for region us-west",
            "SyntaxError: unexpected token in parser",
        ]
    ):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Unique {i}",
                status=TaskStatus.FAILED,
                latest_status_message=msg,
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["recurring_error_clusters"] == []


@pytest.mark.asyncio
async def test_digest_recurring_patterns_from_persistent_anomalies(
    client: AsyncClient, db_session
):
    """persistent_anomalies entries with occurrence_count >= 2 appear in recurring_patterns."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-rec-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "Fingerprint fp-rec-001 affects 2 directions.",
        },
        "CODE_ERROR": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 1,  # below threshold, should be filtered out
            "severity": "medium",
            "latest_details": "low-count anomaly",
        },
    }
    try:
        # Failed task in last 48h with same fingerprint and a direction, so
        # affected_direction_count > 0 and remediation_status is unaddressed.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Recurring fail",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={"direction_id": "dir-recur"},
                latest_status_message="boom: same root cause",
            )
        )
        await db_session.commit()

        # Seed persistent_anomalies key with the actual fingerprint of the message.
        from app.anomaly_detection import _error_fingerprint

        actual_fp = _error_fingerprint("boom: same root cause")
        _ss.persistent_anomalies[proj_key][f"CrossDirectionFingerprint_{actual_fp}"] = {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "boom affects directions",
        }

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()

        patterns = data["recurring_patterns"]
        assert len(patterns) == 2  # two with occurrence_count >= 2, one filtered
        fps = {p["fingerprint"] for p in patterns}
        assert "fp-rec-001" in fps
        assert actual_fp in fps
        # Sorted by occurrence_count desc — fp-rec-001 (4) first.
        assert patterns[0]["fingerprint"] == "fp-rec-001"
        assert patterns[0]["occurrence_count"] == 4
        assert patterns[0]["severity"] == "high"
        assert patterns[0]["remediation_status"] == "unaddressed"
        assert patterns[0]["proactive"] is False
        # actual_fp entry should count the matching failed task's direction.
        actual_entry = next(p for p in patterns if p["fingerprint"] == actual_fp)
        assert actual_entry["affected_direction_count"] == 1
        assert actual_entry["affected_directions"] == [
            {"direction_id": "dir-recur", "direction_text": ""}
        ]
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_remediation_status_investigating(
    client: AsyncClient, db_session
):
    """An in-flight investigate task with matching diagnosis_fingerprint marks status=investigating."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-inv-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "fp-inv-001 pattern",
        },
    }
    try:
        db_session.add(
            make_task(
                project_id=project.id,
                title="Active investigation",
                status=TaskStatus.IN_PROGRESS,
                payloads=[{"type": "investigate"}],
                context={"diagnosis_fingerprint": "fp-inv-001", "proactive": True},
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["remediation_status"] == "investigating"
        assert patterns[0]["proactive"] is True
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_remediation_status_fix_verified(
    client: AsyncClient, db_session
):
    """A verified merged completion with no 48h recurrence marks status=fix_verified."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-ver-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "fp-ver-001 pattern",
        },
    }
    try:
        # Landed AND verified (pr_merged with no unverified_merge_reason), and no
        # recent failed task with this fingerprint → fix_verified.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Verified fix",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "error_fingerprint": "fp-ver-001",
                    "pr_merged": True,
                },
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["remediation_status"] == "fix_verified"
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_fix_shipped_when_recurs(
    client: AsyncClient, db_session
):
    """A landed+verified merge still recurring in 48h marks fix_shipped, not fix_verified."""
    from app import shared_state as _ss
    from app.anomaly_detection import _error_fingerprint

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    actual_fp = _error_fingerprint("boom: shipped but recurring")
    _ss.persistent_anomalies[proj_key] = {
        f"CrossDirectionFingerprint_{actual_fp}": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "recurring pattern",
        },
    }
    try:
        # Verified merged completion for this fingerprint...
        db_session.add(
            make_task(
                project_id=project.id,
                title="Shipped fix",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"error_fingerprint": actual_fp, "pr_merged": True},
            )
        )
        # ...but the same failure recurred within the last 48h → recurrence gate
        # downgrades fix_verified to fix_shipped.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Recurred again",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={"direction_id": "dir-recur"},
                latest_status_message="boom: shipped but recurring",
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        entry = next(p for p in patterns if p["fingerprint"] == actual_fp)
        assert entry["remediation_status"] == "fix_shipped"
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_fix_shipped_not_verified_when_unverified(
    client: AsyncClient, db_session
):
    """A landed merge with a degraded unverified-merge reason is fix_shipped, never fix_verified."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-unv-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "fp-unv-001 pattern",
        },
    }
    try:
        # Landed (pr_merged) but the no-CI gate fell open in a *degraded* way, so
        # the verification standard is NOT met even with no recurrence.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Unverified shipped fix",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "error_fingerprint": "fp-unv-001",
                    "pr_merged": True,
                    "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
                },
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["remediation_status"] == "fix_shipped"
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_completed_pr_url_only_is_unaddressed(
    client: AsyncClient, db_session
):
    """A completed task with only a pr_url (no landed signal) no longer counts as merged."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-url-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "fp-url-001 pattern",
        },
    }
    try:
        # COMPLETED with a PR URL but NO landed signal — the old loose proxy would
        # have marked this fix_shipped; the canonical predicates do not.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Completed but never landed",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "error_fingerprint": "fp-url-001",
                    "pr_url": "https://github.com/example/repo/pull/1",
                },
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["remediation_status"] == "unaddressed"
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_early_warnings_affected_direction_count_per_class(
    client: AsyncClient, db_session
):
    """early_warnings affected_direction_count reflects dirs affected by that specific warning, not global total."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    # Two distinct FailureRateMomentum anomalies with different affected_directions.
    _ss.persistent_anomalies[proj_key] = {
        "FailureRateMomentum": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "FailureRateMomentum: 10% → 20% → 30% across last 3x24h windows",
            "affected_directions": ["dir-a", "dir-b"],
        },
        "FailureRateMomentum_db": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "FailureRateMomentum: 5% → 15% → 25% across last 3x24h windows",
            "affected_directions": ["dir-c"],
        },
    }
    try:
        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()

        warnings = data["early_warnings"]
        # Both FailureRateMomentum entries have occurrence_count >= 2, so both should appear.
        assert len(warnings) == 2

        # Find warnings by failure_class and verify affected_direction_count matches
        # the actual affected_directions for each warning, not a global total.
        warning_1 = next(
            w for w in warnings if w["failure_class"] == "FailureRateMomentum"
        )
        warning_2 = next(
            w for w in warnings if w["failure_class"] == "FailureRateMomentum_db"
        )

        assert warning_1["affected_direction_count"] == 2  # affects dir-a, dir-b
        assert warning_2["affected_direction_count"] == 1  # affects dir-c only

        # Ensure they have different counts (the bug was that they were the same).
        assert (
            warning_1["affected_direction_count"]
            != warning_2["affected_direction_count"]
        )
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_approval_task_direction_attribution(
    client: AsyncClient, db_session
):
    """Approval-pending tasks with direction_id context expose direction attribution in needs_attention."""
    project = make_project(
        directions=[{"id": "dir-a1", "text": "Approval direction", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
        context={"direction_id": "dir-a1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    approval_items = [
        a for a in data["needs_attention"] if a["reason"] == "approval_required"
    ]
    assert approval_items
    assert approval_items[0]["direction_id"] == "dir-a1"
    assert approval_items[0]["direction_text"] == "Approval direction"


@pytest.mark.asyncio
async def test_digest_with_pending_approval(client: AsyncClient, db_session):
    """Tasks requiring approval (non-terminal) appear in needs_attention with reason='approval_required'."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["pending_approval_count"] == 1

    attention_reasons = {a["reason"] for a in data["needs_attention"]}
    assert "approval_required" in attention_reasons

    approval_items = [
        a for a in data["needs_attention"] if a["reason"] == "approval_required"
    ]
    assert len(approval_items) == 1
    assert approval_items[0]["task_id"] == str(task.id)
    assert approval_items[0]["title"] == "Approval Task"


@pytest.mark.asyncio
async def test_digest_approval_compute_experiment_enriched(
    client: AsyncClient, db_session
):
    """A held compute_experiment task surfaces its proposed spend cap and
    deadline plus task_type in its needs_attention entry; a plain approval
    task omits the compute-specific fields."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    compute_task = make_task(
        project_id=project.id,
        title="Held compute launch",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
        payloads=[
            {
                "type": "compute_experiment",
                "spend_cap_usd": 42.5,
                "deadline_seconds": 7200,
            }
        ],
    )
    plain_task = make_task(
        project_id=project.id,
        title="Plain approval",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
        payloads=[{"type": "create"}],
    )
    db_session.add(compute_task)
    db_session.add(plain_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {
        a["task_id"]: a
        for a in data["needs_attention"]
        if a["reason"] == "approval_required"
    }
    compute_entry = by_id[str(compute_task.id)]
    assert compute_entry["task_type"] == "compute_experiment"
    assert compute_entry["spend_cap_usd"] == 42.5
    assert compute_entry["deadline_seconds"] == 7200

    plain_entry = by_id[str(plain_task.id)]
    assert plain_entry["task_type"] == "create"
    assert "spend_cap_usd" not in plain_entry
    assert "deadline_seconds" not in plain_entry


@pytest.mark.asyncio
async def test_digest_time_window_filtering(client: AsyncClient, db_session):
    """Tasks with created_at outside the requested hours window are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task inside the 24-hour window
    inside_task = make_task(
        project_id=project.id,
        title="Inside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=12),
    )
    # Task outside the 24-hour window
    outside_task = make_task(
        project_id=project.id,
        title="Outside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=25),
    )
    db_session.add(inside_task)
    db_session.add(outside_task)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Inside Window" in titles
    assert "Outside Window" not in titles


@pytest.mark.asyncio
async def test_digest_windowing_uses_durable_completed_at_not_status_log_tail(
    client: AsyncClient, db_session
):
    """A post-terminal status_log write (e.g. a latest_status_message append) must
    NOT pull a task whose durable completed_at is OUTSIDE the window back into the
    digest. Deriving from the status_log tail would wrongly include it."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    completed_at = now - timedelta(hours=30)  # OUTSIDE the 24h window
    post_terminal_msg_at = now - timedelta(hours=1)  # INSIDE the window
    task = make_task(
        project_id=project.id,
        title="Completed Long Ago Then Messaged",
        status=TaskStatus.COMPLETED,
        created_at=completed_at,
        completed_at=completed_at,
        status_log=[
            {"s": "COMPLETED", "t": completed_at.isoformat()},
            # Mirrors latest_status_message appending a row post-terminal.
            {"t": post_terminal_msg_at.isoformat(), "msg": "operator note"},
        ],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 0
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Completed Long Ago Then Messaged" not in titles


@pytest.mark.asyncio
async def test_digest_legacy_null_completed_at_windows_by_status_log_tail(
    client: AsyncClient, db_session
):
    """Legacy rows whose durable completed_at was never stamped still window by the
    status_log tail (unchanged fallback), NOT created_at."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    terminal_at = now - timedelta(hours=2)  # INSIDE the 24h window
    task = make_task(
        project_id=project.id,
        title="Legacy In Window",
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=40),  # OUTSIDE the window
        status_log=[{"s": "COMPLETED", "t": terminal_at.isoformat()}],
    )
    db_session.add(task)
    await db_session.commit()
    # Simulate a legacy row whose durable column was never populated.
    task.completed_at = None
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Legacy In Window" in titles


@pytest.mark.asyncio
async def test_digest_project_filter(client: AsyncClient, db_session):
    """project_id query param filters tasks to only the specified project."""
    project_a = make_project(name="Project A")
    project_b = make_project(name="Project B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = make_task(
        project_id=project_a.id,
        title="Task A",
        status=TaskStatus.COMPLETED,
    )
    task_b = make_task(
        project_id=project_b.id,
        title="Task B",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project_a.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Task A" in titles
    assert "Task B" not in titles


@pytest.mark.asyncio
async def test_digest_effort_model_efficiency_includes_cancelled_token_spend(
    client: AsyncClient, db_session
):
    """Cancelled terminal runs contribute spend to the efficiency rollup but do
    not count as landed PRs."""
    from app.llm_cost_attribution import LLM_TOKEN_PRICING

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-opus-4-8")]
    expected_total_cost = 1500 * input_rate + 1500 * output_rate

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "effort": "high"}],
            context={"pr_merged": True},
            provider="anthropic",
            model="claude-opus-4-8",
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 1000,
                "llm_call_count": 1,
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.CANCELLED,
            payloads=[{"type": "create", "effort": "high"}],
            provider="anthropic",
            model="claude-opus-4-8",
            api_call_summary={
                "total_input_tokens": 500,
                "total_output_tokens": 500,
                "llm_call_count": 1,
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    efficiency = data["effort_model_efficiency"]
    assert efficiency["totals"]["run_count"] == 2
    assert efficiency["totals"]["landed_count"] == 1
    assert efficiency["totals"]["total_cost_usd"] == pytest.approx(
        expected_total_cost
    )
    assert efficiency["totals"]["cost_per_merge"] == pytest.approx(
        expected_total_cost
    )
    by_effort = {row["effort"]: row for row in efficiency["by_effort"]}
    assert by_effort["high"]["run_count"] == 2
    assert by_effort["high"]["landed_count"] == 1


@pytest.mark.asyncio
async def test_digest_active_count(client: AsyncClient, db_session):
    """In-progress tasks appear in active_count/active_tasks and not in completed_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_task = make_task(
        project_id=project.id,
        title="Active Task",
        status=TaskStatus.IN_PROGRESS,
    )
    completed_task = make_task(
        project_id=project.id,
        title="Done Task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(active_task)
    db_session.add(completed_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["active_count"] >= 1
    assert data["completed_count"] == 1

    active_ids = {t["task_id"] for t in data["active_tasks"]}
    assert str(active_task.id) in active_ids

    # active task should not bleed into completed_count
    completed_titles = [c["title"] for c in data["recent_completions"]]
    assert "Active Task" not in completed_titles


@pytest.mark.asyncio
async def test_digest_active_tasks_direction_and_cost(client: AsyncClient, db_session):
    """active_tasks entries expose direction_id, direction_text and running_cost_usd."""
    project = make_project(
        directions=[{"id": "d-1", "text": "Improve perf", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provisioned_at = _utcnow() - timedelta(hours=1)
    active_task = make_task(
        project_id=project.id,
        title="Directed Active",
        status=TaskStatus.IN_PROGRESS,
        context={"direction_id": "d-1"},
        vm_provisioned_at=provisioned_at,
        vm_spot=True,
    )
    bare_task = make_task(
        project_id=project.id,
        title="Bare Active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(active_task)
    db_session.add(bare_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    directed = by_id[str(active_task.id)]
    assert directed["direction_id"] == "d-1"
    assert directed["direction_text"] == "Improve perf"
    assert directed["running_cost_usd"] is not None
    assert directed["running_cost_usd"] > 0

    bare = by_id[str(bare_task.id)]
    assert bare["direction_id"] is None
    assert bare["direction_text"] is None
    assert bare["running_cost_usd"] is None


@pytest.mark.asyncio
async def test_digest_active_tasks_includes_scope(client: AsyncClient, db_session):
    """active_tasks entries expose the scope field from task context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    scoped_task = make_task(
        project_id=project.id,
        title="Scoped Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "large"},
    )
    bare_task = make_task(
        project_id=project.id,
        title="Bare Active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(scoped_task)
    db_session.add(bare_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    assert by_id[str(scoped_task.id)]["scope"] == "large"
    assert by_id[str(bare_task.id)]["scope"] is None


@pytest.mark.asyncio
async def test_digest_active_tasks_includes_review_iteration(
    client: AsyncClient, db_session
):
    """active_tasks entries expose review_iteration and review_cap from task.context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    iter_task = make_task(
        project_id=project.id,
        title="In-review Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "medium", "review_loop": {"iteration": 4}},
    )
    fresh_task = make_task(
        project_id=project.id,
        title="Fresh Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    db_session.add(iter_task)
    db_session.add(fresh_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    assert by_id[str(iter_task.id)]["review_iteration"] == 4
    assert by_id[str(iter_task.id)]["review_cap"] == 12
    assert by_id[str(fresh_task.id)]["review_iteration"] == 0
    assert by_id[str(fresh_task.id)]["review_cap"] == 10


@pytest.mark.asyncio
async def test_digest_includes_long_running_completed_task(
    client: AsyncClient, db_session
):
    """A task created before the window but completed within it appears in completed_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task created 30 hours ago (outside 24h window) but completed 1 hour ago (inside window)
    completion_time = _utcnow() - timedelta(hours=1)
    long_running = make_task(
        project_id=project.id,
        title="Long Running Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=30),
        status_log=[{"t": completion_time.isoformat(), "status": "completed"}],
    )
    db_session.add(long_running)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Long Running Task" in titles


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_digest_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/digest",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_activity_basic(client: AsyncClient, db_session):
    """Completed and failed tasks generate TASK_COMPLETED and TASK_FAILED events."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    completed = make_task(
        project_id=project.id,
        title="Completed Task",
        status=TaskStatus.COMPLETED,
    )
    failed = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(completed)
    db_session.add(failed)
    await db_session.commit()

    response = await client.get("/api/admin/activity")
    assert response.status_code == 200
    data = response.json()

    assert "events" in data
    assert "total" in data
    assert data["total"] >= 2

    event_types = {e["type"] for e in data["events"]}
    assert "TASK_COMPLETED" in event_types
    assert "TASK_FAILED" in event_types


@pytest.mark.asyncio
async def test_activity_since_filter(client: AsyncClient, db_session):
    """The since parameter filters out events before the cutoff timestamp."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    old_task = make_task(
        project_id=project.id,
        title="Old Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(days=10),
    )
    new_task = make_task(
        project_id=project.id,
        title="New Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(old_task)
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(old_task)
    await db_session.refresh(new_task)

    # Use a cutoff that excludes the old task
    since = (_utcnow() - timedelta(days=5)).isoformat()
    response = await client.get(f"/api/admin/activity?since={since}")
    assert response.status_code == 200
    data = response.json()

    task_ids = [e["task_id"] for e in data["events"] if e.get("task_id")]
    assert str(new_task.id) in task_ids
    assert str(old_task.id) not in task_ids


@pytest.mark.asyncio
async def test_activity_limit(client: AsyncClient, db_session):
    """The limit parameter caps the number of events returned."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    for i in range(5):
        task = make_task(
            project_id=project.id,
            title=f"Task {i}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/activity?limit=2")
    assert response.status_code == 200
    data = response.json()

    assert len(data["events"]) <= 2
    # total reflects all events found before limiting
    assert data["total"] >= 5


@pytest.mark.asyncio
async def test_activity_failed_includes_failure_class_and_taxonomy(
    client: AsyncClient, db_session
):
    """TASK_FAILED events expose failure_class and taxonomy from task context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    failed = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
    )
    db_session.add(failed)
    await db_session.commit()

    response = await client.get("/api/admin/activity")
    assert response.status_code == 200
    data = response.json()
    failed_events = [e for e in data["events"] if e["type"] == "TASK_FAILED"]
    assert len(failed_events) >= 1
    ev = next(e for e in failed_events if e["task_id"] == str(failed.id))
    assert ev["failure_class"] == "RATE_LIMIT"
    assert ev["taxonomy"] == "infra_related"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_activity_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/activity",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_narrative_good_progress_prefix(client: AsyncClient, db_session):
    """Narrative starts with 'Good progress' when success rate improves by > 0.05 vs previous window."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 1 completed + 3 failed = 0.25 success rate
    prev = _prev_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        )
    )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 3 completed + 1 failed = 0.75 success rate
    # delta = 0.75 - 0.25 = 0.50 > 0.05 → trend="improving" → "Good progress —"
    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Good progress" in data["narrative_summary"]
    assert data["trend"] == "improving"


@pytest.mark.asyncio
async def test_narrative_declining_prefix(client: AsyncClient, db_session):
    """Narrative starts with 'Pipeline health declining' when success rate drops by > 0.05 vs previous."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 3 completed + 1 failed = 0.75 success rate
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    # Current window: 1 completed + 3 failed = 0.25 success rate
    # delta = 0.25 - 0.75 = -0.50 < -0.05 → trend="declining" → "Pipeline health declining —"
    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Pipeline health declining" in data["narrative_summary"]
    assert data["trend"] == "declining"


@pytest.mark.asyncio
async def test_narrative_no_trend_prefix_when_stable(client: AsyncClient, db_session):
    """Narrative has no trend prefix when success rate delta is within ±0.05 (stable)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Both windows: 3 completed + 1 failed = 0.75 success rate → delta = 0.0 → stable
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Good progress" not in data["narrative_summary"]
    assert "Pipeline health declining" not in data["narrative_summary"]
    assert data["trend"] == "stable"


@pytest.mark.asyncio
async def test_narrative_up_from_x(client: AsyncClient, db_session):
    """completed_delta is correct when current window has more completions than previous.

    With only completions and no failures/approvals, the narrative takes the 'All clear'
    path and does not include the delta phrase — but completed_delta is still reported.
    Add a failure to force the standard path and verify 'up from X' appears there.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 1 completed
    prev = _prev_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        )
    )

    # Current window: 2 completed + 1 failed → forces standard narrative path
    # completed_delta = 2 - 1 = 1 > 0 → "(up from 1 last period)"
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "up from 1" in data["narrative_summary"]
    assert data["completed_delta"] == 1


@pytest.mark.asyncio
async def test_narrative_all_quiet_fallback(client: AsyncClient, db_session):
    """Narrative is 'All quiet' when no tasks exist in the window."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "All quiet" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_dominant_category(
    client: AsyncClient, db_session
):
    """When a taxonomy category dominates failures (>=50%), narrative appends an actionable hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Failed Task {i}",
                status=TaskStatus.FAILED,
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "infra_related"
    assert "Infrastructure issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_review_quality(
    client: AsyncClient, db_session
):
    """CODE_ERROR failures with test/lint keywords → review_quality narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Lint Fail {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed"},
                latest_status_message="pytest failed on test_foo",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert (
        data["failure_taxonomy_distribution"]["dominant_category"] == "review_quality"
    )
    assert "Review quality issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_scope_related(
    client: AsyncClient, db_session
):
    """CODE_ERROR failures with expansion_ratio > 1.5 → scope_related narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Scope Fail {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed", "expansion_ratio": 2.5},
                latest_status_message="assertion error in handler",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "scope_related"
    assert "Scope mismatch is the top failure pattern" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_code_defect(client: AsyncClient, db_session):
    """CODE_ERROR failures without review/scope signals → code_defect narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Defect {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed"},
                latest_status_message="null pointer exception in handler",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "code_defect"
    assert "Code defects dominate failures" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_trend_improving(client: AsyncClient, db_session):
    """trend='improving' when success_rate_delta > 0.05 (prev=0.5, current=0.6, delta=0.1)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 5 completed + 5 failed = 0.50 success rate
    prev = _prev_ts()
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 6 completed + 4 failed = 0.60 success rate → delta = 0.10 > 0.05
    curr = _curr_ts()
    for _ in range(6):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "improving"
    assert data["prev_success_rate"] == 0.5
    assert data["success_rate"] == 0.6
    assert data["success_rate_delta"] == 0.1


@pytest.mark.asyncio
async def test_trend_declining(client: AsyncClient, db_session):
    """trend='declining' when success_rate_delta < -0.05 (prev=0.8, current=0.7, delta=-0.1)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 8 completed + 2 failed = 0.80 success rate
    prev = _prev_ts()
    for _ in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 7 completed + 3 failed = 0.70 success rate → delta = -0.10 < -0.05
    curr = _curr_ts()
    for _ in range(7):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "declining"
    assert data["prev_success_rate"] == 0.8
    assert data["success_rate"] == 0.7
    assert data["success_rate_delta"] == -0.1


@pytest.mark.asyncio
async def test_trend_stable(client: AsyncClient, db_session):
    """trend='stable' when |success_rate_delta| <= 0.05 (identical rates, delta=0.0)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Both windows: 3 completed + 1 failed = 0.75 success rate → delta = 0.0
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "stable"
    assert data["success_rate_delta"] == 0.0


@pytest.mark.asyncio
async def test_trend_none_when_no_previous_terminal_tasks(
    client: AsyncClient, db_session
):
    """trend=None when the previous window has zero terminal tasks (can't compute prev_success_rate)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Current window only: 2 completed — no tasks in previous window
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["prev_success_rate"] is None
    assert data["success_rate_delta"] is None
    assert data["trend"] is None


@pytest.mark.asyncio
async def test_failure_group_rate_limit(client: AsyncClient, db_session):
    """Task with an exact Claude rate-limit message is classified into RATE_LIMIT failure group."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Rate Limited Task",
            status=TaskStatus.FAILED,
            latest_status_message="API Error: Rate limit reached",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "RATE_LIMIT" in failure_classes

    rate_limit_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "RATE_LIMIT"
    )
    assert rate_limit_group["count"] == 1
    assert any(
        "Rate Limited Task" in t.get("title", "") for t in rate_limit_group["tasks"]
    )


@pytest.mark.asyncio
async def test_failure_group_provision_failure(client: AsyncClient, db_session):
    """Task with 'quota' in status message is classified into PROVISION_FAILURE failure group."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Quota Exceeded Task",
            status=TaskStatus.FAILED,
            latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "PROVISION_FAILURE" in failure_classes

    provision_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "PROVISION_FAILURE"
    )
    assert provision_group["count"] == 1


@pytest.mark.asyncio
async def test_failure_group_code_error_default(client: AsyncClient, db_session):
    """Task with a generic error message is classified into CODE_ERROR failure group (default)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Generic Error Task",
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: cannot read property 'foo' of undefined",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "CODE_ERROR" in failure_classes

    code_error_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR"
    )
    assert code_error_group["count"] == 1
    task_entry = code_error_group["tasks"][0]
    assert "taxonomy" in task_entry
    assert task_entry["taxonomy"] in {
        "scope_related",
        "review_quality",
        "code_defect",
        "infra_related",
    }


@pytest.mark.asyncio
async def test_failure_groups_sorted_by_count_descending(
    client: AsyncClient, db_session
):
    """failure_groups are sorted by count descending (most frequent failure class first)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 CODE_ERROR tasks (generic messages)
    for i in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Code Error Task {i}",
                status=TaskStatus.FAILED,
                latest_status_message="SyntaxError: unexpected token",
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    # 1 RATE_LIMIT task
    db_session.add(
        make_task(
            project_id=project.id,
            title="Rate Limit Task",
            status=TaskStatus.FAILED,
            latest_status_message="API Error: Rate limit reached",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert len(data["failure_groups"]) >= 2
    # First group should have the highest count
    counts = [g["count"] for g in data["failure_groups"]]
    assert counts == sorted(counts, reverse=True)
    assert data["failure_groups"][0]["failure_class"] == "CODE_ERROR"
    assert data["failure_groups"][0]["count"] == 2


@pytest.mark.asyncio
async def test_failure_group_agent_timeout_subtypes(client: AsyncClient, db_session):
    """AGENT_TIMEOUT failures expose timeout_subtype in recent_failures and failure_groups."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # Cascade cancellations are ADMIN_CANCEL (not AGENT_TIMEOUT) since PR #10508.
    timeout_cases = [
        ("Heartbeat Task", "No heartbeat received after 300s", "heartbeat_lost"),
        ("OOM Task", "exit code 137 out of memory", "oom"),
        ("Exec Task", "execution timed out after 1800s", "execution_timeout"),
        (
            "Iter Task",
            "merge blocked, fix not spawned (iteration cap)",
            "iteration_limit",
        ),
        ("Crash Task", "[FATAL:uncaughtException] boom", "process_crash"),
    ]
    cascade_title = "Cascade Task"
    cascade_msg = "Cancelled: parent evaluation timeout"
    all_tasks = timeout_cases + [(cascade_title, cascade_msg, None)]
    for title, msg, _ in all_tasks:
        db_session.add(
            make_task(
                project_id=project.id,
                title=title,
                status=TaskStatus.FAILED,
                latest_status_message=msg,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    timeout_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "AGENT_TIMEOUT"
    )
    title_to_subtype = {
        t["title"]: t["timeout_subtype"] for t in timeout_group["tasks"]
    }
    for title, _, expected_subtype in timeout_cases:
        assert title_to_subtype[title] == expected_subtype, (
            title,
            title_to_subtype[title],
        )

    rf_by_title = {f["title"]: f for f in data["recent_failures"]}
    for title, _, expected_subtype in timeout_cases:
        assert rf_by_title[title]["timeout_subtype"] == expected_subtype
    # Cascade cancellations are ADMIN_CANCEL — timeout_subtype is not populated.
    assert rf_by_title[cascade_title]["timeout_subtype"] is None


@pytest.mark.asyncio
async def test_non_timeout_failures_have_null_timeout_subtype(
    client: AsyncClient, db_session
):
    """Non-AGENT_TIMEOUT failures expose timeout_subtype=None."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Code Error Task",
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: something broke",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    code_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR"
    )
    assert code_group["tasks"][0]["timeout_subtype"] is None
    assert data["recent_failures"][0]["timeout_subtype"] is None


@pytest.mark.asyncio
async def test_narrative_approval_before_completion(client: AsyncClient, db_session):
    """Approval message appears before completion stats in the narrative (action-first ordering)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # One completed task
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    # One task awaiting approval
    db_session.add(
        make_task(
            project_id=project.id,
            title="Approval Task",
            status=TaskStatus.IN_PROGRESS,
            requires_approval=True,
            created_at=curr,
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    narrative = data["narrative_summary"]
    assert isinstance(narrative, str)
    assert "awaiting your approval" in narrative
    assert "completed" in narrative
    # Approval message must appear before completion stats
    assert narrative.index("awaiting your approval") < narrative.index("completed")


@pytest.mark.asyncio
async def test_narrative_all_clear_when_no_attention_needed(
    client: AsyncClient, db_session
):
    """Narrative is a single 'All clear' sentence when only completions exist (no failures, no approvals)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 3
    assert data["failed_count"] == 0
    assert data["pending_approval_count"] == 0

    narrative = data["narrative_summary"]
    assert "All clear" in narrative
    assert "no action needed" in narrative
    assert "3 tasks completed" in narrative
    # No trend prefix or failure language
    assert "Good progress" not in narrative
    assert "Pipeline health declining" not in narrative
    assert "failed" not in narrative


@pytest.mark.asyncio
async def test_trend_history_includes_first_attempt_rate(
    client: AsyncClient, db_session
):
    """trend_history entries include first_attempt_success_rate and first_attempt_sample_size."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add two completed tasks in the current window with _first_attempt_succeeded context
    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            context={"_first_attempt_succeeded": True},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            context={"_first_attempt_succeeded": False},
        )
    )
    # A task without the metric — should not count toward sample
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    trend_history = data["trend_history"]
    assert isinstance(trend_history, list)
    assert len(trend_history) == 7

    # All entries must have the new fields
    for entry in trend_history:
        assert "first_attempt_success_rate" in entry
        assert "first_attempt_sample_size" in entry

    # The last entry (current period) should reflect our two measured tasks
    last_entry = trend_history[-1]
    assert last_entry["first_attempt_sample_size"] == 2
    assert last_entry["first_attempt_success_rate"] == 0.5


@pytest.mark.asyncio
async def test_first_attempt_success_rate_breakdowns(client: AsyncClient, db_session):
    """first_attempt_success_rate includes by_type, by_scope, trend, and first_attempt_by_review_category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    prev = _prev_ts()

    # Current window: create/small success, refactor/medium failure w/ review cats
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": True, "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "refactor"}],
            context={
                "_first_attempt_succeeded": False,
                "scope": "medium",
                "_first_attempt_rejection_categories": {
                    "architecture": 1,
                    "correctness": 1,
                },
            },
        )
    )
    # Previous window: all failures — to produce an 'improving' trend
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
                payloads=[{"type": "create"}],
                context={"_first_attempt_succeeded": False, "scope": "small"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fa = response.json()["first_attempt_success_rate"]

    assert fa["by_type"] == {"create": 1.0, "refactor": 0.0}
    assert fa["by_scope"] == {"small": 1.0, "medium": 0.0}
    assert fa["trend"] == "improving"
    assert fa["first_attempt_by_review_category"] == {
        "architecture": 1.0,
        "correctness": 1.0,
    }


@pytest.mark.asyncio
async def test_first_attempt_by_review_category_includes_regression_risk(
    client: AsyncClient, db_session
):
    """regression_risk in _first_attempt_rejection_categories surfaces in the digest's
    first_attempt_by_review_category breakdown without being silently dropped."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    prev = _prev_ts()

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": True, "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_succeeded": False,
                "scope": "small",
                "_first_attempt_rejection_categories": {"regression_risk": 1},
            },
        )
    )
    # Previous window: needed so trend computes without errors.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
                payloads=[{"type": "create"}],
                context={"_first_attempt_succeeded": False, "scope": "small"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fa = response.json()["first_attempt_success_rate"]

    by_cat = fa["first_attempt_by_review_category"]
    assert "regression_risk" in by_cat, (
        "regression_risk must surface in first_attempt_by_review_category"
    )
    # by_review_category is the fraction of first-attempt-rejected tasks that
    # flagged this category. We have one such task and it flagged regression_risk,
    # so the share is 1.0 — the value isn't the interesting bit; the key being
    # present (no silent drop) is.
    assert by_cat["regression_risk"] == 1.0


@pytest.mark.asyncio
async def test_heuristic_regression_risk_not_skipped_in_sr_skip_rate(
    client: AsyncClient, db_session
):
    """Heuristic-derived regression_risk entries (issue text tagged [heuristic-derived])
    are treated as valid SR outcomes — not as 'skipped' fallback envelopes — so they
    contribute to catch-rate metrics the same way baseline-derived entries do."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()

    def _make_sr(issue: str, passed: bool = False) -> dict:
        return {
            "verdict": "fail" if not passed else "pass",
            "fixes_applied": False,
            "check_results": {
                "regression_risk": {"passed": passed, "issue": issue},
            },
            "blind_spots": [],
        }

    # 3 fix tasks with heuristic-derived regression_risk (the new signal)
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _make_sr(
                        "[heuristic-derived] +120 source lines / 0 test lines"
                    )
                },
            )
        )
    # 2 fix tasks with baseline-style regression_risk (no heuristic tag)
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _make_sr(
                        "test_foo.py::test_bar now fails after change", passed=True
                    )
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    skip_rate = data.get("self_review_skip_rate")
    assert skip_rate is not None, (
        "self_review_skip_rate must be present (≥5 code-change tasks)"
    )
    # All 5 tasks have valid SR outcomes — none should be classified as skipped
    assert skip_rate["skipped_count"] == 0, (
        "heuristic-derived regression_risk entries must not count as skipped SR outcomes — "
        "aggregation should key on category presence, not issue text"
    )
    assert skip_rate["sample_size"] == 5

    # self_review_effectiveness sample_size must include both heuristic and baseline tasks
    sre = data.get("self_review_effectiveness")
    assert sre is not None, (
        "self_review_effectiveness must be present (≥3 tasks with SR outcomes)"
    )
    assert sre["sample_size"] == 5


@pytest.mark.asyncio
async def test_trend_history_includes_failed_by_taxonomy(
    client: AsyncClient, db_session
):
    """trend_history entries include failed_by_taxonomy counts parallel to failed_by_class."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # A review_quality failure (status message contains a keyword)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="pytest tests failed",
        )
    )
    # A scope_related failure (positive evidence: status-msg scope keyword)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="scope too large for task",
            context={"scope": "medium", "scope_calibrated_from": "small"},
        )
    )
    # A residual code_defect failure (no scope, no review keywords)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="unexpected exception",
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    trend_history = data["trend_history"]
    assert len(trend_history) == 7
    for entry in trend_history:
        assert "failed_by_taxonomy" in entry
        assert isinstance(entry["failed_by_taxonomy"], dict)

    last_entry = trend_history[-1]
    assert last_entry["failed"] == 3
    tax = last_entry["failed_by_taxonomy"]
    assert tax.get("review_quality") == 1
    assert tax.get("scope_related") == 1
    assert tax.get("code_defect") == 1
    assert sum(tax.values()) == 3


@pytest.mark.asyncio
async def test_digest_scope_conversion_rates_aggregated(
    client: AsyncClient, db_session
):
    """scope_conversion_rates aggregates scope_breakdown from research_history within the window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 5,
                        "tasks_created": 6,
                        "scope_breakdown": {
                            "small": {"created": 4, "merged": 3, "failed": 1},
                            "medium": {"created": 2, "merged": 1, "failed": 0},
                        },
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 10,
                        "tasks_created": 10,
                        "scope_breakdown": {
                            "small": {"created": 10, "merged": 10, "failed": 0},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    # scope_conversion_rates should aggregate only the recent entry (not the old one outside window)
    rates = data["scope_conversion_rates"]
    assert rates is not None
    assert rates["small"]["created"] == 4
    assert rates["small"]["merged"] == 3
    assert rates["medium"]["created"] == 2
    assert rates["medium"]["merged"] == 1

    # scope_breakdown should appear on the direction_progress entry
    dp = data["direction_progress"]
    assert len(dp) == 1
    sb = dp[0]["scope_breakdown"]
    assert sb is not None
    assert sb["small"]["created"] == 4
    assert sb["small"]["merged"] == 3
    assert sb["small"]["failed"] == 1
    assert sb["medium"]["created"] == 2


@pytest.mark.asyncio
async def test_digest_scope_breakdown_preserves_failure_sub_counts(
    client: AsyncClient, db_session
):
    """scope_breakdown in direction_progress aggregates failed_infra/failed_code_error/failed_scope_overflow."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 1,
                        "tasks_created": 5,
                        "scope_breakdown": {
                            "small": {
                                "created": 5,
                                "merged": 1,
                                "failed": 4,
                                "failed_infra": 1,
                                "failed_code_error": 2,
                                "failed_scope_overflow": 1,
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["small"]
    assert sb["failed_infra"] == 1
    assert sb["failed_code_error"] == 2
    assert sb["failed_scope_overflow"] == 1


@pytest.mark.asyncio
async def test_digest_scope_breakdown_includes_cross_module_and_failure_stages(
    client: AsyncClient, db_session
):
    """scope_breakdown aggregates merged_cross_module and failure_stages per scope."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-xmod",
                "text": "Direction xmod",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "medium": {
                                "created": 3,
                                "merged": 2,
                                "merged_cross_module": 1,
                                "failed": 1,
                                "failure_stages": {"test": 1},
                            },
                        },
                    },
                    {
                        "timestamp": recent_ts2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "scope_breakdown": {
                            "medium": {
                                "created": 2,
                                "merged": 1,
                                "merged_cross_module": 1,
                                "failed": 1,
                                "failure_stages": {"test": 1, "merge": 1},
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-xmod"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["medium"]
    assert sb["merged"] == 3
    assert sb["merged_cross_module"] == 2
    assert sb["failure_stages"] == {"test": 2, "merge": 1}


@pytest.mark.asyncio
async def test_digest_scope_breakdown_includes_efficiency_predictors(
    client: AsyncClient, db_session
):
    """scope_breakdown aggregates weighted expansion/complexity averages and effort_distribution totals."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "D1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "small": {
                                "created": 3,
                                "merged": 2,
                                "failed": 1,
                                "avg_expansion_ratio_merged": 1.0,
                                "avg_expansion_ratio_failed": 3.0,
                                "avg_complexity_merged": 2.0,
                                "avg_complexity_failed": 4.0,
                                "effort_distribution": {
                                    "low": {"created": 2, "merged": 2, "failed": 0},
                                    "high": {"created": 1, "merged": 0, "failed": 1},
                                },
                            },
                        },
                    },
                    {
                        "timestamp": recent_ts2,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "small": {
                                "created": 3,
                                "merged": 2,
                                "failed": 1,
                                "avg_expansion_ratio_merged": 2.0,
                                "avg_complexity_merged": 3.0,
                                "effort_distribution": {
                                    "low": {"created": 3, "merged": 2, "failed": 1},
                                },
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["small"]
    # Weighted by merged counts (2 each cycle): (1.0*2 + 2.0*2)/4 = 1.5
    assert sb["avg_expansion_ratio_merged"] == 1.5
    assert sb["avg_expansion_ratio"] == 1.5
    # Only one cycle had failed expansion ratio: weight=1, value=3.0
    assert sb["avg_expansion_ratio_failed"] == 3.0
    # Complexity merged weighted: (2.0*2 + 3.0*2)/4 = 2.5
    assert sb["avg_complexity_merged"] == 2.5
    assert sb["avg_complexity_failed"] == 4.0
    ed = sb["effort_distribution"]
    assert ed["low"] == {"created": 5, "merged": 4, "failed": 1}
    assert ed["high"] == {"created": 1, "merged": 0, "failed": 1}


@pytest.mark.asyncio
async def test_digest_spot_vs_on_demand_cost_split(client: AsyncClient, db_session):
    """direction_progress and digest response expose spot/on-demand cost split."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Directed: one on-demand, one spot
    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "medium"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    # Undirected spot task contributes to top-level split only
    db_session.add(
        make_task(
            project_id=project.id,
            title="t3",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={},
            vm_provisioned_at=now - timedelta(hours=3),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()
    dp = body["direction_progress"][0]
    assert "spot_cost_usd" in dp
    assert "on_demand_cost_usd" in dp
    assert dp["spot_cost_usd"] > 0
    assert dp["on_demand_cost_usd"] > 0
    assert (
        round(dp["spot_cost_usd"] + dp["on_demand_cost_usd"], 2)
        == dp["estimated_cost_usd"]
    )

    assert "spot_cost_usd" in body
    assert "on_demand_cost_usd" in body
    # Top-level spot includes both directed spot task and undirected spot task
    assert body["spot_cost_usd"] > dp["spot_cost_usd"]
    assert body["on_demand_cost_usd"] == dp["on_demand_cost_usd"]


@pytest.mark.asyncio
async def test_digest_cost_by_failure_class(client: AsyncClient, db_session):
    """Digest response exposes per-failure-class cost breakdown and total failed cost."""
    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="timeout task",
            status=TaskStatus.FAILED,
            latest_status_message="",
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="external api task",
            status=TaskStatus.FAILED,
            latest_status_message="could not connect to server",
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert "cost_by_failure_class" in body
    assert "total_failed_cost_usd" in body
    cbfc = body["cost_by_failure_class"]
    assert "AGENT_TIMEOUT" in cbfc
    assert "EXTERNAL_API" in cbfc
    assert cbfc["AGENT_TIMEOUT"] > 0
    assert cbfc["EXTERNAL_API"] > 0
    assert round(sum(cbfc.values()), 4) == body["total_failed_cost_usd"]


@pytest.mark.asyncio
async def test_digest_scope_conversion_rates_absent_without_project(
    client: AsyncClient, db_session
):
    """scope_conversion_rates is null when no project_id is provided."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["scope_conversion_rates"] is None
    assert data["prev_scope_conversion_rates"] is None


@pytest.mark.asyncio
async def test_digest_prev_scope_conversion_rates(client: AsyncClient, db_session):
    """prev_scope_conversion_rates aggregates scope_breakdown from the previous window."""
    curr_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    prev_ts = (
        _utcnow() - timedelta(hours=36)
    ).isoformat()  # in prev 24h window (24–48h ago)

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": curr_ts,
                        "prs_merged": 2,
                        "tasks_created": 4,
                        "scope_breakdown": {
                            "medium": {"created": 4, "merged": 2, "failed": 0},
                        },
                    },
                    {
                        "timestamp": prev_ts,
                        "prs_merged": 1,
                        "tasks_created": 4,
                        "scope_breakdown": {
                            "medium": {"created": 4, "merged": 1, "failed": 0},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    # Current window rates
    assert data["scope_conversion_rates"]["medium"]["created"] == 4
    assert data["scope_conversion_rates"]["medium"]["merged"] == 2

    # Previous window rates (for trend comparison)
    prev_rates = data["prev_scope_conversion_rates"]
    assert prev_rates is not None
    assert prev_rates["medium"]["created"] == 4
    assert prev_rates["medium"]["merged"] == 1


@pytest.mark.asyncio
async def test_digest_pipeline_filter_funnel_sums_across_directions(
    client: AsyncClient, db_session
):
    """pipeline_filter_funnel sums filter_funnel counters across all directions."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-a",
                "text": "Direction A",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 2,
                        "findings_produced": 5,
                        "batch_cap_applied": 1,
                        "filter_breakdown": {"hash_dedup": 1, "word_overlap_dedup": 1},
                    },
                ],
            },
            {
                "id": "dir-b",
                "text": "Direction B",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 3,
                        "findings_produced": 7,
                        "batch_cap_applied": 2,
                        "filter_breakdown": {"hash_dedup": 2, "type_filtered": 1},
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-a", "dir-b"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Research {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    pff = data["pipeline_filter_funnel"]
    assert pff is not None
    assert pff["findings_produced"] == 12
    assert pff["hash_dedup"] == 3
    assert pff["word_overlap"] == 1
    assert pff["type_filtered"] == 1
    assert pff["batch_cap"] == 3
    assert pff["tasks_created"] == 5


@pytest.mark.asyncio
async def test_digest_review_rejection_summary(client: AsyncClient, db_session):
    """rejection_reasons per direction and aggregate rejection_pattern_summary surface."""
    project = make_project(
        directions=[
            {"id": "dir-rej", "text": "Rejection dir", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Two rejected tasks with identical notes (after normalization) + one unique
    for i, (title, note) in enumerate(
        [
            ("Task A", "Scope too broad; split into smaller PRs"),
            ("Task B", "scope too broad;   split into smaller prs"),
            ("Task C", "Missing tests for new logic"),
        ]
    ):
        db_session.add(
            make_task(
                project_id=project.id,
                title=title,
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "dir-rej",
                    "pr_rejected": True,
                    "pr_rejection_note": note,
                },
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(minutes=30 + i),
            )
        )
    # Non-rejected failure should not count
    db_session.add(
        make_task(
            project_id=project.id,
            title="Unrelated failure",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-rej"},
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=10),
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    rps = data["rejection_pattern_summary"]
    assert rps["total_rejected"] == 3
    top = {n["snippet"]: n["count"] for n in rps["top_notes"]}
    assert top.get("scope too broad; split into smaller prs") == 2
    assert top.get("missing tests for new logic") == 1

    dp = next(d for d in data["direction_progress"] if d["direction_id"] == "dir-rej")
    reasons = dp["rejection_reasons"]
    assert reasons is not None
    assert len(reasons) == 3
    titles = {r["title"] for r in reasons}
    assert titles == {"Task A", "Task B", "Task C"}
    for r in reasons:
        assert "task_id" in r
        assert "rejection_note" in r


@pytest.mark.asyncio
async def test_digest_scope_mismatch_summary(client: AsyncClient, db_session):
    """scope_mismatch_summary aggregates calibrated tasks by original scope and direction."""
    project = make_project(
        directions=[
            {"id": "dir-sm1", "text": "Dir 1", "status": "active"},
            {"id": "dir-sm2", "text": "Dir 2", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm1",
                "scope": "small",
                "scope_calibrated_from": "medium",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm1",
                "scope": "small",
                "scope_calibrated_from": "medium:cross_module",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c3",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-sm1", "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c4",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm2",
                "scope": "medium",
                "scope_calibrated_from": "large",
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    summary = data["scope_mismatch_summary"]
    assert summary is not None
    assert summary["total_calibrated"] == 3
    assert summary["calibrated_success_rate"] == round(2 / 3, 3)

    by_orig = summary["by_original_scope"]
    assert by_orig["medium"]["total"] == 2
    assert by_orig["medium"]["completed"] == 1
    assert by_orig["medium"]["failed"] == 1
    assert by_orig["large"]["total"] == 1
    assert by_orig["large"]["completed"] == 1

    top = summary["top_directions"]
    assert top[0]["direction_id"] == "dir-sm2"
    assert top[0]["calibration_rate"] == 1.0
    assert top[1]["direction_id"] == "dir-sm1"
    assert top[1]["calibrated_count"] == 2
    assert top[1]["total_tasks"] == 3


@pytest.mark.asyncio
async def test_digest_scope_mismatch_summary_empty(client: AsyncClient, db_session):
    """scope_mismatch_summary is None when no tasks have scope_calibrated_from."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="nc",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"scope": "small"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    assert response.json()["scope_mismatch_summary"] is None


@pytest.mark.asyncio
async def test_digest_retry_pattern_summary(client: AsyncClient, db_session):
    """retry_pattern_summary aggregates _failure_class_history across failed tasks."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks with identical repeating pattern (retry didn't help)
    db_session.add(
        make_task(
            project_id=project.id,
            title="r1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="r2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    # One task where retry changed class (not a repeat)
    db_session.add(
        make_task(
            project_id=project.id,
            title="r3",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["PROVISION_FAILURE", "CODE_ERROR"]},
        )
    )
    # Single-entry history — should be ignored
    db_session.add(
        make_task(
            project_id=project.id,
            title="r4",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR"]},
        )
    )
    # Completed task — should be ignored even with history
    db_session.add(
        make_task(
            project_id=project.id,
            title="r5",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    rps = response.json()["retry_pattern_summary"]
    assert rps["total_multi_attempt_failures"] == 3
    assert rps["repeat_class_count"] == 2
    top = rps["top_patterns"]
    assert top[0]["sequence"] == "CODE_ERROR -> CODE_ERROR"
    assert top[0]["count"] == 2
    sequences = {p["sequence"] for p in top}
    assert "PROVISION_FAILURE -> CODE_ERROR" in sequences


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary(client: AsyncClient, db_session):
    """review_cap_exhaustion_summary counts FAILED tasks with failure_detail=review_cap_exhausted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 16,
                "direction_id": "d-aaa",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 12,
                "direction_id": "d-aaa",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c3",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 8,
                "direction_id": "d-bbb",
            },
        )
    )
    # Unrelated FAILED task — should NOT be counted
    db_session.add(
        make_task(
            project_id=project.id,
            title="other",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"failure_detail": "something_else", "direction_id": "d-ccc"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    assert summary["count"] == 3
    assert summary["avg_iterations"] == pytest.approx((16 + 12 + 8) / 3)
    top = summary["top_directions"]
    assert top[0] == {"direction_id": "d-aaa", "count": 2}
    dirs = {d["direction_id"] for d in top}
    assert "d-bbb" in dirs
    assert "d-ccc" not in dirs
    assert "total_cost_usd" in summary
    assert "avg_cost_usd" in summary


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary_cost(
    client: AsyncClient, db_session
):
    """review_cap_exhaustion_summary.total_cost_usd sums VM cost for cap-exhausted tasks."""
    from app.routers.admin_analytics import SPOT_RATE, ON_DEMAND_RATE
    from datetime import datetime

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 1 hour spot task
    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 16,
                "direction_id": "d-aaa",
            },
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    # 30 min on-demand task
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 12,
                "direction_id": "d-aaa",
            },
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    expected = SPOT_RATE * 3600 + ON_DEMAND_RATE * 1800
    assert summary["total_cost_usd"] == pytest.approx(round(expected, 4))
    assert summary["avg_cost_usd"] == pytest.approx(round(expected / 2, 4))


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary_empty(
    client: AsyncClient, db_session
):
    """review_cap_exhaustion_summary returns zero count and None avg when no cap exhaustion."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="ok",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    assert summary["count"] == 0
    assert summary["avg_iterations"] is None
    assert summary["top_directions"] == []
    assert summary["total_cost_usd"] == 0.0
    assert summary["avg_cost_usd"] is None


@pytest.mark.asyncio
async def test_failure_stage_distribution_aggregates_stages(
    client: AsyncClient, db_session
):
    """failure_stage_distribution tallies _previous_failure_stage and falls back to _derive_failure_stage."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="ctx stage",
            status=TaskStatus.FAILED,
            context={"_previous_failure_stage": "test"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="derived lint",
            status=TaskStatus.FAILED,
            latest_status_message="ruff lint error in handler",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="derived lint 2",
            status=TaskStatus.FAILED,
            latest_status_message="eslint failure",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="provision",
            status=TaskStatus.FAILED,
            context={"failure_category": "clone_failed"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    dist = data["failure_stage_distribution"]
    assert dist["stages"]["test"] == 1
    assert dist["stages"]["lint"] == 2
    assert dist["stages"]["provision"] == 1
    assert dist["total_classified"] == 4
    assert dist["dominant_stage"] == "lint"


@pytest.mark.asyncio
async def test_digest_retry_overhead_cost_computation(client: AsyncClient, db_session):
    """Digest response surfaces retry_overhead_cost_usd, retry_task_count,
    and avg_retries_before_failure for failed tasks with _attempt_number > 1."""
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Retry on-demand: attempt 2, 1h of VM time (in-window)
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry on-demand",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 2},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    # Retry spot: attempt 3, 30m of VM time (in-window)
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry spot",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 3},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    # First-attempt failure: must be excluded from retry counts
    db_session.add(
        make_task(
            project_id=project.id,
            title="first attempt",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 1},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    # Retry with no VM timing: counts toward retry_task_count but not cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry no-vm",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 4},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert body["retry_task_count"] == 3  # attempts 2, 3, 4 (attempt 1 excluded)
    # avg retries = (1 + 2 + 3) / 3 = 2.0 (attempt_number - 1 for each)
    assert body["avg_retries_before_failure"] == pytest.approx(2.0)

    expected_cost = ON_DEMAND_RATE * 3600 + SPOT_RATE * 1800
    assert body["retry_overhead_cost_usd"] == pytest.approx(round(expected_cost, 4))


@pytest.mark.asyncio
async def test_digest_retry_recovery_cost_from_prior_attempts(
    client: AsyncClient, db_session
):
    """retry_recovery_cost_usd sums _prior_attempts_cost across completed tasks
    with _attempt_number > 1 — the compute burned on failed attempts that
    eventually succeeded via retry."""
    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Successful retry with recorded prior-attempt cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="succeeded on retry",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 2, "_prior_attempts_cost": 0.50},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    # Successful retry with multi-attempt accumulated cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="succeeded on 3rd",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 3, "_prior_attempts_cost": 1.25},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=15),
            vm_spot=True,
        )
    )
    # First-attempt success — excluded (no prior cost)
    db_session.add(
        make_task(
            project_id=project.id,
            title="first attempt success",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 1, "_prior_attempts_cost": 9.0},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert body["retry_recovery_cost_usd"] == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_compute_direction_progress_cost_by_provider_model_uses_attempt_history(
    client: AsyncClient, db_session
):
    """cost_by_provider_model in direction_progress reflects attempt_history,
    not a chimera key from independently-resolved Task.provider/Task.model columns.

    Sets up two tasks:
    - One with banked attempt_history entries (provider-switch retry) — no live span.
    - One with only a live span (first attempt) under a distinct provider pair.

    Asserts:
    (a) No chimera key appears.
    (b) Each attempt's cost lands under its OWN recorded pair.
    (c) Banked earlier-attempt cost is NOT lost.
    """
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-pm", "text": "D", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    attempt1_cost = 0.010

    # Task that retried: attempt 1 (anthropic/claude-opus-4-7) banked in
    # attempt_history, final attempt 2 (openai/gpt-5.4) still has a live span
    # (banking only clears vm_provisioned_at/stopped_at for intermediate attempts).
    retried = make_task(
        project_id=project.id,
        title="retried-pm",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "scope": "small"}],
        context={"direction_id": "dir-pm"},
        provider="openai",
        model="gpt-5.4",
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    )
    retried.attempt_history = [
        {
            "vm_provisioned_at": (now - timedelta(hours=3)).isoformat(),
            "vm_stopped_at": (now - timedelta(hours=2)).isoformat(),
            "vm_spot": False,
            "vm_machine_type": "e2-standard-2",
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "vm_runtime_cost_usd": attempt1_cost,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_call_count": 0,
        },
    ]
    db_session.add(retried)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()
    dp_list = body.get("direction_progress", [])
    assert dp_list, "direction_progress must be present"
    dp = dp_list[0]
    by_pm = dp.get("cost_by_provider_model") or {}

    # (a) No chimera keys
    assert "anthropic|gpt-5.4" not in by_pm, "chimera key must not appear"
    assert "openai|claude-opus-4-7" not in by_pm, "chimera key must not appear"

    # (b) Each attempt's own pair present
    assert "anthropic|claude-opus-4-7" in by_pm, "attempt-1 pair must appear"
    assert "openai|gpt-5.4" in by_pm, "attempt-2 pair must appear"

    # (c) Banked earlier-attempt cost not lost
    assert by_pm["anthropic|claude-opus-4-7"] == pytest.approx(attempt1_cost, abs=1e-4)
    # Live span for openai|gpt-5.4 computed by the helper; just verify it's > 0
    assert by_pm["openai|gpt-5.4"] > 0


@pytest.mark.asyncio
async def test_digest_escalation_outcome_summary(client: AsyncClient, db_session):
    """escalation_outcome_summary aggregates escalation_outcomes across directions and recent history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-e1",
                "text": "Dir 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 4,
                                "merged": 2,
                                "failed": 2,
                            },
                            "medium_to_large": {
                                "attempted": 1,
                                "merged": 0,
                                "failed": 1,
                            },
                        },
                    },
                    {
                        "timestamp": old_ts,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 99,
                                "merged": 99,
                                "failed": 0,
                            },
                        },
                    },
                ],
            },
            {
                "id": "dir-e2",
                "text": "Dir 2",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts2,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 2,
                                "merged": 1,
                                "failed": 1,
                            },
                            "small_to_large": {
                                "attempted": 1,
                                "merged": 1,
                                "failed": 0,
                            },
                        },
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["escalation_outcome_summary"]
    assert summary is not None
    assert summary["total_attempted"] == 8
    assert summary["total_merged"] == 4
    assert summary["total_failed"] == 4
    assert summary["overall_success_rate"] == 0.5

    by_path = summary["by_path"]
    assert by_path["small_to_medium"] == {
        "attempted": 6,
        "merged": 3,
        "failed": 3,
        "success_rate": 0.5,
    }
    assert by_path["small_to_large"]["attempted"] == 1
    assert by_path["small_to_large"]["success_rate"] == 1.0
    assert by_path["medium_to_large"]["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_digest_remediation_funnel(client: AsyncClient, db_session):
    """investigate + remediation tasks surface in digest.remediation_funnel."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    inv_created = now - timedelta(hours=3)
    rem_completed = now - timedelta(hours=1)

    # Completed investigation with a parseable diagnosis summary
    inv_done = make_task(
        project_id=project.id,
        title="investigate A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=inv_created,
        plan_output={"summary": "root cause X"},
    )
    # In-flight investigation (counts as active)
    inv_active = make_task(
        project_id=project.id,
        title="investigate B",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
    )
    # Completed investigation without summary (counts but not with_diagnosis)
    inv_no_summary = make_task(
        project_id=project.id,
        title="investigate C",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=4),
        plan_output={"raw": "logs"},
    )
    # Investigation used to validate the fallback path (rem_merged2 has no
    # trigger_failure_class in its own context, so digest should fall back to
    # this investigation's context.last_failure_class).
    inv_fallback = make_task(
        project_id=project.id,
        title="investigate D",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={"summary": "root cause Y"},
        context={"last_failure_class": "LINT_ERROR"},
    )
    db_session.add_all([inv_done, inv_active, inv_no_summary, inv_fallback])
    await db_session.commit()
    await db_session.refresh(inv_done)
    await db_session.refresh(inv_fallback)

    rem_merged = make_task(
        project_id=project.id,
        title="remediation A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_done.id),
            "direction_id": "d1",
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
            "landed_on_integration": True,
        },
        status_log=[{"t": rem_completed.isoformat(), "status": "completed"}],
    )
    # Second merged remediation: no trigger_failure_class in its own context,
    # but its linked investigation has last_failure_class — exercises fallback.
    rem_merged_fallback = make_task(
        project_id=project.id,
        title="remediation C",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_fallback.id),
            "direction_id": "d1",
            "pr_merged": True,
            "landed_on_integration": True,
        },
        status_log=[{"t": rem_completed.isoformat(), "status": "completed"}],
    )
    rem_failed = make_task(
        project_id=project.id,
        title="remediation B",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={"diagnosis_task_id": str(inv_done.id), "direction_id": "d1"},
    )
    db_session.add_all([rem_merged, rem_merged_fallback, rem_failed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    funnel = response.json()["remediation_funnel"]

    assert funnel["investigations_spawned"] == 4
    assert funnel["investigations_completed"] == 3
    assert funnel["investigations_with_diagnosis"] == 2
    assert funnel["active_investigations"] == 1
    assert funnel["remediations_spawned"] == 3
    assert funnel["remediations_merged"] == 2
    assert funnel["remediations_failed"] == 1
    # inv_done created 3h ago, rem_merged completed 1h ago → ~2h
    assert funnel["avg_time_to_remediation_hours"] is not None
    assert 1.5 <= funnel["avg_time_to_remediation_hours"] <= 2.5
    # rem_merged has trigger_failure_class=CODE_ERROR directly;
    # rem_merged_fallback has no trigger_failure_class but its linked
    # investigation has context.last_failure_class=LINT_ERROR.
    assert funnel["merged_remediations_by_trigger_class"] == {
        "CODE_ERROR": 1,
        "LINT_ERROR": 1,
    }
    # Per-class investigation stage counts: inv_fallback has
    # context.last_failure_class=LINT_ERROR; the other three investigations
    # have no class set and bucket into "unknown".
    assert funnel["investigations_spawned_by_class"] == {
        "LINT_ERROR": 1,
        "unknown": 3,
    }
    # Two completed with no class (inv_done, inv_no_summary) and one completed
    # with LINT_ERROR (inv_fallback). inv_active is IN_PROGRESS so excluded.
    assert funnel["investigations_completed_by_class"] == {
        "LINT_ERROR": 1,
        "unknown": 2,
    }
    # rem_merged has trigger CODE_ERROR; the other two remediations have
    # no trigger class set → "unknown".
    assert funnel["remediations_spawned_by_class"] == {
        "CODE_ERROR": 1,
        "unknown": 2,
    }


@pytest.mark.asyncio
async def test_compute_remediation_funnel_helper_direct(db_session):
    """compute_remediation_funnel computes the funnel directly off a session.

    Exercises the extracted helper in isolation (no FastAPI layer) and confirms
    the residual_remediation_funnel passed in lands verbatim in the returned dict
    at its expected key.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)

    inv_done = make_task(
        project_id=project.id,
        title="investigate A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={
            "summary": "root cause X",
            "root_cause": "null payload field",
            "recommended_fix": "add a null guard before the access",
            "confidence": "high",
        },
        context={"last_failure_class": "CODE_ERROR"},
    )
    inv_active = make_task(
        project_id=project.id,
        title="investigate B",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
    )
    db_session.add_all([inv_done, inv_active])
    await db_session.commit()
    await db_session.refresh(inv_done)

    rem_merged = make_task(
        project_id=project.id,
        title="remediation A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_done.id),
            "direction_id": "d1",
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
            "landed_on_integration": True,
        },
        status_log=[
            {"t": (now - timedelta(hours=1)).isoformat(), "status": "completed"}
        ],
    )
    # COMPLETED remediation whose PR never landed on the integration branch:
    # spawned, but NOT merged. It must inflate "spawned" without inflating
    # "merged" (the name-vs-definition mismatch this test guards).
    rem_completed_not_landed = make_task(
        project_id=project.id,
        title="remediation A2 (completed, not landed)",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_done.id),
            "direction_id": "d1",
            "trigger_failure_class": "CODE_ERROR",
        },
    )
    db_session.add_all([rem_merged, rem_completed_not_landed])
    await db_session.commit()

    sentinel_residual = {"spawned": 7, "marker": "residual-stays-here"}
    funnel, investigation_conversion = await compute_remediation_funnel(
        db_session,
        project_id=project.id,
        window_start=window_start,
        residual_remediation_funnel=sentinel_residual,
    )

    assert funnel["investigations_spawned"] == 2
    assert funnel["investigations_completed"] == 1
    assert funnel["investigations_with_diagnosis"] == 1
    assert funnel["active_investigations"] == 1
    # Both completed remediations are spawned, but only the one that actually
    # landed on the integration branch counts as merged.
    assert funnel["remediations_spawned"] == 2
    assert funnel["remediations_merged"] == 1
    assert funnel["remediations_failed"] == 0
    assert funnel["merged_remediations_by_trigger_class"] == {"CODE_ERROR": 1}
    assert funnel["investigations_spawned_by_class"] == {
        "CODE_ERROR": 1,
        "unknown": 1,
    }
    # The caller-supplied residual funnel is spliced through unchanged.
    assert funnel["residual_remediation_funnel"] is sentinel_residual
    # Active-investigation detail surfaces the in-flight investigation only.
    assert [d["task_id"] for d in funnel["active_investigations_detail"]] == [
        str(inv_active.id)
    ]
    # investigation_conversion is returned separately as a top-level digest key.
    assert investigation_conversion["total_completed"] == 1
    assert investigation_conversion["parseable"] == 1
    assert investigation_conversion["actionable"] == 1
    assert investigation_conversion["remediation_spawned"] == 1


@pytest.mark.asyncio
async def test_compute_remediation_and_patterns_helper_direct(db_session):
    """compute_remediation_and_patterns returns all four expected top-level keys.

    Exercises the extracted helper in isolation (no FastAPI layer) and confirms
    it returns a dict with remediation_funnel, investigation_conversion,
    recurring_patterns, and early_warnings.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)

    result = await compute_remediation_and_patterns(
        session=db_session,
        project_id=project.id,
        window_start=window_start,
        failed_tasks=[],
        all_tasks=[],
        dir_id_to_text={},
    )

    assert set(result.keys()) == {
        "remediation_funnel",
        "investigation_conversion",
        "recurring_patterns",
        "early_warnings",
    }
    assert isinstance(result["recurring_patterns"], list)
    assert isinstance(result["early_warnings"], list)
    assert isinstance(result["remediation_funnel"], dict)
    assert isinstance(result["investigation_conversion"], dict)


@pytest.mark.asyncio
async def test_digest_cumulative_merged_by_trigger_class(
    client: AsyncClient, db_session
):
    """cumulative_merged_by_trigger_class counts across all time; per-window counts only in-window."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    inv_in = make_task(
        project_id=project.id,
        title="investigate in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={"summary": "rc"},
    )
    db_session.add(inv_in)
    await db_session.commit()
    await db_session.refresh(inv_in)

    # In-window diagnosis-driven remediation
    rem_in = make_task(
        project_id=project.id,
        title="rem in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_in.id),
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
        },
    )
    # Out-of-window diagnosis-driven remediation
    rem_old = make_task(
        project_id=project.id,
        title="rem out-of-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(days=5),
        context={
            "diagnosis_task_id": "00000000-0000-0000-0000-000000000001",
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
        },
    )
    # In-window CI auto-remediation
    ci_in = make_task(
        project_id=project.id,
        title="ci in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "LINT_ERROR",
            "pr_merged": True,
        },
    )
    # Out-of-window CI auto-remediation
    ci_old = make_task(
        project_id=project.id,
        title="ci out-of-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(days=10),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "LINT_ERROR",
            "pr_merged": True,
        },
    )
    # COMPLETED CI auto-remediation WITHOUT pr_merged — should NOT be counted
    ci_completed_not_merged = make_task(
        project_id=project.id,
        title="ci completed not merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={"auto_remediation": True, "trigger_failure_class": "LINT_ERROR"},
    )
    db_session.add_all([rem_in, rem_old, ci_in, ci_old, ci_completed_not_merged])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    funnel = response.json()["remediation_funnel"]

    # Per-window only sees rem_in (diagnosis-driven, in-window)
    assert funnel["merged_remediations_by_trigger_class"] == {"CODE_ERROR": 1}
    # Cumulative includes both diagnosis-driven and CI auto-remediation, all time
    assert funnel["cumulative_merged_by_trigger_class"] == {
        "CODE_ERROR": 2,
        "LINT_ERROR": 2,
    }


@pytest.mark.asyncio
async def test_digest_ci_auto_remediation(client: AsyncClient, db_session):
    """CI auto-remediation tasks (no diagnosis_task_id) surface in ci_auto_remediation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    ci_merged = make_task(
        project_id=project.id,
        title="ci auto-remediation merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "test",
            "pr_merged": True,
        },
    )
    ci_completed_not_merged = make_task(
        project_id=project.id,
        title="ci auto-remediation completed but not merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "test",
        },
    )
    ci_failed = make_task(
        project_id=project.id,
        title="ci auto-remediation failed",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "build",
        },
    )
    db_session.add_all([ci_merged, ci_completed_not_merged, ci_failed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    ci = response.json()["remediation_funnel"]["ci_auto_remediation"]

    assert ci["spawned"] == 3
    assert ci["merged"] == 1
    assert ci["failed"] == 1
    assert ci["by_ci_failure_type"]["test"] == {"spawned": 2, "merged": 1, "failed": 0}
    assert ci["by_ci_failure_type"]["build"] == {"spawned": 1, "merged": 0, "failed": 1}


@pytest.mark.asyncio
async def test_digest_includes_first_review_approval_rate(
    client: AsyncClient, db_session
):
    """/api/admin/digest returns first_review_approval_rate with expected structure."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Merged create task approved on first review (no rejection categories).
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "pr_merged": True,
                "scope": "small",
                "_had_playbook": True,
                "_had_review_measurements": True,
            },
        )
    )
    # Merged refactor task rejected on first review.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "refactor"}],
            context={
                "pr_merged": True,
                "scope": "medium",
                "_had_playbook": False,
                "_had_review_measurements": False,
                "_first_attempt_rejection_categories": ["correctness"],
            },
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    fra = data["first_review_approval_rate"]
    assert set(fra.keys()) >= {
        "overall",
        "sample_size",
        "by_type",
        "by_scope",
        "with_playbook",
        "without_playbook",
        "with_measurements",
        "without_measurements",
    }
    assert fra["sample_size"] == 2
    assert fra["overall"] == 0.5
    assert fra["by_type"]["create"] == 1.0
    assert fra["by_type"]["refactor"] == 0.0
    assert fra["by_scope"]["small"] == 1.0
    assert fra["by_scope"]["medium"] == 0.0
    assert fra["with_playbook"] == 1.0
    assert fra["without_playbook"] == 0.0
    assert fra["with_measurements"] == 1.0
    assert fra["without_measurements"] == 0.0


@pytest.mark.asyncio
async def test_digest_prior_investigation_reuse(client: AsyncClient, db_session):
    """CI auto-remediation tasks with prior_diagnosis_task_id surface in prior_investigation_reuse."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    with_prior_merged = make_task(
        project_id=project.id,
        title="ci rem with prior merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=3),
        context={
            "auto_remediation": True,
            "prior_diagnosis_task_id": "inv-1",
            "ci_failure_type": "test",
            "pr_merged": True,
            "landed_on_integration": True,
        },
    )
    with_prior_failed = make_task(
        project_id=project.id,
        title="ci rem with prior failed",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "prior_diagnosis_task_id": "inv-2",
            "ci_failure_type": "build",
        },
    )
    without_prior = make_task(
        project_id=project.id,
        title="ci rem no prior",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "ci_failure_type": "test",
        },
    )
    db_session.add_all([with_prior_merged, with_prior_failed, without_prior])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    reuse = response.json()["remediation_funnel"]["prior_investigation_reuse"]

    assert reuse["total_with_prior"] == 2
    assert reuse["merged_with_prior"] == 1
    assert reuse["failed_with_prior"] == 1
    assert reuse["merge_rate"] == 0.5


@pytest.mark.asyncio
async def test_digest_investigation_conversion_breakdown(
    client: AsyncClient, db_session
):
    """investigate tasks categorize into structural/low/actionable/unparseable buckets."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    inv_structural = make_task(
        project_id=project.id,
        title="inv structural",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=5),
        plan_output={
            "summary": "scope problem",
            "root_cause": "direction scope is too broad",
            "recommended_fix": "narrow the direction",
            "confidence": "high",
        },
    )
    inv_low = make_task(
        project_id=project.id,
        title="inv low confidence",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=4),
        plan_output={
            "summary": "unclear",
            "root_cause": "flaky test",
            "recommended_fix": "retry",
            "confidence": "low",
        },
    )
    inv_actionable = make_task(
        project_id=project.id,
        title="inv actionable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={
            "summary": "null deref",
            "root_cause": "missing null check",
            "recommended_fix": "add guard clause in foo()",
            "confidence": "high",
        },
    )
    inv_unparseable = make_task(
        project_id=project.id,
        title="inv unparseable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
        plan_output={"raw": "garbage logs"},
    )
    inv_nofix = make_task(
        project_id=project.id,
        title="inv no recommended fix",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=1),
        plan_output={
            "summary": "something",
            "root_cause": "unknown",
            "recommended_fix": "",
            "confidence": "medium",
        },
    )
    db_session.add_all(
        [inv_structural, inv_low, inv_actionable, inv_unparseable, inv_nofix]
    )
    await db_session.commit()
    await db_session.refresh(inv_actionable)

    rem_child = make_task(
        project_id=project.id,
        title="remediation of actionable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={"diagnosis_task_id": str(inv_actionable.id), "direction_id": "d1"},
    )
    db_session.add(rem_child)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    ic = response.json()["investigation_conversion"]

    assert ic["total_completed"] == 5
    assert ic["parseable"] == 4
    assert ic["unparseable"] == 1
    assert ic["structural"] == 1
    assert ic["low_confidence"] == 1
    assert ic["actionable"] == 1
    assert ic["remediation_spawned"] == 1

    drop_off = response.json()["remediation_funnel"]["diagnosis_drop_off_by_reason"]
    assert drop_off.get("structural") == 1
    assert drop_off.get("low_confidence") == 1
    assert drop_off.get("no_recommended_fix") == 1
    assert "no_direction" not in drop_off


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_positive(client: AsyncClient, db_session):
    """prevention_efficacy reports positive when preventive merge rate > overall."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p1",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 10,
                        "prs_merged": 3,
                        "preventive_context_tasks_created": 4,
                        "preventive_context_tasks_merged": 3,
                        "preventive_context_tasks_failed": 1,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_tasks_created"] == 4
    assert eff["preventive_tasks_merged"] == 3
    assert eff["preventive_tasks_failed"] == 1
    assert eff["preventive_merge_rate"] == 0.75
    assert eff["overall_merge_rate"] == 0.3
    assert eff["assessment"] == "positive"


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_neutral(client: AsyncClient, db_session):
    """prevention_efficacy reports neutral when rates are similar."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p2",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 10,
                        "prs_merged": 5,
                        "preventive_context_tasks_created": 4,
                        "preventive_context_tasks_merged": 2,
                        "preventive_context_tasks_failed": 2,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_merge_rate"] == 0.5
    assert eff["overall_merge_rate"] == 0.5
    assert eff["assessment"] == "neutral"


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_no_data(client: AsyncClient, db_session):
    """prevention_efficacy returns no_data assessment when zero preventive tasks."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p3",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 5,
                        "prs_merged": 2,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_tasks_created"] == 0
    assert eff["preventive_merge_rate"] is None
    assert eff["overall_merge_rate"] == 0.4
    assert eff["assessment"] == "no_data"


@pytest.mark.asyncio
async def test_digest_anomaly_adjustment_inactive_by_default(client: AsyncClient):
    """When no anomaly throttle is active, digest exposes an inactive snapshot."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    adj = response.json()["anomaly_adjustment"]
    assert adj["active"] is False
    assert adj["trigger_anomalies"] == []


@pytest.mark.asyncio
async def test_digest_includes_anomaly_adjustment_when_active(client: AsyncClient):
    """When anomaly_adjustments is active, digest reflects trigger + reduction info."""
    from app import shared_state

    prev = dict(shared_state.anomaly_adjustments)
    try:
        shared_state.anomaly_adjustments.update(
            {
                "active": True,
                "adjusted_at": "2026-04-20T12:00:00",
                "trigger_anomalies": ["PROVISION_FAILURE_SURGE", "EXTERNAL_API_SURGE"],
                "pre_adjustment_max_vms": 20,
                "adjusted_max_vms": 8,
                "failure_gate_threshold": 0.6,
                "pre_adjustment_failure_gate": 0.8,
                "forced_restoration_count": 1,
                "forced_restored_at": "2026-04-20T11:00:00",
            }
        )
        response = await client.get("/api/admin/digest")
        assert response.status_code == 200
        adj = response.json()["anomaly_adjustment"]
        assert adj["active"] is True
        assert adj["adjusted_at"] == "2026-04-20T12:00:00"
        assert adj["trigger_anomalies"] == [
            "PROVISION_FAILURE_SURGE",
            "EXTERNAL_API_SURGE",
        ]
        assert adj["pre_adjustment_max_vms"] == 20
        assert adj["adjusted_max_vms"] == 8
        assert adj["failure_gate_threshold"] == 0.6
        assert adj["pre_adjustment_failure_gate"] == 0.8
        assert adj["forced_restoration_count"] == 1
    finally:
        shared_state.anomaly_adjustments.clear()
        shared_state.anomaly_adjustments.update(prev)


@pytest.mark.asyncio
async def test_digest_uses_first_payload_projection(client: AsyncClient, db_session):
    """Digest query projects payloads[0] SQL-side instead of loading the full array.

    Regression guard for the OOM fix: even with a multi-element payloads array,
    only payloads[0] should drive first_payload-keyed metrics (research_cycles,
    task_type classification), matching the Task.first_payload property.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Multi-payload research task",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "research"},
            {"type": "create"},
            {"type": "create"},
        ],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["research_cycles"] == 1
    assert data["completed_count"] == 1


@pytest.mark.asyncio
async def test_digest_derives_completed_at_from_status_log_projection(
    client: AsyncClient,
    db_session,
):
    """_derive_completed_at reads the SQL-projected status_log[-1]->>'t' timestamp.

    Place a task's status_log completion timestamp OUTSIDE the default 24h window
    but its created_at INSIDE the extended lookback; the digest should still
    include it in recent_completions if projection is wired correctly and exclude
    it if the completion timestamp actually fell outside the window.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Completion timestamp 12h ago — well within the default 24h window.
    recent_complete = (now - timedelta(hours=12)).isoformat()
    task_in_window = make_task(
        project_id=project.id,
        title="Completed in window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=24),
        status_log=[{"t": recent_complete, "status": "COMPLETED"}],
    )
    # Completion timestamp 40h ago — outside the 24h window, should not count.
    old_complete = (now - timedelta(hours=40)).isoformat()
    task_out_of_window = make_task(
        project_id=project.id,
        title="Completed out of window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=48),
        status_log=[{"t": old_complete, "status": "COMPLETED"}],
    )
    db_session.add(task_in_window)
    db_session.add(task_out_of_window)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()
    titles = {c["title"] for c in data["recent_completions"]}
    assert "Completed in window" in titles
    assert "Completed out of window" not in titles


@pytest.mark.asyncio
async def test_digest_handles_empty_payloads_and_status_log(
    client: AsyncClient, db_session
):
    """Empty payloads and status_log must not crash the projection path.

    Edge case: SQL `payloads[0]` returns NULL for empty array; `status_log[-1]->>'t'`
    returns NULL too. Downstream must fall back gracefully.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Empty JSONB task",
        status=TaskStatus.COMPLETED,
        payloads=[],
        status_log=[],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 1


@pytest.mark.asyncio
async def test_stash_projected_task_fields_works_for_orm_loaded_tasks(db_session):
    """Transient projection fields must attach safely to ORM-loaded SQLModel rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Projected task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        status_log=[],
    )
    db_session.add(task)
    await db_session.commit()

    loaded = (
        await db_session.execute(select(Task).where(Task.id == task.id))
    ).scalar_one()
    projected_completed_at = (_utcnow() - timedelta(hours=1)).isoformat()

    _stash_projected_task_fields(
        loaded,
        first_payload={"type": "research"},
        completed_at_ts=projected_completed_at,
    )

    assert loaded.first_payload == {"type": "research"}

    # The durable Task.completed_at column is preferred over the SQL-projected
    # status_log tail: with completed_at stamped (by the terminal insert), the
    # projection is ignored so a post-terminal status_log write cannot move the
    # derived time.
    assert loaded.completed_at is not None
    assert _derive_completed_at(loaded) == loaded.completed_at

    # Legacy rows (completed_at never stamped) fall back to the projection —
    # the transient stash still drives the derived time on that path.
    loaded.completed_at = None
    assert _derive_completed_at(loaded) == datetime.fromisoformat(
        projected_completed_at
    )


@pytest.mark.asyncio
async def test_residual_fingerprints_empty(client: AsyncClient):
    """No tasks → empty residuals list."""
    response = await client.get("/api/admin/residual-fingerprints")
    assert response.status_code == 200
    data = response.json()
    assert data["total_code_error_count"] == 0
    assert data["residual_count"] == 0
    assert data["top_residuals"] == []


@pytest.mark.asyncio
async def test_residual_fingerprints_clusters_code_errors(
    client: AsyncClient, db_session
):
    """FAILED tasks without failure_category cluster by fingerprint; excludes non-CODE_ERROR."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks with nearly identical message — same fingerprint cluster
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                latest_status_message="compilation failed: unexpected token in expression",
            )
        )
    # One task with a different message — second fingerprint
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="assertion error: value mismatch in test suite",
        )
    )
    # Task with failure_category set — excluded from residuals
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="compilation failed: unexpected token in expression",
            context={"failure_category": "CODE_ERROR"},
        )
    )
    # PROVISION_FAILURE — classify_failure returns non-CODE_ERROR, excluded
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="quota exceeded: cpus_all_regions limit reached",
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/residual-fingerprints")
    assert response.status_code == 200
    data = response.json()

    assert (
        data["total_code_error_count"] == 3
    )  # 2 + 1, excludes categorized + provision
    assert data["residual_count"] == 2
    top = data["top_residuals"]
    assert len(top) == 2
    # First entry has the highest count
    assert top[0]["count"] == 2
    assert top[1]["count"] == 1
    entry = top[0]
    assert "fingerprint" in entry
    assert "most_recent_at" in entry
    assert "sample_message" in entry
    assert isinstance(entry["example_task_ids"], list)
    assert len(entry["example_task_ids"]) <= 3


@pytest.mark.asyncio
async def test_residual_fingerprints_requires_auth(unauthenticated_client: AsyncClient):
    """Endpoint requires admin auth."""
    response = await unauthenticated_client.get("/api/admin/residual-fingerprints")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_clear_residual_fingerprints_empties_store(client: AsyncClient):
    """POST /residual-fingerprints/clear wipes the in-memory store and seen-task set."""
    from app.failure_classification import _residual_store, _residual_seen_task_ids

    _residual_store.clear()
    _residual_seen_task_ids.clear()
    _residual_store["fp-aaa"] = {"count": 2, "sample_message": "error: foo"}
    _residual_store["fp-bbb"] = {"count": 1, "sample_message": "error: bar"}
    _residual_seen_task_ids.add("task-id-x")

    response = await client.post("/api/admin/residual-fingerprints/clear")
    assert response.status_code == 200
    data = response.json()
    assert data["cleared"] == 2
    assert len(_residual_store) == 0
    assert len(_residual_seen_task_ids) == 0


@pytest.mark.asyncio
async def test_clear_residual_fingerprints_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Clear endpoint requires admin auth."""
    csrf_token = "test-csrf-token"
    # Set the CSRF cookie on the client instance (per-request cookies= is
    # deprecated in httpx 0.27+); the matching header clears CSRF so the request
    # reaches the admin-auth check this test asserts on.
    unauthenticated_client.cookies.set("csrf_token", csrf_token, domain="test")
    response = await unauthenticated_client.post(
        "/api/admin/residual-fingerprints/clear",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_ignore_residual_case_creates_db_row(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/ignore creates an IGNORED row and returns it."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-abc"
    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/ignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "IGNORED"
    assert "id" in data


@pytest.mark.asyncio
async def test_ignore_residual_case_idempotent(client: AsyncClient, db_session):
    """Calling ignore twice for the same fingerprint/project is idempotent."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-dup"
    for _ in range(2):
        response = await client.post(
            f"/api/admin/residual-cases/{fingerprint}/ignore",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "IGNORED"


@pytest.mark.asyncio
async def test_ignore_residual_case_requires_auth(unauthenticated_client: AsyncClient):
    """Ignore endpoint requires admin auth."""
    csrf_token = "test-csrf-token"
    # Set the CSRF cookie on the client instance (per-request cookies= is
    # deprecated in httpx 0.27+); the matching header clears CSRF so the request
    # reaches the admin-auth check this test asserts on.
    unauthenticated_client.cookies.set("csrf_token", csrf_token, domain="test")
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/ignore",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unignore_residual_case_reactivates(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/unignore sets an IGNORED case back to ACTIVE."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-unignore"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.IGNORED,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "ACTIVE"
    assert "id" in data


@pytest.mark.asyncio
async def test_unignore_residual_case_404_when_missing(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/unignore returns 404 when no case row exists."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/residual-cases/nonexistent-fp/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unignore_residual_case_404_when_not_ignored(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unignore returns 404 when case is not IGNORED."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-active"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.ACTIVE,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unignore_residual_case_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Unignore endpoint requires admin auth."""
    csrf_token = "test-csrf-token"
    # Set the CSRF cookie on the client instance (per-request cookies= is
    # deprecated in httpx 0.27+); the matching header clears CSRF so the request
    # reaches the admin-auth check this test asserts on.
    unauthenticated_client.cookies.set("csrf_token", csrf_token, domain="test")
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/unignore",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unsuspend_residual_case_clears_state(
    client: AsyncClient, db_session, mocker
):
    """POST /residual-cases/{fp}/unsuspend transitions SUSPENDED→ACTIVE and clears fields."""
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-unsuspend"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.SUSPENDED,
        suspended_at=_utcnow(),
        consecutive_failure_count=3,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "ACTIVE"
    assert data["consecutive_failure_count"] == 0
    assert data["suspended_at"] is None
    assert "id" in data

    # Verify DB row was updated
    await db_session.refresh(case)
    assert case.status == ResidualCaseStatus.ACTIVE
    assert case.consecutive_failure_count == 0
    assert case.suspended_at is None


@pytest.mark.asyncio
async def test_unsuspend_residual_case_404_when_missing(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unsuspend returns 404 when no case row exists."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/residual-cases/nonexistent-fp/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsuspend_residual_case_404_when_not_suspended(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unsuspend returns 404 when case is ACTIVE (not SUSPENDED)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-not-suspended"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.ACTIVE,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsuspend_residual_case_emits_message_and_webhook(
    client: AsyncClient, db_session, mocker
):
    """POST /residual-cases/{fp}/unsuspend creates a ProjectMessage and fires the webhook."""
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-webhook-test"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.SUSPENDED,
        suspended_at=_utcnow(),
        consecutive_failure_count=3,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200

    # Verify a ProjectMessage with the correct event_type was created
    from app.models import ProjectMessage

    result = await db_session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
        )
    )
    messages = result.scalars().all()
    unsuspend_msgs = [
        m
        for m in messages
        if (m.context or {}).get("event_type") == "residual_fix_unsuspended"
    ]
    assert len(unsuspend_msgs) == 1
    assert fingerprint in unsuspend_msgs[0].content

    # Verify dispatch was called with RESIDUAL_FIX_UNSUSPENDED
    assert dispatch_mock.called
    call_args = dispatch_mock.call_args_list
    event_types = [
        c.args[1] if c.args else c.kwargs.get("event_type") for c in call_args
    ]
    assert "RESIDUAL_FIX_UNSUSPENDED" in event_types


@pytest.mark.asyncio
async def test_unsuspend_residual_case_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Unsuspend endpoint requires admin auth."""
    csrf_token = "test-csrf-token"
    # Set the CSRF cookie on the client instance (per-request cookies= is
    # deprecated in httpx 0.27+); the matching header clears CSRF so the request
    # reaches the admin-auth check this test asserts on.
    unauthenticated_client.cookies.set("csrf_token", csrf_token, domain="test")
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/unsuspend",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_residual_cases_default_returns_open_states(
    client: AsyncClient, db_session
):
    """Default response includes ACTIVE/REMEDIATING/SUSPENDED, excludes others."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for fp, status in [
        ("active-fp", ResidualCaseStatus.ACTIVE),
        ("remediating-fp", ResidualCaseStatus.REMEDIATING),
        ("suspended-fp", ResidualCaseStatus.SUSPENDED),
        ("ignored-fp", ResidualCaseStatus.IGNORED),
        ("resolved-fp", ResidualCaseStatus.RESOLVED),
    ]:
        db_session.add(
            ResidualRemediationCase(
                project_id=project.id,
                fingerprint=fp,
                status=status,
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/residual-cases")
    assert response.status_code == 200
    data = response.json()
    fps = {c["fingerprint"] for c in data["cases"]}
    assert fps == {"active-fp", "remediating-fp", "suspended-fp"}
    assert sorted(data["status_filter"]) == ["ACTIVE", "REMEDIATING", "SUSPENDED"]


@pytest.mark.asyncio
async def test_list_residual_cases_explicit_status_filter(
    client: AsyncClient, db_session
):
    """Passing ?status=IGNORED returns only IGNORED cases."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="ignored-only",
            status=ResidualCaseStatus.IGNORED,
        )
    )
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="active-other",
            status=ResidualCaseStatus.ACTIVE,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/residual-cases", params={"status": "IGNORED"}
    )
    assert response.status_code == 200
    data = response.json()
    fps = {c["fingerprint"] for c in data["cases"]}
    assert fps == {"ignored-only"}


@pytest.mark.asyncio
async def test_list_residual_cases_rejects_invalid_status(client: AsyncClient):
    """Unknown status values must 400 — typos shouldn't silently return everything."""
    response = await client.get(
        "/api/admin/residual-cases", params={"status": "BOGUS_STATUS"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_residual_cases_orders_by_last_seen_desc(
    client: AsyncClient, db_session
):
    """Most recently-seen cases should surface first so the operator sees pressure."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from datetime import datetime, timedelta

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="older",
            status=ResidualCaseStatus.ACTIVE,
            last_seen_at=now - timedelta(hours=12),
        )
    )
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="newer",
            status=ResidualCaseStatus.ACTIVE,
            last_seen_at=now - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/residual-cases")
    assert response.status_code == 200
    fps = [c["fingerprint"] for c in response.json()["cases"]]
    assert fps[0] == "newer"
    assert fps[1] == "older"


@pytest.mark.asyncio
async def test_list_residual_cases_requires_auth(unauthenticated_client: AsyncClient):
    """List endpoint requires admin auth."""
    response = await unauthenticated_client.get("/api/admin/residual-cases")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_no_case(
    client: AsyncClient, db_session
):
    """Cluster with no matching case row has no case_status field."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            # Use structural error shape per fc071105's tightened regex
            # patterns (`^\s*(error|fail(ed)?)\s*:`); plain prose like
            # "syntax error at token..." no longer matches.
            latest_status_message="SyntaxError: unexpected end of file",
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert "case_status" not in top[0]


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_branches(
    client: AsyncClient, db_session
):
    """Each lifecycle status is surfaced in the corresponding cluster entry."""
    from app.anomaly_detection import _error_fingerprint

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One failed task per status variant so each gets its own fingerprint cluster.
    # Messages must match the structural error patterns added in fc071105
    # (`^\s*(error|fail(ed)?)\s*:` or exception-class line-start) — bare prose
    # like "test failure: active case assertion failed" no longer qualifies.
    messages = {
        ResidualCaseStatus.ACTIVE: "AssertionError: active case 1 == 2 failed",
        ResidualCaseStatus.REMEDIATING: "ImportError: remediating module not found",
        ResidualCaseStatus.SUSPENDED: "TypeError: suspended case got int, expected str",
        ResidualCaseStatus.MERGED: "ValueError: merged case got unexpected None",
        ResidualCaseStatus.FAILED: "KeyError: 'failed_case_lookup_key'",
        ResidualCaseStatus.IGNORED: "AttributeError: ignored case 'foo' missing 'bar'",
    }
    fps = {status: _error_fingerprint(msg) for status, msg in messages.items()}

    for status, msg in messages.items():
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                latest_status_message=msg,
            )
        )
    await db_session.commit()

    # Insert one case row per status
    for status, fp in fps.items():
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=status,
            last_spawned_pr_number=42
            if status == ResidualCaseStatus.REMEDIATING
            else None,
            consecutive_failure_count=3
            if status == ResidualCaseStatus.SUSPENDED
            else 0,
        )
        db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}&top_n=10"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]

    by_fp = {r["fingerprint"]: r for r in top}

    for status, fp in fps.items():
        assert fp in by_fp, f"fingerprint for {status} not in response"
        entry = by_fp[fp]
        assert entry["case_status"] == status, f"{status}: case_status mismatch"

    # REMEDIATING entry carries PR number
    rem_fp = fps[ResidualCaseStatus.REMEDIATING]
    assert by_fp[rem_fp]["last_spawned_pr_number"] == 42

    # SUSPENDED entry carries consecutive_failure_count
    sus_fp = fps[ResidualCaseStatus.SUSPENDED]
    assert by_fp[sus_fp]["consecutive_failure_count"] == 3

    # Entry with no case row should have no case_status — tested in separate test above
    # MERGED/FAILED/IGNORED entries should not carry PR or failure-count noise
    for st in (
        ResidualCaseStatus.MERGED,
        ResidualCaseStatus.FAILED,
        ResidualCaseStatus.IGNORED,
    ):
        assert by_fp[fps[st]].get("last_spawned_pr_number") is None


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_project_scoped(
    client: AsyncClient, db_session
):
    """Case rows from a different project do not pollute the response."""
    from app.anomaly_detection import _error_fingerprint

    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    msg = "TypeError: cross-project test got int, expected str"
    fp = _error_fingerprint(msg)

    db_session.add(
        make_task(
            project_id=project_a.id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
        )
    )
    # Case row belongs to project_b — should NOT be returned for project_a query
    case = ResidualRemediationCase(
        project_id=project_b.id,
        fingerprint=fp,
        status=ResidualCaseStatus.SUSPENDED,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project_a.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert "case_status" not in top[0]


@pytest.mark.asyncio
async def test_residual_fingerprints_includes_audit_deferral_fields(
    client: AsyncClient, db_session
):
    """audit_defer_count and audit_deferred_system_id are included when a case exists."""
    from app.anomaly_detection import _error_fingerprint

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    msg = "RuntimeError: nil pointer dereference in module core"
    fp = _error_fingerprint(msg)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
        )
    )
    case = ResidualRemediationCase(
        fingerprint=fp,
        project_id=project.id,
        status=ResidualCaseStatus.ACTIVE,
        audit_defer_count=3,
        audit_deferred_system_id="sys-payments",
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert top[0]["audit_defer_count"] == 3
    assert top[0]["audit_deferred_system_id"] == "sys-payments"


@pytest.mark.asyncio
async def test_digest_cpm_threshold_flags_expensive_direction(
    client: AsyncClient, db_session
):
    """Directions whose cost_per_merged_pr exceeds median×3 get cost_per_merge_exceeds_threshold=True."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    now = _utcnow()
    recent_ts = (now - timedelta(hours=1)).isoformat()

    # Three directions: cheap (1h / 3 merges), medium (2h / 2 merges), pricey (8h / 1 merge).
    # CPMs: ~0.0223, 0.067, 0.536.  Median = 0.067.  Threshold = 0.067 * 3 = 0.201.
    # Only pricey exceeds the threshold.
    project = make_project(
        directions=[
            {
                "id": "dir-cheap",
                "text": "Cheap direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 3}],
            },
            {
                "id": "dir-med",
                "text": "Medium direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 2}],
            },
            {
                "id": "dir-pricey",
                "text": "Pricey direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id, hours in [("dir-cheap", 1), ("dir-med", 2), ("dir-pricey", 8)]:
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"task-{dir_id}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
                vm_provisioned_at=now - timedelta(hours=hours + 1),
                vm_stopped_at=now - timedelta(hours=1),
                vm_spot=False,
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert set(by_id) == {"dir-cheap", "dir-med", "dir-pricey"}

    assert by_id["dir-cheap"]["cost_per_merge_exceeds_threshold"] is False
    assert by_id["dir-med"]["cost_per_merge_exceeds_threshold"] is False
    assert by_id["dir-pricey"]["cost_per_merge_exceeds_threshold"] is True

    median = data["median_cost_per_merged_pr"]
    assert median is not None
    assert median > 0
    # median CPM ≈ dir-med's CPM (middle value when sorted)
    expected_med_cost = round(ON_DEMAND_RATE * 2 * 3600, 4)
    expected_med_cpm = round(expected_med_cost / 2, 4)
    assert abs(median - expected_med_cpm) < 0.001


@pytest.mark.asyncio
async def test_digest_cpm_threshold_false_when_no_merges(
    client: AsyncClient, db_session
):
    """When no directions have merged PRs, median is None and threshold flag is False for all."""
    now = _utcnow()
    recent_ts = (now - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "No merges direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 0}],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="task-1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["median_cost_per_merged_pr"] is None
    dp = data["direction_progress"][0]
    assert dp["cost_per_merge_exceeds_threshold"] is False


@pytest.mark.asyncio
async def test_drift_snapshot_missing_project_returns_empty(client: AsyncClient):
    """Unknown project_id → empty snapshot with correct shape, not a 4xx."""
    import uuid as _uuid

    project_id = _uuid.uuid4()
    response = await client.get(f"/api/admin/drift-snapshot?project_id={project_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 0
    assert data["flagged_directions"] == []
    assert data["total_directions"] == 0
    assert data["any_system_flagged"] is False
    assert "generated_at" in data
    assert data["window_days"] == 7


@pytest.mark.asyncio
async def test_drift_snapshot_no_flags_when_healthy(client: AsyncClient, db_session):
    """Project with directions but no drift activity → zero flagged directions."""
    project = make_project(
        directions=[
            {
                "id": "d-healthy",
                "text": "Keep CI green",
                "status": "active",
                "priority": "high",
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 0
    assert data["flagged_directions"] == []
    assert data["total_directions"] == 1


@pytest.mark.asyncio
async def test_drift_snapshot_over_claim_direction_flagged(
    client: AsyncClient, db_session
):
    """Direction with many claims but zero merges appears in flagged_directions."""
    from app.models import Task, TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-over-claim"
    project = make_project(
        directions=[
            {
                "id": dir_id,
                "text": "Reduce flakiness",
                "status": "active",
                "priority": "high",
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add enough COMPLETED tasks with plan_output to trip over_claim threshold (>=3 claims, <40% consistency)
    for _ in range(4):
        task = Task(
            project_id=project.id,
            title="Claim task",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "current_direction_id": dir_id}],
            context={"direction_id": dir_id},
            plan_output={"summary": "claimed a change"},
            vm_instance_id=f"vm-{_now().timestamp()}",
            created_at=_now() - timedelta(hours=2),
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:8]}",
            )
        )

    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 1
    assert len(data["flagged_directions"]) == 1
    flagged = data["flagged_directions"][0]
    assert flagged["direction_id"] == dir_id
    assert flagged["direction_text"] == "Reduce flakiness"
    assert flagged["over_claim_flag"] is True
    assert flagged["claimed_changes_count"] == 4
    assert flagged["actually_merged_prs"] == 0
    assert "claim_shipped_consistency" in flagged
    assert "window_days" in data
    assert "generated_at" in data
    # drift_history field present; empty when no stored snapshots exist yet
    assert "drift_history" in flagged
    assert isinstance(flagged["drift_history"], list)


@pytest.mark.asyncio
async def test_drift_snapshot_history_series_returned(client: AsyncClient, db_session):
    """Flagged direction includes history sparkline from stored DirectionDriftSnapshot rows."""
    from app.drift_detection import DirectionDriftSnapshot
    from app.models import TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-trend"
    project = make_project(
        directions=[
            {"id": dir_id, "text": "Trend test", "status": "active", "priority": "high"}
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 3 historical drift snapshots (oldest → newest)
    base_time = _now() - timedelta(hours=6)
    for i in range(3):
        snap = DirectionDriftSnapshot(
            project_id=project.id,
            direction_id=dir_id,
            snapshot_at=base_time + timedelta(hours=i),
            claimed_changes_count=4,
            actually_merged_prs=0,
            claim_shipped_consistency=0.0,
            over_claim_flag=True,
            stalled_despite_churn_flag=False,
        )
        db_session.add(snap)

    # Add enough tasks to trigger the over_claim_flag in the live compute
    now = _now()
    for _ in range(4):
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"direction_id": dir_id, "shipped": True},
            created_at=now - timedelta(hours=1),
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:8]}",
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 1
    flagged = data["flagged_directions"][0]
    assert flagged["direction_id"] == dir_id
    history = flagged["drift_history"]
    assert isinstance(history, list)
    assert len(history) == 3
    # history is oldest-first (chronological order for sparkline)
    for entry in history:
        assert "snapshot_at" in entry
        assert "claim_shipped_consistency" in entry
        assert "over_claim_flag" in entry
        assert "stalled_despite_churn_flag" in entry
    # chronological order: each snapshot_at >= previous
    times = [entry["snapshot_at"] for entry in history]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_drift_snapshot_response_shape_pinned(client: AsyncClient, db_session):
    """Pin the exact key set on flagged_directions[i] and the per-direction history
    entries so a future rename of either side fails CI immediately.

    The asserted key set MUST mirror the frontend TypeScript interfaces
    DriftDirection and DriftHistoryPoint in
    admin-ui/src/components/DriftSignalsCard.tsx. If you change either side,
    update both together — silent drift between server and client is the bug
    class this test exists to catch.
    """
    from app.drift_detection import DirectionDriftSnapshot
    from app.models import TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-shape"
    project = make_project(
        directions=[
            {"id": dir_id, "text": "Shape pin", "status": "active", "priority": "high"}
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base_time = _now() - timedelta(hours=3)
    db_session.add(
        DirectionDriftSnapshot(
            project_id=project.id,
            direction_id=dir_id,
            snapshot_at=base_time,
            claimed_changes_count=4,
            actually_merged_prs=0,
            claim_shipped_consistency=0.0,
            over_claim_flag=True,
            stalled_despite_churn_flag=False,
        )
    )
    now = _now()
    for _ in range(4):
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"direction_id": dir_id, "shipped": True},
            created_at=now - timedelta(hours=1),
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:8]}",
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    flagged = response.json()["flagged_directions"][0]

    # Mirrors DriftDirection (extends DriftSignal) in DriftSignalsCard.tsx.
    assert set(flagged.keys()) == {
        "direction_id",
        "direction_text",
        "over_claim_flag",
        "stalled_despite_churn_flag",
        "claimed_changes_count",
        "actually_merged_prs",
        "claim_shipped_consistency",
        "drift_history",
    }
    assert flagged["drift_history"], (
        "expected at least one history entry from seeded snapshot"
    )
    # Mirrors DriftHistoryPoint in DriftSignalsCard.tsx.
    assert set(flagged["drift_history"][0].keys()) == {
        "snapshot_at",
        "claim_shipped_consistency",
        "over_claim_flag",
        "stalled_despite_churn_flag",
    }


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_aggregation(client: AsyncClient, db_session):
    """cost_per_merge_by_type groups cost and merge counts by task type."""
    from app.routers.admin_analytics import SPOT_RATE

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # create task: 10s, pr_merged=True
    t1 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=10),
        vm_started_at=now - timedelta(seconds=10),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    # fix task: 20s, pr_merged=True
    t2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=20),
        vm_started_at=now - timedelta(seconds=20),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "fix"}],
    )
    # research task: 5s, no merge
    t3 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=5),
        vm_started_at=now - timedelta(seconds=5),
        vm_stopped_at=now,
        context={},
        payloads=[{"type": "research"}],
    )
    for t in (t1, t2, t3):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    cpm_by_type = data["cost_per_merge_by_type"]
    assert "create" in cpm_by_type
    assert "fix" in cpm_by_type
    assert "research" in cpm_by_type

    assert cpm_by_type["create"]["merged_count"] == 1
    assert (
        abs(cpm_by_type["create"]["total_cost_usd"] - round(SPOT_RATE * 10, 4)) < 1e-6
    )
    assert cpm_by_type["create"]["cost_per_merge"] is not None

    assert cpm_by_type["fix"]["merged_count"] == 1
    assert abs(cpm_by_type["fix"]["total_cost_usd"] - round(SPOT_RATE * 20, 4)) < 1e-6
    assert cpm_by_type["fix"]["cost_per_merge"] is not None

    assert cpm_by_type["research"]["merged_count"] == 0
    assert cpm_by_type["research"]["cost_per_merge"] is None


@pytest.mark.asyncio
async def test_digest_residual_remediation_funnel(client: AsyncClient, db_session):
    """Residual remediation funnel sourced from ResidualRemediationCase rows."""
    from app.models import ResidualRemediationCase, ResidualCaseStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    in_window = now - timedelta(hours=6)
    out_of_window = now - timedelta(hours=30)

    def make_case(status, spawned_at):
        import uuid as _uuid

        return ResidualRemediationCase(
            project_id=project.id,
            fingerprint=str(_uuid.uuid4()),
            status=status,
            last_spawned_at=spawned_at,
        )

    # In-window cases: 1 merged, 1 failed, 1 suspended (escalated), 1 remediating
    db_session.add(make_case(ResidualCaseStatus.MERGED, in_window))
    db_session.add(make_case(ResidualCaseStatus.FAILED, in_window))
    db_session.add(make_case(ResidualCaseStatus.SUSPENDED, in_window))
    db_session.add(make_case(ResidualCaseStatus.REMEDIATING, in_window))
    # Out-of-window case: excluded from spawned counts; REMEDIATING counts in live total
    db_session.add(make_case(ResidualCaseStatus.ACTIVE, out_of_window))
    db_session.add(make_case(ResidualCaseStatus.REMEDIATING, out_of_window))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    rr = response.json()["remediation_funnel"]["residual_remediation_funnel"]

    assert rr["spawned"] == 4  # 4 cases with last_spawned_at in 24h window
    assert rr["merged"] == 1
    assert rr["failed"] == 2  # FAILED + SUSPENDED
    assert (
        rr["remediating"] == 2
    )  # live count: both REMEDIATING cases regardless of window
    groups = rr["open_cases_by_error_class"]
    assert groups and groups[0]["error_class"] == "CODE_ERROR"
    assert groups[0]["count"] == 4
    assert len(groups[0]["cases"]) == 4


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_no_vm_tasks(client: AsyncClient, db_session):
    """When no tasks have VM billing data, cost_per_merge_by_type is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["cost_per_merge_by_type"] == {}
    assert data["median_cost_per_merge_by_type"] is None


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_threshold(client: AsyncClient, db_session):
    """exceeds_threshold is True when a type's CPM exceeds median * 3.0.

    Median requires >=3 types to produce a stable low reference point so the
    expensive outlier can exceed 3× median.  With create=10s, fix=20s, debug=1000s
    the median is 20 and 3× threshold is 60; debug (1000s) clearly exceeds it.
    """

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # create: 10s per merge (cheap)
    t_create = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=10),
        vm_started_at=now - timedelta(seconds=10),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    # fix: 20s per merge (medium)
    t_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=20),
        vm_started_at=now - timedelta(seconds=20),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "fix"}],
    )
    # debug: 1000s per merge (far above 3x median=20 → threshold=60)
    t_debug = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=1000),
        vm_started_at=now - timedelta(seconds=1000),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "debug"}],
    )
    for t in (t_create, t_fix, t_debug):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    cpm_by_type = data["cost_per_merge_by_type"]
    median_cpm = data["median_cost_per_merge_by_type"]
    assert median_cpm is not None

    assert cpm_by_type["create"]["cost_per_merge"] is not None
    assert cpm_by_type["fix"]["cost_per_merge"] is not None
    assert cpm_by_type["debug"]["cost_per_merge"] is not None

    # debug CPM (1000s) >> 3 × median (20s); create and fix are below threshold
    assert cpm_by_type["debug"]["exceeds_threshold"] is True
    assert cpm_by_type["create"]["exceeds_threshold"] is False
    assert cpm_by_type["fix"]["exceeds_threshold"] is False


@pytest.mark.asyncio
async def test_digest_calibration_fields_present_when_project_selected(
    client: AsyncClient, db_session
):
    """Digest includes prediction_calibration_* fields when project_id is provided.

    With no StrategicCycle rows the factor defaults to 1.0 and sample_count to 0.
    """
    project = make_project(directions=[])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "prediction_calibration_factor" in data
    assert "prediction_calibration_note" in data
    assert "prediction_calibration_sample_count" in data
    assert data["prediction_calibration_factor"] == 1.0
    assert data["prediction_calibration_sample_count"] == 0
    assert "well-calibrated" in (data["prediction_calibration_note"] or "")


@pytest.mark.asyncio
async def test_digest_calibration_fields_null_without_project(client: AsyncClient):
    """Digest omits calibration values when no project_id is provided."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["prediction_calibration_factor"] is None
    assert data["prediction_calibration_note"] is None
    assert data["prediction_calibration_sample_count"] is None


@pytest.mark.asyncio
async def test_digest_top_level_schema_keys(client: AsyncClient):
    """All dashboard-facing top-level keys are present; tasks_truncated is a boolean."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    required = {
        "window_hours",
        "tasks_truncated",
        "completed_count",
        "failed_count",
        "pending_approval_count",
        "research_cycles",
        "prs_opened",
        "active_count",
        "running_count",
        "queued_count",
        "active_tasks",
        "needs_attention",
        "recent_failures",
        "recent_completions",
        "success_rate",
        "trend",
        "narrative_summary",
        "direction_portfolio",
        "total_estimated_cost_usd",
        "migration_health",
        "migration_slot_contention",
        "merge_gate_fleet_health",
        "failure_groups",
        "recurring_error_clusters",
        "recurring_patterns",
        "direction_progress",
        "retry_waste_by_direction",
        "provision_waste_cost_usd",
        "provision_waste_by_direction",
        "cost_by_failure_class",
        "analysis_overflow_summary",
        "total_merge_retries_saved",
        "merge_early_exit_count",
        "prediction_calibration_factor",
        "prediction_calibration_note",
        "prediction_calibration_sample_count",
    }
    missing = required - data.keys()
    assert not missing, f"Missing dashboard keys: {missing}"
    assert isinstance(data["tasks_truncated"], bool)


@pytest.mark.asyncio
async def test_digest_migration_health_includes_slot_contention(
    client: AsyncClient, db_session
):
    """The digest surfaces migration-slot contention with holder and wait-time detail."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    now = _utcnow()

    holder = make_task(
        project_id=project.id,
        title="Author migration",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "migrate"}],
        started_at=now - timedelta(hours=3, minutes=15),
    )
    waiter = make_task(
        project_id=project.id,
        title="Blocked migration waiter",
        status=TaskStatus.BLOCKED,
        payloads=[
            {
                "type": "create",
                "related_files": ["backend/app/models.py"],
            }
        ],
        context={"related_files": ["backend/app/models.py"]},
        created_at=now - timedelta(hours=1, minutes=45),
        updated_at=now - timedelta(hours=1, minutes=30),
    )
    db_session.add(holder)
    db_session.add(waiter)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    contention = data["migration_health"]["migration_slot_contention"]
    slot_summary = data["migration_slot_contention"]
    assert contention["status"] == "elevated"
    assert contention["severity"] == "critical"
    assert contention["deferred_count"] == 1
    assert contention["holder"]["task_id"] == str(holder.id)
    assert contention["longest_wait"]["task_id"] == str(waiter.id)
    assert slot_summary["waiting_count"] == contention["deferred_count"]
    assert slot_summary["holder_task_id"] == contention["holder"]["task_id"]
    assert slot_summary["threshold_exceeded"] is True


@pytest.mark.asyncio
async def test_digest_needs_attention_approval_before_failed(
    client: AsyncClient, db_session
):
    """approval_required items precede failed items in needs_attention."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Failed Task",
            status=TaskStatus.FAILED,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="Approval Task",
            status=TaskStatus.IN_PROGRESS,
            requires_approval=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    items = response.json()["needs_attention"]
    assert len(items) == 2
    assert items[0]["reason"] == "approval_required"
    assert items[1]["reason"] == "failed"


# Retired: test_digest_pipeline_autonomy_totals — it asserted the window-level
# pipeline_autonomous_prs_merged / pipeline_assisted_prs_merged /
# pipeline_operator_action_count digest keys, which were consumer-less duplicates
# of the per-direction outcome fields and have been removed from the payload.


@pytest.mark.asyncio
async def test_digest_pipeline_autonomy_legacy_fallback(
    client: AsyncClient, db_session
):
    """Legacy entries lacking autonomous_prs_merged are counted as autonomous (fallback)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-legacy",
                "text": "Legacy direction",
                "status": "active",
                "research_history": [
                    # No autonomous_prs_merged key — legacy pre-attribution entry
                    {"timestamp": recent_ts, "prs_merged": 4},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Legacy task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-legacy"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = data["direction_progress"][0]
    # Legacy fallback: autonomous = prs_merged, assisted = 0. The per-direction
    # fields remain consumed (DirectionCard); the window-level pipeline_* rollups
    # that summed them were retired as consumer-less.
    assert dp["autonomous_prs_merged"] == 4
    assert dp["assisted_prs_merged"] == 0
    assert dp["operator_action_count"] == 0
    assert "pipeline_autonomous_prs_merged" not in data
    assert "pipeline_assisted_prs_merged" not in data
    assert "pipeline_operator_action_count" not in data


@pytest.mark.asyncio
async def test_digest_direction_progress_exposes_spawn_gate_block(
    client: AsyncClient, db_session
):
    """Digest direction_progress includes the research spawn-gate comparison block."""
    project = make_project(
        directions=[
            {
                "id": "dir-spawn-gate",
                "text": "Spawn gate direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": _utcnow().isoformat(),
                        "task_id": "research-task-1",
                        "tasks_created": 1,
                        "research_spawn_gate_challenged_count": 2,
                        "research_spawn_gate_refuted_count": 1,
                        "research_spawn_gate_survived_count": 1,
                        "research_spawn_gate_challenged_spawned": 2,
                        "research_spawn_gate_challenged_merged": 1,
                        "research_spawn_gate_challenged_merge_rate": 0.5,
                        "research_spawn_gate_unchallenged_spawned": 3,
                        "research_spawn_gate_unchallenged_merged": 1,
                        "research_spawn_gate_unchallenged_merge_rate": 0.333,
                        "research_spawn_gate_wholesale_retry_exhausted": True,
                        "research_spawn_gate_parse_outcome": "no_hash_matches",
                        "research_spawn_gate_manifest_excluded_fail_closed_count": 1,
                        "research_spawn_gate_manifest_excluded_fail_closed_samples": [
                            {
                                "hash": "excluded-hash",
                                "title": "Budget-excluded finding",
                                "reason": "manifest cap",
                            }
                        ],
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research cycle",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-spawn-gate"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = next(
        d for d in data["direction_progress"] if d["direction_id"] == "dir-spawn-gate"
    )
    gate = dp["research_spawn_gate"]
    assert gate["challenged_count"] == 2
    assert gate["refuted_count"] == 1
    assert gate["survived_count"] == 1
    assert gate["manifest_excluded_fail_closed_count"] == 1
    assert gate["manifest_excluded_fail_closed_samples"] == [
        {
            "hash": "excluded-hash",
            "title": "Budget-excluded finding",
            "reason": "manifest cap",
        }
    ]
    assert gate["challenged_merge_rate"] == 0.5
    assert gate["unchallenged_merge_rate"] == 0.333
    assert gate["exhaustion_count"] == 1
    assert gate["parse_outcome_distribution"] == {"no_hash_matches": 1}


@pytest.mark.asyncio
async def test_digest_direction_progress_exposes_adversarial_give_up(
    client: AsyncClient, db_session
):
    """Digest direction_progress includes adversarial give-up counts and samples."""
    project = make_project(
        directions=[
            {
                "id": "dir-give-up",
                "text": "Give-up direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": _utcnow().isoformat(),
                        "task_id": "research-task-give-up-1",
                        "tasks_created": 1,
                        "research_spawn_gate_adversarial_give_up_count": 3,
                        "research_spawn_gate_adversarial_give_up_samples": [
                            {
                                "title": "Abandoned finding A",
                                "direction_id": "dir-give-up",
                            },
                            {"title": "Abandoned finding B"},
                        ],
                    },
                    {
                        "timestamp": _utcnow().isoformat(),
                        "task_id": "research-task-give-up-2",
                        "tasks_created": 1,
                        "research_spawn_gate_adversarial_give_up_count": 1,
                        "research_spawn_gate_adversarial_give_up_samples": [
                            {"title": "Abandoned finding C"},
                        ],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research cycle",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-give-up"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = next(
        d for d in data["direction_progress"] if d["direction_id"] == "dir-give-up"
    )
    gate = dp["research_spawn_gate"]
    assert gate is not None
    # Counts sum across both history entries.
    assert gate["adversarial_give_up_count"] == 4
    samples = gate["adversarial_give_up_samples"]
    sample_titles = [s["title"] for s in samples]
    assert "Abandoned finding A" in sample_titles
    assert "Abandoned finding B" in sample_titles
    assert "Abandoned finding C" in sample_titles
    assert len(samples) == len({s["title"] for s in samples}), "samples must be deduped"


@pytest.mark.asyncio
async def test_digest_spawn_gate_aggregate_give_up_count_alone_produces_non_none(
    client: AsyncClient, db_session
):
    """A direction with only adversarial give-up activity produces a non-None aggregate."""
    from app.digest_direction_progress import compute_spawn_gate_aggregate

    history = [
        {
            "research_spawn_gate_adversarial_give_up_count": 2,
            "research_spawn_gate_adversarial_give_up_samples": [
                {"title": "Discarded finding X"},
            ],
        }
    ]
    result = compute_spawn_gate_aggregate(history)
    assert result is not None
    assert result["adversarial_give_up_count"] == 2
    assert result["adversarial_give_up_samples"] == [{"title": "Discarded finding X"}]
    # Other counters are zero when no other spawn-gate activity occurred.
    assert result["challenged"] == 0
    assert result["refuted"] == 0
    assert result["survived"] == 0


@pytest.mark.asyncio
async def test_digest_active_tasks_ordered_oldest_first(
    client: AsyncClient, db_session
):
    """active_tasks are ordered oldest started_at first."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    older = make_task(
        project_id=project.id,
        title="Older Task",
        status=TaskStatus.IN_PROGRESS,
        started_at=_utcnow() - timedelta(hours=3),
    )
    newer = make_task(
        project_id=project.id,
        title="Newer Task",
        status=TaskStatus.IN_PROGRESS,
        started_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    active = response.json()["active_tasks"]
    ids = [t["task_id"] for t in active]
    assert ids.index(str(older.id)) < ids.index(str(newer.id))


@pytest.mark.asyncio
async def test_research_actionability_grade_distribution(
    client: AsyncClient, db_session
):
    """Grade distribution is computed from research task plan_output findings."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(id="cycle-action-1", project_id=project.id)
    db_session.add(cycle)

    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"_actionability_structural_downgrades": 2},
        plan_output=[
            {
                "title": "Finding A",
                "actionability_grade": "HIGH",
                "direction_id": "dir-h1",
            },
            {
                "title": "Finding B",
                "actionability_grade": "HIGH",
                "direction_id": "dir-h2",
            },
            {
                "title": "Finding C",
                "actionability_grade": "MEDIUM",
                "direction_id": "dir-m1",
            },
            {
                "title": "Finding D",
                "actionability_grade": "LOW",
                "direction_id": "dir-l1",
            },
        ],
    )
    db_session.add(research_task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 4
    assert data["grade_distribution"]["HIGH"] == 2
    assert data["grade_distribution"]["MEDIUM"] == 1
    assert data["grade_distribution"]["LOW"] == 1
    assert data["cycle_history"][0]["structural_downgrades"] == 2


@pytest.mark.asyncio
async def test_research_actionability_empty_project(client: AsyncClient, db_session):
    """Project with no research tasks returns zero counts."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 0
    assert data["grade_distribution"] == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


@pytest.mark.asyncio
async def test_research_actionability_excludes_non_research_plan_output(
    client: AsyncClient, db_session
):
    """plan_output on non-research task types must NOT leak into the grade distribution."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-action-typefilter", project_id=project.id))

    # Real research task — counted.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            plan_output=[
                {
                    "title": "Real finding",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-real",
                },
            ],
        )
    )
    # Non-research task that also wrote plan_output — must be excluded.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            plan_output=[
                {
                    "title": "Bogus finding",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-bogus",
                },
                {
                    "title": "Bogus 2",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-bogus2",
                },
            ],
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "planning"}],
            plan_output=[
                {
                    "title": "Planner output",
                    "actionability_grade": "LOW",
                    "direction_id": "dir-plan",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 1
    assert data["grade_distribution"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}


@pytest.mark.asyncio
async def test_research_actionability_research_task_does_not_self_attribute(
    client: AsyncClient, db_session
):
    """A research task whose context carries direction_id must not count as its own follow-on."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-action-self", project_id=project.id))

    # Research task with direction_id in context (canonical pattern from
    # research_completion.py:1721) and pr_merged accidentally truthy in
    # context. Without the self-exclusion, this would mark the direction as
    # both tasked and merged.
    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-self", "pr_merged": True},
        plan_output=[
            {
                "title": "Self-attribution probe",
                "actionability_grade": "HIGH",
                "direction_id": "dir-self",
            },
        ],
    )
    db_session.add(research_task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    # Distribution still records the finding...
    assert data["grade_distribution"]["HIGH"] == 1
    # ...but the research task itself must not register as the follow-on.
    assert data["grade_tasked"]["HIGH"] == 0
    assert data["grade_merged"]["HIGH"] == 0


@pytest.mark.asyncio
async def test_research_actionability_merge_requires_pr_merged_flag(
    client: AsyncClient, db_session
):
    """Merge attribution requires a landed signal, not just COMPLETED or
    merge-equivalent unblocks."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    research_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-action-merge",
            project_id=project.id,
            created_at=research_at - _td(hours=2),
        )
    )
    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        created_at=research_at,
        plan_output=[
            {
                "title": "Merged path",
                "actionability_grade": "HIGH",
                "direction_id": "dir-merged",
            },
            {
                "title": "Tasked-but-not-merged",
                "actionability_grade": "HIGH",
                "direction_id": "dir-tasked",
            },
            {
                "title": "Untouched",
                "actionability_grade": "MEDIUM",
                "direction_id": "dir-cold",
            },
        ],
    )
    db_session.add(research_task)

    # Follow-on for dir-merged: COMPLETED + pr_merged=True → merged.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=30),
            context={"direction_id": "dir-merged", "pr_merged": True},
        )
    )
    # Follow-on for dir-merged created after research but only merged to a
    # non-integration branch — must not count as merged.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=45),
            context={
                "direction_id": "dir-merged",
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            },
        )
    )
    # Follow-on for dir-tasked: COMPLETED but NO pr_merged → tasked only.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=30),
            context={"direction_id": "dir-tasked"},
        )
    )
    # Follow-on for dir-merged created BEFORE the research task — must not count.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at - _td(hours=1),
            context={"direction_id": "dir-cold", "pr_merged": True},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_tasked"]["HIGH"] == 2  # dir-merged + dir-tasked
    assert data["grade_merged"]["HIGH"] == 1  # only dir-merged
    # dir-cold's pre-research follow-on must not register.
    assert data["grade_tasked"]["MEDIUM"] == 0
    assert data["grade_merged"]["MEDIUM"] == 0


@pytest.mark.asyncio
async def test_research_actionability_grade_merge_rate_and_calibration(
    client: AsyncClient, db_session
):
    """grade_merge_rate = merged/findings; calibration_score = HIGH_rate - LOW_rate."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    research_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-mr-1",
            project_id=project.id,
            created_at=research_at - _td(hours=1),
        )
    )
    # 3 HIGH findings (2 will merge, 1 just tasked) → HIGH rate = 2/3
    # 2 MEDIUM findings (1 will merge) → MEDIUM rate = 1/2
    # 2 LOW findings (0 will merge) → LOW rate = 0/2 = 0.0
    plan_output = [
        {"title": "H1", "actionability_grade": "HIGH", "direction_id": "dir-h1"},
        {"title": "H2", "actionability_grade": "HIGH", "direction_id": "dir-h2"},
        {"title": "H3", "actionability_grade": "HIGH", "direction_id": "dir-h3"},
        {"title": "M1", "actionability_grade": "MEDIUM", "direction_id": "dir-m1"},
        {"title": "M2", "actionability_grade": "MEDIUM", "direction_id": "dir-m2"},
        {"title": "L1", "actionability_grade": "LOW", "direction_id": "dir-l1"},
        {"title": "L2", "actionability_grade": "LOW", "direction_id": "dir-l2"},
    ]
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_at,
            plan_output=plan_output,
        )
    )
    # Merged follow-ons.
    for d in ("dir-h1", "dir-h2", "dir-m1"):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                created_at=research_at + _td(minutes=10),
                context={"direction_id": d, "pr_merged": True},
            )
        )
    # Tasked-only follow-on for HIGH.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=10),
            context={"direction_id": "dir-h3"},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_merge_rate"]["HIGH"] == pytest.approx(2 / 3)
    assert data["grade_merge_rate"]["MEDIUM"] == pytest.approx(1 / 2)
    assert data["grade_merge_rate"]["LOW"] == 0.0
    # of-tasked: HIGH = 2 merged / 3 tasked, MEDIUM = 1/1, LOW = None (0 tasked)
    assert data["grade_merge_rate_of_tasked"]["HIGH"] == pytest.approx(2 / 3)
    assert data["grade_merge_rate_of_tasked"]["MEDIUM"] == pytest.approx(1.0)
    assert data["grade_merge_rate_of_tasked"]["LOW"] is None
    assert data["calibration_score"] == pytest.approx(2 / 3 - 0.0)


@pytest.mark.asyncio
async def test_research_actionability_calibration_null_when_no_findings(
    client: AsyncClient, db_session
):
    """calibration_score is null when HIGH or LOW has zero findings; merge_rate
    uses null (not 0.0) when grade_distribution is 0."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-mr-empty", project_id=project.id))
    # Only MEDIUM findings — both HIGH and LOW are absent.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            plan_output=[
                {
                    "title": "M1",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-m1",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_merge_rate"]["HIGH"] is None
    assert data["grade_merge_rate"]["LOW"] is None
    assert data["grade_merge_rate"]["MEDIUM"] == 0.0  # 0 merged / 1 finding
    assert data["calibration_score"] is None


@pytest.mark.asyncio
async def test_research_actionability_cycle_history_chronological(
    client: AsyncClient, db_session
):
    """cycle_history has one entry per scanned cycle, oldest→newest, with per-cycle rates."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    now = _utcnow()
    cycle_old_at = now - _td(hours=10)
    cycle_new_at = now - _td(hours=4)
    db_session.add(
        StrategicCycle(
            id="cycle-old",
            project_id=project.id,
            created_at=cycle_old_at,
        )
    )
    db_session.add(
        StrategicCycle(
            id="cycle-new",
            project_id=project.id,
            created_at=cycle_new_at,
        )
    )

    # Research task in OLD cycle window: 1 HIGH (will merge).
    research_old_at = cycle_old_at + _td(minutes=30)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_old_at,
            plan_output=[
                {
                    "title": "Old HIGH",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-old-h",
                },
            ],
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_old_at + _td(minutes=5),
            context={"direction_id": "dir-old-h", "pr_merged": True},
        )
    )

    # Research task in NEW cycle window: 1 LOW (no follow-on).
    research_new_at = cycle_new_at + _td(minutes=30)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_new_at,
            plan_output=[
                {
                    "title": "New LOW",
                    "actionability_grade": "LOW",
                    "direction_id": "dir-new-l",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert "cycle_history" in data
    history = data["cycle_history"]
    assert len(history) == 2
    # Oldest → newest order.
    assert history[0]["cycle_id"] == "cycle-old"
    assert history[1]["cycle_id"] == "cycle-new"
    # Old bucket: 1 HIGH finding, merged. HIGH rate = 1.0, LOW absent → None.
    assert history[0]["total_findings"] == 1
    assert history[0]["grade_distribution"]["HIGH"] == 1
    assert history[0]["grade_merged"]["HIGH"] == 1
    assert history[0]["grade_merge_rate"]["HIGH"] == 1.0
    assert history[0]["grade_merge_rate"]["LOW"] is None
    assert history[0]["calibration_score"] is None  # LOW rate is None
    # New bucket: 1 LOW, not merged. LOW rate = 0.0, HIGH absent → None.
    assert history[1]["total_findings"] == 1
    assert history[1]["grade_distribution"]["LOW"] == 1
    assert history[1]["grade_merge_rate"]["LOW"] == 0.0
    assert history[1]["grade_merge_rate"]["HIGH"] is None
    assert history[1]["calibration_score"] is None


@pytest.mark.asyncio
async def test_research_actionability_cycle_history_null_rate_for_zero_findings(
    client: AsyncClient, db_session
):
    """A cycle with zero findings of a grade returns null (not 0.0) for that grade's merge_rate."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    cyc_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-only-medium",
            project_id=project.id,
            created_at=cyc_at,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=cyc_at + _td(minutes=10),
            plan_output=[
                {
                    "title": "M-only",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-m-only",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["cycle_history"]) == 1
    entry = data["cycle_history"][0]
    assert entry["grade_merge_rate"]["HIGH"] is None
    assert entry["grade_merge_rate"]["LOW"] is None
    assert entry["grade_merge_rate"]["MEDIUM"] == 0.0
    assert entry["calibration_score"] is None


@pytest.mark.asyncio
async def test_anomaly_tracker_snapshot_empty(client: AsyncClient):
    """Empty persistent_anomalies returns empty projects list and null run_at."""
    import app.shared_state as _ss

    saved_dict = dict(_ss.persistent_anomalies)
    saved_run_at = _ss.last_anomaly_detection_run_at
    _ss.persistent_anomalies.clear()
    _ss.last_anomaly_detection_run_at = None
    try:
        response = await client.get("/api/admin/anomaly-tracker/snapshot")
        assert response.status_code == 200
        data = response.json()
        assert data["last_detection_run_at"] is None
        assert data["freshness_window_hours"] == 24
        assert data["total"] == 0
        assert data["projects"] == []
    finally:
        _ss.persistent_anomalies.clear()
        _ss.persistent_anomalies.update(saved_dict)
        _ss.last_anomaly_detection_run_at = saved_run_at


@pytest.mark.asyncio
async def test_anomaly_tracker_snapshot_fresh_and_stale(
    client: AsyncClient, db_session
):
    """Returns sorted entries, marks stuck=true for stale last_seen, echoes run_at."""
    import app.shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    now = _utcnow()
    fresh_last = (now - timedelta(hours=2)).isoformat()
    stale_last = (now - timedelta(hours=72)).isoformat()
    first_seen = (now - timedelta(hours=96)).isoformat()
    run_at = now - timedelta(minutes=3)

    saved_dict = dict(_ss.persistent_anomalies)
    saved_run_at = _ss.last_anomaly_detection_run_at
    _ss.persistent_anomalies.clear()
    _ss.persistent_anomalies[proj_key] = {
        "FailureRateMomentum": {
            "first_seen": first_seen,
            "last_seen": fresh_last,
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "fresh anomaly",
            "affected_directions": ["dir-1"],
        },
        "CrossDirectionFingerprint_abc": {
            "first_seen": first_seen,
            "last_seen": stale_last,
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "stale anomaly",
            "fingerprint": "abc",
        },
    }
    _ss.last_anomaly_detection_run_at = run_at
    try:
        response = await client.get(
            "/api/admin/anomaly-tracker/snapshot",
            params={"project_id": str(project.id), "freshness_hours": 48},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["last_detection_run_at"] == run_at.isoformat()
        assert data["freshness_window_hours"] == 48
        assert data["total"] == 2
        assert len(data["projects"]) == 1
        bucket = data["projects"][0]
        assert bucket["project_id"] == proj_key
        entries = bucket["anomalies"]
        # Sorted by last_seen desc — fresh first.
        assert entries[0]["key"] == "FailureRateMomentum"
        assert entries[0]["stuck"] is False
        assert entries[0]["affected_directions"] == ["dir-1"]
        assert entries[0]["last_dispatch_attempt_at"] is None
        assert entries[1]["key"] == "CrossDirectionFingerprint_abc"
        assert entries[1]["stuck"] is True
        assert entries[1]["fingerprint"] == "abc"
    finally:
        _ss.persistent_anomalies.clear()
        _ss.persistent_anomalies.update(saved_dict)
        _ss.last_anomaly_detection_run_at = saved_run_at


@pytest.mark.asyncio
async def test_viewport_debt_coverage_summary_in_response(
    client: AsyncClient, db_session
):
    """GET /viewport-debt returns coverage_summary with correct shape."""
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    membership = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(membership)
    await db_session.commit()

    fake_rows = [
        {
            "component": f"admin-ui/src/components/Comp{i}.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 20 - i,
            "has_viewport_test": (i % 5 == 0),
            "regression_case_count": 0,
            "source_url": None,
        }
        for i in range(20)
    ]
    top_rows = fake_rows[:15]
    # 4 rows have viewport tests (i=0,5,10,15); 16 do not
    untested_paths = [r["component"] for r in fake_rows if not r["has_viewport_test"]]
    expected_summary = {
        "total_components": 20,
        "untested_count": 16,
        "untested_components": untested_paths,
    }

    with patch(
        "app.viewport_debt._scan_viewport_debt_rows",
        return_value=(top_rows, [], expected_summary),
    ):
        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    assert "rows" in body
    assert "untested_rows" in body
    assert "scanned_at" in body
    assert "coverage_summary" in body
    cs = body["coverage_summary"]
    assert cs["total_components"] == 20
    assert cs["untested_count"] == 16
    assert len(cs["untested_components"]) == 16


@pytest.mark.asyncio
async def test_viewport_debt_coverage_summary_counts_full_set(
    client: AsyncClient, db_session
):
    """coverage_summary reflects pre-truncation totals; untested_components capped at 50."""
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    membership = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(membership)
    await db_session.commit()

    # 60 components all untested — verifies the 50-cap on untested_components
    fake_rows_all = [
        {
            "component": f"admin-ui/src/components/Big{i}.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 60 - i,
            "has_viewport_test": False,
            "regression_case_count": 0,
            "source_url": None,
        }
        for i in range(60)
    ]
    top_rows = fake_rows_all[:15]
    untested_paths = [r["component"] for r in fake_rows_all]
    expected_summary = {
        "total_components": 60,
        "untested_count": 60,
        "untested_components": untested_paths[:50],
    }

    with patch(
        "app.viewport_debt._scan_viewport_debt_rows",
        return_value=(top_rows, [], expected_summary),
    ):
        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    cs = body["coverage_summary"]
    assert cs["total_components"] == 60
    assert cs["untested_count"] == 60
    # top_rows are the truncated 15; rows in response should be 15
    assert len(body["rows"]) == 15
    # untested_components capped at 50
    assert len(cs["untested_components"]) == 50


@pytest.mark.asyncio
async def test_viewport_debt_regression_cases_from_residual_records(
    client: AsyncClient, db_session
):
    """regression_case_count/breakdown are populated from ResidualRemediationCase
    viewport:* fingerprints; IGNORED/RESOLVED cases are excluded."""
    from unittest.mock import patch

    from app.models import ResidualCaseStatus, ResidualRemediationCase

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    membership = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(membership)
    await db_session.commit()

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _case(fingerprint, hit_count, status):
        return ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fingerprint,
            status=status,
            hit_count=hit_count,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )

    db_session.add_all(
        [
            _case("viewport:TestComp:overflow", 3, ResidualCaseStatus.ACTIVE),
            _case("viewport:TestComp:text_truncation", 1, ResidualCaseStatus.ACTIVE),
            # Excluded: IGNORED/RESOLVED cases must not contribute to counts.
            # (distinct fingerprints — (project_id, fingerprint) is unique.)
            _case("viewport:TestComp:blank_render", 99, ResidualCaseStatus.IGNORED),
            _case("viewport:TestComp:small_target", 50, ResidualCaseStatus.RESOLVED),
        ]
    )
    await db_session.commit()

    top_rows = [
        {
            "component": "admin-ui/src/components/TestComp.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 1,
            "has_viewport_test": False,
            "regression_case_count": 0,
            "regression_case_breakdown": {},
            "source_url": None,
        }
    ]
    summary = {
        "total_components": 1,
        "untested_count": 0,
        "untested_components": [],
    }

    with patch(
        "app.viewport_debt._scan_viewport_debt_rows",
        return_value=(top_rows, [], summary),
    ):
        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    row = next(
        r
        for r in response.json()["rows"]
        if r["component"] == "admin-ui/src/components/TestComp.tsx"
    )
    assert row["regression_case_count"] == 4
    assert row["regression_case_breakdown"] == {"overflow": 3, "text_truncation": 1}


@pytest.mark.asyncio
async def test_version_lifecycle_no_versions(client: AsyncClient, db_session):
    """Project with no release versions returns null current_version and empty history."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()
    assert data["current_version"] is None
    assert data["recent_versions"] == []


@pytest.mark.asyncio
async def test_version_lifecycle_with_open_version(client: AsyncClient, db_session):
    """Open version fields are surfaced including qualification data."""

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        qualification_attempts=2,
        last_qualification={
            "status": "dry_run",
            "confidence": 0.92,
            "threshold": 0.90,
            "sub_scores": {"audit": 0.95, "test_erosion": 0.88, "smoketest": 0.90},
            "ci_resolution": {
                "state": "green",
                "resolved_sha": "abc1234567",
                "detail": "candidate commit check-runs: green",
            },
            "blocker": None,
            "finalized_at": "2026-01-01T00:00:00",
            "candidate_sha": "abc1234567",
        },
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert cv["version_number"] == 3
    assert cv["phase"] == "stabilize"
    assert cv["qualification_attempts"] == 2
    assert cv["merged_task_count"] == 0

    qual = cv["last_qualification"]
    assert qual["status"] == "dry_run"
    assert qual["confidence"] == 0.92
    assert qual["threshold"] == 0.90
    assert qual["sub_scores"]["audit"] == 0.95
    assert qual["ci_resolution"]["state"] == "green"
    assert qual["ci_resolution"]["resolved_sha"] == "abc1234567"
    assert qual["blocker"] is None
    # The evaluated candidate commit is surfaced so the UI can detect when the
    # latest audit's findings were computed against a different (older) commit.
    assert qual["candidate_sha"] == "abc1234567"

    assert data["recent_versions"] == []


@pytest.mark.asyncio
async def test_version_lifecycle_phase_reasoning_surfaced(
    client: AsyncClient, db_session
):
    """phase_reasoning is included in the current_version response when set."""

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="stabilize",
        status="open",
        qualification_attempts=0,
        phase_reasoning="Moved to stabilize: 8 tasks merged and build confidence above threshold.",
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert (
        cv["phase_reasoning"]
        == "Moved to stabilize: 8 tasks merged and build confidence above threshold."
    )


@pytest.mark.asyncio
async def test_version_lifecycle_phase_reasoning_null_when_unset(
    client: AsyncClient, db_session
):
    """phase_reasoning is null in the response when no reasoning has been recorded."""

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=6,
        phase="build",
        status="open",
        qualification_attempts=0,
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert cv["phase_reasoning"] is None


@pytest.mark.asyncio
async def test_version_lifecycle_history(client: AsyncClient, db_session):
    """Closed versions appear in recent_versions, open version in current_version."""

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        qualification_attempts=1,
        last_qualification={"status": "promoted", "confidence": 0.95},
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        qualification_attempts=0,
    )
    db_session.add(closed)
    db_session.add(open_v)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    assert data["current_version"]["version_number"] == 2
    assert len(data["recent_versions"]) == 1
    assert data["recent_versions"][0]["version_number"] == 1
    assert data["recent_versions"][0]["status"] == "promoted"
    assert data["recent_versions"][0]["last_qualification"]["confidence"] == 0.95


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_fields_present(
    client: AsyncClient, db_session
):
    """promotion_enabled and promotion_readiness are always present in the response."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["promotion_enabled"], bool)
    pr = data["promotion_readiness"]
    assert pr is not None
    assert "ready" in pr
    assert "reason" in pr
    assert "verdicts_evaluated" in pr


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_enabled_true(
    client: AsyncClient, db_session
):
    """When promotion_enabled is True in runtime_config, the endpoint reflects it."""
    import app.shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    original = _ss.runtime_config.get("promotion_enabled", False)
    try:
        _ss.runtime_config["promotion_enabled"] = True
        response = await client.get(
            f"/api/admin/projects/{project.id}/version-lifecycle"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["promotion_enabled"] is True
    finally:
        _ss.runtime_config["promotion_enabled"] = original


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_readiness_not_ready(
    client: AsyncClient, db_session
):
    """With no dry-run verdicts the readiness summary reports not-ready."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    pr = data["promotion_readiness"]
    assert pr["ready"] is False
    assert pr["verdicts_evaluated"] == 0


@pytest.mark.asyncio
async def test_digest_memo_pathway_exits_zero_without_project(client: AsyncClient):
    """Without project_id, memo_pathway_exits is 0."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["memo_pathway_exits"] == 0


@pytest.mark.asyncio
async def test_digest_memo_pathway_exits_counts_completed_memo_tasks(
    client: AsyncClient, db_session
):
    """Digest surfaces completed memo tasks count via memo_pathway_exits."""
    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Two completed memo tasks
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "memo"}],
            )
        )
    # One completed non-memo task — should not count
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
        )
    )
    # One in-progress memo task — should not count
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "memo"}],
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["memo_pathway_exits"] == 2


@pytest.mark.asyncio
async def test_digest_latest_cycle_includes_memo_stats(client: AsyncClient, db_session):
    """When a completed cycle has memo counts in cycle_metadata, they appear in latest_cycle."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create a completed strategic cycle with memo counts
    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase1_completed(db_session, cycle.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session, cycle.id, memos_available=4, memos_cited=1
    )
    await db_session.commit()

    # Wire the cycle into project.vision._strategic_cycle_history so the
    # digest's legacy-JSONB path can find it.
    from sqlalchemy.orm.attributes import flag_modified

    await db_session.refresh(cycle)
    vision = dict(project.vision or {})
    vision["_strategic_cycle_history"] = [
        {
            "cycle_id": cycle.id,
            "started_at": cycle.phase1_started_at.isoformat()
            if cycle.phase1_started_at
            else None,
            "phase2_completed_at": cycle.phase2_completed_at.isoformat()
            if cycle.phase2_completed_at
            else None,
            "outcome": cycle.outcome,
            "directions_created": 0,
            "directions_retired": 0,
            "vision_updated": False,
        }
    ]
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    lc = data.get("latest_cycle")
    assert lc is not None
    assert lc["memos_available"] == 4
    assert lc["memos_cited"] == 1


async def _wire_cycle_into_vision(db_session, project, cycle):
    """Wire a completed cycle into project.vision._strategic_cycle_history so the
    digest's legacy-JSONB latest_cycle path can find it."""
    from sqlalchemy.orm.attributes import flag_modified

    await db_session.refresh(cycle)
    vision = dict(project.vision or {})
    vision["_strategic_cycle_history"] = [
        {
            "cycle_id": cycle.id,
            "started_at": cycle.phase1_started_at.isoformat()
            if cycle.phase1_started_at
            else None,
            "phase2_completed_at": cycle.phase2_completed_at.isoformat()
            if cycle.phase2_completed_at
            else None,
            "outcome": cycle.outcome,
            "directions_created": 0,
            "directions_retired": 0,
            "vision_updated": False,
        }
    ]
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()


@pytest.mark.asyncio
async def test_digest_latest_cycle_includes_compact_challenge_summary(
    client: AsyncClient, db_session
):
    """A cycle carrying a strategic_judgment_challenge record surfaces the compact
    {mode, verdict, warned_count, blocked_count} summary in latest_cycle —
    never the full bounded item list."""
    from app import strategic_cycle as _sc
    from app.strategic_completion import sanitize_strategic_challenge_record

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    record = sanitize_strategic_challenge_record(
        {
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "approved"},
                {"direction_id": "d-2", "status": "met", "verdict": "rejected"},
            ],
            "retirement_critique_quality": {"verdict": "uncertain"},
            "portfolio_shape_risk": {"verdict": "solid"},
        }
    )
    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase1_completed(db_session, cycle.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session,
        cycle.id,
        extra_metadata={"strategic_judgment_challenge": record},
    )
    await db_session.commit()
    await _wire_cycle_into_vision(db_session, project, cycle)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    lc = response.json().get("latest_cycle")
    assert lc is not None
    challenge = lc["strategic_judgment_challenge"]
    assert challenge == {
        "mode": "ok",
        "verdict": "blocked",  # one rejected evidence item
        "warned_count": 1,  # one uncertain section verdict
        "blocked_count": 1,
        "gate_mode": "enforce",
    }
    # The bounded item list never travels on the digest summary.
    assert "evidence_progress" not in challenge


@pytest.mark.asyncio
async def test_digest_latest_cycle_omits_challenge_for_legacy_cycle(
    client: AsyncClient, db_session
):
    """Cycles with no challenge record omit the key entirely so absence does not
    read as a failure."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(db_session, cycle.id)
    await db_session.commit()
    await _wire_cycle_into_vision(db_session, project, cycle)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    lc = response.json().get("latest_cycle")
    assert lc is not None
    assert "strategic_judgment_challenge" not in lc


@pytest.mark.asyncio
async def test_digest_memo_ever_cited_false_when_no_citations(
    client: AsyncClient, db_session
):
    """memo_ever_cited.ever_cited is False when no cycle has memos_cited > 0."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session, cycle.id, memos_available=3, memos_cited=0
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    mec = data.get("memo_ever_cited")
    assert mec is not None
    assert mec["ever_cited"] is False
    assert mec["first_cycle_id"] is None


@pytest.mark.asyncio
async def test_digest_memo_ever_cited_true_with_oldest_cycle_cited(
    client: AsyncClient, db_session
):
    """memo_ever_cited.ever_cited=True even when citation is outside the 20-cycle display window."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create 22 cycles; only the first one has a citation (outside recent 20-cycle window)
    first_cycle_id = None
    for i in range(22):
        c = await _sc.create(db_session, project_id=project.id)
        await db_session.commit()
        cited = 1 if i == 0 else 0
        await _sc.mark_phase2_completed(
            db_session, c.id, memos_available=1, memos_cited=cited
        )
        await db_session.commit()
        if i == 0:
            first_cycle_id = c.id

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    mec = data.get("memo_ever_cited")
    assert mec is not None
    assert mec["ever_cited"] is True
    assert mec["first_cycle_id"] == first_cycle_id


@pytest.mark.asyncio
async def test_list_qualification_verdicts_empty(client: AsyncClient, db_session):
    """Project with no QualificationVerdict rows returns an empty list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["verdicts"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_qualification_verdicts_returns_rows_newest_first(
    client: AsyncClient, db_session
):
    """Verdict rows are returned newest-first with all expected fields present."""
    from app.models import QualificationVerdict

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=2,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    older = now - timedelta(hours=1)

    v1 = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc1234" + "0" * 33,
        confidence=0.88,
        sub_scores={"audit": 0.90, "test_erosion": 0.85, "smoketest": 0.89},
        blocker=None,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="dry_run",
        threshold=0.90,
        started_at=older - timedelta(minutes=5),
        finalized_at=older,
        created_at=older,
    )
    v2 = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="def5678" + "0" * 33,
        confidence=0.93,
        sub_scores={"audit": 0.95, "test_erosion": 0.91, "smoketest": 0.92},
        blocker=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.90,
        started_at=now - timedelta(minutes=3),
        finalized_at=now,
        created_at=now,
    )
    db_session.add(v1)
    db_session.add(v2)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 2
    verdicts = data["verdicts"]
    assert len(verdicts) == 2

    # Newest first — v2 was created at `now`, v1 at `older`
    newest = verdicts[0]
    assert newest["terminal_status"] == "promoted"
    assert newest["would_promote"] is True
    assert newest["promotion_enabled"] is True
    assert newest["confidence"] == 0.93
    assert newest["sub_scores"]["audit"] == 0.95
    assert newest["blocker"] is None
    assert newest["threshold"] == 0.90
    assert newest["candidate_sha"] == "def5678" + "0" * 33
    assert newest["started_at"] is not None
    assert newest["finalized_at"] is not None
    assert newest["created_at"] is not None
    assert "id" in newest
    assert "release_version_id" in newest

    older_verdict = verdicts[1]
    assert older_verdict["terminal_status"] == "dry_run"
    assert older_verdict["would_promote"] is False


@pytest.mark.asyncio
async def test_list_qualification_verdicts_limit_param(client: AsyncClient, db_session):
    """The limit query parameter is respected and caps the result set."""
    from app.models import QualificationVerdict

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=3,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    for i in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                confidence=0.80 + i * 0.05,
                terminal_status="dry_run",
                would_promote=False,
                promotion_enabled=False,
                created_at=now - timedelta(hours=i),
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts?limit=2"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["verdicts"]) == 2


@pytest.mark.asyncio
async def test_list_qualification_verdicts_only_own_project(
    client: AsyncClient, db_session
):
    """Verdicts from another project are not included in the response."""
    from app.models import QualificationVerdict

    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)
    db_session.add(make_membership(project_a.id))
    db_session.add(make_membership(project_b.id))

    ver_a = ReleaseVersion(
        project_id=project_a.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=1,
    )
    ver_b = ReleaseVersion(
        project_id=project_b.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=1,
    )
    db_session.add(ver_a)
    db_session.add(ver_b)
    await db_session.commit()
    await db_session.refresh(ver_a)
    await db_session.refresh(ver_b)

    now = _utcnow()
    db_session.add(
        QualificationVerdict(
            project_id=project_a.id,
            release_version_id=ver_a.id,
            confidence=0.88,
            terminal_status="dry_run",
            would_promote=False,
            promotion_enabled=False,
            created_at=now,
        )
    )
    db_session.add(
        QualificationVerdict(
            project_id=project_b.id,
            release_version_id=ver_b.id,
            confidence=0.75,
            terminal_status="dry_run",
            would_promote=False,
            promotion_enabled=False,
            created_at=now,
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project_a.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["verdicts"][0]["confidence"] == 0.88


@pytest.mark.asyncio
async def test_list_qualification_verdicts_requires_auth(
    unauthenticated_client: AsyncClient, db_session
):
    """Unauthenticated requests are rejected."""
    import uuid

    fake_id = uuid.uuid4()
    response = await unauthenticated_client.get(
        f"/api/admin/projects/{fake_id}/qualification-verdicts"
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_warm_reuse_funnel_empty(client: AsyncClient, db_session):
    """Endpoint returns all-zero counters when no funnel events have been recorded."""
    import app.warm_backlog_reuse as wbr

    # Reset counters so prior test runs don't bleed over.
    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/warm-reuse-funnel")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_vms_with_eligible_backlog"] == 0
    assert data["teardown_defers_scheduled"] == 0
    assert data["next_role_polls_during_defer"] == 0
    assert data["warm_claims_succeeded"] == 0
    assert data["claim_skipped_total"] == 0
    assert data["claim_skipped_by_reason"] == {}
    assert data["window_hours"] == 24


@pytest.mark.asyncio
async def test_warm_reuse_funnel_counts_after_records(client: AsyncClient, db_session):
    """Counter functions increment correctly and are reflected in the endpoint."""
    import app.warm_backlog_reuse as wbr

    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    wbr.record_vm_with_eligible_backlog()
    wbr.record_vm_with_eligible_backlog()
    wbr.record_teardown_defer()
    wbr.record_next_role_poll_during_defer()
    wbr.record_warm_claim_succeeded()
    wbr.record_claim_skipped("no_eligible")
    wbr.record_claim_skipped("lost_claim_race")
    wbr.record_claim_skipped("no_eligible")

    response = await client.get(f"/api/admin/projects/{project.id}/warm-reuse-funnel")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_vms_with_eligible_backlog"] == 2
    assert data["teardown_defers_scheduled"] == 1
    assert data["next_role_polls_during_defer"] == 1
    assert data["warm_claims_succeeded"] == 1
    assert data["claim_skipped_total"] == 3
    assert data["claim_skipped_by_reason"]["no_eligible"] == 2
    assert data["claim_skipped_by_reason"]["lost_claim_race"] == 1


@pytest.mark.asyncio
async def test_warm_reuse_funnel_requires_auth(unauthenticated_client: AsyncClient):
    """Unauthenticated requests are rejected."""
    import uuid

    fake_id = uuid.uuid4()
    response = await unauthenticated_client.get(
        f"/api/admin/projects/{fake_id}/warm-reuse-funnel"
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_narrative_states_convergence_verdict_when_signal_exists(
    client: AsyncClient, db_session
):
    """When the window carries review/fix churn signal and one cause clearly
    dominates the non-converged PR-chains, the digest narrative states a
    plain-language churn verdict and names the quantified blocker."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # A plain failed task so the narrative bypasses the "All clear" early return
    # and actually reaches the convergence-verdict block.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    # Two non-converged PR-chains (review_loop with churn_detections, never
    # merged) → churn is the single dominant blocker across both chains.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                context={"review_loop": {"iteration": 2, "churn_detections": 3}},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    narrative = response.json()["narrative_summary"]

    # Churn lands in today's trend bucket (older buckets are empty) so the
    # verdict reads "rising", and churn dominates both non-converged chains.
    assert (
        "Review/fix churn is rising; the main blocker is churn "
        "(2/2 non-converged PR chains)." in narrative
    )


@pytest.mark.asyncio
async def test_narrative_suppresses_convergence_verdict_on_thin_window(
    client: AsyncClient, db_session
):
    """A window with activity but no review/fix churn signal carries no
    convergence verdict — the sentence is gated on signal sufficiency."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # Real activity (so the digest is non-empty) but no review_loop chains, so
    # there is no churn signal to narrate.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    narrative = response.json()["narrative_summary"]

    assert "Review/fix churn" not in narrative
    assert "non-converged PR chains" not in narrative


@pytest.mark.asyncio
async def test_digest_includes_sr_false_attestation_trends_from_design_doc(
    client: AsyncClient, db_session
):
    """When sr_false_attestation_trends is in project.design_doc, it appears in digest."""
    project = make_project()
    project.design_doc = {
        "sr_false_attestation_trends": {
            "computed_at": "2026-06-05T10:00:00",
            "bucket_mode": "by_cycle",
            "per_category": {
                "tests_executed_cleanly": {
                    "false_attestation_rate": 0.05,
                    "previous_false_attestation_rate": 0.08,
                    "delta": -0.03,
                    "trend": "improving",
                    "ci_confirmed": 10,
                    "false_attested": 1,
                    "ci_data_missing": 0,
                }
            },
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200

    data = response.json()
    assert "sr_false_attestation_trends" in data
    assert data["sr_false_attestation_trends"] is not None
    assert data["sr_false_attestation_trends"]["computed_at"] == "2026-06-05T10:00:00"
    assert (
        "tests_executed_cleanly" in data["sr_false_attestation_trends"]["per_category"]
    )
    assert (
        data["sr_false_attestation_trends"]["per_category"]["tests_executed_cleanly"][
            "false_attestation_rate"
        ]
        == 0.05
    )


# ── Cumulative capability-trial accumulation ─────────────────────────────────


def _arm_task(project_id, *, had_repo_map, first_attempt_succeeded, days_ago, idx):
    """A terminal task carrying both trial-arm flags, backdated by ``days_ago``."""
    created = _utcnow() - timedelta(days=days_ago)
    return make_task(
        project_id=project_id,
        title=f"arm-task-{idx}",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=created,
        context={
            "_had_repo_map": had_repo_map,
            # assignment key mirrors receipt — post-rollout tasks carry both.
            "_repo_map_arm": had_repo_map,
            "_first_attempt_succeeded": first_attempt_succeeded,
        },
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_accumulation_counts_cumulatively(db_session):
    """The aggregate counts every flagged terminal task regardless of age, and is
    project-scoped + ignores tasks missing either arm flag."""
    project = make_project()
    other = make_project()
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    tasks = []
    # WITH arm: 8 tasks, 6 first-attempt successes — spread across many days so no
    # single 24h window could hold them.
    for i in range(8):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=(i < 6),
                days_ago=i + 1,
                idx=i,
            )
        )
    # WITHOUT arm: 5 tasks, 1 first-attempt success.
    for i in range(5):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=False,
                first_attempt_succeeded=(i < 1),
                days_ago=i + 1,
                idx=100 + i,
            )
        )
    # Noise that must NOT be counted: a task missing the arm flag, and a task on a
    # different project.
    tasks.append(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},  # no _had_repo_map
        )
    )
    tasks.append(
        _arm_task(
            other.id,
            had_repo_map=True,
            first_attempt_succeeded=True,
            days_ago=1,
            idx=999,
        )
    )
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session, project_id=project.id, context_key="_had_repo_map"
    )
    assert accum["with_n"] == 8
    assert accum["with_succ"] == 6
    assert accum["without_n"] == 5
    assert accum["without_succ"] == 1
    assert accum["with_rate"] == 0.75
    assert accum["without_rate"] == 0.2
    assert accum["lift"] == 0.55


@pytest.mark.asyncio
async def test_compute_trial_arm_accumulation_empty_arm_yields_none_rate(db_session):
    """An empty arm reports a None rate and None lift (cannot decide)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Only WITH-arm tasks present.
    db_session.add_all(
        [
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=1,
                idx=i,
            )
            for i in range(3)
        ]
    )
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session, project_id=project.id, context_key="_had_repo_map"
    )
    assert accum["with_n"] == 3
    assert accum["without_n"] == 0
    assert accum["without_rate"] is None
    assert accum["lift"] is None


@pytest.mark.asyncio
async def test_compute_trial_arm_accumulation_intent_to_treat(db_session):
    """When assignment_key is provided, partitioning uses COALESCE(assignment_key,
    context_key) — a treatment-assigned task with no actual map still lands in WITH arm,
    and legacy tasks (no assignment_key) fall back to the receipt key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = _utcnow() - timedelta(days=1)

    def _task(context):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=created,
            context=context,
        )

    tasks = [
        # Treatment-assigned, received map → WITH arm, success
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        # Treatment-assigned, NO map (empty map case) → still WITH arm, failure
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": False,
                "_first_attempt_succeeded": False,
            }
        ),
        # Holdback-assigned, had map anyway → WITHOUT arm, success
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        # Legacy task (no assignment key) — falls back to receipt key, WITH arm
        _task({"_had_repo_map": True, "_first_attempt_succeeded": True}),
        # Legacy task (no assignment key) — falls back to receipt key, WITHOUT arm
        _task({"_had_repo_map": False, "_first_attempt_succeeded": False}),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        assignment_key="_repo_map_arm",
    )
    # WITH arm: 2 assigned + 1 legacy receipt = 3 tasks, 2 successes
    assert accum["with_n"] == 3
    assert accum["with_succ"] == 2
    # WITHOUT arm: 1 assigned holdback + 1 legacy receipt = 2 tasks, 1 success
    assert accum["without_n"] == 2
    assert accum["without_succ"] == 1
    assert accum["with_rate"] == round(2 / 3, 2)
    assert accum["without_rate"] == 0.5
    assert accum["lift"] == round(round(2 / 3, 2) - 0.5, 2)


@pytest.mark.asyncio
async def test_compute_trial_arm_accrual_windows_by_created_at(db_session):
    """Accrual counts assigned-arm tasks created within the window and excludes
    older ones, partitioning WITH/WITHOUT by the assignment flag's truthiness."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    tasks = []
    # In-window WITH arm: 4 tasks created 1–4 days ago.
    for i in range(4):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=i + 1,
                idx=i,
            )
        )
    # In-window WITHOUT arm: 2 tasks created 1–2 days ago.
    for i in range(2):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=False,
                first_attempt_succeeded=False,
                days_ago=i + 1,
                idx=50 + i,
            )
        )
    # Out-of-window (older than 7 days) — must be excluded.
    tasks.append(
        _arm_task(
            project.id,
            had_repo_map=True,
            first_attempt_succeeded=True,
            days_ago=10,
            idx=99,
        )
    )
    db_session.add_all(tasks)
    await db_session.commit()

    accrual = await compute_trial_arm_accrual(
        db_session, project_id=project.id, assignment_key="_repo_map_arm"
    )
    assert accrual["with_7d"] == 4
    assert accrual["without_7d"] == 2
    assert accrual["window_days"] == 7


@pytest.mark.asyncio
async def test_compute_trial_arm_accrual_project_scoped_and_ignores_null_arm(
    db_session,
):
    """Accrual is project-scoped and excludes tasks missing the assignment flag."""
    project = make_project()
    other = make_project()
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    tasks = [
        _arm_task(
            project.id,
            had_repo_map=True,
            first_attempt_succeeded=True,
            days_ago=1,
            idx=0,
        ),
        # Different project — must not count.
        _arm_task(
            other.id, had_repo_map=True, first_attempt_succeeded=True, days_ago=1, idx=1
        ),
        # No assignment key at all — must not count.
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=_utcnow() - timedelta(days=1),
            context={"_had_repo_map": True, "_first_attempt_succeeded": True},
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accrual = await compute_trial_arm_accrual(
        db_session, project_id=project.id, assignment_key="_repo_map_arm"
    )
    assert accrual["with_7d"] == 1
    assert accrual["without_7d"] == 0


@pytest.mark.asyncio
async def test_compute_trial_arm_accrual_window_days_param_respected(db_session):
    """A custom window_days widens the lookback and is echoed in the result."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=1,
                idx=0,
            ),
            # 10 days old: outside default 7d, inside a 14d window.
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=10,
                idx=1,
            ),
        ]
    )
    await db_session.commit()

    default = await compute_trial_arm_accrual(
        db_session, project_id=project.id, assignment_key="_repo_map_arm"
    )
    assert default["with_7d"] == 1

    widened = await compute_trial_arm_accrual(
        db_session,
        project_id=project.id,
        assignment_key="_repo_map_arm",
        window_days=14,
    )
    assert widened["with_7d"] == 2
    assert widened["window_days"] == 14


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_uses_cache_aware_pricing(db_session):
    """Per-arm cost deltas use the shared cache-aware LLM task pricing helper."""
    from app.efficiency_snapshot import (
        LLM_CACHE_READ_INPUT_PRICING,
        LLM_TOKEN_PRICING,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_rate, _output_rate = LLM_TOKEN_PRICING[(provider, model)]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[(provider, model)]
    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                api_call_summary={
                    "total_input_tokens": 1_000_000,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                    "total_cache_creation_input_tokens": 0,
                    "total_cache_read_input_tokens": 1_000_000,
                },
                provider=provider,
                model=model,
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                api_call_summary={
                    "total_input_tokens": 1_000_000,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
                provider=provider,
                model=model,
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    with_expected = round(1_000_000 * cache_read_rate, 6)
    without_expected = round(1_000_000 * input_rate, 6)
    assert result["with_avg_cost_usd"] == pytest.approx(with_expected)
    assert result["without_avg_cost_usd"] == pytest.approx(without_expected)
    assert result["cost_delta"] == pytest.approx(
        round(with_expected - without_expected, 6)
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_prices_banked_retry_cache(db_session):
    """Banked retry cache sub-counts are priced like shared per-attempt LLM cost."""
    from app.efficiency_snapshot import (
        LLM_CACHE_READ_INPUT_PRICING,
        LLM_TOKEN_PRICING,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_rate, _output_rate = LLM_TOKEN_PRICING[(provider, model)]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[(provider, model)]
    banked_input = 1_000_000
    current_input = 500_000

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                provider=provider,
                model=model,
                llm_input_tokens_banked_db=banked_input,
                llm_output_tokens_banked_db=0,
                llm_call_count_banked_db=1,
                attempt_history_db=[
                    {
                        "provider": provider,
                        "model": model,
                        "llm_input_tokens": banked_input,
                        "llm_output_tokens": 0,
                        "llm_call_count": 1,
                        "llm_cache_creation_input_tokens": 0,
                        "llm_cache_read_input_tokens": banked_input,
                    }
                ],
                api_call_summary={
                    "total_input_tokens": current_input,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                provider=provider,
                model=model,
                llm_input_tokens_banked_db=banked_input,
                llm_output_tokens_banked_db=0,
                llm_call_count_banked_db=1,
                attempt_history_db=[
                    {
                        "provider": provider,
                        "model": model,
                        "llm_input_tokens": banked_input,
                        "llm_output_tokens": 0,
                        "llm_call_count": 1,
                    }
                ],
                api_call_summary={
                    "total_input_tokens": current_input,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    with_expected = round(
        banked_input * cache_read_rate + current_input * input_rate,
        6,
    )
    without_expected = round((banked_input + current_input) * input_rate, 6)
    assert result["with_avg_cost_usd"] == pytest.approx(with_expected)
    assert result["without_avg_cost_usd"] == pytest.approx(without_expected)
    assert result["cost_delta"] == pytest.approx(
        round(with_expected - without_expected, 6)
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_keeps_legacy_banked_scalar(db_session):
    """Rows without attempt_history still price banked retry scalar tokens."""
    from app.efficiency_snapshot import LLM_TOKEN_PRICING

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_rate, _output_rate = LLM_TOKEN_PRICING[(provider, model)]
    banked_input = 700_000
    current_input = 300_000

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                provider=provider,
                model=model,
                llm_input_tokens_banked_db=banked_input,
                llm_output_tokens_banked_db=0,
                api_call_summary={
                    "total_input_tokens": current_input,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                provider=provider,
                model=model,
                api_call_summary={
                    "total_input_tokens": current_input,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    with_expected = round((banked_input + current_input) * input_rate, 6)
    without_expected = round(current_input * input_rate, 6)
    assert result["with_avg_cost_usd"] == pytest.approx(with_expected)
    assert result["without_avg_cost_usd"] == pytest.approx(without_expected)
    assert result["cost_delta"] == pytest.approx(
        round(with_expected - without_expected, 6)
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_ignores_non_array_attempt_history(
    db_session,
):
    """Malformed legacy attempt_history JSON falls back to banked scalar tokens."""
    from app.efficiency_snapshot import LLM_TOKEN_PRICING

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_rate, _output_rate = LLM_TOKEN_PRICING[(provider, model)]
    banked_input = 700_000
    current_input = 300_000
    malformed_history_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"_trial_arm": True},
        provider=provider,
        model=model,
        llm_input_tokens_banked_db=banked_input,
        llm_output_tokens_banked_db=0,
        api_call_summary={
            "total_input_tokens": current_input,
            "total_output_tokens": 0,
            "llm_call_count": 1,
        },
    )
    malformed_history_task.attempt_history_db = {"unexpected": "object"}

    db_session.add_all(
        [
            malformed_history_task,
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                provider=provider,
                model=model,
                api_call_summary={
                    "total_input_tokens": current_input,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    with_expected = round((banked_input + current_input) * input_rate, 6)
    without_expected = round(current_input * input_rate, 6)
    assert result["with_avg_cost_usd"] == pytest.approx(with_expected)
    assert result["without_avg_cost_usd"] == pytest.approx(without_expected)
    assert result["cost_delta"] == pytest.approx(
        round(with_expected - without_expected, 6)
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_averages_per_terminal_arm_task(
    db_session,
):
    """Zero-token terminal arm tasks remain in the per-arm average denominator."""
    from app.efficiency_snapshot import LLM_TOKEN_PRICING

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_rate, _output_rate = LLM_TOKEN_PRICING[(provider, model)]
    input_tokens = 1_000_000

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                provider=provider,
                model=model,
                api_call_summary={
                    "total_input_tokens": input_tokens,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                provider=provider,
                model=model,
                api_call_summary={
                    "total_input_tokens": input_tokens,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    with_expected = round(input_tokens * input_rate / 2, 6)
    without_expected = round(input_tokens * input_rate, 6)
    assert result["with_avg_cost_usd"] == pytest.approx(with_expected)
    assert result["without_avg_cost_usd"] == pytest.approx(without_expected)
    assert result["cost_delta"] == pytest.approx(
        round(with_expected - without_expected, 6)
    )


@pytest.mark.asyncio
async def test_compute_trial_arm_cost_delta_counts_distinct_unpriced_tasks(
    db_session,
):
    """Multiple unpriced attempts on one task count as one unpriced task."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                provider="unknown-provider",
                model="unknown-model",
                attempt_history_db=[
                    {
                        "provider": "unknown-provider",
                        "model": "unknown-model-a",
                        "llm_input_tokens": 1,
                        "llm_output_tokens": 0,
                        "llm_call_count": 1,
                    },
                    {
                        "provider": "unknown-provider",
                        "model": "unknown-model-b",
                        "llm_input_tokens": 1,
                        "llm_output_tokens": 0,
                        "llm_call_count": 1,
                    },
                ],
                api_call_summary={
                    "total_input_tokens": 1,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": False},
                provider="deepseek",
                model="deepseek-v4-flash",
                api_call_summary={
                    "total_input_tokens": 1_000_000,
                    "total_output_tokens": 0,
                    "llm_call_count": 1,
                },
            ),
        ]
    )
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    assert result["with_avg_cost_usd"] is None
    assert result["cost_delta"] is None
    assert result["with_unpriced_task_count"] == 1
    assert result["without_unpriced_task_count"] == 0


@pytest.mark.asyncio
async def test_digest_exposes_accrual_projection_for_assignment_trial(db_session):
    """The digest attaches accrual_projection to assignment-based trials except the lane pilot.
    The projection is ephemeral and must NOT be persisted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Enough recent assigned-arm tasks for both arms to have a non-zero accrual rate.
    tasks = []
    for i in range(7):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=1,
                idx=i,
            )
        )
    for i in range(7):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=False,
                first_attempt_succeeded=False,
                days_ago=1,
                idx=50 + i,
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    result = await _compute_digest(hours=24, project_id=project.id, session=db_session)
    verdicts = result["capability_adoption_verdicts"]

    repo_map = verdicts["repo_map"]
    proj = repo_map["accrual_projection"]
    assert proj["with_7d"] == 7
    assert proj["without_7d"] == 7
    assert proj["window_days"] == 7
    assert "holdback_fraction" in proj
    assert "days_to_min_n" in proj

    # Generic assignment-based trials get a projection; the lane pilot is excluded.
    assert "accrual_projection" in verdicts["module_interfaces"]
    assert "accrual_projection" not in verdicts["review_fix_lane"]

    # The accrual projection is durably persisted onto the verdict entry so
    # strategic readers can observe it without hitting the admin digest endpoint.
    await db_session.refresh(project)
    stored = project.capability_adoption_verdicts["repo_map"]
    assert "accrual_projection" in stored
    assert stored["accrual_projection"]["window_days"] == 7


@pytest.mark.asyncio
async def test_trial_verdict_accumulates_across_windows_to_decision(db_session):
    """With per-arm tasks spread across MULTIPLE 24h windows (more than any single
    window holds under the min-N=30 threshold), the verdict converges to a decided
    adopt — driven by the cumulative basis, not the (empty) digest window — and the
    recorded entry exposes that cumulative basis distinct from window_hours."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    tasks = []
    # 35 WITH-arm successes and 35 WITHOUT-arm failures, every one backdated 2–8
    # days so the 24h digest window sees NONE of them. No single window holds the
    # 30-per-arm minimum, so a windowed decision basis would stay continue-trial.
    for i in range(35):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=True,
                first_attempt_succeeded=True,
                days_ago=2 + (i % 7),
                idx=i,
            )
        )
    for i in range(35):
        tasks.append(
            _arm_task(
                project.id,
                had_repo_map=False,
                first_attempt_succeeded=False,
                days_ago=2 + (i % 7),
                idx=200 + i,
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    result = await _compute_digest(hours=24, project_id=project.id, session=db_session)

    # Windowed breakdown is empty (all tasks are older than 24h) — proving the
    # decision did NOT come from the window.
    fasr = result["first_attempt_success_rate"]
    assert fasr["with_repo_map_n"] == 0
    assert fasr["without_repo_map_n"] == 0

    await db_session.refresh(project)
    verdict = project.capability_adoption_verdicts["repo_map"]
    assert verdict["verdict"] == "adopt"
    assert verdict["n_with"] == 35
    assert verdict["n_without"] == 35
    assert verdict["accumulation_basis"] == "cumulative_terminal_arm_tasks"
    # window_hours still describes the dashboard window, NOT the decision basis.
    assert verdict["window_hours"] == 24


@pytest.mark.asyncio
async def test_trial_verdict_insufficient_accumulation_stays_continue_trial(db_session):
    """Below the min-N-per-arm threshold the cumulative verdict stays continue-trial
    (insufficient_data) and still records the auditable accumulation basis."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    tasks = [
        _arm_task(
            project.id,
            had_repo_map=True,
            first_attempt_succeeded=True,
            days_ago=2,
            idx=i,
        )
        for i in range(5)
    ] + [
        _arm_task(
            project.id,
            had_repo_map=False,
            first_attempt_succeeded=False,
            days_ago=2,
            idx=100 + i,
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    await _compute_digest(hours=24, project_id=project.id, session=db_session)

    await db_session.refresh(project)
    verdict = project.capability_adoption_verdicts["repo_map"]
    assert verdict["verdict"] == "continue-trial"
    assert verdict["verdict_reason"] == "insufficient_data"
    assert verdict["n_with"] == 5
    assert verdict["n_without"] == 2
    assert verdict["accumulation_basis"] == "cumulative_terminal_arm_tasks"


@pytest.mark.asyncio
async def test_digest_latest_cycle_from_table_no_jsonb_needed(
    client: AsyncClient, db_session
):
    """digest.latest_cycle is populated from the StrategicCycle table directly —
    no vision._strategic_cycle_history JSONB wiring required (retired migration 0047)."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create and complete a cycle without touching vision JSONB at all.
    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase1_completed(db_session, cycle.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session,
        cycle.id,
        directions_created=3,
        directions_retired=1,
        vision_updated=True,
        summary_text="cycle summary",
        memos_available=5,
        memos_cited=2,
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    lc = response.json().get("latest_cycle")
    assert lc is not None, (
        "latest_cycle should be non-null when a completed cycle exists"
    )
    assert lc["cycle_id"] == cycle.id
    assert lc["outcome"] == _sc.OUTCOME_COMPLETED
    assert lc["directions_created"] == 3
    assert lc["directions_retired"] == 1
    assert lc["vision_updated"] is True
    assert lc["summary_text"] == "cycle summary"
    assert lc["memos_available"] == 5
    assert lc["memos_cited"] == 2


@pytest.mark.asyncio
async def test_digest_latest_cycle_null_when_no_completed_cycles(
    client: AsyncClient, db_session
):
    """digest.latest_cycle is null when no terminal (non-active) cycles exist."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create a cycle but leave it in an active (in-progress) state.
    await _sc.create(db_session, project_id=project.id)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    lc = response.json().get("latest_cycle")
    assert lc is None, "latest_cycle should be null when only active cycles exist"


@pytest.mark.asyncio
async def test_digest_stale_fallback_is_annotated_served_stale(client: AsyncClient):
    """GET /api/admin/digest L3 path annotates served_stale=True when the cache
    serves a stale fallback (compute failed, returning last-good result).

    The precompute L1 cache never has worker-written entries in tests (the
    background worker doesn't run), and the shared L2 cache is empty in a clean
    test DB, so the L3 DigestCache path is always taken here.
    """
    from unittest.mock import MagicMock
    from app.digest_cache import DigestCache, get_digest_cache

    stale_payload = {
        "window_hours": 24,
        "completed_count": 3,
        "failed_count": 2,
        "pending_approval_count": 0,
        "active_count": 0,
        "running_count": 0,
        "queued_count": 0,
        "research_cycles": 0,
        "prs_opened": 0,
        "generated_at": "2026-06-01T00:00:00",
    }

    mock_cache = MagicMock(spec=DigestCache)
    mock_cache.get_or_compute_meta = AsyncMock(return_value=(stale_payload, True))

    app.dependency_overrides[get_digest_cache] = lambda: mock_cache
    try:
        response = await client.get("/api/admin/digest")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert response.status_code == 200
    data = response.json()
    assert data["served_stale"] is True
    assert data["completed_count"] == 3


@pytest.mark.asyncio
async def test_digest_fresh_compute_annotates_served_stale_false(client: AsyncClient):
    """GET /api/admin/digest L3 path annotates served_stale=False on a fresh compute."""
    from unittest.mock import MagicMock
    from app.digest_cache import DigestCache, get_digest_cache

    fresh_payload = {
        "window_hours": 24,
        "completed_count": 7,
        "failed_count": 1,
        "pending_approval_count": 0,
        "active_count": 0,
        "running_count": 0,
        "queued_count": 0,
        "research_cycles": 0,
        "prs_opened": 0,
        "generated_at": "2026-06-12T00:00:00",
    }

    mock_cache = MagicMock(spec=DigestCache)
    mock_cache.get_or_compute_meta = AsyncMock(return_value=(fresh_payload, False))

    app.dependency_overrides[get_digest_cache] = lambda: mock_cache
    try:
        response = await client.get("/api/admin/digest")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert response.status_code == 200
    data = response.json()
    assert data["served_stale"] is False
    assert data["completed_count"] == 7
