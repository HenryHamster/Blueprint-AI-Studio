"""Tests for admin_analytics endpoints."""

import uuid
from collections import deque
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app import shared_state
from app.models import (
    Artifact,
    ArtifactType,
    ImprovementLane,
    ImprovementLaneStatus,
    PoolVm,
    PoolVmState,
)
from app.routers import admin_analytics
from app.time_utils import _utcnow
from app.models import TaskStatus
from testing.factories import make_alert, make_membership, make_project, make_task


@pytest.fixture
def _reset_task_results():
    prev_results = shared_state.deploy_monitoring["task_results"]
    prev_adj = dict(shared_state.anomaly_adjustments)
    shared_state.deploy_monitoring["task_results"] = deque(maxlen=100)
    yield
    shared_state.deploy_monitoring["task_results"] = prev_results
    shared_state.anomaly_adjustments.clear()
    shared_state.anomaly_adjustments.update(prev_adj)


class TestFailingTaskTypes:
    @pytest.mark.asyncio
    async def test_returns_gated_type_with_rate_and_samples(
        self, client: AsyncClient, _reset_task_results
    ):
        """Type with >=70% failure rate and >=3 samples appears in gated_types."""
        now = _utcnow()
        for i in range(5):
            shared_state.deploy_monitoring["task_results"].append(
                (
                    (now - timedelta(minutes=i)).isoformat(),
                    "failure",
                    "CODE_ERROR",
                    "create",
                )
            )
        shared_state.deploy_monitoring["task_results"].append(
            ((now - timedelta(minutes=6)).isoformat(), "success", None, "create")
        )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()

        gated = body["gated_types"]
        assert len(gated) == 1
        entry = gated[0]
        assert entry["task_type"] == "create"
        assert entry["failure_rate"] >= 0.7
        assert entry["sample_size"] == 6
        assert entry["threshold"] == 0.7

    @pytest.mark.asyncio
    async def test_no_gated_types_returns_empty_list(
        self, client: AsyncClient, _reset_task_results
    ):
        """All-success recent results produce an empty gated_types list."""
        now = _utcnow()
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "success", None, "create")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["gated_types"] == []

    @pytest.mark.asyncio
    async def test_insufficient_samples_not_gated(
        self, client: AsyncClient, _reset_task_results
    ):
        """A type with only 2 samples (below n=3 floor) is not reported as gated."""
        now = _utcnow()
        for i in range(2):
            shared_state.deploy_monitoring["task_results"].append(
                (
                    (now - timedelta(minutes=i)).isoformat(),
                    "failure",
                    "CODE_ERROR",
                    "fix",
                )
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        assert resp.json()["gated_types"] == []

    @pytest.mark.asyncio
    async def test_reports_anomaly_tightened_threshold(
        self, client: AsyncClient, _reset_task_results
    ):
        """When anomaly_adjustments.active is True the response reflects the tightened threshold."""
        shared_state.anomaly_adjustments["active"] = True
        shared_state.anomaly_adjustments["failure_gate_threshold"] = 0.6

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["anomaly_tightened"] is True
        assert body["global_failure_threshold"] == 0.6

    @pytest.mark.asyncio
    async def test_healthy_type_excluded_from_gated(
        self, client: AsyncClient, _reset_task_results
    ):
        """When both a failing and a healthy type exist, only the failing one is gated."""
        now = _utcnow()
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                (
                    (now - timedelta(minutes=i)).isoformat(),
                    "failure",
                    "CODE_ERROR",
                    "create",
                )
            )
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "success", None, "review")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        types_gated = [e["task_type"] for e in resp.json()["gated_types"]]
        assert "create" in types_gated
        assert "review" not in types_gated


class TestTaskDurationStats:
    @pytest.mark.asyncio
    async def test_scope_duration_stats_computes_p50_p90(
        self, client: AsyncClient, db_session
    ):
        """Completed tasks grouped by context scope get p50, p90, and sample_size."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        started_at = now - timedelta(hours=2)
        # 3 small tasks at 10/20/30s, 3 large tasks at 100/200/300s.
        # 3+ samples per scope clears the min-floor; separate types ensure only scope path is tested.
        durations_by_scope = {"small": [10, 20, 30], "large": [100, 200, 300]}
        for scope, durs in durations_by_scope.items():
            for secs in durs:
                completed_at = started_at + timedelta(seconds=secs)
                db_session.add(
                    make_task(
                        project_id=project.id,
                        status=TaskStatus.COMPLETED,
                        payloads=[{"type": "create", "prompt": "x"}],
                        context={"scope": scope},
                        started_at=started_at,
                        status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                        created_at=started_at,
                    )
                )
        await db_session.commit()

        resp = await client.get("/api/admin/task-duration-stats")
        assert resp.status_code == 200
        scope_stats = resp.json()["scope_duration_stats"]
        assert set(scope_stats.keys()) == {"small", "large"}
        # p90 with 3 samples: Postgres (production) uses percentile_cont linear
        # interpolation → 28 / 280; SQLite fallback uses nearest-rank → 30 / 300.
        # Accept either so the test runs on both CI (Postgres) and local (SQLite).
        assert scope_stats["small"]["p50_seconds"] == 20.0
        assert scope_stats["small"]["sample_size"] == 3
        assert scope_stats["small"]["p90_seconds"] in (28.0, 30.0)
        assert scope_stats["large"]["p50_seconds"] == 200.0
        assert scope_stats["large"]["sample_size"] == 3
        assert scope_stats["large"]["p90_seconds"] in (280.0, 300.0)

    @pytest.mark.asyncio
    async def test_scope_duration_stats_skips_insufficient_samples(
        self, client: AsyncClient, db_session
    ):
        """Scopes with fewer than 3 samples are omitted from scope_duration_stats."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        started_at = now - timedelta(hours=2)
        for secs in (10, 20):
            completed_at = started_at + timedelta(seconds=secs)
            db_session.add(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "create", "prompt": "x"}],
                    context={"scope": "medium"},
                    started_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    created_at=started_at,
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/task-duration-stats")
        assert resp.status_code == 200
        assert resp.json()["scope_duration_stats"] == {}

    @pytest.mark.asyncio
    async def test_phase_duration_stats_p50(self, client: AsyncClient, db_session):
        """Tasks with vm_provisioned_at/vm_started_at/vm_stopped_at get provisioning+execution p50."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        started_at = now - timedelta(hours=2)
        # provisioning durations 10/20/30s, execution durations 100/200/300s.
        for prov_s, exec_s in [(10, 100), (20, 200), (30, 300)]:
            vm_prov = started_at
            vm_start = vm_prov + timedelta(seconds=prov_s)
            vm_stop = vm_start + timedelta(seconds=exec_s)
            db_session.add(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "create", "prompt": "x"}],
                    started_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": vm_stop.isoformat()}],
                    created_at=started_at,
                    vm_provisioned_at=vm_prov,
                    vm_started_at=vm_start,
                    vm_stopped_at=vm_stop,
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/task-duration-stats")
        assert resp.status_code == 200
        phase = resp.json()["phase_duration_stats"]
        assert phase["provisioning"] == {"p50_seconds": 20.0, "sample_size": 3}
        assert phase["execution"] == {"p50_seconds": 200.0, "sample_size": 3}

    @pytest.mark.asyncio
    async def test_task_duration_stats_windows_by_terminal_time_not_created_at(
        self, client: AsyncClient, db_session
    ):
        """Tasks created before the 7-day cutoff but completed inside it must be included.

        This is the primary regression: the old code filtered by created_at >= cutoff,
        which excluded long-running tasks created >7 days ago but completed recently.
        The correct window is terminal time (completed_at / status_log fallback).
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        # Task created 8 days ago (outside 7-day window by created_at)
        # but completed 1 hour ago (inside the 7-day window by terminal time).
        old_created = now - timedelta(days=8)
        started_at = now - timedelta(hours=3)

        # Add 3 such tasks so the minimum sample floor (3) is cleared.
        for dur_s in (100, 200, 300):
            t_completed = started_at + timedelta(seconds=dur_s)
            db_session.add(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "create", "prompt": "x"}],
                    context={"scope": "medium"},
                    started_at=started_at,
                    completed_at=t_completed,
                    status_log=[{"s": "COMPLETED", "t": t_completed.isoformat()}],
                    created_at=old_created,
                )
            )
        await db_session.commit()

        resp = await client.get("/api/admin/task-duration-stats")
        assert resp.status_code == 200
        data = resp.json()
        # The 3 tasks must appear in scope_duration_stats under "medium".
        # If windowing were still by created_at, all 3 would be excluded and the
        # dict would be empty.
        scope_stats = data["scope_duration_stats"]
        assert "medium" in scope_stats, (
            "Tasks completed inside the 7-day window should be included even if "
            "created_at is older than 7 days"
        )
        assert scope_stats["medium"]["sample_size"] == 3
        assert scope_stats["medium"]["p50_seconds"] == 200.0


