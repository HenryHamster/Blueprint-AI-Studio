"""Tests for the deterministic operator-Alert emitters that replaced the removed
self-repair (detect->investigate->fix) apparatus.

Covers the two NEW emitters re-homed into ``app.pipeline_alert_scan`` plus the
reaper Step-7 zero-cycle starvation detector. These could not be authored by the
slice agents because the new code did not exist yet at slice time:

  * ``_emit_agent_vm_resource_alert`` — fires a WARNING Alert (no remediation
    task) when >=3 VM-layer-classified FAILED tasks land in the 24h window.
  * ``_emit_pipeline_success_rate_alert`` — fires when the 24h success-rate is
    below the configured floor AND the terminal-task sample clears the minimum.
  * ``reaper._sweep_strategic_cycle_starvation`` — emits a deduped
    ``strategic_cycle_stall`` WARNING Alert for an autonomous project with no
    StrategicCycle in the starvation window (the zero-cycle case, distinct from
    a mid-cycle stall).

All three are deterministic Alerts: they enrich the Alert table for operator
visibility and spawn NO autonomous task.
"""

import uuid
from datetime import timedelta

import pytest
from sqlmodel import select

from app.failure_classification import AGENT_TIMEOUT, PROVISION_FAILURE, TIMEOUT_OOM
from app.models import Alert, AlertSeverity, StrategicCycle, TaskStatus
from app.pipeline_alert_scan import (
    _AGENT_VM_RESOURCE_ALERT_TITLE,
    _AGENT_VM_RESOURCE_MIN_FAILURES,
    _PIPELINE_SUCCESS_MIN_SAMPLE,
    _PIPELINE_SUCCESS_RATE_ALERT_TITLE,
    _SIGNAL_STALENESS_WINDOWS,
    _emit_agent_vm_resource_alert,
    _emit_pipeline_success_rate_alert,
    _run_signal_staleness_watchdog,
    run_pipeline_alert_scan_once,
)
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


def _iso(dt) -> str:
    """Render a naive-UTC datetime as the ISO string the watchdog parses."""
    return dt.isoformat()


