"""Tests for failure_classification subtype helpers and classify_failure()."""

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from app.failure_classification import (
    AGENT_TIMEOUT,
    ALL_FAILURE_CLASSES,
    CODE_ERROR,
    EXTERNAL_API,
    VIEWPORT_REGRESSION,
    EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY,
    EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE,
    EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT,
    EXTERNAL_API_SUBTYPE_OTHER,
    EXTERNAL_API_SUBTYPE_PROVIDER_AUTH,
    EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG,
    EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA,
    MIGRATION_ERROR,
    PROVISION_FAILURE,
    RATE_LIMIT,
    BLOCKED_PRECONDITION,
    SCOPE_EMPTY,
    SCOPE_OVERFLOW,
    _external_api_subtype_from_message,
    _fingerprint_message,
    _looks_like_error,
    _timeout_subtype_from_message,
    classify_external_api_subtype,
    classify_failure,
    classify_failure_with_confidence,
    classify_failure_taxonomy,
    classify_timeout_subtype,
    get_account_failure_class_counts,
    get_account_failure_class_records,
    get_top_residual_fingerprints,
    normalize_agent_failure_category,
    persist_failure_category,
    record_account_failure_class,
    record_residual_fingerprint,
    should_trigger_code_defect_investigation,
    should_trigger_review_quality_investigation,
    should_trigger_timeout_investigation,
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
    TIMEOUT_CASCADE,
    TIMEOUT_EXECUTION,
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_OOM,
    TIMEOUT_PROCESS_CRASH,
    TIMEOUT_UNKNOWN,
    _resolve_task_provider_auth,
)
from app.models import Task


class _FakeTask:
    def __init__(self, msg: str) -> None:
        self.latest_status_message = msg


def test_invalid_provider_auth_marks_context_instead_of_silent_fallback():
    task = MagicMock()
    task.id = "task-invalid-provider"
    task.context = {"llm_provider": "not-a-provider", "llm_auth_mode": "api_key"}

    assert _resolve_task_provider_auth(task) == (None, None)
    assert task.context["llm_runtime_resolution_failed"] is True
    assert (
        task.context["llm_runtime_resolution_error"] == "invalid_provider_or_auth_mode"
    )
    assert task.context["llm_provider_raw"] == "not-a-provider"


class TestTimeoutSubtypeFromMessage:
    def test_oom(self):
        assert _timeout_subtype_from_message("exit 137 out of memory") == TIMEOUT_OOM

    def test_heartbeat_lost(self):
        assert (
            _timeout_subtype_from_message("No heartbeat received after 300s")
            == TIMEOUT_HEARTBEAT_LOST
        )

    def test_iteration_limit(self):
        assert (
            _timeout_subtype_from_message("review-fix cycle limit reached")
            == TIMEOUT_ITERATION_LIMIT
        )
        assert (
            _timeout_subtype_from_message("iteration cap exceeded")
            == TIMEOUT_ITERATION_LIMIT
        )

    def test_cascade_via_cancelled_prefix(self):
        assert (
            _timeout_subtype_from_message("Cancelled: parent evaluation timeout")
            == TIMEOUT_CASCADE
        )

    def test_cascade_via_superseded(self):
        assert (
            _timeout_subtype_from_message("task superseded by newer revision")
            == TIMEOUT_CASCADE
        )

    def test_cascade_precedes_heartbeat(self):
        # cascade signal should win over a concurrent heartbeat keyword
        assert (
            _timeout_subtype_from_message("Cancelled: no heartbeat") == TIMEOUT_CASCADE
        )

    def test_process_crash(self):
        assert (
            _timeout_subtype_from_message("agent process crashed unexpectedly")
            == TIMEOUT_PROCESS_CRASH
        )

    def test_execution_timeout(self):
        assert (
            _timeout_subtype_from_message("execution timed out after 3600s")
            == TIMEOUT_EXECUTION
        )

    def test_unknown(self):
        assert (
            _timeout_subtype_from_message("something weird happened") == TIMEOUT_UNKNOWN
        )

    def test_empty(self):
        assert _timeout_subtype_from_message("") == TIMEOUT_UNKNOWN


class TestClassifyTimeoutSubtypeDelegates:
    def test_delegates_to_helper(self):
        task = _FakeTask("exit code 137 OOM killer")
        assert classify_timeout_subtype(task) == TIMEOUT_OOM

    def test_combines_logs(self):
        task = _FakeTask("")
        assert (
            classify_timeout_subtype(task, logs="No heartbeat received")
            == TIMEOUT_HEARTBEAT_LOST
        )

    def test_none_status(self):
        task = _FakeTask(None)  # type: ignore[arg-type]
        assert classify_timeout_subtype(task) == TIMEOUT_UNKNOWN


def _task(msg: str = "", ctx: dict | None = None) -> Task:
    t = MagicMock(spec=Task)
    t.latest_status_message = msg
    t.context = ctx or {}
    return t


class TestPersistFailureCategoryTimeoutSubtype:
    def test_agent_timeout_persists_subtype_from_status(self):
        task = _task("exit code 137 out of memory")
        persist_failure_category(task, AGENT_TIMEOUT)
        assert task.context["failure_category"] == AGENT_TIMEOUT
        assert task.context["timeout_subtype"] == TIMEOUT_OOM

    def test_agent_timeout_combines_logs(self):
        task = _task("")
        persist_failure_category(
            task, AGENT_TIMEOUT, logs="No heartbeat received after 300s"
        )
        assert task.context["timeout_subtype"] == TIMEOUT_HEARTBEAT_LOST

    def test_non_timeout_does_not_write_subtype(self):
        task = _task("AssertionError")
        persist_failure_category(task, CODE_ERROR)
        assert task.context["failure_category"] == CODE_ERROR
        assert "timeout_subtype" not in task.context


class TestNormalizeAgentFailureCategory:
    def test_generic_fatal_model_config_rejection_overrides_code_error(self):
        msg = "Fatal error: ModelNotFoundError: models/gemini-3.1-pro-preview is not found for API version v1beta"
        task = _task(msg, {"llm_provider": "gemini", "llm_auth_mode": "api_key"})

        assert normalize_agent_failure_category("fatal", task) == EXTERNAL_API

    def test_generic_fatal_gemini_quota_overrides_code_error(self):
        msg = "Fatal error: Quota exceeded for quota metric 'GenerateRequests'. Please retry in 39.8s."
        task = _task(msg, {"llm_provider": "gemini", "llm_auth_mode": "api_key"})

        assert normalize_agent_failure_category("fatal", task) == RATE_LIMIT
        assert task.context["rate_limit_kind"] == "rate_limit"
        assert (
            task.context["rate_limit_matched_pattern"]
            == "quota exceeded for quota metric"
        )

    def test_specific_code_label_stays_authoritative(self):
        msg = "pytest failed while docs mention quota exceeded for quota metric"
        task = _task(msg, {"llm_provider": "gemini", "llm_auth_mode": "api_key"})

        assert normalize_agent_failure_category("test_failed", task) == CODE_ERROR
        assert "rate_limit_kind" not in task.context

    def test_generic_fatal_without_provider_signal_stays_code_error(self):
        task = _task("Fatal error: TypeError: cannot read property of undefined")

        assert normalize_agent_failure_category("fatal", task) == CODE_ERROR

    def test_build_failed_routes_to_provision_failure(self):
        """Agent emits build_failed for test setup failures (Postgres sidecar
        startup, Docker build, dep install) — environmental, not code defect.
        Must route to PROVISION_FAILURE so the orchestrator's infra retry
        path applies instead of burning the code-error retry budget."""
        msg = (
            "Test suite could not run (setup failure — not a fixable test failure):\n"
            "[pytest (backend pyproject)] runner timeout: Command failed: "
            "bash scripts/test-backend.sh --postgres\n"
            "Signal: SIGTERM\n=== Starting Postgres sidecar (blueprint-test-pg-15410) ===\n"
            "=== Building backend image ==="
        )
        task = _task(msg)
        assert (
            normalize_agent_failure_category("build_failed", task) == PROVISION_FAILURE
        )

    def test_build_failed_routes_to_provision_failure_via_classify_failure(self):
        """End-to-end: classify_failure sees build_failed in context and routes
        to PROVISION_FAILURE without falling through to text classification."""
        task = _task(
            "Test suite could not run (setup failure)",
            {"failure_category": "build_failed"},
        )
        assert classify_failure(task) == PROVISION_FAILURE


class TestClassifyFailureCodeError:
    """Non-keyword failures fall through to CODE_ERROR (the default)."""

    @pytest.mark.parametrize(
        "msg",
        [
            "AssertionError: expected 3, got 4",
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
            "NameError: name 'foo' is not defined",
            "SyntaxError: invalid syntax at line 42",
        ],
    )
    def test_generic_python_errors(self, msg):
        assert classify_failure(_task(msg)) == CODE_ERROR


class TestClassifyFailureWithConfidence:
    def test_structured_failure_category_is_high_confidence(self):
        task = _task("anything", ctx={"failure_category": "rate_limited"})
        result = classify_failure_with_confidence(task)
        assert result.failure_class == RATE_LIMIT
        assert result.confidence == 0.9

    def test_migration_regex_is_high_confidence(self):
        task = _task("alembic.util.exc.CommandError: Can't locate revision")
        result = classify_failure_with_confidence(task)
        assert result.failure_class == MIGRATION_ERROR
        assert result.confidence == 0.9

    def test_provider_rate_limit_and_provision_and_external_api_are_medium_high(self):
        rate_task = _task(
            "quota exceeded for quota metric",
            ctx={"llm_provider": "gemini", "llm_auth_mode": "api_key"},
        )
        assert classify_failure_with_confidence(rate_task).confidence == 0.8

        provision_task = _task("failed to provision VM in zone us-central1")
        assert classify_failure_with_confidence(provision_task).confidence == 0.8

        external_task = _task("psycopg OperationalError: could not connect to server")
        assert classify_failure_with_confidence(external_task).confidence == 0.8

    def test_timeout_keywords_are_medium_confidence(self):
        task = _task("No heartbeat received after 300s")
        result = classify_failure_with_confidence(task)
        assert result.failure_class == AGENT_TIMEOUT
        assert result.confidence == 0.7

    def test_code_error_fallthrough_is_low_confidence(self):
        task = _task("TypeError: unsupported operand")
        result = classify_failure_with_confidence(task)
        assert result.failure_class == CODE_ERROR
        assert result.confidence == 0.5

    def test_legacy_shim_matches_dataclass_failure_class(self):
        task = _task("No heartbeat received after 300s")
        assert (
            classify_failure(task)
            == classify_failure_with_confidence(task).failure_class
        )


