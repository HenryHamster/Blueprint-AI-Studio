"""Tests for app.alert_dispatch helpers.

Focused on the warm-pool payoff-verdict transition alert
(``_maybe_create_pool_payoff_alert``): creation, broadcast, per-verdict
deduplication, and that the supporting numbers are embedded in the message so
the operator can act from the alert alone. Mirrors the broadcast-mock pattern in
test_alerts.py (patch ``app.alert_dispatch.broadcast_to_project``).
"""

import uuid as _uuid_module
from datetime import timedelta as _timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select as sql_select

from app.alert_dispatch import (
    _LOCAL_RUNNER_HOLD_ALERT_TITLE,
    _MIGRATION_SLOT_CONTENTION_ALERT_TITLE,
    _UNVERIFIED_REASON_LABELS,
    _fmt_duration_seconds,
    _maybe_create_blocked_escalation_alert,
    _maybe_create_escalation_alert,
    _maybe_create_failure_alert,
    _maybe_create_precondition_blocker_alert,
    _maybe_create_local_runner_hold_alert,
    _maybe_create_migration_slot_contention_alert,
    _maybe_create_pool_payoff_alert,
    _maybe_create_runner_deactivated_alert,
    _maybe_create_runner_degraded_alert,
    _maybe_create_unverified_merge_alert,
    _maybe_escalate_runner_deactivated_alerts,
    _maybe_escalate_runner_degraded_alerts,
    _resolve_runner_degraded_alert,
    _runner_escalation_last_notified,
    _runner_outage_escalated_keys,
    sweep_resolve_policy_retirement_alerts,
    sweep_resolve_terminal_task_alerts,
)
from app.models import Alert, AlertSeverity
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


def test_every_operator_action_needed_event_type_declares_a_retirement_rule():
    """Durable operator-action alerts must state how their condition ends."""
    from app.alert_policy import ALERT_EVENT_POLICIES, AlertRetirementMode
    from app.message_audience import get_operator_event_types

    operator_alerts = sorted(
        set(ALERT_EVENT_POLICIES).intersection(get_operator_event_types())
    )
    assert operator_alerts, "the contract must exercise the operator alert registry"
    missing = [
        event_type
        for event_type in operator_alerts
        if ALERT_EVENT_POLICIES[event_type].retirement is None
    ]
    assert not missing, (
        "operator-action-needed alert events without a retirement rule: "
        + ", ".join(missing)
    )
    assert all(
        isinstance(ALERT_EVENT_POLICIES[event_type].retirement, AlertRetirementMode)
        for event_type in operator_alerts
        if event_type
    )
    assert any(
        ALERT_EVENT_POLICIES[event_type].retirement
        == AlertRetirementMode.MANUAL
        for event_type in operator_alerts
        if event_type
    )


