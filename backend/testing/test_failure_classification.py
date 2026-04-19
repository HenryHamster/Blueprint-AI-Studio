"""Tests for failure_classification subtype helpers and classify_failure()."""
from unittest.mock import MagicMock

import pytest

from app.failure_classification import (
    AGENT_TIMEOUT,
    CODE_ERROR,
    EXTERNAL_API,
    PROVISION_FAILURE,
    RATE_LIMIT,
    SCOPE_OVERFLOW,
    persist_failure_category,
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
    _timeout_subtype_from_message,
    classify_failure,
    classify_failure_taxonomy,
    classify_timeout_subtype,
    should_trigger_code_defect_investigation,
    should_trigger_review_quality_investigation,
    should_trigger_timeout_investigation,
)
from app.models import Task


class _FakeTask:
    def __init__(self, msg: str) -> None:
        self.latest_status_message = msg


class TestTimeoutSubtypeFromMessage:
    def test_oom(self):
        assert _timeout_subtype_from_message("exit 137 out of memory") == TIMEOUT_OOM

    def test_heartbeat_lost(self):
        assert _timeout_subtype_from_message("No heartbeat received after 300s") == TIMEOUT_HEARTBEAT_LOST

    def test_iteration_limit(self):
        assert _timeout_subtype_from_message("review-fix cycle limit reached") == TIMEOUT_ITERATION_LIMIT
        assert _timeout_subtype_from_message("iteration cap exceeded") == TIMEOUT_ITERATION_LIMIT

    def test_cascade_via_cancelled_prefix(self):
        assert _timeout_subtype_from_message("Cancelled: parent evaluation timeout") == TIMEOUT_CASCADE

    def test_cascade_via_superseded(self):
        assert _timeout_subtype_from_message("task superseded by newer revision") == TIMEOUT_CASCADE

    def test_cascade_precedes_heartbeat(self):
        # cascade signal should win over a concurrent heartbeat keyword
        assert _timeout_subtype_from_message("Cancelled: no heartbeat") == TIMEOUT_CASCADE

    def test_process_crash(self):
        assert _timeout_subtype_from_message("agent process crashed unexpectedly") == TIMEOUT_PROCESS_CRASH

    def test_execution_timeout(self):
        assert _timeout_subtype_from_message("execution timed out after 3600s") == TIMEOUT_EXECUTION

    def test_unknown(self):
        assert _timeout_subtype_from_message("something weird happened") == TIMEOUT_UNKNOWN

    def test_empty(self):
        assert _timeout_subtype_from_message("") == TIMEOUT_UNKNOWN


class TestClassifyTimeoutSubtypeDelegates:
    def test_delegates_to_helper(self):
        task = _FakeTask("exit code 137 OOM killer")
        assert classify_timeout_subtype(task) == TIMEOUT_OOM

    def test_combines_logs(self):
        task = _FakeTask("")
        assert classify_timeout_subtype(task, logs="No heartbeat received") == TIMEOUT_HEARTBEAT_LOST

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
        persist_failure_category(task, AGENT_TIMEOUT, logs="No heartbeat received after 300s")
        assert task.context["timeout_subtype"] == TIMEOUT_HEARTBEAT_LOST

    def test_non_timeout_does_not_write_subtype(self):
        task = _task("AssertionError")
        persist_failure_category(task, CODE_ERROR)
        assert task.context["failure_category"] == CODE_ERROR
        assert "timeout_subtype" not in task.context


class TestClassifyFailureCodeError:
    """Non-keyword failures fall through to CODE_ERROR (the default)."""

    @pytest.mark.parametrize("msg", [
        "AssertionError: expected 3, got 4",
        "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
        "NameError: name 'foo' is not defined",
        "SyntaxError: invalid syntax at line 42",
    ])
    def test_generic_python_errors(self, msg):
        assert classify_failure(_task(msg)) == CODE_ERROR


class TestClassifyFailureProvision:
    @pytest.mark.parametrize("msg", [
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
    ])
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
    @pytest.mark.parametrize("msg", [
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
        "Cancelled: parent evaluation timeout",
        "Cancelled: some new orchestrator reason",
        "Superseded by fix on PR #1234",
    ])
    def test_timeout_keywords(self, msg):
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT

    def test_cascade_beats_code_error_default(self):
        """A 'Cancelled:' prefix routes to AGENT_TIMEOUT even if tail looks like code."""
        msg = "Cancelled: AssertionError in downstream"
        assert classify_failure(_task(msg)) == AGENT_TIMEOUT


