"""Tests for alert creation side-effects.

Covers: failure alerts, circuit breaker alerts, auto-pause/resume alerts,
and deduplication logic.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from sqlmodel import select as sql_select

from app.models import Alert, AlertSeverity, TaskStatus
from app.main import (
    _maybe_create_failure_alert,
    _maybe_create_circuit_breaker_alert,
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


# ---------------------------------------------------------------------------
# Task failure alerts
# ---------------------------------------------------------------------------

class TestFailureAlert:
    """Tests for _maybe_create_failure_alert."""

    @pytest.mark.asyncio
    async def test_creates_error_alert_for_failed_task(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(project_id=project.id, status=TaskStatus.FAILED, title="Deploy widget")
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
    async def test_deduplication_skips_if_undismissed_alert_exists(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mock_bc = mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _maybe_create_circuit_breaker_alert(
            project.id, db_session, "3 consecutive failures in 1h"
        )
        await db_session.commit()

        alerts = await _undismissed_alerts(db_session, project_id=project.id, severity=AlertSeverity.WARNING)
        assert len(alerts) == 1
        assert alerts[0].title == "Circuit breaker opened: autonomous tasks paused"
        assert "3 consecutive failures" in alerts[0].message
        assert alerts[0].task_id is None

    @pytest.mark.asyncio
    async def test_deduplication_skips_if_undismissed_alert_exists(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _maybe_create_circuit_breaker_alert(project.id, db_session, "first reason")
        await db_session.commit()

        await _maybe_create_circuit_breaker_alert(project.id, db_session, "second reason")
        await db_session.commit()

        alerts = await _all_alerts(
            db_session, project_id=project.id,
        )
        # Filter to circuit breaker alerts specifically
        cb_alerts = [a for a in alerts if a.title == "Circuit breaker opened: autonomous tasks paused"]
        assert len(cb_alerts) == 1
        assert "first reason" in cb_alerts[0].message

    @pytest.mark.asyncio
    async def test_new_alert_created_after_dismissal(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create and dismiss first alert
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "first reason")
        await db_session.commit()

        first = (await db_session.execute(
            sql_select(Alert).where(
                Alert.project_id == project.id,
                Alert.dismissed_at.is_(None),
            )
        )).scalars().first()
        first.dismissed_at = _utcnow()
        db_session.add(first)
        await db_session.commit()

        # Second call should create a new alert now
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "new reason")
        await db_session.commit()

        all_cb = [
            a for a in await _all_alerts(db_session, project_id=project.id)
            if a.title == "Circuit breaker opened: autonomous tasks paused"
        ]
        assert len(all_cb) == 2
        undismissed = [a for a in all_cb if a.dismissed_at is None]
        assert len(undismissed) == 1
        assert "new reason" in undismissed[0].message

    @pytest.mark.asyncio
    async def test_different_projects_get_separate_alerts(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
# Auto-pause alerts
# ---------------------------------------------------------------------------

class TestAutoPauseAlerts:
    """Tests for _create_auto_pause_alerts."""

    @pytest.mark.asyncio
    async def test_creates_warning_alert_per_project(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
            alerts = await _undismissed_alerts(db_session, project_id=pid, severity=AlertSeverity.WARNING)
            assert len(alerts) == 1
            assert "auto-paused" in alerts[0].title.lower()
            assert "gcp" in alerts[0].message
            assert "github_api" in alerts[0].message

    @pytest.mark.asyncio
    async def test_failed_deps_listed_in_message(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_pause_alerts(db_session, ["gcp"])
        await db_session.commit()

        # Second call with different deps should be deduped (same title)
        await _create_auto_pause_alerts(db_session, ["gcp", "github_api"])
        await db_session.commit()

        alerts = await _undismissed_alerts(db_session, project_id=project.id, severity=AlertSeverity.WARNING)
        assert len(alerts) == 1

    @pytest.mark.asyncio
    async def test_no_projects_creates_no_alerts(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)

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
    async def test_creates_info_alert_and_dismisses_pause_warnings(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        pause_alerts = (await db_session.execute(
            sql_select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.WARNING,
                Alert.title == "Pipeline auto-paused: external dependencies down",
            )
        )).scalars().all()
        assert len(pause_alerts) == 1
        assert pause_alerts[0].dismissed_at is not None

        # Resume alert should exist
        resume_alerts = await _undismissed_alerts(
            db_session, project_id=project.id, severity=AlertSeverity.INFO,
        )
        assert len(resume_alerts) == 1
        assert "auto-resumed" in resume_alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_resume_without_prior_pause_still_creates_info_alert(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_resume_alerts(db_session)
        await db_session.commit()

        alerts = await _undismissed_alerts(db_session, project_id=project.id, severity=AlertSeverity.INFO)
        assert len(alerts) == 1
        assert "auto-resumed" in alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_dismisses_multiple_pause_warnings(self, db_session, mocker):
        """If somehow multiple pause warnings exist, all should be dismissed."""
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        warnings = (await db_session.execute(
            sql_select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.WARNING,
            )
        )).scalars().all()
        assert all(w.dismissed_at is not None for w in warnings)

        # One INFO resume alert
        infos = await _undismissed_alerts(db_session, project_id=project.id, severity=AlertSeverity.INFO)
        assert len(infos) == 1

    @pytest.mark.asyncio
    async def test_resume_across_multiple_projects(self, db_session, mocker):
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
            infos = await _undismissed_alerts(db_session, project_id=pid, severity=AlertSeverity.INFO)
            assert len(infos) == 1
            # Each project's pause alert is dismissed
            warnings = (await db_session.execute(
                sql_select(Alert).where(
                    Alert.project_id == pid,
                    Alert.severity == AlertSeverity.WARNING,
                )
            )).scalars().all()
            assert all(w.dismissed_at is not None for w in warnings)


# ---------------------------------------------------------------------------
# Cross-cutting deduplication
# ---------------------------------------------------------------------------

class TestAlertDeduplication:
    """End-to-end deduplication scenarios across alert types."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_full_lifecycle(self, db_session, mocker):
        """Create -> dedup -> dismiss -> create again."""
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        all_cb = [a for a in await _all_alerts(db_session, project_id=project.id) if a.title == cb_title]
        assert len(all_cb) == 1

        # 3) Dismiss
        all_cb[0].dismissed_at = _utcnow()
        db_session.add(all_cb[0])
        await db_session.commit()

        # 4) New alert now succeeds
        await _maybe_create_circuit_breaker_alert(project.id, db_session, "reason C")
        await db_session.commit()

        all_cb = [a for a in await _all_alerts(db_session, project_id=project.id) if a.title == cb_title]
        assert len(all_cb) == 2
        undismissed = [a for a in all_cb if a.dismissed_at is None]
        assert len(undismissed) == 1
        assert "reason C" in undismissed[0].message

    @pytest.mark.asyncio
    async def test_failure_alert_dedup_allows_new_after_dismiss(self, db_session, mocker):
        """Dismissed failure alert should not block a new one for the same task."""
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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
        mocker.patch("app.main.broadcast_to_project", new_callable=AsyncMock)
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

        warnings = (await db_session.execute(
            sql_select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.WARNING,
                Alert.title == "Pipeline auto-paused: external dependencies down",
            )
        )).scalars().all()
        assert len(warnings) == 2
        undismissed = [w for w in warnings if w.dismissed_at is None]
        assert len(undismissed) == 1


