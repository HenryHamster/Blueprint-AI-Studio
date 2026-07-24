"""Tests for alert creation side-effects.

Covers: failure alerts, circuit breaker alerts, auto-pause/resume alerts,
and deduplication logic.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlmodel import select as sql_select

from app.models import Alert, AlertSeverity, Task, TaskStatus
from app.alert_dispatch import (
    _maybe_create_failure_alert,
    _maybe_create_circuit_breaker_alert,
    _maybe_create_escalation_alert,
    _maybe_create_precondition_blocker_alert,
    _maybe_create_stale_staged_direction_alert,
)
from app.deploy_health import (
    _create_auto_pause_alerts,
    _create_auto_resume_alerts,
    _create_forced_resume_alerts,
)
from testing.factories import make_project, make_task, make_alert


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _all_alerts(db_session, **filters):
    """Return all Alert rows, optionally filtered by column equality."""
    stmt = sql_select(Alert)
    for col, val in filters.items():
        stmt = stmt.where(getattr(Alert, col) == val)
    return (await db_session.execute(stmt)).scalars().all()


async def _undismissed_alerts(db_session, **filters):
    stmt = sql_select(Alert).where(Alert.dismissed_at.is_(None))
    for col, val in filters.items():
        stmt = stmt.where(getattr(Alert, col) == val)
    return (await db_session.execute(stmt)).scalars().all()


async def _sr_doc(db_session, project) -> dict:
    """Read SR analytics the way the production reader does.

    The SR analytics keys moved out of ``project.design_doc`` into the dedicated
    ``project_sr_analytics`` row. ``compute_sr_catch_rate_trends`` now writes only
    ``sr_row.data``; ``project.design_doc`` survives as a per-key *read* fallback
    for projects not yet backfilled, and dedicated values win the merge. Asserting
    against ``project.design_doc`` therefore never observes a freshly written alert.
    """
    from app.sr_analytics_store import read_project_sr_analytics

    return await read_project_sr_analytics(db_session, project)


async def _set_sr_trends(db_session, project, trends: dict) -> None:
    """Overwrite the stored ``sr_catch_rate_trends`` blob in the dedicated row.

    Mutating ``design_doc`` cannot restage these tests any more: the impl merges
    ``doc.update(sr_row.data)``, so a dedicated value written by an earlier phase
    shadows whatever design_doc says.
    """
    from app.models import ProjectSRAnalytics
    from sqlalchemy.orm.attributes import flag_modified

    row = await db_session.get(ProjectSRAnalytics, project.id)
    assert row is not None, "expected compute_sr_catch_rate_trends to create the row"
    data = dict(row.data or {})
    data["sr_catch_rate_trends"] = trends
    row.data = data
    flag_modified(row, "data")
    db_session.add(row)
    await db_session.commit()


# ---------------------------------------------------------------------------
# Task failure alerts
# ---------------------------------------------------------------------------


class TestFailureAlert:
    """Tests for _maybe_create_failure_alert."""

    @pytest.mark.asyncio
    async def test_creates_error_alert_for_failed_task(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id, status=TaskStatus.FAILED, title="Deploy widget"
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.ERROR
        assert alerts[0].project_id == project.id
        assert alerts[0].task_id == task.id
        assert "Deploy widget" in alerts[0].title

    @pytest.mark.asyncio
    async def test_smoketest_tasks_are_excluded(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            payloads=[{"type": "smoketest"}],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_retry_tasks_are_excluded(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            context={"is_retry": True},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_deduplication_skips_if_undismissed_alert_exists(
        self, db_session, mocker
    ):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Pre-existing undismissed alert for this task
        existing = make_alert(project_id=project.id, task_id=task.id, severity="ERROR")
        db_session.add(existing)
        await db_session.commit()

        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1  # still only the original

    @pytest.mark.asyncio
    async def test_error_snippet_included_in_message(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session, error_snippet="OOM killed")
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        assert "OOM killed" in alerts[0].message

    @pytest.mark.asyncio
    async def test_default_message_when_no_snippet(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        assert "See task logs for details." in alerts[0].message

    @pytest.mark.asyncio
    async def test_task_failed_dispatch_payload_is_enriched(self, db_session, mocker):
        import asyncio

        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            title="Build widget",
            payloads=[{"type": "create", "scope": "small"}],
            context={
                "direction_id": "d-abc",
                "direction_text": "improve coverage",
                "pr_url": "https://example.com/pr/1",
            },
            attempt_count=2,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session, error_snippet="boom")
        await asyncio.sleep(0)

        dispatch_mock.assert_called_once()
        payload = dispatch_mock.call_args.args[2]
        assert dispatch_mock.call_args.args[1] == "TASK_FAILED"
        assert payload["failure_class"]
        assert "taxonomy" in payload
        assert payload["direction_id"] == "d-abc"
        assert payload["direction_text"] == "improve coverage"
        assert payload["scope"] == "small"
        assert payload["pr_url"] == "https://example.com/pr/1"
        assert payload["attempt_count"] == 2

    @pytest.mark.asyncio
    async def test_broadcasts_alert_created_event(self, db_session, mocker):
        mock_bc = mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        await _maybe_create_failure_alert(task, db_session)

        mock_bc.assert_called_once()
        call_args = mock_bc.call_args
        assert call_args[0][0] == str(project.id)
        assert call_args[0][1] == "alert_created"
        assert call_args[0][2]["severity"] == "ERROR"


# ---------------------------------------------------------------------------
# Circuit breaker alerts
# ---------------------------------------------------------------------------


class TestCircuitBreakerAlert:
    """Tests for _maybe_create_circuit_breaker_alert."""

    @pytest.mark.asyncio
    async def test_creates_warning_alert_with_reason(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _maybe_create_circuit_breaker_alert(
            project.id, db_session, "3 consecutive failures in 1h"
        )
        await db_session.commit()

        alerts = await _undismissed_alerts(
            db_session, project_id=project.id, severity=AlertSeverity.WARNING
        )
        assert len(alerts) == 1
        assert alerts[0].title == "Circuit breaker opened: autonomous tasks paused"
        assert "3 consecutive failures" in alerts[0].message
        assert alerts[0].task_id is None

    @pytest.mark.asyncio
    async def test_deduplication_skips_if_undismissed_alert_exists(
        self, db_session, mocker
    ):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _maybe_create_circuit_breaker_alert(
            project.id, db_session, "first reason"
        )
        await db_session.commit()

        await _maybe_create_circuit_breaker_alert(
            project.id, db_session, "second reason"
        )
        await db_session.commit()

        alerts = await _all_alerts(
            db_session,
            project_id=project.id,
        )
        # Filter to circuit breaker alerts specifically
        cb_alerts = [
            a
            for a in alerts
            if a.title == "Circuit breaker opened: autonomous tasks paused"
        ]
        assert len(cb_alerts) == 1
        assert "first reason" in cb_alerts[0].message

    @pytest.mark.asyncio
    async def test_new_alert_created_after_dismissal(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create and dismiss first alert
        await _maybe_create_circuit_breaker_alert(
            project.id, db_session, "first reason"
        )
        await db_session.commit()

        first = (
            (
                await db_session.execute(
                    sql_select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        first.dismissed_at = _utcnow()
        db_session.add(first)
        await db_session.commit()

        # Second call should create a new alert now
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "new reason")
        await db_session.commit()

        all_cb = [
            a
            for a in await _all_alerts(db_session, project_id=project.id)
            if a.title == "Circuit breaker opened: autonomous tasks paused"
        ]
        assert len(all_cb) == 2
        undismissed = [a for a in all_cb if a.dismissed_at is None]
        assert len(undismissed) == 1
        assert "new reason" in undismissed[0].message

    @pytest.mark.asyncio
    async def test_different_projects_get_separate_alerts(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        p1 = make_project(name="Project A")
        p2 = make_project(name="Project B")
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()
        await db_session.refresh(p1)
        await db_session.refresh(p2)

        await _maybe_create_circuit_breaker_alert(p1.id, db_session, "reason A")
        await _maybe_create_circuit_breaker_alert(p2.id, db_session, "reason B")
        await db_session.commit()

        a1 = await _undismissed_alerts(db_session, project_id=p1.id)
        a2 = await _undismissed_alerts(db_session, project_id=p2.id)
        assert len(a1) == 1
        assert len(a2) == 1
        assert "reason A" in a1[0].message
        assert "reason B" in a2[0].message


# ---------------------------------------------------------------------------
# Review-fix early-escalation alerts
# ---------------------------------------------------------------------------


class TestEscalationAlert:
    """Tests for _maybe_create_escalation_alert (stuck / standoff / repeat-fp)."""

    async def _make_task(self, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id, status=TaskStatus.FAILED, title="Build widget"
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return project, task

    @pytest.mark.asyncio
    async def test_creates_warning_alert_tied_to_task(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=5
        )

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.project_id == project.id
        assert alert.task_id == task.id
        # Title encodes PR + reason; message encodes the iteration count.
        assert "PR #42" in alert.title
        assert "stuck cycle" in alert.title
        assert "5" in alert.message

    @pytest.mark.asyncio
    async def test_standoff_reason_label_in_title(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=7, reason="standoff", iteration=3
        )

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        assert "dispute standoff" in alerts[0].title
        assert "PR #7" in alerts[0].title

    @pytest.mark.asyncio
    async def test_repeat_fingerprint_reason_label_in_title(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=9, reason="repeat-fingerprint", iteration=2
        )

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        assert "repeat-fingerprint halt" in alerts[0].title

    @pytest.mark.asyncio
    async def test_deduplicates_same_pr_and_reason(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=5
        )
        # Re-entry with a higher iteration count must NOT create a second alert.
        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=6
        )

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        # Original message (iteration 5) is preserved — dedup short-circuits.
        assert "5" in alerts[0].message

    @pytest.mark.asyncio
    async def test_distinct_reasons_create_separate_alerts(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=5
        )
        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="standoff", iteration=5
        )

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 2
        titles = {a.title for a in alerts}
        assert any("stuck cycle" in t for t in titles)
        assert any("dispute standoff" in t for t in titles)

    @pytest.mark.asyncio
    async def test_new_alert_after_dismissal(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=5
        )
        first = (await _all_alerts(db_session, task_id=task.id))[0]
        first.dismissed_at = _utcnow()
        db_session.add(first)
        await db_session.commit()

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=6
        )

        all_alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(all_alerts) == 2
        undismissed = [a for a in all_alerts if a.dismissed_at is None]
        assert len(undismissed) == 1
        assert "6" in undismissed[0].message

    @pytest.mark.asyncio
    async def test_broadcasts_alert_created_event(self, db_session, mocker):
        mock_bc = mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project, task = await self._make_task(db_session)

        await _maybe_create_escalation_alert(
            task, db_session, pr_number=42, reason="stuck", iteration=5
        )

        mock_bc.assert_called_once()
        call_args = mock_bc.call_args
        assert call_args[0][0] == str(project.id)
        assert call_args[0][1] == "alert_created"
        assert call_args[0][2]["severity"] == "WARNING"


# ---------------------------------------------------------------------------
# Precondition-blocker escalation alerts
# ---------------------------------------------------------------------------


class TestPreconditionBlockerAlert:
    """Tests for _maybe_create_precondition_blocker_alert.

    Mirrors the review-fix escalation lane: a WARNING alert tied to the task,
    deduplicated by (task_id, blocker identity), fired regardless of approval
    gates (the helper reads no gate).
    """

    async def _make_task(self, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = make_task(
            project_id=project.id, status=TaskStatus.QUEUED, title="Build widget"
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return project, task

    @pytest.mark.asyncio
    async def test_creates_warning_alert_tied_to_task(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project, task = await self._make_task(db_session)

        created = await _maybe_create_precondition_blocker_alert(
            task,
            db_session,
            blocker_kind="file conflict on a.py, b.py",
            detail="Waiting: file conflict on a.py, b.py",
        )
        assert created is True

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.project_id == project.id
        assert alert.task_id == task.id
        # Title encodes the blocker identity so distinct blockers dedup apart.
        assert "file conflict on a.py, b.py" in alert.title
        assert "Waiting: file conflict" in alert.message

    @pytest.mark.asyncio
    async def test_deduplicates_same_blocker(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="upstream dependency", detail="first"
        )
        assert created is True
        # Re-fire on the same blocker identity must NOT create a second alert.
        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="upstream dependency", detail="second"
        )
        assert created is False

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 1
        # Original message preserved — dedup short-circuits before re-insert.
        assert alerts[0].message == "first"

    @pytest.mark.asyncio
    async def test_distinct_blockers_create_separate_alerts(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="file conflict on a.py", detail="d1"
        )
        assert created is True
        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="pending rollup 'Refactor X'", detail="d2"
        )
        assert created is True

        alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(alerts) == 2
        titles = {a.title for a in alerts}
        assert any("file conflict on a.py" in t for t in titles)
        assert any("pending rollup 'Refactor X'" in t for t in titles)

    @pytest.mark.asyncio
    async def test_broadcasts_alert_created_event(self, db_session, mocker):
        mock_bc = mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project, task = await self._make_task(db_session)

        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="upstream dependency", detail="d"
        )
        assert created is True

        mock_bc.assert_called_once()
        call_args = mock_bc.call_args
        assert call_args[0][0] == str(project.id)
        assert call_args[0][1] == "alert_created"
        assert call_args[0][2]["severity"] == "WARNING"

    @pytest.mark.asyncio
    async def test_dedup_path_does_not_commit_caller_task_mutation(
        self, db_session, mocker
    ):
        """Contract boundary: on the dedup (existing-alert) path the helper
        returns False WITHOUT committing, so it never flushes a task mutation the
        caller staged on its behalf. Callers that park a task into BLOCKED
        (orchestrator_filters._record_defer_and_maybe_escalate) must own that
        commit themselves — durability must not hinge on whether this helper
        happened to create (and therefore commit) an alert this cycle."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        _project, task = await self._make_task(db_session)

        # Seed the alert so the next call hits the dedup short-circuit.
        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="file conflict on a.py", detail="d1"
        )
        assert created is True

        # Spy on commit, stage a task mutation the helper must NOT persist.
        commit_spy = mocker.spy(db_session, "commit")
        task.status = TaskStatus.BLOCKED
        db_session.add(task)

        created = await _maybe_create_precondition_blocker_alert(
            task, db_session, blocker_kind="file conflict on a.py", detail="d2"
        )
        assert created is False
        # The dedup path committed nothing — the caller's staged BLOCKED mutation
        # is not persisted as a side effect of the alert helper.
        commit_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-pause alerts
