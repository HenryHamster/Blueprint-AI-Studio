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

from datetime import timedelta

import pytest

import app.orchestrator as _orch
import app.shared_state as _ss
from app.time_utils import _utcnow


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