class TestLaneDiag:
    @staticmethod
    def _add_review_loop_task(
        db_session,
        project_id,
        pr_number,
        task_type,
        created_at,
        active_seconds,
        fix_reason=None,
    ) -> None:
        context: dict = {}
        if pr_number is not None:
            review_loop = {"pr_number": pr_number}
            if fix_reason:
                review_loop["fix_reason"] = fix_reason
            context["review_loop"] = review_loop
        completed_at = created_at + timedelta(seconds=active_seconds)
        db_session.add(
            make_task(
                project_id=project_id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": task_type, "prompt": "x"}],
                context=context,
                started_at=created_at,
                created_at=created_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
            )
        )

    @pytest.mark.asyncio
    async def test_aggregates_lane_distribution_phase_breakdown_and_relays(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        other_project = make_project(name="Other")
        db_session.add_all([project, other_project])
        await db_session.commit()

        base = _utcnow() - timedelta(hours=3)
        for pr, typ, mins, secs, reason in (
            (101, "review", 0, 600, None),
            (101, "create", 20, 1800, "code_review"),
            (101, "review", 60, 300, None),
            (101, "fix", 80, 1200, "ci_failure"),
            (202, "review", 10, 120, None),
            (202, "create", 15, 240, "merge_conflict"),
            (303, "review", 130, 50, None),
        ):
            self._add_review_loop_task(
                db_session,
                project.id,
                pr,
                typ,
                base + timedelta(minutes=mins),
                secs,
                reason,
            )
        self._add_review_loop_task(
            db_session, project.id, None, "fix", base, 60, "merge_conflict"
        )
        self._add_review_loop_task(
            db_session, other_project.id, 404, "fix", base, 60, "merge_conflict"
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&pr_breakdown_limit=2"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == str(project.id)
        assert body["task_count"] == 7
        assert body["pr_count"] == 3
        assert body["pr_breakdown_truncated"] is True
        assert body["relay_counts_by_lane"] == {
            "conflict": 1,
            "ci": 1,
            "arbitration": 0,
            "review_convergence": 1,
        }

        lanes = body["lane_iteration_distribution"]
        assert lanes["review_convergence"] == {
            "p50_iterations": 1.0,
            "p90_iterations": 3.0,
            "max_iterations": 3,
            "task_count": 5,
            "pr_count": 3,
        }
        assert lanes["conflict"]["task_count"] == 1
        assert lanes["ci"]["task_count"] == 1
        assert lanes["arbitration"]["task_count"] == 0

        by_pr = {row["pr_number"]: row for row in body["per_pr_phase_breakdown"]}
        assert list(by_pr.keys()) == ["101", "202"]
        assert by_pr["101"]["review_seconds"] == 900.0
        assert by_pr["101"]["fix_seconds"] == 3000.0
        assert by_pr["101"]["gap_seconds"] == 2100.0
        assert by_pr["101"]["total_seconds"] == 6000.0
        assert by_pr["101"]["queue_seconds"] == 0.0
        assert by_pr["101"]["provision_seconds"] == 0.0
        assert by_pr["101"]["cold_start_seconds"] == 0.0
        assert by_pr["101"]["execution_seconds"] == 0.0
        assert by_pr["202"]["review_seconds"] == 120.0
        assert by_pr["202"]["fix_seconds"] == 240.0
        assert by_pr["202"]["gap_seconds"] == 180.0
        assert by_pr["202"]["total_seconds"] == 540.0
        assert by_pr["202"]["queue_seconds"] == 0.0
        assert by_pr["202"]["provision_seconds"] == 0.0
        assert by_pr["202"]["cold_start_seconds"] == 0.0
        assert by_pr["202"]["execution_seconds"] == 0.0

    @pytest.mark.asyncio
    async def test_vm_phase_timing_breakdown_per_pr(
        self, client: AsyncClient, db_session
    ):
        """Tasks with VM timestamps contribute queue/provision/cold_start/execution seconds."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = _utcnow() - timedelta(hours=2)

        # PR 101: two tasks, both with full VM timestamps
        # Task 1: queue=30s, provision=60s, cold_start=10s, execution=300s
        t1_created = base
        t1_dispatched = t1_created + timedelta(seconds=30)
        t1_provisioned = t1_dispatched + timedelta(seconds=60)
        t1_started = t1_provisioned + timedelta(seconds=10)
        t1_stopped = t1_started + timedelta(seconds=300)
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "review", "prompt": "x"}],
                context={"review_loop": {"pr_number": 101}},
                created_at=t1_created,
                started_at=t1_started,
                status_log=[{"s": "COMPLETED", "t": t1_stopped.isoformat()}],
                vm_dispatched_at=t1_dispatched,
                vm_provisioned_at=t1_provisioned,
                vm_started_at=t1_started,
                vm_stopped_at=t1_stopped,
            )
        )

        # Task 2: queue=20s, provision=40s, cold_start=5s, execution=600s
        t2_created = base + timedelta(minutes=10)
        t2_dispatched = t2_created + timedelta(seconds=20)
        t2_provisioned = t2_dispatched + timedelta(seconds=40)
        t2_started = t2_provisioned + timedelta(seconds=5)
        t2_stopped = t2_started + timedelta(seconds=600)
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "fix", "prompt": "x"}],
                context={"review_loop": {"pr_number": 101, "fix_reason": "ci_failure"}},
                created_at=t2_created,
                started_at=t2_started,
                status_log=[{"s": "COMPLETED", "t": t2_stopped.isoformat()}],
                vm_dispatched_at=t2_dispatched,
                vm_provisioned_at=t2_provisioned,
                vm_started_at=t2_started,
                vm_stopped_at=t2_stopped,
            )
        )

        # PR 202: one task with no VM timestamps — should contribute 0 for all VM phases
        t3_created = base + timedelta(minutes=5)
        t3_stopped = t3_created + timedelta(seconds=120)
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "review", "prompt": "x"}],
                context={"review_loop": {"pr_number": 202}},
                created_at=t3_created,
                started_at=t3_created,
                status_log=[{"s": "COMPLETED", "t": t3_stopped.isoformat()}],
            )
        )

        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()

        by_pr = {row["pr_number"]: row for row in body["per_pr_phase_breakdown"]}

        # PR 101: sums of both tasks
        assert by_pr["101"]["queue_seconds"] == 50.0  # 30 + 20
        assert by_pr["101"]["provision_seconds"] == 100.0  # 60 + 40
        assert by_pr["101"]["cold_start_seconds"] == 15.0  # 10 + 5
        assert by_pr["101"]["execution_seconds"] == 900.0  # 300 + 600

        # PR 202: no VM timestamps → all zero
        assert by_pr["202"]["queue_seconds"] == 0.0
        assert by_pr["202"]["provision_seconds"] == 0.0
        assert by_pr["202"]["cold_start_seconds"] == 0.0
        assert by_pr["202"]["execution_seconds"] == 0.0

    @pytest.mark.asyncio
    async def test_version_number_scopes_to_version_window(
        self, client: AsyncClient, db_session
    ):
        """Tasks outside the version window are excluded; version metadata is returned."""
        from app.models import ReleaseVersion

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        v9_opened = _utcnow() - timedelta(days=10)
        v9_promoted = _utcnow() - timedelta(days=3)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            phase="build",
            status="promoted",
            opened_at=v9_opened,
            promoted_at=v9_promoted,
        )
        db_session.add(version)
        await db_session.commit()

        # In-window task (created during v9)
        self._add_review_loop_task(
            db_session,
            project.id,
            901,
            "review",
            v9_opened + timedelta(days=1),
            600,
        )
        self._add_review_loop_task(
            db_session,
            project.id,
            901,
            "fix",
            v9_opened + timedelta(days=2),
            1200,
            "ci_failure",
        )
        # Out-of-window task (created before v9 opened)
        self._add_review_loop_task(
            db_session,
            project.id,
            800,
            "review",
            v9_opened - timedelta(days=2),
            300,
        )
        # Out-of-window task (created after v9 promoted)
        self._add_review_loop_task(
            db_session,
            project.id,
            950,
            "review",
            v9_promoted + timedelta(days=1),
            300,
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&version_number=9"
        )
        assert resp.status_code == 200
        body = resp.json()

        # Only in-window tasks should be counted
        assert body["pr_count"] == 1
        assert body["task_count"] == 2

        # Version metadata must be present
        v = body["version"]
        assert v["version_number"] == 9
        assert v["phase"] == "build"
        assert v["status"] == "promoted"
        assert v["opened_at"] is not None
        assert v["promoted_at"] is not None

        # Standard metrics are present
        assert "lane_iteration_distribution" in body
        assert "relay_counts_by_lane" in body
        assert "lane_waste" in body

    @pytest.mark.asyncio
    async def test_version_number_not_found_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Requesting a non-existent version_number returns 404."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&version_number=99"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_version_param_returns_flat_response_unchanged(
        self, client: AsyncClient, db_session
    ):
        """Without version_number, the flat rolling-window response is unchanged."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()

        # All legacy keys still present
        assert "lane_iteration_distribution" in body
        assert "per_pr_phase_breakdown" in body
        assert "relay_counts_by_lane" in body
        assert "pr_count" in body
        assert "task_count" in body
        assert "window_days" in body
        assert "window_start" in body
        assert "lane_waste" in body
        # No version key injected
        assert "version" not in body

    @pytest.mark.asyncio
    async def test_version_scoped_window_survives_newer_task_displacement(
        self, client: AsyncClient, db_session
    ):
        """Closed-version aggregates are correct even when newer tasks exceed max_tasks.

        When more out-of-window tasks exist than max_tasks, a lower-bound-only SQL
        query would fill the cap with out-of-window rows and return empty aggregates
        for the closed version. The upper-bound SQL fix must prevent this.
        """
        from app.models import ReleaseVersion

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        v9_opened = _utcnow() - timedelta(days=10)
        v9_promoted = _utcnow() - timedelta(days=3)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            phase="build",
            status="promoted",
            opened_at=v9_opened,
            promoted_at=v9_promoted,
        )
        db_session.add(version)
        await db_session.commit()

        # One in-window task for the closed version
        self._add_review_loop_task(
            db_session,
            project.id,
            901,
            "fix",
            v9_opened + timedelta(days=1),
            600,
            "ci_failure",
        )

        # Newer out-of-window tasks that would fill a small max_tasks cap
        for i in range(5):
            self._add_review_loop_task(
                db_session,
                project.id,
                999 + i,
                "fix",
                v9_promoted + timedelta(days=i + 1),
                300,
                "ci_failure",
            )
        await db_session.commit()

        # Use max_tasks=3 so out-of-window tasks would displace the in-window one
        # if the upper bound were not pushed into SQL.
        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&version_number=9&max_tasks=3"
        )
        assert resp.status_code == 200
        body = resp.json()

        # The in-window task must still be counted despite the smaller cap
        assert body["pr_count"] == 1
        assert body["task_count"] == 1

    @pytest.mark.asyncio
    async def test_abandoned_version_window_ends_at_abandoned_at(
        self, client: AsyncClient, db_session
    ):
        """Abandoned versions scope the window to abandoned_at, not now.

        Without the fix, window_end falls back to now and the response includes
        tasks created after the version was abandoned (the successor version's
        work). The fix recovers abandoned_at from last_qualification provenance
        and uses it as the window upper bound.
        """
        from app.models import ReleaseVersion

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        v10_opened = _utcnow() - timedelta(days=5)
        v10_abandoned = _utcnow() - timedelta(days=2)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=10,
            phase="build",
            status="abandoned",
            opened_at=v10_opened,
            promoted_at=None,
            last_qualification={
                "status": "abandoned",
                "reason": "operator_override",
                "abandoned_at": v10_abandoned.isoformat(),
            },
        )
        db_session.add(version)
        await db_session.commit()

        # In-window task (created during v10, before abandonment)
        self._add_review_loop_task(
            db_session,
            project.id,
            1001,
            "review",
            v10_opened + timedelta(days=1),
            600,
        )
        # Out-of-window task (created after v10 was abandoned — successor work)
        self._add_review_loop_task(
            db_session,
            project.id,
            1002,
            "fix",
            v10_abandoned + timedelta(hours=1),
            300,
            "ci_failure",
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&version_number=10"
        )
        assert resp.status_code == 200
        body = resp.json()

        # Only the in-window task should be counted
        assert body["pr_count"] == 1
        assert body["task_count"] == 1

        # Version metadata must include abandoned_at and have promoted_at=None
        v = body["version"]
        assert v["version_number"] == 10
        assert v["status"] == "abandoned"
        assert v["promoted_at"] is None
        assert v["abandoned_at"] is not None
        assert v["abandoned_at"].startswith(v10_abandoned.isoformat()[:19])

    @pytest.mark.asyncio
    async def test_abandoned_version_displacement_prevented(
        self, client: AsyncClient, db_session
    ):
        """Abandoned-version in-window tasks survive successor task displacement.

        With max_tasks smaller than the number of out-of-window (successor)
        tasks, a lower-bound-only SQL query fills the cap with successor rows
        and returns empty aggregates. The upper-bound SQL fix must push
        created_at < abandoned_at into SQL for all closed versions, including
        abandoned ones.
        """
        from app.models import ReleaseVersion

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        v10_opened = _utcnow() - timedelta(days=5)
        v10_abandoned = _utcnow() - timedelta(days=2)
        version = ReleaseVersion(
            project_id=project.id,
            version_number=10,
            phase="build",
            status="abandoned",
            opened_at=v10_opened,
            promoted_at=None,
            last_qualification={
                "status": "abandoned",
                "abandoned_at": v10_abandoned.isoformat(),
            },
        )
        db_session.add(version)
        await db_session.commit()

        # One in-window task for the abandoned version
        self._add_review_loop_task(
            db_session,
            project.id,
            2001,
            "fix",
            v10_opened + timedelta(days=1),
            600,
            "ci_failure",
        )

        # Successor tasks created after abandonment that would fill a small cap
        for i in range(5):
            self._add_review_loop_task(
                db_session,
                project.id,
                3000 + i,
                "fix",
                v10_abandoned + timedelta(hours=i + 1),
                300,
                "ci_failure",
            )
        await db_session.commit()

        # max_tasks=3: without the SQL upper bound, out-of-window rows displace
        # the in-window one and the response returns empty aggregates.
        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&version_number=10&max_tasks=3"
        )
        assert resp.status_code == 200
        body = resp.json()

        # The in-window task must still be counted despite the smaller cap
        assert body["pr_count"] == 1
        assert body["task_count"] == 1


