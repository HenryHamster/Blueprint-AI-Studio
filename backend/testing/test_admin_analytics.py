"""Tests for admin_analytics endpoints."""

import uuid
from collections import deque
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app import shared_state
from app.models import Artifact, ArtifactType
from app.routers import admin_analytics
from app.time_utils import _utcnow
from app.models import TaskStatus
from testing.factories import make_alert, make_project, make_task


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


class TestLaneDiag:
    @staticmethod
    def _add_review_loop_task(
        db_session, project_id, pr_number, task_type, created_at, active_seconds, fix_reason=None
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
            (101, "panel_review", 110, 600, None),
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
        assert body["task_count"] == 8
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
        assert lanes["arbitration"]["task_count"] == 1

        by_pr = {row["pr_number"]: row for row in body["per_pr_phase_breakdown"]}
        assert list(by_pr.keys()) == ["101", "202"]
        assert by_pr["101"]["review_seconds"] == 1500.0
        assert by_pr["101"]["fix_seconds"] == 3000.0
        assert by_pr["101"]["gap_seconds"] == 2700.0
        assert by_pr["101"]["total_seconds"] == 7200.0
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
    async def test_lane_diag_counts_panel_review_runtime_as_active_time(
        self, client: AsyncClient, db_session
    ):
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        base = _utcnow() - timedelta(hours=1)
        self._add_review_loop_task(
            db_session, project.id, 101, "review", base, 300
        )
        self._add_review_loop_task(
            db_session,
            project.id,
            101,
            "panel_review",
            base + timedelta(minutes=10),
            600,
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["lane_iteration_distribution"]["arbitration"]["task_count"] == 1
        [pr_breakdown] = body["per_pr_phase_breakdown"]
        assert pr_breakdown["review_seconds"] == 900.0
        assert pr_breakdown["fix_seconds"] == 0.0
        assert pr_breakdown["gap_seconds"] == 300.0
        assert pr_breakdown["total_seconds"] == 1200.0
        assert pr_breakdown["queue_seconds"] == 0.0
        assert pr_breakdown["provision_seconds"] == 0.0
        assert pr_breakdown["cold_start_seconds"] == 0.0
        assert pr_breakdown["execution_seconds"] == 0.0

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
            db_session, project.id, 901, "review",
            v9_opened + timedelta(days=1), 600,
        )
        self._add_review_loop_task(
            db_session, project.id, 901, "fix",
            v9_opened + timedelta(days=2), 1200, "ci_failure",
        )
        # Out-of-window task (created before v9 opened)
        self._add_review_loop_task(
            db_session, project.id, 800, "review",
            v9_opened - timedelta(days=2), 300,
        )
        # Out-of-window task (created after v9 promoted)
        self._add_review_loop_task(
            db_session, project.id, 950, "review",
            v9_promoted + timedelta(days=1), 300,
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
            db_session, project.id, pr_number=501, original_task_id=oid_a,
            iteration=1, task_type="review", created_at=base,
        )  # review_ambiguity (LANE_REVIEW_CONVERGENCE, iter≤2), cap-counted
        self._add_chain_task(
            db_session, project.id, pr_number=501, original_task_id=oid_a,
            iteration=2, task_type="fix", fix_reason="ci_failure",
            created_at=base + timedelta(minutes=10),
        )  # ci_churn, cap-counted
        self._add_chain_task(
            db_session, project.id, pr_number=501, original_task_id=oid_a,
            iteration=3, task_type="fix", status=TaskStatus.FAILED,
            failure_category="AGENT_TIMEOUT", created_at=base + timedelta(minutes=20),
        )  # infra, NOT cap-counted, terminal FAILED included
        self._add_chain_task(
            db_session, project.id, pr_number=501, original_task_id=oid_a,
            iteration=4, task_type="fix", fix_reason="merge_conflict",
            failure_detail="review_cap_exhausted",
            created_at=base + timedelta(minutes=30),
        )  # merge_conflict, cap-counted, marks the chain cap-exhausted
        self._add_chain_task(
            db_session, project.id, pr_number=501, original_task_id=oid_a,
            iteration=5, task_type="review", stale_head=True,
            created_at=base + timedelta(minutes=40),
        )  # lost_reasoning (LANE_REVIEW_CONVERGENCE, iter≥3), NOT cap-counted (stale-head discard)
        self._add_original(db_session, project.id, oid_a, base - timedelta(minutes=5))

        # Chain B (PR 502) — merged via original hydration; lost-state lanes.
        self._add_chain_task(
            db_session, project.id, pr_number=502, original_task_id=oid_b,
            iteration=1, task_type="review", drift={"drift_in": ["a.py"]},
            created_at=base + timedelta(minutes=5),
        )  # lost_repo_state
        self._add_chain_task(
            db_session, project.id, pr_number=502, original_task_id=oid_b,
            iteration=2, task_type="fix",
            fix_loop_history={
                "test": [
                    {"loop": "test", "iteration": 1,
                     "exit_reason": "divergence_oscillation",
                     "ended_at": "2026-06-01T00:00:00"}
                ]
            },
            created_at=base + timedelta(minutes=15),
        )  # lost_reasoning
        self._add_chain_task(
            db_session, project.id, pr_number=502, original_task_id=oid_b,
            iteration=3, task_type="panel_review",
            created_at=base + timedelta(minutes=25),
        )  # review_ambiguity
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
            "review_ambiguity": 2,
            "lost_reasoning": 2,
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
        assert b_cats == {1: "lost_repo_state", 2: "lost_reasoning", 3: "review_ambiguity"}

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
            db_session, project.id, pr_number=601, original_task_id=oid,
            iteration=1, task_type="review", created_at=base,
        )
        self._add_chain_task(
            db_session, project.id, pr_number=601, original_task_id=oid,
            iteration=2, task_type="fix", fix_reason="code_review",
            created_at=base + timedelta(minutes=10),
        )
        self._add_chain_task(
            db_session, project.id, pr_number=601, original_task_id=oid,
            iteration=3, task_type="fix", status=TaskStatus.FAILED,
            failure_category="CODE_ERROR", created_at=base + timedelta(minutes=20),
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
            db_session, project.id, pr_number=701, original_task_id=oid,
            iteration=1, task_type="review", created_at=base,
        )
        self._add_chain_task(
            db_session, project.id, pr_number=702, original_task_id=oid,
            iteration=1, task_type="review", created_at=base + timedelta(minutes=5),
        )
        # Two FAILED tasks (more recent) — with a shared max_tasks=2 query these
        # would evict the COMPLETED tasks, eroding the legacy surface.
        self._add_chain_task(
            db_session, project.id, pr_number=703, original_task_id=oid,
            iteration=1, task_type="review", status=TaskStatus.FAILED,
            failure_category="CODE_ERROR",
            created_at=base + timedelta(minutes=20),
        )
        self._add_chain_task(
            db_session, project.id, pr_number=704, original_task_id=oid,
            iteration=1, task_type="review", status=TaskStatus.FAILED,
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

        resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}&max_tasks=2")
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
                "task_count": 2,
                "estimated_cost_usd": pytest.approx(
                    round(5400 * admin_analytics.ON_DEMAND_RATE, 2)
                ),
            },
            {
                "direction_id": None,
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
        assert body["max_tasks"] == 5000
        assert body["truncated"] is False


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
        assert resp.json() == {"open_prs": []}
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

        assert resp.status_code == 200
        assert resp.json() == {"open_prs": []}
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
        assert resp.json() == {"open_prs": []}
        token_mock.assert_awaited_once()
        prs_mock.assert_not_awaited()

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
