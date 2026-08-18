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
from sqlmodel import select

import app.orchestrator as _orch
import app.orchestrator_liveness as _liv
import app.shared_state as _ss
from app.time_utils import _utcnow
from app.models import Alert, StrategicCycle
from testing.factories import make_project


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
        last_completed = now - timedelta(hours=25)
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
                phase1_started_at=now - timedelta(hours=25),
                outcome="phase1_running",
            )
        )
        await db_session.commit()

        routed = []

        async def record_broadcast(project_id, event, payload):
            routed.append((project_id, event, payload))

        monkeypatch.setattr("app.alert_dispatch.broadcast_to_project", record_broadcast)
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
        assert f"last_completed_at={last_completed.isoformat()}" in alert.message
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

    async def test_quiet_healthy_portfolio_does_not_raise_signal(self, db_session):
        """A recent intentional no-delta receipt is healthy idle, not a halt."""
        from app.reaper_sweeps import (
            _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
            _sweep_strategic_cycle_starvation,
        )

        now = _utcnow()
        project = make_project(
            autonomous_enabled=True,
            created_at=now - timedelta(days=3),
            vision={
                "_strategic_no_delta_skips": [
                    {"skipped_at": (now - timedelta(hours=1)).isoformat()}
                ]
            },
        )
        db_session.add(project)
        await db_session.commit()

        assert await _sweep_strategic_cycle_starvation(db_session, now) == 0
        assert not (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == _STRATEGIC_CYCLE_LIVENESS_ALERT_TITLE,
                )
            )
        ).scalars().first()

    async def test_failure_breaker_decays_before_early_return(self, db_session):
        """The 48h decay runs before unavailable-account return, reopening backstop eligibility."""
        from app.autonomous_research import _spawn_research_task_inner

        now = _utcnow()
        project = make_project(
            autonomous_enabled=False,
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
