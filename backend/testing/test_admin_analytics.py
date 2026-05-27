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