class TestClassifyFailureViewportGateInfra:
    """VIEWPORT_GATE_INFRA_UNAVAILABLE sentinel routes to PROVISION_FAILURE,
    not CODE_ERROR or VIEWPORT_REGRESSION, so infra shortfalls don't penalize
    direction health or create spurious residual-fingerprint remediation tasks."""

    def test_sentinel_routes_to_provision_failure(self):
        t = _task("VIEWPORT_GATE_INFRA_UNAVAILABLE: chromium not found on PATH")
        assert classify_failure(t) == PROVISION_FAILURE

    def test_sentinel_not_code_error(self):
        t = _task("VIEWPORT_GATE_INFRA_UNAVAILABLE: docker fallback also unavailable")
        result = classify_failure(t)
        assert result != CODE_ERROR

    def test_sentinel_not_viewport_regression(self):
        t = _task("VIEWPORT_GATE_INFRA_UNAVAILABLE: sudo required for os deps")
        result = classify_failure(t)
        assert result != VIEWPORT_REGRESSION

    def test_sentinel_in_logs_routes_to_provision_failure(self):
        t = _task("")
        result = classify_failure(
            t, logs="VIEWPORT_GATE_INFRA_UNAVAILABLE: libnspr missing"
        )
        assert result == PROVISION_FAILURE

    def test_provision_failure_is_infra_class(self):
        from app.failure_classification import INFRA_CLASSES

        assert PROVISION_FAILURE in INFRA_CLASSES

    def test_regular_viewport_failure_still_routes_to_viewport_regression(self):
        t = _task("expectnooverflowat( failed at 375px width")
        assert classify_failure(t) == VIEWPORT_REGRESSION

    def test_text_truncation_routes_to_viewport_regression(self):
        t = _task(
            "expectNoTextTruncationAt(375) detected clipped text in MessagesPanel"
        )
        assert classify_failure(t) == VIEWPORT_REGRESSION

    def test_text_truncation_in_logs_routes_to_viewport_regression(self):
        t = _task("")
        result = classify_failure(
            t, logs="expectNoTextTruncationAt(375): silent truncation detected"
        )
        assert result == VIEWPORT_REGRESSION

    def test_text_truncation_not_code_error(self):
        t = _task("expectNoTextTruncationAt(375) failed at DashboardHome")
        assert classify_failure(t) != CODE_ERROR

    def test_raw_libatk_routes_to_provision_failure(self):
        t = _task(
            "error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file"
        )
        assert classify_failure(t) == PROVISION_FAILURE

    def test_raw_libnss3_routes_to_provision_failure(self):
        t = _task("libnss3.so => not found when launching Chromium")
        assert classify_failure(t) == PROVISION_FAILURE

    def test_raw_libgbm_routes_to_provision_failure(self):
        t = _task(
            "libgbm.so.1: cannot open shared object file: No such file or directory"
        )
        assert classify_failure(t) == PROVISION_FAILURE

    def test_host_system_missing_deps_routes_to_provision_failure(self):
        t = _task("host system is missing dependencies required by Playwright Chromium")
        assert classify_failure(t) == PROVISION_FAILURE

    def test_playwright_sudo_routes_to_provision_failure(self):
        t = _task(
            "Playwright Chromium install requires sudo and the host library is missing"
        )
        assert classify_failure(t) == PROVISION_FAILURE

    def test_admin_ui_gate_blocked_routes_to_provision_failure(self):
        t = _task("admin-ui viewport gate is blocked in this VM due to missing libs")
        assert classify_failure(t) == PROVISION_FAILURE

    def test_verbatim_production_message_provision_failure(self):
        # Verbatim production message that previously landed as CODE_ERROR
        t = _task(
            "Retries exhausted (3/3). admin-ui viewport gate is blocked in this VM: "
            "Playwright Chromium install requires sudo and the host library lib..."
        )
        assert classify_failure(t) == PROVISION_FAILURE

    def test_fast_path_code_error_context_overridden_by_bootstrap_patterns(self):
        # task.context already has failure_category=CODE_ERROR (fast-path) but status
        # message contains a bootstrap signature — must still return PROVISION_FAILURE.
        t = _task(
            "libatk-1.0.so.0: cannot open shared object file",
            ctx={"failure_category": "claude_code_failed"},
        )
        assert classify_failure(t) == PROVISION_FAILURE

    def test_normalize_agent_failure_category_bootstrap_returns_provision_failure(self):
        t = _task(
            "Playwright Chromium install requires sudo and the host library libatk is missing",
        )
        assert (
            normalize_agent_failure_category("claude_code_failed", t)
            == PROVISION_FAILURE
        )

    def test_bootstrap_not_viewport_regression(self):
        t = _task(
            "libgbm.so.1: cannot open shared object file: No such file or directory"
        )
        assert classify_failure(t) != VIEWPORT_REGRESSION

    def test_bootstrap_not_code_error(self):
        t = _task("host system is missing dependencies required for Playwright")
        assert classify_failure(t) != CODE_ERROR


class TestClassifyFailureProvision:
    @pytest.mark.parametrize(
        "msg",
        [
            "failed to provision VM in zone us-central1",
            "Provisioning failed: instance group unreachable",
            "VM provisioning timed out",
            "CPUS_ALL_REGIONS quota exceeded",
            "IN_USE_ADDRESSES quota exceeded in region",
            "sshd: connection closed by remote host",
            "startup script exited non-zero",
            "no space left on device",
            "disk quota exceeded on /var",
            "filesystem is full",
            "vm failed to start after 120s",
            "compute.googleapis.com returned 500",
            "container killed by platform",
        ],
    )
    def test_provision_keywords(self, msg):
        assert classify_failure(_task(msg)) == PROVISION_FAILURE

    def test_heartbeat_since_provisioning_is_not_provision(self):
        """Benign 'since provisioning' suffix must not trigger PROVISION_FAILURE."""
        msg = "No heartbeat received after 300s since provisioning"
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    def test_database_error_precedes_provision_keyword(self):
        """'could not connect to server' contains 'connection refused'-like cues
        but must route to EXTERNAL_API, not PROVISION_FAILURE."""
        msg = "could not connect to server: Connection refused"
        assert classify_failure(_task(msg)) == EXTERNAL_API


class TestClassifyFailureAgentTimeout:
    @pytest.mark.parametrize(
        "msg",
        [
            "No heartbeat received for 600s",
            "task reaped by orchestrator",
            "VM never started",
            "execution timed out after 2700s",
            "received SIGTERM",
            "out of memory: killed by kernel",
            "oom-kill triggered on cgroup",
            "exit code 137",
            "exit code 143",
            "cannot allocate memory",
            "memory limit exceeded",
            "node process killed",
            "[FATAL:uncaughtException] TypeError",
            "process crashed during clone",
            "iteration cap reached",
            "ci wait exceeded",
            "stuck in ci-wait",
            "backstop timeout fired",
            "waiting on dependency PR",
            "Waiting: serializing migration — task 7af4d393 is already producing a migration in this project",
            "Cancelled: parent evaluation timeout",
            "Cancelled: some new orchestrator reason",
            "Superseded by fix on PR #1234",
        ],
    )
    def test_timeout_keywords(self, msg):
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    def test_pr_creation_heartbeat_is_agent_timeout(self):
        """Regression: the reaper calls classify_failure before updating latest_status_message.
        Tasks that die (OOM/SIGKILL) while the agent is creating a PR leave the PR-creation
        heartbeat as the last status message. Without this pattern, classify_failure falls
        through to CODE_ERROR and record_residual_fingerprint creates a spurious fingerprint
        that triggers auto-remediation for a non-existent code defect."""
        msg = "[13.6MB] Creating pull request for blueprint/task-df8cfed3..."
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    @pytest.mark.parametrize(
        "msg",
        [
            "[12.1MB] Using tool: Grep",
            "[13.7MB] Using tool: Grep",
            "[8.4MB] Using tool: Read",
            "[10.2MB] Using tool: Edit",
            "[9.5MB cg:512.0MB] Using tool: Bash",
        ],
    )
    def test_tool_call_heartbeat_is_agent_timeout(self, msg):
        """Regression: tasks reaped mid-tool-call (OOM of Claude subprocess, VM kill,
        network drop) leave 'Using tool: <Name>' as their latest_status_message. Without
        this pattern, classify_failure falls through to CODE_ERROR and
        record_residual_fingerprint creates spurious fingerprints that trigger
        auto-remediation for non-existent code defects. Mirrors the
        'creating pull request for blueprint/' guard."""
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    def test_cascade_beats_code_error_default(self):
        """A 'Cancelled:' prefix routes to AGENT_TIMEOUT even if tail looks like code."""
        msg = "Cancelled: AssertionError in downstream"
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT


class TestClassifyFailureExternalApi:
    @pytest.mark.parametrize(
        "msg",
        [
            "GitHub API rate limit exceeded",
            "api.github.com returned 429 too many requests",
            "api.anthropic.com returned 500",
            "500 Internal Server Error",
            "502 Bad Gateway from upstream",
            "503 Service Unavailable",
            "Claude API returned 500",
            "connection timed out to upstream",
            "SSL handshake failed",
            "certificate verification failed",
            "could not resolve hostname api.github.com",
            "name resolution failed",
            "network unreachable",
            "context_length_exceeded on request",
            "token limit exceeded",
            "failed to create pull request",
            "failed to create branch feat/x",
            "merge blocked by branch protection",
            "merge conflicts detected",
            "PR #42 merge failed 15 times (>= 15): server error — giving up, needs manual intervention",
            "PR #42 merge hard-capped after 15 failures — manual action required",
            "required status check missing",
            "401 Unauthorized",
            "Bad credentials returned from GitHub",
            "token expired",
            # Anthropic 403 when an org has disabled OAuth for Claude Code:
            # an auth/infra failure, must NOT fall through to CODE_ERROR.
            "Claude subprocess exited code 1: HTTP 403 oauth_org_not_allowed",
            "Error: Overloaded",
            # Provider model-config rejections — upstream 400s (not code defects,
            # not rate limits, not auth failures). Our OpenAI/Gemini adapter
            # gates known-bad combos at settings_to_cli but the classifier is
            # the safety net for unknown incompatibilities that slip through.
            "ModelNotFoundError: models/gemini-3.1-flash-preview is not found for API version v1beta",
            "model not found: gpt-5-experimental",
            "is not found for API version v1beta, or is not supported for generateContent",
            "is not supported for generateContent",
            "Unsupported service_tier: flex",
            "400: Unsupported value: 'service_tier' does not support 'flex' with this model",
        ],
    )
    def test_external_api_keywords(self, msg):
        assert classify_failure(_task(msg)) == EXTERNAL_API

    def test_database_error_is_external_api(self):
        assert classify_failure(_task("psycopg.OperationalError")) == EXTERNAL_API
        assert (
            classify_failure(_task("asyncpg.exceptions.ConnectionFailure"))
            == EXTERNAL_API
        )


