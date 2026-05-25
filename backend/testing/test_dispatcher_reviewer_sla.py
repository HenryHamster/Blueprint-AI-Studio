"""Tests for the reviewer-SLA stall signal of the pipeline regression dispatcher.

Covers TestReviewerSlaStallSignal: _evaluate_reviewer_sla_signal trips when an
ACTIVE regression case has a linked EVALUATING task that has been stalled past
the configured SLA threshold (reviewer_sla_stall_hours).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import Task, TaskStatus
from app.time_utils import _utcnow

from testing.factories import make_project, make_task


class TestReviewerSlaStallSignal:
    """Tests for _evaluate_reviewer_sla_signal."""

    @pytest.mark.asyncio
    async def test_stall_trips_and_spawns(self, db_session):
        """ACTIVE case with EVALUATING task older than threshold spawns an investigation."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        evaluating_at = now - timedelta(hours=10)
        stalled_task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=evaluating_at,
            updated_at=evaluating_at,
        )
        stalled_task.status_log = [
            {
                "t": evaluating_at.isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ]
        db_session.add(stalled_task)
        await db_session.commit()
        await db_session.refresh(stalled_task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=stalled_task.id,
            last_remediation_pr_number=1234,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 1
        await db_session.commit()

        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        investigate_tasks = [
            t for t in tasks if (t.first_payload or {}).get("type") == "investigate"
        ]
        assert len(investigate_tasks) == 1
        ctx = investigate_tasks[0].context or {}
        assert ctx.get("regression_class", "").startswith("reviewer_sla_stall:")
        assert ctx.get("signal_name") == "reviewer_sla_stall"
        prompt = (investigate_tasks[0].first_payload or {}).get("prompt", "")
        assert "#1234" in prompt
        assert "EVALUATING" in prompt

    @pytest.mark.asyncio
    async def test_within_sla_does_not_spawn(self, db_session):
        """ACTIVE case with EVALUATING task younger than threshold does not spawn."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        recent_at = now - timedelta(hours=2)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=recent_at,
            updated_at=recent_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_skip_terminal_task(self, db_session):
        """Cases whose linked task is COMPLETED/FAILED/CANCELLED are skipped."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        for terminal_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            task = make_task(
                project_id=project.id,
                status=terminal_status,
                created_at=old_at,
                updated_at=old_at,
            )
            db_session.add(task)
        await db_session.commit()

        terminal_tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        for t in terminal_tasks:
            case = RegressionRemediationCase(
                project_id=project.id,
                regression_class=f"sr_catch_rate:{t.id}",
                status=RegressionCaseStatus.ACTIVE,
                hit_count=1,
                last_remediation_task_id=t.id,
            )
            db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 5)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_skip_non_evaluating_task(self, db_session):
        """Cases whose task is IN_PROGRESS (not yet in EVALUATING) are skipped."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_dedup_in_flight_investigation_blocks_second_spawn(self, db_session):
        """A second cycle with the prior SLA investigation still in-flight does not re-spawn."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        evaluating_at = now - timedelta(hours=10)
        stalled_task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=evaluating_at,
            updated_at=evaluating_at,
        )
        db_session.add(stalled_task)
        await db_session.commit()
        await db_session.refresh(stalled_task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=stalled_task.id,
        )
        db_session.add(case)
        await db_session.commit()
        await db_session.refresh(case)

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            first = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
            assert first == 1
            await db_session.commit()

            second = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
            assert second == 0
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

    @pytest.mark.asyncio
    async def test_status_log_fallback_uses_updated_at(self, db_session):
        """When status_log has no EVALUATING entry, elapsed is derived from updated_at."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=10)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
            status_log=[],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate_fallback",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 1

    @pytest.mark.asyncio
    async def test_feature_flag_disabled_suppresses_spawns(self, db_session):
        """Setting reviewer_sla_stall_enabled=False suppresses all spawns."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_enabled"] = False
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_high_threshold_suppresses_borderline_case(self, db_session):
        """Setting reviewer_sla_stall_hours=48 suppresses a case stalled for only 8h."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=8)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 48
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_budget_zero_returns_immediately(self, db_session):
        """spawn_budget_left=0 returns 0 without touching DB."""
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 0)
        assert spawned == 0
