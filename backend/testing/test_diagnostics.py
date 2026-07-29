"""
Unit and integration tests for stuck-task diagnostic helpers.

Tests two areas:
1. _classify_stuck_failure() — keyword-based failure classification
2. _build_stuck_tasks()     — stuck detection for all 4 patterns
"""

import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.routers.admin_debug import (
    _classify_stuck_failure,
    _build_stuck_tasks,
    _build_direction_portfolio_summary,
    _build_direction_remediation_state,
    _build_problem_model_trim_verdict,
    get_global_diagnostics,
    get_project_diagnostics,
    DIAGNOSTICS_TOP_N,
    _MAX_ACTIVE_TASKS_DEFAULT,
)
from app.models import Project, ProjectMember, Task, TaskStatus

# The /diagnostics endpoint filters projects with func.jsonb_array_length(...),
# which SQLite does not implement — these endpoint tests require real Postgres
# (CI full suite / `bash scripts/test-backend.sh testing/test_diagnostics.py`).
USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


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
        # NB: "quota exceeded" alone used to match the provision bank, but
        # bare "quota" was removed because it mis-caught LLM-provider quota
        # errors. Use a specific GCP-quota phrase that still routes here.
        for kw in [
            "provision error",
            "CPUS_ALL_REGIONS quota exceeded",
            "vm failed to start",
            "connection refused",
            "sshd failed",
            "startup script failed",
        ]:
            assert (
                _classify_stuck_failure(self._task_with_message(kw))
                == "PROVISION_FAILURE"
            ), kw

    def test_provision_keywords_previously_missing(self):
        # cpus_all_regions was missing from old lightweight classifier
        assert (
            _classify_stuck_failure(
                self._task_with_message("cpus_all_regions quota exceeded")
            )
            == "PROVISION_FAILURE"
        )

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
            assert (
                _classify_stuck_failure(self._task_with_message(kw)) == "RATE_LIMIT"
            ), kw

    def test_external_api_keywords(self):
        # NB: the "ssl"/"401" keywords are anchored ("ssl handshake failed" /
        # "http 401") so a benign log line that merely *contains* the substring
        # "ssl" or the digits "401" is no longer misrouted to EXTERNAL_API.
        # Genuine TLS-handshake / HTTP-401 failures still classify here.
        for kw in [
            "api.github.com error",
            "api.anthropic.com down",
            "502 bad gateway",
            "503 service unavailable",
            "ssl handshake failed",
            "certificate expired",
            "Request failed: HTTP 401",
        ]:
            assert (
                _classify_stuck_failure(self._task_with_message(kw)) == "EXTERNAL_API"
            ), kw

    def test_benign_ssl_or_401_substring_not_external_api(self):
        # Anchored "ssl handshake failed"/"http 401": a benign line that merely
        # mentions SSL/TLS or contains the digits 401 (a count/id) must NOT be
        # misrouted to EXTERNAL_API. _classify_stuck_failure maps the CODE_ERROR
        # default to None, so an unclassified benign line returns None here.
        for kw in [
            "reused cached ssl context but expected a fresh one",
            "openssl module loaded; verifying signature",
            "compiled 401 modules but expected 402",
            "record 401 missing required field",
        ]:
            assert (
                _classify_stuck_failure(self._task_with_message(kw)) != "EXTERNAL_API"
            ), kw

    def test_overloaded_is_external_api(self):
        # Anthropic 529 is server-side capacity — not a Claude rate limit, must not disable accounts.
        assert (
            _classify_stuck_failure(self._task_with_message("overloaded server"))
            == "EXTERNAL_API"
        )
        assert (
            _classify_stuck_failure(self._task_with_message("Error: Overloaded"))
            == "EXTERNAL_API"
        )

    def test_non_claude_rate_limit_words_are_not_rate_limit(self):
        # Broad keywords that previously caused false-positive account disables.
        # None of these are real Claude rate-limit messages; classifier must not flag them.
        from app.failure_classification import classify_failure

        for msg, expected in [
            ("credit card declined", "CODE_ERROR"),  # generic "credit"
            ("billing address required", "CODE_ERROR"),  # generic "billing"
            ("GitHub API rate limit exceeded", "EXTERNAL_API"),  # GitHub 429
            (
                "too many requests to downstream service",
                "CODE_ERROR",
            ),  # generic 429 phrase
            ("HTTP 429 from upstream", "CODE_ERROR"),  # bare 429
            ("disk quota exceeded on /var", "PROVISION_FAILURE"),  # GCP/disk quota
        ]:
            t = self._task_with_message(msg)
            assert classify_failure(t) == expected, f"{msg!r} → expected {expected}"

    def test_agent_timeout_keywords(self):
        for kw in [
            "no heartbeat received",
            "reaped by watchdog",
            "execution timed out",
            "node process killed",
            "oom-kill",
            "killed by oom",
            "sigterm received",
            "exit code 137",
            "exit code 143",
        ]:
            assert (
                _classify_stuck_failure(self._task_with_message(kw)) == "AGENT_TIMEOUT"
            ), kw

    def test_agent_timeout_keywords_previously_missing(self):
        # exit code 124 (timeout) and exit code 139 (segfault) were missing from old classifier
        assert (
            _classify_stuck_failure(self._task_with_message("exit code 124"))
            == "AGENT_TIMEOUT"
        )
        assert (
            _classify_stuck_failure(self._task_with_message("exit code 139"))
            == "AGENT_TIMEOUT"
        )

    def test_empty_message_returns_none(self):
        assert _classify_stuck_failure(self._task_with_message("")) is None

    def test_unrelated_message_returns_none(self):
        # CODE_ERROR is suppressed → returns None for stuck tasks
        assert (
            _classify_stuck_failure(
                self._task_with_message("task completed successfully")
            )
            is None
        )

    def test_none_message_returns_none(self):
        t = MagicMock(spec=Task)
        t.latest_status_message = None
        assert _classify_stuck_failure(t) is None

    def test_case_insensitive(self):
        assert (
            _classify_stuck_failure(
                self._task_with_message("API ERROR: RATE LIMIT REACHED")
            )
            == "RATE_LIMIT"
        )
        assert (
            _classify_stuck_failure(self._task_with_message("VM FAILED TO START"))
            == "PROVISION_FAILURE"
        )

    def test_cascade_cancellation_messages(self):
        """Cascade-cancelled children should be ADMIN_CANCEL, not CODE_ERROR."""
        from app.failure_classification import classify_failure

        cases = [
            ("Cancelled: parent evaluation timeout after 2.0h", "ADMIN_CANCEL"),
            (
                "Cancelled: parent CI wait backstop timeout after 1.9h (reason)",
                "ADMIN_CANCEL",
            ),
            ("Cancelled: parent dependency failed", "ADMIN_CANCEL"),
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

        task = _make_task(
            project.id,
            TaskStatus.PROVISIONING,
            provisioned_ago_secs=8 * 60,
            no_heartbeat=True,
        )
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

        task = _make_task(
            project.id,
            TaskStatus.PROVISIONING,
            provisioned_ago_secs=3 * 60,
            no_heartbeat=True,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_provision_timeout_not_triggered_when_heartbeat_present(
        self, db_session
    ):
        """PROVISIONING task that already has a heartbeat must NOT be flagged."""
        project = Project(name="diag-test3", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(
            project.id,
            TaskStatus.PROVISIONING,
            provisioned_ago_secs=8 * 60,
            heartbeat_ago_secs=60,
        )
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

        task = _make_task(project.id, TaskStatus.IN_PROGRESS, heartbeat_ago_secs=8 * 60)
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

    @pytest.mark.asyncio
    async def test_no_heartbeat_uses_scope_aware_threshold(self, db_session):
        """Large-scope task with 8-min heartbeat silence is NOT stuck (threshold=15min)."""
        project = Project(name="diag-hb-scope", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(project.id, TaskStatus.IN_PROGRESS, heartbeat_ago_secs=8 * 60)
        task.context = {"scope": "large"}
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
            project.id,
            TaskStatus.QUEUED,
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
            project.id,
            TaskStatus.QUEUED,
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
            project_id=project.id,
            title="dep",
            payloads=[],
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(dep_task)
        await db_session.commit()
        await db_session.refresh(dep_task)

        waiting_task = _make_task(
            project.id,
            TaskStatus.QUEUED,
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
            project.id,
            TaskStatus.QUEUED,
            created_ago_secs=5 * 60,  # under the 10-min threshold
            depends_on=[str(dep_task.id)],
        )
        db_session.add(waiting_task)
        await db_session.commit()
        await db_session.refresh(waiting_task)

        result = await _build_stuck_tasks([waiting_task], _utc_now(), db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_stuck_deps_not_triggered_when_only_some_deps_failed(
        self, db_session
    ):
        """Task with one FAILED dep and one QUEUED dep must NOT be flagged."""
        project = Project(name="diag-sd4", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep_failed = Task(
            project_id=project.id,
            title="failed dep",
            payloads=[],
            status=TaskStatus.FAILED,
        )
        dep_active = Task(
            project_id=project.id,
            title="active dep",
            payloads=[],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep_failed, dep_active])
        await db_session.commit()
        await db_session.refresh(dep_failed)
        await db_session.refresh(dep_active)

        waiting_task = _make_task(
            project.id,
            TaskStatus.QUEUED,
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
    async def test_multiple_queued_tasks_with_deps_use_single_db_query(
        self, db_session, mocker
    ):
        """
        When multiple QUEUED tasks have dependencies, all dep statuses should be
        fetched in a single query rather than one per task (N+1 prevention).
        """
        project = Project(name="diag-batch", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create two separate dependency chains, both fully failed
        dep_a = Task(
            project_id=project.id, title="dep_a", payloads=[], status=TaskStatus.FAILED
        )
        dep_b = Task(
            project_id=project.id, title="dep_b", payloads=[], status=TaskStatus.FAILED
        )
        db_session.add_all([dep_a, dep_b])
        await db_session.commit()
        await db_session.refresh(dep_a)
        await db_session.refresh(dep_b)

        task_a = _make_task(
            project.id,
            TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_a.id)],
        )
        task_b = _make_task(
            project.id,
            TaskStatus.QUEUED,
            created_ago_secs=15 * 60,
            depends_on=[str(dep_b.id)],
        )
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "child_status",
        [
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
            TaskStatus.BLOCKED,
        ],
    )
    async def test_evaluating_parent_with_active_child_is_not_stuck(
        self, db_session, child_status
    ):
        """An old parent is progressing while any pipeline child is live."""
        project = Project(name=f"diag-eval-{child_status}", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        parent = _make_task(
            project.id,
            TaskStatus.EVALUATING,
            created_ago_secs=4 * 3600,
            started_ago_secs=4 * 3600,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=parent.id,
                title="live pipeline child",
                payloads=[{"type": "fix"}],
                status=child_status,
            )
        )
        await db_session.commit()

        result = await _build_stuck_tasks([parent], _utc_now(), db_session)
        assert result == []
        await db_session.refresh(parent)
        assert parent.status == TaskStatus.EVALUATING

    @pytest.mark.asyncio
    async def test_evaluating_parent_with_only_terminal_children_is_stuck(
        self, db_session
    ):
        """Terminal children do not hide a genuinely stale EVALUATING parent."""
        project = Project(name="diag-eval-terminal", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        parent = _make_task(
            project.id,
            TaskStatus.EVALUATING,
            created_ago_secs=4 * 3600,
            started_ago_secs=4 * 3600,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)
        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=parent.id,
                title="finished pipeline child",
                payloads=[{"type": "fix"}],
                status=TaskStatus.COMPLETED,
            )
        )
        await db_session.commit()

        result = await _build_stuck_tasks([parent], _utc_now(), db_session)
        assert len(result) == 1
        assert result[0]["stuck_reason"] == "evaluation_timeout"

    @pytest.mark.asyncio
    async def test_approval_gated_child_does_not_hide_stale_parent(
        self, db_session
    ):
        """Operator-gated descendants cannot make an ancestor look live."""
        project = Project(name="diag-eval-gated", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()

        parent = _make_task(
            project.id,
            TaskStatus.EVALUATING,
            created_ago_secs=4 * 3600,
            started_ago_secs=4 * 3600,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)
        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=parent.id,
                title="operator-gated pipeline child",
                payloads=[{"type": "fix"}],
                status=TaskStatus.BLOCKED,
                requires_approval=True,
            )
        )
        await db_session.commit()

        result = await _build_stuck_tasks([parent], _utc_now(), db_session)
        assert len(result) == 1
        assert result[0]["stuck_reason"] == "evaluation_timeout"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sibling_status, expected_stuck",
        [
            (TaskStatus.IN_PROGRESS, False),
            (TaskStatus.COMPLETED, True),
        ],
    )
    @pytest.mark.parametrize("identity_surface", ["payload", "context"])
    async def test_evaluating_parent_uses_review_chain_sibling_liveness(
        self, db_session, sibling_status, expected_stuck, identity_surface
    ):
        """Review/fix siblings are linked by durable original-task identity."""
        project = Project(
            name=f"diag-eval-chain-{sibling_status}-{identity_surface}",
            user_id="test-user-123",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_root = _make_task(project.id, TaskStatus.EVALUATING)
        db_session.add(research_root)
        await db_session.commit()
        await db_session.refresh(research_root)

        parent = _make_task(
            project.id,
            TaskStatus.EVALUATING,
            created_ago_secs=4 * 3600,
            started_ago_secs=4 * 3600,
        )
        parent.parent_task_id = research_root.id
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        db_session.add(
            Task(
                project_id=project.id,
                parent_task_id=research_root.id,
                title="migration fix sibling",
                payloads=[
                    {
                        "type": "fix",
                        **(
                            {"original_task_id": str(parent.id)}
                            if identity_surface == "payload"
                            else {}
                        ),
                    }
                ],
                context=(
                    {"review_loop": {"original_task_id": str(parent.id)}}
                    if identity_surface == "context"
                    else {}
                ),
                status=sibling_status,
            )
        )
        await db_session.commit()

        result = await _build_stuck_tasks([parent], _utc_now(), db_session)
        if expected_stuck:
            assert len(result) == 1
            assert result[0]["stuck_reason"] == "evaluation_timeout"
        else:
            assert result == []

    # ── output fields ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_output_contains_all_required_fields(self, db_session):
        """Each stuck entry must contain the documented set of fields."""
        project = Project(name="diag-fields", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(
            project.id,
            TaskStatus.QUEUED,
            created_ago_secs=35 * 60,
            status_message="API Error: Rate limit reached",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _build_stuck_tasks([task], _utc_now(), db_session)

        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {
            "task_id",
            "title",
            "status",
            "stuck_reason",
            "stuck_for_seconds",
            "attempt_count",
            "failure_class",
            "unresolvable_deps",
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

        healthy_queued = _make_task(
            project.id, TaskStatus.QUEUED, created_ago_secs=5 * 60
        )
        healthy_in_progress = _make_task(
            project.id, TaskStatus.IN_PROGRESS, heartbeat_ago_secs=60
        )
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
        project = self._project(
            [
                {"id": "d1", "text": "clean direction", "status": "active"},
                {"id": "d2", "text": "also clean", "status": "proposed"},
            ]
        )
        assert _build_direction_remediation_state(project) == []

    def test_surfaces_auto_paused_direction(self):
        project = self._project(
            [
                {
                    "id": "d1",
                    "text": "paused dir",
                    "status": "paused",
                    "auto_paused": True,
                    "paused_reason": "High code failure rate: 0.75",
                    "paused_at": "2026-04-10T00:00:00",
                    "auto_pause_count": 2,
                }
            ]
        )
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        e = entries[0]
        assert e["direction_id"] == "d1"
        assert e["auto_paused"] is True
        assert e["paused_reason"] == "High code failure rate: 0.75"
        assert e["auto_pause_count"] == 2
        assert e["needs_manual_intervention"] is False

    def test_surfaces_active_remediation_monitor(self):
        project = self._project(
            [
                {
                    "id": "d1",
                    "text": "monitoring",
                    "status": "active",
                    "remediation_monitor_started_at": "2026-04-15T00:00:00",
                    "remediation_attempts": 1,
                    "last_remediation_result": "merged",
                    "error_fingerprint": "abc123",
                }
            ]
        )
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        e = entries[0]
        assert e["remediation_monitor_active"] is True
        assert e["remediation_attempts"] == 1
        assert e["last_remediation_result"] == "merged"
        assert e["error_fingerprint"] == "abc123"

    def test_surfaces_needs_manual_intervention(self):
        project = self._project(
            [
                {
                    "id": "d1",
                    "text": "stuck",
                    "status": "paused",
                    "needs_manual_intervention": True,
                    "remediation_attempts": 3,
                    "last_remediation_result": "failed",
                }
            ]
        )
        entries = _build_direction_remediation_state(project)
        assert len(entries) == 1
        assert entries[0]["needs_manual_intervention"] is True
        assert entries[0]["remediation_attempts"] == 3

    def test_mixed_directions_only_notable_returned(self):
        project = self._project(
            [
                {"id": "clean", "text": "ok", "status": "active"},
                {
                    "id": "paused",
                    "text": "paused",
                    "status": "paused",
                    "auto_paused": True,
                },
                {
                    "id": "escalated",
                    "text": "stuck",
                    "status": "paused",
                    "needs_manual_intervention": True,
                    "remediation_attempts": 3,
                },
            ]
        )
        entries = _build_direction_remediation_state(project)
        ids = {e["direction_id"] for e in entries}
        assert ids == {"paused", "escalated"}

    def test_handles_empty_or_missing_directions(self):
        assert _build_direction_remediation_state(self._project([])) == []
        assert _build_direction_remediation_state(self._project(None)) == []


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — GET /api/admin/diagnostics (pagination + global counters)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="/diagnostics uses jsonb_array_length — Postgres-only",
)
class TestGlobalDiagnosticsPagination:
    """Endpoint-level tests calling get_global_diagnostics() directly with a DB session."""

    async def _make_project(self, db_session, name):
        project = Project(name=name, user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        return project

    @pytest.mark.asyncio
    async def test_default_limit_caps_scan_and_global_counts_independent_of_page(
        self, db_session
    ):
        """Default limit=50 caps the scanned window, but queued/in_progress counts
        come from a GROUP BY over the FULL active set (not the page)."""
        project = await self._make_project(db_session, "diag-page")

        # 55 fresh QUEUED (older) + 5 fresh IN_PROGRESS (newest). created_at ASC
        # ordering means the default 50-row window is all QUEUED, so a page-scoped
        # in_progress_count would be 0 — the bug this asserts against.
        for _ in range(55):
            db_session.add(
                _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=300)
            )
        for _ in range(5):
            db_session.add(
                _make_task(
                    project.id,
                    TaskStatus.IN_PROGRESS,
                    created_ago_secs=0,
                    heartbeat_ago_secs=30,
                )
            )
        await db_session.commit()

        result = await get_global_diagnostics(
            limit=50, offset=0, session=db_session, current_user={}
        )

        assert result["total_active_tasks_scanned"] == 50
        assert result["queued_count"] == 55
        assert result["in_progress_count"] == 5
        assert result["truncated"] is True
        assert any("not scanned" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_offset_windowing(self, db_session):
        """offset advances the window; the tail page is not flagged truncated."""
        project = await self._make_project(db_session, "diag-offset")
        for _ in range(60):
            db_session.add(
                _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=300)
            )
        await db_session.commit()

        result = await get_global_diagnostics(
            limit=50, offset=50, session=db_session, current_user={}
        )

        assert result["total_active_tasks_scanned"] == 10
        assert result["queued_count"] == 60
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_high_limit_emits_near_maximum_warning(self, db_session):
        """limit>=400 surfaces a 'near the maximum' warning regardless of row count."""
        project = await self._make_project(db_session, "diag-highlimit")
        db_session.add(_make_task(project.id, TaskStatus.QUEUED, created_ago_secs=60))
        await db_session.commit()

        result = await get_global_diagnostics(
            limit=400, offset=0, session=db_session, current_user={}
        )

        assert any("near the maximum" in w for w in result["warnings"])
        # Only 1 active task < 400 window → nothing beyond the window.
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_stuck_tasks_top_n_truncation_with_full_count(self, db_session):
        """stuck_tasks is capped at DIAGNOSTICS_TOP_N, sorted most-stuck-first,
        while total_stuck_count reflects every stuck task in the scanned window."""
        project = await self._make_project(db_session, "diag-topn")
        stuck_n = DIAGNOSTICS_TOP_N + 5
        # long_queued: QUEUED, no deps, created >30 min ago. Vary age so ordering
        # by stuck_for_seconds is deterministic.
        for i in range(stuck_n):
            db_session.add(
                _make_task(
                    project.id,
                    TaskStatus.QUEUED,
                    created_ago_secs=35 * 60 + i * 60,
                )
            )
        await db_session.commit()

        result = await get_global_diagnostics(
            limit=100, offset=0, session=db_session, current_user={}
        )

        assert len(result["stuck_tasks"]) == DIAGNOSTICS_TOP_N
        assert result["total_stuck_count"] == stuck_n
        secs = [s["stuck_for_seconds"] for s in result["stuck_tasks"]]
        assert secs == sorted(secs, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — GET /api/admin/projects/{id}/diagnostics (active-task cap)
# ══════════════════════════════════════════════════════════════════════════════


class TestProjectDiagnosticsActiveTaskCap:
    """The per-project diagnostics endpoint caps the active-task scan and reports
    truncation. SQLite-compatible (no jsonb_array_length here)."""

    async def _make_project_with_member(self, db_session, name, user_id):
        project = Project(name=name, user_id=user_id)
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        db_session.add(
            ProjectMember(project_id=project.id, user_id=user_id, role="owner")
        )
        await db_session.commit()
        return project

    @pytest.mark.asyncio
    async def test_truncation_flagged_when_active_exceeds_cap(self, db_session):
        user_id = "diag-cap-user"
        project = await self._make_project_with_member(db_session, "diag-cap", user_id)
        # 4 active tasks; cap at 2 → truncated, only 2 retained.
        for i in range(4):
            db_session.add(
                _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=300 + i * 60)
            )
        await db_session.commit()

        result = await get_project_diagnostics(
            project_id=project.id,
            session=db_session,
            current_user={"user_id": user_id},
            max_active_tasks=2,
        )

        assert result["active_tasks_truncated"] is True
        assert result["active_tasks_cap"] == 2

    @pytest.mark.asyncio
    async def test_no_truncation_when_active_within_cap(self, db_session):
        user_id = "diag-nocap-user"
        project = await self._make_project_with_member(
            db_session, "diag-nocap", user_id
        )
        for _ in range(2):
            db_session.add(
                _make_task(project.id, TaskStatus.QUEUED, created_ago_secs=300)
            )
        await db_session.commit()

        result = await get_project_diagnostics(
            project_id=project.id,
            session=db_session,
            current_user={"user_id": user_id},
            max_active_tasks=_MAX_ACTIVE_TASKS_DEFAULT,
        )

        assert result["active_tasks_truncated"] is False
        assert result["active_tasks_cap"] == _MAX_ACTIVE_TASKS_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# _build_direction_portfolio_summary — surfaces non-code direction health
# ─────────────────────────────────────────────────────────────────────────────


def test_portfolio_summary_surfaces_non_code_direction():
    """A 0-PR direction with non-code generalized successes is surfaced with
    its non-code contribution computed as generalized - merged."""
    project = Project(name="p", user_id="u")
    project.directions = [
        {
            "id": "dir-noncode",
            "text": "Audit the auth subsystem",
            "status": "active",
            "research_history": [
                {
                    "cycle": 1,
                    "prs_merged": 0,
                    "generalized_successes": 3,
                }
            ],
        }
    ]
    summary = _build_direction_portfolio_summary(project)
    assert len(summary) == 1
    entry = summary[0]
    assert entry["direction_id"] == "dir-noncode"
    assert entry["prs_merged_recent"] == 0
    assert entry["generalized_successes_recent"] == 3
    assert entry["non_code_successes_recent"] == 3


def test_portfolio_summary_skips_code_only_direction():
    """A direction whose generalized successes equal its merges (pure code) is
    omitted to keep the diagnostics payload compact."""
    project = Project(name="p", user_id="u")
    project.directions = [
        {
            "id": "dir-code",
            "text": "Ship features",
            "status": "active",
            "research_history": [
                {"cycle": 1, "prs_merged": 2, "generalized_successes": 2},
            ],
        }
    ]
    assert _build_direction_portfolio_summary(project) == []


# ── _build_problem_model_trim_verdict ────────────────────────────────────────


def test_trim_verdict_none_without_packer_meta():
    """Payloads without packer metadata (older / unpacked) yield no verdict."""
    assert _build_problem_model_trim_verdict({"problem_model": {"gaps": []}}) is None


def test_trim_verdict_full_counts_gap_memory_trim():
    """A problem_model that shipped untrimmed reports status 'full' but still
    surfaces how many gaps had their append-only memory pre-trimmed."""
    payload = {
        "problem_model": {
            "gaps": [{"id": "g1"}],
            "_gap_memory_trimmed": [{"gap_id": "g1", "fields": {"history": {"shown": 5, "total": 20}}}],
        },
        "_packer_meta": {"budget_total": 122880, "budget_used": 40000, "trimmed": [], "dropped": []},
    }
    v = _build_problem_model_trim_verdict(payload)
    assert v["status"] == "full"
    assert v["gap_memory_trimmed_count"] == 1
    assert v["budget_total"] == 122880
    assert "floor" not in v


def test_trim_verdict_summarized():
    """problem_model listed in _packer_meta.trimmed (but not floored) is 'summarized'."""
    payload = {
        "problem_model": {"gaps": []},
        "_packer_meta": {"trimmed": [{"key": "problem_model", "kind": "summarized"}], "dropped": []},
    }
    assert _build_problem_model_trim_verdict(payload)["status"] == "summarized"


def test_trim_verdict_floor_takes_precedence_over_summarized():
    """When the summarize path degraded the model to the floor, status is 'floor'
    and the floor detail (stripped fields / dropped gaps) is surfaced."""
    payload = {
        "problem_model": {
            "gaps": [],
            "_agent_view_floor_applied": {
                "floor_bytes": 32768,
                "stripped_gap_fields": ["history", "evidence"],
                "dropped_gaps": 3,
                "still_over_floor": True,
            },
        },
        "_packer_meta": {"trimmed": [{"key": "problem_model"}], "dropped": []},
    }
    v = _build_problem_model_trim_verdict(payload)
    assert v["status"] == "floor"
    assert v["floor"]["dropped_gaps"] == 3
    assert v["floor"]["stripped_gap_fields"] == ["history", "evidence"]
    assert v["floor"]["summarized_capabilities_count"] == 0
    assert v["floor"]["summarized_friction_count"] == 0
    assert v["floor"]["still_over_floor"] is True


def test_trim_verdict_floor_surfaces_skeletonized_sections():
    """Floor verdict includes how many capabilities and friction entries were
    skeletonized so operators can tell the strategist saw a reduced model."""
    payload = {
        "problem_model": {
            "gaps": [],
            "_agent_view_floor_applied": {
                "floor_bytes": 32768,
                "stripped_gap_fields": [],
                "dropped_gaps": 0,
                "summarized_capabilities": 1,
                "summarized_friction": 1,
            },
        },
        "_packer_meta": {"trimmed": [{"key": "problem_model"}], "dropped": []},
    }
    floor = payload["problem_model"]["_agent_view_floor_applied"]
    assert isinstance(floor["summarized_capabilities"], int)
    assert isinstance(floor["summarized_friction"], int)
    v = _build_problem_model_trim_verdict(payload)
    assert v["status"] == "floor"
    assert v["floor"]["summarized_capabilities_count"] == 1
    assert v["floor"]["summarized_friction_count"] == 1


def test_trim_verdict_dropped_carries_reason():
    """A problem_model in _packer_meta.dropped is 'dropped' with its reason."""
    payload = {
        "_packer_meta": {
            "trimmed": [],
            "dropped": [{"key": "problem_model", "reason": "exceeds remaining budget"}],
        },
    }
    v = _build_problem_model_trim_verdict(payload)
    assert v["status"] == "dropped"
    assert v["dropped_reason"] == "exceeds remaining budget"


def test_portfolio_summary_skips_inactive_and_empty():
    """Archived directions and directions with no history produce no entries."""
    project = Project(name="p", user_id="u")
    project.directions = [
        {
            "id": "dir-archived",
            "text": "Old",
            "status": "archived",
            "research_history": [
                {"cycle": 1, "prs_merged": 0, "generalized_successes": 5}
            ],
        },
        {"id": "dir-empty", "text": "New", "status": "active", "research_history": []},
    ]
    assert _build_direction_portfolio_summary(project) == []


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — QUEUED-row deferred-column regression (health-overview 500)
# ══════════════════════════════════════════════════════════════════════════════


class TestQueuedScanDeferredColumns:
    """Regression for the 2026-06-04..07 health-overview MissingGreenlet 500s.

    _compute_health_overview loads QUEUED rows with
    load_only(*QUEUED_SCAN_COLUMNS) — heavy JSONB deferred. A QUEUED task that
    trips a stuck branch then reaches the shared stuck-entry tail, which reads
    attempt_count and runs _classify_stuck_failure (latest_status_message +
    heavy JSONB). Any of those being deferred sync-lazy-loads under the async
    session → MissingGreenlet → the endpoint AND every digest-precompute health
    tick 500 for as long as any task sits queued >30 min — i.e. exactly during
    a rate-limit pause, when the operator most needs the panel.

    NB the expunge_all() below is load-bearing: with the freshly-committed
    instance still in the identity map, the re-query returns the fully-loaded
    object and deferral never happens — which is precisely why CI never caught
    the prod 500.
    """

    @pytest.mark.asyncio
    async def test_stuck_queued_row_via_scan_columns_builds_without_lazy_load(
        self, db_session
    ):
        from sqlalchemy.orm import load_only
        from sqlmodel import select

        from app.routers.admin_health import QUEUED_SCAN_COLUMNS

        project = Project(name="diag-queued-deferred", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(
            project.id,
            TaskStatus.QUEUED,
            created_ago_secs=31 * 60,  # > long_queued threshold (30 min)
            status_message="Rate limit hit — requeued without burning retry",
        )
        task.attempt_count = 2
        db_session.add(task)
        await db_session.commit()
        task_id = task.id
        db_session.expunge_all()

        row = (
            (
                await db_session.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .options(load_only(*QUEUED_SCAN_COLUMNS))
                )
            )
            .scalars()
            .one()
        )

        result = await _build_stuck_tasks([row], _utc_now(), db_session)

        assert len(result) == 1
        entry = result[0]
        assert entry["stuck_reason"] == "long_queued"
        assert entry["attempt_count"] == 2
        # Heavy JSONB stays deferred for QUEUED rows → classification is
        # suppressed (None / "unclassified"), never lazy-loaded.
        assert entry["failure_class"] is None

    @pytest.mark.asyncio
    async def test_classify_skips_persistent_row_with_deferred_jsonb(
        self, db_session
    ):
        """A DB-loaded row missing the heavy JSONB must return None, not load it.

        The message used here WOULD classify as RATE_LIMIT on a fully-loaded
        row (it is a verbatim RATE_LIMIT_PATTERNS string, same as
        test_rate_limit_keywords) — proving the early-out fires on deferral,
        not on message content.
        """
        from sqlalchemy.orm import load_only
        from sqlmodel import select

        project = Project(name="diag-classify-deferred", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = _make_task(
            project.id,
            TaskStatus.QUEUED,
            status_message="API Error: Rate limit reached",
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id
        db_session.expunge_all()

        row = (
            (
                await db_session.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .options(
                        load_only(
                            Task.id, Task.status, Task.latest_status_message
                        )
                    )
                )
            )
            .scalars()
            .one()
        )

        assert _classify_stuck_failure(row) is None

    def test_classify_still_works_on_transient_and_mock_instances(self):
        """The deferral guard must not regress in-memory classification paths.

        Mocks are not sqlalchemy-inspectable (NoInspectionAvailable) — the
        guard must skip them, and classification of a verbatim
        RATE_LIMIT_PATTERNS message must still succeed.
        """
        t = MagicMock(spec=Task)
        t.latest_status_message = "API Error: Rate limit reached"
        assert _classify_stuck_failure(t) == "RATE_LIMIT"
