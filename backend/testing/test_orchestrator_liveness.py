"""Liveness heartbeat decoupling + CI-fix branch resolution.

Incident 2026-05-28: the orchestrator's CI-poll cycle wall-time scales O(N) with
the EVALUATING backlog. The old ``/health`` liveness verdict required a *full
cycle* to complete every 60s, so once the backlog grew the cycle outran the
window, ``/health`` returned 503, Cloud Run killed the container mid-cycle, and
the restart discarded the cycle's progress — an unrecoverable death-loop that
froze the whole pipeline. The fix gates liveness on an in-cycle progress
*heartbeat* (restart only on a genuine wedge), and resolves the fix-spawn branch
from the live PR head so a missing stored branch can't wedge a poison task.
"""

import asyncio
import threading
import uuid
from datetime import timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func
from sqlmodel import select

import app.orchestrator as _orch
import app.orchestrator_liveness as _liv
import app.shared_state as _ss
from app.time_utils import _utcnow
from app.models import (
    Alert,
    LocalRunner,
    MessagesNoisePrefs,
    Project,
    StrategicCycle,
    Task,
    TaskStatus,
)
from testing.factories import make_project
from app.workbench_state import WORKBENCH_STATE_KEY


@pytest.fixture
def restore_orch_state():
    """Snapshot/restore the mutated module globals so these tests don't leak
    liveness state into the rest of the suite."""
    saved = (
        _orch._orchestrator_last_cycle,
        _orch._orchestrator_last_progress,
        _orch._orchestrator_cycle_started,
    )
    saved_leader = _ss.is_leader
    saved_health = dict(_ss.external_health)
    yield
    (
        _orch._orchestrator_last_cycle,
        _orch._orchestrator_last_progress,
        _orch._orchestrator_cycle_started,
    ) = saved
    _ss.is_leader = saved_leader
    _ss.external_health.clear()
    _ss.external_health.update(saved_health)


def _make_leader_db_healthy():
    _ss.is_leader = True
    _ss.external_health["db"] = True
    _ss.external_health["failed_deps"] = []


async def _make_resident_liveness_project(db_session, now, heartbeat_age):
    from app.strategic_resident_session import RESIDENT_SESSION_KEY

    project = make_project(
        autonomous_enabled=True,
        build_config={"automatic_strategic_cycles_enabled": False},
        created_at=now - timedelta(days=3),
    )
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        title="Resident liveness worker",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - heartbeat_age,
        context={
            "_resident_session": True,
            "strategic_session_id": "liveness-session",
            "_resident_worker_generation": 1,
        },
    )
    db_session.add(task)
    await db_session.flush()
    project.vision = {
        RESIDENT_SESSION_KEY: {
            "session_id": "liveness-session",
            "task_id": str(task.id),
            "worker_generation": 1,
            "worker_state": "ready",
            "status": "active",
            "stop_requested": False,
            "started_at": (now - timedelta(days=2)).isoformat(),
            "worker_heartbeat_at": (now - heartbeat_age).isoformat(),
        }
    }
    db_session.add(project)
    await db_session.commit()
    return project


class TestLivenessModuleExtraction:
    """The liveness heartbeat + watchdog LOGIC was extracted to
    ``app.orchestrator_liveness`` while the mutable gauges
    (``_orchestrator_last_*``) stay owned by ``app.orchestrator``. These pin the
    extraction contract: the public API is single-sourced in the new module and
    re-exported from ``app.orchestrator``, and the extracted predicate reads (and
    the heartbeat setter writes) the gauges that remain on ``app.orchestrator`` —
    so a slow-but-progressing worker verdict crosses the module boundary intact.
    """

    def test_public_api_is_single_sourced_in_liveness_module(self):
        # Re-exported names on app.orchestrator must be the SAME objects defined
        # in app.orchestrator_liveness — one implementation, two import paths.
        for name in (
            "evaluate_orchestrator_liveness",
            "hard_watchdog_should_abort",
            "_mark_orchestrator_progress",
            "_bounded_leader_step",
            "start_orchestrator_hard_watchdog",
            "stop_orchestrator_hard_watchdog",
            "OrchestratorLiveness",
            "ORCHESTRATOR_HANG_SECONDS",
            "ORCHESTRATOR_HARD_WATCHDOG_SECONDS",
        ):
            assert getattr(_orch, name) is getattr(_liv, name), name

    def test_extracted_predicate_reads_orchestrator_owned_gauges(
        self, restore_orch_state
    ):
        # Gauges are owned by app.orchestrator; the extracted predicate reaches
        # them via a lazy import. A stale completed cycle but a fresh heartbeat
        # written on app.orchestrator must read as healthy (not stalled) through
        # the NEW module's function.
        now = _utcnow()
        _orch._orchestrator_last_cycle = now - timedelta(seconds=600)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=400)
        _orch._orchestrator_last_progress = now - timedelta(seconds=2)

        liveness = _liv.evaluate_orchestrator_liveness(now)
        assert liveness.healthy is True
        assert liveness.stalled is False
        assert liveness.alive is True

    def test_extracted_predicate_flags_wedge_on_orchestrator_owned_gauges(
        self, restore_orch_state
    ):
        now = _utcnow()
        stale = now - timedelta(seconds=_liv.ORCHESTRATOR_HANG_SECONDS + 30)
        _orch._orchestrator_last_progress = stale
        _orch._orchestrator_last_cycle = stale
        _orch._orchestrator_cycle_started = stale

        assert _liv.evaluate_orchestrator_liveness(now).stalled is True

    def test_mark_progress_setter_writes_orchestrator_owned_gauge(
        self, restore_orch_state
    ):
        # The extracted heartbeat setter must write the gauge that lives on
        # app.orchestrator (not a private copy), so /health and the worker agree.
        _orch._orchestrator_last_progress = None
        _liv._mark_orchestrator_progress()
        assert _orch._orchestrator_last_progress is not None
        age = (_utcnow() - _orch._orchestrator_last_progress).total_seconds()
        assert age < 5