@pytest.mark.asyncio
async def test_policy_retirement_sweep_clears_legacy_timeline_rows(db_session, mocker):
    """The policy gate's former durable timeline rows are retired in bounded batches."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.INFO,
        title="Pipeline auto-resumed",
        message="legacy timeline row",
        event_type="deployment.auto_resume",
    )
    db_session.add(alert)
    await db_session.commit()

    assert await sweep_resolve_policy_retirement_alerts(db_session, limit=1) == 1
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None
    bcast.assert_awaited_once()
    assert bcast.await_args.args[1] == "alert_dismissed"


def make_warm_pool_project(**kwargs):
    build_config = {
        "alert_domains": {
            "warm_pool": {"enabled": True},
        }
    }
    build_config.update(kwargs.pop("build_config", {}) or {})
    return make_project(build_config=build_config, **kwargs)


_SUPPORTING = {
    "pool_origin_share": 0.5,
    "pool_warm_starts": 8,
    "pool_hit_rate": 0.4,
    "total_pool_dispatches": 50,
    "cost_drift_pct": 20.0,
    "cost_incl_idle_current": 12.0,
    "cost_baseline": 10.0,
}


async def _payoff_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("Warm-pool payoff"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_pool_payoff_alert_is_suppressed_for_strategist_internal_audience(db_session, mocker):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_warm_pool_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_pool_payoff_alert(
        project.id,
        db_session,
        verdict_state="COST_REGRESSION",
        supporting=_SUPPORTING,
        recommended_action="Reduce the pool target or disable.",
        severity=AlertSeverity.WARNING,
    )

    alerts = await _payoff_alerts(db_session, project.id)
    assert alerts == []
    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_payoff_alert_with_supporting_numbers_is_suppressed(db_session, mocker):
    bcast = mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_warm_pool_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_pool_payoff_alert(
        project.id,
        db_session,
        verdict_state="COST_REGRESSION",
        supporting=_SUPPORTING,
        recommended_action="Reduce the pool target or disable.",
    )
    assert await _payoff_alerts(db_session, project.id) == []
    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_payoff_alert_repeated_verdict_remains_suppressed(db_session, mocker):
    bcast = mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_warm_pool_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(2):
        await _maybe_create_pool_payoff_alert(
            project.id,
            db_session,
            verdict_state="COST_REGRESSION",
            supporting=_SUPPORTING,
            recommended_action="Reduce the pool target or disable.",
        )
    alerts = await _payoff_alerts(db_session, project.id)
    assert alerts == []
    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_payoff_alert_distinct_verdicts_remain_suppressed(db_session, mocker):
    bcast = mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_warm_pool_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_pool_payoff_alert(
        project.id,
        db_session,
        verdict_state="COST_REGRESSION",
        supporting=_SUPPORTING,
        recommended_action="Reduce the pool target or disable.",
        severity=AlertSeverity.WARNING,
    )
    await _maybe_create_pool_payoff_alert(
        project.id,
        db_session,
        verdict_state="PROVEN",
        supporting=_SUPPORTING,
        recommended_action="Keep the pool — consider widening scope.",
        severity=AlertSeverity.WARNING,
    )
    alerts = await _payoff_alerts(db_session, project.id)
    assert alerts == []
    bcast.assert_not_awaited()


# ── _maybe_create_blocked_escalation_alert (terminal BLOCKED escalation) ──────


async def _blocked_escalation_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("permanently blocked"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_blocked_escalation_alert_creates_error_and_broadcasts(db_session, mocker):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, status="BLOCKED", blocker_kind="upstream_dependency")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _maybe_create_blocked_escalation_alert(
        task,
        db_session,
        blocker_kind="upstream_dependency",
        detail="Upstream dependency failed — needs operator intervention.",
    )

    alerts = await _blocked_escalation_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].task_id == task.id
    assert "Upstream dependency failed" in alerts[0].message
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_escalation_alert_dedupes_by_task_and_title(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, status="BLOCKED", blocker_kind="file_conflict")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    for _ in range(3):
        await _maybe_create_blocked_escalation_alert(
            task,
            db_session,
            blocker_kind="file_conflict",
            detail="Blocker outlived the escalation deadline.",
        )

    # Same (task_id, title) ⇒ exactly one undismissed ERROR alert.
    alerts = await _blocked_escalation_alerts(db_session, project.id)
    assert len(alerts) == 1


# ---------------------------------------------------------------------------
# No-CI UNVERIFIED-merge alert (_maybe_create_unverified_merge_alert)
# ---------------------------------------------------------------------------


async def _unverified_alerts(db_session, task_id):
    stmt = sql_select(Alert).where(
        Alert.task_id == task_id,
        Alert.title.contains("Merged UNVERIFIED"),
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _make_task(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, title="No-CI merge parent")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_unverified_merge_alert_creates_and_broadcasts(db_session, mocker):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    task = await _make_task(db_session)

    await _maybe_create_unverified_merge_alert(
        task,
        db_session,
        pr_number=4242,
        reason="no_detectable_test_command",
    )

    alerts = await _unverified_alerts(db_session, task.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].task_id == task.id
    assert "PR #4242" in alerts[0].title
    # The reason label (not the raw key) is rendered into the message.
    assert _UNVERIFIED_REASON_LABELS["no_detectable_test_command"] in (
        alerts[0].message
    )
    assert "UNVERIFIED" in alerts[0].message
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_unverified_merge_alert_reason_label_mapping(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    for reason, label in _UNVERIFIED_REASON_LABELS.items():
        task = await _make_task(db_session)
        await _maybe_create_unverified_merge_alert(
            task, db_session, pr_number=7, reason=reason
        )
        msg = (await _unverified_alerts(db_session, task.id))[0].message
        assert label in msg, f"{reason} must render label '{label}'"


@pytest.mark.asyncio
async def test_unverified_merge_alert_dedups_per_pr(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    task = await _make_task(db_session)

    # Two merge sweeps on the SAME PR must not spam — deduped on (task_id, title).
    for _ in range(2):
        await _maybe_create_unverified_merge_alert(
            task, db_session, pr_number=4242, reason="undetected_runtime"
        )
    alerts = await _unverified_alerts(db_session, task.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_unverified_merge_alert_dispatches_external_notification(
    db_session, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    task = await _make_task(db_session)

    await _maybe_create_unverified_merge_alert(
        task, db_session, pr_number=99, reason="kill_switch_off"
    )
    # Fire-and-forget via ensure_future — let the scheduled task run.
    import asyncio

    await asyncio.sleep(0)
    dispatch.assert_awaited_once()
    args, _kwargs = dispatch.call_args
    assert args[1] == "MERGE_UNVERIFIED"
    assert args[2]["pr_number"] == 99
    assert args[2]["reason"] == "kill_switch_off"


# ── Fleet-wide merge-gate verification degradation alert ─────────────────────

from app.alert_dispatch import (  # noqa: E402
    _MERGE_GATE_DEGRADATION_ALERT_TITLE,
    _maybe_create_merge_gate_degradation_alerts,
)


async def _degradation_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title == _MERGE_GATE_DEGRADATION_ALERT_TITLE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_merge_gate_degradation_alert_creates_and_broadcasts(db_session, mocker):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = await _maybe_create_merge_gate_degradation_alerts(
        db_session,
        verification_rate=0.3,
        verified_count=3,
        fell_open_count=7,
        degraded_project_count=4,
        project_count=5,
        most_common_reason="verify_setup_failed",
        severity=AlertSeverity.ERROR,
    )
    await db_session.commit()

    alerts = await _degradation_alerts(db_session, project.id)
    assert created == 1
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    # Dominant fall-open reason is rendered via its human-readable label.
    assert _UNVERIFIED_REASON_LABELS["verify_setup_failed"] in alerts[0].message
    assert "30% verified" in alerts[0].message
    assert "4 of 5 projects are degraded" in alerts[0].message
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_merge_gate_degradation_alert_dedups_on_stable_title(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await _maybe_create_merge_gate_degradation_alerts(
        db_session,
        verification_rate=0.3,
        verified_count=3,
        fell_open_count=7,
        degraded_project_count=4,
        project_count=5,
        most_common_reason="verify_setup_failed",
        severity=AlertSeverity.ERROR,
    )
    await db_session.commit()
    # A later tick with DIFFERENT drifting numbers must still dedup on the
    # stable title — a moving metric never defeats dedup.
    second = await _maybe_create_merge_gate_degradation_alerts(
        db_session,
        verification_rate=0.25,
        verified_count=2,
        fell_open_count=8,
        degraded_project_count=6,
        project_count=7,
        most_common_reason="no_detectable_test_command",
        severity=AlertSeverity.WARNING,
    )
    await db_session.commit()

    assert first == 1
    assert second == 0  # deduped on stable title
    alerts = await _degradation_alerts(db_session, project.id)
    assert len(alerts) == 1


# ── migration-slot-contention alert ──────────────────────────────────────────


async def _slot_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title == _MIGRATION_SLOT_CONTENTION_ALERT_TITLE,
        Alert.dismissed_at.is_(None),
    )
    return (await db_session.execute(stmt)).scalars().all()


def test_fmt_duration_seconds():
    assert _fmt_duration_seconds(None) == "n/a"
    assert _fmt_duration_seconds(30) == "30s"
    assert _fmt_duration_seconds(90) == "1m"
    assert _fmt_duration_seconds(3600) == "1h"
    assert _fmt_duration_seconds(2 * 3600 + 5 * 60) == "2h 5m"


@pytest.mark.asyncio
async def test_migration_slot_contention_alert_creates_and_embeds_numbers(
    db_session, mocker
):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    holder = make_task(project_id=project.id)
    db_session.add(holder)
    await db_session.commit()

    created = await _maybe_create_migration_slot_contention_alert(
        db_session,
        project.id,
        holder_task_id=str(holder.id),
        held_seconds=2 * 3600,
        waiting_count=5,
        longest_wait_seconds=45 * 60,
    )
    await db_session.commit()
    assert created == 1
    bcast.assert_awaited()

    alerts = await _slot_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.task_id == holder.id
    assert alert.severity == AlertSeverity.WARNING
    # Drifting numbers live in the message (not the title) so dedup survives them.
    assert "2h" in alert.message
    assert "5 schema-touching task" in alert.message
    assert "45m" in alert.message


@pytest.mark.asyncio
async def test_migration_slot_contention_alert_dedups_on_stable_title(
    db_session, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await _maybe_create_migration_slot_contention_alert(
        db_session,
        project.id,
        holder_task_id=None,
        held_seconds=2 * 3600,
        waiting_count=3,
        longest_wait_seconds=10 * 60,
    )
    await db_session.commit()
    assert first == 1

    # Drifting numbers must not defeat dedup — same stable title.
    second = await _maybe_create_migration_slot_contention_alert(
        db_session,
        project.id,
        holder_task_id=None,
        held_seconds=3 * 3600,
        waiting_count=7,
        longest_wait_seconds=90 * 60,
    )
    await db_session.commit()
    assert second == 0
    assert len(await _slot_alerts(db_session, project.id)) == 1


# ── runner-deactivated escalation cadence persistence ───────────────────────


async def _runner_deactivated_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == "runner.deactivated",
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_runner_escalation_cadence_survives_restart(db_session, mocker):
    """Verify that runner-outage re-page cadence persists across orchestrator restart.

    The 1h-then-every-4h escalation cadence is tracked using alert.last_escalated_at
    so that after restart, the periodic sweep correctly respects the cadence instead of
    immediately re-paging every alert older than 1h.

    Exercises the real production path: alert is created with last_seen_at=now (set by
    alert_policy) and last_escalated_at=None (never escalated by sweep). The sweep must
    use last_escalated_at — NOT last_seen_at — to decide when to first re-page.
    """
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()

    # Create initial runner-deactivated alert (WARNING severity, idle runner).
    # alert_policy sets last_seen_at=now at creation; last_escalated_at starts None.
    created = await _maybe_create_runner_deactivated_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-01",
        had_active_work=False,
        seconds_since_last_seen=600,
    )
    assert created is True

    # Fetch the alert and verify initial state
    alerts = await _runner_deactivated_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    # Confirm production state: last_seen_at is set (by alert_policy), last_escalated_at
    # is None (sweep has not run yet). Do NOT null out last_seen_at — that state never
    # occurs in production and would mask the cadence bug this test guards against.
    assert alert.last_seen_at is not None, "alert_policy always sets last_seen_at on create"
    assert alert.last_escalated_at is None, "sweep has not run; last_escalated_at should be None"

    # Simulate time passage: set created_at to 2 hours ago so age >= 1h threshold
    now = _utcnow()
    alert.created_at = now - _timedelta(hours=2)
    db_session.add(alert)
    await db_session.commit()

    # First sweep: should dispatch notification (age >= 1h, last_escalated_at is None)
    dispatched_before = await _maybe_escalate_runner_deactivated_alerts(db_session)
    assert dispatched_before == 1, "First sweep should dispatch after 1h"

    # Refresh to get updated state — last_escalated_at should now be set
    await db_session.refresh(alert)
    first_last_escalated = alert.last_escalated_at
    assert first_last_escalated is not None, "last_escalated_at should be set after first notification"

    # Simulate orchestrator restart: clear in-memory escalation dicts
    _runner_escalation_last_notified.clear()
    _runner_outage_escalated_keys.clear()

    # Second sweep (immediately after restart): should NOT dispatch again
    # because last_escalated_at has preserved the cadence
    dispatched_after_restart = await _maybe_escalate_runner_deactivated_alerts(db_session)
    assert (
        dispatched_after_restart == 0
    ), "After restart, cadence should survive — should not re-notify within 4h window"


# ── runner-degraded escalation cadence persistence ──────────────────────────


async def _runner_degraded_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == "runner.degraded",
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _make_open_degraded_alert(db_session, project, *, runner_id, age_hours):
    """Create an open degraded alert aged ``age_hours`` in the past (created_at)."""
    created = await _maybe_create_runner_degraded_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-degraded-01",
        consecutive_failures=3,
        seconds_since_last_success=900,
    )
    assert created is True
    alerts = await _runner_degraded_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert alert.last_escalated_at is None, "sweep has not run; last_escalated_at None"
    alert.created_at = _utcnow() - _timedelta(hours=age_hours)
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)
    return alert


@pytest.mark.asyncio
async def test_runner_degraded_escalation_cadence_survives_restart(db_session, mocker):
    """Degraded re-page cadence persists across orchestrator restart.

    Mirrors ``test_runner_escalation_cadence_survives_restart`` for the degraded
    path: a degraded alert aged past the 1h first threshold re-pages on the first
    sweep and stamps ``last_escalated_at``; a second sweep — even after clearing
    in-memory escalation state (simulating a restart) — must NOT re-page again
    inside the 4h repeat window, because the cadence lives in the persisted
    ``last_escalated_at`` column, not in memory.
    """
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    # Aged 2h: past the 1h first-re-page threshold, but below the 4h severity-flip
    # threshold so severity stays WARNING (isolates the cadence behaviour).
    alert = await _make_open_degraded_alert(
        db_session, project, runner_id=runner_id, age_hours=2
    )

    dispatch.reset_mock()
    dispatched_before = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert dispatched_before == 1, "First sweep should re-page after 1h"
    assert dispatch.call_count == 1
    # Confirm the follow-up page carries the escalation marker.
    _args, _kwargs = dispatch.call_args
    assert _args[1] == "RUNNER_DEGRADED_ESCALATED"
    assert _args[2]["escalation"] is True

    await db_session.refresh(alert)
    assert alert.last_escalated_at is not None, "last_escalated_at set after re-page"
    assert alert.severity == AlertSeverity.WARNING, "under 4h: no severity flip yet"

    # Simulate orchestrator restart: the degraded path keeps NO in-memory cadence
    # state (last_escalated_at is the source of truth). Clearing the deactivated
    # in-memory dicts here documents that no degraded state depends on them.
    _runner_escalation_last_notified.clear()
    _runner_outage_escalated_keys.clear()

    dispatch.reset_mock()
    dispatched_after_restart = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert (
        dispatched_after_restart == 0
    ), "After restart, cadence should survive — no re-page inside the 4h window"
    assert dispatch.call_count == 0


@pytest.mark.asyncio
async def test_runner_degraded_escalation_dispatches_distinct_event_type(db_session, mocker):
    """The initial degraded alert dispatches RUNNER_DEGRADED, but the escalation
    re-page sweep dispatches RUNNER_DEGRADED_ESCALATED — a distinct event type
    matching the RUNNER_OUTAGE_ESCALATED / RUNNER_DEACTIVATED outage-path pattern.

    Operators can distinguish a fresh degradation from a still-degraded re-page
    and route them to different priority buckets.
    """
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()

    # 1) Initial creation → must dispatch RUNNER_DEGRADED (NOT escalated).
    dispatch.reset_mock()
    created = await _maybe_create_runner_degraded_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-degraded-02",
        consecutive_failures=3,
        seconds_since_last_success=900,
    )
    assert created is True
    assert dispatch.call_count == 1
    init_args, _init_kwargs = dispatch.call_args
    assert init_args[1] == "RUNNER_DEGRADED", (
        f"initial alert must dispatch RUNNER_DEGRADED, got {init_args[1]}"
    )
    assert init_args[2].get("escalation", None) is None

    # 2) Age the alert past the 1h re-page threshold.
    alerts = await _runner_degraded_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    alert.created_at = _utcnow() - _timedelta(hours=2)
    db_session.add(alert)
    await db_session.commit()

    # 3) Escalation sweep → must dispatch RUNNER_DEGRADED_ESCALATED.
    dispatch.reset_mock()
    dispatched = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert dispatched == 1
    assert dispatch.call_count == 1
    esc_args, _esc_kwargs = dispatch.call_args
    assert esc_args[1] == "RUNNER_DEGRADED_ESCALATED", (
        f"escalation re-page must dispatch RUNNER_DEGRADED_ESCALATED, got {esc_args[1]}"
    )
    assert esc_args[2].get("escalation") is True


@pytest.mark.asyncio
async def test_runner_degraded_escalation_flips_warning_to_error(db_session, mocker):
    """A degraded alert aged past the 4h threshold flips WARNING→ERROR and broadcasts."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    # Aged 5h: past both the 1h re-page threshold and the 4h severity threshold.
    alert = await _make_open_degraded_alert(
        db_session, project, runner_id=runner_id, age_hours=5
    )
    bcast.reset_mock()

    dispatched = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert dispatched == 1

    await db_session.refresh(alert)
    assert alert.severity == AlertSeverity.ERROR, "aged past 4h → escalate to ERROR"
    assert "escalated" in alert.title.lower()
    assert alert.occurrence_count == 2, "occurrence bumped on re-page"

    # Severity flip must broadcast an alert_updated so the operator UI reflects it.
    updated_calls = [
        c for c in bcast.await_args_list if c.args[1] == "alert_updated"
    ]
    assert updated_calls, "flip must broadcast alert_updated"
    assert updated_calls[-1].args[2]["severity"] == AlertSeverity.ERROR.value


