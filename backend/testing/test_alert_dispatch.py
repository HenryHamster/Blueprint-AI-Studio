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
)
from app.models import Alert, AlertSeverity
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


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
async def test_pool_payoff_alert_creates_and_broadcasts(db_session, mocker):
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
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "COST_REGRESSION" in alerts[0].title
    assert "Reduce the pool target or disable." in alerts[0].message
    bcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_payoff_alert_embeds_supporting_numbers(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
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
    msg = (await _payoff_alerts(db_session, project.id))[0].message
    assert "0.4" in msg  # pool hit rate
    assert "0.5" in msg  # pool-origin share
    assert "8 warm starts" in msg
    assert "50 pool dispatches" in msg
    assert "20.00%" in msg  # idle-inclusive cost drift
    assert "12" in msg and "10" in msg  # current vs baseline cost


@pytest.mark.asyncio
async def test_pool_payoff_alert_dedups_same_verdict(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
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
    assert len(alerts) == 1  # second call deduped on (project_id, title)


@pytest.mark.asyncio
async def test_pool_payoff_alert_distinct_verdict_creates_separate(db_session, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
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
    assert len(alerts) == 2
    states = {a.title.split(":")[1].split(" on ")[0].strip() for a in alerts}
    assert states == {"COST_REGRESSION", "PROVEN"}


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
