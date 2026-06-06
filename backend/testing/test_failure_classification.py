"""Tests for failure_classification subtype helpers and classify_failure()."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    NO_PROGRESS_TIMEOUT,
    PROVISION_FAILURE,
    RATE_LIMIT,
    REASSIGNED,
    BLOCKED_PRECONDITION,
    BRANCH_GONE,
    SCOPE_EMPTY,
    SCOPE_OVERFLOW,
    _external_api_subtype_from_message,
    _timeout_subtype_from_message,
    classify_external_api_subtype,
    classify_failure,
    classify_failure_with_confidence,
    classify_failure_taxonomy,
    classify_timeout_subtype,
    normalize_agent_failure_category,
    persist_failure_category,
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


class TestStuckLlmCallHelper:
    """Unit tests for the 0-output-token stuck-call detector used by both the
    classifier (agent-reported timeouts) and the reaper (agent_stalled reaps)."""

    def _t(self, summary):
        t = _task("")
        t.api_call_summary = summary
        return t

    def test_call_started_zero_output_is_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert _stuck_llm_call_no_progress(
            self._t({"llm_call_count": 1, "total_output_tokens": 0})
        ) is True

    def test_output_present_is_not_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert _stuck_llm_call_no_progress(
            self._t({"llm_call_count": 2, "total_output_tokens": 800})
        ) is False

    def test_no_call_started_is_not_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert _stuck_llm_call_no_progress(
            self._t({"llm_call_count": 0, "total_output_tokens": 0})
        ) is False

    def test_missing_or_nondict_summary_is_not_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert _stuck_llm_call_no_progress(self._t(None)) is False

    def test_unreadable_summary_does_not_raise(self):
        # Regression for #6864: api_call_summary is deferred/expired on analytics
        # call sites (digest_activity / admin activity), so reading it raises
        # MissingGreenlet under async. getattr's default does NOT catch that
        # (not an AttributeError) — it must be guarded so classify_failure stays
        # safe on partially-loaded entities. The helper declines (returns False),
        # it does NOT propagate the error.
        from sqlalchemy.exc import MissingGreenlet

        from app.failure_classification import _stuck_llm_call_no_progress

        class _DeferredTask:
            @property
            def api_call_summary(self):
                raise MissingGreenlet("greenlet_spawn has not been called")

        assert _stuck_llm_call_no_progress(_DeferredTask()) is False


class TestStuckLlmCallReclassifiedToRateLimit:
    """An execution/time-limit-stall AGENT_TIMEOUT where the agent started an LLM
    call but produced 0 output tokens is a silent provider hang (429/529 the
    Claude Code SDK absorbed via internal retry), not a code/agent defect —
    classify as RATE_LIMIT so it holds/reroutes instead of burning the retry
    budget and terminal-failing a healthy task. Incident 2026-05-29."""

    def _stalled(self, msg: str, summary, ctx: dict | None = None) -> Task:
        t = _task(msg, ctx)
        t.api_call_summary = summary
        return t

    def test_fatal_45min_timeout_zero_output_reclassifies(self):
        # Agent self-reported 45-min ClaudeTimeoutError → "fatal error" → AGENT_TIMEOUT.
        t = self._stalled(
            "Fatal error: Claude Code exceeded 45-minute time limit",
            {"llm_call_count": 1, "total_input_tokens": 0, "total_output_tokens": 0},
        )
        assert classify_failure(t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "stuck_llm_call_no_output"
        assert t.context.get("rate_limit_kind") == "unknown"

    def test_tool_use_stall_zero_output_reclassifies(self):
        # Agent wraps the mid-stream stall as a fatal ClaudeTimeoutError.
        t = self._stalled(
            "Fatal error: mid-stream tool-use stall: tool in-flight for >30 min without result",
            {"llm_call_count": 1, "total_input_tokens": 0, "total_output_tokens": 0},
        )
        assert classify_failure(t) == RATE_LIMIT

    def test_output_tokens_present_stays_agent_timeout(self):
        # Real work happened (output produced) before the stall → genuine timeout.
        t = self._stalled(
            "Fatal error: Claude Code exceeded 45-minute time limit",
            {"llm_call_count": 3, "total_input_tokens": 5000, "total_output_tokens": 1200},
        )
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_no_llm_call_started_stays_agent_timeout(self):
        # Pre-LLM death (provision/startup) — not a provider hang.
        t = self._stalled(
            "Fatal error: Claude Code exceeded 45-minute time limit",
            {"llm_call_count": 0, "total_input_tokens": 0, "total_output_tokens": 0},
        )
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_cancellation_with_zero_output_not_reclassified(self):
        # A deliberate orchestration cancellation also has 0 output tokens but is
        # NOT a provider hang — it lacks an execution-stall signature, so it must
        # stay AGENT_TIMEOUT (proves the reclassification is narrowly scoped).
        t = self._stalled(
            "Cancelled: dependency task failed",
            {"llm_call_count": 1, "total_input_tokens": 0, "total_output_tokens": 0},
        )
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_missing_summary_stays_agent_timeout(self):
        t = _task("Fatal error: Claude Code exceeded 45-minute time limit")
        t.api_call_summary = None
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_code_error_not_reclassified_even_with_zero_output(self):
        # Reclassification is gated on AGENT_TIMEOUT — a real code error must stay
        # CODE_ERROR even with a 0-output-token summary + a time-limit substring.
        # (The CLI-death CODE_ERROR rule below doesn't fire either: this task has
        # no generic agent failure_category.)
        t = self._stalled(
            "AssertionError: expected 1 got 2",
            {"llm_call_count": 1, "total_input_tokens": 100, "total_output_tokens": 0},
        )
        assert classify_failure(t) == CODE_ERROR


class TestCliDeathReclassifiedToRateLimit:
    """A CODE_ERROR whose agent label is generic (claude_code_failed /
    incomplete / fatal / …) and whose CLI output tail shows the process died
    without the model ever producing output (init-banner-only tail) or died
    mid-stream (content deltas with no terminal result event) is an
    infra/provider death — usage exhaustion, auth rejection, or a severed
    stream — not a code defect. It must classify RATE_LIMIT (canonical
    cooldown + account blacklist + budget-free requeue) instead of burning
    CODE_ERROR retries. Incident 2026-06-06: this signature burned full
    3-attempt budgets across multiple lineages overnight."""

    _INIT_ONLY_TAIL = (
        "Claude subprocess exited with code 1 without shipping a PR.\n\n"
        "--- Claude output tail ---\n"
        '{"type":"system","subtype":"init","cwd":"/tmp/workspace","session_id":"abc123"}'
    )
    _STREAM_CUT_TAIL = (
        "Claude exited without calling ./done, ./fail, or opening a PR.\n\n"
        "--- Claude output tail ---\n"
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"signature_delta","signature":"d7nCuJvWDPvT/UFq"}}}'
    )

    def _dead(self, msg: str, category: str = "claude_code_failed", summary=None) -> Task:
        t = _task(msg, {"failure_category": category})
        t.api_call_summary = summary
        return t

    def test_init_banner_only_tail_reclassifies(self):
        t = self._dead(self._INIT_ONLY_TAIL)
        assert classify_failure(t) == RATE_LIMIT
        assert (
            t.context.get("rate_limit_inferred_from")
            == "cli_startup_death_init_only_tail"
        )
        assert t.context.get("rate_limit_kind") == "unknown"

    def test_stream_cut_tail_reclassifies(self):
        t = self._dead(self._STREAM_CUT_TAIL, category="incomplete")
        assert classify_failure(t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "cli_stream_cut_tail"

    def test_zero_output_telemetry_reclassifies_without_tail(self):
        # Telemetry alone (no output tail captured) is sufficient: an LLM call
        # started but 0 output tokens ever arrived.
        t = self._dead(
            "Claude subprocess exited with code 1 without shipping a PR.",
            summary={"llm_call_count": 1, "total_output_tokens": 0},
        )
        assert classify_failure(t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "stuck_llm_call_no_output"

    def test_real_output_after_init_stays_code_error(self):
        # The model produced assistant output and the run reached a terminal
        # result event — a genuine agent failure, not a startup death.
        msg = (
            "Claude subprocess exited with code 1 without shipping a PR.\n\n"
            "--- Claude output tail ---\n"
            '{"type":"system","subtype":"init","cwd":"/tmp/workspace"}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\n'
            '{"type":"result","subtype":"error_during_execution"}'
        )
        t = self._dead(msg)
        assert classify_failure(t) == CODE_ERROR

    def test_stream_with_terminal_result_stays_code_error(self):
        # Deltas in the tail are fine when the stream completed (result event
        # present) — the cut detector must not sweep normal runs.
        msg = (
            "Claude exited without calling ./done, ./fail, or opening a PR.\n\n"
            "--- Claude output tail ---\n"
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{}}}\n'
            '{"type":"result","subtype":"success"}'
        )
        t = self._dead(msg, category="incomplete")
        assert classify_failure(t) == CODE_ERROR

    def test_specific_category_never_swept(self):
        # A specific (non-generic) agent label is a real failure signal — even
        # an init-only tail must not reclassify it.
        t = self._dead(self._INIT_ONLY_TAIL, category="test_failed")
        assert classify_failure(t) == CODE_ERROR

    def test_no_tail_no_telemetry_stays_code_error(self):
        t = self._dead("Claude subprocess exited with code 1 without shipping a PR.")
        assert classify_failure(t) == CODE_ERROR


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

    def test_reassigned_routes_to_reassigned_class(self):
        """Agent self-terminates gracefully on a non-transient heartbeat 4xx
        (reassignment/deauth) or cancel/supersede during startup and posts
        failure_category='reassigned' (cluster C7). It must canonicalize to the
        REASSIGNED class — NOT CODE_ERROR — so an intentional handoff is not
        treated as a code fault."""
        task = _task("Task reassigned/deauthorized — agent self-terminating")
        assert normalize_agent_failure_category("reassigned", task) == REASSIGNED

    def test_reassigned_does_not_burn_retry_budget_or_penalize_health(self):
        """REASSIGNED must be in INFRA_CLASSES so the retry-budget reducer and
        direction-health analytics treat it as infra (no budget burn, no health
        penalty), matching the other graceful-exit classes (SCOPE_EMPTY etc.)."""
        from app.failure_classification import INFRA_CLASSES

        assert REASSIGNED in INFRA_CLASSES
        assert REASSIGNED != CODE_ERROR

    def test_reassigned_in_all_failure_classes(self):
        task = _task("Task reassigned/deauthorized")
        assert normalize_agent_failure_category("reassigned", task) == REASSIGNED
        assert REASSIGNED in ALL_FAILURE_CLASSES

    def test_reassigned_via_context_fast_path(self):
        """classify_failure's context['failure_category'] fast path also honors
        the lowercase 'reassigned' label (the agent writes it into context)."""
        task = _task(
            "Task reassigned/deauthorized — agent self-terminating",
            {"failure_category": "reassigned"},
        )
        assert classify_failure(task) == REASSIGNED


class TestNoProgressTimeout:
    """The agent's no-progress watchdog fired on a genuinely silent call but NO
    confirmed rate-limit message matched. Reported as failure_category
    'no_progress_timeout' → NO_PROGRESS_TIMEOUT class: transient infra, requeue
    without burning retry, and (critically) WITHOUT blocking the account. Only a
    CONFIRMED rate-limit message ('rate_limited') routes to RATE_LIMIT + block."""

    def test_normalizes_to_no_progress_timeout_class(self):
        task = _task(
            "Claude Code produced no model output for >15 min — transient provider hang"
        )
        assert (
            normalize_agent_failure_category("no_progress_timeout", task)
            == NO_PROGRESS_TIMEOUT
        )

    def test_context_fast_path_honors_label(self):
        task = _task(
            "Claude Code produced no model output (no stream deltas)",
            {"failure_category": "no_progress_timeout"},
        )
        assert classify_failure(task) == NO_PROGRESS_TIMEOUT

    def test_is_infra_class_no_budget_burn_no_health_penalty(self):
        """NO_PROGRESS_TIMEOUT must be in INFRA_CLASSES (it is also imported there
        as the direction-health infra set) so an inferred transient hang neither
        burns the retry budget nor penalizes direction health — and it is NOT a
        code defect."""
        from app.failure_classification import INFRA_CLASSES

        assert NO_PROGRESS_TIMEOUT in INFRA_CLASSES
        assert NO_PROGRESS_TIMEOUT != CODE_ERROR
        # Distinct from RATE_LIMIT: the requeue path's account-block branches are
        # gated on RATE_LIMIT, so a NO_PROGRESS_TIMEOUT requeue leaves the account
        # available. The two must never collapse to the same class.
        assert NO_PROGRESS_TIMEOUT != RATE_LIMIT

    def test_in_all_failure_classes(self):
        assert NO_PROGRESS_TIMEOUT in ALL_FAILURE_CLASSES

    def test_confidence_is_high_for_structured_label(self):
        task = _task("anything", ctx={"failure_category": "no_progress_timeout"})
        result = classify_failure_with_confidence(task)
        assert result.failure_class == NO_PROGRESS_TIMEOUT
        assert result.confidence == 0.9


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
            "Claude API Error: 401 Invalid authentication credentials",
            '{"error_status":401,"error":"authentication_failed"}',
            "Failed to authenticate. API Error: 401 Invalid authentication credentials",
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


class TestClassifyFailureBenignSubstringNotMisclassified:
    """Anchored "ssl"/"401" keywords: a benign log line that merely *contains*
    the substring "ssl" or the digits "401" (and no genuine network/auth error
    phrase) must NOT be misrouted to EXTERNAL_API. Before the keywords were
    anchored ("ssl handshake failed" / "http 401"), these fell through to
    EXTERNAL_API and the false-positive rate grew with log volume.
    """

    @pytest.mark.parametrize(
        "msg",
        [
            # "ssl" appears only as an incidental mention, not a handshake error.
            "AssertionError: reused cached ssl context but expected a fresh one",
            "DEBUG openssl module loaded; verifying signature",
            # "401" appears only as a count / identifier digit run.
            "AssertionError: compiled 401 modules but expected 402",
            "ValueError: record 401 missing required field",
        ],
    )
    def test_benign_ssl_or_401_substring_not_external_api(self, msg):
        result = classify_failure(_task(msg))
        assert result != EXTERNAL_API, (
            f"benign substring line was misrouted to EXTERNAL_API: {msg!r}"
        )
        # These are structurally error-shaped (AssertionError/ValueError) with no
        # infra keyword, so they fall through to the CODE_ERROR default.
        assert result == CODE_ERROR

    def test_genuine_ssl_handshake_still_external_api(self):
        # The anchored phrase must still match a real TLS handshake failure.
        assert (
            classify_failure(_task("SSL handshake failed to api.anthropic.com"))
            == EXTERNAL_API
        )

    def test_genuine_http_401_still_external_api(self):
        # The anchored "http 401" form (without the word "unauthorized") still
        # routes a genuine auth failure to EXTERNAL_API.
        assert classify_failure(_task("Request failed: HTTP 401")) == EXTERNAL_API


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
            "some other agent log...\n"
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}\n'
        )
        task = _task(
            "Memo handler exited 1 with no JSON object",
            ctx={"failure_category": "agent_self_failed"},
        )
        assert classify_failure(task, logs=logs) == RATE_LIMIT

    def test_codex_cause_c_silent_mid_turn_death_preempts_to_external_api(self):
        """Regression (2026-05-26, cause C): the Codex CLI (gpt-5.4-mini)
        emits ``{"type":"thread.started"}`` + ``{"type":"turn.started"}``,
        then exits CLEAN (exitCode=0) before reaching ``turn.completed``.
        Upstream OpenAI-side disconnect (token cap / silent refusal / 5xx
        the CLI swallows as success). The memo handler reclassifies
        agent-side now (see agent/src/handlers/memo.ts), but a backend
        belt-and-suspenders check mirrors the override so any OTHER Codex
        consumer that forgets the agent-side fix still routes through
        EXTERNAL_API (proper backoff) instead of CODE_ERROR (burns retries).

        Bounded by len < 8KB so real CLI deaths (init banner only) trip the
        override while normal-sized output with these substrings does not.
        """
        # Synthetic agent output: thread.started + turn.started, NO
        # turn.completed, exitCode=0. Pre-fix this would fall through to
        # the agent_category fast-path with `failure_category=parse_failed`
        # → CODE_ERROR → 3 retries → terminal FAIL on a transient issue.
        all_output = (
            '{"type":"thread.started","thread_id":"t-abc"}\n{"type":"turn.started"}\n'
        )
        msg = (
            "Memo handler could not extract a JSON object with all required "
            "keys (summary, reasoning, citations, tags). exitCode=0, "
            f"allOutput={len(all_output)}b, lastResultText=0b\n"
            "--- output tail ---\n"
            f"{all_output}"
        )
        assert len(msg) < 8192, "test fixture must stay under the 8KB bound"
        # Even a generic parse_failed agent_category in context must NOT
        # win the fast-path here — the preempt fires first.
        task = _task(msg, ctx={"failure_category": "parse_failed"})
        assert classify_failure(task) == EXTERNAL_API, (
            "Codex thread.started + no turn.completed + exitCode=0 + "
            "<8KB output is the cause-C silent-mid-turn-death pattern; "
            "must route to EXTERNAL_API so the rate-limit/backoff path "
            "runs instead of burning code-error retries."
        )

    def test_codex_cause_c_does_not_fire_when_turn_completed_present(self):
        """Control: a stream with BOTH thread.started AND turn.completed is
        a normal Codex run — the preempt must not steal it from the
        downstream classification."""
        all_output = (
            '{"type":"thread.started"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100}}\n'
        )
        msg = (
            "Memo handler could not extract a JSON object. exitCode=0, "
            f"allOutput={len(all_output)}b\n"
            "--- output tail ---\n"
            f"{all_output}"
        )
        task = _task(msg, ctx={"failure_category": "parse_failed"})
        # parse_failed → CODE_ERROR via the agent_category fast-path, NOT
        # EXTERNAL_API — the agent really did emit a completed turn that
        # downstream parsing rejected.
        assert classify_failure(task) == CODE_ERROR

    def test_codex_cause_c_does_not_fire_when_exit_code_nonzero(self):
        """Control: exitCode != 0 is the cause-A path (Codex/Claude crash),
        which `claude_code_failed` + the existing override already cover.
        The new preempt requires exitCode=0 to stay narrowly targeted."""
        all_output = '{"type":"thread.started"}\n{"type":"turn.started"}\n'
        msg = (
            "Memo handler could not extract a JSON object. exitCode=1, "
            f"allOutput={len(all_output)}b\n"
            "--- output tail ---\n"
            f"{all_output}"
        )
        task = _task(msg, ctx={"failure_category": "claude_code_failed"})
        # claude_code_failed → CODE_ERROR via the existing fast-path; the
        # cause-C preempt requires exitCode=0 and must not fire here. Asserting
        # the exact class (not merely != EXTERNAL_API) closes a silent-misroute
        # hole: a future bootstrap/provision pattern could send this
        # claude_code_failed + exitCode=1 case to PROVISION_FAILURE/AGENT_TIMEOUT
        # undetected if we only checked it stayed out of EXTERNAL_API.
        assert classify_failure(task) == CODE_ERROR

    def test_codex_cause_c_does_not_fire_on_large_outputs(self):
        """Control: outputs >= 8KB mean real work was produced — the
        thread.started/turn.completed delta is unlikely to be a silent
        provider death and is more likely a genuine parse failure of a
        large response. The preempt's 8KB bound keeps it conservative."""
        # Pad to >= 8KB to bypass the bound.
        padding = "x" * 9000
        all_output = (
            f'{{"type":"thread.started"}}\n{{"type":"turn.started"}}\n{padding}\n'
        )
        msg = f"exitCode=0\n{all_output}"
        assert len(msg) >= 8192
        task = _task(msg, ctx={"failure_category": "parse_failed"})
        # Falls through to the normal parse_failed fast-path → CODE_ERROR.
        assert classify_failure(task) == CODE_ERROR

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