async def _open_alert_with_title(session, project_id, title):
    return (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == title,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _open_warning_alerts(session, project_id, title):
    return (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == title,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


def _vm_failed_task(project_id, *, reason: str):
    """Build a FAILED task whose context classifies to a VM-layer exit reason."""
    if reason == "vm_provisioning_failure":
        ctx = {"failure_category": PROVISION_FAILURE}
    elif reason == "spot_preempt":
        ctx = {"spot_preemption_count": 1}
    elif reason == "oom":
        ctx = {"failure_category": AGENT_TIMEOUT, "timeout_subtype": TIMEOUT_OOM}
    else:
        raise ValueError(reason)
    return make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        context=ctx,
        created_at=_utcnow() - timedelta(hours=1),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )


# ── _emit_agent_vm_resource_alert ────────────────────────────────────────────


class TestAgentVmResourceAlert:
    @pytest.mark.asyncio
    async def test_fires_when_failures_meet_threshold(self, db_session):
        """>= MIN_FAILURES VM-layer failures in window emits exactly one WARNING."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        for r in ("oom", "spot_preempt", "vm_provisioning_failure"):
            db_session.add(_vm_failed_task(project.id, reason=r))
        await db_session.commit()
        assert _AGENT_VM_RESOURCE_MIN_FAILURES == 3  # guards the seed count

        created = await _emit_agent_vm_resource_alert(db_session, project.id)
        await db_session.commit()

        assert created == 1
        alerts = await _open_warning_alerts(
            db_session, project.id, _AGENT_VM_RESOURCE_ALERT_TITLE
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING
        # breakdown enumerates each exit reason exactly once
        msg = alerts[0].message
        assert "oom=1" in msg
        assert "spot_preempt=1" in msg
        assert "vm_provisioning_failure=1" in msg

    @pytest.mark.asyncio
    async def test_no_alert_below_threshold(self, db_session):
        """2 VM failures (< 3) must NOT emit an alert."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        for r in ("oom", "spot_preempt"):
            db_session.add(_vm_failed_task(project.id, reason=r))
        await db_session.commit()

        created = await _emit_agent_vm_resource_alert(db_session, project.id)
        await db_session.commit()

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _AGENT_VM_RESOURCE_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_code_failures_excluded(self, db_session):
        """FAILED tasks that classify as code-level (no VM exit reason) are ignored."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        for _ in range(4):
            db_session.add(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.FAILED,
                    context={"failure_category": "CODE_ERROR"},
                    created_at=_utcnow() - timedelta(hours=1),
                )
            )
        await db_session.commit()

        created = await _emit_agent_vm_resource_alert(db_session, project.id)
        await db_session.commit()

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _AGENT_VM_RESOURCE_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_deduped_on_second_run(self, db_session):
        """A second scan with an open alert already present does not duplicate it."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        for r in ("oom", "oom", "oom"):
            db_session.add(_vm_failed_task(project.id, reason=r))
        await db_session.commit()

        first = await _emit_agent_vm_resource_alert(db_session, project.id)
        await db_session.commit()
        second = await _emit_agent_vm_resource_alert(db_session, project.id)
        await db_session.commit()

        assert first == 1
        assert second == 0
        assert (
            len(
                await _open_warning_alerts(
                    db_session, project.id, _AGENT_VM_RESOURCE_ALERT_TITLE
                )
            )
            == 1
        )


# ── _emit_pipeline_success_rate_alert ────────────────────────────────────────


class TestPipelineSuccessRateAlert:
    @pytest.mark.asyncio
    async def test_fires_below_floor_with_sufficient_sample(self, db_session):
        """Success-rate below the 0.5 default floor with >= MIN_SAMPLE fires once."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # 3 completed + 9 failed = 12 terminal (>= MIN_SAMPLE), success_rate 0.25.
        ts = _utcnow() - timedelta(hours=2)
        for _ in range(3):
            db_session.add(
                make_task(
                    project_id=project.id, status=TaskStatus.COMPLETED, created_at=ts
                )
            )
        for _ in range(9):
            db_session.add(
                make_task(
                    project_id=project.id, status=TaskStatus.FAILED, created_at=ts
                )
            )
        await db_session.commit()
        assert _PIPELINE_SUCCESS_MIN_SAMPLE == 10  # guards the seed count

        created = await _emit_pipeline_success_rate_alert(db_session, project.id)
        await db_session.commit()

        assert created == 1
        alerts = await _open_warning_alerts(
            db_session, project.id, _PIPELINE_SUCCESS_RATE_ALERT_TITLE
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING

    @pytest.mark.asyncio
    async def test_no_alert_below_min_sample(self, db_session):
        """Below the minimum terminal-task sample, a low rate does NOT fire (fluke guard)."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        ts = _utcnow() - timedelta(hours=2)
        # 1 completed + 2 failed = 3 terminal (< MIN_SAMPLE), rate 0.33.
        db_session.add(
            make_task(project_id=project.id, status=TaskStatus.COMPLETED, created_at=ts)
        )
        for _ in range(2):
            db_session.add(
                make_task(
                    project_id=project.id, status=TaskStatus.FAILED, created_at=ts
                )
            )
        await db_session.commit()

        created = await _emit_pipeline_success_rate_alert(db_session, project.id)
        await db_session.commit()

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _PIPELINE_SUCCESS_RATE_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_no_alert_when_above_floor(self, db_session):
        """A healthy success-rate (>= floor) with a sufficient sample does not fire."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        ts = _utcnow() - timedelta(hours=2)
        for _ in range(10):
            db_session.add(
                make_task(
                    project_id=project.id, status=TaskStatus.COMPLETED, created_at=ts
                )
            )
        for _ in range(2):
            db_session.add(
                make_task(
                    project_id=project.id, status=TaskStatus.FAILED, created_at=ts
                )
            )
        await db_session.commit()

        created = await _emit_pipeline_success_rate_alert(db_session, project.id)
        await db_session.commit()

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _PIPELINE_SUCCESS_RATE_ALERT_TITLE
        )


# ── reaper Step 7: zero-cycle strategic starvation ───────────────────────────


class TestStrategicCycleStarvation:
    @pytest.mark.asyncio
    async def test_fires_for_autonomous_project_with_no_cycle(self, db_session):
        """An older autonomous project with no StrategicCycle in window gets one WARNING."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=10),  # older than the 24h window
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        created = await _sweep_strategic_cycle_starvation(db_session, now)

        assert created == 1
        alerts = await _open_warning_alerts(
            db_session, project.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING

    @pytest.mark.asyncio
    async def test_no_alert_when_recent_cycle_exists(self, db_session):
        """A recent StrategicCycle row suppresses the starvation alert."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True, created_at=now - timedelta(days=10)
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        db_session.add(
            StrategicCycle(
                id=uuid.uuid4().hex,
                project_id=project.id,
                phase1_started_at=now - timedelta(hours=1),
                phase2_completed_at=now - timedelta(minutes=30),
                outcome="completed",
            )
        )
        await db_session.commit()

        created = await _sweep_strategic_cycle_starvation(db_session, now)

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_skips_new_project_within_window(self, db_session):
        """A brand-new autonomous project (younger than the window) is not flagged."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True, created_at=now - timedelta(hours=1)
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        created = await _sweep_strategic_cycle_starvation(db_session, now)

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_skips_non_autonomous_project(self, db_session):
        """A non-autonomous project is never flagged, even with no cycles."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        project = make_project(
            autonomous_enabled=False, created_at=now - timedelta(days=10)
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        created = await _sweep_strategic_cycle_starvation(db_session, now)

        assert created == 0
        assert not await _open_warning_alerts(
            db_session, project.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_deduped_on_second_sweep(self, db_session):
        """A second sweep with an open starvation alert does not duplicate it."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True, created_at=now - timedelta(days=10)
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        first = await _sweep_strategic_cycle_starvation(db_session, now)
        second = await _sweep_strategic_cycle_starvation(db_session, now)

        assert first == 1
        assert second == 0
        assert (
            len(
                await _open_warning_alerts(
                    db_session, project.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
                )
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_batched_lookup_differentiates_projects(self, db_session):
        """The batched (non-N+1) sweep flags only the starved project when several
        projects with mixed cycle/alert state are scanned together: one starved,
        one with a recent cycle, one with an existing open alert."""
        from app.reaper import _sweep_strategic_cycle_starvation
        from app.reaper_sweeps import _STRATEGIC_CYCLE_STALL_ALERT_TITLE

        now = _utcnow()
        old = now - timedelta(days=10)

        starved = make_project(autonomous_enabled=True, created_at=old)
        with_cycle = make_project(autonomous_enabled=True, created_at=old)
        with_alert = make_project(autonomous_enabled=True, created_at=old)
        db_session.add_all([starved, with_cycle, with_alert])
        await db_session.commit()
        for p in (starved, with_cycle, with_alert):
            await db_session.refresh(p)

        # with_cycle has a recent cycle → no alert.
        db_session.add(
            StrategicCycle(
                id=uuid.uuid4().hex,
                project_id=with_cycle.id,
                phase1_started_at=now - timedelta(hours=1),
                phase2_completed_at=now - timedelta(minutes=30),
                outcome="completed",
            )
        )
        # with_alert already has an open WARNING dedup alert → not duplicated.
        db_session.add(
            Alert(
                project_id=with_alert.id,
                severity=AlertSeverity.WARNING,
                title=_STRATEGIC_CYCLE_STALL_ALERT_TITLE,
                message="pre-existing",
            )
        )
        await db_session.commit()

        created = await _sweep_strategic_cycle_starvation(db_session, now)

        assert created == 1
        assert (
            len(
                await _open_warning_alerts(
                    db_session, starved.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
                )
            )
            == 1
        )
        assert not await _open_warning_alerts(
            db_session, with_cycle.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
        )
        # Still exactly one alert for with_alert — not duplicated by the sweep.
        assert (
            len(
                await _open_warning_alerts(
                    db_session, with_alert.id, _STRATEGIC_CYCLE_STALL_ALERT_TITLE
                )
            )
            == 1
        )


# ── _run_signal_staleness_watchdog ───────────────────────────────────────────


class TestSignalStalenessWatchdog:
    """The re-homed signal-staleness watchdog ("Signal staleness: <signal>" and
    "Signal frozen: sr_catch_rate"). Previously covered by the deleted
    test_pipeline_regression_dispatcher tests."""

    @pytest.mark.asyncio
    async def test_fires_staleness_for_overdue_signal(self, db_session):
        """A signal whose last-computed timestamp is past its window emits a WARNING."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        window = _SIGNAL_STALENESS_WINDOWS["fix_loop_divergence"]
        # Computed well past the freshness window → stale.
        overdue = now - window - timedelta(hours=12)
        design_doc = {
            "fix_loop_divergence_signal_last_computed_at": _iso(overdue),
        }

        created = await _run_signal_staleness_watchdog(
            db_session, project.id, design_doc
        )
        await db_session.commit()

        assert created == 1
        alerts = await _open_alert_with_title(
            db_session, project.id, "Signal staleness: fix_loop_divergence"
        )
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING

    @pytest.mark.asyncio
    async def test_no_staleness_for_fresh_signal(self, db_session):
        """A signal computed inside its freshness window emits nothing."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        design_doc = {
            "fix_loop_divergence_signal_last_computed_at": _iso(
                now - timedelta(hours=1)
            ),
        }

        created = await _run_signal_staleness_watchdog(
            db_session, project.id, design_doc
        )
        await db_session.commit()

        assert created == 0
        assert not await _open_alert_with_title(
            db_session, project.id, "Signal staleness: fix_loop_divergence"
        )

    @pytest.mark.asyncio
    async def test_missing_timestamp_emits_nothing(self, db_session):
        """A signal with no recorded timestamp is never flagged stale."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        created = await _run_signal_staleness_watchdog(db_session, project.id, {})
        await db_session.commit()
        assert created == 0

    @pytest.mark.asyncio
    async def test_fires_frozen_sr_catch_rate(self, db_session):
        """Fresh heartbeat + stale sufficiency data emits 'Signal frozen: sr_catch_rate'."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        design_doc = {
            "sr_catch_rate_trends": {
                # heartbeat is fresh (< 6h) ...
                "signal_last_computed_at": _iso(now - timedelta(hours=1)),
                # ... but the real data has not refreshed in > 24h → frozen.
                "last_sufficient_computed_at": _iso(now - timedelta(hours=30)),
            }
        }

        created = await _run_signal_staleness_watchdog(
            db_session, project.id, design_doc
        )
        await db_session.commit()

        # last_sufficient_computed_at is 30h old (< the 48h sr_catch_rate staleness
        # window, so no "Signal staleness" alert) but trips the frozen sub-alert.
        frozen = await _open_alert_with_title(
            db_session, project.id, "Signal frozen: sr_catch_rate"
        )
        assert len(frozen) == 1
        assert frozen[0].severity == AlertSeverity.WARNING
        assert created >= 1

    @pytest.mark.asyncio
    async def test_staleness_deduped_on_second_run(self, db_session):
        """A second watchdog run does not duplicate an already-open staleness alert."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        window = _SIGNAL_STALENESS_WINDOWS["deploy_health"]
        from app import shared_state as _shared_state

        original = dict(_shared_state.deploy_monitoring)
        try:
            _shared_state.deploy_monitoring["signal_last_computed_at"] = _iso(
                now - window - timedelta(hours=2)
            )
            first = await _run_signal_staleness_watchdog(db_session, project.id, {})
            await db_session.commit()
            second = await _run_signal_staleness_watchdog(db_session, project.id, {})
            await db_session.commit()
        finally:
            _shared_state.deploy_monitoring.clear()
            _shared_state.deploy_monitoring.update(original)

        assert first == 1
        assert second == 0
        assert (
            len(
                await _open_alert_with_title(
                    db_session, project.id, "Signal staleness: deploy_health"
                )
            )
            == 1
        )


# ── run_pipeline_alert_scan_once (orchestration + same-cycle isolation) ───────


class TestRunPipelineAlertScanOnce:
    @pytest.mark.asyncio
    async def test_missing_project_returns_zero(self, db_session):
        created = await run_pipeline_alert_scan_once(db_session, uuid.uuid4())
        assert created == 0

    @pytest.mark.asyncio
    async def test_aggregates_emitter_results(self, db_session):
        """The orchestrator runs every emitter and sums their created counts."""
        project = make_project(
            design_doc={
                "fix_loop_divergence_signal_last_computed_at": _iso(
                    _utcnow()
                    - _SIGNAL_STALENESS_WINDOWS["fix_loop_divergence"]
                    - timedelta(hours=12)
                )
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Seed a VM-resource failure cluster so a second emitter also fires.
        for r in ("oom", "spot_preempt", "vm_provisioning_failure"):
            db_session.add(_vm_failed_task(project.id, reason=r))
        await db_session.commit()

        created = await run_pipeline_alert_scan_once(db_session, project.id)

        # staleness (1) + agent_vm_resource (1) = 2
        assert created == 2
        assert await _open_alert_with_title(
            db_session, project.id, "Signal staleness: fix_loop_divergence"
        )
        assert await _open_alert_with_title(
            db_session, project.id, _AGENT_VM_RESOURCE_ALERT_TITLE
        )

    @pytest.mark.asyncio
    async def test_later_emitter_failure_does_not_drop_earlier_alert(
        self, db_session, monkeypatch
    ):
        """The same-cycle isolation fix: a LATER emitter raising must not roll back
        an EARLIER emitter's already-committed Alert, and the count must stay accurate.

        Before the fix, all emitters shared one session with a single end-of-scan
        commit: the failing emitter's session.rollback() discarded the earlier
        (still-uncommitted) Alert while ``created`` had already been incremented.
        """
        import app.pipeline_alert_scan as pas

        project = make_project(
            design_doc={
                "fix_loop_divergence_signal_last_computed_at": _iso(
                    _utcnow()
                    - _SIGNAL_STALENESS_WINDOWS["fix_loop_divergence"]
                    - timedelta(hours=12)
                )
            }
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Make the LAST emitter blow up after the staleness emitter has run + committed.
        async def _boom(session, project_id):
            raise RuntimeError("synthetic emitter failure")

        monkeypatch.setattr(pas, "_emit_pipeline_success_rate_alert", _boom)

        created = await run_pipeline_alert_scan_once(db_session, project.id)

        # The staleness alert was committed before the failing emitter ran, so it
        # survives — and `created` counts exactly the one persisted alert.
        assert created == 1
        surviving = await _open_alert_with_title(
            db_session, project.id, "Signal staleness: fix_loop_divergence"
        )
        assert len(surviving) == 1

        # Cross-check against the DB on a fresh session: the alert truly persisted
        # (not merely visible in the dirty in-test session).
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as verify_session:
            persisted = await _open_alert_with_title(
                verify_session, project.id, "Signal staleness: fix_loop_divergence"
            )
        assert len(persisted) == 1
