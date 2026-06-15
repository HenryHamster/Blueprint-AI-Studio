"""Tests for release_stall_alerts — managed_check_wait, auto-merge, and deploy-failed checks.

Covers:
  * _check_version_managed_check_wait_stall — threshold gate (no fire before 20 min),
    first-fire WARNING, escalation to ERROR after 2h, re-alert suppression within the
    20-min re-alert interval.
  * _check_version_auto_merge_stall — SKIPS versions in managed_check_wait sub-state
    (those are handled by the dedicated MCW checker with a much shorter threshold).
  * _alert_promotion_deploy_failed — first-fire creates ERROR alert, reason codes, idempotency.
  * _resolve_promotion_deploy_failed_alert — no-op when none open, dismisses open alert, idempotent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import release_qualifier as rq
from app import release_version as rv
from app.models import Alert, AlertSeverity, Project
from app.release_stall_alerts import (
    _MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS,
    _MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS,
    _MANAGED_CHECK_WAIT_STALL_SECONDS,
    _alert_promotion_deploy_failed,
    _check_version_auto_merge_stall,
    _check_version_managed_check_wait_stall,
    _deploy_failed_alert_title,
    _resolve_promotion_deploy_failed_alert,
)
from app.time_utils import _utcnow
from sqlalchemy import select


async def _make_project_and_version(db_session):
    project = Project(
        name="Stall-Test",
        user_id="stall-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _mcw_qual(
    *, enabled_seconds_ago: float, last_alerted_seconds_ago: float | None = None
):
    """Build a managed_check_wait pending qualification dict."""
    enabled_at = (_utcnow() - timedelta(seconds=enabled_seconds_ago)).isoformat()
    pending: dict = {
        "pr_number": 99,
        "pr_url": "https://github.com/acme/widget/pull/99",
        "auto_merge_enabled": False,
        "migration_carrying": True,
        "managed_check_wait": True,
        "why_not_native_automerge": "migration_guard_deferred",
        "enabled_at": enabled_at,
        "ci_state": "pending",
    }
    if last_alerted_seconds_ago is not None:
        pending["mcw_last_alerted_at"] = (
            _utcnow() - timedelta(seconds=last_alerted_seconds_ago)
        ).isoformat()
    return {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "confidence": 0.95,
        "promotion_pending": pending,
    }


# ---------------------------------------------------------------------------
# _check_version_managed_check_wait_stall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcw_stall_does_not_fire_before_threshold(db_session):
    """No alert fires when the managed_check_wait sub-state is under 20 min old."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS - 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    assert fired is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_mcw_stall_fires_warning_at_threshold(db_session):
    """After 20 min a WARNING-severity alert fires and mcw_last_alerted_at is stamped."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "managed_check_wait" in alerts[0].title.lower()

    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("mcw_last_alerted_at") is not None
    assert pending.get("mcw_alert_count") == 1


@pytest.mark.asyncio
async def test_mcw_stall_escalates_to_error_after_critical_threshold(db_session):
    """After 2h (_MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS) the alert severity escalates to ERROR."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS + 60
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR


@pytest.mark.asyncio
async def test_mcw_stall_suppressed_within_realert_interval(db_session):
    """A second call within the 20-min re-alert window returns False (no duplicate alert)."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 120,
        last_alerted_seconds_ago=_MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS - 60,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    assert fired is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_mcw_stall_refires_after_realert_interval(db_session):
    """A second call AFTER the 20-min re-alert window fires again and increments alert_count."""
    _project, version = await _make_project_and_version(db_session)
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=_MANAGED_CHECK_WAIT_STALL_SECONDS + 120,
        last_alerted_seconds_ago=_MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS + 60,
    )
    pending = dict(version.last_qualification["promotion_pending"])
    pending["mcw_alert_count"] = 1
    qual = dict(version.last_qualification)
    qual["promotion_pending"] = pending
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_managed_check_wait_stall(db_session, version)
    await db_session.commit()
    assert fired is True

    await db_session.refresh(version)
    assert version.last_qualification["promotion_pending"]["mcw_alert_count"] == 2


# ---------------------------------------------------------------------------
# _check_version_auto_merge_stall — must skip managed_check_wait versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_merge_stall_skips_managed_check_wait(db_session, mocker):
    """_check_version_auto_merge_stall returns False without creating an alert when
    the pending sub-state has managed_check_wait=True (handled by the MCW checker)."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    _project, version = await _make_project_and_version(db_session)
    # Use a very old enabled_at so the auto-merge threshold would fire if MCW weren't skipped.
    version.last_qualification = _mcw_qual(
        enabled_seconds_ago=rq._AUTO_MERGE_STALL_SECONDS + 3600
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _check_version_auto_merge_stall(db_session, version)
    assert fired is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# _alert_promotion_deploy_failed / _resolve_promotion_deploy_failed_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_failed_alert_first_fire(db_session, mocker):
    """First call creates an ERROR alert and stamps deploy_confirm_alerted_at."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    # Silence the _notify_operator call so the test stays self-contained.
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "rolled_back"
    version.promoted_sha = "abc123" * 3
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    fired = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    assert fired is True

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert _deploy_failed_alert_title(version.version_number) == alerts[0].title

    await db_session.refresh(version)
    assert version.deploy_confirm_alerted_at is not None


@pytest.mark.asyncio
async def test_deploy_failed_alert_reason_codes(db_session, mocker):
    """Known reason codes appear verbatim in the alert message."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "sha_mismatch"
    version.promoted_sha = "deadbeef1234"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert any("sha_mismatch" in a.message for a in alerts)


@pytest.mark.asyncio
async def test_deploy_failed_alert_idempotent(db_session, mocker):
    """Calling _alert_promotion_deploy_failed twice does not create a second alert."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "halted"
    version.promoted_sha = "cafebabe1234"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_noop_when_none(db_session):
    """_resolve_promotion_deploy_failed_alert returns 0 when no open alert exists."""
    _project, version = await _make_project_and_version(db_session)

    count = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    assert count == 0


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_dismisses_open(db_session, mocker):
    """_resolve_promotion_deploy_failed_alert dismisses an open deploy-failed alert."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "no_new_revision"
    version.promoted_sha = "001122334455"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    count = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    assert count == 1

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_resolve_deploy_failed_alert_idempotent(db_session, mocker):
    """Calling _resolve_promotion_deploy_failed_alert twice returns 0 on the second call."""
    mocker.patch(
        "app.release_stall_alerts._release_qualifier_module",
        return_value=rq,
    )
    mocker.patch(
        "app.release_promotion._notify_operator",
        return_value=None,
        create=True,
    )
    _project, version = await _make_project_and_version(db_session)
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = "rolled_back"
    version.promoted_sha = "aabbccddeeff"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _alert_promotion_deploy_failed(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()
    second = await _resolve_promotion_deploy_failed_alert(db_session, version)
    await db_session.commit()

    assert first == 1
    assert second == 0