# ---------------------------------------------------------------------------
# External notification dispatch for high-severity resume events
# ---------------------------------------------------------------------------

class TestForcedResumeExternalNotifications:
    """Forced resume must dispatch external notifications (webhook + per-project)."""

    @pytest.mark.asyncio
    async def test_forced_resume_dispatches_webhook_and_external_notifications(self, db_session, mocker):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch("app.main._send_webhook_notification", new_callable=AsyncMock)
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock,
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
    async def test_forced_resume_no_projects_still_calls_webhook(self, db_session, mocker):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch("app.main._send_webhook_notification", new_callable=AsyncMock)
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock,
        )

        await _create_forced_resume_alerts(db_session, 3600.0, [])
        await db_session.commit()

        webhook_mock.assert_awaited_once()
        dispatch_mock.assert_not_called()


class TestFlappingResumeExternalNotifications:
    """Flapping auto-resume must dispatch external notifications; non-flapping must not."""

    @pytest.mark.asyncio
    async def test_flapping_resume_dispatches_webhook_and_external_notifications(self, db_session, mocker):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch("app.main._send_webhook_notification", new_callable=AsyncMock)
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock,
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
    async def test_non_flapping_resume_skips_external_notifications(self, db_session, mocker):
        mocker.patch("app.deploy_health.broadcast_to_project", new_callable=AsyncMock)
        webhook_mock = mocker.patch("app.main._send_webhook_notification", new_callable=AsyncMock)
        dispatch_mock = mocker.patch(
            "app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock,
        )
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _create_auto_resume_alerts(db_session, flapping=False)
        await db_session.commit()

        webhook_mock.assert_not_awaited()
        dispatch_mock.assert_not_called()