@pytest.mark.asyncio
async def test_runner_degraded_escalation_suppressed_when_acknowledged(db_session, mocker):
    """An acknowledged degraded alert is never re-paged or escalated."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    # Aged 6h: would both re-page and flip if not acknowledged.
    alert = await _make_open_degraded_alert(
        db_session, project, runner_id=runner_id, age_hours=6
    )
    alert.acknowledged_at = _utcnow()
    db_session.add(alert)
    await db_session.commit()

    dispatch.reset_mock()
    dispatched = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert dispatched == 0, "acknowledged alert must not re-page"
    assert dispatch.call_count == 0

    await db_session.refresh(alert)
    assert alert.severity == AlertSeverity.WARNING, "acknowledged alert must not flip"
    assert alert.last_escalated_at is None


@pytest.mark.asyncio
async def test_runner_degraded_escalation_noop_when_resolved(db_session, mocker):
    """A recovered (dismissed) degraded alert is not escalated — flap suppression."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    await _make_open_degraded_alert(
        db_session, project, runner_id=runner_id, age_hours=6
    )
    # Runner recovers: the degraded alert is dismissed.
    resolved = await _resolve_runner_degraded_alert(db_session, project.id, runner_id)
    assert resolved == 1

    dispatch.reset_mock()
    dispatched = await _maybe_escalate_runner_degraded_alerts(db_session)
    assert dispatched == 0, "dismissed alert must not escalate"
    assert dispatch.call_count == 0


@pytest.mark.asyncio
async def test_runner_degraded_escalation_via_check_degraded_runners(db_session, mocker):
    """Integration: a still-degraded runner re-pages through _check_degraded_runners."""
    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-degraded-esc",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=3,
        capabilities={"task_types": "all"},
    )
    db_session.add(runner)
    await db_session.commit()

    # First sweep creates the WARNING degraded alert.
    await _check_degraded_runners(db_session)
    alerts = await _runner_degraded_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]

    # Age it past the 1h re-page threshold.
    alert.created_at = _utcnow() - _timedelta(hours=2)
    db_session.add(alert)
    await db_session.commit()

    dispatch.reset_mock()
    # Runner still degraded → sweep re-pages via the escalation step.
    await _check_degraded_runners(db_session)
    escalation_calls = [
        c for c in dispatch.call_args_list
        if c.args[1] == "RUNNER_DEGRADED_ESCALATED" and c.args[2].get("escalation") is True
    ]
    assert escalation_calls, "still-degraded runner should re-page through the sweep"

    await db_session.refresh(alert)
    assert alert.last_escalated_at is not None


# ── Fleet-level no-healthy-runner aggregate alert ────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE,
    _maybe_create_fleet_no_healthy_runner_alert,
    _resolve_fleet_no_healthy_runner_alert,
)


async def _fleet_no_healthy_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_alert_creates_and_broadcasts(db_session, mocker):
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = await _maybe_create_fleet_no_healthy_runner_alert(
        db_session,
        project_id=project.id,
        offline_count=2,
        degraded_count=1,
    )

    assert created is True
    alerts = await _fleet_no_healthy_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.ERROR
    assert "offline" in alert.message
    assert "degraded" in alert.message
    assert "2 offline" in alert.message
    assert "1 degraded" in alert.message
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_alert_deduped(db_session, mocker):
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Three sweep passes must produce exactly one alert row.
    for _ in range(3):
        await _maybe_create_fleet_no_healthy_runner_alert(
            db_session,
            project_id=project.id,
            offline_count=1,
            degraded_count=0,
        )

    alerts = await _fleet_no_healthy_alerts(db_session, project.id)
    assert len(alerts) == 1, "repeated sweep passes must dedup to one alert"


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_alert_auto_resolves_on_recovery(db_session, mocker):
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # All runners offline → fleet alert fires.
    created = await _maybe_create_fleet_no_healthy_runner_alert(
        db_session,
        project_id=project.id,
        offline_count=1,
        degraded_count=0,
    )
    assert created is True

    alerts = await _fleet_no_healthy_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    # A runner recovers → alert is resolved.
    resolved = await _resolve_fleet_no_healthy_runner_alert(db_session, project.id)
    assert resolved == 1

    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "alert must be dismissed once a runner recovers"

    # Broadcast: once for create, once for dismiss.
    assert bcast.await_count == 2


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_resolve_is_idempotent(db_session, mocker):
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_fleet_no_healthy_runner_alert(
        db_session, project_id=project.id, offline_count=1, degraded_count=0
    )

    first = await _resolve_fleet_no_healthy_runner_alert(db_session, project.id)
    second = await _resolve_fleet_no_healthy_runner_alert(db_session, project.id)

    assert first == 1
    assert second == 0, "second resolve is a no-op — alert already dismissed"


# ── Low-disk runner alert ─────────────────────────────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _RUNNER_LOW_DISK_EVENT_TYPE,
    _maybe_create_runner_low_disk_alert,
    _resolve_runner_low_disk_alert,
)


async def _low_disk_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _RUNNER_LOW_DISK_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_runner_low_disk_alert_creates_and_broadcasts(db_session, mocker):
    """A runner below the disk floor raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold = 1_073_741_824  # 1 GiB
    disk_free = 500_000_000   # 500 MB — below threshold

    created = await _maybe_create_runner_low_disk_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-disk-01",
        disk_free_bytes=disk_free,
        threshold_bytes=threshold,
    )

    assert created is True
    alerts = await _low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "low disk" in alert.title.lower()
    assert "0.5" in alert.message  # 500 MB free ≈ 0.5 GB
    assert "1.0" in alert.message  # 1.0 GB threshold
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_low_disk_alert_deduped(db_session, mocker):
    """Repeated sweep passes create exactly one alert row."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    for _ in range(3):
        await _maybe_create_runner_low_disk_alert(
            db_session,
            project_id=project.id,
            runner_id=runner_id,
            hostname="runner-disk-01",
            disk_free_bytes=100_000_000,
            threshold_bytes=1_073_741_824,
        )

    alerts = await _low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1, "repeated sweep passes must dedup to one alert"