class TestClassifyFailureOrchestratorDefer:
    """The orchestrator defers a task on a precondition it cannot satisfy yet
    (a file is being edited by another in-flight task, or the task overlaps a
    pending rollup) by stamping a "Waiting: ..." status message and leaving the
    task QUEUED. When such a task later reaches a terminal classification path,
    its defer marker must route to BLOCKED_PRECONDITION (budget-exempt) just
    like an agent-emitted "BLOCKER:" — not to a generic CODE_ERROR that burns a
    retry slot and dings direction health."""

    def test_file_conflict_defer_marker(self):
        t = _task("Waiting: file conflict on backend/app/x.py (blocked by: Refactor X)")
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_rollup_overlap_defer_marker(self):
        t = _task("Waiting: overlaps pending rollup 'Refactor X'")
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_defer_marker_overrides_generic_agent_category(self):
        """The marker is authoritative even when a generic CODE_ERROR category
        was written alongside it — mirrors the "BLOCKER:" prefix behavior."""
        t = _task(
            "Waiting: file conflict on backend/app/x.py",
            {"failure_category": "CODE_ERROR"},
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_high_confidence_for_defer_marker(self):
        t = _task("Waiting: overlaps pending rollup 'Refactor X'")
        result = classify_failure_with_confidence(t)
        assert result.failure_class == BLOCKED_PRECONDITION
        assert result.confidence >= 0.9

    def test_serializing_migration_stays_agent_timeout(self):
        """The migration-serialization wait is a deliberate ordering wait, not a
        precondition blocker — it must keep its AGENT_TIMEOUT route. This is the
        primary regression guard for the narrow defer-marker substrings."""
        t = _task(
            "Waiting: serializing migration — task 7af4d393 is already producing "
            "a migration in this project"
        )
        assert classify_failure(t) == AGENT_TIMEOUT

    def test_bare_file_conflict_mention_does_not_match(self):
        """A passing mention of "file conflict" without the "Waiting:" defer
        prefix must NOT over-match — it stays in its normal class."""
        t = _task("AssertionError: file conflict detected during merge")
        assert classify_failure(t) == CODE_ERROR


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

    def test_rate_limit_skipped(self):
        # Weekly quota exhaustion (RATE_LIMIT / INFRA_CLASS) must not spawn
        # an investigation — the failure is operational, not a code defect,
        # and a self-rate-limited investigation cascade would waste quota.
        t = _task(
            "You've hit your weekly limit · resets 9pm (UTC)",
            {"direction_id": "d-1", "failure_category": "rate_limited"},
        )
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

    def test_benign_ssl_substring_not_network_transient(self):
        # Anchored "ssl handshake failed" — an incidental "ssl" mention must not
        # be routed to the network_transient subtype.
        assert (
            _external_api_subtype_from_message("reused cached ssl context")
            == EXTERNAL_API_SUBTYPE_OTHER
        )

    def test_benign_401_substring_not_provider_auth(self):
        # Anchored "http 401" — an incidental "401" digit run (count/id) must not
        # be routed to the provider_auth subtype.
        assert (
            _external_api_subtype_from_message("compiled 401 modules ok")
            == EXTERNAL_API_SUBTYPE_OTHER
        )


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


# ---------------------------------------------------------------------------
# Cross-language contract / drift tests
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
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

    @staticmethod
    def _agent_src_contains(needle: str) -> bool:
        """True if ``needle`` (case-insensitive) appears anywhere in the agent's
        non-test TypeScript sources.

        Scans the whole ``agent/src`` tree rather than a single file: heartbeat
        status strings legitimately move between modules during refactors (e.g.
        ``Using tool:`` migrated index.ts → providers/runners.ts in the 2026-05
        agent decomposition). A single-file pin breaks the moment that happens,
        producing a false drift alarm even though the contract still holds.
        """
        agent_src = _REPO_ROOT / "agent" / "src"
        if not agent_src.is_dir():
            pytest.skip(
                f"{agent_src} not available in this environment — the backend "
                "test image ships no agent/ tree (build context is ./backend). "
                "This cross-language drift guard runs wherever the full repo "
                "tree is checked out (local dev, agent VM workspace)."
            )
        needle = needle.lower()
        for path in agent_src.rglob("*.ts"):
            if path.name.endswith(".test.ts"):
                continue
            if needle in path.read_text(encoding="utf-8").lower():
                return True
        return False

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
        assert self._agent_src_contains("creating pull request for"), (
            "agent/src no longer emits 'Creating pull request for …'; "
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
        assert self._agent_src_contains("using tool:"), (
            "agent/src no longer emits 'Using tool: <Name>'; "
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

    # The viewport bootstrap cross-layer contract test was removed when the
    # agent-side VIEWPORT_BOOTSTRAP_PATTERNS array was dropped from
    # agent/src/index.ts (commit 96590c9 — dead lintFixLoop / Playwright
    # bootstrap scaffolding cleanup). The backend's
    # _VIEWPORT_INFRA_UNAVAILABLE_PATTERNS is now standalone (no agent-side
    # mirror), so there is no cross-language contract to enforce.


class TestBranchGoneClassification:
    """A `git clone --branch <gone>` failure (branch deleted, usually because its
    PR already merged) is an orchestration race, not a code defect — it must
    classify as BRANCH_GONE, override the agent's generic 'fatal' CODE_ERROR,
    stay budget-exempt, and not trigger a code-defect investigation."""

    # The exact production status message (review fbd497dd of PR #7547).
    _PROD_MSG = (
        "[CODE_ERROR] Retries exhausted (3/3). Fatal error: Command failed: "
        "git clone --depth 50 --branch plan/763859dc "
        "https://x-access-token:***@github.com/HenryHamster/BlueprintAIStudio.git "
        "/tmp/workspace\nCloning into '/tmp/workspace'...\nwarning: Could not "
        "find remote branch plan/763859dc to clone.\nfatal: Remote branch "
        "plan/763859dc not found"
    )

    def test_production_clone_warning_classifies_branch_gone(self):
        assert classify_failure(_task(self._PROD_MSG)) == BRANCH_GONE

    def test_overrides_agent_posted_code_error(self):
        # The agent labels the failed clone CODE_ERROR; branch-gone must win
        # because the check runs before the agent_category fast-path.
        t = _task(self._PROD_MSG, ctx={"failure_category": "CODE_ERROR"})
        assert classify_failure(t) == BRANCH_GONE

    def test_overrides_persisted_code_error_verdict(self):
        t = _task(self._PROD_MSG, ctx={"failure_class": CODE_ERROR})
        assert classify_failure(t) == BRANCH_GONE

    def test_fetch_ref_form_classifies_branch_gone(self):
        assert (
            classify_failure(_task("fatal: couldn't find remote ref plan/abc123"))
            == BRANCH_GONE
        )

    def test_not_found_in_upstream_form_classifies_branch_gone(self):
        msg = "fatal: Remote branch feature/x not found in upstream origin"
        assert classify_failure(_task(msg)) == BRANCH_GONE

    def test_generic_not_found_code_error_does_not_overmatch(self):
        # A real code error that merely says "not found" must stay CODE_ERROR.
        t = _task(
            "AssertionError: expected config file not found in /tmp/workspace",
            ctx={"failure_category": "CODE_ERROR"},
        )
        assert classify_failure(t) == CODE_ERROR

    def test_branch_gone_is_budget_exempt_infra_class(self):
        from app.failure_classification import INFRA_CLASSES

        assert BRANCH_GONE in INFRA_CLASSES
        assert BRANCH_GONE in ALL_FAILURE_CLASSES

    def test_branch_gone_does_not_trigger_code_defect_investigation(self):
        # No debug/remediation task should spawn for a branch-gone race.
        t = _task(self._PROD_MSG, ctx={"failure_category": "CODE_ERROR"})
        assert should_trigger_code_defect_investigation(t) is False