class TestClassifyFailureExternalApi:
    @pytest.mark.parametrize("msg", [
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
        "Error: Overloaded",
    ])
    def test_external_api_keywords(self, msg):
        assert classify_failure(_task(msg)) == EXTERNAL_API

    def test_database_error_is_external_api(self):
        assert classify_failure(_task("psycopg.OperationalError")) == EXTERNAL_API
        assert classify_failure(_task("asyncpg.exceptions.ConnectionFailure")) == EXTERNAL_API


class TestClassifyFailureRateLimit:
    @pytest.mark.parametrize("msg", [
        "API Error: Rate limit reached",
        "You've hit your limit · resets 5am",
        "Claude AI usage limit reached",
        "5-hour limit reached",
        "This request would exceed your account's rate limit.",
    ])
    def test_verbatim_claude_rate_limit_phrases(self, msg):
        assert classify_failure(_task(msg)) == RATE_LIMIT

    def test_generic_429_does_not_match(self):
        """Bare '429' / 'rate limit' must not classify as RATE_LIMIT — only exact
        Claude phrases may disable the account."""
        assert classify_failure(_task("HTTP 429 from upstream service")) == CODE_ERROR


class TestClassifyFailureScopeOverflow:
    def test_agent_reported_scope_overflow(self):
        t = _task("scope exceeded", {"failure_category": "scope_overflow"})
        assert classify_failure(t) == SCOPE_OVERFLOW

    def test_canonical_scope_overflow_in_context(self):
        t = _task("", {"failure_category": "SCOPE_OVERFLOW"})
        assert classify_failure(t) == SCOPE_OVERFLOW


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


def _code_defect_task(task_type: str = "create", direction_id: str | None = "d-1") -> Task:
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
        assert should_trigger_code_defect_investigation(_code_defect_task(direction_id=None)) is False

    def test_investigate_type_skipped(self):
        assert should_trigger_code_defect_investigation(_code_defect_task(task_type="investigate")) is False

    def test_smoketest_type_skipped(self):
        assert should_trigger_code_defect_investigation(_code_defect_task(task_type="smoketest")) is False

    def test_review_quality_taxonomy_skipped(self):
        t = _task("pytest failures in test_foo", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_code_defect_investigation(t) is False

    def test_infra_failure_skipped(self):
        t = _task("No heartbeat received", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_code_defect_investigation(t) is False


def _timeout_task(subtype: str, task_type: str = "create", direction_id: str | None = "d-1") -> Task:
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
        assert should_trigger_timeout_investigation(_timeout_task("process_crash")) is True

    def test_heartbeat_lost_triggers(self):
        assert should_trigger_timeout_investigation(_timeout_task("heartbeat_lost")) is True

    def test_cascade_skipped(self):
        assert should_trigger_timeout_investigation(_timeout_task("cascade_cancellation")) is False

    def test_iteration_limit_skipped(self):
        assert should_trigger_timeout_investigation(_timeout_task("iteration_limit")) is False

    def test_no_direction_skipped(self):
        assert should_trigger_timeout_investigation(_timeout_task("oom", direction_id=None)) is False

    def test_investigate_type_skipped(self):
        assert should_trigger_timeout_investigation(_timeout_task("oom", task_type="investigate")) is False

    def test_non_timeout_failure_skipped(self):
        t = _task("AttributeError: NoneType has no 'x'", {"direction_id": "d-1", "timeout_subtype": "oom"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_timeout_investigation(t) is False


def _review_quality_task(task_type: str = "create", direction_id: str | None = "d-1") -> Task:
    ctx: dict = {"scope": "small"}
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    t = _task("pytest failures in test_foo", ctx)
    t.payloads = [{"type": task_type, "prompt": "p"}]
    return t


class TestShouldTriggerReviewQualityInvestigation:
    def test_review_quality_with_direction_triggers(self):
        assert should_trigger_review_quality_investigation(_review_quality_task()) is True

    def test_no_direction_skipped(self):
        assert should_trigger_review_quality_investigation(_review_quality_task(direction_id=None)) is False

    def test_investigate_type_skipped(self):
        assert should_trigger_review_quality_investigation(_review_quality_task(task_type="investigate")) is False

    def test_smoketest_type_skipped(self):
        assert should_trigger_review_quality_investigation(_review_quality_task(task_type="smoketest")) is False

    def test_code_defect_taxonomy_skipped(self):
        t = _task("AttributeError: NoneType has no 'x'", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_review_quality_investigation(t) is False

    def test_infra_failure_skipped(self):
        t = _task("No heartbeat received", {"direction_id": "d-1"})
        t.payloads = [{"type": "create", "prompt": "p"}]
        assert should_trigger_review_quality_investigation(t) is False