@pytest.mark.asyncio
async def test_runner_low_disk_alert_auto_resolves_on_recovery(db_session, mocker):
    """Alert fires when disk is low and auto-resolves when disk recovers."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold = 1_073_741_824  # 1 GiB

    # Runner reports low disk → alert fires.
    created = await _maybe_create_runner_low_disk_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-disk-01",
        disk_free_bytes=200_000_000,
        threshold_bytes=threshold,
    )
    assert created is True
    alerts = await _low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    # Runner frees disk → alert resolves.
    resolved = await _resolve_runner_low_disk_alert(db_session, project.id, runner_id)
    assert resolved == 1

    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "alert must dismiss once disk recovers"

    # Broadcast: once for create, once for dismiss.
    assert bcast.await_count == 2


@pytest.mark.asyncio
async def test_low_disk_runner_check_integration(db_session, mocker):
    """_check_degraded_runners raises a low-disk alert and auto-resolves on recovery."""
    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active runner reporting low disk (100 MB — well below the 1 GiB default threshold).
    runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-low-disk",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=0,
        capabilities={"task_types": "all", "disk_free_bytes": 100_000_000},
    )
    db_session.add(runner)
    await db_session.commit()

    # Sweep → low-disk alert must fire.
    await _check_degraded_runners(db_session)
    alerts = await _low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].dismissed_at is None

    # Second sweep → deduplication (no second alert).
    await _check_degraded_runners(db_session)
    alerts = await _low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1

    # Runner frees disk (5 GiB — above the 1 GiB threshold).
    await db_session.refresh(runner)
    caps = dict(runner.capabilities or {})
    caps["disk_free_bytes"] = 5_368_709_120  # 5 GiB
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    # Sweep again → alert must be dismissed.
    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "low-disk alert must auto-resolve on recovery"


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_check_integration(db_session, mocker):
    """_check_degraded_runners raises a fleet alert when all runners are unhealthy
    and auto-resolves it once a healthy runner is present.
    """
    from datetime import timedelta

    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One runner that is offline (inactive, never heartbeated recently).
    offline_runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-offline",
        status="inactive",
        last_seen_at=_utcnow() - timedelta(hours=2),
        capabilities={},
    )
    db_session.add(offline_runner)
    await db_session.commit()

    # Sweep → fleet alert must fire (sole runner is offline).
    await _check_degraded_runners(db_session)

    alerts = await _fleet_no_healthy_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].dismissed_at is None

    # Second sweep → deduplication (no second alert).
    await _check_degraded_runners(db_session)
    alerts = await _fleet_no_healthy_alerts(db_session, project.id)
    assert len(alerts) == 1

    # Now add a healthy runner (active, fresh heartbeat, no failures).
    healthy_runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-healthy",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=0,
        capabilities={"task_types": "all"},
    )
    db_session.add(healthy_runner)
    await db_session.commit()

    # Sweep again → fleet alert must be dismissed because a healthy runner exists.
    await _check_degraded_runners(db_session)

    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "fleet alert must auto-resolve when healthy runner appears"


@pytest.mark.asyncio
async def test_fleet_no_healthy_runner_alert_resolves_on_binding_removal(db_session, mocker):
    """Fleet-no-healthy alert resolves when the runner binding to a project is removed.

    A bound project that loses its last runner binding disappears from
    projects_with_runners, so neither the create loop nor the healthy-resolve loop
    touches it. The monitor must still dismiss the open alert so it does not stay
    permanently open after the binding is removed.
    """
    from datetime import timedelta

    from app.models import LocalRunner, RunnerProjectBinding
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    enrollment = make_project()
    bound = make_project()
    db_session.add(enrollment)
    db_session.add(bound)
    await db_session.commit()
    await db_session.refresh(enrollment)
    await db_session.refresh(bound)

    # Runner enrolled in enrollment project, bound to the bound project.
    runner = LocalRunner(
        project_id=enrollment.id,
        token_hash="hash-shared-offline",
        status="inactive",
        last_seen_at=_utcnow() - timedelta(hours=2),
        capabilities={},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    binding = RunnerProjectBinding(runner_id=runner.id, project_id=bound.id)
    db_session.add(binding)
    await db_session.commit()

    # Sweep → fleet alert fires for the bound project (sole runner is offline).
    await _check_degraded_runners(db_session)
    alerts = await _fleet_no_healthy_alerts(db_session, bound.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    # Remove the binding so the bound project has no runners at all.
    await db_session.delete(binding)
    await db_session.commit()

    # Sweep again → alert must be dismissed now that the binding is gone.
    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, (
        "fleet alert must auto-resolve when the runner binding to a project is removed"
    )


# ── local-runner hold alert external notification dispatch ────────────────────


async def _hold_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title == _LOCAL_RUNNER_HOLD_ALERT_TITLE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_local_runner_hold_alert_dispatches_external_notification(
    db_session, mocker
):
    """_maybe_create_local_runner_hold_alert dispatches via the external notification path.

    All other runner health alerts (RUNNER_DEACTIVATED, RUNNER_DEGRADED,
    FLEET_NO_HEALTHY, RUNNER_LOW_DISK) dispatch through the retry-capable
    external notification path. The hold alert previously only broadcast to
    the WebSocket UI — this test guards that it now also pages operators
    through their configured notification channels.
    """
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, title="compute-experiment-task")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    created = await _maybe_create_local_runner_hold_alert(
        task,
        db_session,
        held_seconds=900,
        timeout_seconds=21600,
    )
    await db_session.commit()

    assert created is True

    # Verify the alert row was created with the expected fields.
    alerts = await _hold_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].task_id == task.id
    assert "900s" in alerts[0].message or "15m" in alerts[0].message

    # Fire-and-forget via ensure_future — let the scheduled task run.
    import asyncio

    await asyncio.sleep(0)
    dispatch.assert_awaited_once()
    args, _kwargs = dispatch.call_args
    assert args[1] == "RUNNER_LOCAL_HOLD"
    assert args[2]["task_id"] == str(task.id)
    assert args[2]["held_seconds"] == 900
    assert args[2]["timeout_seconds"] == 21600


@pytest.mark.asyncio
async def test_local_runner_hold_alert_dedups_same_task(db_session, mocker):
    """Repeated sweep passes for the same held task do not create duplicate alerts."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    first = await _maybe_create_local_runner_hold_alert(
        task, db_session, held_seconds=900, timeout_seconds=21600
    )
    await db_session.commit()
    assert first is True

    second = await _maybe_create_local_runner_hold_alert(
        task, db_session, held_seconds=1200, timeout_seconds=21600
    )
    await db_session.commit()
    assert second is False  # deduped on (task_id, title, dismissed_at IS NULL)

    alerts = await _hold_alerts(db_session, project.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_local_runner_hold_alert_handles_notification_failure(
    db_session, mocker
):
    """Alert is still created when the notification dispatch raises.

    The fire-and-forget dispatch_external_notification call is best-effort;
    a failure to page the operator must not prevent the alert row from being
    written — the WebSocket broadcast and the alert row are the primary
    reliability path.
    """
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        side_effect=RuntimeError("webhook unreachable"),
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, title="failing-notify-task")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    created = await _maybe_create_local_runner_hold_alert(
        task,
        db_session,
        held_seconds=600,
        timeout_seconds=21600,
    )
    await db_session.commit()

    # Alert MUST still be created even though the notification failed.
    assert created is True

    alerts = await _hold_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].task_id == task.id


# ── Runner idle-GPU alert ────────────────────────────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _RUNNER_IDLE_GPU_EVENT_TYPE,
    _maybe_create_runner_idle_gpu_alert,
    _resolve_runner_idle_gpu_alert,
)


async def _idle_gpu_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _RUNNER_IDLE_GPU_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_runner_idle_gpu_alert_creates_and_broadcasts(db_session, mocker):
    """A GPU runner below the utilization floor raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold_pct = 10
    gpu_utilization_pct = 2.0  # 2% — well below idle threshold

    created = await _maybe_create_runner_idle_gpu_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="gpu-runner-01",
        gpu_utilization_pct=gpu_utilization_pct,
        threshold_pct=threshold_pct,
    )

    assert created is True
    alerts = await _idle_gpu_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "idle" in alert.title.lower()
    assert "2%" in alert.message  # 2% utilization
    assert "10%" in alert.message  # 10% threshold
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_idle_gpu_alert_deduped(db_session, mocker):
    """Repeated sweep passes create exactly one alert row."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    for _ in range(3):
        await _maybe_create_runner_idle_gpu_alert(
            db_session,
            project_id=project.id,
            runner_id=runner_id,
            hostname="gpu-runner-01",
            gpu_utilization_pct=2.0,
            threshold_pct=10,
        )

    alerts = await _idle_gpu_alerts(db_session, project.id)
    assert len(alerts) == 1, "repeated sweep passes must dedup to one alert"


