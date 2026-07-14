"""Tests for failure_classification subtype helpers and classify_failure()."""

import json
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.failure_classification import (
    ADMIN_CANCEL,
    AGENT_ERROR_REPORT_CONTEXT_KEY,
    AGENT_TIMEOUT,
    ALL_FAILURE_CLASSES,
    APPROVED_CAP_PR_CLOSED,
    CO_DEFERRED_SHELVED,
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
    sanitize_agent_error_report,
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


CORPUS_PATH = (
    Path(__file__).resolve().parent
    / "__fixtures__"
    / "credential_redaction_corpus.json"
)


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


class TestApprovedCapPrClosedFailureClass:
    def test_registered_budget_exempt_and_distinct_from_code_error(self):
        from app.failure_classification import INFRA_CLASSES

        assert APPROVED_CAP_PR_CLOSED == "APPROVED_CAP_PR_CLOSED"
        assert APPROVED_CAP_PR_CLOSED in ALL_FAILURE_CLASSES
        assert APPROVED_CAP_PR_CLOSED in INFRA_CLASSES
        assert APPROVED_CAP_PR_CLOSED != CODE_ERROR

    def test_classify_persisted_approved_cap_category(self):
        task = _task(ctx={"failure_category": APPROVED_CAP_PR_CLOSED})

        assert classify_failure(task) == APPROVED_CAP_PR_CLOSED
        assert classify_failure_taxonomy(task) == TAXONOMY_INFRA_RELATED


def _effort_timeout_task(subtype: str, *, effort: str = "medium") -> Task:
    """Build a task that classify_failure() resolves to AGENT_TIMEOUT with the
    given timeout subtype and a context-level effort tier.

    Uses the persisted-verdict short-circuit (``failure_class``) so the
    classifier returns AGENT_TIMEOUT deterministically without depending on
    status-message regex, and pins ``timeout_subtype`` so the subtype gate is
    explicit. ``effort`` lands in context where Layer-2 routing reads it.
    """
    t = MagicMock(spec=Task)
    t.latest_status_message = ""
    t.id = "task-timeout"
    t.context = {
        "failure_class": AGENT_TIMEOUT,
        "timeout_subtype": subtype,
        "effort": effort,
    }
    t.first_payload = {"type": "create"}
    return t


class TestIsPureExecutionTimeout:
    def test_execution_timeout_is_pure(self):
        from app.failure_classification import is_pure_execution_timeout

        assert (
            is_pure_execution_timeout(_effort_timeout_task(TIMEOUT_EXECUTION)) is True
        )

    @pytest.mark.parametrize(
        "subtype",
        [
            TIMEOUT_OOM,
            TIMEOUT_PROCESS_CRASH,
            TIMEOUT_HEARTBEAT_LOST,
            TIMEOUT_CASCADE,
            TIMEOUT_ITERATION_LIMIT,
            TIMEOUT_UNKNOWN,
        ],
    )
    def test_other_timeout_subtypes_are_not_pure(self, subtype):
        from app.failure_classification import is_pure_execution_timeout

        assert is_pure_execution_timeout(_effort_timeout_task(subtype)) is False

    def test_non_timeout_failure_is_not_pure(self):
        from app.failure_classification import is_pure_execution_timeout

        t = MagicMock(spec=Task)
        t.latest_status_message = "AssertionError: boom"
        t.context = {"failure_class": CODE_ERROR}
        t.first_payload = {"type": "create"}
        assert is_pure_execution_timeout(t) is False


class TestEscalatedEffortForExecutionTimeout:
    def test_execution_timeout_escalates_one_tier(self):
        from app.failure_classification import (
            escalated_effort_for_execution_timeout,
        )

        task = _effort_timeout_task(TIMEOUT_EXECUTION, effort="medium")
        assert escalated_effort_for_execution_timeout(task) == "high"

    def test_execution_timeout_at_max_does_not_escalate(self):
        from app.failure_classification import (
            escalated_effort_for_execution_timeout,
        )

        task = _effort_timeout_task(TIMEOUT_EXECUTION, effort="max")
        assert escalated_effort_for_execution_timeout(task) is None

    @pytest.mark.parametrize(
        "subtype",
        [TIMEOUT_OOM, TIMEOUT_PROCESS_CRASH, TIMEOUT_HEARTBEAT_LOST],
    )
    def test_resource_timeouts_do_not_escalate(self, subtype):
        from app.failure_classification import (
            escalated_effort_for_execution_timeout,
        )

        task = _effort_timeout_task(subtype, effort="medium")
        assert escalated_effort_for_execution_timeout(task) is None

    def test_non_timeout_does_not_escalate(self):
        from app.failure_classification import (
            escalated_effort_for_execution_timeout,
        )

        t = MagicMock(spec=Task)
        t.latest_status_message = ""
        t.context = {"failure_class": RATE_LIMIT, "effort": "medium"}
        t.first_payload = {"type": "create"}
        assert escalated_effort_for_execution_timeout(t) is None

    def test_kill_switch_off_disables_escalation(self, monkeypatch):
        import app.runtime_config_state as rcs
        from app.failure_classification import (
            escalated_effort_for_execution_timeout,
        )

        monkeypatch.setitem(
            rcs.runtime_config,
            "effort_escalation_on_execution_timeout_enabled",
            False,
        )
        task = _effort_timeout_task(TIMEOUT_EXECUTION, effort="medium")
        assert escalated_effort_for_execution_timeout(task) is None


class TestStuckLlmCallHelper:
    """Unit tests for the 0-output-token stuck-call detector used by both the
    classifier (agent-reported timeouts) and the reaper (agent_stalled reaps)."""

    def _t(self, summary):
        t = _task("")
        t.api_call_summary = summary
        return t

    def test_call_started_zero_output_is_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert (
            _stuck_llm_call_no_progress(
                self._t({"llm_call_count": 1, "total_output_tokens": 0})
            )
            is True
        )

    def test_output_present_is_not_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert (
            _stuck_llm_call_no_progress(
                self._t({"llm_call_count": 2, "total_output_tokens": 800})
            )
            is False
        )

    def test_no_call_started_is_not_stuck(self):
        from app.failure_classification import _stuck_llm_call_no_progress

        assert (
            _stuck_llm_call_no_progress(
                self._t({"llm_call_count": 0, "total_output_tokens": 0})
            )
            is False
        )

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
            {
                "llm_call_count": 3,
                "total_input_tokens": 5000,
                "total_output_tokens": 1200,
            },
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
        # NOT a provider hang — it lacks an execution-stall signature and routes to
        # ADMIN_CANCEL, not RATE_LIMIT (proves the reclassification is narrowly scoped).
        t = self._stalled(
            "Cancelled: dependency task failed",
            {"llm_call_count": 1, "total_input_tokens": 0, "total_output_tokens": 0},
        )
        assert classify_failure(t) == ADMIN_CANCEL

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

    def _dead(
        self, msg: str, category: str = "claude_code_failed", summary=None
    ) -> Task:
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


class TestAgentErrorReportClassification:
    """The structured agent error report (shipped by the agent runner with every
    FAILED status) is the PRIMARY classification signal: a report proving the
    provider CLI died without completing a model run routes to RATE_LIMIT
    (account rotation + cooldown, no retry burn) — never CODE_ERROR — and a
    report showing a completed run suppresses the output-text tail heuristics
    (which remain only as the fallback for old agents without a report).
    Incident 2026-06-06: a create task died 5s after CLI start (exit 1,
    init-banner-only tail — a provider error on the first API call), was
    classified CODE_ERROR, and burned all 3 retries in seconds."""

    @staticmethod
    def _report(**overrides) -> dict:
        report = {
            "schema": 1,
            "provider": "anthropic",
            "auth_mode": "oauth",
            "exit_code": 1,
            "exit_signal": None,
            "duration_ms": 5000,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "",
            "last_stream_event": "system",
        }
        report.update(overrides)
        return report

    def _failed(
        self, report: dict | None, category: str = "claude_code_failed"
    ) -> Task:
        ctx: dict = {"failure_category": category}
        if report is not None:
            ctx[AGENT_ERROR_REPORT_CONTEXT_KEY] = report
        t = _task("Claude subprocess exited with code 1 without shipping a PR.", ctx)
        t.api_call_summary = None
        return t

    def test_startup_death_report_classifies_rate_limit(self):
        # The incident signature: instant exit, model never produced output.
        t = self._failed(self._report())
        assert classify_failure(t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "error_report_startup_death"
        assert t.context.get("rate_limit_kind") == "unknown"

    def test_startup_death_report_via_normalize(self):
        # The status-write boundary path (normalize_agent_failure_category) —
        # the exact route the agent's FAILED POST takes.
        t = self._failed(self._report())
        assert normalize_agent_failure_category("claude_code_failed", t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "error_report_startup_death"

    def test_provider_error_event_report_classifies_rate_limit(self):
        t = self._failed(
            self._report(
                model_output_seen=True,
                provider_errors=[
                    {
                        "source": "claude-stream",
                        "code": "overloaded_error",
                        "message": "Overloaded",
                    }
                ],
            )
        )
        assert classify_failure(t) == RATE_LIMIT
        assert (
            t.context.get("rate_limit_inferred_from") == "error_report_provider_error"
        )

    def test_stream_cut_report_classifies_rate_limit(self):
        # Model produced output but the run never reached its terminal result
        # event and exited non-zero — the stream was severed mid-write.
        t = self._failed(self._report(model_output_seen=True))
        assert classify_failure(t) == RATE_LIMIT
        assert t.context.get("rate_limit_inferred_from") == "error_report_stream_cut"

    def test_completed_run_report_stays_code_error(self):
        # Genuine code failure: the model ran, produced output, and reached its
        # terminal result event — the non-zero exit belongs to the work. Must
        # stay CODE_ERROR and burn the retry slot.
        t = self._failed(self._report(model_output_seen=True, result_event_seen=True))
        assert classify_failure(t) == CODE_ERROR
        assert "rate_limit_inferred_from" not in t.context

    def test_clean_exit_report_stays_code_error(self):
        # exit 0 (e.g. `incomplete` — agent never shipped) is never a provider
        # death regardless of the other fields.
        t = self._failed(self._report(exit_code=0), category="incomplete")
        assert classify_failure(t) == CODE_ERROR

    def test_specific_label_never_overridden_by_death_report(self):
        # test_failed is an authoritative handler verdict about the work; even
        # a death-shaped report must not sweep it.
        t = self._failed(self._report(), category="test_failed")
        assert classify_failure(t) == CODE_ERROR

    def test_completed_run_report_suppresses_init_banner_tail_heuristic(self):
        # Report present and conclusive-clean → the text-tail fallback must NOT
        # run, even when the logs happen to contain an init-banner-only tail
        # (e.g. quoted output inside an otherwise-complete run).
        msg = (
            "Claude subprocess exited with code 1 without shipping a PR.\n\n"
            "--- Claude output tail ---\n"
            '{"type":"system","subtype":"init","cwd":"/tmp/workspace"}'
        )
        ctx = {
            "failure_category": "claude_code_failed",
            AGENT_ERROR_REPORT_CONTEXT_KEY: self._report(
                model_output_seen=True, result_event_seen=True
            ),
        }
        t = _task(msg, ctx)
        t.api_call_summary = None
        assert classify_failure(t) == CODE_ERROR

    def test_text_tail_fallback_still_fires_without_report(self):
        # Old agents send no report — the init-banner-only tail heuristic must
        # keep working as the fallback.
        msg = (
            "Claude subprocess exited with code 1 without shipping a PR.\n\n"
            "--- Claude output tail ---\n"
            '{"type":"system","subtype":"init","cwd":"/tmp/workspace"}'
        )
        t = _task(msg, {"failure_category": "claude_code_failed"})
        t.api_call_summary = None
        assert classify_failure(t) == RATE_LIMIT
        assert (
            t.context.get("rate_limit_inferred_from")
            == "cli_startup_death_init_only_tail"
        )

    def test_death_report_without_agent_category_classifies_rate_limit(self):
        # A handler that posts FAILED without a failure_category still benefits:
        # the no-category fallthrough consults the report before defaulting to
        # CODE_ERROR.
        t = _task(
            "agent died after provider call",
            {AGENT_ERROR_REPORT_CONTEXT_KEY: self._report()},
        )
        t.api_call_summary = None
        assert classify_failure(t) == RATE_LIMIT

    def test_malformed_report_falls_back_to_text_heuristics(self):
        # A non-dict report value behaves exactly like "no report".
        msg = (
            "Claude subprocess exited with code 1 without shipping a PR.\n\n"
            "--- Claude output tail ---\n"
            '{"type":"system","subtype":"init"}'
        )
        t = _task(
            msg,
            {
                "failure_category": "claude_code_failed",
                AGENT_ERROR_REPORT_CONTEXT_KEY: "not-a-dict",
            },
        )
        t.api_call_summary = None
        assert classify_failure(t) == RATE_LIMIT
        assert (
            t.context.get("rate_limit_inferred_from")
            == "cli_startup_death_init_only_tail"
        )


class TestSanitizeAgentErrorReport:
    def test_non_dict_returns_none(self):
        assert sanitize_agent_error_report(None) is None
        assert sanitize_agent_error_report("exit 1") is None
        assert sanitize_agent_error_report([1, 2]) is None

    def test_whitelists_and_bounds_fields(self):
        raw = {
            "schema": 1,
            "provider": "anthropic" * 20,
            "auth_mode": "oauth",
            "exit_code": 1,
            "exit_signal": "SIGTERM",
            "duration_ms": 5123.7,
            "model_output_seen": False,
            "result_event_seen": False,
            "stderr_tail": "x" * 10_000,
            "last_stream_event": "system",
            "classified": "rate_limit",
            "provider_errors": [
                {"source": "claude-stream", "code": "c" * 500, "message": "m" * 5000}
            ],
            "unexpected_key": {"nested": "junk"},
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        assert "unexpected_key" not in out
        assert len(out["provider"]) == 50
        assert out["exit_code"] == 1
        assert out["duration_ms"] == 5123
        assert len(out["stderr_tail"]) == 2000
        assert len(out["provider_errors"]) == 1
        assert len(out["provider_errors"][0]["message"]) == 500
        assert len(out["provider_errors"][0]["code"]) == 100

    def test_provider_errors_capped_and_invalid_entries_dropped(self):
        raw = {
            "exit_code": 1,
            "provider_errors": [{"message": f"err-{i}"} for i in range(10)]
            + ["not-a-dict", {"no_message": True}],
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        assert len(out["provider_errors"]) == 5

    def test_invalid_exit_code_becomes_none(self):
        out = sanitize_agent_error_report({"exit_code": "1"})
        assert out is not None
        assert out["exit_code"] is None
        # bool is an int subclass — must not pass as an exit code
        out2 = sanitize_agent_error_report({"exit_code": True})
        assert out2 is not None
        assert out2["exit_code"] is None

    def test_sanitized_death_report_still_classifies(self):
        # End-to-end shape check: a sanitized report round-trips through the
        # classifier (guards against the sanitizer dropping a load-bearing key).
        out = sanitize_agent_error_report(
            {
                "schema": 1,
                "exit_code": 1,
                "model_output_seen": False,
                "result_event_seen": False,
                "provider_errors": [],
            }
        )
        t = _task("exited 1", {"failure_category": "fatal"})
        t.api_call_summary = None
        t.context = {**t.context, AGENT_ERROR_REPORT_CONTEXT_KEY: out}
        assert classify_failure(t) == RATE_LIMIT


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

    def test_reset_failed_routes_to_provision_failure(self):
        task = _task("reset_failed: warm re-target failed")

        assert (
            normalize_agent_failure_category("reset_failed", task) == PROVISION_FAILURE
        )

    def test_oauth_rotation_failed_routes_to_external_api(self):
        task = _task("OAuth rotation failed before running task")

        assert (
            normalize_agent_failure_category("oauth_rotation_failed", task)
            == EXTERNAL_API
        )

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

    def test_runtime_unavailable_routes_to_provision_failure(self):
        """Agent emits runtime_unavailable when a required CLI binary (e.g. codex)
        is absent from PATH — infra decline, not a code defect.
        Must route to PROVISION_FAILURE so it requeues without burning retry budget."""
        task = _task("runtime_unavailable:codex")
        assert (
            normalize_agent_failure_category("runtime_unavailable", task)
            == PROVISION_FAILURE
        )

    def test_runtime_unavailable_via_classify_failure_context_fast_path(self):
        """End-to-end: classify_failure sees runtime_unavailable in context
        and routes to PROVISION_FAILURE without falling through to text classification."""
        task = _task(
            "runtime_unavailable:codex",
            {"failure_category": "runtime_unavailable"},
        )
        assert classify_failure(task) == PROVISION_FAILURE

    def test_vm_config_missing_routes_to_provision_failure(self):
        """Agent emits vm_config_missing when the provider CLI died because a
        boot/retarget-materialized config file is gone from the VM (Claude CLI
        exits in seconds with "Claude configuration file not found" when
        ~/.claude.json was destroyed). A VM/infra fault — must route to
        PROVISION_FAILURE (infra backoff + requeue on a fresh VM), never
        RATE_LIMIT: the generic-label init-banner-only tail reclassification
        misread these deaths as usage exhaustion (2026-06-06 incidents, tasks
        1330d291 / cf28097f)."""
        msg = (
            "Fatal error: Claude Code provider config file missing on VM "
            "(boot/retarget materialization lost) — "
            "Claude configuration file not found"
        )
        task = _task(msg)
        assert (
            normalize_agent_failure_category("vm_config_missing", task)
            == PROVISION_FAILURE
        )

    def test_vm_config_missing_via_classify_failure_context_fast_path(self):
        """End-to-end: classify_failure sees vm_config_missing in context and
        routes to PROVISION_FAILURE without falling through to the text
        classifier (whose init-banner heuristics produced the usage-exhausted
        misread this label exists to prevent)."""
        task = _task(
            "Fatal error: Claude configuration file not found",
            {"failure_category": "vm_config_missing"},
        )
        assert classify_failure(task) == PROVISION_FAILURE

    def test_diff_range_unresolvable_routes_to_provision_failure(self):
        """A release audit / audit-challenge / merge-grade handler emits
        diff_range_unresolvable when it cannot fetch the pinned candidate commit
        (or verify the three-dot diff range) before invoking the agent — a
        workspace candidate-fetch transient (one GitHub I/O blip on
        `git fetch origin <sha>`), NOT a code defect. Must route to
        PROVISION_FAILURE so the release qualifier respawns the audit/challenge
        leg under _MAX_AUDIT_RL_RESPAWNS instead of finalizing the whole attempt
        infra_failure. Before this mapping it fell through to classify_failure()
        and defaulted to EXTERNAL_API, which the respawn gate excludes — so a
        single fetch blip terminally burned the qualification attempt
        (2026-06-26: the SAME candidate re-fetched + COMPLETED ~9 min later)."""
        task = _task(
            "Release audit aborted: diff range main...<sha> does not resolve"
        )
        assert (
            normalize_agent_failure_category("diff_range_unresolvable", task)
            == PROVISION_FAILURE
        )

    def test_diff_range_unresolvable_via_classify_failure_context_fast_path(self):
        """End-to-end: classify_failure sees diff_range_unresolvable in context
        and routes to PROVISION_FAILURE (the value the respawn gate compares),
        not the EXTERNAL_API the text classifier would otherwise default to."""
        task = _task(
            "Release audit aborted: diff range does not resolve",
            {"failure_category": "diff_range_unresolvable"},
        )
        assert classify_failure(task) == PROVISION_FAILURE

    def test_github_token_fetch_failed_routes_to_provision_failure(self):
        """Agent emits github_token_fetch_failed when the startup github/token fetch
        times out or fails. The backend can legitimately block up to ~120s while
        _github_request sleeps on a near-empty rate-limit bucket, so a credential
        fetch failure is infra/transient — must route to PROVISION_FAILURE so the
        task requeues on a fresh VM without burning the terminal retry budget or
        cascading the parent plan (incident 2026-06-13/14: task ca3279e1 cascaded
        parent e9dbc87e because the 30s timeout fired and was classified CODE_ERROR)."""
        task = _task("GET /api/agent/github/token?task_id=x failed after 5 retries")
        assert (
            normalize_agent_failure_category("github_token_fetch_failed", task)
            == PROVISION_FAILURE
        )

    def test_github_token_fetch_failed_via_classify_failure_context_fast_path(self):
        """End-to-end: classify_failure sees github_token_fetch_failed in context and
        routes to PROVISION_FAILURE without falling through to the text classifier."""
        task = _task(
            "Fatal error: GET /api/agent/github/token?task_id=x failed after 5 retries",
            {"failure_category": "github_token_fetch_failed"},
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

    def test_failed_viewport_task_records_viewport_fingerprint(self):
        """FAILED task with viewport pattern populates _viewport_residual_store."""
        import app.failure_residual as fr
        from app.failure_residual import get_top_viewport_residual_fingerprints
        from app.models import TaskStatus

        fr._viewport_residual_store.clear()
        fr._viewport_residual_seen_task_ids.clear()

        t = MagicMock(spec=Task)
        t.latest_status_message = "expectnooverflowat( failed at 375px"
        t.context = {}
        t.id = "task-vp-record-classify"
        t.project_id = None
        t.status = TaskStatus.FAILED

        result = classify_failure(t)
        assert result == VIEWPORT_REGRESSION

        results = get_top_viewport_residual_fingerprints()
        assert any("task-vp-record-classify" in r["task_ids"] for r in results)


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
        """A 'Cancelled:' prefix routes to ADMIN_CANCEL even if tail looks like code."""
        msg = "Cancelled: AssertionError in downstream"
        assert classify_failure(_task(msg)) == ADMIN_CANCEL

    @pytest.mark.parametrize(
        "msg",
        [
            "Cancelled: dependency task failed",
            "Cancelled: parent dependency failed",
            "Cancelled: parent evaluation timeout",
            "Cancelled: AssertionError in downstream",
        ],
    )
    def test_cancelled_prefix_routes_to_admin_cancel(self, msg):
        """Tasks cancelled by orchestrator route to ADMIN_CANCEL, not AGENT_TIMEOUT."""
        assert classify_failure(_task(msg)) == ADMIN_CANCEL

    @pytest.mark.parametrize(
        "msg",
        [
            "Superseded by fix on PR #42",
            "superseded by newer revision",
        ],
    )
    def test_superseded_routes_to_admin_cancel(self, msg):
        """System-initiated supersession routes to ADMIN_CANCEL, not AGENT_TIMEOUT."""
        assert classify_failure(_task(msg)) == ADMIN_CANCEL

    def test_admin_cancel_in_infra_classes(self):
        """ADMIN_CANCEL must be budget-exempt (in INFRA_CLASSES)."""
        from app.failure_classification import INFRA_CLASSES

        assert ADMIN_CANCEL in INFRA_CLASSES

    def test_resident_session_stop_failure_category_routes_to_admin_cancel(self):
        """A strategic_review task stopped by the operator should classify as ADMIN_CANCEL.

        stop_resident_session stamps failure_category='ADMIN_CANCEL' on the task
        context so the classifier's fast-path (agent_category in INFRA_CLASSES)
        returns ADMIN_CANCEL immediately, without falling through to text-matching
        on the status message 'Resident strategic session stopped by operator'.
        """
        task = _task(
            "Resident strategic session stopped by operator",
            ctx={"failure_category": ADMIN_CANCEL},
        )
        assert classify_failure(task) == ADMIN_CANCEL


class TestAdminSupersedesPersistsAdminCancel:
    def test_admin_superseded_task_persists_admin_cancel_not_agent_timeout(self):
        """Admin supersession writes ADMIN_CANCEL, not AGENT_TIMEOUT.

        This covers the 6 administrative cancel call sites (review supersession,
        fix-supersedes-review, churn halt, dispatch dedup running/queued sibling,
        and queue-cleanup backstop) that previously hardcoded AGENT_TIMEOUT,
        causing phantom 90.8% cascade_cancellation dominance in compute_timeout_rate.
        """
        from app.digest_metrics import compute_timeout_rate

        task = _task(
            "Superseded by fix on PR #42: review was scoped to a pre-conflict snapshot"
        )
        task.started_at = object()  # marks it as execution-reached for the denominator

        persist_failure_category(task, ADMIN_CANCEL)

        assert task.context["failure_category"] == ADMIN_CANCEL
        assert "timeout_subtype" not in task.context

        result = compute_timeout_rate([task])
        assert result["timeout_count"] == 0
        assert result["by_subtype"]["cascade_cancellation"]["count"] == 0


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


class TestPrBranchDeleted:
    """Agent-emitted 'pr_branch_deleted' routes to SCOPE_EMPTY (task had no work)
    so it doesn't consume retry budget or penalize direction health."""

    def test_pr_branch_deleted_maps_without_warning(self, caplog):
        """The pr_branch_deleted category maps to SCOPE_EMPTY without falling
        through to the warning log that catches unmapped categories."""
        import logging

        task = _task("PR branch was already deleted (merged and auto-deleted)")
        with caplog.at_level(logging.WARNING):
            result = normalize_agent_failure_category("pr_branch_deleted", task)
        assert result == SCOPE_EMPTY
        # Verify no warning was logged for the unmapped category
        assert not any(
            "[FAILURE_CLASS] Unknown agent failure_category" in record.message
            for record in caplog.records
        )

    def test_pr_branch_deleted_via_context_fast_path(self):
        """classify_failure's context['failure_category'] fast path also honors
        the lowercase 'pr_branch_deleted' label."""
        task = _task(
            "PR branch was already deleted",
            {"failure_category": "pr_branch_deleted"},
        )
        assert classify_failure(task) == SCOPE_EMPTY

    def test_pr_branch_deleted_is_scope_empty(self):
        """Verify pr_branch_deleted routes to SCOPE_EMPTY class."""
        task = _task("PR branch deleted after merge")
        assert (
            normalize_agent_failure_category("pr_branch_deleted", task) == SCOPE_EMPTY
        )

    def test_pr_branch_deleted_in_infra_classes(self):
        """pr_branch_deleted maps to SCOPE_EMPTY which is in INFRA_CLASSES,
        so direction-health analytics don't penalize the agent."""
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

    def test_provider_quota_balance_preempts_generic_unauthorized(self):
        assert (
            _external_api_subtype_from_message(
                "HTTP 401 Unauthorized from api.deepseek.com: insufficient balance"
            )
            == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA
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

    def test_provider_quota_capacity_preempts_generic_auth(self):
        assert (
            _external_api_subtype_from_message(
                "api.deepseek.com overloaded: authentication fails"
            )
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


class TestParkWakeFailureClassRegistration:
    """park_wake_timeout / park_wake_stranded are canonical recognized classes.

    They are written by the reaper's wake-delivery sweep / expire path and must be
    budget-exempt INFRA classes with infra taxonomy so failure distributions stop
    presenting them as generic code-like residual fingerprints.
    """

    def test_constants_match_persisted_literals(self):
        from app.failure_classification import (
            PARK_WAKE_STRANDED,
            PARK_WAKE_TIMEOUT,
        )

        # Values MUST equal the literals the expire/strand paths persist into
        # task.context['failure_class'] so the persisted-verdict short-circuit
        # returns them unchanged.
        assert PARK_WAKE_TIMEOUT == "park_wake_timeout"
        assert PARK_WAKE_STRANDED == "park_wake_stranded"

    def test_recognized_and_budget_exempt(self):
        from app.failure_classification import (
            ALL_FAILURE_CLASSES,
            INFRA_CLASSES,
            PARK_WAKE_STRANDED,
            PARK_WAKE_TIMEOUT,
        )

        for cls in (PARK_WAKE_TIMEOUT, PARK_WAKE_STRANDED):
            assert cls in ALL_FAILURE_CLASSES
            assert cls in INFRA_CLASSES  # budget-exempt: no retry burn / health ding

    def test_classify_and_taxonomy(self):
        from app.failure_classification import (
            PARK_WAKE_STRANDED,
            PARK_WAKE_TIMEOUT,
            TAXONOMY_INFRA_RELATED,
            classify_failure,
            classify_failure_taxonomy,
        )

        for cls in (PARK_WAKE_TIMEOUT, PARK_WAKE_STRANDED):
            task = _task(ctx={"failure_category": cls, "failure_class": cls})
            assert classify_failure(task) == cls
            assert classify_failure_taxonomy(task) == TAXONOMY_INFRA_RELATED

    def test_normalize_agent_failure_category_canonicalizes(self):
        from app.failure_classification import (
            PARK_WAKE_STRANDED,
            PARK_WAKE_TIMEOUT,
            normalize_agent_failure_category,
        )

        task = _task()
        assert (
            normalize_agent_failure_category("park_wake_timeout", task)
            == PARK_WAKE_TIMEOUT
        )
        assert (
            normalize_agent_failure_category("park_wake_stranded", task)
            == PARK_WAKE_STRANDED
        )


class TestDeadAuthClassification:
    """Dead OAuth sessions must route to DEAD_AUTH (budget-exempt, auto-disable)
    rather than RATE_LIMIT (transient cooldown) or CODE_ERROR (burns retry slot).

    Observed 2026-06-10: OpenAI Codex accounts with terminated sessions were
    misclassified as RATE_LIMIT, burning 1–3 attempts per new task until
    blacklisted_accounts eventually routed around the dead account.
    """

    @staticmethod
    def _openai_oauth_report(**overrides) -> dict:
        report = {
            "schema": 1,
            "provider": "openai",
            "auth_mode": "oauth",
            "exit_code": 1,
            "exit_signal": None,
            "duration_ms": 20000,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "",
            "last_stream_event": "system",
        }
        report.update(overrides)
        return report

    @staticmethod
    def _anthropic_oauth_report(**overrides) -> dict:
        report = {
            "schema": 1,
            "provider": "anthropic",
            "auth_mode": "oauth",
            "exit_code": 1,
            "exit_signal": None,
            "duration_ms": 20000,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "",
            "last_stream_event": "system",
        }
        report.update(overrides)
        return report

    def _dead_auth_task(
        self, report: dict, category: str = "claude_code_failed"
    ) -> "Task":
        ctx: dict = {
            "failure_category": category,
            AGENT_ERROR_REPORT_CONTEXT_KEY: report,
            "llm_provider": report.get("provider", "openai"),
            "llm_auth_mode": report.get("auth_mode", "oauth"),
        }
        t = _task("Claude subprocess exited with code 1", ctx)
        t.api_call_summary = None
        return t

    # ── OpenAI OAUTH dead-auth signatures ──────────────────────────────────

    def test_app_session_terminated_classifies_dead_auth(self):
        report = self._openai_oauth_report(
            stderr_tail="Error: app_session_terminated\nYour session has ended."
        )
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) == DEAD_AUTH
        assert t.context.get("dead_auth_matched_pattern") == "app_session_terminated"

    def test_token_invalidated_classifies_dead_auth(self):
        report = self._openai_oauth_report(
            stderr_tail="token_invalidated: access token has been revoked by admin"
        )
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) == DEAD_AUTH
        assert t.context.get("dead_auth_matched_pattern") == "token_invalidated"

    def test_session_ended_message_classifies_dead_auth(self):
        report = self._openai_oauth_report(
            stderr_tail="Your session has ended. Please log in again."
        )
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) == DEAD_AUTH

    # ── Anthropic OAUTH dead-auth signatures ───────────────────────────────

    def test_anthropic_oauth_org_not_allowed_classifies_dead_auth(self):
        report = self._anthropic_oauth_report(
            stderr_tail="oauth_org_not_allowed: organization has disabled Claude Code OAuth"
        )
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) == DEAD_AUTH
        assert t.context.get("dead_auth_matched_pattern") == "oauth_org_not_allowed"

    def test_anthropic_token_refresh_failed_classifies_rate_limit(self):
        # token_refresh_failed is treated as transient (not a terminal dead-auth signal),
        # so it falls through to RATE_LIMIT and does NOT set dead_auth_matched_pattern.
        report = self._anthropic_oauth_report(
            stderr_tail="token_refresh_failed: permanent 401 from token endpoint"
        )
        t = self._dead_auth_task(report)
        assert classify_failure(t) == RATE_LIMIT
        assert "dead_auth_matched_pattern" not in t.context

    # ── Disambiguation: must NOT classify as dead-auth ─────────────────────

    def test_plain_rate_limit_stays_rate_limit(self):
        # Ordinary rate-limit death (no dead-auth pattern in stderr) must stay RATE_LIMIT.
        report = self._openai_oauth_report(
            stderr_tail="Rate limit reached for gpt-5.4-mini"
        )
        t = self._dead_auth_task(report)
        assert classify_failure(t) == RATE_LIMIT

    def test_model_output_seen_prevents_dead_auth(self):
        # If the model produced output before dying, the CLI passed auth —
        # can't be a dead session; must stay RATE_LIMIT.
        report = self._openai_oauth_report(
            stderr_tail="app_session_terminated",
            model_output_seen=True,
        )
        t = self._dead_auth_task(report)
        assert classify_failure(t) == RATE_LIMIT

    def test_api_key_auth_mode_no_dead_auth(self):
        # API-key auth has no OAuth session to die; dead-auth patterns must not fire.
        report = {
            "schema": 1,
            "provider": "openai",
            "auth_mode": "api_key",
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "app_session_terminated",
        }
        ctx = {
            "failure_category": "claude_code_failed",
            AGENT_ERROR_REPORT_CONTEXT_KEY: report,
            "llm_provider": "openai",
            "llm_auth_mode": "api_key",
        }
        t = _task("agent died", ctx)
        t.api_call_summary = None
        # API-key has no dead_auth_patterns, so falls through to RATE_LIMIT
        assert classify_failure(t) == RATE_LIMIT

    def test_no_report_no_dead_auth(self):
        # Without an error report (old agents) there's no stderr_tail to check —
        # must not classify as dead-auth.
        ctx = {
            "failure_category": "claude_code_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task("app_session_terminated in logs", ctx)
        t.api_call_summary = None
        # Text-only fallback can't distinguish dead-auth; stays RATE_LIMIT or
        # CODE_ERROR depending on whether the text matches a rate-limit pattern.
        result = classify_failure(t)
        from app.failure_classification import DEAD_AUTH

        assert result != DEAD_AUTH

    # ── Classification metadata ─────────────────────────────────────────────

    def test_dead_auth_is_infra_class(self):
        from app.failure_classification import (
            DEAD_AUTH,
            INFRA_CLASSES,
            ALL_FAILURE_CLASSES,
        )

        assert DEAD_AUTH in INFRA_CLASSES
        assert DEAD_AUTH in ALL_FAILURE_CLASSES

    def test_dead_auth_via_normalize(self):
        # The agent-status write boundary (normalize_agent_failure_category) must
        # also route to DEAD_AUTH when the error report has the signature.
        from app.failure_classification import DEAD_AUTH

        report = self._openai_oauth_report(
            stderr_tail="app_session_terminated\nYour session has ended."
        )
        ctx = {
            AGENT_ERROR_REPORT_CONTEXT_KEY: report,
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task("Claude subprocess exited with code 1", ctx)
        t.api_call_summary = None
        assert normalize_agent_failure_category("claude_code_failed", t) == DEAD_AUTH

    # ── refresh_token_reused (new pattern, incident 2026-06-13) ────────────

    def test_refresh_token_reused_classifies_dead_auth(self):
        # Real stderr from Codex CLI when a cached refresh token has been consumed.
        # Incident: account SavarinCodex stayed healthy (2.9% success rate) because
        # this phrasing wasn't in _OAUTH_DEAD_AUTH_PATTERNS → bucketed PROVISION_FAILURE
        # → never auto-disabled → every new task coin-flipped into the dead account.
        real_stderr = (
            "ERROR codex_login::auth::manager: Failed to refresh token: "
            '401 Unauthorized: {"error": {"message": "Your refresh token has '
            "already been used to generate a new access token. Please log out and "
            'sign in again."}}'
        )
        report = self._openai_oauth_report(stderr_tail=real_stderr)
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) == DEAD_AUTH
        assert (
            t.context.get("dead_auth_matched_pattern")
            == "refresh token has already been used"
        )

    def test_refresh_token_reused_not_classified_when_model_output_seen(self):
        # If the CLI produced model output before dying, auth was successful —
        # dead-auth must not fire even if the pattern somehow appears in stderr.
        real_stderr = "WARNING: refresh token was already used (late error)\n"
        report = self._openai_oauth_report(
            stderr_tail=real_stderr,
            model_output_seen=True,
        )
        t = self._dead_auth_task(report)
        from app.failure_classification import DEAD_AUTH

        assert classify_failure(t) != DEAD_AUTH

    # ── panel_review_lens_failed + dead-auth path (incident 2026-06-13) ────

    def test_panel_review_lens_failed_with_dead_auth_in_logs(self):
        # panel_review_lens_failed: when the Codex lens dies with a consumed
        # refresh token, failPanelClosed stores the full error message in
        # task.latest_status_message (via _sanitize_log_tail(payload.logs, 8000)).
        # For a startup-death the total output is short (< 4096 chars), so the
        # dead-auth guard fires and classification returns DEAD_AUTH, not
        # PROVISION_FAILURE.
        full_status = (
            "Panel review fan-out failed: lens 'correctness' agent pass exited 1.\n"
            "Reading prompt from stdin...\n"
            "2026-06-13T20:48:11.576087Z ERROR codex_login::auth::manager: "
            "Failed to refresh token: 401 Unauthorized: "
            '{"error": {"message": "Your refresh token has already been used '
            'to generate a new access token."}}\n'
        )
        ctx = {
            "failure_category": "panel_review_lens_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task(full_status, ctx)
        t.api_call_summary = None
        from app.failure_classification import DEAD_AUTH

        result = classify_failure(t)
        assert result == DEAD_AUTH
        assert (
            t.context.get("dead_auth_matched_pattern")
            == "refresh token has already been used"
        )

    def test_panel_review_lens_failed_dead_auth_not_fired_on_long_output(self):
        # If the lens ran successfully and produced kilobytes of model output
        # before failing closed (e.g. no usable verdict), latest_status_message
        # holds the last 8000 chars of that output — which may mention dead-auth
        # literals if the diff being reviewed touches oauth / test files.
        # The length guard (>= 4096 chars) must suppress the dead-auth scan
        # so a healthy account is never falsely disabled.
        model_output_with_dead_auth_phrase = (
            # Simulate a lens reviewing a diff that adds the dead-auth pattern
            # as a test string — the model quotes it in its analysis.
            "The test file adds 'refresh token has already been used' as a "
            "fixture string to cover the OAuth error case.  This looks correct "
            "and necessary for coverage. " * 200  # ~8000 chars of model output
        )
        # In production this is _sanitize_log_tail(failLogs, 8000) — the last
        # 8000 chars of "Panel review fan-out failed: ...\n<model output>".
        full_status = (
            "Panel review fan-out failed: lens 'correctness' produced no usable verdict.\n"
            + model_output_with_dead_auth_phrase
        )
        assert len(full_status) >= 4096, "test precondition: status must be long"
        ctx = {
            "failure_category": "panel_review_lens_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task(full_status, ctx)
        t.api_call_summary = None
        from app.failure_classification import DEAD_AUTH

        result = classify_failure(t)
        assert result != DEAD_AUTH, (
            "dead-auth must not fire when lens output is long (model output path)"
        )
        assert "dead_auth_matched_pattern" not in t.context

    def test_panel_review_lens_failed_rate_limited_classifies_rate_limit(self):
        # When the lens subprocess is rate-limited (429), the error appears in
        # latest_status_message.  Classification must return RATE_LIMIT (no-burn
        # requeue) not PROVISION_FAILURE (which burns attempt_count).
        # Uses "rate limit reached for" which is in _API_KEY_RATE_LIMIT_PATTERNS.
        full_status = (
            "Panel review fan-out failed: lens 'security' agent pass exited 1.\n"
            "Error: 429 Too Many Requests\nrate limit reached for model gpt-4o\n"
        )
        ctx = {
            "failure_category": "panel_review_lens_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "api_key",
        }
        t = _task(full_status, ctx)
        t.api_call_summary = None
        from app.failure_classification import (
            RATE_LIMIT,
            normalize_agent_failure_category,
        )

        # Drive via the production write-boundary path: normalize_agent_failure_category
        # persists failure_class to context (agent_status.py:1085-1096), then
        # classify_failure short-circuits on that persisted value.
        normalized = normalize_agent_failure_category("panel_review_lens_failed", t)
        assert normalized == RATE_LIMIT, (
            f"normalize should return RATE_LIMIT for rate-limited panel_review_lens_failed, got {normalized}"
        )
        # Simulate the agent_status.py write boundary persistence
        new_ctx = dict(t.context or {})
        new_ctx["failure_category"] = normalized
        new_ctx["failure_class"] = normalized
        t.context = new_ctx
        # classify_failure must short-circuit to the persisted RATE_LIMIT verdict
        result = classify_failure(t)
        assert result == RATE_LIMIT, (
            f"rate-limited panel_review_lens_failed should be RATE_LIMIT after normalize+persist, got {result}"
        )

    def test_panel_review_lens_failed_without_dead_auth_stays_provision_failure(self):
        # panel_review_lens_failed without a dead-auth or rate-limit pattern
        # should stay PROVISION_FAILURE (lens subprocess crashed for a generic
        # infra reason — no retry budget should be burned).
        full_status = (
            "Panel review fan-out failed: lens 'correctness' agent pass exited 1.\n"
            "Error: ENOENT: no such file or directory, open '/tmp/foo'\n"
        )
        ctx = {
            "failure_category": "panel_review_lens_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task(full_status, ctx)
        t.api_call_summary = None

        result = classify_failure(t)
        assert result == PROVISION_FAILURE
        assert "dead_auth_matched_pattern" not in t.context

    def test_model_no_access_is_config_rejection(self):
        # Acceptance proof: a 'selected model may not exist / no access' failure
        # from panel_review_lens_failed must classify as EXTERNAL_API
        # (provider_model_config subtype), NOT PROVISION_FAILURE.
        # PROVISION_FAILURE burns the retry budget; EXTERNAL_API does not.
        full_status = (
            "Panel review fan-out failed: lens 'security' agent pass exited 1.\n"
            "Error: 404 The model `o3-mini` does not exist or you do not have "
            "access to it.\n"
        )
        ctx = {
            "failure_category": "panel_review_lens_failed",
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        }
        t = _task(full_status, ctx)
        t.api_call_summary = None

        result = classify_failure(t)
        assert result == EXTERNAL_API, (
            f"model-no-access panel_review_lens_failed must classify as EXTERNAL_API, got {result!r}"
        )

        from app.failure_classification import (
            EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG,
            classify_external_api_subtype,
        )

        subtype = classify_external_api_subtype(t)
        assert subtype == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG, (
            f"expected provider_model_config subtype, got {subtype!r}"
        )

    # ── Cross-provider isolation (incident 2026-06-14) ─────────────────────

    def test_stale_dead_auth_verdict_short_circuits_new_attempt(self):
        """Demonstrate the cross-provider bug: a stale failure_class = 'DEAD_AUTH'
        in task.context causes _classify_failure_class to short-circuit and return
        DEAD_AUTH for any subsequent attempt — even on a completely different provider.

        This is the bug that disabled all 6 Anthropic accounts when Codex dead-auths
        wrote failure_class='DEAD_AUTH' + dead_auth_matched_pattern='token_invalidated'
        into task context, and the reaper reaped the next attempt (on an Anthropic
        account) using the short-circuit path.
        """
        from app.failure_classification import DEAD_AUTH

        ctx = {
            "failure_class": "DEAD_AUTH",  # stale verdict from Codex dead-auth
            "dead_auth_matched_pattern": "token_invalidated",  # OpenAI-only pattern
            "failure_category": "claude_code_failed",
            "llm_provider": "anthropic",  # task now runs on Anthropic
            "llm_auth_mode": "oauth",
        }
        t = _task("Some Anthropic failure", ctx)
        t.api_call_summary = None
        # The short-circuit fires: returns stale DEAD_AUTH without re-checking
        # whether token_invalidated is even an Anthropic dead-auth pattern.
        assert classify_failure(t) == DEAD_AUTH

    def test_cleared_dead_auth_context_classifies_anthropic_task_correctly(self):
        """After the requeue fix: failure_class and dead_auth_matched_pattern are
        cleared from context on dead-auth requeue, so the next attempt's
        classification is done fresh for the new provider.

        Regression test for incident 2026-06-14 — cross-provider dead-auth disable.
        Codex (openai/oauth) tasks that dead-auth with 'token_invalidated' must
        not cause Anthropic accounts to be disabled.
        """
        from app.failure_classification import DEAD_AUTH

        # Simulate the state AFTER the requeue path clears stale keys:
        # failure_class and dead_auth_matched_pattern are gone; the task now
        # carries a new Anthropic account assignment.
        ctx = {
            # These were cleared by the fix (popped before task.context = da_ctx):
            # "failure_class": "DEAD_AUTH",       ← cleared
            # "dead_auth_matched_pattern": ...,    ← cleared
            #
            # New Anthropic account assigned by the orchestrator:
            "llm_provider": "anthropic",
            "llm_auth_mode": "oauth",
            "failure_category": "claude_code_failed",
        }
        t = _task("Anthropic task failed for non-auth reason", ctx)
        t.api_call_summary = None
        # Without stale context, classify_failure runs the full heuristic.
        # An Anthropic task with a generic failure status and no dead-auth
        # patterns must NOT classify as DEAD_AUTH.
        result = classify_failure(t)
        assert result != DEAD_AUTH, (
            f"Anthropic task must not classify as DEAD_AUTH after Codex dead-auth "
            f"requeue clears stale context; got {result!r}"
        )

    def test_openai_dead_auth_pattern_does_not_match_anthropic_task(self):
        """Provider isolation: 'token_invalidated' is an OpenAI-only dead-auth
        pattern. An Anthropic task must not match it even if it appears in stderr.

        This validates that _task_dead_auth_patterns() uses the task's own
        provider patterns (not all providers') — an Anthropic task only triggers
        DEAD_AUTH on 'oauth_org_not_allowed', never on 'token_invalidated'.
        """
        from app.failure_classification import DEAD_AUTH

        report = self._anthropic_oauth_report(
            stderr_tail="token_invalidated: some error from somewhere"
        )
        t = self._dead_auth_task(report)
        # Anthropic patterns do NOT include "token_invalidated", so this must
        # fall through to a non-DEAD_AUTH class.
        assert classify_failure(t) != DEAD_AUTH
        assert "dead_auth_matched_pattern" not in t.context


class TestRedactCredentials:
    """Unit tests for the _redact_credentials helper.

    Each sub-class tests a specific credential family; the last sub-class
    tests that classification still works on sanitized text.
    """

    from app.failure_classification import _redact_credentials as _r

    # ── GitHub tokens ────────────────────────────────────────────────────────

    def test_ghp_token_redacted(self):
        from app.failure_classification import _redact_credentials

        assert _redact_credentials("token ghp_AbCdEfGhIjKl1234") == "token ghp_***"

    def test_ghs_token_redacted(self):
        from app.failure_classification import _redact_credentials

        assert _redact_credentials("auth ghs_AbCdEfGhIjKl1234") == "auth ghs_***"

    def test_github_pat_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("github_pat_11ABCDE12_longpatvalue123")
        assert out == "github_pat_***"

    def test_git_url_credential_redacted(self):
        from app.failure_classification import _redact_credentials

        url = "https://x-access-token:ghs_secret123@github.com/org/repo.git"
        out = _redact_credentials(url)
        assert "ghs_secret123" not in out
        assert "https://x-access-token:***@" in out

    # ── Anthropic keys ───────────────────────────────────────────────────────

    def test_sk_ant_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials(
            "key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
        )
        assert "sk-ant-api03-" not in out
        assert "sk-ant-***" in out

    # ── OpenAI keys ──────────────────────────────────────────────────────────

    def test_sk_proj_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("OPENAI_API_KEY=sk-proj-abcdefghijk1234567890extra")
        assert "sk-proj-abcdefghijk1234567890extra" not in out
        assert "OPENAI_API_KEY=***" in out

    def test_zai_api_key_env_var_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials(
            'ZAI_API_KEY=plain-zai-token ZAI_API_KEY="quoted-zai-token" '
            '{"ZAI_API_KEY":"json-zai-token","ANTHROPIC_AUTH_TOKEN":"json-anthropic-token"}'
        )
        assert "plain-zai-token" not in out
        assert "quoted-zai-token" not in out
        assert "json-zai-token" not in out
        assert "json-anthropic-token" not in out

    def test_sk_generic_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("key sk-abcdefghijklmnopqrstuvwxyz1234 used")
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in out
        assert "sk-***" in out

    def test_sk_too_short_not_redacted(self):
        from app.failure_classification import _redact_credentials

        # Fewer than 20 chars after "sk-" — must NOT be treated as an API key.
        out = _redact_credentials("error code sk-1234 encountered")
        assert "sk-1234" in out

    # ── Google AIza keys ─────────────────────────────────────────────────────

    def test_google_aiza_key_redacted(self):
        from app.failure_classification import _redact_credentials

        # 35 alphanumeric chars after "AIza"
        key = "AIza" + "A" * 35
        out = _redact_credentials(f"api key {key} used")
        assert key not in out
        assert "AIza***" in out

    def test_google_aiza_too_short_not_redacted(self):
        from app.failure_classification import _redact_credentials

        # 10 trailing chars — below {20,} minimum — must not match.
        key = "AIza" + "A" * 10
        out = _redact_credentials(f"key={key}")
        assert key in out

    # ── JWT tokens ───────────────────────────────────────────────────────────

    def test_jwt_three_part_redacted(self):
        from app.failure_classification import _redact_credentials

        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = _redact_credentials(f"Authorization: Bearer {jwt}")
        assert jwt not in out
        # The Bearer pattern fires first, or the JWT pattern — either redacts
        assert "eyJ" not in out or "eyJ***" in out

    # ── Bearer tokens ────────────────────────────────────────────────────────

    def test_bearer_token_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("Authorization: Bearer ya29.a0AfH6SMBx_longtoken")
        assert "ya29.a0AfH6SMBx_longtoken" not in out
        assert "Bearer ***" in out

    def test_ya29_bare_token_redacted(self):
        from app.failure_classification import _redact_credentials

        token = "ya29.a0AfH6SMBx_AbCdEfGhIjKlMnOpQrStUvWx"
        bare = _redact_credentials(token)
        kv = _redact_credentials(f"GOOGLE_oauth_token={token}")
        assert token not in bare
        assert token not in kv
        assert "ya29***" in bare
        assert "GOOGLE_oauth_token=" in kv
        assert "***" in kv

    def test_bearer_case_insensitive(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("authorization: BEARER secret_token_value")
        assert "secret_token_value" not in out

    # ── OAuth token query params ─────────────────────────────────────────────

    def test_oauth_token_query_param_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("GET /api?oauth_token=secretvalue&state=xyz")
        assert "secretvalue" not in out
        assert "oauth_token=***" in out

    # ── PEM private keys ─────────────────────────────────────────────────────

    def test_pem_private_key_redacted(self):
        from app.failure_classification import _redact_credentials

        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyz\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = _redact_credentials(pem)
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "***PRIVATE KEY***" in out

    # ── JSON/key-value credential fields ─────────────────────────────────────

    def test_access_token_json_field_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials('{"access_token": "ya29.a0AfH6SMBx"}')
        assert "ya29.a0AfH6SMBx" not in out
        assert "access_token" in out  # field name preserved

    def test_refresh_token_kv_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("refresh_token=1//0abcdefghijklmno")
        assert "1//0abcdefghijklmno" not in out

    def test_api_key_field_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials('api_key: "AIzaSyXXXXXXXXXXXXX"')
        assert "AIzaSyXXXXXXXXXXXXX" not in out

    def test_client_secret_field_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("client_secret=abcdefghijklmnop1234")
        assert "abcdefghijklmnop1234" not in out

    def test_id_token_field_redacted(self):
        from app.failure_classification import _redact_credentials

        # Short JWT-like value in id_token field
        out = _redact_credentials("id_token=eyJhbGciOiJSUzI1NiIsImtpZCI6Ijll")
        assert "eyJhbGciOiJSUzI1NiIsImtpZCI6Ijll" not in out

    def test_short_value_not_redacted(self):
        from app.failure_classification import _redact_credentials

        # Fewer than 8 chars — booleans/null/short IDs must not be redacted.
        out = _redact_credentials('{"token": "abc"}')
        assert "abc" in out

    def test_boolean_value_not_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials('{"token": false}')
        assert "false" in out

    # ── Empty / passthrough ──────────────────────────────────────────────────

    def test_empty_string_unchanged(self):
        from app.failure_classification import _redact_credentials

        assert _redact_credentials("") == ""

    def test_clean_text_unchanged(self):
        from app.failure_classification import _redact_credentials

        text = "No credentials here, just a normal error message."
        assert _redact_credentials(text) == text

    # ── AWS IAM access keys ──────────────────────────────────────────────────

    def test_aws_iam_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "AKIA***" in out

    def test_aws_iam_key_too_short_not_redacted(self):
        from app.failure_classification import _redact_credentials

        # 15 chars after AKIA — one short of the required 16
        out = _redact_credentials("AKIA123456789AB")
        assert "AKIA123456789AB" in out

    # ── Stripe keys ──────────────────────────────────────────────────────────

    def test_stripe_live_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("STRIPE_SECRET=sk_live_abcdef123456")
        assert "sk_live_abcdef123456" not in out
        assert "sk_live_***" in out

    def test_stripe_test_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("STRIPE_SECRET=sk_test_abcdef123456")
        assert "sk_test_abcdef123456" not in out
        assert "sk_test_***" in out

    def test_stripe_restricted_key_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("key=rk_live_abcdef123456")
        assert "rk_live_abcdef123456" not in out
        assert "rk_live_***" in out

    def test_stripe_webhook_secret_redacted(self):
        from app.failure_classification import _redact_credentials

        out = _redact_credentials("WEBHOOK_SECRET=whsec_abcdef123456")
        assert "whsec_abcdef123456" not in out
        assert "whsec_***" in out

    # ── Database DSNs with embedded credentials ──────────────────────────────

    def test_postgresql_url_credentials_redacted(self):
        from app.failure_classification import _redact_credentials

        dsn = "postgresql://dbuser:s3cr3t@db.host/mydb"
        out = _redact_credentials(dsn)
        assert "s3cr3t" not in out
        assert "postgresql://***:***@db.host/mydb" in out

    def test_postgres_alias_url_credentials_redacted(self):
        from app.failure_classification import _redact_credentials

        dsn = "postgres://dbuser:s3cr3t@db.host/mydb"
        out = _redact_credentials(dsn)
        assert "s3cr3t" not in out
        assert "postgres://***:***@db.host/mydb" in out

    def test_postgresql_asyncpg_url_credentials_redacted(self):
        from app.failure_classification import _redact_credentials

        dsn = "postgresql+asyncpg://dbuser:s3cr3t@db.host/mydb"
        out = _redact_credentials(dsn)
        assert "s3cr3t" not in out
        assert "postgresql://***:***@db.host/mydb" in out

    # ── Multiple patterns in one string ─────────────────────────────────────

    def test_multiple_credentials_all_redacted(self):
        from app.failure_classification import _redact_credentials

        text = (
            "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrst "
            "GOOGLE_KEY=AIza" + "B" * 35 + " "
            "Authorization: Bearer mytoken123"
        )
        out = _redact_credentials(text)
        assert "sk-proj-abcdefghijklmnopqrst" not in out
        assert "AIza" + "B" * 35 not in out
        assert "mytoken123" not in out


def _load_redaction_corpus() -> list[dict[str, str]]:
    return json.loads(CORPUS_PATH.read_text())


def test_redaction_corpus_parity():
    from app.broadcast import _sanitize_log
    from app.failure_classification import _redact_credentials

    for entry in _load_redaction_corpus():
        broadcast_out = _sanitize_log(entry["secret"])
        failure_out = _redact_credentials(entry["secret"])
        assert entry["raw"] not in broadcast_out, entry["family"]
        assert entry["raw"] not in failure_out, entry["family"]


class TestSanitizeReportAppliesRedaction:
    """sanitize_agent_error_report must apply _redact_credentials to
    stderr_tail and provider_errors[].message before storing them."""

    def test_stderr_tail_credentials_redacted(self):
        raw = {
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "Error: invalid key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        assert "sk-ant-api03-" not in out["stderr_tail"]
        assert "sk-ant-***" in out["stderr_tail"]

    def test_provider_error_message_credentials_redacted(self):
        raw = {
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [
                {
                    "source": "claude-stream",
                    "message": "Auth failed: ghp_AbCdEfGhIjKl1234secrettoken",
                }
            ],
            "stderr_tail": "",
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        assert len(out["provider_errors"]) == 1
        msg = out["provider_errors"][0]["message"]
        assert "ghp_AbCdEfGhIjKl1234secrettoken" not in msg
        assert "ghp_***" in msg

    def test_google_key_in_stderr_redacted(self):
        key = "AIza" + "C" * 35
        raw = {
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": f"GEMINI_API_KEY={key} caused auth error",
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        assert key not in out["stderr_tail"]
        assert "AIza***" in out["stderr_tail"]

    def test_openai_key_in_provider_error_redacted(self):
        raw = {
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [
                {
                    "message": "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxx raised 401",
                }
            ],
            "stderr_tail": "",
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        msg = out["provider_errors"][0]["message"]
        assert "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxx" not in msg
        assert "sk-proj-***" in msg

    def test_sanitized_report_still_classifies_rate_limit(self):
        """Redaction must not break the classifier — a sanitized startup-death
        report (no model output, non-zero exit) still routes to RATE_LIMIT."""
        raw = {
            "schema": 1,
            "exit_code": 1,
            "model_output_seen": False,
            "result_event_seen": False,
            "provider_errors": [],
            "stderr_tail": "OPENAI_API_KEY=sk-ant-api03-secret123456789 killed",
        }
        out = sanitize_agent_error_report(raw)
        assert out is not None
        t = _task("exited 1", {"failure_category": "fatal"})
        t.api_call_summary = None
        t.context = {**t.context, AGENT_ERROR_REPORT_CONTEXT_KEY: out}
        assert classify_failure(t) == RATE_LIMIT


class TestIsRecoverableInfraTerminal:
    """Unit tests for is_recoverable_infra_terminal — the guard that decides
    whether the reaper should preserve or close a PR when its fix task hits a
    terminal infrastructure failure."""

    def _fix_task(self, ctx: dict) -> "Task":
        t = MagicMock(spec=Task)
        t.context = ctx
        return t

    def test_returns_true_for_agent_timeout_with_budget_remaining(self):
        """An AGENT_TIMEOUT fix task with iterations remaining is recoverable."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 101},
            }
        )
        assert is_recoverable_infra_terminal(t) is True

    def test_returns_false_for_code_error(self):
        """A CODE_ERROR (merit failure) is NOT recoverable — PR should close."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "failure_category": "CODE_ERROR",
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 102},
            }
        )
        assert is_recoverable_infra_terminal(t) is False

    def test_returns_false_when_iteration_cap_exhausted(self):
        """When iteration == max_iterations the review loop has no budget left."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {
                    "iteration": 12,
                    "max_iterations": 12,
                    "pr_number": 103,
                },
            }
        )
        assert is_recoverable_infra_terminal(t) is False

    def test_returns_false_when_iteration_exceeds_cap(self):
        """iteration > max_iterations is also treated as exhausted."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {
                    "iteration": 13,
                    "max_iterations": 12,
                    "pr_number": 104,
                },
            }
        )
        assert is_recoverable_infra_terminal(t) is False

    def test_returns_true_when_no_review_loop_context(self):
        """No review_loop in context → iteration budget unknown → preserve PR."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task({"failure_category": "AGENT_TIMEOUT"})
        assert is_recoverable_infra_terminal(t) is True

    def test_returns_false_when_infra_history_at_threshold(self):
        """Three infra failures in _failure_class_history → budget exhausted."""
        from datetime import datetime, timedelta
        from app.failure_classification import is_recoverable_infra_terminal

        recent = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 105},
                "_failure_class_history": [
                    {"class": "AGENT_TIMEOUT", "at": recent},
                    {"class": "AGENT_TIMEOUT", "at": recent},
                    {"class": "AGENT_TIMEOUT", "at": recent},
                ],
            }
        )
        assert is_recoverable_infra_terminal(t) is False

    def test_returns_true_when_infra_history_below_threshold(self):
        """Two infra entries is below the threshold of 3 — still recoverable."""
        from datetime import datetime, timedelta
        from app.failure_classification import is_recoverable_infra_terminal

        recent = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 106},
                "_failure_class_history": [
                    {"class": "AGENT_TIMEOUT", "at": recent},
                    {"class": "AGENT_TIMEOUT", "at": recent},
                ],
            }
        )
        assert is_recoverable_infra_terminal(t) is True

    def test_provision_failure_also_recoverable(self):
        """PROVISION_FAILURE is in INFRA_CLASSES and is also recoverable."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "failure_category": "PROVISION_FAILURE",
                "review_loop": {"iteration": 1, "max_iterations": 10, "pr_number": 107},
            }
        )
        assert is_recoverable_infra_terminal(t) is True

    def test_missing_failure_category_returns_false(self):
        """No failure_category → not an infra class → False."""
        from app.failure_classification import is_recoverable_infra_terminal

        t = self._fix_task(
            {
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 108},
            }
        )
        assert is_recoverable_infra_terminal(t) is False

    def test_stale_history_entries_ignored_by_ttl(self):
        """Entries older than 48h are dropped by normalize_failure_class_history —
        stale repeated failures don't count toward the budget threshold."""
        from datetime import datetime, timedelta
        from app.failure_classification import is_recoverable_infra_terminal

        old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=72)).isoformat()
        t = self._fix_task(
            {
                "failure_category": "AGENT_TIMEOUT",
                "review_loop": {"iteration": 2, "max_iterations": 12, "pr_number": 109},
                "_failure_class_history": [
                    {"class": "AGENT_TIMEOUT", "at": old},
                    {"class": "AGENT_TIMEOUT", "at": old},
                    {"class": "AGENT_TIMEOUT", "at": old},
                ],
            }
        )
        # All three are expired → infra_count = 0 < 3 → still recoverable.
        assert is_recoverable_infra_terminal(t) is True


class TestPrereqEscalationClassification:
    """Prerequisite-escalation / co-deferred CANCELLED tasks must NOT be CODE_ERROR.

    When research_completion cancels a child task because its declared prerequisite
    has no live/deferred provider this cycle (prerequisite_escalated_to_parent=True)
    or because it is co-deferred alongside its same-cycle-deferred foundation
    (CO_DEFERRED_SHELVED), the failure classifier must return BLOCKED_PRECONDITION
    (a budget-exempt INFRA class) rather than falling through to CODE_ERROR.

    Acceptance proof for: test_prereq_escalation_not_code_error
    """

    def test_no_live_provider_routes_to_blocked_precondition(self):
        """prerequisite_escalated_to_parent=True + no_live_or_deferred_provider reason."""
        t = _task(
            "Escalated to parent: finding 'fix: foo' declared task prerequisite(s) "
            "with no spawned, deferred, or in-flight provider.",
            {
                "prerequisite_escalated_to_parent": True,
                "prerequisite_escalation_reason": "no_live_or_deferred_provider",
            },
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_structurally_unresolvable_routes_to_blocked_precondition(self):
        """prerequisite_escalated_to_parent=True + structurally_unresolvable reason."""
        t = _task(
            "Escalated to parent: finding 'fix: bar' declared only "
            "structurally unresolvable prerequisite(s).",
            {
                "prerequisite_escalated_to_parent": True,
                "prerequisite_escalation_reason": "structurally_unresolvable",
            },
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_co_deferred_shelved_routes_to_blocked_precondition(self):
        """CO_DEFERRED_SHELVED marker routes to BLOCKED_PRECONDITION."""
        t = _task(
            "",
            {CO_DEFERRED_SHELVED: True},
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION

    def test_prereq_escalation_not_code_error(self):
        """Selector asserted in task acceptance evidence."""
        t = _task(
            "Escalated to parent: finding 'feat: inject domain knowledge' declared "
            "task prerequisite(s) with no spawned, deferred, or in-flight provider.",
            {
                "prerequisite_escalated_to_parent": True,
                "prerequisite_escalation_reason": "no_live_or_deferred_provider",
            },
        )
        result = classify_failure(t)
        assert result != CODE_ERROR
        assert result == BLOCKED_PRECONDITION

    def test_prereq_escalation_overrides_persisted_code_error(self):
        """Context marker overrides a previously-persisted failure_class=CODE_ERROR."""
        t = _task(
            "Escalated to parent: finding 'fix: baz' declared only "
            "structurally unresolvable prerequisite(s).",
            {
                "prerequisite_escalated_to_parent": True,
                "failure_class": CODE_ERROR,
            },
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION


class TestCiInfraFlakeLogSignatures:
    """The promotion gate's Postgres/npm setup-flake signatures must classify as
    infra flakes so the harvest self-heals (re-qualify) instead of spawning a
    doomed code-fix task — the #12492 incident, where a postgres:15 pull blip and
    an npm ci timeout blocked the promotion with zero real regression."""

    def test_postgres_setup_flakes_are_infra(self):
        from app.failure_classification import _is_ci_infra_flake_log

        for log in (
            "::error::could not pull postgres:15 from Docker Hub or mirror.gcr.io",
            "::error::PostgreSQL did not become ready",
            # Pre-existing signatures still classify.
            "npm error network request failed",
            "toomanyrequests: you have reached your pull rate limit",
        ):
            assert _is_ci_infra_flake_log(log), log

    def test_ambiguous_setup_signatures_are_not_infra(self):
        """Signatures that can accompany a REAL, code-fixable failure must NOT be
        classified as infra (dropped after review):
          - "pull failed (attempt" is a per-attempt breadcrumb emitted even when
            the pull RECOVERS on retry, co-mingled with real pytest output.
          - "npm ci failed after" fires on a deterministic lockfile mismatch as
            readily as on a registry outage.
        Misrouting either to infra would defeat the self-heal (doomed re-qualify
        loop) instead of spawning the fix."""
        from app.failure_classification import _is_ci_infra_flake_log

        for log in (
            "::warning::postgres:15 pull failed (attempt 2/3) — backing off",
            "::error::npm ci failed after 3 attempts",
        ):
            assert not _is_ci_infra_flake_log(log), log

    def test_real_test_failures_are_not_infra(self):
        from app.failure_classification import _is_ci_infra_flake_log

        # A genuine assertion / collection error must NOT be swallowed as infra —
        # otherwise a real regression would be re-qualified into promotion.
        for log in (
            "E       AssertionError: assert 'open' == 'promoted'",
            "ModuleNotFoundError: No module named 'app.does_not_exist'",
            "<pytest crashed or hung; no FAILED summary captured>",
            "",
        ):
            assert not _is_ci_infra_flake_log(log), log

    def test_prereq_escalation_is_in_infra_classes(self):
        from app.failure_classification import INFRA_CLASSES

        assert BLOCKED_PRECONDITION in INFRA_CLASSES

    def test_co_deferred_overrides_persisted_code_error(self):
        """CO_DEFERRED_SHELVED overrides a stale persisted CODE_ERROR verdict."""
        t = _task(
            "",
            {
                CO_DEFERRED_SHELVED: True,
                "failure_class": CODE_ERROR,
            },
        )
        assert classify_failure(t) == BLOCKED_PRECONDITION
