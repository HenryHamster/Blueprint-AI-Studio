"""Tests for admin_analytics endpoints."""
import uuid
from collections import deque
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app import shared_state
from app.time_utils import _utcnow
from app.models import TaskStatus
from testing.factories import make_project, make_task


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
    async def test_returns_gated_type_with_rate_and_samples(self, client: AsyncClient, _reset_task_results):
        """Type with >=70% failure rate and >=3 samples appears in gated_types."""
        now = _utcnow()
        for i in range(5):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "create")
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
    async def test_no_gated_types_returns_empty_list(self, client: AsyncClient, _reset_task_results):
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
    async def test_insufficient_samples_not_gated(self, client: AsyncClient, _reset_task_results):
        """A type with only 2 samples (below n=3 floor) is not reported as gated."""
        now = _utcnow()
        for i in range(2):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "fix")
            )

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        assert resp.json()["gated_types"] == []

    @pytest.mark.asyncio
    async def test_reports_anomaly_tightened_threshold(self, client: AsyncClient, _reset_task_results):
        """When anomaly_adjustments.active is True the response reflects the tightened threshold."""
        shared_state.anomaly_adjustments["active"] = True
        shared_state.anomaly_adjustments["failure_gate_threshold"] = 0.6

        resp = await client.get("/api/admin/failing-task-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["anomaly_tightened"] is True
        assert body["global_failure_threshold"] == 0.6

    @pytest.mark.asyncio
    async def test_healthy_type_excluded_from_gated(self, client: AsyncClient, _reset_task_results):
        """When both a failing and a healthy type exist, only the failing one is gated."""
        now = _utcnow()
        for i in range(4):
            shared_state.deploy_monitoring["task_results"].append(
                ((now - timedelta(minutes=i)).isoformat(), "failure", "CODE_ERROR", "create")
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
    async def test_scope_duration_stats_computes_p50_p90(self, client: AsyncClient, db_session):
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
                db_session.add(make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "create", "prompt": "x"}],
                    context={"scope": scope},
                    started_at=started_at,
                    status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                    created_at=started_at,
                ))
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
    async def test_scope_duration_stats_skips_insufficient_samples(self, client: AsyncClient, db_session):
        """Scopes with fewer than 3 samples are omitted from scope_duration_stats."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        now = _utcnow()
        started_at = now - timedelta(hours=2)
        for secs in (10, 20):
            completed_at = started_at + timedelta(seconds=secs)
            db_session.add(make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x"}],
                context={"scope": "medium"},
                started_at=started_at,
                status_log=[{"s": "COMPLETED", "t": completed_at.isoformat()}],
                created_at=started_at,
            ))
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
            db_session.add(make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x"}],
                started_at=started_at,
                status_log=[{"s": "COMPLETED", "t": vm_stop.isoformat()}],
                created_at=started_at,
                vm_provisioned_at=vm_prov,
                vm_started_at=vm_start,
                vm_stopped_at=vm_stop,
            ))
        await db_session.commit()

        resp = await client.get("/api/admin/task-duration-stats")
        assert resp.status_code == 200
        phase = resp.json()["phase_duration_stats"]
        assert phase["provisioning"] == {"p50_seconds": 20.0, "sample_size": 3}
        assert phase["execution"] == {"p50_seconds": 200.0, "sample_size": 3}


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
    async def test_updates_direction_streak_for_non_infra_class(self, _reset_task_results):
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
    async def test_agent_timeout_does_not_touch_code_failure_streak(self, _reset_task_results):
        """Infra classes (AGENT_TIMEOUT) are tracked on a separate counter and
        do not increment the direction's code-failure streak."""
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

        streak = shared_state.direction_failure_streaks.get(direction_id)
        assert streak is not None
        assert streak["consecutive_failures"] == 0
        assert streak["consecutive_infra_failures"] == 1
        assert streak["last_failure_class"] == "AGENT_TIMEOUT"
        assert streak["held"] is False
        results = list(shared_state.deploy_monitoring["task_results"])
        assert len(results) == 1
        assert results[0][2] == "AGENT_TIMEOUT"
        shared_state.direction_failure_streaks.pop(direction_id, None)