@pytest.mark.asyncio
async def test_runner_idle_gpu_alert_auto_resolves_on_recovery(db_session, mocker):
    """Alert fires when GPU is idle and auto-resolves when GPU utilization recovers."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold_pct = 10

    # Runner reports idle GPU → alert fires.
    created = await _maybe_create_runner_idle_gpu_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="gpu-runner-01",
        gpu_utilization_pct=2.0,
        threshold_pct=threshold_pct,
    )
    assert created is True
    alerts = await _idle_gpu_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    # GPU utilization recovers → alert resolves.
    resolved = await _resolve_runner_idle_gpu_alert(db_session, project.id, runner_id)
    assert resolved == 1

    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "alert must dismiss once GPU utilization recovers"

    # Broadcast: once for create, once for dismiss.
    assert bcast.await_count == 2


@pytest.mark.asyncio
async def test_idle_gpu_runner_check_integration(db_session, mocker):
    """_check_degraded_runners raises an idle-GPU alert and auto-resolves on recovery."""
    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active GPU runner reporting idle utilization (2% — well below the 10% default threshold).
    runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-idle-gpu",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=0,
        capabilities={"gpu_utilization_pct": 2},
    )
    db_session.add(runner)
    await db_session.commit()

    # Sweep → idle-GPU alert must fire.
    await _check_degraded_runners(db_session)
    alerts = await _idle_gpu_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].dismissed_at is None

    # Second sweep → deduplication (no second alert).
    await _check_degraded_runners(db_session)
    alerts = await _idle_gpu_alerts(db_session, project.id)
    assert len(alerts) == 1

    # Runner GPU utilization recovers (80% — above the 10% threshold).
    await db_session.refresh(runner)
    caps = dict(runner.capabilities or {})
    caps["gpu_utilization_pct"] = 80
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    # Sweep again → alert must be dismissed.
    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "idle-GPU alert must auto-resolve on recovery"


# ── Runner token-stale alert ──────────────────────────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _RUNNER_TOKEN_STALE_EVENT_TYPE,
    _maybe_create_runner_token_stale_alert,
)


async def _stale_token_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _RUNNER_TOKEN_STALE_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


# ── Low-memory runner alert ────────────────────────────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _RUNNER_LOW_MEMORY_EVENT_TYPE,
    _maybe_create_runner_low_memory_alert,
    _resolve_runner_low_memory_alert,
)


async def _low_memory_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _RUNNER_LOW_MEMORY_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_runner_stale_token_alert_creates_and_resolves_via_sweep(db_session, mocker):
    """_check_degraded_runners raises a token-stale WARNING alert and auto-resolves on rotation."""
    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active runner with a stale token (>90 days).
    runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-stale-token",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=0,
        token_created_at=_utcnow() - _timedelta(days=100),
    )
    db_session.add(runner)
    await db_session.commit()

    # Sweep → token-stale alert must fire.
    await _check_degraded_runners(db_session)
    alerts = await _stale_token_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].dismissed_at is None

    # Second sweep → deduplication (no second alert).
    await _check_degraded_runners(db_session)
    alerts = await _stale_token_alerts(db_session, project.id)
    assert len(alerts) == 1

    # Runner rotates token (token_created_at updated to now).
    await db_session.refresh(runner)
    runner.token_created_at = _utcnow() - _timedelta(days=1)  # 1 day old — well within threshold
    db_session.add(runner)
    await db_session.commit()

    # Sweep again → alert must be dismissed.
    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "stale-token alert must auto-resolve on token rotation"


@pytest.mark.asyncio
async def test_runner_stale_token_alert_creates_and_broadcasts(db_session, mocker):
    """A runner with an old token raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    token_age_days = 100
    threshold_days = 90

    created = await _maybe_create_runner_token_stale_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="old-token-runner",
        token_age_days=token_age_days,
        threshold_days=threshold_days,
    )

    assert created is True
    alerts = await _stale_token_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "stale" in alert.title.lower()
    assert "100" in alert.message
    bcast.assert_awaited_once()


# ── Low-memory runner alert tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runner_low_memory_alert_creates_and_broadcasts(db_session, mocker):
    """A runner below the memory floor raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold = 1_073_741_824  # 1 GiB
    mem_free = 100_000_000     # ~95 MB — below threshold

    created = await _maybe_create_runner_low_memory_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-mem-01",
        memory_free_bytes=mem_free,
        threshold_bytes=threshold,
    )

    assert created is True
    alerts = await _low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "low memory" in alert.title.lower()
    assert "95" in alert.message  # ~95 MB free
    assert "1024" in alert.message  # 1.0 GiB = 1024 MB threshold
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_low_memory_alert_deduped(db_session, mocker):
    """Repeated sweep passes create exactly one alert row."""
    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    for _ in range(3):
        await _maybe_create_runner_low_memory_alert(
            db_session,
            project_id=project.id,
            runner_id=runner_id,
            hostname="runner-mem-01",
            memory_free_bytes=100_000_000,
            threshold_bytes=1_073_741_824,
        )

    alerts = await _low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1, "repeated sweep passes must dedup to one alert"


@pytest.mark.asyncio
async def test_runner_low_memory_alert_auto_resolves_on_recovery(db_session, mocker):
    """Alert fires when memory is low and auto-resolves when memory recovers."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    threshold = 1_073_741_824  # 1 GiB

    # Runner reports low memory → alert fires.
    created = await _maybe_create_runner_low_memory_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-mem-01",
        memory_free_bytes=100_000_000,
        threshold_bytes=threshold,
    )
    assert created is True
    alerts = await _low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    # Memory recovers → alert resolves.
    resolved = await _resolve_runner_low_memory_alert(db_session, project.id, runner_id)
    assert resolved == 1

    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "alert must dismiss once memory recovers"

    # Broadcast: once for create, once for dismiss.
    assert bcast.await_count == 2


@pytest.mark.asyncio
async def test_low_memory_runner_check_integration(db_session, mocker):
    """_check_degraded_runners raises a low-memory alert and auto-resolves on recovery."""
    from app.models import LocalRunner
    from app.orchestrator_runner_dispatch import _check_degraded_runners
    from app.time_utils import _utcnow

    mocker.patch("app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active runner reporting low memory (50 MB — well below the 512 MiB default threshold).
    runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-low-mem",
        status="active",
        last_seen_at=_utcnow(),
        consecutive_job_failures=0,
        capabilities={"memory_free_bytes": 50_000_000},
    )
    db_session.add(runner)
    await db_session.commit()

    # Sweep → low-memory alert must fire.
    await _check_degraded_runners(db_session)
    alerts = await _low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].dismissed_at is None

    # Second sweep → deduplication (no second alert).
    await _check_degraded_runners(db_session)
    alerts = await _low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1

    # Runner frees memory (2 GiB — above the 512 MiB threshold).
    await db_session.refresh(runner)
    caps = dict(runner.capabilities or {})
    caps["memory_free_bytes"] = 2_147_483_648  # 2 GiB
    runner.capabilities = caps
    db_session.add(runner)
    await db_session.commit()

    # Sweep again → alert must be dismissed.
    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "low-memory alert must auto-resolve on recovery"


# ---------------------------------------------------------------------------
# Repeated cross-project runner access alert
# ---------------------------------------------------------------------------

from app.alert_dispatch import (  # noqa: E402
    _RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE,
    _RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD,
    _RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS,
    _maybe_create_runner_cross_project_access_alert,
    _record_runner_cross_project_mismatch,
    _resolve_runner_cross_project_access_alert,
    _runner_cross_project_mismatch_timestamps,
)


async def _cross_project_access_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_repeated_cross_project_access_raises_one_error_alert(db_session, mocker):
    broadcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    dispatch = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    target_project_id = _uuid_module.uuid4()
    _runner_cross_project_mismatch_timestamps.pop(str(runner_id), None)

    for _ in range(_RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD - 1):
        should_alert, _count = _record_runner_cross_project_mismatch(runner_id)
        assert should_alert is False

    should_alert, count = _record_runner_cross_project_mismatch(runner_id)
    assert should_alert is True
    created = await _maybe_create_runner_cross_project_access_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-cross-01",
        mismatch_count=count,
        attempt_project_id=target_project_id,
    )
    assert created is True
    alerts = await _cross_project_access_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    broadcast.assert_awaited_once()
    dispatch.assert_called_once()

    broadcast.reset_mock()
    dispatch.reset_mock()
    _record_runner_cross_project_mismatch(runner_id)
    created_again = await _maybe_create_runner_cross_project_access_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-cross-01",
        mismatch_count=count + 1,
        attempt_project_id=target_project_id,
    )
    assert created_again is False
    assert len(await _cross_project_access_alerts(db_session, project.id)) == 1
    broadcast.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_cross_project_access_prunes_expired_timestamps():
    from datetime import timedelta

    runner_id = _uuid_module.uuid4()
    _runner_cross_project_mismatch_timestamps.pop(str(runner_id), None)
    base = _utcnow()
    for offset in range(_RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD):
        _record_runner_cross_project_mismatch(
            runner_id, now=base + timedelta(seconds=offset)
        )

    should_alert, count = _record_runner_cross_project_mismatch(
        runner_id,
        now=base
        + timedelta(
            seconds=(
                _RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS
                + _RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD
            )
        ),
    )
    assert should_alert is False
    assert count == 1
    _runner_cross_project_mismatch_timestamps.pop(str(runner_id), None)


@pytest.mark.asyncio
async def test_cross_project_access_alert_resolves(db_session, mocker):
    broadcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_id = _uuid_module.uuid4()
    _runner_cross_project_mismatch_timestamps[str(runner_id)] = [_utcnow()]
    await _maybe_create_runner_cross_project_access_alert(
        db_session,
        project_id=project.id,
        runner_id=runner_id,
        hostname="runner-cross-02",
        mismatch_count=_RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD,
    )
    alerts = await _cross_project_access_alerts(db_session, project.id)
    assert len(alerts) == 1
    broadcast.reset_mock()

    dismissed = await _resolve_runner_cross_project_access_alert(
        db_session, project.id, runner_id
    )
    assert dismissed == 1
    assert str(runner_id) not in _runner_cross_project_mismatch_timestamps
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None
    broadcast.assert_awaited_once()


# ── GCE task-VM low-disk / low-memory alerts (warm-pool VM monitoring parity) ──

from app.alert_dispatch import (  # noqa: E402
    _VM_LOW_DISK_EVENT_TYPE,
    _VM_LOW_MEMORY_EVENT_TYPE,
    _maybe_create_vm_low_disk_alert,
    _maybe_create_vm_low_memory_alert,
    _resolve_vm_low_memory_alert,
)
from app.models import TaskStatus as _TaskStatus  # noqa: E402