# ---------------------------------------------------------------------------


class TestAutoPauseAlerts:
    """Tests for _create_auto_pause_alerts."""

    @pytest.mark.asyncio
    async def test_creates_warning_alert_per_project(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        p1 = make_project(name="Project A")
        p2 = make_project(name="Project B")
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()
        await db_session.refresh(p1)
        await db_session.refresh(p2)

        await _create_auto_pause_alerts(db_session, ["gcp", "github_api"])
        await db_session.commit()

        for pid in (p1.id, p2.id):
            alerts = await _undismissed_alerts(
                db_session, project_id=pid, severity=AlertSeverity.WARNING
            )
            assert len(alerts) == 1
            assert "auto-paused" in alerts[0].title.lower()
            assert "gcp" in alerts[0].message
            assert "github_api" in alerts[0].message

    @pytest.mark.asyncio
    async def test_failed_deps_listed_in_message(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_pause_alerts(db_session, ["db", "dispatch_api"])
        await db_session.commit()

        alerts = await _undismissed_alerts(db_session, project_id=project.id)
        assert len(alerts) == 1
        assert "db" in alerts[0].message
        assert "dispatch_api" in alerts[0].message
        assert "Will auto-resume when healthy." in alerts[0].message

    @pytest.mark.asyncio
    async def test_deduplication_per_project(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        # Second call with different deps should be deduped (same title)
        await _create_auto_pause_alerts(db_session, ["gcp", "github_api"])
        await db_session.commit()

        alerts = await _undismissed_alerts(
            db_session, project_id=project.id, severity=AlertSeverity.WARNING
        )
        assert len(alerts) == 1

    @pytest.mark.asyncio
    async def test_no_projects_creates_no_alerts(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        alerts = await _all_alerts(db_session)
        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# Auto-resume alerts
# ---------------------------------------------------------------------------


class TestAutoResumeAlerts:
    """Tests for _create_auto_resume_alerts."""

    @pytest.mark.asyncio
    async def test_creates_info_alert_and_dismisses_pause_warnings(
        self, db_session, mocker
    ):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # First create a pause alert
        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        # Now resume
        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        # Pause alert should be dismissed
        pause_alerts = (
            (
                await db_session.execute(
                    sql_select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.severity == AlertSeverity.WARNING,
                        Alert.title
                        == "Pipeline auto-paused: external dependencies down",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(pause_alerts) == 1
        assert pause_alerts[0].dismissed_at is not None

        # Resume alert should exist
        resume_alerts = await _undismissed_alerts(
            db_session,
            project_id=project.id,
            severity=AlertSeverity.INFO,
        )
        assert len(resume_alerts) == 1
        assert "auto-resumed" in resume_alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_resume_without_prior_pause_still_creates_info_alert(
        self, db_session, mocker
    ):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        alerts = await _undismissed_alerts(
            db_session, project_id=project.id, severity=AlertSeverity.INFO
        )
        assert len(alerts) == 1
        assert "auto-resumed" in alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_dismisses_multiple_pause_warnings(self, db_session, mocker):
        """If somehow multiple pause warnings exist, all should be dismissed."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Manually create two pause alerts (bypassing dedup for edge-case testing)
        for i in range(2):
            alert = make_alert(
                project_id=project.id,
                severity="WARNING",
                title="Pipeline auto-paused: external dependencies down",
                message=f"Reason {i}",
            )
            db_session.add(alert)
        await db_session.commit()

        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        # Both warnings dismissed
        warnings = (
            (
                await db_session.execute(
                    sql_select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.severity == AlertSeverity.WARNING,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert all(w.dismissed_at is not None for w in warnings)

        # One INFO resume alert
        infos = await _undismissed_alerts(
            db_session, project_id=project.id, severity=AlertSeverity.INFO
        )
        assert len(infos) == 1

    @pytest.mark.asyncio
    async def test_resume_across_multiple_projects(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        p1 = make_project(name="Project A")
        p2 = make_project(name="Project B")
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()
        await db_session.refresh(p1)
        await db_session.refresh(p2)

        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        for pid in (p1.id, p2.id):
            # Each project gets an INFO alert
            infos = await _undismissed_alerts(
                db_session, project_id=pid, severity=AlertSeverity.INFO
            )
            assert len(infos) == 1
            # Each project's pause alert is dismissed
            warnings = (
                (
                    await db_session.execute(
                        sql_select(Alert).where(
                            Alert.project_id == pid,
                            Alert.severity == AlertSeverity.WARNING,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(w.dismissed_at is not None for w in warnings)


# ---------------------------------------------------------------------------
# Cross-cutting deduplication
# ---------------------------------------------------------------------------


class TestAlertDeduplication:
    """End-to-end deduplication scenarios across alert types."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_full_lifecycle(self, db_session, mocker):
        """Create -> dedup -> dismiss -> create again."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 1) Create first alert
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "reason A")
        await db_session.commit()

        # 2) Second call deduped
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "reason B")
        await db_session.commit()

        cb_title = "Circuit breaker opened: autonomous tasks paused"
        all_cb = [
            a
            for a in await _all_alerts(db_session, project_id=project.id)
            if a.title == cb_title
        ]
        assert len(all_cb) == 1

        # 3) Dismiss
        all_cb[0].dismissed_at = _utcnow()
        db_session.add(all_cb[0])
        await db_session.commit()

        # 4) New alert now succeeds
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "reason C")
        await db_session.commit()

        all_cb = [
            a
            for a in await _all_alerts(db_session, project_id=project.id)
            if a.title == cb_title
        ]
        assert len(all_cb) == 2
        undismissed = [a for a in all_cb if a.dismissed_at is None]
        assert len(undismissed) == 1
        assert "reason C" in undismissed[0].message

    @pytest.mark.asyncio
    async def test_failure_alert_dedup_allows_new_after_dismiss(
        self, db_session, mocker
    ):
        """Dismissed failure alert should not block a new one for the same task."""
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Create first alert
        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        # Dismiss it
        first = (await _all_alerts(db_session, task_id=task.id))[0]
        first.dismissed_at = _utcnow()
        db_session.add(first)
        await db_session.commit()

        # New alert should be created
        await _maybe_create_failure_alert(task, db_session)
        await db_session.commit()

        all_task_alerts = await _all_alerts(db_session, task_id=task.id)
        assert len(all_task_alerts) == 2
        undismissed = [a for a in all_task_alerts if a.dismissed_at is None]
        assert len(undismissed) == 1

    @pytest.mark.asyncio
    async def test_auto_pause_dedup_allows_new_after_dismiss(self, db_session, mocker):
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        # Dismiss via resume
        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        # New pause alert should be created
        await _create_auto_pause_alerts(db_session, ["github_api"])
        await db_session.commit()

        warnings = (
            (
                await db_session.execute(
                    sql_select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.severity == AlertSeverity.WARNING,
                        Alert.title
                        == "Pipeline auto-paused: external dependencies down",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(warnings) == 2
        undismissed = [w for w in warnings if w.dismissed_at is None]
        assert len(undismissed) == 1


# ---------------------------------------------------------------------------
# External notification dispatch for high-severity resume events
# ---------------------------------------------------------------------------


class TestForcedResumeExternalNotifications:
    """Forced resume must dispatch external notifications (webhook + per-project)."""

    @pytest.mark.asyncio
    async def test_forced_resume_dispatches_webhook_and_external_notifications(
        self, db_session, mocker
    ):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch(
            "app.notification._send_webhook_notification", new_callable=AsyncMock
        )
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        p1 = make_project(name="P1")
        p2 = make_project(name="P2")
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()
        await db_session.refresh(p1)
        await db_session.refresh(p2)

        await _create_forced_resume_alerts(db_session, 7200.0, ["gcp", "github_api"])
        await db_session.commit()

        webhook_mock.assert_awaited_once()
        args, _ = webhook_mock.call_args
        assert args[0] == "FORCED_RESUME"
        assert "force-resumed" in args[1].lower()

        assert dispatch_mock.call_count == 2
        called_event_types = {call.args[1] for call in dispatch_mock.call_args_list}
        assert called_event_types == {"FORCED_RESUME"}
        called_project_ids = {call.args[0] for call in dispatch_mock.call_args_list}
        assert called_project_ids == {p1.id, p2.id}
        for call in dispatch_mock.call_args_list:
            payload = call.args[2]
            assert payload["pause_duration_seconds"] == 7200
            assert payload["failed_deps"] == ["gcp", "github_api"]
            assert "force-resumed" in payload["title"].lower()

    @pytest.mark.asyncio
    async def test_forced_resume_no_projects_still_calls_webhook(
        self, db_session, mocker
    ):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch(
            "app.notification._send_webhook_notification", new_callable=AsyncMock
        )
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )

        await _create_forced_resume_alerts(db_session, 3600.0, [])
        await db_session.commit()

        webhook_mock.assert_awaited_once()
        dispatch_mock.assert_not_called()


class TestFlappingResumeExternalNotifications:
    """Flapping auto-resume must dispatch external notifications; non-flapping must not."""

    @pytest.mark.asyncio
    async def test_flapping_resume_dispatches_webhook_and_external_notifications(
        self, db_session, mocker
    ):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch(
            "app.notification._send_webhook_notification", new_callable=AsyncMock
        )
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_resume_alerts(db_session, flapping=True)
        await db_session.commit()

        webhook_mock.assert_awaited_once()
        args, _ = webhook_mock.call_args
        assert args[0] == "FLAPPING_RESUME"
        assert "flapping" in args[1].lower()

        dispatch_mock.assert_called_once()
        call = dispatch_mock.call_args
        assert call.args[0] == project.id
        assert call.args[1] == "FLAPPING_RESUME"
        assert "flapping" in call.args[2]["title"].lower()

    @pytest.mark.asyncio
    async def test_non_flapping_resume_skips_external_notifications(
        self, db_session, mocker
    ):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch(
            "app.notification._send_webhook_notification", new_callable=AsyncMock
        )
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        )
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_resume_alerts(db_session, flapping=False)
        await db_session.commit()

        webhook_mock.assert_not_awaited()
        dispatch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# SR catch-rate baseline-floor alert
# ---------------------------------------------------------------------------


def _make_sr_floor_task(project_id, *, age_days: float, hit: bool) -> Task:
    """Task where SR catches (hit=True) or misses (hit=False) a 'security' issue."""
    check_results = {"security": {"passed": False, "issue": "x"}} if hit else {}
    task = Task(
        project_id=project_id,
        title="SR floor test task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": {"security": 1},
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def _floor_dispatch_calls(mock_dispatch):
    return [
        c
        for c in mock_dispatch.call_args_list
        if (c.kwargs.get("event_type") or (c.args[1] if len(c.args) > 1 else ""))
        == "sr_catch_rate_below_floor"
    ]


class TestSRCatchRateFloorAlert:
    """baseline-floor alert (sr_catch_rate_below_floor) in compute_sr_catch_rate_trends."""

    def _tasks(
        self, project_id, *, current_hits: int, current_misses: int, prev: int = 5
    ):
        """Build task lists spanning current (age_days=2) and previous (age_days=20) windows."""
        tasks = []
        for _ in range(current_hits):
            tasks.append(_make_sr_floor_task(project_id, age_days=2, hit=True))
        for _ in range(current_misses):
            tasks.append(_make_sr_floor_task(project_id, age_days=2, hit=False))
        for _ in range(prev):
            tasks.append(_make_sr_floor_task(project_id, age_days=20, hit=True))
        return tasks

    @pytest.mark.asyncio
    async def test_floor_alert_fires_when_consistently_below_floor(self, db_session):
        from app.sr_analytics import compute_sr_catch_rate_trends

        # Pre-seed rate_history with 2 below-floor entries (threshold=0.50).
        # computed_at yesterday → is_new_window=False → history preserved.
        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.35, 0.40]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ) as mock_dispatch,
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

        assert result is not None
        assert result["categories"]["security"].get("below_floor") is True

        floor_calls = _floor_dispatch_calls(mock_dispatch)
        assert len(floor_calls) == 1
        payload = floor_calls[0].kwargs.get("payload") or floor_calls[0].args[2]
        assert "below_floor_categories" in payload
        assert payload["floor_threshold"] == 0.5
        sec = next(
            c for c in payload["below_floor_categories"] if c["category"] == "security"
        )
        assert sec["current_hit_rate"] is not None
        assert sec["sample_size"] > 0

    @pytest.mark.asyncio
    async def test_floor_alert_dedup_does_not_fire_twice_in_same_window(
        self, db_session
    ):
        from app.sr_analytics import compute_sr_catch_rate_trends

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.35, 0.40]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ) as d1,
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)

        await db_session.refresh(project)
        assert "sr_catch_rate_floor_alert" in await _sr_doc(db_session, project)
        assert len(_floor_dispatch_calls(d1)) == 1

        # Second call in same window — should be deduped
        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ) as d2,
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)

        assert len(_floor_dispatch_calls(d2)) == 0

    @pytest.mark.asyncio
    async def test_floor_alert_does_not_fire_when_above_floor(self, db_session):
        from app.sr_analytics import compute_sr_catch_rate_trends

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.60, 0.70]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 4/5 hits → current hit_rate=0.80, well above floor
        tasks = self._tasks(project.id, current_hits=4, current_misses=1)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ) as mock_dispatch,
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

        assert result is not None
        assert result["categories"]["security"].get("below_floor") is False
        assert len(_floor_dispatch_calls(mock_dispatch)) == 0

    @pytest.mark.asyncio
    async def test_regression_alert_unaffected_when_floor_also_triggers(
        self, db_session
    ):
        """A simultaneous regression and floor violation fires both distinct events."""
        from app.sr_analytics import compute_sr_catch_rate_trends

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        # rate_history below floor; current window will also be decreasing vs previous
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.30, 0.35]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # current: 2/5 hits = 0.40; previous: 4/5 hits = 0.80 → decreasing AND below floor
        tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for _ in range(4):
            tasks.append(_make_sr_floor_task(project.id, age_days=20, hit=True))
        tasks.append(_make_sr_floor_task(project.id, age_days=20, hit=False))
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ) as mock_dispatch,
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

        assert result is not None
        assert result["categories"]["security"]["trend"] == "decreasing"
        assert result["categories"]["security"].get("below_floor") is True

        event_types = [
            c.kwargs.get("event_type") or (c.args[1] if len(c.args) > 1 else "")
            for c in mock_dispatch.call_args_list
        ]
        assert "sr_catch_rate_regression" in event_types
        assert "sr_catch_rate_below_floor" in event_types


# ---------------------------------------------------------------------------
# SR catch-rate Alert rows (in-app AlertsBanner surfacing)
# ---------------------------------------------------------------------------


class TestSRCatchRateFloorAlertRow:
    """Verify floor-firing path inserts an ERROR Alert row and dismisses on clear."""

    def _tasks(
        self, project_id, *, current_hits: int, current_misses: int, prev: int = 5
    ):
        tasks = []
        for _ in range(current_hits):
            tasks.append(_make_sr_floor_task(project_id, age_days=2, hit=True))
        for _ in range(current_misses):
            tasks.append(_make_sr_floor_task(project_id, age_days=2, hit=False))
        for _ in range(prev):
            tasks.append(_make_sr_floor_task(project_id, age_days=20, hit=True))
        return tasks

    @pytest.mark.asyncio
    async def test_floor_alert_creates_error_row(self, db_session):
        from app.playbook_learning import (
            compute_sr_catch_rate_trends,
            _SR_FLOOR_ALERT_TITLE_PREFIX,
        )

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.35, 0.40]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
        await db_session.commit()

        rows = await _undismissed_alerts(db_session, project_id=project.id)
        floor_rows = [
            r for r in rows if r.title.startswith(_SR_FLOOR_ALERT_TITLE_PREFIX)
        ]
        assert len(floor_rows) == 1
        row = floor_rows[0]
        assert row.severity == AlertSeverity.ERROR
        assert row.task_id is None
        assert "security" in row.title
        # Message mirrors the post_failure_alert_message description
        assert "security" in row.message
        assert "below floor" in row.message

    @pytest.mark.asyncio
    async def test_floor_alert_no_duplicate_when_dedup_holds(self, db_session):
        from app.playbook_learning import (
            compute_sr_catch_rate_trends,
            _SR_FLOOR_ALERT_TITLE_PREFIX,
        )

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.35, 0.40]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
            await db_session.commit()
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
            await db_session.commit()

        rows = await _undismissed_alerts(db_session, project_id=project.id)
        floor_rows = [
            r for r in rows if r.title.startswith(_SR_FLOOR_ALERT_TITLE_PREFIX)
        ]
        assert len(floor_rows) == 1

    @pytest.mark.asyncio
    async def test_floor_alert_dismissed_on_clear(self, db_session):
        """Firing-then-clearing dismisses the prior Alert row and clears design_doc state."""
        from app.playbook_learning import (
            compute_sr_catch_rate_trends,
            _SR_FLOOR_ALERT_TITLE_PREFIX,
        )

        yesterday = (_utcnow() - timedelta(days=1)).isoformat()
        project = make_project(
            design_doc={
                "sr_catch_rate_trends": {
                    "computed_at": yesterday,
                    "categories": {"security": {"rate_history": [0.35, 0.40]}},
                },
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Phase 1: fire
        firing_tasks = self._tasks(project.id, current_hits=2, current_misses=3)
        for t in firing_tasks:
            db_session.add(t)
        await db_session.flush()
        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, firing_tasks)
        await db_session.commit()
        await db_session.refresh(project)
        assert "sr_catch_rate_floor_alert" in await _sr_doc(db_session, project)
        rows = await _undismissed_alerts(db_session, project_id=project.id)
        assert any(r.title.startswith(_SR_FLOOR_ALERT_TITLE_PREFIX) for r in rows)

        # Phase 2: clear (history above floor → no longer below_floor).
        # Rewrite rate_history to above-floor values in the dedicated row so the
        # next computation does not register below_floor for this category.
        # Phase 1 already wrote sr_catch_rate_trends there, and the dedicated row
        # wins the merge, so editing design_doc here would have no effect.
        trends = dict((await _sr_doc(db_session, project))["sr_catch_rate_trends"])
        cats = dict(trends.get("categories") or {})
        cats["security"] = {"rate_history": [0.80, 0.85]}
        trends["categories"] = cats
        await _set_sr_trends(db_session, project, trends)

        clearing_tasks = self._tasks(project.id, current_hits=4, current_misses=1)
        for t in clearing_tasks:
            db_session.add(t)
        await db_session.flush()
        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, clearing_tasks)
        await db_session.commit()
        await db_session.refresh(project)

        assert "sr_catch_rate_floor_alert" not in await _sr_doc(db_session, project)
        rows = await _undismissed_alerts(db_session, project_id=project.id)
        floor_rows = [
            r for r in rows if r.title.startswith(_SR_FLOOR_ALERT_TITLE_PREFIX)
        ]
        assert floor_rows == []
        # Original row is still present in the table but dismissed
        all_rows = await _all_alerts(db_session, project_id=project.id)
        prior = [
            r for r in all_rows if r.title.startswith(_SR_FLOOR_ALERT_TITLE_PREFIX)
        ]
        assert len(prior) >= 1
        assert all(r.dismissed_at is not None for r in prior)


class TestSRCatchRateRegressionAlertRow:
    """Verify regression-firing path inserts a WARNING Alert row and dismisses on clear."""

    def _make_sr_task(self, project_id, *, age_days, human_cats, sr_failed_cats):
        # Mirror _make_sr_task in test_playbook_auto.py for self-contained reuse.
        check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
        task = Task(
            project_id=project_id,
            title="SR regression test task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": check_results},
                "_first_attempt_rejection_categories": dict(human_cats),
            },
        )
        task.created_at = _utcnow() - timedelta(days=age_days)
        return task

    def _decreasing_set(self, project_id):
        tasks = []
        # current: 3/5 hits → 0.60
        for _ in range(3):
            tasks.append(
                self._make_sr_task(
                    project_id,
                    age_days=2,
                    human_cats={"test-coverage": 1},
                    sr_failed_cats=["test-coverage"],
                )
            )
        for _ in range(2):
            tasks.append(
                self._make_sr_task(
                    project_id,
                    age_days=2,
                    human_cats={"test-coverage": 1},
                    sr_failed_cats=[],
                )
            )
        # previous: 5/5 hits → 1.00
        for _ in range(5):
            tasks.append(
                self._make_sr_task(
                    project_id,
                    age_days=20,
                    human_cats={"test-coverage": 1},
                    sr_failed_cats=["test-coverage"],
                )
            )
        return tasks

    @pytest.mark.asyncio
    async def test_regression_alert_creates_warning_row(self, db_session):
        from app.sr_analytics import (
            compute_sr_catch_rate_trends,
            _SR_REGRESSION_ALERT_TITLE_PREFIX,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._decreasing_set(project.id)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
        await db_session.commit()

        rows = await _undismissed_alerts(db_session, project_id=project.id)
        reg_rows = [
            r for r in rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
        ]
        assert len(reg_rows) == 1
        row = reg_rows[0]
        assert row.severity == AlertSeverity.WARNING
        assert row.task_id is None
        assert "test-coverage" in row.title
        # Message mirrors the failure-alert description (per-category transition + arrow)
        assert "test-coverage" in row.message
        assert "→" in row.message

    @pytest.mark.asyncio
    async def test_regression_alert_no_duplicate_when_dedup_holds(self, db_session):
        from app.sr_analytics import (
            compute_sr_catch_rate_trends,
            _SR_REGRESSION_ALERT_TITLE_PREFIX,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        tasks = self._decreasing_set(project.id)
        for t in tasks:
            db_session.add(t)
        await db_session.flush()

        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
            await db_session.commit()
            await compute_sr_catch_rate_trends(project.id, db_session, tasks)
            await db_session.commit()

        rows = await _undismissed_alerts(db_session, project_id=project.id)
        reg_rows = [
            r for r in rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
        ]
        assert len(reg_rows) == 1

    @pytest.mark.asyncio
    async def test_regression_alert_dismissed_on_clear(self, db_session):
        from app.sr_analytics import (
            compute_sr_catch_rate_trends,
            _SR_REGRESSION_ALERT_TITLE_PREFIX,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Phase 1: fire
        firing_tasks = self._decreasing_set(project.id)
        for t in firing_tasks:
            db_session.add(t)
        await db_session.flush()
        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, firing_tasks)
        await db_session.commit()
        await db_session.refresh(project)
        assert "sr_catch_rate_trends_alert" in await _sr_doc(db_session, project)

        # Phase 2: a fresh stable dataset — both windows hit-rate 1.00, trend stable.
        # compute_sr_catch_rate_trends operates on the explicitly-passed list, so
        # we pass only the stable tasks (the firing tasks remain in the DB but are
        # irrelevant to this call's analysis).
        stable_tasks = []
        for _ in range(5):
            stable_tasks.append(
                self._make_sr_task(
                    project.id,
                    age_days=2,
                    human_cats={"test-coverage": 1},
                    sr_failed_cats=["test-coverage"],
                )
            )
        for _ in range(5):
            stable_tasks.append(
                self._make_sr_task(
                    project.id,
                    age_days=20,
                    human_cats={"test-coverage": 1},
                    sr_failed_cats=["test-coverage"],
                )
            )
        for t in stable_tasks:
            db_session.add(t)
        await db_session.flush()
        with (
            patch(
                "app.notification_dispatch.post_failure_alert_message",
                new_callable=AsyncMock,
            ),
            patch(
                "app.notification_dispatch.dispatch_external_notification",
                new_callable=AsyncMock,
            ),
            patch(
                "asyncio.ensure_future", side_effect=lambda coro: coro.close() or None
            ),
        ):
            await compute_sr_catch_rate_trends(project.id, db_session, stable_tasks)
        await db_session.commit()
        await db_session.refresh(project)

        assert "sr_catch_rate_trends_alert" not in await _sr_doc(db_session, project)
        rows = await _undismissed_alerts(db_session, project_id=project.id)
        reg_rows = [
            r for r in rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
        ]
        assert reg_rows == []


class TestStaleStagedDirectionAlert:
    """Tests for _maybe_create_stale_staged_direction_alert.

    A project-scoped WARNING alert (task_id=None) deduplicated by
    (project_id, direction-id-in-title); distinct directions get distinct
    alerts; the message names the direction and how long it has waited.
    """

    async def _make_project(self, db_session):
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        return project

    @pytest.mark.asyncio
    async def test_creates_warning_alert_scoped_to_project(self, db_session, mocker):
        mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project = await self._make_project(db_session)

        created = await _maybe_create_stale_staged_direction_alert(
            db_session,
            project.id,
            direction_id="dir-abc",
            direction_text="Refactor the widget pipeline",
            waited_days=30,
            staged_for_version=4,
            open_version=4,
        )
        assert created is True

        alerts = await _all_alerts(db_session, project_id=project.id)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == AlertSeverity.WARNING
        assert alert.project_id == project.id
        assert alert.task_id is None
        assert "dir-abc" in alert.title
        assert "30 day" in alert.message
        assert "Refactor the widget pipeline" in alert.message

    @pytest.mark.asyncio
    async def test_deduplicates_same_direction(self, db_session, mocker):
        mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project = await self._make_project(db_session)

        first = await _maybe_create_stale_staged_direction_alert(
            db_session, project.id, direction_id="dir-x",
            direction_text="t", waited_days=25,
            staged_for_version=3, open_version=3,
        )
        assert first is True
        # A later sweep with a creeping age must NOT create a second alert.
        second = await _maybe_create_stale_staged_direction_alert(
            db_session, project.id, direction_id="dir-x",
            direction_text="t", waited_days=40,
            staged_for_version=3, open_version=3,
        )
        assert second is False
        assert len(await _all_alerts(db_session, project_id=project.id)) == 1

    @pytest.mark.asyncio
    async def test_distinct_directions_get_distinct_alerts(self, db_session, mocker):
        mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project = await self._make_project(db_session)

        await _maybe_create_stale_staged_direction_alert(
            db_session, project.id, direction_id="dir-1",
            direction_text="one", waited_days=25,
            staged_for_version=3, open_version=3,
        )
        await _maybe_create_stale_staged_direction_alert(
            db_session, project.id, direction_id="dir-2",
            direction_text="two", waited_days=25,
            staged_for_version=3, open_version=3,
        )
        alerts = await _all_alerts(db_session, project_id=project.id)
        assert len(alerts) == 2
        titles = {a.title for a in alerts}
        assert any("dir-1" in t for t in titles)
        assert any("dir-2" in t for t in titles)

    @pytest.mark.asyncio
    async def test_broadcasts_alert_created_event(self, db_session, mocker):
        mock_bc = mocker.patch(
            "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
        )
        project = await self._make_project(db_session)

        await _maybe_create_stale_staged_direction_alert(
            db_session, project.id, direction_id="dir-bc",
            direction_text="t", waited_days=22,
            staged_for_version=2, open_version=2,
        )
        mock_bc.assert_called_once()
        call_args = mock_bc.call_args
        assert call_args[0][0] == str(project.id)
        assert call_args[0][1] == "alert_created"