@pytest.mark.asyncio
class TestStrategicLoopLiveness:
    """Completion-keyed strategic-loop detection and its operator route."""

    async def test_halted_loop_raises_and_routes_signal(self, db_session, monkeypatch):
        """An old/incomplete cycle raises one alert and broadcasts it."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        last_completed = now - timedelta(hours=26)
        db_session.add(
            StrategicCycle(
                id=f"sc-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                phase1_started_at=last_completed - timedelta(minutes=20),
                phase1_completed_at=last_completed - timedelta(minutes=5),
                phase2_completed_at=last_completed,
                outcome="completed",
            )
        )
        db_session.add(
            StrategicCycle(
                id=f"sc-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                phase1_started_at=now - timedelta(hours=26),
                outcome="phase1_running",
            )
        )
        await db_session.commit()

        routed = []

        async def record_broadcast(project_id, event, payload):
            routed.append((project_id, event, payload))

        monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", record_broadcast)
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1

        await db_session.refresh(project)
        refresh_marker = (project.vision or {}).get("_strategic_refresh")
        assert refresh_marker is not None
        assert refresh_marker["origin"] == "qualification"
        assert refresh_marker["reason"] == "strategic_cycle_starvation"

        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert "loop=strategic" in alert.message
        assert (
            f"last_successful_completed_at={last_completed.isoformat()}"
            in alert.message
        )
        assert routed == [
            (
                str(project.id),
                "alert_created",
                {
                    "alert_id": str(alert.id),
                    "severity": "WARNING",
                    "title": _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                },
            )
        ]

    async def test_deduped_alert_still_commits_the_strategic_refresh(
        self, db_session
    ):
        """The refresh marker must survive when the alert is deduped away.

        ``request_strategic_refresh`` only flushes, and the deduped path skips
        the alert write that owns the commit. Without an explicit commit the
        marker is discarded by the next sweep's rollback — precisely the case
        the refresh exists for.
        """
        from app.models import AlertSeverity
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        project_id = project.id
        db_session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title=_STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                message="pre-existing open stall alert",
                event_type="research.strategic_cycle_liveness",
            )
        )
        await db_session.commit()

        # Deduped: no new alert is created for this project.
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0

        # Simulate the reaper's next sweep rolling back its own transaction:
        # a committed marker survives, an uncommitted one would vanish.
        await db_session.rollback()
        vision = (
            await db_session.execute(
                select(Project.vision).where(Project.id == project_id)
            )
        ).scalars().one()
        marker = (vision or {}).get("_strategic_refresh")
        assert marker is not None
        assert marker["reason"] == "strategic_cycle_starvation"
        assert marker["origin"] == "qualification"
        assert (
            len(
                (
                    await db_session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                        )
                    )
                ).scalars().all()
            )
            == 1
        )

    async def test_second_halt_after_recovery_raises_a_fresh_signal(
        self, db_session, monkeypatch
    ):
        """A recovered liveness alert retires before a later halt re-alerts."""
        from app.alert_policy import AlertRetirementMode, get_alert_event_policy
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        assert (
            get_alert_event_policy("research.strategic_cycle_liveness").retirement
            == AlertRetirementMode.SOURCE_CONDITION
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
            vision={_RESIDENT_LIVENESS_SUPPRESSION_KEY: 3},
        )
        db_session.add(project)
        await db_session.commit()

        routed = []

        async def record_broadcast(project_id, event, payload):
            routed.append((project_id, event, payload))

        monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", record_broadcast)

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        first_alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert first_alert.dismissed_at is None
        assert first_alert.domain == "core"
        assert first_alert.event_type == "research.strategic_cycle_liveness"
        assert first_alert.dedupe_key == (
            f"research.strategic_cycle_liveness:{project.id}"
        )

        completed_at = now
        db_session.add(
            StrategicCycle(
                id=f"sc-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                phase2_completed_at=completed_at,
                outcome="completed",
            )
        )
        await db_session.commit()

        # The completion is a healthy receipt and must retire the first alert.
        assert await _sweep_strategic_cycle_starvation(
            db_session, now + timedelta(hours=1)
        ) == 0
        await db_session.refresh(first_alert)
        assert first_alert.dismissed_at is not None
        await db_session.refresh(project)
        assert _RESIDENT_LIVENESS_SUPPRESSION_KEY not in (project.vision or {})

        # Once that receipt ages out, a distinct halt must create a new row.
        assert await _sweep_strategic_cycle_starvation(
            db_session, now + timedelta(hours=26)
        ) == 1
        alerts = (
            await db_session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
                .order_by(Alert.created_at, Alert.id)
            )
        ).scalars().all()
        assert len(alerts) == 2
        assert alerts[0].id != alerts[1].id
        assert alerts[0].dismissed_at is not None
        assert alerts[1].dismissed_at is None
        assert [event for _, event, _ in routed] == [
            "alert_created",
            "alert_dismissed",
            "alert_created",
        ]

    async def test_quiet_resident_owned_portfolio_does_not_raise_signal(
        self, db_session
    ):
        """A disabled, idle Resident portfolio is not a strategic halt."""
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
            vision={_RESIDENT_LIVENESS_SUPPRESSION_KEY: 3},
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        await db_session.refresh(project)
        assert (project.vision or {}).get("_strategic_refresh") is None
        assert _RESIDENT_LIVENESS_SUPPRESSION_KEY not in (project.vision or {})

        # A repeated reaper pass remains quiet because no Resident ownership or
        # Workday recovery path makes this project a liveness candidate.
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        await db_session.refresh(project)
        assert (project.vision or {}).get("_strategic_refresh") is None
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()


    async def test_quiet_lane_is_downgraded_not_dropped(self, db_session):
        """A deliberately quiet portfolio must stay VISIBLE, just not paging.

        The sweep excludes a disabled lane from the halt detector AND retires
        any open halt WARNING on it. Correct on its own terms -- no cycle
        receipt can be expected -- but until this signal existed it meant a
        wedged project and a deliberately quiet one produced byte-identical
        alert feeds. That silence is the defect: it hid a whole strategic
        engine being off for ten days.
        """
        from app.alert_dispatch import (
            _STRATEGIC_LANE_DISABLED_ALERT_TITLE,
            strategic_lane_disabled_dedupe_key,
        )
        from app.models import AlertSeverity
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={
                "automatic_strategic_cycles_enabled": {
                    "enabled": False,
                    "reason": "bounded tactical canary",
                    "disabled_by": "operator-patch",
                }
            },
            created_at=now - timedelta(days=3),
            vision={_RESIDENT_LIVENESS_SUPPRESSION_KEY: 3},
        )
        db_session.add(project)
        await db_session.commit()

        # The halt detector still reports zero: this is a downgrade, not a new
        # paging condition, and it must not inflate the sweep's halt count.
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0

        signal = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_LANE_DISABLED_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert signal.severity == AlertSeverity.INFO
        assert signal.dedupe_key == strategic_lane_disabled_dedupe_key(project.id)
        assert "bounded tactical canary" in signal.message
        assert "operator-patch" in signal.message
        assert "expires=never" in signal.message
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()

    async def test_quiet_lane_signal_is_deduped_across_sweeps(self, db_session):
        """One row, no occurrence churn, however often the reaper sweeps.

        A sibling investigation found 19 identical WARNINGs minted because
        ``dedupe_key``/``occurrence_count`` went unused. This producer must not
        repeat that: an unchanged description is a no-op write, so neither a new
        row nor an ``occurrence_count`` bump happens on a quiet re-sweep.
        """
        from app.alert_dispatch import _STRATEGIC_LANE_DISABLED_ALERT_TITLE
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
            vision={_RESIDENT_LIVENESS_SUPPRESSION_KEY: 3},
        )
        db_session.add(project)
        await db_session.commit()

        for _ in range(5):
            assert await _sweep_strategic_cycle_starvation(db_session, now) == 0

        rows = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_LANE_DISABLED_ALERT_TITLE,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert (rows[0].occurrence_count or 1) == 1
        assert "legacy-bare-boolean" in rows[0].message

    async def test_quiet_lane_signal_retires_when_the_window_lapses(
        self, db_session
    ):
        """Re-enabling the lane (or letting ``until`` pass) dismisses the row."""
        from app.alert_dispatch import _STRATEGIC_LANE_DISABLED_ALERT_TITLE
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        until = now + timedelta(hours=6)
        project = make_project(
            autonomous_enabled=True,
            build_config={
                "automatic_strategic_cycles_enabled": {
                    "enabled": False,
                    "until": until.isoformat() + "Z",
                    "reason": "six hour canary",
                }
            },
            created_at=now - timedelta(days=3),
            vision={_RESIDENT_LIVENESS_SUPPRESSION_KEY: 3},
        )
        db_session.add(project)
        await db_session.commit()
        project_id = project.id

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        opened = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == _STRATEGIC_LANE_DISABLED_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert opened.dismissed_at is None

        # The very same stored value, swept after the bound lapses: the lane is
        # live again with no operator write at all, and the signal retires.
        await _sweep_strategic_cycle_starvation(
            db_session, until + timedelta(minutes=1)
        )
        await db_session.refresh(opened)
        assert opened.dismissed_at is not None
        assert (
            await db_session.execute(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.title == _STRATEGIC_LANE_DISABLED_ALERT_TITLE,
                )
            )
        ).scalar_one() == 1

    async def test_successful_resident_recovery_suppresses_alert(
        self, db_session, monkeypatch
    ):
        """A successful Workday restart suppresses the same-pass halt alert."""
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = await _make_resident_liveness_project(
            db_session, now, timedelta(hours=2)
        )
        db_session.add(
            MessagesNoisePrefs(
                user_id="workday-operator",
                project_id=project.id,
                prefs={
                    WORKBENCH_STATE_KEY: {
                        "interaction_mode": "continuous",
                    }
                },
            )
        )
        await db_session.commit()

        attempts = AsyncMock(return_value={"session_id": "recovered"})
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            attempts,
        )

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert attempts.await_count == 2
        await db_session.refresh(project)
        assert project.vision[_RESIDENT_LIVENESS_SUPPRESSION_KEY] == 2
        assert attempts.await_args.kwargs["operator_user_id"] == "workday-operator"
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()

    async def test_endlessly_restarted_resident_eventually_raises_the_halt_signal(
        self, db_session, monkeypatch
    ):
        """Repeated successful Resident restarts cannot mute liveness forever."""
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_BUDGET,
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = await _make_resident_liveness_project(
            db_session, now, timedelta(hours=2)
        )
        db_session.add(
            MessagesNoisePrefs(
                user_id="workday-operator",
                project_id=project.id,
                prefs={
                    WORKBENCH_STATE_KEY: {
                        "interaction_mode": "continuous",
                    }
                },
            )
        )
        await db_session.commit()

        restart = AsyncMock(return_value={"session_id": "restarted"})
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            restart,
        )

        for _ in range(_RESIDENT_LIVENESS_SUPPRESSION_BUDGET):
            assert await _sweep_strategic_cycle_starvation(db_session, now) == 0

        await db_session.refresh(project)
        assert project.vision[_RESIDENT_LIVENESS_SUPPRESSION_KEY] == (
            _RESIDENT_LIVENESS_SUPPRESSION_BUDGET
        )
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        assert restart.await_count == _RESIDENT_LIVENESS_SUPPRESSION_BUDGET + 1

        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert "loop=strategic" in alert.message

    async def test_resident_halt_without_continuous_operator_is_not_a_candidate(
        self, db_session, monkeypatch
    ):
        """Chat-mode prefs must not arm Resident recovery."""
        from app.reaper_sweeps import _sweep_strategic_cycle_starvation

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()
        db_session.add(
            MessagesNoisePrefs(
                user_id="chat-operator",
                project_id=project.id,
                prefs={WORKBENCH_STATE_KEY: {"interaction_mode": "chat"}},
            )
        )
        await db_session.commit()

        attempts = AsyncMock()
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            attempts,
        )

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert attempts.await_count == 0

    async def test_failed_resident_recovery_still_raises_signal(
        self, db_session, monkeypatch
    ):
        """A Workday path that cannot restart the Resident remains actionable."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()
        db_session.add(
            MessagesNoisePrefs(
                user_id="workday-operator",
                project_id=project.id,
                prefs={WORKBENCH_STATE_KEY: {"interaction_mode": "continuous"}},
            )
        )
        await db_session.commit()

        attempts = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            attempts,
        )

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert "loop=strategic" in alert.message

    async def test_starvation_window_is_floored_by_strategic_backstop(
        self, db_session, mocker
    ):
        """The halt window cannot pre-empt the cadence backstop plus grace."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        mocker.patch.dict(
            _ss.runtime_config,
            {
                "strategic_cycle_starvation_hours": 1,
                "strategic_max_interval_minutes": 180,
            },
        )
        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(hours=5),
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert "last 4h" in alert.message

    async def test_resident_recovery_is_bounded_for_runner_and_session_states(
        self, db_session, monkeypatch
    ):
        """Three halted passes do not duplicate a held or newly-started Resident."""
        from app.reaper_sweeps import _sweep_strategic_cycle_starvation
        from app.strategic_resident_session import (
            RESIDENT_SESSION_KEY,
            RESIDENT_SESSION_REASON_KEY,
        )

        now = _utcnow()

        def resident_blob(*, worker_state="ready", task_id=None):
            return {
                RESIDENT_SESSION_KEY: {
                    "session_id": f"resident-{uuid.uuid4().hex[:8]}",
                    "task_id": str(task_id) if task_id else None,
                    "worker_generation": 1,
                    "worker_state": worker_state,
                    "status": "active",
                    "stop_requested": False,
                    "started_at": now.isoformat(),
                }
            }

        no_runner = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
        )
        live = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
            vision=resident_blob(),
        )
        retry_wait = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
        )
        clean_start = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            created_at=now - timedelta(days=3),
        )
        db_session.add_all([no_runner, live, retry_wait, clean_start])
        await db_session.commit()

        live_task = Task(
            project_id=live.id,
            title="Live resident worker",
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.PROVISIONING,
            context={
                "_resident_session": True,
                "strategic_session_id": "live-session",
                "_resident_worker_generation": 1,
            },
        )
        db_session.add(live_task)
        await db_session.flush()
        live.vision = resident_blob(task_id=live_task.id)
        db_session.add(live)
        held_task = Task(
            project_id=retry_wait.id,
            title="Held resident worker",
            payloads=[{"type": "strategic_review"}],
            status=TaskStatus.PROVISIONING,
            retry_after_until=now + timedelta(hours=1),
            context={
                "_resident_session": True,
                "strategic_session_id": "retry-session",
                "_resident_worker_generation": 1,
            },
        )
        db_session.add(held_task)
        await db_session.flush()
        retry_wait.vision = resident_blob(
            worker_state="retry_wait", task_id=held_task.id
        )
        db_session.add(retry_wait)
        db_session.add_all(
            [
                MessagesNoisePrefs(
                    user_id=f"operator-{index}",
                    project_id=project.id,
                    prefs={WORKBENCH_STATE_KEY: {"interaction_mode": "continuous"}},
                )
                for index, project in enumerate(
                    [no_runner, live, retry_wait, clean_start], start=1
                )
            ]
        )
        runner = LocalRunner(
            project_id=clean_start.id,
            token_hash=uuid.uuid4().hex * 2,
            status="active",
            capabilities={"task_types": "all", "max_concurrent": 2},
            last_seen_at=now,
        )
        db_session.add(runner)
        await db_session.commit()

        spawned = []

        async def fake_spawn(session, project, runner_id, **kwargs):
            task = Task(
                project_id=project.id,
                title="Resident recovery task",
                payloads=[{"type": "strategic_review"}],
                status=TaskStatus.PROVISIONING,
                vm_instance_id=f"runner:{runner_id}",
                context={
                    "_resident_session": True,
                    "strategic_session_id": kwargs["session_id"],
                    "_resident_worker_generation": kwargs["worker_generation"],
                },
            )
            session.add(task)
            await session.flush()
            spawned.append(task.id)
            return task

        monkeypatch.setattr(
            "app.strategic_resident_session._spawn_resident_strategic_task",
            fake_spawn,
        )

        for _ in range(3):
            await _sweep_strategic_cycle_starvation(db_session, now)

        await db_session.refresh(no_runner)
        await db_session.refresh(live)
        await db_session.refresh(retry_wait)
        await db_session.refresh(clean_start)
        assert no_runner.vision[RESIDENT_SESSION_REASON_KEY]["reason"] == "no_runner"
        assert len(spawned) == 1
        assert live.vision[RESIDENT_SESSION_KEY]["status"] == "active"
        assert retry_wait.vision[RESIDENT_SESSION_KEY]["worker_state"] == "retry_wait"
        assert clean_start.vision[RESIDENT_SESSION_KEY]["task_id"] == str(spawned[0])
        assert (
            len(
                (
                    await db_session.execute(
                        select(Task).where(Task.project_id == clean_start.id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )
        assert (
            len(
                (
                    await db_session.execute(
                        select(Task).where(Task.project_id == live.id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )
        assert (
            len(
                (
                    await db_session.execute(
                        select(Task).where(Task.project_id == retry_wait.id)
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )

    async def test_quiet_healthy_portfolio_does_not_raise_signal(self, db_session):
        """A recent intentional no-delta receipt is healthy idle, not a halt."""
        from app.reaper_sweeps import (
            _RESIDENT_LIVENESS_SUPPRESSION_KEY,
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
            vision={
                _RESIDENT_LIVENESS_SUPPRESSION_KEY: 3,
                "_strategic_no_delta_skips": [
                    {"skipped_at": (now - timedelta(hours=1)).isoformat()}
                ],
            },
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        await db_session.refresh(project)
        assert (project.vision or {}).get("_strategic_refresh") is None
        assert _RESIDENT_LIVENESS_SUPPRESSION_KEY not in (project.vision or {})
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()

    async def test_failed_phase2_timestamp_does_not_count_as_full_cycle_liveness(
        self, db_session
    ):
        """A failed/cancelled phase-2 timestamp is not a success receipt."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()

        # Reconciliation records a phase-2 failure/cancellation timestamp, but
        # that terminal failure is not a successful liveness receipt.
        db_session.add(
            StrategicCycle(
                id=f"sc-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                phase2_started_at=now - timedelta(hours=2),
                phase2_completed_at=now - timedelta(hours=1),
                outcome="failed_phase2",
                cycle_metadata={"terminal_disposition": "cancelled"},
            )
        )
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert "last_successful_completed_at=never" in alert.message

    async def test_live_resident_session_is_not_reported_halted(
        self, db_session, monkeypatch
    ):
        """A fresh exact Resident worker stays healthy past the cycle window."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )
        now = _utcnow()
        project = await _make_resident_liveness_project(
            db_session, now, timedelta(minutes=1)
        )

        restart = AsyncMock()
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            restart,
        )

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert restart.await_count == 0
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()

    async def test_quieting_an_alerted_project_retires_its_open_halt_alert(
        self, db_session, monkeypatch
    ):
        """Alerted-then-quieted must not leave a permanent stale halt banner.

        A quiet project is filtered out of the candidate set before the
        per-candidate resolution loop, and the event's SOURCE_CONDITION
        retirement mode means no policy sweep retires it either — so the
        starvation sweep itself has to dismiss the row it created.
        """
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": True},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()

        # The automatic lane halts and the operator gets the WARNING.
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        alert = (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().one()
        assert alert.dismissed_at is None

        # The operator then deliberately quiets the project: automatic cycles
        # off, chat mode, no Resident ownership.
        project.build_config = {"automatic_strategic_cycles_enabled": False}
        db_session.add(project)
        await db_session.commit()

        restart = AsyncMock()
        monkeypatch.setattr(
            "app.strategic_resident_session._auto_start_on_workday_entry",
            restart,
        )

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert restart.await_count == 0
        await db_session.refresh(alert)
        assert alert.dismissed_at is not None

        # Idempotent: a second quiet pass has nothing left to retire.
        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                    Alert.dismissed_at.is_(None),
                )
            )
        ).scalars().first()

    async def test_liveness_alert_routes_to_operator_action_needed(self):
        from app.message_audience import OPERATOR_ACTION_NEEDED, route_event

        assert route_event("research.strategic_cycle_liveness") == OPERATOR_ACTION_NEEDED

    async def test_wall_clock_backstop_ignores_failure_breaker(self):
        from app.autonomous_research import _strategic_backstop_due

        now = _utcnow()
        assert _strategic_backstop_due(
            now - timedelta(days=2), now, max_interval_minutes=1_440
        ) is True

    async def test_wall_clock_backstop_fires_with_failure_breaker_open(
        self, db_session, mocker
    ):
        """A stale strategic loop still gets a run opportunity after failures."""
        from unittest.mock import AsyncMock

        from app.autonomous_research import _spawn_research_task
        from app.models import Task, TaskStatus

        mock_spawn = mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.strategic_spawning._spawn_design_doc_update",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=True,
        )
        mocker.patch.dict(_ss.runtime_config, {"strategic_max_interval_minutes": 180})

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            directions=[{"id": "d-liveness-backstop", "status": "active"}],
            vision={
                "purpose": "test",
                "_research_spawn_count": 1,
                "_strategic_failures": 3,
                "_strategic_failures_at": now.isoformat(),
            },
            design_doc={"systems/api": {"id": "api", "status": "working"}},
        )
        db_session.add(project)
        await db_session.commit()

        db_session.add(
            Task(
                project_id=project.id,
                title="Old strategic cycle",
                payloads=[{"type": "strategic_review", "base_branch": "main"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(hours=4),
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        assert mock_spawn.await_count == 1

    async def test_halted_loop_with_open_breaker_still_consumes_the_sweep_refresh_marker(
        self, db_session, mocker
    ):
        """The starvation sweep's recovery arm reaches a halted loop."""
        from app.autonomous_research import _spawn_research_task
        from app.models import TaskStatus
        from app.reaper_sweeps import _sweep_strategic_cycle_starvation

        strategic_spawn = mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.strategic_spawning._spawn_design_doc_update",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=True,
        )
        mocker.patch(
            "app.autonomous_research.project_parallelism_cap", return_value=0
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": True},
            directions=[],
            vision={"purpose": "test"},
            design_doc={"systems/api": {"id": "api", "status": "working"}},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        await db_session.refresh(project)
        assert project.vision["_strategic_refresh"]["reason"] == (
            "strategic_cycle_starvation"
        )

        _ss.circuit_breakers[str(project.id)] = {
            "state": "OPEN",
            "failure_count": 3,
            "success_count": 0,
            "opened_at": now,
            "cooldown_seconds": 1_800,
            "base_cooldown": 1_800,
            "half_open_task_id": None,
        }
        try:
            await _spawn_research_task(project, db_session)
        finally:
            _ss.circuit_breakers.pop(str(project.id), None)

        await db_session.refresh(project)
        assert strategic_spawn.await_count == 1
        assert project.vision.get("_strategic_refresh") is None
        assert project.vision["_strategic_refresh_last_fired_origin"] == (
            "qualification"
        )
        assert not (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        ).scalars().all()

    @pytest.mark.parametrize("strategic_failures", [3, 4, 5, 6])
    async def test_idle_sweep_driver_reaches_starvation_bypass_for_direction_less_project_at_four_failures(
        self, db_session, mocker, strategic_failures
    ):
        """The idle caller must not shadow the spawner's recovery ceiling.

        The legacy vision purpose is a schema placeholder, while the current
        problem model has a real purpose. A direction-less project in this
        state must reach the spawner even after the old three-failure park;
        the spawner then owns the bounded starvation-bypass decision.
        """
        from app.autonomous_research import _run_idle_research_for_project

        inner_spawn = mocker.patch(
            "app.autonomous_research._spawn_research_task_inner",
            new=AsyncMock(return_value=1),
        )
        mocker.patch(
            "app.autonomous_research._project_spend_ceiling_spawn_defer_reason",
            new=AsyncMock(return_value="test defer"),
        )
        mocker.patch(
            "app.autonomous_research.commit_legacy_authorized",
            new=AsyncMock(),
        )
        project = make_project(
            autonomous_enabled=True,
            directions=[],
            vision={
                "purpose": "<from analysis>",
                "_strategic_failures": strategic_failures,
            },
            problem_model={"intent": {"purpose": "Build the reliable thing"}},
        )
        db_session.add(project)
        await db_session.commit()
        from app.problem_model import project_has_purpose

        assert project_has_purpose(project) is True
        assert _ss.monthly_budget_gate_engaged is False
        assert _ss.daily_budget_gate_engaged is False

        spawned = await _run_idle_research_for_project(
            project,
            db_session,
            headroom_budget=1,
            expected_legacy_generation=None,
            fleet_has_capacity=False,
        )
        assert inner_spawn.await_count == 1
        assert spawned == 1
        inner_spawn.assert_awaited_once_with(
            project,
            db_session,
            expected_legacy_generation=None,
            headroom_budget=1,
        )

    async def test_starvation_bypass_does_not_admit_tactical_research_through_open_breaker(
        self, db_session, mocker
    ):
        """The bypass covers the strategic recovery spawn only, not tactical research.

        An active strategic task resets ``is_strategic`` and the spawn falls
        through to ordinary direction research. That fall-through must still be
        blocked by the OPEN breaker — otherwise the starvation bypass silently
        re-admits every tactical spawn on a struggling project.
        """
        from app.autonomous_research import _spawn_research_task
        from app.reaper_sweeps import _sweep_strategic_cycle_starvation

        strategic_spawn = mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.strategic_spawning._spawn_design_doc_update",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=True,
        )

        now = _utcnow()
        direction = {
            "id": "d-starvation-tactical",
            "text": "Tactical direction",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
        }
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": True},
            directions=[direction],
            vision={"purpose": "test"},
            design_doc={"systems/api": {"id": "api", "status": "working"}},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        await db_session.refresh(project)
        assert project.vision["_strategic_refresh"]["reason"] == (
            "strategic_cycle_starvation"
        )

        # An ACTIVE strategic task forces the is_strategic reset -> tactical
        # fall-through, the exact path the bypass must not open.
        db_session.add(
            Task(
                project_id=project.id,
                title="Running strategic cycle",
                payloads=[{"type": "strategic_review", "base_branch": "main"}],
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
        await db_session.commit()

        async def _research_count() -> int:
            rows = (
                (
                    await db_session.execute(
                        select(Task).where(
                            Task.project_id == project.id,
                            Task.payloads[0]["type"].as_string() == "research",
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

        _ss.circuit_breakers[str(project.id)] = {
            "state": "OPEN",
            "failure_count": 3,
            "success_count": 0,
            "opened_at": now,
            "cooldown_seconds": 1_800,
            "base_cooldown": 1_800,
            "half_open_task_id": None,
        }
        try:
            await _spawn_research_task(project, db_session)
            await db_session.refresh(project)
            assert strategic_spawn.await_count == 0  # blocked by the active cycle
            assert await _research_count() == 0  # breaker still guards tactical

            # Positive control: with the breaker CLOSED the very same state DOES
            # spawn tactical research, so the assertion above is the breaker's
            # doing and not an unrelated guard.
            _ss.circuit_breakers[str(project.id)]["state"] = "CLOSED"
            await _spawn_research_task(project, db_session)
            await db_session.refresh(project)
            assert await _research_count() == 1
        finally:
            _ss.circuit_breakers.pop(str(project.id), None)

    async def test_starvation_bypass_parks_once_the_failure_ceiling_is_reached(
        self, db_session, mocker
    ):
        """A recovery loop whose cycles keep failing stops bypassing and parks.

        The starvation sweep re-arms its marker on every reaper tick, so the
        bypass needs its own ceiling: past ``_STARVATION_BYPASS_FAILURE_CAP``
        recorded strategic failures it must stop crossing the OPEN breaker.
        """
        from app.autonomous_research import (
            _STARVATION_BYPASS_FAILURE_CAP,
            _spawn_research_task,
        )
        from app.reaper_sweeps import _sweep_strategic_cycle_starvation

        strategic_spawn = mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.strategic_spawning._spawn_design_doc_update",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=True,
        )
        mocker.patch(
            "app.autonomous_research.project_parallelism_cap", return_value=0
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": True},
            directions=[],
            vision={
                "purpose": "test",
                # Recorded by the recovery cycles this bypass already spawned;
                # recent enough that the 48h decay has not cleared it.
                "_strategic_failures": _STARVATION_BYPASS_FAILURE_CAP,
                "_strategic_failures_at": now.isoformat(),
            },
            design_doc={"systems/api": {"id": "api", "status": "working"}},
            created_at=now - timedelta(days=3),
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 1
        await db_session.refresh(project)

        _ss.circuit_breakers[str(project.id)] = {
            "state": "OPEN",
            "failure_count": 3,
            "success_count": 0,
            "opened_at": now,
            "cooldown_seconds": 1_800,
            "base_cooldown": 1_800,
            "half_open_task_id": None,
        }
        try:
            await _spawn_research_task(project, db_session)
        finally:
            _ss.circuit_breakers.pop(str(project.id), None)

        await db_session.refresh(project)
        assert strategic_spawn.await_count == 0
        # The marker is preserved (parked, not consumed) so recovery resumes
        # once the failure latch decays.
        assert project.vision["_strategic_refresh"]["reason"] == (
            "strategic_cycle_starvation"
        )

    async def test_wall_clock_backstop_does_not_claim_fire_for_resident_owned_project(
        self, db_session, mocker, caplog
    ):
        """A disabled automatic lane cannot honestly claim a backstop firing."""
        from app.autonomous_research import _spawn_research_task
        from app.models import Task, TaskStatus

        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=True,
        )
        mocker.patch.dict(_ss.runtime_config, {"strategic_max_interval_minutes": 180})

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            build_config={"automatic_strategic_cycles_enabled": False},
            vision={"purpose": "resident-owned project", "_research_spawn_count": 1},
            directions=[],
        )
        db_session.add(project)
        await db_session.commit()
        db_session.add(
            Task(
                project_id=project.id,
                title="Old strategic cycle",
                payloads=[{"type": "strategic_review", "base_branch": "main"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(hours=4),
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
        await db_session.commit()

        with caplog.at_level("INFO", logger="app.autonomous_research"):
            await _spawn_research_task(project, db_session)

        assert "[STRATEGIC] Wall-clock backstop firing" not in caplog.text

    async def test_failure_breaker_decays_before_unavailable_account_return(
        self, db_session, mocker
    ):
        """The 48h decay runs before unavailable-account return."""
        from app.autonomous_research import _spawn_research_task_inner

        mocker.patch(
            "app.autonomous_research._research_route_has_available_account",
            return_value=False,
        )
        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            vision={
                "_strategic_failures": 3,
                "_strategic_failures_at": (
                    (now - timedelta(hours=49)).replace(tzinfo=timezone.utc).isoformat()
                ),
            },
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _spawn_research_task_inner(project, db_session)
        await db_session.refresh(project)

        assert project.vision.get("_strategic_failures") is None
        assert project.vision.get("_strategic_failures_at") is None

    async def test_failure_breaker_decays_before_zero_headroom_return(self, db_session):
        from app.autonomous_research import _spawn_research_task_inner
        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            vision={
                "_strategic_failures": 3,
                "_strategic_failures_at": (now - timedelta(hours=49)).replace(
                    tzinfo=timezone.utc
                ).isoformat(),
            },
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        assert await _spawn_research_task_inner(project, db_session, headroom_budget=0) == 0
        await db_session.refresh(project)
        assert project.vision.get("_strategic_failures") is None
        assert project.vision.get("_strategic_failures_at") is None


class TestResolveCiFixBranch:
    def test_prefers_review_loop_branch(self):
        assert (
            _orch._resolve_ci_fix_branch(
                {"branch": "blueprint/fix-1"},
                {"branch": "other"},
                {"head": {"ref": "pr-head"}},
            )
            == "blueprint/fix-1"
        )

    def test_falls_back_to_payload_branch(self):
        assert (
            _orch._resolve_ci_fix_branch(
                {}, {"branch": "payload-branch"}, {"head": {"ref": "pr-head"}}
            )
            == "payload-branch"
        )

    def test_falls_back_to_pr_head_ref_when_stored_empty(self):
        """The incident: both stored sources empty → must use the live PR head
        ref instead of producing an empty FixPayload.branch (which raises a
        ValueError every cycle and wedges the task as a poison pill)."""
        assert (
            _orch._resolve_ci_fix_branch(
                {"branch": ""}, {"branch": ""}, {"head": {"ref": "blueprint/task-abc"}}
            )
            == "blueprint/task-abc"
        )

    def test_all_empty_returns_empty_string(self):
        assert _orch._resolve_ci_fix_branch({}, {}, {}) == ""

    def test_handles_none_inputs(self):
        assert _orch._resolve_ci_fix_branch(None, None, None) == ""

    def test_missing_head_key_returns_empty(self):
        assert _orch._resolve_ci_fix_branch({}, {}, {"head": None}) == ""


class TestMarkProgress:
    def test_updates_progress_timestamp(self, restore_orch_state):
        _orch._orchestrator_last_progress = None
        _orch._mark_orchestrator_progress()
        assert _orch._orchestrator_last_progress is not None
        age = (_utcnow() - _orch._orchestrator_last_progress).total_seconds()
        assert age < 5


@pytest.mark.asyncio
class TestBoundedLeaderStep:
    """The post-cycle monitoring/idle tail runs AFTER the dispatch cycle's last
    heartbeat and used to have none of its own. ``_bounded_leader_step`` gives
    every tail step a heartbeat + a hard timeout so no single hung await can
    freeze ``_orchestrator_last_progress`` and let Cloud Run liveness-kill a
    still-progressing leader (incident 2026-06-15: a relock-starved post-commit
    sweep on one hot row wedged the tail → 503 → instance kill)."""

    async def test_normal_step_runs_and_pumps_heartbeat(self, restore_orch_state):
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=300)
        ran = False

        async def step():
            nonlocal ran
            ran = True

        await _orch._bounded_leader_step("ok", step(), budget=5)
        assert ran is True
        # heartbeat refreshed by the step
        assert (_utcnow() - _orch._orchestrator_last_progress).total_seconds() < 5

    async def test_hung_step_is_abandoned_not_fatal_and_heartbeats(
        self, restore_orch_state
    ):
        """A step that exceeds its budget must be abandoned (no raise) AND must
        still pump the heartbeat — the whole point is that a hung tail step keeps
        liveness fresh and lets the next cycle proceed instead of wedging."""
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=300)

        async def hang():
            # Block until abandoned at the budget — never returns on its own.
            await asyncio.Event().wait()

        # Must not raise.
        await _orch._bounded_leader_step("hang", hang(), budget=0.05)
        assert (_utcnow() - _orch._orchestrator_last_progress).total_seconds() < 5

    async def test_raising_step_propagates_but_still_heartbeats(
        self, restore_orch_state
    ):
        """A non-timeout exception propagates to the caller's existing
        consecutive-error backoff handler, but the heartbeat still fires in
        ``finally`` so a transient error never strands liveness."""
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=300)

        async def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError):
            await _orch._bounded_leader_step("boom", boom(), budget=5)
        assert (_utcnow() - _orch._orchestrator_last_progress).total_seconds() < 5


class TestWatchdogLiveness:
    """In-process background-task watchdog coverage.

    The watchdog in ``lifecycle.py`` cancels-and-restarts the orchestrator
    purely on ``evaluate_orchestrator_liveness(...).stalled``. Before the
    cluster-1 fix it judged liveness on full-cycle *completion* against a
    hardcoded 120s threshold while ``/health`` used the progress heartbeat —
    the two drifted and the watchdog killed healthy-but-slow cycles in a
    restart loop (incident 2026-05-28). These pin the watchdog and ``/health``
    to the SAME shared predicate so they can never disagree again.
    """

    def test_slow_but_progressing_cycle_is_not_stalled(self, restore_orch_state):
        """A cycle running far longer than any cycle-completion threshold but
        still heartbeating (fresh progress, stale cycle-completion timestamp)
        must NOT be cancelled by the watchdog."""
        now = _utcnow()
        # Cycle completion is ancient — pre-fix this tripped the 120s watchdog.
        _orch._orchestrator_last_cycle = now - timedelta(seconds=600)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=400)
        # ...but progress is fresh, so the orchestrator is healthy and alive.
        _orch._orchestrator_last_progress = now - timedelta(seconds=2)

        liveness = _orch.evaluate_orchestrator_liveness(now)
        assert liveness.stalled is False
        assert liveness.healthy is True
        assert liveness.alive is True

    def test_no_progress_past_threshold_is_stalled(self, restore_orch_state):
        """An orchestrator that has emitted no progress for longer than the
        hang threshold IS cancelled, preserving genuine-hang recovery."""
        now = _utcnow()
        stale = now - timedelta(seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 30)
        _orch._orchestrator_last_progress = stale
        _orch._orchestrator_last_cycle = stale
        _orch._orchestrator_cycle_started = stale

        liveness = _orch.evaluate_orchestrator_liveness(now)
        assert liveness.stalled is True
        assert liveness.healthy is False
        assert liveness.reason

    def test_startup_before_first_progress_is_not_stalled(self, restore_orch_state):
        """Cold start inside the threshold window: nothing recorded yet (or a
        cycle just started) must not be flagged as a hang to cancel."""
        now = _utcnow()
        _orch._orchestrator_last_progress = None
        _orch._orchestrator_last_cycle = None
        # Nothing has even started yet.
        _orch._orchestrator_cycle_started = None
        liveness = _orch.evaluate_orchestrator_liveness(now)
        assert liveness.stalled is False
        assert liveness.alive is False

        # First cycle started but not yet past the threshold → still sane.
        _orch._orchestrator_cycle_started = now - timedelta(
            seconds=_orch.ORCHESTRATOR_HANG_SECONDS - 10
        )
        liveness = _orch.evaluate_orchestrator_liveness(now)
        assert liveness.stalled is False

    def test_first_cycle_started_but_never_progressed_is_stalled(
        self, restore_orch_state
    ):
        """A first cycle that started but never emitted its first progress past
        the threshold is a wedged startup the watchdog must recover."""
        now = _utcnow()
        _orch._orchestrator_last_progress = None
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = now - timedelta(
            seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 30
        )

        liveness = _orch.evaluate_orchestrator_liveness(now)
        assert liveness.stalled is True
        assert liveness.reason


@pytest.mark.asyncio
class TestHealthLiveness:
    async def test_slow_cycle_with_fresh_progress_is_healthy(
        self, client, restore_orch_state
    ):
        """Core regression guard: a cycle that has been running far longer than
        60s but is still heartbeating (fresh progress) must report 200, not 503.
        Pre-fix this returned 503 and Cloud Run killed the container mid-cycle."""
        _make_leader_db_healthy()
        now = _utcnow()
        _orch._orchestrator_last_cycle = now - timedelta(seconds=300)  # stale cycle
        _orch._orchestrator_last_progress = now - timedelta(seconds=3)  # fresh hb
        _orch._orchestrator_cycle_started = now - timedelta(seconds=200)

        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orchestrator_alive"] is True
        # A slow cycle is surfaced as a degraded signal for alerting, but it does
        # NOT remove the instance from the LB or trigger a liveness kill.
        assert "orchestrator_slow_cycle" in body["degraded_deps"]
        assert body["orchestrator_progress_age_seconds"] < 60

    async def test_wedged_worker_no_progress_is_unhealthy(
        self, client, restore_orch_state
    ):
        """A genuinely wedged worker (no heartbeat for > hang threshold) still
        reports 503 so Cloud Run restarts it — the safety net is preserved."""
        _make_leader_db_healthy()
        now = _utcnow()
        stale = now - timedelta(seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 60)
        _orch._orchestrator_last_cycle = stale
        _orch._orchestrator_last_progress = stale

        resp = await client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unhealthy"

    async def test_cold_start_no_progress_is_unhealthy(
        self, client, restore_orch_state
    ):
        """Before the first cycle's first heartbeat, progress is None → 503
        (the liveness probe's initialDelaySeconds covers this window)."""
        _make_leader_db_healthy()
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_last_progress = None

        resp = await client.get("/health")
        assert resp.status_code == 503

    async def test_fresh_progress_falls_back_to_last_cycle(
        self, client, restore_orch_state
    ):
        """If progress is unset but a recent cycle completed, the cycle time is
        used as the heartbeat fallback (healthy)."""
        _make_leader_db_healthy()
        now = _utcnow()
        _orch._orchestrator_last_progress = None
        _orch._orchestrator_last_cycle = now - timedelta(seconds=5)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=5)

        resp = await client.get("/health")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestAuthedOrchestratorHealth:
    """The two AUTHENTICATED operator diagnostics — /api/debug/orchestrator and
    /api/health/detailed — must route their orchestrator verdict through the
    SAME shared progress-heartbeat predicate as /health and the watchdog, so a
    slow-but-progressing cycle is never misreported as down/hung while a
    genuinely wedged worker still is. Cycle-completion age stays only as the
    informational slow-cycle signal. See orchestrator-operator-health-split.
    """

    @pytest.fixture(autouse=True)
    def _use_local_leader_authority(self, restore_orch_state):
        _ss.is_leader = True

    async def test_debug_slow_but_progressing_is_alive_not_hung(
        self, client, restore_orch_state
    ):
        """Stale completed cycle but fresh heartbeat → alive, not hung. The
        cycle age is still surfaced as the informational slow-cycle signal."""
        now = _utcnow()
        _orch._orchestrator_last_cycle = now - timedelta(seconds=600)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=400)
        _orch._orchestrator_last_progress = now - timedelta(seconds=2)

        resp = await client.get("/api/debug/orchestrator")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orchestrator_alive"] is True
        assert body["orchestrator_hung"] is False
        # Heartbeat liveness is fresh...
        assert body["orchestrator_progress_age_seconds"] < 60
        # ...while cycle-completion age stays as the informational slow signal.
        assert body["last_cycle_seconds_ago"] >= 120
        assert body["orchestrator_cycle_age_seconds"] >= 120

    async def test_debug_wedged_worker_is_hung(self, client, restore_orch_state):
        """No heartbeat past the hang threshold → reported hung/not-alive."""
        now = _utcnow()
        stale = now - timedelta(seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 60)
        _orch._orchestrator_last_cycle = stale
        _orch._orchestrator_last_progress = stale
        _orch._orchestrator_cycle_started = stale

        resp = await client.get("/api/debug/orchestrator")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orchestrator_hung"] is True

    async def test_debug_wedged_first_cycle_is_hung(self, client, restore_orch_state):
        """A first cycle that started but never heartbeat past the threshold is
        a wedged startup → hung and not alive."""
        now = _utcnow()
        _orch._orchestrator_last_progress = None
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = now - timedelta(
            seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 30
        )

        resp = await client.get("/api/debug/orchestrator")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orchestrator_hung"] is True
        assert body["orchestrator_alive"] is False

    async def test_debug_api_role_uses_worker_lock_heartbeat(
        self, client, restore_orch_state, mocker
    ):
        """Split API diagnostics must not classify their empty local gauges."""
        _ss.is_leader = False
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_last_progress = None
        observed = mocker.patch(
            "app.main.observe_background_leader_health",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "authority": "background_leader_advisory_lock",
                    "cycle_age_seconds": None,
                    "cycle_age_kind": "not_observable_cross_process",
                    "heartbeat_age_seconds": 4.0,
                    "progress_age_seconds": 4.0,
                    "stale_after_seconds": 90.0,
                    "leader_pid": 123,
                    "reason": "background leader keepalive is fresh",
                }
            ),
        )

        resp = await client.get("/api/debug/orchestrator")

        assert resp.status_code == 200
        body = resp.json()
        assert body["orchestrator_alive"] is True
        assert body["orchestrator_hung"] is False
        assert body["orchestrator_progress_age_seconds"] == 4.0
        assert body["orchestrator_cycle_age_seconds"] is None
        assert body["orchestrator_authority"] == "background_leader_advisory_lock"
        observed.assert_awaited_once()

    async def test_detailed_slow_but_progressing_is_not_down(
        self, client, restore_orch_state
    ):
        """Stale completed cycle + fresh heartbeat → status degraded (alive but
        slow), NOT down. Progress age is surfaced alongside cycle age."""
        now = _utcnow()
        _orch._orchestrator_last_cycle = now - timedelta(seconds=600)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=400)
        _orch._orchestrator_last_progress = now - timedelta(seconds=2)

        resp = await client.get("/api/health/detailed")
        assert resp.status_code == 200
        orch = resp.json()["components"]["orchestrator"]
        assert orch["status"] in ("ok", "degraded")
        assert orch["status"] != "down"
        assert orch["cycle_age_seconds"] >= 120
        assert orch["progress_age_seconds"] < 60

    async def test_detailed_fresh_cycle_is_ok(self, client, restore_orch_state):
        """A fresh heartbeat with a fresh completed cycle → ok."""
        now = _utcnow()
        _orch._orchestrator_last_cycle = now - timedelta(seconds=3)
        _orch._orchestrator_cycle_started = now - timedelta(seconds=3)
        _orch._orchestrator_last_progress = now - timedelta(seconds=3)

        resp = await client.get("/api/health/detailed")
        assert resp.status_code == 200
        orch = resp.json()["components"]["orchestrator"]
        assert orch["status"] == "ok"

    async def test_detailed_wedged_worker_is_down(self, client, restore_orch_state):
        """No heartbeat past the hang threshold → status down."""
        now = _utcnow()
        stale = now - timedelta(seconds=_orch.ORCHESTRATOR_HANG_SECONDS + 60)
        _orch._orchestrator_last_cycle = stale
        _orch._orchestrator_last_progress = stale
        _orch._orchestrator_cycle_started = stale

        resp = await client.get("/api/health/detailed")
        assert resp.status_code == 200
        assert resp.json()["components"]["orchestrator"]["status"] == "down"

    async def test_detailed_api_role_uses_worker_lock_heartbeat(
        self, client, restore_orch_state, mocker
    ):
        """Split API processes must not classify their empty local gauges."""
        _ss.is_leader = False
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_last_progress = None
        observed = mocker.patch(
            "app.main.observe_background_leader_health",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "authority": "background_leader_advisory_lock",
                    "cycle_age_seconds": None,
                    "cycle_age_kind": "not_observable_cross_process",
                    "heartbeat_age_seconds": 4.0,
                    "progress_age_seconds": 4.0,
                    "stale_after_seconds": 90.0,
                    "leader_pid": 123,
                    "reason": "background leader keepalive is fresh",
                }
            ),
        )

        resp = await client.get("/api/health/detailed")

        assert resp.status_code == 200
        orch = resp.json()["components"]["orchestrator"]
        assert orch["status"] == "ok"
        assert orch["authority"] == "background_leader_advisory_lock"
        assert orch["heartbeat_age_seconds"] == 4.0
        assert orch["cycle_age_seconds"] is None
        observed.assert_awaited_once()