async def _vm_low_disk_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _VM_LOW_DISK_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _vm_low_memory_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == _VM_LOW_MEMORY_EVENT_TYPE,
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_vm_low_disk_alert_creates_and_broadcasts(db_session, mocker):
    """A GCE task VM below the disk floor raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = await _maybe_create_vm_low_disk_alert(
        db_session,
        project_id=project.id,
        vm_instance_id="agent-vm-deadbeef",
        task_id=_uuid_module.uuid4(),
        disk_free_bytes=500_000_000,  # ≈ 0.5 GB — below 1 GiB threshold
        threshold_bytes=1_073_741_824,
    )

    assert created is True
    alerts = await _vm_low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "low disk" in alert.title.lower()
    assert "agent-vm-deadbeef" in alert.title
    assert "0.5" in alert.message  # 500 MB free ≈ 0.5 GB
    assert "1.0" in alert.message  # 1.0 GB threshold
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_vm_low_memory_alert_creates_and_broadcasts(db_session, mocker):
    """A GCE task VM approaching OOM raises one WARNING alert and broadcasts it."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = await _maybe_create_vm_low_memory_alert(
        db_session,
        project_id=project.id,
        vm_instance_id="agent-vm-cafef00d",
        task_id=_uuid_module.uuid4(),
        memory_free_bytes=100 * 1024 * 1024,  # 100 MB — below 512 MB threshold
        threshold_bytes=536_870_912,
    )

    assert created is True
    alerts = await _vm_low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "low memory" in alert.title.lower()
    assert "agent-vm-cafef00d" in alert.title
    assert "100 MB" in alert.message  # free memory
    assert "512 MB" in alert.message  # threshold
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_vm_low_memory_alert_deduped(db_session, mocker):
    """Repeated sweep passes create exactly one VM low-memory alert row."""
    mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        await _maybe_create_vm_low_memory_alert(
            db_session,
            project_id=project.id,
            vm_instance_id="agent-vm-dedupe01",
            memory_free_bytes=50 * 1024 * 1024,
            threshold_bytes=536_870_912,
        )

    alerts = await _vm_low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1, "repeated sweep passes must dedup to one alert"


@pytest.mark.asyncio
async def test_vm_low_memory_alert_auto_resolves(db_session, mocker):
    """VM low-memory alert fires then auto-resolves when memory recovers."""
    bcast = mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_vm_low_memory_alert(
        db_session,
        project_id=project.id,
        vm_instance_id="agent-vm-recover1",
        memory_free_bytes=10 * 1024 * 1024,
        threshold_bytes=536_870_912,
    )
    alerts = await _vm_low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].dismissed_at is None

    resolved = await _resolve_vm_low_memory_alert(
        db_session, project.id, "agent-vm-recover1"
    )
    assert resolved == 1
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None
    assert bcast.await_count == 2  # create + dismiss


@pytest.mark.asyncio
async def test_low_memory_vms_check_integration(db_session, mocker):
    """_check_degraded_runners raises a VM OOM alert from heartbeat metrics and
    auto-resolves it once the task completes; runner-pinned and terminal-status
    tasks are ignored.
    """
    from app.orchestrator_runner_dispatch import _check_degraded_runners

    mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active agent VM near OOM: cgroup 7900/8192 MB → ~292 MB headroom < 512 MB.
    oom_task = make_task(
        project_id=project.id,
        status=_TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-oom0001",
        last_metrics_json={"cgroup_mb": 7900.0, "cgroup_limit_mb": 8192.0},
    )
    # Runner-pinned task with low headroom — must be ignored (runner sweeps own it).
    runner_task = make_task(
        project_id=project.id,
        status=_TaskStatus.IN_PROGRESS,
        vm_instance_id="runner:abc123",
        last_metrics_json={"cgroup_mb": 7900.0, "cgroup_limit_mb": 8192.0},
    )
    # Terminal task on an agent VM — must be ignored (no longer running).
    done_task = make_task(
        project_id=project.id,
        status=_TaskStatus.COMPLETED,
        vm_instance_id="agent-vm-done001",
        last_metrics_json={"cgroup_mb": 7900.0, "cgroup_limit_mb": 8192.0},
    )
    # Healthy agent VM with plenty of headroom — must NOT alert.
    healthy_task = make_task(
        project_id=project.id,
        status=_TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-healthy1",
        last_metrics_json={"cgroup_mb": 1000.0, "cgroup_limit_mb": 8192.0},
    )
    db_session.add_all([oom_task, runner_task, done_task, healthy_task])
    await db_session.commit()

    await _check_degraded_runners(db_session)
    alerts = await _vm_low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1, "exactly one VM (the agent OOM VM) should alert"
    assert "agent-vm-oom0001" in alerts[0].title
    assert alerts[0].dismissed_at is None

    # Second sweep → dedup.
    await _check_degraded_runners(db_session)
    alerts = await _vm_low_memory_alerts(db_session, project.id)
    assert len(alerts) == 1

    # The task completes → sweep must auto-resolve the alert.
    await db_session.refresh(oom_task)
    oom_task.status = _TaskStatus.COMPLETED
    db_session.add(oom_task)
    await db_session.commit()

    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, (
        "VM OOM alert must auto-resolve once the task leaves an active state"
    )