class TestLaneWasteDiag:
    """Chain-complete review/fix waste diagnostics (the additive ``lane_waste``)."""

    @staticmethod
    def _add_chain_task(
        db_session,
        project_id,
        *,
        pr_number,
        original_task_id,
        iteration,
        task_type,
        created_at,
        status=TaskStatus.COMPLETED,
        fix_reason=None,
        failure_category=None,
        failure_detail=None,
        drift=None,
        fix_loop_history=None,
        stale_head=False,
        active_seconds=60,
    ) -> uuid.UUID:
        review_loop = {"pr_number": pr_number, "iteration": iteration}
        if original_task_id is not None:
            review_loop["original_task_id"] = str(original_task_id)
        if fix_reason:
            review_loop["fix_reason"] = fix_reason
        if drift:
            review_loop["drift_signal"] = drift
        context: dict = {"review_loop": review_loop}
        if failure_category:
            context["failure_category"] = failure_category
        if failure_detail:
            context["failure_detail"] = failure_detail
        if stale_head:
            context["_review_stale_head"] = True
        completed_at = created_at + timedelta(seconds=active_seconds)
        task = make_task(
            project_id=project_id,
            status=status,
            created_by="ORCHESTRATOR",
            payloads=[{"type": task_type, "prompt": "x"}],
            context=context,
            fix_loop_history=fix_loop_history or {},
            started_at=created_at,
            created_at=created_at,
            status_log=[{"s": status.value, "t": completed_at.isoformat()}],
        )
        db_session.add(task)
        return task.id

    @staticmethod
    def _add_original(
        db_session,
        project_id,
        original_task_id,
        created_at,
        *,
        merged=False,
        failure_detail=None,
    ):
        context: dict = {}
        if merged:
            context["pr_merged"] = True
        if failure_detail:
            context["failure_detail"] = failure_detail
        db_session.add(
            make_task(
                id=original_task_id,
                project_id=project_id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                context=context,
                started_at=created_at,
                created_at=created_at,
                status_log=[{"s": "COMPLETED", "t": created_at.isoformat()}],
            )
        )

    @pytest.mark.asyncio
    async def test_reports_chain_complete_waste_taxonomy_and_drilldowns(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = _utcnow() - timedelta(hours=2)
        oid_a = uuid.UUID("11111111-1111-1111-1111-111111111111")
        oid_b = uuid.UUID("22222222-2222-2222-2222-222222222222")

        # Chain A (PR 501) — cap-exhausted, mixed waste, infra + stale-head excluded.
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=501,
            original_task_id=oid_a,
            iteration=1,
            task_type="review",
            created_at=base,
        )  # review_ambiguity (LANE_REVIEW_CONVERGENCE, iter≤2), cap-counted
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=501,
            original_task_id=oid_a,
            iteration=2,
            task_type="fix",
            fix_reason="ci_failure",
            created_at=base + timedelta(minutes=10),
        )  # ci_churn, cap-counted
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=501,
            original_task_id=oid_a,
            iteration=3,
            task_type="fix",
            status=TaskStatus.FAILED,
            failure_category="AGENT_TIMEOUT",
            created_at=base + timedelta(minutes=20),
        )  # infra, NOT cap-counted, terminal FAILED included
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=501,
            original_task_id=oid_a,
            iteration=4,
            task_type="fix",
            fix_reason="merge_conflict",
            failure_detail="review_cap_exhausted",
            created_at=base + timedelta(minutes=30),
        )  # merge_conflict, cap-counted, marks the chain cap-exhausted
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=501,
            original_task_id=oid_a,
            iteration=5,
            task_type="review",
            stale_head=True,
            created_at=base + timedelta(minutes=40),
        )  # lost_reasoning (LANE_REVIEW_CONVERGENCE, iter≥3), NOT cap-counted (stale-head discard)
        self._add_original(db_session, project.id, oid_a, base - timedelta(minutes=5))

        # Chain B (PR 502) — merged via original hydration; lost-state lanes.
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=502,
            original_task_id=oid_b,
            iteration=1,
            task_type="review",
            drift={"drift_in": ["a.py"]},
            created_at=base + timedelta(minutes=5),
        )  # lost_repo_state
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=502,
            original_task_id=oid_b,
            iteration=2,
            task_type="fix",
            fix_loop_history={
                "test": [
                    {
                        "loop": "test",
                        "iteration": 1,
                        "exit_reason": "divergence_oscillation",
                        "ended_at": "2026-06-01T00:00:00",
                    }
                ]
            },
            created_at=base + timedelta(minutes=15),
        )  # lost_reasoning
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=502,
            original_task_id=oid_b,
            iteration=3,
            task_type="review",
            created_at=base + timedelta(minutes=25),
        )  # lost_reasoning (LANE_REVIEW_CONVERGENCE, iter>2)
        self._add_original(
            db_session, project.id, oid_b, base - timedelta(minutes=5), merged=True
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
        assert resp.status_code == 200
        waste = resp.json()["lane_waste"]

        # Aggregate waste taxonomy spans every pilot category.
        assert waste["waste_taxonomy"] == {
            "infra": 1,
            "merge_conflict": 1,
            "ci_churn": 1,
            "review_ambiguity": 1,
            "lost_reasoning": 3,
            "lost_repo_state": 1,
            "unknown": 0,
        }
        assert waste["waste_task_count"] == 8
        # Cap parity: infra FAILED + stale-head discard excluded from cap totals.
        assert waste["cap_counted_task_count"] == 6
        assert waste["cap_excluded_task_count"] == 2
        assert waste["chain_count"] == 2
        assert waste["cap_exhausted_chain_count"] == 1

        chains = {c["pr_number"]: c for c in waste["chains"]}
        assert set(chains) == {501, 502}

        chain_a = chains[501]
        assert chain_a["original_task_id"] == str(oid_a)
        assert chain_a["project_id"] == str(project.id)
        assert chain_a["terminal_outcome"] == "cap_exhausted"
        assert chain_a["cap_exhausted"] is True
        assert chain_a["task_count"] == 5
        assert chain_a["cap_counted_iterations"] == 3
        assert chain_a["cap_excluded_iterations"] == 2
        assert chain_a["waste_totals"]["infra"] == 1
        assert chain_a["max_iteration"] == 5

        # Drilldown handles carry the per-task terminal/lane/cap markers.
        a_by_iter = {t["iteration"]: t for t in chain_a["tasks"]}
        assert a_by_iter[3]["status"] == "FAILED"
        assert a_by_iter[3]["waste_category"] == "infra"
        assert a_by_iter[3]["failure_category"] == "AGENT_TIMEOUT"
        assert a_by_iter[3]["counts_toward_cap"] is False
        assert a_by_iter[5]["stale_head_discard"] is True
        assert a_by_iter[5]["counts_toward_cap"] is False
        assert a_by_iter[2]["lane"] == "ci"
        assert a_by_iter[2]["counts_toward_cap"] is True
        assert {"task_id", "type", "created_at", "completed_at", "started_at"} <= set(
            a_by_iter[1]
        )

        # Chain B's terminal outcome is hydrated from the (review_loop-less) original.
        chain_b = chains[502]
        assert chain_b["terminal_outcome"] == "merged"
        assert chain_b["cap_exhausted"] is False
        b_cats = {t["iteration"]: t["waste_category"] for t in chain_b["tasks"]}
        assert b_cats == {
            1: "lost_repo_state",
            2: "lost_reasoning",
            3: "lost_reasoning",
        }

    @pytest.mark.asyncio
    async def test_additive_contract_preserves_legacy_keys_and_includes_terminal_tasks(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = _utcnow() - timedelta(hours=1)
        oid = uuid.UUID("33333333-3333-3333-3333-333333333333")
        # Two COMPLETED iterations + one terminal FAILED (non-infra) on the same PR.
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=601,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            created_at=base,
        )
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=601,
            original_task_id=oid,
            iteration=2,
            task_type="fix",
            fix_reason="code_review",
            created_at=base + timedelta(minutes=10),
        )
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=601,
            original_task_id=oid,
            iteration=3,
            task_type="fix",
            status=TaskStatus.FAILED,
            failure_category="CODE_ERROR",
            created_at=base + timedelta(minutes=20),
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()

        # Legacy contract: every original key present and unchanged in meaning —
        # only the two COMPLETED, PR-bearing tasks count toward the legacy surface.
        for key in (
            "lane_iteration_distribution",
            "per_pr_phase_breakdown",
            "relay_counts_by_lane",
            "pr_count",
            "task_count",
            "pr_breakdown_limit",
            "pr_breakdown_truncated",
            "window_days",
            "window_start",
        ):
            assert key in body
        assert body["task_count"] == 2
        assert body["pr_count"] == 1

        # Additive surface: the terminal FAILED task IS included as chain waste.
        waste = body["lane_waste"]
        assert waste["waste_task_count"] == 3
        assert waste["chain_count"] == 1
        [chain] = waste["chains"]
        assert chain["task_count"] == 3
        # CODE_ERROR is NOT infra-class, so the FAILED task still counts toward cap.
        assert chain["cap_counted_iterations"] == 3
        statuses = {t["status"] for t in chain["tasks"]}
        assert statuses == {"COMPLETED", "FAILED"}

    @pytest.mark.asyncio
    async def test_legacy_surface_not_eroded_by_failed_tasks_under_truncation(
        self, client: AsyncClient, db_session
    ):
        """FAILED/CANCELLED tasks must not displace COMPLETED rows from the legacy surface.

        With max_tasks=2 and 2 recent FAILED + 2 older COMPLETED tasks, the
        legacy aggregates (task_count, pr_count) must still count both COMPLETED
        tasks — the separate completed-only query ensures FAILED rows never evict
        them from the top-N pool.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = _utcnow() - timedelta(hours=3)
        oid = uuid.UUID("44444444-4444-4444-4444-444444444444")

        # Two COMPLETED tasks (older) — must always appear in legacy surface.
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=701,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            created_at=base,
        )
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=702,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            created_at=base + timedelta(minutes=5),
        )
        # Two FAILED tasks (more recent) — with a shared max_tasks=2 query these
        # would evict the COMPLETED tasks, eroding the legacy surface.
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=703,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            status=TaskStatus.FAILED,
            failure_category="CODE_ERROR",
            created_at=base + timedelta(minutes=20),
        )
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=704,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            status=TaskStatus.FAILED,
            failure_category="CODE_ERROR",
            created_at=base + timedelta(minutes=25),
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&max_tasks=2"
        )
        assert resp.status_code == 200
        body = resp.json()

        # Legacy surface counts both COMPLETED tasks regardless of the 2 FAILED
        # tasks that are more recent and would have displaced them in a shared query.
        assert body["task_count"] == 2
        assert body["pr_count"] == 2

        # Waste surface uses its own (broadened) query and captures the 2 FAILED
        # tasks (the 2 most-recent within max_tasks=2).
        waste = body["lane_waste"]
        assert waste["waste_task_count"] == 2
        statuses = {t["status"] for c in waste["chains"] for t in c["tasks"]}
        assert statuses == {"FAILED"}

    @pytest.mark.asyncio
    async def test_hydrates_original_tasks_outside_window_for_canonical_outcomes(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        merged_oid = uuid.UUID("55555555-5555-5555-5555-555555555555")
        cap_oid = uuid.UUID("66666666-6666-6666-6666-666666666666")

        self._add_chain_task(
            db_session,
            project.id,
            pr_number=801,
            original_task_id=merged_oid,
            iteration=1,
            task_type="review",
            created_at=now - timedelta(minutes=20),
        )
        self._add_chain_task(
            db_session,
            project.id,
            pr_number=802,
            original_task_id=cap_oid,
            iteration=1,
            task_type="fix",
            created_at=now - timedelta(minutes=10),
        )
        self._add_original(
            db_session,
            project.id,
            merged_oid,
            now - timedelta(days=8),
            merged=True,
        )
        self._add_original(
            db_session,
            project.id,
            cap_oid,
            now - timedelta(days=9),
            failure_detail="review_cap_exhausted",
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/lane-diag?project_id={project.id}&max_tasks=2"
        )
        assert resp.status_code == 200
        waste = resp.json()["lane_waste"]
        chains = {c["pr_number"]: c for c in waste["chains"]}
        assert chains[801]["terminal_outcome"] == "merged"
        assert chains[801]["cap_exhausted"] is False
        assert chains[802]["terminal_outcome"] == "cap_exhausted"
        assert chains[802]["cap_exhausted"] is True
        assert waste["cap_exhausted_chain_count"] == 1

    @pytest.mark.asyncio
    async def test_uses_escalation_alerts_for_canonical_chain_outcome(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        oid = uuid.UUID("77777777-7777-7777-7777-777777777777")
        task_id = self._add_chain_task(
            db_session,
            project.id,
            pr_number=803,
            original_task_id=oid,
            iteration=1,
            task_type="review",
            created_at=now - timedelta(minutes=5),
        )
        self._add_original(
            db_session,
            project.id,
            oid,
            now - timedelta(days=8),
        )
        await db_session.commit()

        db_session.add(
            make_alert(
                project.id,
                task_id=task_id,
                title="Review-fix escalation: stuck cycle on PR #803",
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
        assert resp.status_code == 200
        waste = resp.json()["lane_waste"]
        [chain] = waste["chains"]
        assert chain["pr_number"] == 803
        assert chain["terminal_outcome"] == "stuck"
        assert chain["cap_exhausted"] is False
        assert waste["cap_exhausted_chain_count"] == 0


class TestTaskCostBreakdown:
    @pytest.mark.asyncio
    async def test_breakdown_filters_by_project_window_and_groups_costs(
        self, client: AsyncClient, db_session
    ):
        """Only terminal tasks in the requested project/window contribute to cost buckets."""
        project_a = make_project()
        project_b = make_project(name="Other Project")
        db_session.add_all([project_a, project_b])
        await db_session.commit()

        now = _utcnow()

        def add_cost_task(
            project_id: uuid.UUID,
            *,
            title: str,
            task_type: str,
            direction_id: str | None,
            completed_minutes_ago: int,
            vm_seconds: int,
        ) -> None:
            completed_at = now - timedelta(minutes=completed_minutes_ago)
            started_at = completed_at - timedelta(seconds=vm_seconds)
            db_session.add(
                make_task(
                    project_id=project_id,
                    title=title,
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": task_type, "prompt": "x"}],
                    context={"direction_id": direction_id} if direction_id else {},
                    started_at=started_at,
                    created_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    vm_provisioned_at=started_at,
                    vm_stopped_at=completed_at,
                    vm_spot=False,
                )
            )

        add_cost_task(
            project_a.id,
            title="Research expensive",
            task_type="research",
            direction_id="dir-a",
            completed_minutes_ago=10,
            vm_seconds=3600,
        )
        add_cost_task(
            project_a.id,
            title="Create supporting task",
            task_type="create",
            direction_id="dir-a",
            completed_minutes_ago=20,
            vm_seconds=1800,
        )
        add_cost_task(
            project_a.id,
            title="Unassigned task",
            task_type="create",
            direction_id=None,
            completed_minutes_ago=30,
            vm_seconds=600,
        )
        add_cost_task(
            project_a.id,
            title="Too old to count",
            task_type="research",
            direction_id="dir-old",
            completed_minutes_ago=180,
            vm_seconds=7200,
        )
        add_cost_task(
            project_b.id,
            title="Other project task",
            task_type="research",
            direction_id="dir-b",
            completed_minutes_ago=15,
            vm_seconds=3600,
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project_a.id}&window=1h&top_n=2"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["project_id"] == str(project_a.id)
        assert body["window"] == "1h"
        assert body["task_count"] == 3
        assert body["total_estimated_cost_usd"] == pytest.approx(0.11)

        assert body["task_type_breakdown"] == [
            {
                "task_type": "research",
                "task_count": 1,
                "estimated_cost_usd": pytest.approx(
                    round(3600 * admin_analytics.ON_DEMAND_RATE, 2)
                ),
            },
            {
                "task_type": "create",
                "task_count": 2,
                "estimated_cost_usd": pytest.approx(
                    round(2400 * admin_analytics.ON_DEMAND_RATE, 2)
                ),
            },
        ]
        assert body["direction_breakdown"] == [
            {
                "direction_id": "dir-a",
                "direction_text": None,
                "task_count": 2,
                "estimated_cost_usd": pytest.approx(
                    round(5400 * admin_analytics.ON_DEMAND_RATE, 2)
                ),
            },
            {
                "direction_id": None,
                "direction_text": None,
                "task_count": 1,
                "estimated_cost_usd": pytest.approx(
                    round(600 * admin_analytics.ON_DEMAND_RATE, 2)
                ),
            },
        ]
        assert [task["task_title"] for task in body["top_tasks"]] == [
            "Research expensive",
            "Create supporting task",
        ]
        assert body["top_tasks"][0]["estimated_cost_usd"] == pytest.approx(
            round(3600 * admin_analytics.ON_DEMAND_RATE, 2)
        )
        assert body["top_tasks"][1]["estimated_cost_usd"] == pytest.approx(
            round(1800 * admin_analytics.ON_DEMAND_RATE, 2)
        )

    @pytest.mark.asyncio
    async def test_direction_text_surfaced_in_breakdown(
        self, client: AsyncClient, db_session
    ):
        """direction_text from context is returned in direction_breakdown; first-non-empty
        wins when two tasks share a direction but only one carries direction_text."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        def _task(title: str, direction_id: str, vm_seconds: int, direction_text: str | None = None) -> None:
            completed_at = now - timedelta(minutes=10)
            started_at = completed_at - timedelta(seconds=vm_seconds)
            ctx: dict = {"direction_id": direction_id}
            if direction_text is not None:
                ctx["direction_text"] = direction_text
            db_session.add(
                make_task(
                    project_id=project.id,
                    title=title,
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "create", "prompt": "x"}],
                    context=ctx,
                    started_at=started_at,
                    created_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    vm_provisioned_at=started_at,
                    vm_stopped_at=completed_at,
                    vm_spot=False,
                )
            )

        # Two tasks in the same direction: the task without text has a later created_at
        # (vm_seconds=300 → less time before completed_at → later created_at), so it is
        # returned first in the DESC-ordered query result. The first-non-empty aggregation
        # (admin_analytics.py:583-584) skips it and picks up the text from the second row.
        _task("Task without text", direction_id="dir-x", vm_seconds=300, direction_text=None)
        _task("Task with text", direction_id="dir-x", vm_seconds=600, direction_text="Fix the login flow")
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert len(body["direction_breakdown"]) == 1
        entry = body["direction_breakdown"][0]
        assert entry["direction_id"] == "dir-x"
        assert entry["direction_text"] == "Fix the login flow"
        assert entry["task_count"] == 2

    @pytest.mark.asyncio
    async def test_banked_retry_cost_added_to_current_vm_interval(
        self, client: AsyncClient, db_session
    ):
        """A retried task's banked cost from prior attempts is included in the total."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=10)
        started_at = completed_at - timedelta(seconds=1800)
        banked = 0.02
        current_attempt_cost = round(1800 * admin_analytics.ON_DEMAND_RATE, 4)
        db_session.add(
            make_task(
                project_id=project.id,
                title="Retried task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                created_at=started_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=started_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
                vm_runtime_cost_banked_db=banked,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["total_estimated_cost_usd"] == pytest.approx(
            round(banked + current_attempt_cost, 2)
        )

    @pytest.mark.asyncio
    async def test_cancelled_task_with_banked_cost_included(
        self, client: AsyncClient, db_session
    ):
        """A CANCELLED task that burned VM cost on a prior attempt counts in the breakdown."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=15)
        banked = 0.05
        db_session.add(
            make_task(
                project_id=project.id,
                title="Cancelled with spend",
                status=TaskStatus.CANCELLED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                created_at=completed_at,
                status_log=[{"s": "CANCELLED", "t": completed_at.isoformat()}],
                vm_runtime_cost_banked_db=banked,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["total_estimated_cost_usd"] == pytest.approx(banked)

    @pytest.mark.asyncio
    async def test_old_created_recently_finished_task_included_by_completion_time(
        self, client: AsyncClient, db_session
    ):
        """A task created before the window but completed within it is counted (windowing
        uses completion time, not creation time)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=20)
        created_at = now - timedelta(hours=3)
        started_at = completed_at - timedelta(seconds=3600)
        db_session.add(
            make_task(
                project_id=project.id,
                title="Old created recently finished",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                created_at=created_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=started_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["total_estimated_cost_usd"] == pytest.approx(
            round(3600 * admin_analytics.ON_DEMAND_RATE, 2)
        )

    @pytest.mark.asyncio
    async def test_post_terminal_status_log_write_does_not_move_task_into_window(
        self, client: AsyncClient, db_session
    ):
        """A durable ``completed_at`` OUTSIDE the window keeps the task excluded even
        when a LATER ``status_log`` entry (simulating a post-terminal
        ``latest_status_message`` write) falls INSIDE the window.

        Regression guard for the bug this change fixes: deriving from the
        ``status_log`` tail would wrongly pull the task into the window.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=90)  # OUTSIDE the 1h window
        started_at = completed_at - timedelta(seconds=1800)
        post_terminal_msg_at = now - timedelta(minutes=5)  # INSIDE the window
        db_session.add(
            make_task(
                project_id=project.id,
                title="Completed then messaged",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                created_at=started_at,
                completed_at=completed_at,
                status_log=[
                    {"s": "COMPLETED", "t": completed_at.isoformat()},
                    # Mirrors latest_status_message appending a row post-terminal.
                    {"t": post_terminal_msg_at.isoformat(), "msg": "operator note"},
                ],
                vm_provisioned_at=started_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        # Durable completed_at (90m ago) governs windowing; the later audit row
        # is ignored, so the task stays out of the 1h window.
        assert body["task_count"] == 0

    @pytest.mark.asyncio
    async def test_legacy_null_completed_at_windows_by_status_log_tail(
        self, client: AsyncClient, db_session
    ):
        """Legacy rows whose durable ``completed_at`` was never stamped still derive
        window membership from the ``status_log`` tail (unchanged fallback)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=20)  # INSIDE the 1h window
        started_at = completed_at - timedelta(seconds=1800)
        task = make_task(
            project_id=project.id,
            title="Legacy completed",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "prompt": "x"}],
            started_at=started_at,
            created_at=now - timedelta(hours=3),  # OUTSIDE the window
            status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
            vm_provisioned_at=started_at,
            vm_stopped_at=completed_at,
            vm_spot=False,
        )
        db_session.add(task)
        await db_session.commit()
        # Simulate a legacy row whose durable column was never populated.
        task.completed_at = None
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        # Falls back to the status_log tail (20m ago, in-window) — NOT created_at
        # (3h ago, out-of-window) — so the task is counted.
        assert body["task_count"] == 1

    @pytest.mark.asyncio
    async def test_old_created_task_completed_in_window_is_included(
        self, client: AsyncClient, db_session
    ):
        """A task whose VM stopped before the cutoff but whose completion was logged
        inside the window still contributes to the cost window."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=20)
        vm_stopped_at = now - timedelta(hours=2)
        vm_started_at = vm_stopped_at - timedelta(seconds=1800)
        db_session.add(
            make_task(
                project_id=project.id,
                title="Old created late completed",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=vm_started_at,
                created_at=now - timedelta(days=3),
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=vm_started_at,
                vm_stopped_at=vm_stopped_at,
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["total_estimated_cost_usd"] == pytest.approx(
            round(1800 * admin_analytics.ON_DEMAND_RATE, 2)
        )

    @pytest.mark.asyncio
    async def test_max_tasks_caps_materialized_set(
        self, client: AsyncClient, db_session
    ):
        """The query is bounded by max_tasks so a large backlog can't OOM the worker.

        With max_tasks=2 and three in-window tasks, only the two most recently
        created rows are materialized, and the response flags truncation.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        for minutes_ago in (10, 20, 30):
            completed_at = now - timedelta(minutes=minutes_ago)
            started_at = completed_at - timedelta(seconds=600)
            db_session.add(
                make_task(
                    project_id=project.id,
                    title=f"Task {minutes_ago}",
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "create", "prompt": "x"}],
                    started_at=started_at,
                    created_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    vm_provisioned_at=started_at,
                    vm_stopped_at=completed_at,
                    vm_spot=False,
                )
            )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h&max_tasks=2"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_tasks"] == 2
        assert body["truncated"] is True
        # Only the two most-recently-created tasks survive the LIMIT.
        assert body["task_count"] == 2
        assert {t["task_title"] for t in body["top_tasks"]} == {"Task 10", "Task 20"}

    @pytest.mark.asyncio
    async def test_truncated_false_when_under_cap(
        self, client: AsyncClient, db_session
    ):
        """truncated is False when the result set fits under max_tasks."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        completed_at = now - timedelta(minutes=10)
        started_at = completed_at - timedelta(seconds=600)
        db_session.add(
            make_task(
                project_id=project.id,
                title="Only task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                created_at=started_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=started_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_tasks"] == 1000
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_spot_rate_and_evidenceless_exclusion(
        self, client: AsyncClient, db_session
    ):
        """Server-side cost aggregation prices spot vs on-demand per row and drops
        tasks with no VM cost evidence.

        Exercises the SQL rate CASE (the spot branch specifically) and the
        evidence filter that replaced the per-row Python ``cost is None`` skip
        when the breakdown was moved into SQL.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        completed_at = now - timedelta(minutes=10)
        started_at = completed_at - timedelta(seconds=3600)

        def _add(title: str, task_type: str, *, spot: bool, evidence: bool) -> None:
            kwargs: dict = dict(
                project_id=project.id,
                title=title,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": task_type, "prompt": "x"}],
                started_at=started_at,
                created_at=started_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_spot=spot,
            )
            if evidence:
                # A provisioning window is the cost evidence; without it (and no
                # banked cost) the task must be excluded entirely.
                kwargs["vm_provisioned_at"] = started_at
                kwargs["vm_stopped_at"] = completed_at
            db_session.add(make_task(**kwargs))

        _add("on-demand", "create", spot=False, evidence=True)
        _add("spot", "research", spot=True, evidence=True)
        _add("no-evidence", "create", spot=False, evidence=False)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/task-cost-breakdown?project_id={project.id}&window=1h"
        )
        assert resp.status_code == 200
        body = resp.json()

        on_demand_cost = round(3600 * admin_analytics.ON_DEMAND_RATE, 2)
        spot_cost = round(3600 * admin_analytics.SPOT_RATE, 2)
        # Only the two evidence-bearing tasks are counted.
        assert body["task_count"] == 2
        assert body["total_estimated_cost_usd"] == pytest.approx(
            round(
                3600 * admin_analytics.ON_DEMAND_RATE
                + 3600 * admin_analytics.SPOT_RATE,
                2,
            )
        )
        by_type = {r["task_type"]: r for r in body["task_type_breakdown"]}
        assert by_type["create"]["estimated_cost_usd"] == pytest.approx(on_demand_cost)
        assert by_type["research"]["estimated_cost_usd"] == pytest.approx(spot_cost)
        # Spot is strictly cheaper — proves the spot branch of the rate CASE ran
        # rather than a single flat rate for both rows.
        assert spot_cost < on_demand_cost
        assert "no-evidence" not in {t["task_title"] for t in body["top_tasks"]}


class TestOpenPrs:
    @pytest.fixture(autouse=True)
    def _reset_open_prs_cache(self):
        admin_analytics._open_prs_cache.clear()
        yield
        admin_analytics._open_prs_cache.clear()

    @staticmethod
    def _github_pr(
        number: int, *, title: str = "Open PR", branch: str = "feature/test"
    ) -> dict:
        return {
            "number": number,
            "html_url": f"https://github.com/acme/widgets/pull/{number}",
            "title": title,
            "head": {"ref": branch},
            "created_at": "2026-04-29T12:00:00Z",
        }

    @pytest.mark.asyncio
    async def test_project_without_github_repo_returns_empty_without_external_calls(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(return_value=[self._github_pr(1)])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        assert resp.json() == {
            "open_prs": [],
            "project_integration_branch": "main",
            "project_promotion_target": "main",
            "degraded": False,
            "degraded_reason": None,
        }
        token_mock.assert_not_awaited()
        prs_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_token_lookup_error_returns_empty_and_skips_github(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(side_effect=RuntimeError("boom"))
        prs_mock = AsyncMock(return_value=[self._github_pr(1)])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")
        cached = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        assert resp.json() == {
            "open_prs": [],
            "project_integration_branch": "main",
            "project_promotion_target": "main",
            "degraded": True,
            "degraded_reason": "github_token_error",
        }
        assert cached.status_code == 200
        assert cached.json() == resp.json()
        token_mock.assert_awaited_once()
        prs_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falsy_token_returns_empty(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value=None)
        prs_mock = AsyncMock(return_value=[self._github_pr(1)])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        assert resp.json() == {
            "open_prs": [],
            "project_integration_branch": "main",
            "project_promotion_target": "main",
            "degraded": True,
            "degraded_reason": "no_token",
        }
        token_mock.assert_awaited_once()
        prs_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_healthy_empty_response_is_not_degraded(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        assert resp.json() == {
            "open_prs": [],
            "project_integration_branch": "main",
            "project_promotion_target": "main",
            "degraded": False,
            "degraded_reason": None,
        }
        token_mock.assert_awaited_once()
        prs_mock.assert_awaited_once_with("token", "acme", "widgets")

    @pytest.mark.asyncio
    async def test_unmatched_github_pr_is_orphaned(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(
            return_value=[
                self._github_pr(17, title="Lonely PR", branch="lonely-branch")
            ]
        )
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        assert resp.json() == {
            "open_prs": [
                {
                    "pr_number": 17,
                    "pr_url": "https://github.com/acme/widgets/pull/17",
                    "pr_title": "Lonely PR",
                    "pr_branch": "lonely-branch",
                    "pr_base_branch": "",
                    "pr_created_at": "2026-04-29T12:00:00Z",
                    "tasks": [],
                    "is_orphaned": True,
                }
            ],
            "project_integration_branch": "main",
            "project_promotion_target": "main",
            "degraded": False,
            "degraded_reason": None,
        }
        token_mock.assert_awaited_once()
        prs_mock.assert_awaited_once_with("token", "acme", "widgets")

    @pytest.mark.asyncio
    async def test_artifact_match_links_active_task_and_dedupes_context_match(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            title="Implement monitor",
            payloads=[{"type": "review", "prompt": "x"}],
            context={"review_loop": {"pr_number": 42}},
        )
        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/acme/widgets/pull/42",
        )
        db_session.add(project)
        db_session.add(task)
        db_session.add(artifact)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(
            return_value=[
                self._github_pr(42, title="Monitor PR", branch="feature/monitor")
            ]
        )
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["open_prs"][0]["is_orphaned"] is False
        assert body["open_prs"][0]["tasks"] == [
            {
                "task_id": str(task.id),
                "task_status": TaskStatus.IN_PROGRESS,
                "task_type": "review",
                "task_title": "Implement monitor",
            }
        ]

    @pytest.mark.asyncio
    async def test_review_loop_pr_number_links_task(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            title="Review monitor",
            payloads=[{"type": "review", "prompt": "x"}],
            context={"review_loop": {"pr_number": 55}},
        )
        db_session.add(project)
        db_session.add(task)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(
            return_value=[
                self._github_pr(55, title="Review PR", branch="review-branch")
            ]
        )
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        pr = resp.json()["open_prs"][0]
        assert pr["is_orphaned"] is False
        assert pr["tasks"] == [
            {
                "task_id": str(task.id),
                "task_status": TaskStatus.EVALUATING,
                "task_type": "review",
                "task_title": "Review monitor",
            }
        ]

    @pytest.mark.asyncio
    async def test_rollup_pr_number_links_task(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        task = make_task(
            project_id=project.id,
            status=TaskStatus.PROVISIONING,
            title="Rollup monitor",
            payloads=[{"type": "plan", "prompt": "x"}],
            context={"rollup_pr_number": 56},
        )
        db_session.add(project)
        db_session.add(task)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(
            return_value=[
                self._github_pr(56, title="Rollup PR", branch="rollup-branch")
            ]
        )
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        pr = resp.json()["open_prs"][0]
        assert pr["is_orphaned"] is False
        assert pr["tasks"] == [
            {
                "task_id": str(task.id),
                "task_status": TaskStatus.PROVISIONING,
                "task_type": "plan",
                "task_title": "Rollup monitor",
            }
        ]

    @pytest.mark.asyncio
    async def test_open_pr_filters_to_current_pr_numbers_before_cap(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """An older linked active task still shows up even when newer unrelated
        active tasks would otherwise consume the 500-row cap."""
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        for i in range(500):
            task = make_task(
                project_id=project.id,
                status=TaskStatus.IN_PROGRESS,
                title=f"Unrelated active {i}",
                payloads=[{"type": "review", "prompt": "x"}],
                context={"review_loop": {"pr_number": 1000 + i}},
            )
            db_session.add(task)
            db_session.add(
                Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.PULL_REQUEST,
                    pr_url=f"https://github.com/acme/widgets/pull/{1000 + i}",
                )
            )

        linked_task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            title="Linked legacy task",
            payloads=[{"type": "review", "prompt": "x"}],
            created_at=_utcnow() - timedelta(days=2),
            context={
                "review_loop": {"pr_number": 99},
                "rollup_pr_number": 99,
            },
        )
        db_session.add(linked_task)
        db_session.add(
            Artifact(
                task_id=linked_task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/widgets/pull/99",
            )
        )
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(
            return_value=[
                self._github_pr(99, title="Legacy PR", branch="legacy-branch")
            ]
        )
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["open_prs"][0]["is_orphaned"] is False
        assert body["open_prs"][0]["tasks"] == [
            {
                "task_id": str(linked_task.id),
                "task_status": TaskStatus.IN_PROGRESS,
                "task_type": "review",
                "task_title": "Linked legacy task",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ],
    )
    async def test_terminal_status_task_is_excluded_even_when_pr_matches(
        self, client: AsyncClient, db_session, monkeypatch, status
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        task = make_task(
            project_id=project.id,
            status=status,
            title="Terminal task",
            payloads=[{"type": "create", "prompt": "x"}],
            context={"review_loop": {"pr_number": 77}, "rollup_pr_number": 77},
        )
        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/acme/widgets/pull/77",
        )
        db_session.add(project)
        db_session.add(task)
        db_session.add(artifact)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(return_value=[self._github_pr(77, title="Terminal PR")])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        resp = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert resp.status_code == 200
        pr = resp.json()["open_prs"][0]
        assert pr["tasks"] == []
        assert pr["is_orphaned"] is True

    @pytest.mark.asyncio
    async def test_second_call_uses_cache_without_second_github_fetch(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        project = make_project(github_owner="acme", github_repo="widgets")
        db_session.add(project)
        await db_session.commit()

        from app import github, services

        token_mock = AsyncMock(return_value="token")
        prs_mock = AsyncMock(return_value=[self._github_pr(88, title="Cached PR")])
        monkeypatch.setattr(services, "_get_project_github_token", token_mock)
        monkeypatch.setattr(github, "get_open_pull_requests", prs_mock)

        first = await client.get(f"/api/admin/open-prs?project_id={project.id}")
        second = await client.get(f"/api/admin/open-prs?project_id={project.id}")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        token_mock.assert_awaited_once()
        prs_mock.assert_awaited_once_with("token", "acme", "widgets")


class TestAdminRecordFailureResult:
    """Cover the _record_failure_result helper used on admin.py failure paths
    (bulk/manual review spawn cap reached, rollup review spawn cap reached)."""

    @pytest.mark.asyncio
    async def test_appends_failure_to_deploy_monitoring(self, _reset_task_results):
        """A failure recorded via the helper shows up in deploy_monitoring task_results."""
        from app.admin import _record_failure_result
        from app.models import Task, TaskStatus
        from app.routers.agent_status import _recorded_task_ids

        task_id = uuid.uuid4()
        _recorded_task_ids.discard(str(task_id))
        task = Task(
            id=task_id,
            project_id=uuid.uuid4(),
            title="t",
            status=TaskStatus.FAILED,
            payloads=[{"type": "review", "prompt": "x"}],
        )

        _record_failure_result(task, "AGENT_TIMEOUT")

        results = list(shared_state.deploy_monitoring["task_results"])
        assert len(results) == 1
        _ts, outcome, failure_class, task_type = results[0]
        assert outcome == "failure"
        assert failure_class == "AGENT_TIMEOUT"
        assert task_type == "review"

    @pytest.mark.asyncio
    async def test_updates_direction_streak_for_non_infra_class(
        self, _reset_task_results
    ):
        """When the task has a direction_id and failure_class is non-infra (e.g. CODE_ERROR),
        record_direction_result updates the direction failure streak."""
        from app.admin import _record_failure_result
        from app.models import Task, TaskStatus
        from app.routers.agent_status import _recorded_task_ids

        direction_id = str(uuid.uuid4())
        shared_state.direction_failure_streaks.pop(direction_id, None)
        task_id = uuid.uuid4()
        _recorded_task_ids.discard(str(task_id))
        task = Task(
            id=task_id,
            project_id=uuid.uuid4(),
            title="t",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create", "prompt": "x"}],
            context={"direction_id": direction_id},
        )

        _record_failure_result(task, "CODE_ERROR")

        streak = shared_state.direction_failure_streaks.get(direction_id)
        assert streak is not None
        assert streak["consecutive_failures"] == 1
        assert streak["last_failure_class"] == "CODE_ERROR"
        shared_state.direction_failure_streaks.pop(direction_id, None)

    @pytest.mark.asyncio
    async def test_agent_timeout_does_not_touch_direction_streak(
        self, _reset_task_results
    ):
        """Infra failures (AGENT_TIMEOUT, RATE_LIMIT, PROVISION_FAILURE, etc.)
        are platform noise — not the direction's fault. They must not
        increment any direction streak or hold flag. Deploy monitoring
        still records them (for overall pipeline health)."""
        from app.admin import _record_failure_result
        from app.models import Task, TaskStatus
        from app.routers.agent_status import _recorded_task_ids

        direction_id = str(uuid.uuid4())
        shared_state.direction_failure_streaks.pop(direction_id, None)
        task_id = uuid.uuid4()
        _recorded_task_ids.discard(str(task_id))
        task = Task(
            id=task_id,
            project_id=uuid.uuid4(),
            title="t",
            status=TaskStatus.FAILED,
            payloads=[{"type": "review", "prompt": "x"}],
            context={"direction_id": direction_id},
        )

        _record_failure_result(task, "AGENT_TIMEOUT")

        # Direction may have no streak entry at all, or one with zero counters.
        streak = shared_state.direction_failure_streaks.get(direction_id, {})
        assert streak.get("consecutive_failures", 0) == 0
        assert not streak.get("held")
        # Deploy monitoring still captures the failure (pipeline-level signal).
        results = list(shared_state.deploy_monitoring["task_results"])
        assert len(results) == 1
        assert results[0][2] == "AGENT_TIMEOUT"
        shared_state.direction_failure_streaks.pop(direction_id, None)


# ---------------------------------------------------------------------------
# TestImprovementLanes
# ---------------------------------------------------------------------------


def _make_lane(project_id, **kwargs):
    """Minimal ImprovementLane factory for tests."""
    defaults = dict(
        id=uuid.uuid4(),
        project_id=project_id,
        direction_id="d-test123",
        title="Test lane",
        goal="Improve something",
        branch_name="lane/test-branch",
        base_branch="main",
        status=ImprovementLaneStatus.PARKED.value,
        checkpoint_seq=0,
        checkpoints=[],
        workspace_state="COLD",
        phase_requeues=0,
        llm_input_tokens_total=0,
        llm_output_tokens_total=0,
        vm_cost_usd_total=0.0,
        reserved_idle_cost_usd_total=0.0,
    )
    defaults.update(kwargs)
    return ImprovementLane(**defaults)


class TestImprovementLanes:
    @pytest.mark.asyncio
    async def test_lists_lanes_for_project_with_reservation(
        self, client: AsyncClient, db_session
    ):
        """Admin gets lane status/checkpoint/workspace/reservation/arm/cost fields."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        lane = _make_lane(
            project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
            direction_id="d-abc",
            branch_name="lane/active-branch",
            checkpoint_seq=3,
            checkpoints=[
                {
                    "seq": 1,
                    "phase": "review",
                    "created_at": (now - timedelta(seconds=120)).isoformat(),
                    "sha": "aaa111",
                },
                {
                    "seq": 2,
                    "phase": "fix",
                    "created_at": (now - timedelta(seconds=60)).isoformat(),
                    "sha": "bbb222",
                },
                {
                    "seq": 3,
                    "phase": "review",
                    "created_at": (now - timedelta(seconds=30)).isoformat(),
                    "sha": "ccc333",
                },
            ],
            last_checkpoint_phase="review",
            last_checkpoint_sha="ccc333",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-res1",
            pilot_arm="lane",
            llm_input_tokens_total=500,
            llm_output_tokens_total=250,
            vm_cost_usd_total=0.12,
            reserved_idle_cost_usd_total=0.03,
        )
        db_session.add(lane)
        await db_session.commit()

        expires_at = now + timedelta(hours=1)
        pool_vm = PoolVm(
            vm_instance_id="agent-vm-res1",
            project_id=project.id,
            state=PoolVmState.IDLE,
            reserved_lane_id=lane.id,
            reservation_expires_at=expires_at,
        )
        db_session.add(pool_vm)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_returned"] == 1
        row = body["lanes"][0]

        assert row["id"] == str(lane.id)
        assert row["status"] == ImprovementLaneStatus.ACTIVE.value
        assert row["direction_id"] == "d-abc"
        assert row["branch"] == "lane/active-branch"
        assert row["last_checkpoint_phase"] == "review"
        assert row["last_checkpoint_seq"] == 3
        assert row["last_checkpoint_sha"] == "ccc333"
        assert row["last_checkpoint_age_seconds"] is not None
        assert row["last_checkpoint_age_seconds"] >= 28  # ~30s ago
        assert row["checkpoint_loss_count"] == 0  # 3 written, 3 kept
        assert row["workspace_state"] == "WARM_RESERVED"
        assert row["workspace_vm_instance_id"] == "agent-vm-res1"
        assert row["reservation_expires_at"] is not None
        assert row["reserved_vm_instance_id"] == "agent-vm-res1"
        assert row["pilot_arm"] == "lane"
        assert row["phase_requeues"] == 0
        assert row["llm_input_tokens_total"] == 500
        assert row["llm_output_tokens_total"] == 250
        assert abs(row["vm_cost_usd_total"] - 0.12) < 1e-5
        assert abs(row["reserved_idle_cost_usd_total"] - 0.03) < 1e-5
        assert row["shadow"] is False
        assert row["title"] == "Test lane"
        assert row["recovery"] is None

    @pytest.mark.asyncio
    async def test_title_included_in_lane_row(self, client: AsyncClient, db_session):
        """Lane title is returned as a top-level field in the row."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane(project.id, title="My custom lane title")
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["title"] == "My custom lane title"

    @pytest.mark.asyncio
    async def test_recovery_field_included_when_manifest_has_recovery(
        self, client: AsyncClient, db_session
    ):
        """Recovery state from lane manifest is surfaced in the row."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        recovery_payload = {
            "reason": "checkpoint_miss",
            "attempt": 2,
            "exhausted": False,
            "at": "2026-06-01T12:00:00",
        }
        lane = _make_lane(project.id, manifest={"recovery": recovery_payload})
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["recovery"] == recovery_payload
        assert row["recovery"]["reason"] == "checkpoint_miss"
        assert row["recovery"]["attempt"] == 2
        assert row["recovery"]["exhausted"] is False

    @pytest.mark.asyncio
    async def test_recovery_field_null_when_manifest_empty(
        self, client: AsyncClient, db_session
    ):
        """Recovery is None when lane manifest has no recovery key."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane(project.id, manifest={"other_key": "value"})
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["recovery"] is None

    @pytest.mark.asyncio
    async def test_pagination_limit_offset(self, client: AsyncClient, db_session):
        """Pagination via limit/offset returns correct subset and ordering."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        for i in range(3):
            lane = _make_lane(
                project.id,
                branch_name=f"lane/branch-{i}",
                status=ImprovementLaneStatus.PARKED.value,
            )
            # Stagger updated_at so ordering is deterministic.
            lane.updated_at = now - timedelta(minutes=i)
            db_session.add(lane)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes?project_id={project.id}&limit=2&offset=1"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_returned"] == 2
        assert body["offset"] == 1
        assert body["limit"] == 2

    @pytest.mark.asyncio
    async def test_project_isolation(self, client: AsyncClient, db_session):
        """Only lanes belonging to the requested project are returned."""
        project_a = make_project()
        project_b = make_project()
        db_session.add(project_a)
        db_session.add(project_b)
        await db_session.commit()

        lane_a = _make_lane(project_a.id, branch_name="lane/for-a")
        lane_b = _make_lane(project_b.id, branch_name="lane/for-b")
        db_session.add(lane_a)
        db_session.add(lane_b)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes?project_id={project_a.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_returned"] == 1
        assert body["lanes"][0]["branch"] == "lane/for-a"

    @pytest.mark.asyncio
    async def test_checkpoint_age_derived_from_last_checkpoint(
        self, client: AsyncClient, db_session
    ):
        """last_checkpoint_age_seconds is derived from the last checkpoint's created_at."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        age_seconds = 300
        lane = _make_lane(
            project.id,
            checkpoint_seq=1,
            checkpoints=[
                {
                    "seq": 1,
                    "phase": "fix",
                    "created_at": (now - timedelta(seconds=age_seconds)).isoformat(),
                    "sha": "abc123",
                }
            ],
            last_checkpoint_phase="fix",
            last_checkpoint_sha="abc123",
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        row = body["lanes"][0]
        assert row["last_checkpoint_age_seconds"] is not None
        # Allow ±5s for test execution time.
        assert abs(row["last_checkpoint_age_seconds"] - age_seconds) < 5

    @pytest.mark.asyncio
    async def test_checkpoint_loss_count_derived(self, client: AsyncClient, db_session):
        """checkpoint_loss_count = checkpoint_seq - len(checkpoints) when window is full."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        # Simulate 55 written but only 3 kept (window would cap at 50 in prod).
        lane = _make_lane(
            project.id,
            checkpoint_seq=55,
            checkpoints=[
                {
                    "seq": 53,
                    "phase": "fix",
                    "created_at": (now - timedelta(seconds=300)).isoformat(),
                    "sha": "a1",
                },
                {
                    "seq": 54,
                    "phase": "review",
                    "created_at": (now - timedelta(seconds=200)).isoformat(),
                    "sha": "b2",
                },
                {
                    "seq": 55,
                    "phase": "fix",
                    "created_at": (now - timedelta(seconds=100)).isoformat(),
                    "sha": "c3",
                },
            ],
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        # 55 written, 3 in window → 52 lost.
        assert row["checkpoint_loss_count"] == 52

    @pytest.mark.asyncio
    async def test_shadow_mode_flag(self, client: AsyncClient, db_session):
        """shadow=True when checkpoints exist but workspace is COLD and no pilot_arm."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        lane = _make_lane(
            project.id,
            checkpoint_seq=2,
            checkpoints=[
                {
                    "seq": 1,
                    "phase": "fix",
                    "created_at": (now - timedelta(seconds=200)).isoformat(),
                    "sha": "x1",
                },
                {
                    "seq": 2,
                    "phase": "review",
                    "created_at": (now - timedelta(seconds=100)).isoformat(),
                    "sha": "x2",
                },
            ],
            workspace_state="COLD",
            pilot_arm=None,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["shadow"] is True

    @pytest.mark.asyncio
    async def test_unauthenticated_caller_gets_403(
        self, override_get_session, db_session
    ):
        """A caller without admin privileges receives 403."""
        from fastapi import HTTPException
        from app.auth import get_current_user, require_admin
        from app.main import app as _app
        from httpx import AsyncClient, ASGITransport

        async def non_admin():
            return {"user_id": "other", "role": "user"}

        async def reject_admin():
            raise HTTPException(status_code=403, detail="Admin privileges required")

        _app.dependency_overrides[get_current_user] = non_admin
        _app.dependency_overrides[require_admin] = reject_admin
        try:
            async with AsyncClient(
                transport=ASGITransport(app=_app), base_url="http://test"
            ) as ac:
                resp = await ac.get(
                    f"/api/admin/improvement-lanes?project_id={uuid.uuid4()}"
                )
            assert resp.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_current_user, None)
            _app.dependency_overrides.pop(require_admin, None)

    @pytest.mark.asyncio
    async def test_at_risk_false_when_phase_requeues_below_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=False when phase_requeues < _LANE_MAX_PHASE_REQUEUES - 1."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane(project.id, phase_requeues=1)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["phase_requeues"] == 1
        assert row["at_risk"] is False

    @pytest.mark.asyncio
    async def test_at_risk_true_when_phase_requeues_at_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=True when phase_requeues >= _LANE_MAX_PHASE_REQUEUES - 1 (i.e., >= 2)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Test at threshold (phase_requeues = 2, which is _LANE_MAX_PHASE_REQUEUES - 1)
        lane = _make_lane(project.id, phase_requeues=2)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["phase_requeues"] == 2
        assert row["at_risk"] is True

    @pytest.mark.asyncio
    async def test_at_risk_true_when_phase_requeues_above_threshold(
        self, client: AsyncClient, db_session
    ):
        """at_risk=True when phase_requeues > _LANE_MAX_PHASE_REQUEUES - 1."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Test above threshold (phase_requeues = 3, which is _LANE_MAX_PHASE_REQUEUES)
        lane = _make_lane(project.id, phase_requeues=3)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        row = resp.json()["lanes"][0]
        assert row["phase_requeues"] == 3
        assert row["at_risk"] is True


class TestShadowLaneHealth:
    """Tests for shadow_health rollup in GET /api/admin/improvement-lanes."""

    @pytest.mark.asyncio
    async def test_shadow_health_basic_counts(self, client: AsyncClient, db_session):
        """Shadow lane with checkpoint_seq=2 → shadow_lane_count=1, checkpoints_written=2."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane(
            project.id,
            checkpoint_seq=2,
            checkpoints=[
                {
                    "seq": 1,
                    "phase": "fix",
                    "created_at": "2026-01-01T00:00:00",
                    "sha": "a1",
                },
                {
                    "seq": 2,
                    "phase": "review",
                    "created_at": "2026-01-01T01:00:00",
                    "sha": "a2",
                },
            ],
            workspace_state="COLD",
            pilot_arm=None,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        sh = resp.json()["shadow_health"]
        assert sh["shadow_lane_count"] == 1
        assert sh["checkpoints_written"] == 2
        assert sh["checkpoint_miss_count"] == 0
        assert sh["miss_rate"] is None

    @pytest.mark.asyncio
    async def test_shadow_health_miss_rate(self, client: AsyncClient, db_session):
        """Two shadow lanes with phase_requeues=1 and 0 → miss_rate=1/(2+1)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane_a = _make_lane(
            project.id,
            branch_name="lane/a",
            checkpoint_seq=2,
            phase_requeues=1,
            workspace_state="COLD",
            pilot_arm=None,
        )
        lane_b = _make_lane(
            project.id,
            branch_name="lane/b",
            checkpoint_seq=1,
            phase_requeues=0,
            workspace_state="COLD",
            pilot_arm=None,
        )
        db_session.add(lane_a)
        db_session.add(lane_b)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        sh = resp.json()["shadow_health"]
        assert sh["shadow_lane_count"] == 2
        assert sh["checkpoints_written"] == 3
        assert sh["checkpoint_miss_count"] == 1
        # miss_rate = 1 / (3 + 1) = 0.25
        assert abs(sh["miss_rate"] - 0.25) < 1e-6

    @pytest.mark.asyncio
    async def test_shadow_health_exhausted_closes(
        self, client: AsyncClient, db_session
    ):
        """KILLED shadow lane with manifest.recovery.exhausted=True → exhausted_close_count=1."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        lane = _make_lane(
            project.id,
            checkpoint_seq=1,
            status=ImprovementLaneStatus.KILLED.value,
            workspace_state="COLD",
            pilot_arm=None,
            manifest={
                "recovery": {"exhausted": True, "attempt": 3, "reason": "no_checkpoint"}
            },
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        sh = resp.json()["shadow_health"]
        assert sh["exhausted_close_count"] == 1

    @pytest.mark.asyncio
    async def test_shadow_health_excludes_itt_lanes(
        self, client: AsyncClient, db_session
    ):
        """Lanes with pilot_arm set are ITT arms and must NOT appear in the shadow rollup."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # ITT arm lane — should be excluded
        itt_lane = _make_lane(
            project.id,
            branch_name="lane/itt",
            checkpoint_seq=5,
            phase_requeues=2,
            workspace_state="COLD",
            pilot_arm="lane",
        )
        # Shadow lane — should be included
        shadow_lane = _make_lane(
            project.id,
            branch_name="lane/shadow",
            checkpoint_seq=3,
            phase_requeues=0,
            workspace_state="COLD",
            pilot_arm=None,
        )
        db_session.add(itt_lane)
        db_session.add(shadow_lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        sh = resp.json()["shadow_health"]
        assert sh["shadow_lane_count"] == 1
        assert sh["checkpoints_written"] == 3
        assert sh["checkpoint_miss_count"] == 0

    @pytest.mark.asyncio
    async def test_shadow_health_empty(self, client: AsyncClient, db_session):
        """No shadow lanes → shadow_lane_count=0 and miss_rate=None."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        sh = resp.json()["shadow_health"]
        assert sh["shadow_lane_count"] == 0
        assert sh["checkpoints_written"] == 0
        assert sh["checkpoint_miss_count"] == 0
        assert sh["miss_rate"] is None


# ---------------------------------------------------------------------------
# TestWorkspaceStateCounts
# ---------------------------------------------------------------------------


class TestWorkspaceStateCounts:
    """Tests for workspace_state_counts aggregate in GET /api/admin/improvement-lanes."""

    @pytest.mark.asyncio
    async def test_workspace_state_counts_returned(
        self, client: AsyncClient, db_session
    ):
        """Three lanes with mixed workspace_state values → counts tally correctly."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        db_session.add(_make_lane(project.id, workspace_state="COLD"))
        db_session.add(_make_lane(project.id, workspace_state="WARM_RESERVED"))
        db_session.add(_make_lane(project.id, workspace_state="WARM_RESERVED"))
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        counts = resp.json()["workspace_state_counts"]
        assert counts["COLD"] == 1
        assert counts["WARM_RESERVED"] == 2

    @pytest.mark.asyncio
    async def test_workspace_state_counts_fleet_wide(
        self, client: AsyncClient, db_session
    ):
        """workspace_state_counts covers all lanes, not just the paginated window."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Add 3 lanes but request limit=1 (only 1 in paginated window).
        for _ in range(3):
            db_session.add(_make_lane(project.id, workspace_state="WARM_RESERVED"))
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/improvement-lanes?project_id={project.id}&limit=1&offset=0"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_returned"] == 1
        assert body["workspace_state_counts"]["WARM_RESERVED"] == 3


# ---------------------------------------------------------------------------
# TestImprovementLanesTrialMetrics
# ---------------------------------------------------------------------------


class TestImprovementLanesTrialMetrics:
    """Tests for trial_metrics field in GET /api/admin/improvement-lanes."""

    @pytest.mark.asyncio
    async def test_trial_metrics_present_with_zero_counts_when_no_pilot_tasks(
        self, client: AsyncClient, db_session
    ):
        """No pilot tasks → trial_metrics has n_lane=0, n_control=0, verdict=CONTINUE."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert "trial_metrics" in body
        tm = body["trial_metrics"]
        assert tm is not None
        assert tm["n_lane"] == 0
        assert tm["n_control"] == 0
        assert tm["verdict"] == "CONTINUE"
        assert tm["verdict_reason"] == "insufficient_data"
        assert isinstance(tm["immediate_alert"], bool)

    @pytest.mark.asyncio
    async def test_trial_metrics_present_alongside_lanes_and_shadow_health(
        self, client: AsyncClient, db_session
    ):
        """trial_metrics is returned alongside the lanes list and shadow_health keys."""
        project = make_project()
        db_session.add(project)
        # Commit the project first so its row physically exists before the lane
        # is inserted. ImprovementLane.project_id is a bare FK column with no ORM
        # relationship() to Project, so a single flush has no unit-of-work
        # dependency to order the project insert ahead of the lane — committing
        # both together can insert the lane first and trip
        # improvementlane_project_id_fkey. Mirrors every other lane test here.
        await db_session.commit()
        lane = _make_lane(project.id, status=ImprovementLaneStatus.ACTIVE.value)
        db_session.add(lane)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        # All three top-level fields must coexist.
        assert "lanes" in body
        assert "shadow_health" in body
        assert "trial_metrics" in body

    @pytest.mark.asyncio
    async def test_trial_metrics_null_when_aggregation_raises(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        """Aggregation failure → trial_metrics is None, lanes list still returned."""
        import app.review_fix_lane_pilot as _rlp

        async def _bad_aggregates(session, project_id):
            raise RuntimeError("simulated aggregation failure")

        monkeypatch.setattr(_rlp, "compute_lane_pilot_aggregates", _bad_aggregates)

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert "lanes" in body
        # trial_metrics is null when aggregation fails.
        assert body.get("trial_metrics") is None

    @pytest.mark.asyncio
    async def test_trial_metrics_includes_relay_comparison_when_stored(
        self, client: AsyncClient, db_session
    ):
        """When capability_adoption_verdicts stores a lane_relay_comparison blob for
        the lane-pilot capability, it is surfaced under trial_metrics."""
        from app.capability_trial_verdict import LANE_PILOT_CAP_NAME

        relay_cmp = {
            "source_version": {
                "version_number": 7,
                "captured_at": "2026-01-01T00:00:00",
                "terminal_status": "promoted",
            },
            "relay": {
                "wall_clock_seconds": 3600.0,
                "active_seconds": 1800.0,
                "gap_seconds": 1800.0,
            },
            "lane": {
                "wall_clock_seconds": 2800.0,
                "active_seconds": 1600.0,
                "gap_seconds": 1200.0,
            },
            "deltas": {
                "wall_clock_seconds": -800.0,
                "active_seconds": -200.0,
                "gap_seconds": -600.0,
            },
        }
        project = make_project(
            capability_adoption_verdicts={
                LANE_PILOT_CAP_NAME: {
                    "verdict": "CONTINUE",
                    "verdict_reason": "insufficient_data",
                    "lane_relay_comparison": relay_cmp,
                }
            }
        )
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        tm = body.get("trial_metrics")
        assert tm is not None
        assert "lane_relay_comparison" in tm
        cmp_out = tm["lane_relay_comparison"]
        assert cmp_out["source_version"]["version_number"] == 7
        assert cmp_out["relay"]["wall_clock_seconds"] == 3600.0
        assert cmp_out["deltas"]["wall_clock_seconds"] == -800.0

    @pytest.mark.asyncio
    async def test_trial_metrics_omits_relay_comparison_when_absent(
        self, client: AsyncClient, db_session
    ):
        """When capability_adoption_verdicts has no lane_relay_comparison, the key
        is absent from trial_metrics (not set to null)."""
        from app.capability_trial_verdict import LANE_PILOT_CAP_NAME

        project = make_project(
            capability_adoption_verdicts={
                LANE_PILOT_CAP_NAME: {
                    "verdict": "CONTINUE",
                    "verdict_reason": "insufficient_data",
                }
            }
        )
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/improvement-lanes?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        tm = body.get("trial_metrics")
        assert tm is not None
        assert "lane_relay_comparison" not in tm


class TestLaneBaselines:
    """Tests for GET /api/admin/lane-baselines."""

    @staticmethod
    def _make_baseline(version_number: int, pr_count: int = 3) -> dict:
        """Build a minimal lane baseline blob matching the persist_version_lane_baseline shape."""
        return {
            "version_number": version_number,
            "baseline_def_version": "v1",
            "terminal_status": "promoted",
            "captured_at": "2026-01-01T00:00:00",
            "opened_at": "2025-12-01T00:00:00",
            "closed_at": "2026-01-01T00:00:00",
            "pr_count": pr_count,
            "task_count": pr_count * 2,
            "relay_medians": {
                "wall_clock_seconds": 3600.0,
                "active_seconds": 1800.0,
                "gap_seconds": 1800.0,
                "task_count": 2.0,
                "pr_count": pr_count,
            },
        }

    @pytest.mark.asyncio
    async def test_lane_baselines_endpoint_returns_persisted_versions(
        self, client: AsyncClient, db_session
    ):
        """Project with two stored baselines returns both sorted descending by version,
        each including the relay_medians field needed for lane-vs-relay comparison."""
        from app.lane_baseline_snapshot import DESIGN_DOC_KEY

        project = make_project(
            design_doc={
                DESIGN_DOC_KEY: {
                    "9": self._make_baseline(9, pr_count=5),
                    "10": self._make_baseline(10, pr_count=8),
                }
            }
        )
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-baselines?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == str(project.id)
        assert body["count"] == 2
        # Most recent version first.
        versions = [b["version_number"] for b in body["baselines"]]
        assert versions == [10, 9]
        # relay_medians field is present in each baseline.
        for b in body["baselines"]:
            assert "relay_medians" in b
            assert b["relay_medians"]["pr_count"] > 0

    @pytest.mark.asyncio
    async def test_project_with_no_baselines_returns_empty(
        self, client: AsyncClient, db_session
    ):
        """Project that exists but has no stored baselines returns count=0 and empty list."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-baselines?project_id={project.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["baselines"] == []

    @pytest.mark.asyncio
    async def test_unknown_project_id_returns_404(
        self, client: AsyncClient, db_session
    ):
        """A project_id that does not exist returns HTTP 404."""
        resp = await client.get(f"/api/admin/lane-baselines?project_id={uuid.uuid4()}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_baselines_sorted_descending_by_version(
        self, client: AsyncClient, db_session
    ):
        """Baselines are ordered newest-first regardless of insertion order in the dict."""
        from app.lane_baseline_snapshot import DESIGN_DOC_KEY

        project = make_project(
            design_doc={
                DESIGN_DOC_KEY: {
                    "3": self._make_baseline(3),
                    "1": self._make_baseline(1),
                    "2": self._make_baseline(2),
                }
            }
        )
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-baselines?project_id={project.id}")
        assert resp.status_code == 200
        versions = [b["version_number"] for b in resp.json()["baselines"]]
        assert versions == [3, 2, 1]


# ---------------------------------------------------------------------------
# TestImprovementLanesDrain
# ---------------------------------------------------------------------------


class TestImprovementLanesDrain:
    """Tests for POST /api/admin/improvement-lanes/drain."""

    @pytest.mark.asyncio
    async def test_operator_drain_lane_pilot_uses_kill_cleanup_semantics(
        self, client: AsyncClient, db_session, mocker
    ):
        """Drain endpoint uses apply_lane_pilot_kill semantics: clears reservation,
        kills lane rows, disables runtime flag, and records a durable KILL verdict
        so is_review_fix_lane_killed returns True after reload.

        This is the named acceptance test: same cleanup semantics as the automated
        KILL path, not a bare boolean flip.
        """
        from app.review_fix_lane_pilot import is_review_fix_lane_killed
        from app.shared_state import runtime_config

        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        lane = _make_lane(
            project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
            pilot_arm="lane",
        )
        db_session.add(lane)
        await db_session.flush()

        pool_vm = PoolVm(
            vm_instance_id="drain-test-vm-1",
            project_id=project.id,
            state=PoolVmState.IDLE,
            reserved_lane_id=lane.id,
        )
        db_session.add(pool_vm)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/drain?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["project_id"] == str(project.id)
        assert body["flag_disabled"] is True
        assert body["reservations_cleared"] == 1
        assert body["lanes_killed"] == 1
        assert body["reason"] == "operator_drain"

        # VM reservation must be cleared.
        await db_session.refresh(pool_vm)
        assert pool_vm.reserved_lane_id is None
        assert pool_vm.reservation_expires_at is None

        # Lane row must be KILLED.
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.KILLED.value

        # Runtime flag must be disabled.
        assert runtime_config.get("review_fix_lane_pilot_enabled") is False

        # Durable KILL verdict must be recorded so the spawn gate blocks after reload.
        await db_session.refresh(project)
        assert is_review_fix_lane_killed(project) is True

    @pytest.mark.asyncio
    async def test_drain_idempotent_second_call_returns_zero_counts(
        self, client: AsyncClient, db_session, mocker
    ):
        """A second drain on an already-drained pilot succeeds with zero counts."""
        from app.shared_state import runtime_config

        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": False},
        )

        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/drain?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reservations_cleared"] == 0
        assert body["lanes_killed"] == 0
        assert body["flag_disabled"] is True

    @pytest.mark.asyncio
    async def test_drain_non_owner_member_returns_403(
        self, client: AsyncClient, db_session
    ):
        """A project member without owner role receives 403."""
        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="member")
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/drain?project_id={project.id}"
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_drain_unknown_project_returns_404(
        self, client: AsyncClient, db_session
    ):
        """A project_id that does not exist returns HTTP 404."""
        resp = await client.post(
            f"/api/admin/improvement-lanes/drain?project_id={uuid.uuid4()}"
        )
        assert resp.status_code == 404


class TestKillImprovementLane:
    """Tests for POST /api/admin/improvement-lanes/{lane_id}/kill."""

    @pytest.mark.asyncio
    async def test_kill_lane_transitions_to_killed_and_releases_reservation(
        self, client: AsyncClient, db_session
    ):
        """Happy path: owner kills a single ACTIVE lane via HTTP. The lane
        transitions to KILLED, its warm-pool reservation is released, and the
        response carries project_id, lane_id, and the kill report counts."""
        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        lane = _make_lane(
            project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
            pilot_arm="lane",
        )
        db_session.add(lane)
        await db_session.flush()

        pool_vm = PoolVm(
            vm_instance_id="kill-lane-test-vm-1",
            project_id=project.id,
            state=PoolVmState.IDLE,
            reserved_lane_id=lane.id,
            reservation_expires_at=_utcnow(),
            reservation_hard_expires_at=_utcnow(),
        )
        db_session.add(pool_vm)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/{lane.id}/kill?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == str(project.id)
        assert body["lane_id"] == str(lane.id)
        assert body["lanes_killed"] == 1
        assert body["reservations_cleared"] == 1

        # Lane row must be KILLED.
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.KILLED.value

        # Reservation must be released so the member rejoins the general pool.
        await db_session.refresh(pool_vm)
        assert pool_vm.reserved_lane_id is None
        assert pool_vm.reservation_expires_at is None
        assert pool_vm.reservation_hard_expires_at is None

    @pytest.mark.asyncio
    async def test_kill_lane_idempotent_on_terminal_lane(
        self, client: AsyncClient, db_session
    ):
        """Killing an already-KILLED lane succeeds with zero counts."""
        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        lane = _make_lane(
            project.id,
            status=ImprovementLaneStatus.KILLED.value,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/{lane.id}/kill?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lanes_killed"] == 0
        assert body["reservations_cleared"] == 0
        assert body["lane_id"] == str(lane.id)

    @pytest.mark.asyncio
    async def test_kill_lane_cross_project_returns_404(
        self, client: AsyncClient, db_session
    ):
        """The lane belongs to project A, but the request scopes to project B
        (also owned by the caller). The cross-project guard returns 404 rather
        than killing a lane outside the named project."""
        owner_project = make_project(name="Owner project")
        other_project = make_project(name="Other project")
        db_session.add(owner_project)
        db_session.add(other_project)
        db_session.add(
            make_membership(owner_project.id, user_id="test-user-123", role="owner")
        )
        db_session.add(
            make_membership(other_project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        # Lane lives in owner_project ...
        lane = _make_lane(
            owner_project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
        )
        db_session.add(lane)
        await db_session.commit()

        # ... but the request names other_project, so the guard must 404.
        resp = await client.post(
            f"/api/admin/improvement-lanes/{lane.id}/kill?project_id={other_project.id}"
        )
        assert resp.status_code == 404

        # The lane must be untouched (still ACTIVE) since the kill was rejected.
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.ACTIVE.value

    @pytest.mark.asyncio
    async def test_kill_lane_non_owner_member_returns_403(
        self, client: AsyncClient, db_session
    ):
        """A project member without owner role receives 403."""
        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="member")
        )
        await db_session.commit()

        lane = _make_lane(
            project.id,
            status=ImprovementLaneStatus.ACTIVE.value,
        )
        db_session.add(lane)
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/{lane.id}/kill?project_id={project.id}"
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_kill_lane_unknown_lane_returns_404(
        self, client: AsyncClient, db_session
    ):
        """An unknown lane_id under an owned project returns HTTP 404."""
        project = make_project()
        db_session.add(project)
        db_session.add(
            make_membership(project.id, user_id="test-user-123", role="owner")
        )
        await db_session.commit()

        resp = await client.post(
            f"/api/admin/improvement-lanes/{uuid.uuid4()}/kill?project_id={project.id}"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestLanePilotPreflight
# ---------------------------------------------------------------------------


class TestLanePilotPreflight:
    """Tests for GET /api/admin/improvement-lanes/preflight."""

    @pytest.mark.asyncio
    async def test_preflight_all_checks_met(
        self, client: AsyncClient, db_session, mocker
    ):
        """All preconditions met → ready=True and every check met=True."""
        from app.shared_state import runtime_config

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        mocker.patch(
            "app.warm_pool.is_warm_pool_enabled",
            return_value=True,
        )
        mocker.patch(
            "app.warm_pool.is_project_warm_pool_scoped",
            return_value=True,
        )
        mocker.patch(
            "app.warm_pool.warm_pool_project_scope",
            return_value=set(),
        )
        mocker.patch(
            "app.warm_pool.resolve_project_warm_pool_target",
            return_value=2,
        )
        mocker.patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_killed",
            return_value=False,
        )
        mocker.patch(
            "app.review_fix_lane_pilot.is_pilot_eligible_project",
            return_value=True,
        )
        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["project_id"] == str(project.id)
        assert body["durable_verdict"] is None
        for check in body["checks"]:
            assert check["met"] is True, f"Expected {check['name']} to be met"

    @pytest.mark.asyncio
    async def test_preflight_warm_pool_disabled(
        self, client: AsyncClient, db_session, mocker
    ):
        """warm_pool_enabled=False → ready=False, warm_pool_enabled check unmet."""
        from app.shared_state import runtime_config

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        mocker.patch("app.warm_pool.is_warm_pool_enabled", return_value=False)
        mocker.patch("app.warm_pool.is_project_warm_pool_scoped", return_value=True)
        mocker.patch("app.warm_pool.warm_pool_project_scope", return_value=set())
        mocker.patch("app.warm_pool.resolve_project_warm_pool_target", return_value=2)
        mocker.patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_killed", return_value=False
        )
        mocker.patch(
            "app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True
        )
        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        check_map = {c["name"]: c for c in body["checks"]}
        assert check_map["warm_pool_enabled"]["met"] is False
        assert check_map["warm_pool_enabled"]["fix_hint"] is not None

    @pytest.mark.asyncio
    async def test_preflight_project_not_in_scope(
        self, client: AsyncClient, db_session, mocker
    ):
        """Project outside warm pool scope → project_in_scope check unmet."""
        from app.shared_state import runtime_config

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        other_id = str(uuid.uuid4())
        mocker.patch("app.warm_pool.is_warm_pool_enabled", return_value=True)
        mocker.patch("app.warm_pool.is_project_warm_pool_scoped", return_value=False)
        mocker.patch("app.warm_pool.warm_pool_project_scope", return_value={other_id})
        mocker.patch("app.warm_pool.resolve_project_warm_pool_target", return_value=2)
        mocker.patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_killed", return_value=False
        )
        mocker.patch(
            "app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True
        )
        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        check_map = {c["name"]: c for c in body["checks"]}
        assert check_map["project_in_scope"]["met"] is False
        assert other_id in check_map["project_in_scope"]["blocking_value"]

    @pytest.mark.asyncio
    async def test_preflight_kill_verdict_blocks(
        self, client: AsyncClient, db_session, mocker
    ):
        """Durable KILL verdict → no_kill_verdict check unmet and durable_verdict=KILL."""
        from app.capability_trial_verdict import (
            LANE_PILOT_CAP_NAME,
            record_trial_verdict,
        )
        from app.shared_state import runtime_config
        from app.time_utils import _utcnow

        project = make_project()
        record_trial_verdict(
            project,
            LANE_PILOT_CAP_NAME,
            {
                "verdict": "KILL",
                "verdict_reason": "test_kill",
                "evidence_source": "test",
                "evaluated_at": _utcnow().isoformat(),
            },
        )
        db_session.add(project)
        await db_session.commit()

        mocker.patch("app.warm_pool.is_warm_pool_enabled", return_value=True)
        mocker.patch("app.warm_pool.is_project_warm_pool_scoped", return_value=True)
        mocker.patch("app.warm_pool.warm_pool_project_scope", return_value=set())
        mocker.patch("app.warm_pool.resolve_project_warm_pool_target", return_value=2)
        mocker.patch(
            "app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True
        )
        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        assert body["durable_verdict"] == "KILL"
        check_map = {c["name"]: c for c in body["checks"]}
        assert check_map["no_kill_verdict"]["met"] is False

    @pytest.mark.asyncio
    async def test_preflight_zero_pool_target(
        self, client: AsyncClient, db_session, mocker
    ):
        """Effective pool target=0 → warm_pool_target_nonzero check unmet."""
        from app.shared_state import runtime_config

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        mocker.patch("app.warm_pool.is_warm_pool_enabled", return_value=True)
        mocker.patch("app.warm_pool.is_project_warm_pool_scoped", return_value=True)
        mocker.patch("app.warm_pool.warm_pool_project_scope", return_value=set())
        mocker.patch("app.warm_pool.resolve_project_warm_pool_target", return_value=0)
        mocker.patch(
            "app.review_fix_lane_pilot.is_review_fix_lane_killed", return_value=False
        )
        mocker.patch(
            "app.review_fix_lane_pilot.is_pilot_eligible_project", return_value=True
        )
        mocker.patch.dict(
            runtime_config,
            {"improvement_lanes_enabled": True, "review_fix_lane_pilot_enabled": True},
        )

        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        check_map = {c["name"]: c for c in body["checks"]}
        assert check_map["warm_pool_target_nonzero"]["met"] is False
        assert check_map["warm_pool_target_nonzero"]["blocking_value"] == "0"

    @pytest.mark.asyncio
    async def test_preflight_unknown_project_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Unknown project_id → HTTP 404."""
        resp = await client.get(
            f"/api/admin/improvement-lanes/preflight?project_id={uuid.uuid4()}"
        )
        assert resp.status_code == 404


class TestSpendBurnRate:
    @pytest.mark.asyncio
    async def test_returns_daily_and_monthly_spend(
        self, client: AsyncClient, db_session
    ):
        """Completed tasks in window contribute to daily rate and monthly forecast."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        # Two tasks completing 1 day apart (within 7d window), each 1h of VM time.
        for days_ago in (1, 3):
            completed_at = now - timedelta(days=days_ago)
            started_at = completed_at - timedelta(hours=1)
            db_session.add(
                make_task(
                    project_id=project.id,
                    title=f"task-{days_ago}d",
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "create", "prompt": "x"}],
                    started_at=started_at,
                    created_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    vm_provisioned_at=started_at,
                    vm_stopped_at=completed_at,
                    vm_spot=False,
                )
            )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days=7"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == str(project.id)
        assert body["window_days"] == 7
        assert body["task_count"] == 2
        # Two 1-hour tasks at on-demand rate
        expected_total = round(2 * 3600 * admin_analytics.ON_DEMAND_RATE, 2)
        assert body["window_cost_usd"] == pytest.approx(expected_total, abs=0.01)
        assert body["daily_spend_usd"] == pytest.approx(expected_total / 7, abs=0.01)
        assert body["monthly_forecast_usd"] == pytest.approx(
            (expected_total / 7) * 30, abs=0.01
        )

    @pytest.mark.asyncio
    async def test_excludes_tasks_outside_window(
        self, client: AsyncClient, db_session
    ):
        """Tasks older than window_days do not contribute to burn rate."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        # One task inside the window, one outside
        for days_ago, title in [(1, "in-window"), (10, "out-of-window")]:
            completed_at = now - timedelta(days=days_ago)
            started_at = completed_at - timedelta(hours=1)
            db_session.add(
                make_task(
                    project_id=project.id,
                    title=title,
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "create", "prompt": "x"}],
                    started_at=started_at,
                    created_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    vm_provisioned_at=started_at,
                    vm_stopped_at=completed_at,
                    vm_spot=False,
                )
            )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days=7"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1

    @pytest.mark.asyncio
    async def test_no_tasks_returns_zero_rates(self, client: AsyncClient, db_session):
        """Empty project returns zero daily and monthly rates."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days=7"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 0
        assert body["window_cost_usd"] == 0.0
        assert body["daily_spend_usd"] == 0.0
        assert body["monthly_forecast_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_spend_burn_rate_counts_task_completed_inside_window_when_created_before_window(
        self, client: AsyncClient, db_session
    ):
        """A task created before the window but completed inside it is counted.

        Long-running tasks start before the reporting cutoff but finish within
        the window.  The burn-rate endpoint must filter by completion time, not
        creation time, so these tasks are included in window_cost_usd and
        task_count.
        """
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        window_days = 7

        # Task created 10 days ago (outside window) but completed 2 days ago (inside window).
        created_at = now - timedelta(days=10)
        completed_at = now - timedelta(days=2)
        db_session.add(
            make_task(
                project_id=project.id,
                title="long-running-task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=created_at,
                created_at=created_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=created_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        # Excluded control: task created AND completed well outside the window.
        outside_at = now - timedelta(days=20)
        db_session.add(
            make_task(
                project_id=project.id,
                title="fully-outside-task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=outside_at,
                created_at=outside_at,
                status_log=[{"s": "COMPLETED", "t": outside_at.isoformat()}],
                vm_provisioned_at=outside_at,
                vm_stopped_at=outside_at + timedelta(hours=1),
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days={window_days}"
        )
        assert resp.status_code == 200
        body = resp.json()
        # Only the long-running task (completed inside window) should be counted.
        assert body["task_count"] == 1, (
            f"Expected task_count=1 (long-running task completed inside window), got {body['task_count']}"
        )
        assert body["window_cost_usd"] > 0.0, "Expected positive cost for long-running task"
        # Verify derived fields are internally consistent (rounding tolerance=0.10
        # because daily_spend_usd and monthly_forecast_usd are each rounded to 2dp
        # independently, so monthly can drift from daily*30 by up to ~0.15).
        assert body["daily_spend_usd"] == pytest.approx(
            body["window_cost_usd"] / window_days, abs=0.10
        )
        assert body["monthly_forecast_usd"] == pytest.approx(
            body["daily_spend_usd"] * 30, abs=0.10
        )

    @pytest.mark.asyncio
    async def test_spend_burn_rate_counts_task_created_before_widened_window(
        self, client: AsyncClient, db_session
    ):
        """Tasks created before the widened SQL bound still count if they finish in-window."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        window_days = 7
        created_at = now - timedelta(days=20)
        completed_at = now - timedelta(days=1)
        db_session.add(
            make_task(
                project_id=project.id,
                title="very-long-running-task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=created_at,
                created_at=created_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=created_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days={window_days}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["window_cost_usd"] > 0.0

    @pytest.mark.asyncio
    async def test_spend_burn_rate_in_window_task_survives_null_completed_at_crowd(
        self, client: AsyncClient, db_session
    ):
        """A task completed inside the window is not dropped behind 20k null-completed rows."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        window_days = 7
        target_created_at = now - timedelta(days=10)
        target_completed_at = now - timedelta(days=2)
        db_session.add(
            make_task(
                project_id=project.id,
                title="target-task",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=target_created_at,
                created_at=target_created_at,
                status_log=[{"s": "COMPLETED", "t": target_completed_at.isoformat()}],
                vm_provisioned_at=target_created_at,
                vm_stopped_at=target_completed_at,
                vm_spot=False,
            )
        )

        crowd_terminal_at = now - timedelta(days=20)
        crowd_created_at = now - timedelta(days=1)
        db_session.add_all(
            [
                make_task(
                    project_id=project.id,
                    title=f"crowd-{idx}",
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "create", "prompt": "x"}],
                    started_at=crowd_created_at,
                    created_at=crowd_created_at,
                    status_log=[{"s": "COMPLETED", "t": crowd_terminal_at.isoformat()}],
                    vm_provisioned_at=crowd_created_at,
                    vm_stopped_at=crowd_terminal_at + timedelta(hours=1),
                    vm_spot=False,
                )
                for idx in range(20000)
            ]
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days={window_days}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["window_cost_usd"] > 0.0

    @pytest.mark.asyncio
    async def test_spend_burn_rate_excludes_evidenceless_task(
        self, client: AsyncClient, db_session
    ):
        """A terminal, in-window task with no VM cost evidence (no provisioning
        window, no banked cost) is excluded from the SQL-aggregated burn rate."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        completed_at = now - timedelta(days=1)
        started_at = completed_at - timedelta(hours=1)
        # Evidence-bearing task: 1h on-demand VM.
        db_session.add(
            make_task(
                project_id=project.id,
                title="with-evidence",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                created_at=started_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                vm_provisioned_at=started_at,
                vm_stopped_at=completed_at,
                vm_spot=False,
            )
        )
        # Evidence-less task: same window, but never provisioned and no banked cost.
        db_session.add(
            make_task(
                project_id=project.id,
                title="no-evidence",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "create", "prompt": "x"}],
                created_at=completed_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/spend-burn-rate?project_id={project.id}&window_days=7"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_count"] == 1
        assert body["window_cost_usd"] == pytest.approx(
            round(3600 * admin_analytics.ON_DEMAND_RATE, 2), abs=0.01
        )