class TestHardWatchdogDecision:
    """The out-of-loop hard watchdog's *decision* (no thread): when does it
    declare the leader's event loop wedged past the hard bound?

    Prod 2026-06-15: a fully-blocked leader loop (sync CPU stall / total pool
    exhaustion) starves both the in-loop watchdog and /health, so only Cloud
    Run's slow probe recycled the container (~6-min wedge). The hard watchdog is
    the OS-thread backstop; this verifies its trigger reuses the shared liveness
    predicate with a LARGER bound, and never fires for a fresh/idle marker.
    """

    def test_fresh_heartbeat_not_aborted(self, restore_orch_state):
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = None
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=5)
        abort, reason = _orch.hard_watchdog_should_abort()
        assert abort is False
        assert reason == ""

    def test_stale_past_hard_bound_aborts(self, restore_orch_state):
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = None
        _orch._orchestrator_last_progress = _utcnow() - timedelta(
            seconds=_orch.ORCHESTRATOR_HARD_WATCHDOG_SECONDS + 30
        )
        abort, reason = _orch.hard_watchdog_should_abort()
        assert abort is True
        assert "no progress heartbeat" in reason

    def test_stale_within_graceful_window_not_hard_aborted(self, restore_orch_state):
        """Past the in-loop HANG bound but inside the hard bound: the graceful
        in-loop cancel-and-restart path owns this window — the hard watchdog must
        NOT pre-empt it (else it hard-kills a process the in-loop path would have
        recovered without a restart)."""
        assert (
            _orch.ORCHESTRATOR_HANG_SECONDS < _orch.ORCHESTRATOR_HARD_WATCHDOG_SECONDS
        )
        midpoint = (
            _orch.ORCHESTRATOR_HANG_SECONDS + _orch.ORCHESTRATOR_HARD_WATCHDOG_SECONDS
        ) / 2
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = None
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=midpoint)
        abort, _reason = _orch.hard_watchdog_should_abort()
        assert abort is False
        # ...yet the in-loop predicate DOES see it as stalled (graceful path active).
        assert _orch.evaluate_orchestrator_liveness().stalled is True

    def test_nothing_recorded_not_aborted(self, restore_orch_state):
        """Fresh process / follower: no heartbeat, no cycle started → not a wedge."""
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_last_progress = None
        _orch._orchestrator_cycle_started = None
        abort, reason = _orch.hard_watchdog_should_abort()
        assert abort is False
        assert reason == ""

    def test_hard_bound_strictly_above_hang_bound(self):
        """Invariant: the hard bound is clamped strictly above the in-loop bound
        so the graceful path always gets first chance."""
        assert (
            _orch.ORCHESTRATOR_HARD_WATCHDOG_SECONDS
            >= _orch.ORCHESTRATOR_HANG_SECONDS + 30
        )