@pytest.mark.asyncio
async def test_low_disk_vms_check_integration(db_session, mocker):
    """_check_degraded_runners raises a VM low-disk alert from heartbeat metrics
    and auto-resolves it on recovery.
    """
    from app.orchestrator_runner_dispatch import _check_degraded_runners

    mocker.patch(
        "app.runner_health_alerts.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Active agent VM reporting 200 MB free disk — below the 1 GiB default floor.
    disk_task = make_task(
        project_id=project.id,
        status=_TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-disk001",
        last_metrics_json={"disk_free_mb": 200.0, "disk_total_mb": 50_000.0},
    )
    db_session.add(disk_task)
    await db_session.commit()

    await _check_degraded_runners(db_session)
    alerts = await _vm_low_disk_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert "agent-vm-disk001" in alerts[0].title
    assert alerts[0].dismissed_at is None

    # VM frees disk (10 GB free) → sweep auto-resolves.
    await db_session.refresh(disk_task)
    disk_task.last_metrics_json = {"disk_free_mb": 10_240.0, "disk_total_mb": 50_000.0}
    db_session.add(disk_task)
    await db_session.commit()

    await _check_degraded_runners(db_session)
    await db_session.refresh(alerts[0])
    assert alerts[0].dismissed_at is not None, "low-disk VM alert must auto-resolve on recovery"


# ── Wake-delivery stall alert ─────────────────────────────────────────────────

from app.alert_dispatch import (  # noqa: E402
    _maybe_create_wake_delivery_stall_alert,
    _resolve_wake_delivery_stall_alert,
)


async def _wake_delivery_stall_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == "deployment.wake_delivery_stalled",
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_wake_delivery_stall_alert_stamps_correct_event_type(db_session, mocker):
    """_maybe_create_wake_delivery_stall_alert must stamp event_type='deployment.wake_delivery_stalled'."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = await _maybe_create_wake_delivery_stall_alert(
        db_session, project.id, stall_cycles=3
    )
    await db_session.commit()

    assert created == 1
    alerts = await _wake_delivery_stall_alerts(db_session, project.id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.event_type == "deployment.wake_delivery_stalled"
    assert alert.severity == AlertSeverity.WARNING
    assert "3" in alert.message


@pytest.mark.asyncio
async def test_wake_delivery_stall_alert_dedupes_on_stable_title(db_session, mocker):
    """A sustained stall must raise exactly ONE alert per project across reaper ticks."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await _maybe_create_wake_delivery_stall_alert(
        db_session, project.id, stall_cycles=1
    )
    await db_session.commit()
    assert first == 1

    second = await _maybe_create_wake_delivery_stall_alert(
        db_session, project.id, stall_cycles=2
    )
    await db_session.commit()
    assert second == 0, "duplicate stall tick must not create a second alert"
    assert len(await _wake_delivery_stall_alerts(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_wake_delivery_stall_alert_broadcasts_on_create(db_session, mocker):
    """Alert creation must broadcast alert_created to the project channel."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_wake_delivery_stall_alert(
        db_session, project.id, stall_cycles=1
    )
    await db_session.commit()

    bcast.assert_awaited_once()
    call_args = bcast.call_args
    assert call_args.args[1] == "alert_created"


@pytest.mark.asyncio
async def test_wake_delivery_stall_alert_resolves_on_recovery(db_session, mocker):
    """_resolve_wake_delivery_stall_alert dismisses the open stall alert."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _maybe_create_wake_delivery_stall_alert(
        db_session, project.id, stall_cycles=1
    )
    await db_session.commit()

    resolved = await _resolve_wake_delivery_stall_alert(db_session, project.id)
    assert resolved == 1

    alerts = await _wake_delivery_stall_alerts(db_session, project.id)
    assert alerts[0].dismissed_at is not None, "stall alert must be dismissed on recovery"
    assert bcast.await_count == 2  # alert_created + alert_dismissed


# ── module-finding-chain escalation alert (concern-scoped dedup) ────────────
#
# The module-finding-chain reason dedups on the concern's identity + occurrence
# count (via ``dedupe_scope``), NOT the PR number embedded in the default
# title, because its evidence is per-CONCERN: the same concern can resurface
# across many PR numbers with no new merged occurrence, and one PR can raise
# more than one distinct concern past threshold.


async def _module_finding_chain_alerts(db_session, task_id):
    stmt = sql_select(Alert).where(
        Alert.task_id == task_id,
        Alert.title.contains("recurring module finding"),
        Alert.dismissed_at.is_(None),
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _make_module_finding_chain_task(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(project_id=project.id, title="Chain-original task")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_module_finding_chain_alert_reentry_on_same_pr_is_suppressed(
    db_session, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    task = await _make_module_finding_chain_task(db_session)

    for pr_number in (101, 101, 101):
        await _maybe_create_escalation_alert(
            task,
            db_session,
            pr_number=pr_number,
            reason="module-finding-chain",
            iteration=3,
            detail="Recurring concern in backend/app/example.py.",
            dedupe_scope="backend/app/example.py (finding abc12345, occurrence 3)",
        )
        await db_session.commit()

    alerts = await _module_finding_chain_alerts(db_session, task.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_module_finding_chain_alert_new_pr_same_occurrence_count_is_suppressed(
    db_session, mocker
):
    """A NEW PR number re-raising the SAME concern with NO new merged
    occurrence must not spam a second alert -- the dedup key is the concern's
    identity + occurrence count, not the PR number."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    task = await _make_module_finding_chain_task(db_session)

    for pr_number in (101, 102, 103):
        await _maybe_create_escalation_alert(
            task,
            db_session,
            pr_number=pr_number,
            reason="module-finding-chain",
            iteration=3,
            detail="Recurring concern in backend/app/example.py.",
            dedupe_scope="backend/app/example.py (finding abc12345, occurrence 3)",
        )
        await db_session.commit()

    alerts = await _module_finding_chain_alerts(db_session, task.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_module_finding_chain_alert_new_merged_occurrence_alerts_again(
    db_session, mocker
):
    """A genuinely NEW merged occurrence (verified_occurrences increases) of the
    same concern must raise a fresh alert, not be swallowed by dedup."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    task = await _make_module_finding_chain_task(db_session)

    await _maybe_create_escalation_alert(
        task,
        db_session,
        pr_number=101,
        reason="module-finding-chain",
        iteration=3,
        detail="Recurring concern in backend/app/example.py.",
        dedupe_scope="backend/app/example.py (finding abc12345, occurrence 3)",
    )
    await db_session.commit()

    await _maybe_create_escalation_alert(
        task,
        db_session,
        pr_number=104,
        reason="module-finding-chain",
        iteration=4,
        detail="Recurring concern in backend/app/example.py.",
        dedupe_scope="backend/app/example.py (finding abc12345, occurrence 4)",
    )
    await db_session.commit()

    alerts = await _module_finding_chain_alerts(db_session, task.id)
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_module_finding_chain_alert_two_distinct_concerns_on_same_pr_both_alert(
    db_session, mocker
):
    """Two distinct chains past threshold, both raised on the same PR, must
    both surface -- a second concern must not be swallowed by the first."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    task = await _make_module_finding_chain_task(db_session)

    await _maybe_create_escalation_alert(
        task,
        db_session,
        pr_number=101,
        reason="module-finding-chain",
        iteration=3,
        detail="Recurring concern in backend/app/one.py.",
        dedupe_scope="backend/app/one.py (finding aaaa1111, occurrence 3)",
    )
    await db_session.commit()

    await _maybe_create_escalation_alert(
        task,
        db_session,
        pr_number=101,
        reason="module-finding-chain",
        iteration=3,
        detail="Recurring concern in backend/app/two.py.",
        dedupe_scope="backend/app/two.py (finding bbbb2222, occurrence 3)",
    )
    await db_session.commit()

    alerts = await _module_finding_chain_alerts(db_session, task.id)
    assert len(alerts) == 2
    titles = {alert.title for alert in alerts}
    assert len(titles) == 2  # distinct titles -- not just distinct rows


# ── sweep_resolve_terminal_task_alerts (stale "task is stuck" reconcile) ──────
#
# Every ``task.blocked`` emitter asserts an in-flight condition. Measured on the
# live feed 2026-08-21: of 20 tasks holding an open ``task.blocked`` alert, 14
# were already terminal and exactly one was actually BLOCKED. Nothing retired
# them on the termination path.


async def _open_stuck_alerts(db_session, project_id):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == "task.blocked",
        Alert.dismissed_at.is_(None),
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _stuck_alert_for(
    db_session, project_id, status, title, event_type="task.blocked"
):
    """Persist one terminal-or-live task carrying one open alert of that family."""
    task = make_task(project_id=project_id, status=status)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=title,
        message="stuck",
        task_id=task.id,
        event_type=event_type,
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)
    return task, alert


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "CANCELLED"])
@pytest.mark.asyncio
async def test_stuck_alert_is_dismissed_once_its_task_goes_terminal(
    db_session, mocker, terminal_status
):
    """A task that ended cannot still be stalled, blocked, or stuck evaluating."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _task, alert = await _stuck_alert_for(
        db_session, project.id, terminal_status, "Task stalled in queue: widget"
    )

    dismissed = await sweep_resolve_terminal_task_alerts(db_session)

    assert dismissed == 1
    await db_session.refresh(alert)
    assert (
        alert.dismissed_at is not None
    ), f"a {terminal_status} task's stuck-alert must not stay open"
    assert await _open_stuck_alerts(db_session, project.id) == []
    bcast.assert_awaited_once()
    assert bcast.await_args.args[1] == "alert_dismissed"


@pytest.mark.parametrize(
    "live_status", ["QUEUED", "IN_PROGRESS", "EVALUATING", "BLOCKED"]
)
@pytest.mark.asyncio
async def test_stuck_alert_survives_while_its_task_is_still_in_flight(
    db_session, mocker, live_status
):
    """Control leg: a non-terminal task keeps its alert. Never resolve live signal."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _task, alert = await _stuck_alert_for(
        db_session, project.id, live_status, "Precondition blocker: precondition"
    )

    dismissed = await sweep_resolve_terminal_task_alerts(db_session)

    assert dismissed == 0
    await db_session.refresh(alert)
    assert (
        alert.dismissed_at is None
    ), f"a {live_status} task is still in flight — its alert is real signal"
    assert len(await _open_stuck_alerts(db_session, project.id)) == 1
    bcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_alert_survives_while_the_task_is_still_failed(
    db_session, mocker
):
    """The asymmetry the rule table exists for: FAILED does NOT clear task.failed.

    ``task.blocked`` and ``review.escalated`` are falsified by every terminal
    status. ``task.failed`` is not — on a task that is still FAILED the alert is
    still literally true and is the operator's only standing pointer at it. A
    shared terminal-status set across the families would silently swallow it,
    which is why each family carries its own set in the rule table.

    (This pin predates the rule table, when ``task.failed`` was simply outside
    the sweep's single-family filter. The assertion is unchanged; only the
    reason it holds has moved from "different event type" to "different status
    set", so the name moved with it.)
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id, status="FAILED")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    other = Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title="Task failed: widget",
        message="failed",
        task_id=task.id,
        event_type="task.failed",
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    assert await sweep_resolve_terminal_task_alerts(db_session) == 0
    await db_session.refresh(other)
    assert other.dismissed_at is None


@pytest.mark.asyncio
async def test_approval_timeout_held_alert_retires_when_the_hold_resolves(
    db_session, mocker
):
    """``approval.timeout_held`` needs a CONDITION-derived release, not a manual
    one: it claims "parked, waiting on you" (incident 70c5ec8f), and a MANUAL
    retirement would make the row immortal once the hold ends — the alert-
    lifecycle-vs-condition-lifecycle defect class. The rule table retires it, so
    no call site has to remember to."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _task, alert = await _stuck_alert_for(
        db_session,
        project.id,
        "CANCELLED",
        "Approval-gate timeout: work HELD for an operator decision (not failed)",
        event_type="approval.timeout_held",
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 1
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None


@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_does_not_reopen_or_restamp(db_session, mocker):
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _task, alert = await _stuck_alert_for(
        db_session, project.id, "CANCELLED", "Task permanently blocked: precondition"
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 1
    await db_session.refresh(alert)
    first_stamp = alert.dismissed_at
    assert first_stamp is not None

    assert await sweep_resolve_terminal_task_alerts(db_session) == 0
    await db_session.refresh(alert)
    assert alert.dismissed_at == first_stamp, "a second pass must not re-stamp"
    assert bcast.await_count == 1, "a resolved alert must not re-broadcast"


@pytest.mark.asyncio
async def test_clearing_a_stale_alert_restores_notification_for_a_requeued_task(
    db_session, mocker
):
    """The second-order payoff: under-clearing causes under-NOTIFYING.

    ``_maybe_create_precondition_blocker_alert`` dedupes on ``(task_id, title)``
    among undismissed rows. So while a stale alert stays open, a task that is
    requeued and blocks again on the same blocker raises NOTHING — the operator
    loses the new incident to the corpse of the old one. Resolving on terminal
    is what makes the next real block audible again.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task, stale = await _stuck_alert_for(
        db_session, project.id, "CANCELLED", "Precondition blocker: precondition"
    )

    # Without the sweep the fresh incident is swallowed by the stale row.
    task.status = "BLOCKED"
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    suppressed = await _maybe_create_precondition_blocker_alert(
        task,
        db_session,
        blocker_kind="precondition",
        detail="blocked again",
        severity=AlertSeverity.WARNING,
    )
    assert suppressed is False, "stale open alert suppresses the real new one"

    # Resolve the corpse the way the sweep would have, then retry.
    task.status = "CANCELLED"
    db_session.add(task)
    await db_session.commit()
    assert await sweep_resolve_terminal_task_alerts(db_session) == 1
    await db_session.refresh(stale)
    assert stale.dismissed_at is not None

    task.status = "BLOCKED"
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    audible = await _maybe_create_precondition_blocker_alert(
        task,
        db_session,
        blocker_kind="precondition",
        detail="blocked again",
        severity=AlertSeverity.WARNING,
    )
    assert audible is True, "once the corpse is cleared the new incident alerts"
    assert len(await _open_stuck_alerts(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_sweep_batch_is_bounded_and_drains_across_cycles(db_session, mocker):
    """A backlog must not be held in one write transaction — and must still drain."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(5):
        await _stuck_alert_for(
            db_session, project.id, "CANCELLED", "Task stalled in queue: widget"
        )

    assert await sweep_resolve_terminal_task_alerts(db_session, limit=2) == 2
    assert len(await _open_stuck_alerts(db_session, project.id)) == 3
    assert await sweep_resolve_terminal_task_alerts(db_session, limit=2) == 2
    assert await sweep_resolve_terminal_task_alerts(db_session, limit=2) == 1
    assert await _open_stuck_alerts(db_session, project.id) == []


# ── the other two families the same sweep now owns ────────────────────────────
#
# ``task.blocked`` was only the first family with this defect. Measured on the
# same live feed 2026-08-21, with the ``task.blocked`` rule already merged but
# not yet deployed: 12 of 12 open ``task.failed`` alerts named an
# already-terminal task (8 CANCELLED, 1 COMPLETED, 3 legitimately still
# FAILED), and 5 of 7 open ``review.escalated`` alerts named a COMPLETED task.
#
# The families are NOT interchangeable, and every test below exists to hold
# that line: FAILED falsifies ``task.blocked`` and ``review.escalated`` but
# NOT ``task.failed``, whose message is still true on a task that is still
# failed.


async def _open_alerts_of(db_session, project_id, event_type):
    stmt = sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.event_type == event_type,
        Alert.dismissed_at.is_(None),
    )
    return (await db_session.execute(stmt)).scalars().all()


async def _alert_for(db_session, project_id, status, title, event_type):
    """Persist one task in ``status`` carrying one open alert of ``event_type``."""
    task = make_task(project_id=project_id, status=status)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=title,
        message="condition detail",
        task_id=task.id,
        event_type=event_type,
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)
    return task, alert


async def _fresh_project(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "CANCELLED"])
@pytest.mark.asyncio
async def test_review_escalation_is_dismissed_once_its_task_goes_terminal(
    db_session, mocker, terminal_status
):
    """"Manual review required" cannot be true of a task that already ended.

    The alert names the chain-original task and asks for an adjudication of a
    live review loop. Once that task is terminal the loop is over: a requeue
    mints fresh escalation state rather than re-entering the old episode, so
    there is nothing the operator could still decide here.
    """
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = await _fresh_project(db_session)

    _task, alert = await _alert_for(
        db_session,
        project.id,
        terminal_status,
        "Review-fix escalation: stuck cycle on PR #1777",
        "review.escalated",
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 1
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None, (
        f"a {terminal_status} task has no live review loop left to adjudicate"
    )
    assert await _open_alerts_of(db_session, project.id, "review.escalated") == []
    bcast.assert_awaited_once()
    assert bcast.await_args.args[1] == "alert_dismissed"


@pytest.mark.parametrize(
    "live_status", ["QUEUED", "IN_PROGRESS", "EVALUATING", "BLOCKED"]
)
@pytest.mark.asyncio
async def test_review_escalation_survives_while_its_task_is_still_in_flight(
    db_session, mocker, live_status
):
    """Control leg: an escalation on a live chain is exactly the signal wanted.

    EVALUATING is the status escalated chains actually sit in, so this leg is
    the one that would catch an over-broad rule.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _fresh_project(db_session)

    _task, alert = await _alert_for(
        db_session,
        project.id,
        live_status,
        "Review-fix escalation: dispute standoff on PR #1769",
        "review.escalated",
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 0
    await db_session.refresh(alert)
    assert alert.dismissed_at is None, (
        f"a {live_status} chain still needs the operator decision this asks for"
    )


@pytest.mark.parametrize("settled_status", ["COMPLETED", "CANCELLED"])
@pytest.mark.asyncio
async def test_failure_alert_is_dismissed_once_the_failure_was_settled(
    db_session, mocker, settled_status
):
    """A failure alert asks for attention; COMPLETED and CANCELLED both settle it.

    COMPLETED means the work was retried and succeeded. CANCELLED means it was
    deliberately abandoned. Either way the operator has nothing left to do, and
    on the live feed these two accounted for 9 of the 12 open ``task.failed``
    alerts.
    """
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = await _fresh_project(db_session)

    _task, alert = await _alert_for(
        db_session, project.id, settled_status, "Task failed: widget", "task.failed"
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 1
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None, (
        f"a task that is now {settled_status} no longer needs its failure alert"
    )
    bcast.assert_awaited_once()


@pytest.mark.parametrize(
    "unrelated_event_type",
    ["deployment.auto_resume", "release.ci_unresolved", "release.calcifying"],
)
@pytest.mark.asyncio
async def test_sweep_leaves_families_outside_the_rule_table_alone(
    db_session, mocker, unrelated_event_type
):
    """Scope pin: only families in the rule table are touched, terminal or not.

    These three are the largest open families on the live feed and none of them
    is task-derived — their conditions live in the deployment and release
    lanes. A terminal task must never be read as evidence about them.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _fresh_project(db_session)

    _task, alert = await _alert_for(
        db_session, project.id, "CANCELLED", "Unrelated condition", unrelated_event_type
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 0
    await db_session.refresh(alert)
    assert alert.dismissed_at is None


@pytest.mark.asyncio
async def test_all_three_families_clear_in_one_pass(db_session, mocker):
    """The OR-ed query must resolve every family in a single sweep, not just one.

    Written against the shape a naive rewrite produces — filtering on one family
    and looping — which would clear one row per cycle and leave the rest.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _fresh_project(db_session)

    await _alert_for(
        db_session, project.id, "COMPLETED", "Task stalled in queue: a", "task.blocked"
    )
    await _alert_for(
        db_session,
        project.id,
        "COMPLETED",
        "Review-fix escalation: stuck cycle on PR #1",
        "review.escalated",
    )
    await _alert_for(
        db_session, project.id, "CANCELLED", "Task failed: c", "task.failed"
    )
    # ...and one of each that must be left behind.
    await _alert_for(db_session, project.id, "FAILED", "Task failed: d", "task.failed")
    await _alert_for(
        db_session, project.id, "BLOCKED", "Task stalled in queue: e", "task.blocked"
    )

    assert await sweep_resolve_terminal_task_alerts(db_session) == 3
    assert len(await _open_alerts_of(db_session, project.id, "task.failed")) == 1
    assert len(await _open_alerts_of(db_session, project.id, "task.blocked")) == 1
    assert await _open_alerts_of(db_session, project.id, "review.escalated") == []


@pytest.mark.asyncio
async def test_a_stale_alert_of_any_family_does_not_swallow_the_next_failure_alert(
    db_session, mocker
):
    """The widest second-order payoff, and why this is not cosmetic.

    ``_maybe_create_failure_alert`` must dedupe only within the ``task.failed``
    family. A corpse ``review.escalated`` row from a converged loop must not
    mute a genuine later failure of the same task.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    project = await _fresh_project(db_session)

    task, stale = await _alert_for(
        db_session,
        project.id,
        "COMPLETED",
        "Review-fix escalation: stuck cycle on PR #1777",
        "review.escalated",
    )

    # The task is requeued and this time genuinely fails.
    task.status = "FAILED"
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _maybe_create_failure_alert(task, db_session, "boom")
    await db_session.commit()
    assert len(await _open_alerts_of(db_session, project.id, "task.failed")) == 1, (
        "an unrelated open alert must not suppress the real failure alert"
    )

    # Clear the corpse the way the sweep does, then let it fail again.
    task.status = "COMPLETED"
    db_session.add(task)
    await db_session.commit()
    assert await sweep_resolve_terminal_task_alerts(db_session) == 2
    await db_session.refresh(stale)
    assert stale.dismissed_at is not None

    task.status = "FAILED"
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    await _maybe_create_failure_alert(task, db_session, "boom")
    await db_session.commit()
    assert len(await _open_alerts_of(db_session, project.id, "task.failed")) == 1, (
        "once the corpse is cleared the real failure is audible again"
    )


@pytest.mark.asyncio
async def test_rule_table_never_lets_a_family_inherit_another_status_set(db_session):
    """Pin the table itself: the FAILED asymmetry is the contract, not an accident.

    A future edit that collapses these onto one shared set would still pass
    every behavioural test above for ``task.blocked`` and ``review.escalated``
    while quietly clearing live ``task.failed`` signal. Assert the shape.
    """
    from app.alert_dispatch import _TERMINAL_TASK_ALERT_RULES
    from app.models import TaskStatus

    assert set(_TERMINAL_TASK_ALERT_RULES) == {
        "task.blocked",
        "review.escalated",
        "task.failed",
        "approval.timeout_held",
    }
    # "held for an operator decision" is falsified by EVERY terminal status,
    # including the CANCELLED the hold's own 72h bound produces. Literal, not
    # the constant, so a change to the constant cannot certify itself.
    assert _TERMINAL_TASK_ALERT_RULES["approval.timeout_held"] == {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    assert TaskStatus.FAILED in _TERMINAL_TASK_ALERT_RULES["task.blocked"]
    assert TaskStatus.FAILED in _TERMINAL_TASK_ALERT_RULES["review.escalated"]
    assert TaskStatus.FAILED not in _TERMINAL_TASK_ALERT_RULES["task.failed"], (
        "a task.failed alert on a still-FAILED task is TRUE and must survive"
    )
    assert _TERMINAL_TASK_ALERT_RULES["task.failed"] == {
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    }


@pytest.mark.asyncio
async def test_unrelated_open_alert_does_not_swallow_a_real_failure(db_session):
    """A different family's open alert must not suppress a failure alert.

    The task-only lookup this replaced treated any undismissed row on the task
    -- ``task.blocked``, ``review.escalated`` -- as the failure episode, so the
    real failure never reached the feed. Dedup belongs inside the episode, not
    across every signal the task ever raised.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(project_id=project.id, status="FAILED")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Alert(
            project_id=project.id,
            task_id=task.id,
            severity=AlertSeverity.WARNING,
            title="Task blocked",
            message="blocked on a dependency",
            event_type="task.blocked",
        )
    )
    await db_session.commit()

    await _maybe_create_failure_alert(task, db_session, "boom")
    await db_session.commit()

    assert len(await _open_alerts_of(db_session, project.id, "task.failed")) == 1
    assert len(await _open_alerts_of(db_session, project.id, "task.blocked")) == 1
