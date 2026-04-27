"""Tests for failure_classification subtype helpers and classify_failure()."""
from unittest.mock import MagicMock

import pytest

from app.failure_classification import (
    AGENT_TIMEOUT,
    ALL_FAILURE_CLASSES,
    CODE_ERROR,
    EXTERNAL_API,
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
    SCOPE_OVERFLOW,
    _external_api_subtype_from_message,
    _fingerprint_message,
    _timeout_subtype_from_message,
    classify_external_api_subtype,
    classify_failure,
    classify_failure_taxonomy,
    classify_timeout_subtype,
    get_account_failure_class_counts,
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
    assert task.context["llm_runtime_resolution_error"] == "invalid_provider_or_auth_mode"
    assert task.context["llm_provider_raw"] == "not-a-provider"


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
        assert task.context["rate_limit_matched_pattern"] == "quota exceeded for quota metric"

    def test_specific_code_label_stays_authoritative(self):
        msg = "pytest failed while docs mention quota exceeded for quota metric"
        task = _task(msg, {"llm_provider": "gemini", "llm_auth_mode": "api_key"})

        assert normalize_agent_failure_category("test_failed", task) == CODE_ERROR
        assert "rate_limit_kind" not in task.context

    def test_generic_fatal_without_provider_signal_stays_code_error(self):
        task = _task("Fatal error: TypeError: cannot read property of undefined")

        assert normalize_agent_failure_category("fatal", task) == CODE_ERROR


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

    def test_pr_creation_heartbeat_is_agent_timeout(self):
        """Regression: the reaper calls classify_failure before updating latest_status_message.
        Tasks that die (OOM/SIGKILL) while the agent is creating a PR leave the PR-creation
        heartbeat as the last status message. Without this pattern, classify_failure falls
        through to CODE_ERROR and record_residual_fingerprint creates a spurious fingerprint
        that triggers auto-remediation for a non-existent code defect."""
        msg = "[13.6MB] Creating pull request for blueprint/task-df8cfed3..."
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
    ])
    def test_external_api_keywords(self, msg):
        assert classify_failure(_task(msg)) == EXTERNAL_API

    def test_database_error_is_external_api(self):
        assert classify_failure(_task("psycopg.OperationalError")) == EXTERNAL_API
        assert classify_failure(_task("asyncpg.exceptions.ConnectionFailure")) == EXTERNAL_API


class TestClassifyFailureMigration:
    @pytest.mark.parametrize("msg", [
        "alembic.util.exc.CommandError: Can't locate revision identified by '0042abc'",
        "RevisionError: revision 'abc123' not found",
        "Multiple head revisions are present for given argument 'head'",
        "Only a single head supported, found 2",
        "Multiple heads are present in the migration history",
        "psycopg.errors.UndefinedColumn: column tasks.new_field does not exist",
        "asyncpg.exceptions.UndefinedColumnError: column does not exist",
        "Schema version mismatch: expected 0042 got 0041",
        "EXPECTED_SCHEMA_REVISION mismatch on startup",
    ])
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
        assert classify_failure(_task("psycopg.OperationalError: connection refused")) == EXTERNAL_API
        assert classify_failure(_task("could not connect to server: timeout")) == EXTERNAL_API

    def test_generic_operator_does_not_exist_not_migration(self):
        assert classify_failure(_task("operator does not exist: text = int")) == EXTERNAL_API

    def test_migration_taxonomy_is_code_defect(self):
        t = _task("RevisionError: cannot locate")
        assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT


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
        task = _task(openai_err, ctx={"llm_provider": "openai", "llm_auth_mode": "api_key"})
        result = classify_failure(task, logs=openai_err)
        assert result != PROVISION_FAILURE


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
        t = _task("all accounts in rate-limit cooldown (earliest recovery: 2026-04-24T12:30:00+00:00)")
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