class TestClassifyFailureMigration:
    @pytest.mark.parametrize(
        "msg",
        [
            "alembic.util.exc.CommandError: Can't locate revision identified by '0042abc'",
            "RevisionError: revision 'abc123' not found",
            "Multiple head revisions are present for given argument 'head'",
            "Only a single head supported, found 2",
            "Multiple heads are present in the migration history",
            "psycopg.errors.UndefinedColumn: column tasks.new_field does not exist",
            "asyncpg.exceptions.UndefinedColumnError: column does not exist",
            "Schema version mismatch: expected 0042 got 0041",
            "EXPECTED_SCHEMA_REVISION mismatch on startup",
        ],
    )
    def test_migration_keywords(self, msg):
        assert classify_failure(_task(msg)) == MIGRATION_ERROR

    def test_downgrade_with_not_implemented(self):
        msg = "alembic downgrade failed: NotImplementedError: no downgrade defined"
        assert classify_failure(_task(msg)) == MIGRATION_ERROR

    def test_downgrade_with_operational_error(self):
        msg = "downgrade -1 raised OperationalError: column referenced by view"
        assert classify_failure(_task(msg)) == MIGRATION_ERROR

    def test_generic_db_error_not_migration(self):
        """Plain psycopg connectivity errors must stay EXTERNAL_API."""
        assert (
            classify_failure(_task("psycopg.OperationalError: connection refused"))
            == EXTERNAL_API
        )
        assert (
            classify_failure(_task("could not connect to server: timeout"))
            == EXTERNAL_API
        )

    def test_generic_operator_does_not_exist_not_migration(self):
        assert (
            classify_failure(_task("operator does not exist: text = int"))
            == EXTERNAL_API
        )

    def test_migration_taxonomy_is_code_defect(self):
        t = _task("RevisionError: cannot locate")
        assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT


class TestClassifyFailureRateLimit:
    @pytest.mark.parametrize(
        "msg",
        [
            "API Error: Rate limit reached",
            "You've hit your limit · resets 5am",
            "Claude AI usage limit reached",
            "5-hour limit reached",
            "This request would exceed your account's rate limit.",
        ],
    )
    def test_verbatim_claude_rate_limit_phrases(self, msg):
        assert classify_failure(_task(msg)) == RATE_LIMIT

    def test_generic_429_does_not_match(self):
        """Bare '429' / 'rate limit' must not classify as RATE_LIMIT — only exact
        Claude phrases may disable the account."""
        assert classify_failure(_task("HTTP 429 from upstream service")) == CODE_ERROR

    def test_rate_limit_event_in_agent_output_preempts_handler_code_error(self):
        """Regression (2026-05-24, task da68cb17 + 4 siblings): when the agent
        is rate-limited mid-stream by the Claude Code CLI, it emits a structured
        ``{"type":"rate_limit_event",...}`` line then dies before producing the
        downstream handler's expected output (memo / audit / investigate JSON).
        The handler then reports its own ``exitCode=1, "couldn't extract JSON"``
        error and writes a generic CODE_ERROR ``failure_category``. Without this
        check, that CODE_ERROR fast-path won — burning all 3 retries on the same
        rate-limited account because the RATE_LIMIT requeue handler
        (agent_status.py) never ran. 5 tasks terminal-failed this way when 3
        Claude accounts weekly-burned simultaneously."""
        # Exact shape from da68cb17's status_message tail.
        msg = (
            "[CODE_ERROR] Retries exhausted (3/3). Memo handler could not extract "
            "a JSON object with all required keys (summary, reasoning, citations, "
            "tags). exitCode=1, allOutput=3238b, lastResultText=55b\n"
            "--- allOutput tail ---\n"
            'ude/projects/-tmp-workspace/memory/"},"fast_mode_state":"off"}\n'
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"reset_at":"2026-05-24T11:00:00Z"}}\n'
        )
        # The downstream memo handler stamped a generic CODE_ERROR label — this
        # is the exact bug. The fast-path would return CODE_ERROR and the
        # RATE_LIMIT requeue handler in agent_status.py would never run.
        task = _task(msg, ctx={"failure_category": "agent_self_failed"})
        assert classify_failure(task) == RATE_LIMIT, (
            "rate_limit_event in agent output must preempt the handler's "
            "downstream CODE_ERROR label — otherwise the rate-limit requeue "
            "handler never runs and the task burns its retry budget against "
            "the same dead account."
        )

    def test_rate_limit_event_in_logs_also_preempts(self):
        """rate_limit_event may arrive via the logs argument rather than status
        message (e.g. when the orchestrator captures stdout separately) — both
        sources are scanned."""
        logs = (
            'some other agent log...\n'
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}\n'
        )
        task = _task(
            "Memo handler exited 1 with no JSON object",
            ctx={"failure_category": "agent_self_failed"},
        )
        assert classify_failure(task, logs=logs) == RATE_LIMIT

    def test_gemini_daily_quota_exhaustion_not_misclassified_as_provision(self):
        """Regression: Gemini CLI quota-exhaustion was being misclassified as
        PROVISION_FAILURE because the provision bank contained a bare 'quota'
        keyword that shadowed LLM-provider quota errors. That bypassed the
        rate-limit requeue path in agent_status.py and burned attempt_count,
        permanently failing the task after 3 retries instead of waiting out
        the provider's cooldown window."""
        # Verbatim Gemini CLI TerminalQuotaError body (with cause.message).
        gemini_err = (
            "TerminalQuotaError: You have exhausted your daily quota on this model. "
            "You exceeded your current quota, please check your plan and billing details. "
            "Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_requests_per_model_per_day, limit: 250, model: gemini-3.1-pro. "
            "Please retry in 27m12s."
        )
        # Must NOT be PROVISION_FAILURE (that's GCP compute-quota territory).
        task = _task(
            gemini_err,
            ctx={"llm_provider": "gemini", "llm_auth_mode": "api_key"},
        )
        result = classify_failure(task, logs=gemini_err)
        assert result != PROVISION_FAILURE, (
            "Gemini quota-exhausted must not route through PROVISION_FAILURE "
            "(which burns retries); expected RATE_LIMIT or EXTERNAL_API."
        )

    def test_openai_quota_phrase_not_misclassified_as_provision(self):
        """Same regression for OpenAI's quota bodies — they include the
        generic phrase 'You exceeded your current quota' that was eaten by
        the bare 'quota' match in the provision bank."""
        openai_err = (
            "Error: 429 You exceeded your current quota, please check your plan "
            "and billing details."
        )
        task = _task(
            openai_err, ctx={"llm_provider": "openai", "llm_auth_mode": "api_key"}
        )
        result = classify_failure(task, logs=openai_err)
        assert result != PROVISION_FAILURE


class TestClassifyFailureScopeOverflow:
    def test_agent_reported_scope_overflow(self):
        t = _task("scope exceeded", {"failure_category": "scope_overflow"})
        assert classify_failure(t) == SCOPE_OVERFLOW

    def test_canonical_scope_overflow_in_context(self):
        t = _task("", {"failure_category": "SCOPE_OVERFLOW"})
        assert classify_failure(t) == SCOPE_OVERFLOW


