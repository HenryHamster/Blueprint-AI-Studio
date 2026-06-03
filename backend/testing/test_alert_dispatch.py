"""Tests for app.alert_dispatch helpers.

Focused on the warm-pool payoff-verdict transition alert
(``_maybe_create_pool_payoff_alert``): creation, broadcast, per-verdict
deduplication, and that the supporting numbers are embedded in the message so
the operator can act from the alert alone. Mirrors the broadcast-mock pattern in
test_alerts.py (patch ``app.alert_dispatch.broadcast_to_project``).
"""

import pytest
from unittest.mock import AsyncMock

from sqlmodel import select as sql_select

from app.alert_dispatch import (
    _UNVERIFIED_REASON_LABELS,
    _maybe_create_pool_payoff_alert,
    _maybe_create_unverified_merge_alert,
)
from app.models import Alert, AlertSeverity
from testing.factories import make_project, make_task


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
    project = make_project()
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
    project = make_project()
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
    project = make_project()
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
    project = make_project()
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
        severity=AlertSeverity.INFO,
    )
    alerts = await _payoff_alerts(db_session, project.id)
    assert len(alerts) == 2
    states = {a.title.split(":")[1].split(" on ")[0].strip() for a in alerts}
    assert states == {"COST_REGRESSION", "PROVEN"}


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