class TestFingerprintMessage:
    def test_strips_uuid(self):
        fp = _fingerprint_message("task 550e8400-e29b-41d4-a716-446655440000 failed")
        assert '<id>' in fp
        assert '550e8400' not in fp

    def test_strips_numbers(self):
        fp = _fingerprint_message("exit code 137 after 300s")
        assert '<n>' in fp
        assert '137' not in fp

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

    def _task_with_id(self, msg: str, task_id: str) -> "Task":
        t = MagicMock(spec=Task)
        t.latest_status_message = msg
        t.context = {}
        t.id = task_id
        return t

    def test_record_then_query(self):
        t = self._task_with_id("AttributeError: NoneType has no attr 'x'", "task-1")
        record_residual_fingerprint(t)
        results = get_top_residual_fingerprints(window_seconds=3600)
        assert len(results) == 1
        assert results[0]['count'] == 1
        assert results[0]['task_ids'] == ['task-1']

    def test_dedup_same_task_id(self):
        t = self._task_with_id("some error", "task-abc")
        record_residual_fingerprint(t)
        record_residual_fingerprint(t)
        results = get_top_residual_fingerprints()
        assert results[0]['count'] == 1

    def test_near_duplicates_cluster(self):
        t1 = self._task_with_id("AttributeError: expected 3, got 4", "task-1")
        t2 = self._task_with_id("AttributeError: expected 9, got 12", "task-2")
        record_residual_fingerprint(t1)
        record_residual_fingerprint(t2)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]['count'] == 2

    def test_classify_failure_fallthrough_records(self):
        t = self._task_with_id("AttributeError: NoneType has no attr 'x'", "task-ff")
        result = classify_failure(t)
        assert result == CODE_ERROR
        results = get_top_residual_fingerprints()
        assert any(r['task_ids'] == ['task-ff'] or 'task-ff' in r['task_ids'] for r in results)

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
        assert results[0]['count'] == 2

    def test_status_message_retained_as_context(self):
        """Even when fingerprint is driven by the error line, the agent
        status at failure time is still stored for human display."""
        t = self._task_with_id("[12.3mb] Running tests...", "task-status")
        err_logs = "AssertionError: expected true to be false"
        record_residual_fingerprint(t, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        # sample_message = error line (fingerprint source)
        assert "AssertionError" in results[0]['sample_message']
        # sample_status = original heartbeat (human context)
        assert "Running tests" in results[0].get('sample_status', '')

    def test_window_filter_excludes_old(self):
        import app.failure_classification as fc
        fc._residual_store['old-fp'] = {
            'count': 5,
            'first_seen': 0.0,
            'last_seen': 0.0,
            'sample_message': 'old error',
            'task_ids': ['t-old'],
        }
        results = get_top_residual_fingerprints(window_seconds=60)
        assert all(r['fingerprint'] != 'old-fp' for r in results)

    def test_sorted_by_count_descending(self):
        import app.failure_classification as fc
        import time
        now = time.time()
        fc._residual_store['fp-a'] = {'count': 3, 'first_seen': now, 'last_seen': now, 'sample_message': 'a', 'task_ids': []}
        fc._residual_store['fp-b'] = {'count': 10, 'first_seen': now, 'last_seen': now, 'sample_message': 'b', 'task_ids': []}
        results = get_top_residual_fingerprints()
        assert results[0]['fingerprint'] == 'fp-b'
        assert results[1]['fingerprint'] == 'fp-a'

    def test_no_record_for_non_code_error(self):
        t = self._task_with_id("No heartbeat received after 300s", "task-timeout")
        result = classify_failure(t)
        assert result == AGENT_TIMEOUT
        results = get_top_residual_fingerprints()
        assert not any('task-timeout' in r['task_ids'] for r in results)


class TestDeriveFailureStageMigration:
    """Migration-specific keywords map to the 'migration' stage."""

    def test_revision_error(self):
        from app.failure_stage import _derive_failure_stage
        assert _derive_failure_stage(
            "MIGRATION_ERROR", "alembic.script.revision.RevisionError: foo"
        ) == "migration"

    def test_multiple_heads(self):
        from app.failure_stage import _derive_failure_stage
        assert _derive_failure_stage(
            "MIGRATION_ERROR", "Multiple heads are present for given argument 'head'"
        ) == "migration"

    def test_undefined_column_maps_to_migration_not_integration(self):
        from app.failure_stage import _derive_failure_stage
        # UndefinedColumnError would match the import-error bank; migration
        # must be checked first.
        assert _derive_failure_stage(
            "MIGRATION_ERROR", "psycopg.errors.UndefinedColumnError: column x does not exist"
        ) == "migration"

    def test_schema_version_mismatch(self):
        from app.failure_stage import _derive_failure_stage
        assert _derive_failure_stage(
            "MIGRATION_ERROR", "schema version mismatch: expected_schema_revision=0037"
        ) == "migration"

    def test_alembic_command_error(self):
        from app.failure_stage import _derive_failure_stage
        assert _derive_failure_stage(
            "MIGRATION_ERROR", "alembic.util.exc.CommandError: can't locate revision"
        ) == "migration"


class TestExternalApiSubtypeFromMessage:
    def test_db_connectivity(self):
        assert _external_api_subtype_from_message("psycopg.OperationalError: connection refused") == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_db_connectivity_asyncpg(self):
        assert _external_api_subtype_from_message("asyncpg.exceptions.ConnectionFailure: too many clients") == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_db_connectivity_could_not_connect(self):
        assert _external_api_subtype_from_message("could not connect to server: Connection refused") == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_provider_model_config(self):
        msg = "ModelNotFoundError: models/gemini-3.1-flash-preview is not found for API version v1beta"
        assert _external_api_subtype_from_message(msg) == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG

    def test_provider_auth_unauthorized(self):
        assert _external_api_subtype_from_message("401 Unauthorized from api.anthropic.com") == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    def test_provider_auth_bad_credentials(self):
        assert _external_api_subtype_from_message("Bad credentials returned from GitHub") == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    def test_provider_auth_token_expired(self):
        assert _external_api_subtype_from_message("token expired, please re-authenticate") == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    def test_github_pr_create(self):
        assert _external_api_subtype_from_message("failed to create pull request: 422 Unprocessable Entity") == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE

    def test_github_pr_create_rollup(self):
        assert _external_api_subtype_from_message("rollup pr creation failed on main") == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE

    def test_github_pr_create_merge_blocked(self):
        assert _external_api_subtype_from_message("merge blocked by branch protection rules") == EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE

    def test_provider_quota_overloaded(self):
        assert _external_api_subtype_from_message("Error: Overloaded") == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    def test_provider_quota_context_length(self):
        assert _external_api_subtype_from_message("context_length_exceeded on model request") == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    def test_provider_quota_github_rate_limit(self):
        assert _external_api_subtype_from_message("api.github.com returned 429 too many requests") == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    def test_network_transient_503(self):
        assert _external_api_subtype_from_message("503 service unavailable from upstream") == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT

    def test_network_transient_ssl(self):
        assert _external_api_subtype_from_message("SSL handshake failed during connection") == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT

    def test_network_transient_dns(self):
        assert _external_api_subtype_from_message("could not resolve hostname api.example.com") == EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT

    def test_other_fallback(self):
        assert _external_api_subtype_from_message("some unknown external api error") == EXTERNAL_API_SUBTYPE_OTHER

    def test_empty_is_other(self):
        assert _external_api_subtype_from_message("") == EXTERNAL_API_SUBTYPE_OTHER


class TestClassifyExternalApiSubtype:
    def test_delegates_to_helper(self):
        task = _FakeTask("psycopg.OperationalError: connection refused")
        assert classify_external_api_subtype(task) == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_combines_logs(self):
        task = _FakeTask("")
        assert classify_external_api_subtype(task, logs="asyncpg connection failure") == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_none_status(self):
        task = _FakeTask(None)  # type: ignore[arg-type]
        assert classify_external_api_subtype(task) == EXTERNAL_API_SUBTYPE_OTHER


class TestPersistFailureCategoryExternalApiSubtype:
    def test_external_api_persists_subtype(self):
        task = _task("psycopg.OperationalError: connection refused")
        persist_failure_category(task, EXTERNAL_API)
        assert task.context["failure_category"] == EXTERNAL_API
        assert task.context["external_api_subtype"] == EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    def test_external_api_persists_auth_subtype(self):
        task = _task("401 Unauthorized")
        persist_failure_category(task, EXTERNAL_API)
        assert task.context["external_api_subtype"] == EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    def test_external_api_persists_model_config_subtype(self):
        task = _task("400: Unsupported value: 'service_tier' does not support 'flex' with this model")
        persist_failure_category(task, EXTERNAL_API)
        assert task.context["external_api_subtype"] == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG

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
        fc._account_failure_log["acct-2"] = {CODE_ERROR: [time.time() - 90000]}
        counts = get_account_failure_class_counts("acct-2", window_seconds=86400)
        assert counts[CODE_ERROR] == 0

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