class TestClassifyFailureBlockedPrecondition:
    """Production 2026-05-05: tasks 12679e04 + 79a4a5ec each burned a retry slot
    on a missing-on-main dispatcher file. The agent surfaced "BLOCKER:" per the
    prompt's instruction to pause; the classifier mapped that to CODE_ERROR
    (because the agent emitted ``failure_category=CODE_ERROR`` alongside the
    BLOCKER text), which counted as a real failure and re-spawned the parent.
    The "BLOCKER:" prefix must override the agent_category fast-path."""

    def test_uppercase_blocker_prefix(self):
        t = _task(
            "BLOCKER: backend/app/pipeline_regression_dispatcher.py does not exist on main",
            {"failure_category": "CODE_ERROR"},
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_mixed_case_blocker_prefix(self):
        t = _task(
            "Blocker: backend/app/pipeline_regression_dispatcher.py is not present on main",
            {"failure_category": "agent_self_failed"},
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_lowercase_blocker_prefix(self):
        t = _task("blocker: missing upstream PR")
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_leading_whitespace_before_blocker_prefix(self):
        t = _task("   BLOCKER: prompt depends on file not yet on main")
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_passing_mention_of_blocker_does_not_match(self):
        """A passing mention of "blocker" inside an error trace does NOT route here —
        only the leading-prefix style does. Anything else stays in its normal class."""
        t = _task("AssertionError: expected blocker but got None")
        assert classify_failure(t) == CODE_ERROR

    def test_high_confidence_for_blocker(self):
        t = _task("BLOCKER: upstream rollup PR not yet merged")
        result = classify_failure_with_confidence(t)
        assert result.failure_class == BLOCKED_PRECONDITION
        assert result.confidence >= 0.9

    def test_in_all_failure_classes(self):
        assert BLOCKED_PRECONDITION in ALL_FAILURE_CLASSES

    def test_in_infra_classes(self):
        """BLOCKED_PRECONDITION is infra-class so direction-health/research-productivity
        analytics don't penalize the agent for correctly pausing."""
        from app.failure_classification import INFRA_CLASSES

        assert BLOCKED_PRECONDITION in INFRA_CLASSES


class TestClassifyFailureScopeEmpty:
    """SCOPE_EMPTY sentinel routes to SCOPE_EMPTY, not CODE_ERROR, so cluster
    sibling pre-emptions don't penalize direction health or consume retry budget."""

    def test_prefix_routes_to_scope_empty(self):
        t = _task(
            "SCOPE_EMPTY: sibling cluster 2/5 (PR #4646) already merged these files"
        )
        assert classify_failure(t) == SCOPE_EMPTY

    def test_lowercase_prefix_routes_to_scope_empty(self):
        t = _task("scope_empty: related_files fully covered by sibling PR")
        assert classify_failure(t) == SCOPE_EMPTY

    def test_context_fast_path_scope_empty(self):
        t = _task("", {"failure_category": "scope_empty"})
        assert classify_failure(t) == SCOPE_EMPTY

    def test_canonical_uppercase_context_routes(self):
        t = _task("", {"failure_category": "SCOPE_EMPTY"})
        assert classify_failure(t) == SCOPE_EMPTY

    def test_scope_empty_not_code_error(self):
        t = _task("SCOPE_EMPTY: backend/app/models.py already on main via PR #4650")
        assert classify_failure(t) != CODE_ERROR

    def test_high_confidence_for_scope_empty(self):
        t = _task("SCOPE_EMPTY: sibling cluster merged the declared files")
        result = classify_failure_with_confidence(t)
        assert result.failure_class == SCOPE_EMPTY
        assert result.confidence >= 0.9

    def test_in_all_failure_classes(self):
        assert SCOPE_EMPTY in ALL_FAILURE_CLASSES

    def test_in_infra_classes(self):
        """SCOPE_EMPTY is infra-class so direction-health analytics don't penalize
        the agent for correctly detecting and exiting a pre-empted cluster."""
        from app.failure_classification import INFRA_CLASSES

        assert SCOPE_EMPTY in INFRA_CLASSES


class TestClassifyFailureFastPath:
    """context['failure_category'] shortcut preempts text matching."""

    def test_fast_path_lowercase_label(self):
        t = _task("AssertionError", {"failure_category": "rate_limited"})
        assert classify_failure(t) == RATE_LIMIT

    def test_fast_path_canonical_name(self):
        t = _task("whatever", {"failure_category": "PROVISION_FAILURE"})
        assert classify_failure(t) == PROVISION_FAILURE

    def test_fast_path_timeout_label_maps_to_agent_timeout(self):
        t = _task("anything", {"failure_category": "timeout"})
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_unknown_context_label_falls_through_to_regex(self):
        """Unrecognised context labels must not short-circuit — must re-match text."""
        t = _task("failed to provision VM", {"failure_category": "some_unknown"})
        assert classify_failure(t) == PROVISION_FAILURE

    def test_retry_after_exhausted_maps_to_rate_limit(self):
        """Orchestrator auto-fail after too many retry_after cycles must not be CODE_ERROR."""
        t = _task(
            "Auto-failed after 16 retry_after cycles (threshold 16); held 240m. "
            "Pool state: all accounts in rate-limit cooldown (earliest recovery: 2026-04-24T12:30:00+00:00).",
            {"failure_category": "retry_after_exhausted"},
        )
        assert classify_failure(t) == RATE_LIMIT


class TestClassifyFailurePipelineRateLimitMessages:
    """Pipeline-internal rate-limit status strings must not fall through to CODE_ERROR."""

    def test_requeued_waiting_for_account_cooldown(self):
        """The exact fingerprint phrase that was creating spurious auto-remediation tasks."""
        t = _task("Rate limited — requeued, waiting for account cooldown")
        assert classify_failure(t) == RATE_LIMIT

    def test_requeued_with_exhausted_account_blacklisted(self):
        t = _task("Rate limited — requeued with exhausted account blacklisted")
        assert classify_failure(t) == RATE_LIMIT

    def test_all_accounts_rate_limited_waiting_for_cooldown(self):
        t = _task("All accounts rate-limited — waiting for cooldown")
        assert classify_failure(t) == RATE_LIMIT

    def test_all_accounts_in_rate_limit_cooldown(self):
        t = _task(
            "all accounts in rate-limit cooldown (earliest recovery: 2026-04-24T12:30:00+00:00)"
        )
        assert classify_failure(t) == RATE_LIMIT

    def test_auto_failed_with_rate_limit_pool_state(self):
        t = _task(
            "Auto-failed after 16 retry_after cycles (threshold 16); held 240m. "
            "Pool state: all accounts in rate-limit cooldown (earliest recovery: 2026-04-24T12:30:00+00:00).",
        )
        assert classify_failure(t) == RATE_LIMIT

    def test_github_rate_limit_with_cooldown_is_still_external_api(self):
        """GitHub rate limits must still route to EXTERNAL_API, not RATE_LIMIT."""
        t = _task("GitHub API rate limit exceeded — cooldown active")
        assert classify_failure(t) == EXTERNAL_API


class TestClassifyFailureEdgeCases:
    def test_empty_status_is_agent_timeout(self):
        assert classify_failure(_task("")) == AGENT_TIMEOUT

    def test_whitespace_only_logs_is_agent_timeout(self):
        assert classify_failure(_task(""), logs="   \n\t") == AGENT_TIMEOUT

    def test_none_status_is_agent_timeout(self):
        t = _task("")
        t.latest_status_message = None
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_none_context_does_not_crash(self):
        t = MagicMock(spec=Task)
        t.latest_status_message = "AssertionError: bad"
        t.context = None
        assert classify_failure(t) == CODE_ERROR

    def test_logs_field_contributes_to_match(self):
        t = _task("")
        assert classify_failure(t, logs="No heartbeat received") == AGENT_TIMEOUT

    def test_case_insensitive(self):
        assert classify_failure(_task("FAILED TO PROVISION VM")) == PROVISION_FAILURE


class TestClassifyFailurePriority:
    """Ambiguous messages that touch multiple keyword banks must route by priority."""

    def test_iteration_cap_before_merge_blocked(self):
        """Workflow-limit check runs before merge-blocked EXTERNAL_API bank."""
        msg = "merge blocked, fix not spawned (iteration cap)"
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    def test_github_rate_limit_is_external_not_rate_limit(self):
        """GitHub 429 must not disable the Claude account."""
        assert classify_failure(_task("GitHub API rate limit exceeded")) == EXTERNAL_API

    def test_overload_is_external_not_rate_limit(self):
        """Anthropic 529 overload is capacity, not account rate limit."""
        assert classify_failure(_task("Error: Overloaded")) == EXTERNAL_API


class TestClassifyFailureTaxonomyEdges:
    def test_empty_task_is_infra_via_agent_timeout(self):
        """Empty status → AGENT_TIMEOUT → infra_related."""
        t = _task("")
        assert classify_failure_taxonomy(t) == TAXONOMY_INFRA_RELATED

    def test_review_quality_tsc_keyword(self):
        t = _task("tsc: type error in handler.ts", {"scope": "small"})
        assert classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY

    def test_scope_related_from_exceeds_scope_message(self):
        t = _task("aborted: exceeds scope budget", {"scope": "small"})
        assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED

    def test_code_defect_default(self):
        t = _task("AttributeError: NoneType has no 'x'", {"scope": "small"})
        assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT

    def test_scope_overflow_is_scope_related_not_infra(self):
        """SCOPE_OVERFLOW is in INFRA_CLASSES for retry routing, but the
        taxonomy must classify it as scope_related for analytics."""
        t = _task("", {"failure_category": "SCOPE_OVERFLOW", "scope": "small"})
        assert classify_failure(t) == SCOPE_OVERFLOW
        assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED

    def test_bare_complexity_is_code_defect_not_scope(self):
        """'time complexity' / 'reduce complexity' are code-quality, not scope."""
        t = _task("reduce time complexity of implementation", {"scope": "small"})
        assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT

    def test_scope_complexity_phrase_is_scope_related(self):
        t = _task("aborted: scope complexity exceeds budget", {"scope": "small"})
        assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED


def _code_defect_task(
    task_type: str = "create", direction_id: str | None = "d-1"
) -> Task:
    ctx: dict = {"scope": "small"}
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    t = _task("AttributeError: NoneType has no 'x'", ctx)
    t.payloads = [{"type": task_type, "prompt": "p"}]
    return t


class TestShouldTriggerCodeDefectInvestigation:
    def test_code_defect_with_direction_triggers(self):
        assert should_trigger_code_defect_investigation(_code_defect_task()) is True

    def test_no_direction_skipped(self):
        assert (
            should_trigger_code_defect_investigation(
                _code_defect_task(direction_id=None)
            )
            is False
        )

    def test_investigate_type_skipped(self):
        assert (
            should_trigger_code_defect_investigation(
                _code_defect_task(task_type="investigate")
            )
            is False
        )

    def test_smoketest_type_skipped(self):
        assert (
            should_trigger_code_defect_investigation(
                _code_defect_task(task_type="smoketest")
            )
            is False
        )

    def test_review_quality_taxonomy_skipped(self):
        t = _task("pytest failures in test_foo", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_code_defect_investigation(t) is False

    def test_infra_failure_skipped(self):
        t = _task("No heartbeat received", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_code_defect_investigation(t) is False


def _timeout_task(
    subtype: str, task_type: str = "create", direction_id: str | None = "d-1"
) -> Task:
    ctx: dict = {"timeout_subtype": subtype}
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    t = _task("No heartbeat received after 300s", ctx)
    t.payloads = [{"type": task_type, "prompt": "p"}]
    return t


class TestShouldTriggerTimeoutInvestigation:
    def test_oom_triggers(self):
        assert should_trigger_timeout_investigation(_timeout_task("oom")) is True

    def test_process_crash_triggers(self):
        assert (
            should_trigger_timeout_investigation(_timeout_task("process_crash")) is True
        )

    def test_heartbeat_lost_triggers(self):
        assert (
            should_trigger_timeout_investigation(_timeout_task("heartbeat_lost"))
            is True
        )

    def test_cascade_skipped(self):
        assert (
            should_trigger_timeout_investigation(_timeout_task("cascade_cancellation"))
            is False
        )

    def test_iteration_limit_skipped(self):
        assert (
            should_trigger_timeout_investigation(_timeout_task("iteration_limit"))
            is False
        )

    def test_no_direction_skipped(self):
        assert (
            should_trigger_timeout_investigation(
                _timeout_task("oom", direction_id=None)
            )
            is False
        )

    def test_investigate_type_skipped(self):
        assert (
            should_trigger_timeout_investigation(
                _timeout_task("oom", task_type="investigate")
            )
            is False
        )

    def test_non_timeout_failure_skipped(self):
        t = _task(
            "AttributeError: NoneType has no 'x'",
            {"direction_id": "d-1", "timeout_subtype": "oom"},
        )
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_timeout_investigation(t) is False


def _review_quality_task(
    task_type: str = "create", direction_id: str | None = "d-1"
) -> Task:
    ctx: dict = {"scope": "small"}
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    t = _task("pytest failures in test_foo", ctx)
    t.payloads = [{"type": task_type, "prompt": "p"}]
    return t


class TestShouldTriggerReviewQualityInvestigation:
    def test_review_quality_with_direction_triggers(self):
        assert (
            should_trigger_review_quality_investigation(_review_quality_task()) is True
        )

    def test_no_direction_skipped(self):
        assert (
            should_trigger_review_quality_investigation(
                _review_quality_task(direction_id=None)
            )
            is False
        )

    def test_investigate_type_skipped(self):
        assert (
            should_trigger_review_quality_investigation(
                _review_quality_task(task_type="investigate")
            )
            is False
        )

    def test_smoketest_type_skipped(self):
        assert (
            should_trigger_review_quality_investigation(
                _review_quality_task(task_type="smoketest")
            )
            is False
        )

    def test_code_defect_taxonomy_skipped(self):
        t = _task("AttributeError: NoneType has no 'x'", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_review_quality_investigation(t) is False

    def test_infra_failure_skipped(self):
        t = _task("No heartbeat received", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_review_quality_investigation(t) is False


class TestFingerprintMessage:
    def test_strips_uuid(self):
        fp = _fingerprint_message("task 550e8400-e29b-41d4-a716-446655440000 failed")
        assert "<id>" in fp
        assert "550e8400" not in fp

    def test_strips_numbers(self):
        fp = _fingerprint_message("exit code 137 after 300s")
        assert "<n>" in fp
        assert "137" not in fp

    def test_strips_memory_prefixes(self):
        msg1 = "[16mb] using tool: bash"
        msg2 = "[16mb cg:512mb] using tool: bash"
        assert _fingerprint_message(msg1) == "using tool: bash"
        assert _fingerprint_message(msg2) == "using tool: bash"

    def test_strips_decimal_memory_prefixes(self):
        """Regression: the residual-fingerprint spawner was producing one
        fingerprint per decimal memory reading ([10.5MB] / [10.4MB] / …)
        because the old prefix regex required integer-only `\\d+mb`."""
        msg1 = "[10.5mb] clone complete, configuring workspace..."
        msg2 = "[10.4mb] clone complete, configuring workspace..."
        msg3 = "[10.5MB cg:512.4MB] clone complete, configuring workspace..."
        assert _fingerprint_message(msg1) == _fingerprint_message(msg2)
        assert _fingerprint_message(msg2) == _fingerprint_message(msg3)
        assert "mb" not in _fingerprint_message(msg1)

    def test_strips_embedded_size_tokens(self):
        """Size tokens mid-message ('15.4MB' not as prefix) collapse too."""
        msg1 = "rss 15.4mb heap 12.1mb after retry"
        msg2 = "rss 15.6mb heap 12.3mb after retry"
        assert _fingerprint_message(msg1) == _fingerprint_message(msg2)

    def test_strips_embedded_iec_and_byte_size_tokens(self):
        msg1 = "rss=15.4MiB heap 123456 bytes after retry"
        msg2 = "rss=15.6MiB heap 654321 bytes after retry"
        fp = _fingerprint_message(msg1)
        assert fp == _fingerprint_message(msg2)
        assert "mib" not in fp
        assert "bytes" not in fp

    def test_near_duplicates_cluster(self):
        fp1 = _fingerprint_message("AssertionError: expected 3, got 4")
        fp2 = _fingerprint_message("AssertionError: expected 9, got 12")
        assert fp1 == fp2

    def test_empty_returns_empty_string(self):
        assert _fingerprint_message("") == ""

    def test_truncates_long_messages(self):
        assert len(_fingerprint_message("x" * 500)) <= 200


class TestResidualStore:
    def setup_method(self):
        import app.failure_classification as fc

        fc._residual_store.clear()
        fc._residual_seen_task_ids.clear()
        fc._residual_closed_fingerprints.clear()

    def _task_with_id(self, msg: str, task_id: str, status=None) -> "Task":
        from app.models import TaskStatus

        t = MagicMock(spec=Task)
        t.latest_status_message = msg
        t.context = {}
        t.id = task_id
        # Residual-fingerprint Gate 1 requires FAILED. Tests that need a
        # different status pass it explicitly.
        t.status = status if status is not None else TaskStatus.FAILED
        return t

    def test_record_then_query(self):
        t = self._task_with_id("[10.5MB] Cloning repo...", "task-1")
        record_residual_fingerprint(t, logs="AttributeError: NoneType has no attr 'x'")
        results = get_top_residual_fingerprints(window_seconds=3600)
        assert len(results) == 1
        assert results[0]["count"] == 1
        assert results[0]["task_ids"] == ["task-1"]

    def test_dedup_same_task_id(self):
        t = self._task_with_id("[10.5MB] Cloning...", "task-abc")
        record_residual_fingerprint(t, logs="TypeError: some error")
        record_residual_fingerprint(t, logs="TypeError: some error")
        results = get_top_residual_fingerprints()
        assert results[0]["count"] == 1

    def test_near_duplicates_cluster(self):
        t1 = self._task_with_id("[10.5MB] Cloning...", "task-1")
        t2 = self._task_with_id("[10.5MB] Cloning...", "task-2")
        record_residual_fingerprint(t1, logs="AttributeError: expected 3, got 4")
        record_residual_fingerprint(t2, logs="AttributeError: expected 9, got 12")
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 2

    def test_classify_failure_fallthrough_records(self):
        t = self._task_with_id("[10.5MB] Cloning...", "task-ff")
        # Status alone never drives the fingerprint — pass logs with an
        # error-shaped line.
        result = classify_failure(t, logs="AttributeError: NoneType has no attr 'x'")
        assert result == CODE_ERROR
        results = get_top_residual_fingerprints()
        assert any(
            r["task_ids"] == ["task-ff"] or "task-ff" in r["task_ids"] for r in results
        )

    def test_non_failed_status_does_not_record(self):
        """Gate 1: classify_failure may be called from analytics paths on
        COMPLETED / CANCELLED / EVALUATING tasks (read-only classification).
        Those calls must NOT pollute the residual store with success-shape
        inputs (rollup messages, plan output JSON, review approvals)."""
        from app.models import TaskStatus

        for non_failed_status in (
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.EVALUATING,
            TaskStatus.IN_PROGRESS,
        ):
            import app.failure_classification as fc

            fc._residual_store.clear()
            fc._residual_seen_task_ids.clear()
            t = self._task_with_id(
                "TypeError: real error in logs",
                f"task-{non_failed_status}",
                status=non_failed_status,
            )
            record_residual_fingerprint(t, logs="TypeError: real error in logs")
            results = get_top_residual_fingerprints()
            assert results == [], (
                f"status={non_failed_status} should not record, got {results}"
            )

    def test_no_error_line_in_logs_does_not_record(self):
        """Gate 2: no fallback to latest_status_message. If logs lack an
        error-shaped line, no fingerprint is recorded — the status message
        is almost always a benign heartbeat that pollutes the store."""
        t = self._task_with_id("[10.5MB] Cloning org/repo...", "task-noerr")
        record_residual_fingerprint(t)  # no logs at all
        assert get_top_residual_fingerprints() == []
        record_residual_fingerprint(
            t, logs="just some progress info\nnothing exceptional"
        )
        assert get_top_residual_fingerprints() == []

    def test_json_output_does_not_record_even_with_exception_keyword(self):
        """Regression for the 2026-05-04 plan-output false-positive: a
        plan task's serialized JSON output mentioning ``Exception`` inside
        a prompt string (e.g. lint guidance ``avoid except Exception:
        blocks (BLE001)``) used to fingerprint the JSON prefix as a
        CODE_ERROR sample, which then drove auto-remediation tasks that
        ``fixed`` a research prompt rather than a real bug."""
        json_logs = (
            '{"subtasks": [{"prompt": "avoid except Exception: blocks (BLE001) '
            'and prefer specific exception classes"}]}'
        )
        t = self._task_with_id("[10.5MB] Cloning...", "task-json")
        record_residual_fingerprint(t, logs=json_logs)
        assert get_top_residual_fingerprints() == []

    def test_lint_prose_mentioning_exception_does_not_record(self):
        """Substring-anywhere matching used to flag prose like
        ``avoid except Exception: blocks`` as error-shaped. Strict
        line-start patterns require the keyword at a structural position."""
        t = self._task_with_id("[10.5MB] Cloning...", "task-prose")
        record_residual_fingerprint(
            t, logs="avoid except Exception: blocks (BLE001)\nuse specific classes"
        )
        assert get_top_residual_fingerprints() == []

    def test_fingerprint_prefers_error_line_over_status_message(self):
        """Regression: status messages were dominating the fingerprint
        even when structured error text was available in the logs."""
        t1 = self._task_with_id("[10.5mb] Cloning repo...", "task-A")
        t2 = self._task_with_id("[10.4mb] Rebasing onto main...", "task-B")
        # Same error in logs — different status heartbeats. Should cluster.
        err_logs = "TypeError: getProviderRunner is not a function\n  at handleCreate"
        record_residual_fingerprint(t1, logs=err_logs)
        record_residual_fingerprint(t2, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1, f"expected 1 fingerprint, got {len(results)}"
        assert results[0]["count"] == 2

    def test_status_message_retained_as_context(self):
        """Even when fingerprint is driven by the error line, the agent
        status at failure time is still stored for human display."""
        t = self._task_with_id("[12.3mb] Running tests...", "task-status")
        err_logs = "AssertionError: expected true to be false"
        record_residual_fingerprint(t, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        # sample_message = error line (fingerprint source)
        assert "AssertionError" in results[0]["sample_message"]
        # sample_status = original heartbeat (human context)
        assert "Running tests" in results[0].get("sample_status", "")

    def test_window_filter_excludes_old(self):
        import app.failure_classification as fc

        fc._residual_store["old-fp"] = {
            "count": 5,
            "first_seen": 0.0,
            "last_seen": 0.0,
            "sample_message": "old error",
            "task_ids": ["t-old"],
        }
        results = get_top_residual_fingerprints(window_seconds=60)
        assert all(r["fingerprint"] != "old-fp" for r in results)

    def test_sorted_by_count_descending(self):
        import app.failure_classification as fc
        import time

        now = time.time()
        fc._residual_store["fp-a"] = {
            "count": 3,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "a",
            "task_ids": [],
        }
        fc._residual_store["fp-b"] = {
            "count": 10,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "b",
            "task_ids": [],
        }
        results = get_top_residual_fingerprints()
        assert results[0]["fingerprint"] == "fp-b"
        assert results[1]["fingerprint"] == "fp-a"

    def test_no_record_for_non_code_error(self):
        t = self._task_with_id("No heartbeat received after 300s", "task-timeout")
        result = classify_failure(t)
        assert result == AGENT_TIMEOUT
        results = get_top_residual_fingerprints()
        assert not any("task-timeout" in r["task_ids"] for r in results)

    def test_heartbeat_only_contributions_do_not_record(self):
        """Reversed from prior behavior (2026-05-04): heartbeat-only
        contributions used to be recorded with ``is_error_shaped=False``
        so the spawn-site threshold could filter them. But carrying them
        in the store still polluted the admin-UI residual list and
        reactivated CLOSED_ORGANICALLY DB rows. The strict gate now
        requires an error-shaped log line; heartbeat-only contributions
        are dropped entirely."""
        for benign in (
            "PROVISION_PROGRESS: creds fetched, entering env setup",
            "[12.1MB] Using tool: Grep",
            "Creating pull request...",
            "[10.5MB cg:512MB] Cloning org/repo...",
            "Rebasing onto main...",
            "Capturing test baseline...",
            "Fetching GitHub credentials...",
            "DIAGNOSTIC_TEST",
        ):
            import app.failure_classification as fc

            fc._residual_store.clear()
            fc._residual_seen_task_ids.clear()
            t = self._task_with_id(benign, f"task-benign-{hash(benign)}")
            record_residual_fingerprint(t)
            results = get_top_residual_fingerprints()
            assert results == [], f"{benign!r} should not record, got {results}"

    def test_error_shaped_logs_record_as_error_shaped(self):
        """``[ERROR] ...`` in logs is the agent's explicit error prefix
        (agent/src/index.ts:2606). With strict gating the signal must be
        in the logs argument, not just the status field — but matching is
        unchanged."""
        t = self._task_with_id("[10.5MB] Cloning...", "task-err-logs")
        record_residual_fingerprint(t, logs="[ERROR] connection refused after 30s")
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["is_error_shaped"] is True

    def test_logs_with_real_error_mark_cluster_error_shaped(self):
        """A heartbeat-shaped status combined with an error-shaped log
        line still produces an error-shaped cluster (fingerprint comes
        from the log line, not the status)."""
        t = self._task_with_id("[10.5MB] Using tool: Bash", "task-real-err")
        err_logs = "TypeError: 'NoneType' object is not callable\n  at handle"
        record_residual_fingerprint(t, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["is_error_shaped"] is True
        assert "TypeError" in results[0]["sample_message"]

    def test_repeat_error_contributions_cluster_and_stay_error_shaped(self):
        """Two FAILED tasks with the same error line cluster into one
        fingerprint. ``is_error_shaped`` is always True under strict
        gating since unshaped contributions never reach the store."""
        t1 = self._task_with_id("[10.5MB] Cloning...", "task-1")
        t2 = self._task_with_id("[10.5MB] Cloning...", "task-2")
        err = "AttributeError: NoneType has no attr 'x'"
        record_residual_fingerprint(t1, logs=err)
        record_residual_fingerprint(t2, logs=err)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 2
        assert results[0]["is_error_shaped"] is True

    def test_looks_like_error_predicate(self):
        # Real error-shaped lines.
        assert _looks_like_error("AttributeError: NoneType has no attr 'x'")
        assert _looks_like_error("TypeError: foo is not a function")
        assert _looks_like_error("[ERROR] connection refused")
        assert _looks_like_error("[FATAL] DB connection lost")
        assert _looks_like_error("Traceback (most recent call last):")
        assert _looks_like_error("AssertionError: expected true")
        assert _looks_like_error("FAILED: build step 3")
        assert _looks_like_error("FAIL: test_foo")
        assert _looks_like_error("Error: file not found")
        assert _looks_like_error("cannot find module 'x'")
        assert _looks_like_error("no such file or directory")
        # Whitespace tolerated at line start.
        assert _looks_like_error("    raise AttributeError('nope')")
        assert _looks_like_error("  TypeError: bad arg")

        # Benign agent heartbeats — no match.
        assert not _looks_like_error("PROVISION_PROGRESS: payload ready")
        assert not _looks_like_error("[10.5MB] Using tool: Bash")
        assert not _looks_like_error("Creating pull request...")
        assert not _looks_like_error("Rebasing onto main...")
        assert not _looks_like_error("DIAGNOSTIC_TEST")
        assert not _looks_like_error("")
        assert not _looks_like_error(None)

        # Strict-shape regression cases (the prior substring matcher
        # would have accepted these and polluted the residual store).
        assert not _looks_like_error(
            "avoid except Exception: blocks (BLE001)"
        )  # exception keyword in the middle of prose
        assert not _looks_like_error(
            "review notes: this catches the regression risk exception cases"
        )  # exception keyword as a normal noun
        assert not _looks_like_error(
            '{"prompt": "see TypeError handling at line 42"}'
        )  # JSON object — skipped outright
        assert not _looks_like_error(
            "all 3 subtasks completed with no actionable changes"
        )  # success message
        assert not _looks_like_error(
            "research created 4 tasks from 5 findings"
        )  # success message
        assert not _looks_like_error(
            "approved. the pr cleanly wires already-persisted data"
        )  # review approval

    def test_get_top_residual_fingerprints_excludes_closed_cases(self):
        """Fingerprints whose case has been closed must be absent from results."""
        import app.failure_classification as fc
        import time

        now = time.time()
        fc._residual_store["open-fp"] = {
            "count": 5,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "open error",
            "task_ids": ["t1"],
        }
        fc._residual_store["closed-fp"] = {
            "count": 8,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "closed error",
            "task_ids": ["t2"],
        }
        fc._mark_residual_case_closed("closed-fp")

        results = get_top_residual_fingerprints()

        fps = [r["fingerprint"] for r in results]
        assert "open-fp" in fps
        assert "closed-fp" not in fps

    def test_record_residual_fingerprint_reactivates_closed_case(self):
        """A new occurrence of a closed fingerprint removes it from the closed set."""
        import app.failure_classification as fc

        fc._mark_residual_case_closed("some-fp")
        assert "some-fp" in fc._residual_closed_fingerprints

        t = self._task_with_id("[10.5MB] Cloning...", "task-reactivate")
        err = "AttributeError: some-fp error text"
        # Seed the store with the existing fingerprint so the update path runs
        fp = fc._fingerprint_message(err) or "<empty>"
        fc._mark_residual_case_closed(fp)
        record_residual_fingerprint(t, logs=err)

        # After recording a new occurrence the fingerprint must be gone from closed set
        assert fp not in fc._residual_closed_fingerprints


class TestPipelineRegressionCloseLoop:
    """State-machine unit tests for RegressionRemediationCase close-loop transitions.

    Covers: ACTIVE→MERGED (happy path), MERGED→CLOSED (signal recovery),
    recurrence after MERGED (re_arm), and failed-remediation escalation to
    NEEDS_MANUAL_INTERVENTION.
    """

    def _make_case(self, status: str | None = None):
        from app.models import RegressionRemediationCase

        case = RegressionRemediationCase(
            project_id=uuid.uuid4(),
            regression_class="sr_catch_rate_drop",
        )
        if status is not None:
            case.status = status
        return case

    def test_mark_merged_happy_path(self):
        from app.models import RegressionCaseStatus

        case = self._make_case()
        snapshot = {"value": 0.75, "threshold": 0.7}
        case.mark_merged(pr_number=42, signal_snapshot=snapshot)
        assert case.status == RegressionCaseStatus.MERGED
        assert case.signal_snapshot_at_merge == snapshot
        assert case.last_remediation_pr_number == 42
        assert case.last_remediation_result == "merged"
        assert len(case.fingerprint_to_fix_log) == 1
        assert case.fingerprint_to_fix_log[0]["fix_pr_number"] == 42
        assert case.fingerprint_to_fix_log[0]["fingerprint"] == "sr_catch_rate_drop"

    def test_mark_closed_signal_recovery(self):
        from app.models import RegressionCaseStatus

        case = self._make_case(status=RegressionCaseStatus.MERGED)
        case.mark_closed()
        assert case.status == RegressionCaseStatus.CLOSED
        assert case.last_remediation_result == "closed"

    def test_re_arm_below_cap_reactivates(self):
        from app.models import RegressionCaseStatus

        case = self._make_case()
        case.re_arm(max_attempts=3)
        assert case.status == RegressionCaseStatus.ACTIVE
        assert case.attempts == 1
        assert case.last_remediation_result == "failed"

    def test_re_arm_at_cap_escalates(self):
        from app.models import RegressionCaseStatus

        case = self._make_case()
        case.attempts = 2
        case.re_arm(max_attempts=3)
        assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
        assert case.needs_manual_intervention_at is not None

    def test_re_arm_after_merged_recurrence(self):
        from app.models import RegressionCaseStatus

        case = self._make_case(status=RegressionCaseStatus.MERGED)
        case.re_arm(max_attempts=3)
        assert case.status == RegressionCaseStatus.ACTIVE

    def test_fix_log_accumulates_across_merged_prs(self):
        case = self._make_case()
        case.mark_merged(pr_number=10, signal_snapshot={})
        case.mark_merged(pr_number=20, signal_snapshot={})
        pr_numbers = [e["fix_pr_number"] for e in case.fingerprint_to_fix_log]
        assert pr_numbers == [10, 20]

    # prior_attempt_context_suffix tests

    def test_prior_attempt_context_suffix_attempts_1_empty(self):
        """attempts == 1: suffix is empty so first-attempt prompts are unchanged."""
        case = self._make_case()
        case.attempts = 1
        case.last_remediation_pr_number = 999
        assert case.prior_attempt_context_suffix() == ""

    def test_prior_attempt_context_suffix_attempts_2_with_pr_and_signal(self):
        """attempts == 2: suffix names prior PR and post-merge signal value."""
        case = self._make_case()
        case.attempts = 2
        case.last_remediation_pr_number = 5678
        case.signal_snapshot_at_merge = {"current_rate": 0.65, "prior_rate": 0.80}
        suffix = case.prior_attempt_context_suffix()
        assert "PRIOR ATTEMPT CONTEXT" in suffix
        assert "#5678" in suffix
        assert "65.0%" in suffix
        assert (
            "different angle" in suffix.lower()
            or "different contributing" in suffix.lower()
        )

    def test_prior_attempt_context_suffix_no_pr_no_pr_reference(self):
        """attempts >= 2 but no prior PR: suffix must not mention a PR number."""
        case = self._make_case()
        case.attempts = 2
        case.last_remediation_pr_number = None
        suffix = case.prior_attempt_context_suffix()
        assert "PRIOR ATTEMPT CONTEXT" in suffix
        assert "PR #" not in suffix

    def test_prior_attempt_context_suffix_no_signal_snapshot_skips_rate(self):
        """attempts >= 2 with empty merge snapshot: suffix omits the signal line."""
        case = self._make_case()
        case.attempts = 3
        case.signal_snapshot_at_merge = {}
        suffix = case.prior_attempt_context_suffix()
        assert "PRIOR ATTEMPT CONTEXT" in suffix
        assert "Signal value" not in suffix


class TestDeriveFailureStageMigration:
    """Migration-specific keywords map to the 'migration' stage."""

    def test_revision_error(self):
        from app.failure_stage import _derive_failure_stage

        assert (
            _derive_failure_stage(
                "MIGRATION_ERROR", "alembic.script.revision.RevisionError: foo"
            )
            == "migration"
        )

    def test_multiple_heads(self):
        from app.failure_stage import _derive_failure_stage

        assert (
            _derive_failure_stage(
                "MIGRATION_ERROR",
                "Multiple heads are present for given argument 'head'",
            )
            == "migration"
        )

    def test_undefined_column_maps_to_migration_not_integration(self):
        from app.failure_stage import _derive_failure_stage

        # UndefinedColumnError would match the import-error bank; migration
        # must be checked first.
        assert (
            _derive_failure_stage(
                "MIGRATION_ERROR",
                "psycopg.errors.UndefinedColumnError: column x does not exist",
            )
            == "migration"
        )

    def test_schema_version_mismatch(self):
        from app.failure_stage import _derive_failure_stage

        assert (
            _derive_failure_stage(
                "MIGRATION_ERROR",
                "schema version mismatch: expected_schema_revision=0037",
            )
            == "migration"
        )

    def test_alembic_command_error(self):
        from app.failure_stage import _derive_failure_stage

        assert (
            _derive_failure_stage(
                "MIGRATION_ERROR",
                "alembic.util.exc.CommandError: can't locate revision",
            )
            == "migration"
        )


class TestExternalApiSubtypeFromMessage:
    def test_db_connectivity(self):
        assert (
            _external_api_subtype_from_message(
                "psycopg.OperationalError: connection refused"
            )
            == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_db_connectivity_asyncpg(self):
        assert (
            _external_api_subtype_from_message(
                "asyncpg.exceptions.ConnectionFailure: too many clients"
            )
            == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_db_connectivity_could_not_connect(self):
        assert (
            _external_api_subtype_from_message(
                "could not connect to server: Connection refused"
            )
            == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_provider_model_config(self):
        msg = "ModelNotFoundError: models/gemini-3.1-flash-preview is not found for API version v1beta"
        assert (
            _external_api_subtype_from_message(msg)
            == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG
        )

    def test_provider_auth_unauthorized(self):
        assert (
            _external_api_subtype_from_message(
                "401 Unauthorized from api.anthropic.com"
            )
            == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH
        )

    def test_provider_auth_bad_credentials(self):
        assert (
            _external_api_subtype_from_message("Bad credentials returned from GitHub")
            == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH
        )

    def test_provider_auth_token_expired(self):
        assert (
            _external_api_subtype_from_message("token expired, please re-authenticate")
            == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH
        )

    def test_provider_auth_oauth_org_disabled(self):
        # Anthropic 403 when an org has disabled OAuth for Claude Code.
        assert (
            _external_api_subtype_from_message(
                "HTTP 403 oauth_org_not_allowed: organization has disabled this"
            )
            == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH
        )

    def test_github_pr_create(self):
        assert (
            _external_api_subtype_from_message(
                "failed to create pull request: 422 Unprocessable Entity"
            )
            == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE
        )

    def test_github_pr_create_rollup(self):
        assert (
            _external_api_subtype_from_message("rollup pr creation failed on main")
            == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE
        )

    def test_github_pr_create_merge_blocked(self):
        assert (
            _external_api_subtype_from_message(
                "merge blocked by branch protection rules"
            )
            == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE
        )

    def test_provider_quota_overloaded(self):
        assert (
            _external_api_subtype_from_message("Error: Overloaded")
            == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA
        )

    def test_provider_quota_context_length(self):
        assert (
            _external_api_subtype_from_message(
                "context_length_exceeded on model request"
            )
            == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA
        )

    def test_provider_quota_github_rate_limit(self):
        assert (
            _external_api_subtype_from_message(
                "api.github.com returned 429 too many requests"
            )
            == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA
        )

    def test_network_transient_503(self):
        assert (
            _external_api_subtype_from_message("503 service unavailable from upstream")
            == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT
        )

    def test_network_transient_ssl(self):
        assert (
            _external_api_subtype_from_message("SSL handshake failed during connection")
            == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT
        )

    def test_network_transient_dns(self):
        assert (
            _external_api_subtype_from_message(
                "could not resolve hostname api.example.com"
            )
            == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT
        )

    def test_other_fallback(self):
        assert (
            _external_api_subtype_from_message("some unknown external api error")
            == EXTERNAL_API_SUBTYPE_OTHER
        )

    def test_empty_is_other(self):
        assert _external_api_subtype_from_message("") == EXTERNAL_API_SUBTYPE_OTHER


class TestClassifyExternalApiSubtype:
    def test_delegates_to_helper(self):
        task = _FakeTask("psycopg.OperationalError: connection refused")
        assert (
            classify_external_api_subtype(task) == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_combines_logs(self):
        task = _FakeTask("")
        assert (
            classify_external_api_subtype(task, logs="asyncpg connection failure")
            == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_none_status(self):
        task = _FakeTask(None)  # type: ignore[arg-type]
        assert classify_external_api_subtype(task) == EXTERNAL_API_SUBTYPE_OTHER


class TestPersistFailureCategoryExternalApiSubtype:
    def test_external_api_persists_subtype(self):
        task = _task("psycopg.OperationalError: connection refused")
        persist_failure_category(task, EXTERNAL_API)
        assert task.context["failure_category"] == EXTERNAL_API
        assert (
            task.context["external_api_subtype"] == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY
        )

    def test_external_api_persists_auth_subtype(self):
        task = _task("401 Unauthorized")
        persist_failure_category(task, EXTERNAL_API)
        assert (
            task.context["external_api_subtype"] == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH
        )

    def test_external_api_persists_model_config_subtype(self):
        task = _task(
            "400: Unsupported value: 'service_tier' does not support 'flex' with this model"
        )
        persist_failure_category(task, EXTERNAL_API)
        assert (
            task.context["external_api_subtype"]
            == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG
        )

    def test_non_external_api_does_not_write_subtype(self):
        task = _task("No heartbeat received")
        persist_failure_category(task, AGENT_TIMEOUT)
        assert "external_api_subtype" not in task.context


class TestAccountFailureClassTracking:
    def setup_method(self):
        import app.failure_classification as fc

        fc._account_failure_log.clear()

    def test_record_and_retrieve_counts(self):
        record_account_failure_class("acct-1", CODE_ERROR)
        record_account_failure_class("acct-1", CODE_ERROR)
        record_account_failure_class("acct-1", RATE_LIMIT)
        counts = get_account_failure_class_counts("acct-1")
        assert counts[CODE_ERROR] == 2
        assert counts[RATE_LIMIT] == 1
        assert counts[EXTERNAL_API] == 0

    def test_unknown_account_returns_zero_counts(self):
        counts = get_account_failure_class_counts("nonexistent-acct")
        assert all(v == 0 for v in counts.values())
        assert set(counts.keys()) == set(ALL_FAILURE_CLASSES)

    def test_window_filter_excludes_old_events(self):
        import app.failure_classification as fc
        import time

        fc._account_failure_log["acct-2"] = [(CODE_ERROR, 0.5, time.time() - 90000)]
        counts = get_account_failure_class_counts("acct-2", window_seconds=86400)
        assert counts[CODE_ERROR] == 0

    def test_record_and_retrieve_raw_records_with_confidence(self):
        record_account_failure_class("acct-raw", CODE_ERROR, confidence=0.5)
        record_account_failure_class("acct-raw", RATE_LIMIT, confidence=0.8)
        records = get_account_failure_class_records("acct-raw")
        assert len(records) == 2
        assert any(rec[0] == CODE_ERROR and rec[1] == 0.5 for rec in records)
        assert any(rec[0] == RATE_LIMIT and rec[1] == 0.8 for rec in records)

    def test_persist_failure_category_forwards_confidence(self):
        task = _task("some error", ctx={"llm_account_id": "acct-confidence"})
        persist_failure_category(task, CODE_ERROR, confidence=0.5)
        records = get_account_failure_class_records("acct-confidence")
        assert len(records) == 1
        assert records[0][0] == CODE_ERROR
        assert records[0][1] == 0.5

    def test_persist_failure_category_records_for_account(self):
        task = _task("some error", ctx={"llm_account_id": "acct-3"})
        persist_failure_category(task, CODE_ERROR)
        counts = get_account_failure_class_counts("acct-3")
        assert counts[CODE_ERROR] == 1

    def test_persist_without_account_id_does_not_raise(self):
        task = _task("some error")
        persist_failure_category(task, CODE_ERROR)

    def test_all_failure_classes_coverage(self):
        for cls in ALL_FAILURE_CLASSES:
            record_account_failure_class("acct-all", cls)
        counts = get_account_failure_class_counts("acct-all")
        assert all(counts[cls] == 1 for cls in ALL_FAILURE_CLASSES)


class TestResidualFingerprintDbWrite:
    """Tests for _upsert_residual_case_async DB persistence."""

    @pytest_asyncio.fixture
    async def session_factory(self):
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        yield factory
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_insert_new_fingerprint(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_classification import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-new", now_dt, "some error")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-new",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.hit_count == 1
        assert row.status == ResidualCaseStatus.ACTIVE
        assert row.sample_message == "some error"

    @pytest.mark.asyncio
    async def test_second_hit_bumps_count(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_classification import _upsert_residual_case_async
        from app.models import ResidualRemediationCase
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-bump", now_dt, "err")
        await _upsert_residual_case_async(project_id, "fp-bump", now_dt, "err2")

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-bump",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].hit_count == 2

    @pytest.mark.asyncio
    async def test_closed_organically_reopened(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_classification import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-closed", now_dt, "err")
        async with session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-closed",
                        )
                    )
                )
                .scalars()
                .first()
            )
            row.status = ResidualCaseStatus.CLOSED_ORGANICALLY
            session.add(row)

        await _upsert_residual_case_async(project_id, "fp-closed", now_dt, "err again")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-closed",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row.status == ResidualCaseStatus.ACTIVE
        assert row.hit_count == 2

    @pytest.mark.asyncio
    async def test_ignored_status_preserved_on_rehit(
        self, session_factory, monkeypatch
    ):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_classification import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-ignored", now_dt, "err")
        async with session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-ignored",
                        )
                    )
                )
                .scalars()
                .first()
            )
            row.status = ResidualCaseStatus.IGNORED
            session.add(row)

        await _upsert_residual_case_async(project_id, "fp-ignored", now_dt, "err2")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-ignored",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row.status == ResidualCaseStatus.IGNORED
        assert row.hit_count == 2

    @pytest.mark.asyncio
    async def test_repeated_hits_are_not_undercounted(
        self, session_factory, monkeypatch
    ):
        """N sequential record calls must produce hit_count == N (SQL atomic increment).

        The SQL-level UPDATE hit_count = hit_count + 1 (not Python read-modify-write)
        ensures no lost updates. On Postgres, with_for_update() additionally serialises
        concurrent writers; SQLite StaticPool serialises them implicitly.
        """
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_classification import _upsert_residual_case_async
        from app.models import ResidualRemediationCase
        from sqlmodel import select

        N = 8
        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        for _ in range(N):
            await _upsert_residual_case_async(project_id, "fp-repeated", now_dt, "err")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-repeated",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.hit_count == N


# ---------------------------------------------------------------------------
# Cross-language contract / drift tests
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
_AGENT_INDEX = _REPO_ROOT / "agent" / "src" / "index.ts"
_VIEWPORT_TS = _REPO_ROOT / "admin-ui" / "src" / "test" / "viewport.ts"


class TestCrossLanguageClassificationContracts:
    """Contract tests: assert each classification-critical literal still exists
    in its source of truth.

    If one of these tests fails, a string has drifted.  Fix it by updating the
    matching side — either the emitting source (agent/src/index.ts or
    admin-ui/src/test/viewport.ts) or the classifier pattern in
    backend/app/failure_classification.py — so both stay in sync.

    Failures here are at *change time*, before any misclassification occurs in
    production.
    """

    @staticmethod
    def _src(path: Path) -> str:
        if not path.exists():
            pytest.skip(
                f"{path} not available in this environment — the backend test "
                "image ships neither agent/ nor admin-ui/ (build context is "
                "./backend) and CI mounts neither. This cross-language drift "
                "guard runs wherever the full repo tree is checked out (local "
                "dev, agent VM workspace)."
            )
        return path.read_text(encoding="utf-8").lower()

    # ------------------------------------------------------------------
    # Agent heartbeat / status strings
    # ------------------------------------------------------------------

    def test_pr_creation_heartbeat_prefix_in_agent_source(self):
        """failure_classification.py checks 'creating pull request for blueprint/'
        (after lowercasing).  The agent emits this via sendHeartbeatSync() at
        agent/src/index.ts.  If the agent renames the string, tasks reaped
        mid-PR-push will fall through to CODE_ERROR and generate spurious
        residual fingerprints.
        """
        src = self._src(_AGENT_INDEX)
        assert "creating pull request for" in src, (
            "agent/src/index.ts no longer emits 'Creating pull request for …'; "
            "update the 'creating pull request for blueprint/' pattern in "
            "failure_classification._classify_failure_class to match the new string, "
            "or update the agent to keep the same prefix."
        )

    def test_using_tool_heartbeat_in_agent_source(self):
        """failure_classification.py checks 'using tool: ' (after lowercasing).
        The agent sets statusRef.value = `Using tool: ${toolName}` during every
        Claude Code tool call.  If renamed, tasks reaped mid-tool-call will fall
        through to CODE_ERROR instead of AGENT_TIMEOUT.
        """
        src = self._src(_AGENT_INDEX)
        assert "using tool:" in src, (
            "agent/src/index.ts no longer emits 'Using tool: <Name>'; "
            "update the 'using tool: ' pattern in "
            "failure_classification._classify_failure_class to match the new string, "
            "or update the agent to keep the same prefix."
        )

    # ------------------------------------------------------------------
    # Viewport helper function names (thrown in error messages)
    # ------------------------------------------------------------------

    def test_expect_no_overflow_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS checks 'expectnooverflowat(' (lowercase).
        This substring appears in the Error messages thrown by expectNoOverflowAt()
        in admin-ui/src/test/viewport.ts.  If the helper is renamed, viewport
        failures will silently dissolve into CODE_ERROR residuals instead of
        routing to VIEWPORT_REGRESSION.
        """
        src = self._src(_VIEWPORT_TS)
        assert "expectnooverflowat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectNoOverflowAt'; "
            "update _VIEWPORT_PATTERNS in failure_classification.py to match "
            "the new function name, or update viewport.ts to keep the same name."
        )

    def test_expect_no_text_truncation_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS checks 'expectnotexttruncationat(' (lowercase).
        Mirrors expectNoTextTruncationAt() in admin-ui/src/test/viewport.ts.
        """
        src = self._src(_VIEWPORT_TS)
        assert "expectnotexttruncationat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectNoTextTruncationAt'; "
            "update _VIEWPORT_PATTERNS in failure_classification.py to match "
            "the new function name, or update viewport.ts to keep the same name."
        )

    def test_expect_min_touch_target_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS checks 'expectmintouchtargetat(' (lowercase).
        Mirrors expectMinTouchTargetAt() in admin-ui/src/test/viewport.ts.
        """
        src = self._src(_VIEWPORT_TS)
        assert "expectmintouchtargetat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectMinTouchTargetAt'; "
            "update _VIEWPORT_PATTERNS in failure_classification.py to match "
            "the new function name, or update viewport.ts to keep the same name."
        )

    # ------------------------------------------------------------------
    # Viewport bootstrap-failure signatures (cross-layer contract)
    # ------------------------------------------------------------------

    def test_viewport_bootstrap_patterns_in_agent_source(self):
        """_VIEWPORT_INFRA_UNAVAILABLE_PATTERNS includes the same bootstrap
        signatures as agent/src/index.ts VIEWPORT_BOOTSTRAP_PATTERNS.
        If agent renames or removes any of these, the backend classifier would
        fall back to CODE_ERROR for bootstrap failures it can no longer recognize.
        """
        src = self._src(_AGENT_INDEX)
        for expected in (
            "libatk",
            "libgbm",
            "libxkbcommon",
            "libxcomposite",
            "libasound",
            "cannot open shared object file",
            "host system is missing dependencies",
            "executable doesn't exist at",
        ):
            assert expected in src, (
                f"agent/src/index.ts VIEWPORT_BOOTSTRAP_PATTERNS no longer contains {expected!r}; "
                "update _VIEWPORT_INFRA_UNAVAILABLE_PATTERNS in failure_classification.py "
                "to match the new pattern, or update the agent to keep the same string."
            )


class TestAccountStateRateLimitCorrelation:
    """Generic failures during account rate-limit cooldowns classify as RATE_LIMIT."""

    def _make_rl_account(self, account_id: str, in_cooldown: bool) -> dict:
        from datetime import timedelta

        from app.time_utils import _utcnow

        if in_cooldown:
            at = _utcnow() - timedelta(minutes=5)
            return {
                "enabled": True,
                "disabled_models": {
                    "claude-opus-4-5": {
                        "at": at,
                        "hits": 1,
                        "kind": "rate_limit",
                    }
                },
            }
        return {"enabled": True, "disabled_models": {}}

    def test_incomplete_with_rate_limited_account_classifies_rate_limit(
        self, monkeypatch
    ):
        account_id = "acct-rl-test-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == RATE_LIMIT

    def test_incomplete_with_healthy_account_stays_code_error(self, monkeypatch):
        account_id = "acct-healthy-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=False)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_default_fallback_with_rate_limited_account_classifies_rate_limit(
        self, monkeypatch
    ):
        account_id = "acct-rl-test-2"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        # No failure_category in context, no rate-limit keywords — generic failure
        task = _task(
            "failed to read investigation report", ctx={"llm_account_id": account_id}
        )
        assert classify_failure(task) == RATE_LIMIT

    def test_default_fallback_with_no_account_stays_code_error(self, monkeypatch):
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})

        task = _task("failed to read investigation report")
        assert classify_failure(task) == CODE_ERROR

    def test_specific_failure_not_overridden(self, monkeypatch):
        """test_failed / lint_failed with rate-limited account stays CODE_ERROR — specific failures are authoritative."""
        account_id = "acct-rl-test-3"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "test_failed", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_normalize_incomplete_with_rate_limited_account(self, monkeypatch):
        account_id = "acct-rl-norm-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task("", ctx={"llm_account_id": account_id})
        assert normalize_agent_failure_category("incomplete", task) == RATE_LIMIT

    def test_normalize_incomplete_with_healthy_account(self, monkeypatch):
        account_id = "acct-healthy-2"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=False)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task("", ctx={"llm_account_id": account_id})
        assert normalize_agent_failure_category("incomplete", task) == CODE_ERROR


class TestPoolWideRateLimitCorrelation:
    """Pool-wide cooldown correlates generic deaths even when no account was assigned."""

    def test_pre_assignment_death_pool_wide_block(self, monkeypatch):
        """No account assigned, entire pool blocked → RATE_LIMIT (not CODE_ERROR)."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: True
        )
        task = _task("", ctx={"failure_category": "incomplete"})
        assert classify_failure(task) == RATE_LIMIT

    def test_healthy_account_pool_wide_block_still_rate_limit(self, monkeypatch):
        """Assigned account is healthy; pool-wide block drives RATE_LIMIT."""
        account_id = "acct-healthy-pool"
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: {"enabled": True, "disabled_models": {}}},
        )
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: True
        )
        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == RATE_LIMIT

    def test_pool_not_blocked_generic_failure_code_error(self, monkeypatch):
        """Pool not in cooldown → generic incomplete stays CODE_ERROR."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: False
        )
        task = _task("", ctx={"failure_category": "incomplete"})
        assert classify_failure(task) == CODE_ERROR

    def test_specific_label_not_overridden_by_pool_block(self, monkeypatch):
        """Authoritative labels (test_failed) are not overridden by pool-wide block."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: True
        )
        task = _task("", ctx={"failure_category": "test_failed"})
        assert classify_failure(task) == CODE_ERROR

    def test_pool_was_rate_limited_accepts_tz_aware_timestamp(self, monkeypatch):
        """Tz-aware at_time must not raise — helper normalizes to naive UTC before calling pool check."""
        from datetime import datetime, timezone

        from app.failure_classification import _pool_was_rate_limited
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: True
        )
        assert _pool_was_rate_limited(at_time=datetime.now(tz=timezone.utc)) is True


class TestAtTimeFailureInstantCorrelation:
    """Verifies that classify_failure respects at_time so a rate-limit window
    that has already expired by sweep time is still detected at failure time."""

    def _make_rl_account(self, account_id: str, *, in_cooldown: bool):
        """Return a fake pool entry whose cooldown state matches in_cooldown."""
        if in_cooldown:
            from datetime import datetime, timedelta

            return {
                "id": account_id,
                "disabled_models": {
                    "claude-sonnet-4-6": {
                        "at": datetime.utcnow() - timedelta(minutes=2),
                        "kind": "rate_limit",
                        "hits": 1,
                    }
                },
            }
        return {"id": account_id, "disabled_models": {}}

    def test_account_rate_limited_at_failure_time_not_sweep_time(self, monkeypatch):
        """A generic failure classified at the failure instant (at_time=failure_time)
        is RATE_LIMIT; the same failure classified at sweep time (after cooldown
        expires) falls back to CODE_ERROR.

        This locks in the fix that threads at_time through to the account-state
        correlation helpers so a short cooldown that expired before the reaper
        sweep still protects the retry budget.
        """
        from datetime import datetime, timedelta
        import app.max_plan_pool as pool_mod

        account_id = "acct-at-time-test"

        # Account was rate-limited; cooldown active at failure_time, expired by sweep_time.
        def _account_corr(task, at_time=None):
            # Simulate cooldown active only when at_time is provided (failure instant).
            # When at_time is None (sweep time), the cooldown has already cleared.
            return at_time is not None

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: self._make_rl_account(account_id, in_cooldown=True)},
        )
        monkeypatch.setattr(
            "app.failure_classification._account_was_rate_limited",
            _account_corr,
        )
        # Pool-wide check must be disabled so only the account-level at_time
        # correlation drives the result — isolates the variable under test.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None: False
        )

        task = _task(
            "",
            ctx={
                "failure_category": "claude_code_failed",
                "llm_account_id": account_id,
            },
        )

        # With at_time=failure_time (non-None), cooldown is detected → RATE_LIMIT.
        failure_time = datetime.utcnow() - timedelta(minutes=5)
        assert classify_failure(task, at_time=failure_time) == RATE_LIMIT, (
            "failure at failure_time must be RATE_LIMIT (cooldown active then)"
        )

        # With at_time=None (sweep time), cooldown has expired → CODE_ERROR.
        assert classify_failure(task, at_time=None) == CODE_ERROR, (
            "failure re-classified at sweep time must be CODE_ERROR (cooldown expired)"
        )