class TestHardWatchdogStep:
    """One poll of the watchdog loop body (pure, no thread)."""

    def test_step_aborts_when_guarded_and_stale(self):
        calls = []
        fired = _orch._hard_watchdog_step(
            is_guarded=lambda: True,
            should_abort=lambda: (True, "wedged"),
            abort=lambda reason: calls.append(reason),
        )
        assert fired is True
        assert calls == ["wedged"]

    def test_step_skips_when_not_guarded(self):
        """Follower protection: a non-leader is never killed even if its (stale,
        leftover) marker would otherwise read as wedged."""
        calls = []
        fired = _orch._hard_watchdog_step(
            is_guarded=lambda: False,
            should_abort=lambda: (True, "wedged"),
            abort=lambda reason: calls.append(reason),
        )
        assert fired is False
        assert calls == []

    def test_step_swallows_should_abort_error(self):
        """The watchdog must never crash its own thread and leave the loop
        unguarded — an error in the decision is swallowed, no abort."""
        calls = []

        def boom():
            raise RuntimeError("transient read error")

        fired = _orch._hard_watchdog_step(
            is_guarded=lambda: True,
            should_abort=boom,
            abort=lambda reason: calls.append(reason),
        )
        assert fired is False
        assert calls == []


class TestHardWatchdogThread:
    """The real daemon thread: it observes module globals and fires the injected
    abort — proving it runs independently of the (here, un-blocked) event loop."""

    def test_thread_fires_on_stale_leader(self, restore_orch_state):
        # The application lifespan and other tests may already have started the
        # process-global watchdog.  ``start_*`` is intentionally idempotent, so
        # without isolating that state this test can inherit a thread whose
        # callbacks predate ``fake_abort`` and then wait forever for the wrong
        # Event.  Always prove a newly injected thread here.
        _orch.stop_orchestrator_hard_watchdog()
        _ss.is_leader = True
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = None
        _orch._orchestrator_last_progress = _utcnow() - timedelta(
            seconds=_orch.ORCHESTRATOR_HARD_WATCHDOG_SECONDS + 60
        )
        fired = threading.Event()
        captured = {}

        def fake_abort(reason):
            captured["reason"] = reason
            fired.set()

        try:
            _orch.start_orchestrator_hard_watchdog(
                is_guarded=lambda: _ss.is_leader,
                poll_interval=0.01,
                abort=fake_abort,
            )
            assert fired.wait(timeout=5), "watchdog did not fire on a stale leader"
            assert "no progress heartbeat" in captured["reason"]
        finally:
            _orch.stop_orchestrator_hard_watchdog()

    def test_thread_does_not_fire_for_follower(self, restore_orch_state):
        _orch.stop_orchestrator_hard_watchdog()
        _ss.is_leader = False  # follower: never guarded
        _orch._orchestrator_last_cycle = None
        _orch._orchestrator_cycle_started = None
        _orch._orchestrator_last_progress = _utcnow() - timedelta(seconds=10_000)
        fired = threading.Event()

        try:
            _orch.start_orchestrator_hard_watchdog(
                is_guarded=lambda: _ss.is_leader,
                poll_interval=0.01,
                abort=lambda reason: fired.set(),
            )
            assert not fired.wait(timeout=0.5), "watchdog must not kill a follower"
        finally:
            _orch.stop_orchestrator_hard_watchdog()
