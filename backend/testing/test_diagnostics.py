"""
Unit and integration tests for stuck-task diagnostic helpers.

Tests two areas:
1. _classify_stuck_failure() — keyword-based failure classification
2. _build_stuck_tasks()     — stuck detection for all 4 patterns
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.routers.admin_debug import (
    _classify_stuck_failure,
    _build_stuck_tasks,
    _build_direction_remediation_state,
)
from app.models import Project, Task, TaskStatus


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    """Naive UTC timestamp — matches DB convention (TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_task(
    project_id,
    status: TaskStatus,
    created_ago_secs: int = 0,
    started_ago_secs: int = 0,
    provisioned_ago_secs: int = 0,
    heartbeat_ago_secs: int = 0,
    no_heartbeat: bool = False,
    depends_on=None,
    status_message: str = "",
) -> Task:
    now = _utc_now()
    task = Task(
        project_id=project_id,
        title="test task",
        payloads=[],
        status=status,
        created_at=now - timedelta(seconds=created_ago_secs),
        latest_status_message=status_message,
        depends_on=depends_on,
    )
    if started_ago_secs:
        task.started_at = now - timedelta(seconds=started_ago_secs)
    # vm_provisioned_at is the orchestrator-side VM clock used by the
    # provision_timeout diagnostic. Default to started_ago_secs if not given
    # so existing tests that passed started_ago_secs still exercise the
    # pre-claim window.
    if provisioned_ago_secs:
        task.vm_provisioned_at = now - timedelta(seconds=provisioned_ago_secs)
    elif started_ago_secs and status == TaskStatus.PROVISIONING:
        task.vm_provisioned_at = now - timedelta(seconds=started_ago_secs)
    if not no_heartbeat and heartbeat_ago_secs:
        task.last_heartbeat = now - timedelta(seconds=heartbeat_ago_secs)
    return task


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — _classify_stuck_failure()
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyStuckFailure:
    """Pure unit tests — no DB needed."""

    def _task_with_message(self, msg: str) -> Task:
        t = MagicMock(spec=Task)
        t.latest_status_message = msg
        return t

    def test_provision_keywords(self):
        for kw in ["provision error", "quota exceeded", "vm failed to start", "connection refused", "sshd failed", "startup script failed"]:
            assert _classify_stuck_failure(self._task_with_message(kw)) == "PROVISION_FAILURE", kw

    def test_provision_keywords_previously_missing(self):
        # cpus_all_regions was missing from old lightweight classifier
        assert _classify_stuck_failure(self._task_with_message("cpus_all_regions quota exceeded")) == "PROVISION_FAILURE"

    def test_rate_limit_keywords(self):
        # Exact verbatim Claude Code CLI / Anthropic API rate-limit strings — see RATE_LIMIT_PATTERNS.
        for kw in [
            "You've hit your limit · resets 5am (Asia/Seoul)",
            "You've hit your usage limit - View usage",
            "Claude AI usage limit reached, please try again after 2pm",
            "You're out of extra usage · resets 11:30am (Asia/Colombo)",
            "5-hour limit reached - resets 9pm",
            "API Error: Rate limit reached",
            "This request would exceed your account's rate limit. Please try again later.",
        ]:
            assert _classify_stuck_failure(self._task_with_message(kw)) == "RATE_LIMIT", kw

    def test_external_api_keywords(self):
        for kw in ["api.github.com error", "api.anthropic.com down", "502 bad gateway", "503 service unavailable", "ssl handshake", "certificate expired"]:
            assert _classify_stuck_failure(self._task_with_message(kw)) == "EXTERNAL_API", kw

    def test_overloaded_is_external_api(self):
        # Anthropic 529 is server-side capacity — not a Claude rate limit, must not disable accounts.
        assert _classify_stuck_failure(self._task_with_message("overloaded server")) == "EXTERNAL_API"
        assert _classify_stuck_failure(self._task_with_message("Error: Overloaded")) == "EXTERNAL_API"

    def test_non_claude_rate_limit_words_are_not_rate_limit(self):
        # Broad keywords that previously caused false-positive account disables.
        # None of these are real Claude rate-limit messages; classifier must not flag them.
        from app.failure_classification import classify_failure
        for msg, expected in [
            ("credit card declined", "CODE_ERROR"),                       # generic "credit"
            ("billing address required", "CODE_ERROR"),                   # generic "billing"
            ("GitHub API rate limit exceeded", "EXTERNAL_API"),            # GitHub 429
            ("too many requests to downstream service", "CODE_ERROR"),    # generic 429 phrase
            ("HTTP 429 from upstream", "CODE_ERROR"),                     # bare 429
            ("disk quota exceeded on /var", "PROVISION_FAILURE"),         # GCP/disk quota
        ]:
            t = self._task_with_message(msg)
            assert classify_failure(t) == expected, f"{msg!r} → expected {expected}"

    def test_agent_timeout_keywords(self):
        for kw in ["no heartbeat received", "reaped by watchdog", "execution timed out", "node process killed", "oom-kill", "killed by oom", "sigterm received", "exit code 137", "exit code 143"]:
            assert _classify_stuck_failure(self._task_with_message(kw)) == "AGENT_TIMEOUT", kw

    def test_agent_timeout_keywords_previously_missing(self):
        # exit code 124 (timeout) and exit code 139 (segfault) were missing from old classifier
        assert _classify_stuck_failure(self._task_with_message("exit code 124")) == "AGENT_TIMEOUT"
        assert _classify_stuck_failure(self._task_with_message("exit code 139")) == "AGENT_TIMEOUT"

    def test_empty_message_returns_none(self):
        assert _classify_stuck_failure(self._task_with_message("")) is None

    def test_unrelated_message_returns_none(self):
        # CODE_ERROR is suppressed → returns None for stuck tasks
        assert _classify_stuck_failure(self._task_with_message("task completed successfully")) is None

    def test_none_message_returns_none(self):
        t = MagicMock(spec=Task)
        t.latest_status_message = None
        assert _classify_stuck_failure(t) is None

    def test_case_insensitive(self):
        assert _classify_stuck_failure(self._task_with_message("API ERROR: RATE LIMIT REACHED")) == "RATE_LIMIT"
        assert _classify_stuck_failure(self._task_with_message("VM FAILED TO START")) == "PROVISION_FAILURE"

    def test_cascade_cancellation_messages(self):
        """Cascade-cancelled children should be AGENT_TIMEOUT, not CODE_ERROR."""
        from app.failure_classification import classify_failure
        cases = [
            ("Cancelled: parent evaluation timeout after 2.0h", "AGENT_TIMEOUT"),
            ("Cancelled: parent CI wait backstop timeout after 1.9h (reason)", "AGENT_TIMEOUT"),
            ("Cancelled: parent dependency failed", "AGENT_TIMEOUT"),
        ]
        for msg, expected in cases:
            t = self._task_with_message(msg)
            assert classify_failure(t) == expected, f"{msg!r} → expected {expected}"


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — _build_stuck_tasks()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildStuckTasks:
    """Integration tests using a real DB session."""

    # ── provision_timeout ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_provision_timeout_detected(self, db_session):
        """PROVISIONING task with no heartbeat and vm_provisioned_at >6 min ago is detected."""
        project = Project(name="diag-test", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.PROVISIONING,
                          provisioned_ago_secs=8 * 60, no_heartbeat=True)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        now = _utc_now()
        result = await _build_stuck_tasks([task], now, db_session)

        assert len(result) == 1
        assert result[0]["stuck_reason"] == "provision_timeout"
        assert result[0]["stuck_for_seconds"] == pytest.approx(2 * 60, abs=5)

    @pytest.mark.asyncio
    async def test_provision_timeout_not_triggered_when_too_young(self, db_session):
        """PROVISIONING task whose VM was provisioned only 3 min ago must NOT be flagged."""
        project = Project(name="diag-test2", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.PROVISIONING,
                          provisioned_ago_secs=3 * 60, no_heartbeat=True)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_provision_timeout_not_triggered_when_heartbeat_present(self, db_session):
        """PROVISIONING task that already has a heartbeat must NOT be flagged."""
        project = Project(name="diag-test3", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.PROVISIONING,
                          provisioned_ago_secs=8 * 60, heartbeat_ago_secs=60)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    # ── no_heartbeat ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_heartbeat_detected(self, db_session):
        """IN_PROGRESS task with last heartbeat >5 min ago is detected."""
        project = Project(name="diag-hb", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.IN_PROGRESS,
                          heartbeat_ago_secs=8 * 60)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)

        assert len(result) == 1
        assert result[0]["stuck_reason"] == "no_heartbeat"
        assert result[0]["stuck_for_seconds"] == pytest.approx(3 * 60, abs=5)

    @pytest.mark.asyncio
    async def test_no_heartbeat_not_triggered_when_fresh(self, db_session):
        """IN_PROGRESS task with recent heartbeat must NOT be flagged."""
        project = Project(name="diag-hb2", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.IN_PROGRESS, heartbeat_ago_secs=60)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_heartbeat_not_triggered_when_no_heartbeat_field(self, db_session):
        """IN_PROGRESS task that never set a heartbeat is not classified as no_heartbeat."""
        project = Project(name="diag-hb3", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.IN_PROGRESS, no_heartbeat=True)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    # ── long_queued ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_long_queued_detected(self, db_session):
        """QUEUED task with no deps created >30 min ago is detected."""
        project = Project(name="diag-lq", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=35 * 60)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)

        assert len(result) == 1
        assert result[0]["stuck_reason"] == "long_queued"
        assert result[0]["stuck_for_seconds"] == pytest.approx(5 * 60, abs=5)

    @pytest.mark.asyncio
    async def test_long_queued_not_triggered_when_young(self, db_session):
        """QUEUED task created only 10 min ago must NOT be flagged as long_queued."""
        project = Project(name="diag-lq2", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=10 * 60)
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    # ── stuck_deps ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stuck_deps_detected_when_all_deps_failed(self, db_session):
        """QUEUED task whose every dep is FAILED and is old enough → stuck_deps."""
        project = Project(name="diag-sd", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_task = Task(
            project_id=project.id, title="dep", payloads=[], status=TaskStatus.FAILED
        )
        db_session.add(dep_task)
        await db_session.commit()
        await db_session.refresh(dep_task)

        waiting_task = _make_task(
            project.id, TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_task.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)

        assert len(result) == 1
        assert result[0]["stuck_reason"] == "stuck_deps"
        assert str(dep_task.id) in result[0]["unresolvable_deps"]

    @pytest.mark.asyncio
    async def test_stuck_deps_detected_when_all_deps_cancelled(self, db_session):
        """CANCELLED deps also count as terminal failures for stuck_deps detection."""
        project = Project(name="diag-sd-cancel", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_task = Task(
            project_id=project.id, title="dep", payloads=[], status=TaskStatus.CANCELLED
        )
        db_session.add(dep_task)
        await db_session.commit()
        await db_session.refresh(dep_task)

        waiting_task = _make_task(
            project.id, TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_task.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)

        assert len(result) == 1
        assert result[0]["stuck_reason"] == "stuck_deps"

    @pytest.mark.asyncio
    async def test_stuck_deps_not_triggered_when_dep_still_running(self, db_session):
        """QUEUED task with an IN_PROGRESS dep must NOT be flagged as stuck_deps."""
        project = Project(name="diag-sd2", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_task = Task(
            project_id=project.id, title="dep", payloads=[], status=TaskStatus.IN_PROGRESS
        )
        db_session.add(dep_task)
        await db_session.commit()
        await db_session.refresh(dep_task)

        waiting_task = _make_task(
            project.id, TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_task.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_stuck_deps_not_triggered_when_task_too_young(self, db_session):
        """QUEUED task with failed deps but created only 5 min ago is NOT stuck yet."""
        project = Project(name="diag-sd3", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_task = Task(
            project_id=project.id, title="dep", payloads=[], status=TaskStatus.FAILED
        )
        db_session.add(dep_task)
        await db_session.commit()
        await db_session.refresh(dep_task)

        waiting_task = _make_task(
            project.id, TaskStatus.QUEUED,
            created_ago_secs=5 * 60,  # under the 10-min threshold
            depends_on=[str(dep_task.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_stuck_deps_not_triggered_when_only_some_deps_failed(self, db_session):
        """Task with one FAILED dep and one QUEUED dep must NOT be flagged."""
        project = Project(name="diag-sd4", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_failed = Task(
            project_id=project.id, title="failed dep", payloads=[], status=TaskStatus.FAILED
        )
        dep_active = Task(
            project_id=project.id, title="active dep", payloads=[], status=TaskStatus.QUEUED
        )
        db_session.add_all([dep_failed, dep_active])
        await db_session.commit()
        await db_session.refresh(dep_failed)
        await db_session.refresh(dep_active)

        waiting_task = _make_task(
            project.id, TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_failed.id), str(dep_active.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)
        assert result == []

    # ── batch query — multiple QUEUED tasks with deps ─────────────────────────

    @pytest.mark.asyncio
    async def test_multiple_queued_tasks_with_deps_use_single_db_query(self, db_session, mocker):
        """
        When multiple QUEUED tasks have dependencies, all dep statuses should be
        fetched in a single query rather than one per task (N+1 prevention).
        """
        project = Project(name="diag-batch", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create two separate dependency chains, both fully failed
        dep_a = Task(project_id=project.id, title="dep_a", payloads=[], status=TaskStatus.FAILED)
        dep_b = Task(project_id=project.id, title="dep_b", payloads=[], status=TaskStatus.FAILED)
        db_session.add_all([dep_a, dep_b])
        await db_session.commit()
        await db_session.refresh(dep_a)
        await db_session.refresh(dep_b)

        task_a = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=15 * 60,
                            depends_on=[str(dep_a.id)])
        task_b = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=15 * 60,
                            depends_on=[str(dep_b.id)])
        db_session.add_all([task_a, task_b])
        await db_session.commit()
        await db_session.refresh(task_a)
        await db_session.refresh(task_b)

        # Spy on session.execute to count query calls
        original_execute = db_session.execute
        call_count = 0

        async def counting_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            return await original_execute(stmt, *args, **kwargs)

        mocker.patch.object(db_session, "execute", side_effect=counting_execute)

        result = await _build_stuck_tasks([task_a, task_b], _utc_now(), db_session)

        assert len(result) == 2, "Both tasks should be detected as stuck"
        assert call_count == 1, (
            f"Expected exactly 1 DB query for dependency lookup, got {call_count}"
        )

    # ── output fields ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_output_contains_all_required_fields(self, db_session):
        """Each stuck entry must contain the documented set of fields."""
        project = Project(name="diag-fields", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=35 * 60,
                          status_message="API Error: Rate limit reached")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)

        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {
            "task_id", "title", "status", "stuck_reason",
            "stuck_for_seconds", "attempt_count", "failure_class", "unresolvable_deps",
            "remediation_hint",
        }
        assert entry["failure_class"] == "RATE_LIMIT"
        assert entry["unresolvable_deps"] == []

    @pytest.mark.asyncio
    async def test_non_stuck_tasks_return_empty(self, db_session):
        """A mix of healthy tasks produces an empty result."""
        project = Project(name="diag-healthy", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        healthy_queued = _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=5 * 60)
        healthy_in_progress = _make_task(project.id, TaskStatus.IN_PROGRESS, heartbeat_ago_secs=60)
        db_session.add_all([healthy_queued, healthy_in_progress])
        await db_session.commit()

        result = await _build_stuck_tasks(
            [healthy_queued, healthy_in_progress], _utc_now(), db_session
        )
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — _build_direction_remediation_state()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildDirectionRemediationState:
    """Pure unit tests — operates on Project.directions (JSON)."""

    def _project(self, directions):
        p = MagicMock(spec=Project)
        p.directions = directions
        return p

    def test_omits_directions_with_no_notable_state(self):
        project = self._project([
            {"id": "d1", "text": "clean direction", "status": "active"},
            {"id": "d2", "text": "also clean", "status": "proposed"},
        ])
        assert _build_direction_remediation_state(project) == []

    def test_surfaces_auto_paused_direction(self):
        project = self._project([{
            "id": "d1", "text": "paused dir", "status": "paused",
            "auto_paused": True, "paused_reason": "High code failure rate: 0.75",
            "paused_at": "2026-04-10T00:00:00", "auto_pause_count": 2,
        }])
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        e = entries[0]
        assert e["direction_id"] == "d1"
        assert e["auto_paused"] is True
        assert e["paused_reason"] == "High code failure rate: 0.75"
        assert e["auto_pause_count"] == 2
        assert e["needs_manual_intervention"] is False

    def test_surfaces_active_remediation_monitor(self):
        project = self._project([{
            "id": "d1", "text": "monitoring", "status": "active",
            "remediation_monitor_started_at": "2026-04-15T00:00:00",
            "remediation_attempts": 1,
            "last_remediation_result": "merged",
            "error_fingerprint": "abc123",
        }])
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        e = entries[0]
        assert e["remediation_monitor_active"] is True
        assert e["remediation_attempts"] == 1
        assert e["last_remediation_result"] == "merged"
        assert e["error_fingerprint"] == "abc123"

    def test_surfaces_needs_manual_intervention(self):
        project = self._project([{
            "id": "d1", "text": "stuck", "status": "paused",
            "needs_manual_intervention": True,
            "remediation_attempts": 3,
            "last_remediation_result": "failed",
        }])
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        assert entries[0]["needs_manual_intervention"] is True
        assert entries[0]["remediation_attempts"] == 3

    def test_mixed_directions_only_notable_returned(self):
        project = self._project([
            {"id": "clean", "text": "ok", "status": "active"},
            {"id": "paused", "text": "paused", "status": "paused", "auto_paused": True},
            {"id": "escalated", "text": "stuck", "status": "paused",
             "needs_manual_intervention": True, "remediation_attempts": 3},
        ])
        entries = _build_direction_remediation_state(project)
        ids = {e["direction_id"] for e in entries}
        assert ids == {"paused", "escalated"}

    def test_handles_empty_or_missing_directions(self):
        assert _build_direction_remediation_state(self._project([])) == []
        assert _build_direction_remediation_state(self._project(None)) == []
