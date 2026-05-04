"""Task failure classification for deployment monitoring in Blueprint AI Studio."""
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    confidence: float


def _resolve_task_provider_auth(task: "Task"):
    """Resolve (provider, auth_mode) enums for a task, or (None, None) on any failure.

    Shared entry point for both _task_rate_limit_patterns (legacy tuple-of-strings)
    and _task_rate_limit_signal (structured result). Keeps the provider/auth-mode
    lookup logic in one place.
    """
    from app.max_plan_pool import get_task_llm_auth_mode, get_task_llm_provider
    from app.models import LLMAuthMode, LLMProviderName

    provider_raw = get_task_llm_provider(task)
    auth_mode_raw = get_task_llm_auth_mode(task)
    if not provider_raw or not auth_mode_raw:
        return None, None
    try:
        return LLMProviderName(provider_raw), LLMAuthMode(auth_mode_raw)
    except ValueError as exc:
        try:
            ctx = dict(getattr(task, "context", {}) or {})
            ctx["llm_runtime_resolution_failed"] = True
            ctx["llm_runtime_resolution_error"] = "invalid_provider_or_auth_mode"
            ctx["llm_provider_raw"] = provider_raw
            ctx["llm_auth_mode_raw"] = auth_mode_raw
            task.context = ctx
        except Exception:
            logger.exception(
                "[FAILURE_CLASS] Failed to mark invalid provider/auth context for task %s",
                getattr(task, "id", None),
            )
        logger.warning(
            "[FAILURE_CLASS] Invalid task provider/auth mode for task %s: provider=%r auth_mode=%r (%s)",
            getattr(task, "id", None),
            provider_raw,
            auth_mode_raw,
            exc,
        )
        return None, None


def _task_rate_limit_patterns(task: "Task") -> tuple[str, ...]:
    from app.max_plan_pool import RATE_LIMIT_PATTERNS
    from app.providers.registry import get_provider

    provider, auth_mode = _resolve_task_provider_auth(task)
    if provider is None or auth_mode is None:
        return tuple(RATE_LIMIT_PATTERNS)
    try:
        return get_provider(provider).rate_limit_patterns(auth_mode)
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Failed to resolve rate-limit patterns for provider=%s auth_mode=%s",
            provider,
            auth_mode,
        )
        return tuple(RATE_LIMIT_PATTERNS)


def _task_rate_limit_signal(task: "Task", combined: str):
    """Return a RateLimitSignal for the task+text, or None.

    Uses the task's provider/auth_mode to pick the right classifier. When
    provider/auth_mode are unresolvable (e.g. legacy task without structured
    context), falls back to None — callers treat that as "legacy rate-limit
    match" with no kind/retry info. Never raises.
    """
    from app.providers.registry import get_provider

    provider, auth_mode = _resolve_task_provider_auth(task)
    if provider is None or auth_mode is None:
        return None
    try:
        return get_provider(provider).classify_rate_limit_signal(auth_mode, combined)
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] classify_rate_limit_signal failed for provider=%s auth_mode=%s",
            provider,
            auth_mode,
        )
        return None


# Canonical failure classes that the classifier and rollups use.
# Any mutation here must be reflected in the analytics sets that import INFRA_CLASSES.
CODE_ERROR = "CODE_ERROR"
PROVISION_FAILURE = "PROVISION_FAILURE"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
EXTERNAL_API = "EXTERNAL_API"
RATE_LIMIT = "RATE_LIMIT"
# Structural mismatch between task scope and actual change footprint — the agent
# planned/produced more files than the scope budget allows. Not a code defect
# and not an infra outage; retry logic uses this class to route through scope
# escalation (medium→large decomposition) instead of a same-scope retry.
SCOPE_OVERFLOW = "SCOPE_OVERFLOW"
# Migration-specific code defect: alembic chain breaks, duplicate heads,
# missing migrations causing UndefinedColumn at runtime, EXPECTED_SCHEMA_REVISION
# mismatches, or downgrade NotImplementedError/OperationalError. Distinct from
# CODE_ERROR so auto-remediation can apply migration-specific fixes (rechain
# down_revision, regenerate migration, bump EXPECTED_SCHEMA_REVISION).
MIGRATION_ERROR = "MIGRATION_ERROR"


# Substrings that uniquely identify migration-related failures. Kept narrow so
# generic database connectivity errors stay in EXTERNAL_API.
_MIGRATION_PATTERNS: tuple[str, ...] = (
    "can't locate revision",
    "cant locate revision",
    "revisionerror",
    "multiple head revisions",
    "only a single head supported",
    "multiple heads are present",
    "undefinedcolumn",
    "undefinedcolumnerror",
    "schema version mismatch",
    "expected_schema_revision",
    "alembic.util.exc.commanderror",
)


# Provider model/config rejections are upstream runtime incompatibilities, not
# repository code defects. Keep this list narrow so ordinary test failures that
# mention "model" still fall through normally.
_PROVIDER_MODEL_CONFIG_PATTERNS: tuple[str, ...] = (
    "modelnotfounderror",
    "model not found",
    "is not found for api version",
    "is not supported for generatecontent",
    "unsupported service_tier",
    "unsupported value: 'service_tier'",
)


def _provider_model_config_rejection(combined: str) -> bool:
    return any(kw in combined for kw in _PROVIDER_MODEL_CONFIG_PATTERNS)


# Failure classes that don't penalize direction health / research productivity —
# the task never really ran because the infra (VMs, rate limits, upstream APIs,
# agent lifecycle) got in the way, or the scope was structurally mismatched
# (SCOPE_OVERFLOW — wrong budget, not bad code). Imported by direction_outcomes
# and research_completion so both stay in sync.
INFRA_CLASSES: frozenset[str] = frozenset({
    PROVISION_FAILURE,
    RATE_LIMIT,
    AGENT_TIMEOUT,
    EXTERNAL_API,
    SCOPE_OVERFLOW,
})

# Ordered tuple of all canonical failure classes for per-account health panel display.
ALL_FAILURE_CLASSES: tuple[str, ...] = (
    CODE_ERROR, RATE_LIMIT, EXTERNAL_API, PROVISION_FAILURE, MIGRATION_ERROR, AGENT_TIMEOUT, SCOPE_OVERFLOW,
)


# Taxonomy sub-categories — finer-grained grouping for failure-mode analysis.
# INFRA_RELATED covers everything in INFRA_CLASSES; the other three partition
# CODE_ERROR failures into review-quality (test/lint/style), scope-related
# (complexity/size), and residual code defects.
TAXONOMY_REVIEW_QUALITY = "review_quality"
TAXONOMY_SCOPE_RELATED = "scope_related"
TAXONOMY_CODE_DEFECT = "code_defect"
TAXONOMY_INFRA_RELATED = "infra_related"


_REVIEW_QUALITY_KEYWORDS = (
    "pytest", "jest", "ruff", "eslint", "tsc", "mypy",
    "lint_failed", "test_failed", "build_failed",
    "lint error", "lint failed", "tests failed", "test failure",
    "type error", "typecheck", "type check",
    "style", "formatting",
)

_SCOPE_KEYWORDS = (
    "too many changes", "too many files", "multi-file conflict",
    "scope too large", "exceeds scope",
    "scope complexity", "implementation complexity exceeds", "complexity budget",
)


def classify_failure_taxonomy(task: "Task") -> str:
    """Return a 4-category taxonomy label for a failed task.

    Buckets CODE_ERROR failures into review_quality / scope_related / code_defect
    based on status-message signals and task context; all INFRA_CLASSES map to
    infra_related. Reuses classify_failure() for the base class.
    """
    base = classify_failure(task)
    # SCOPE_OVERFLOW is in INFRA_CLASSES for retry/budget routing, but it
    # represents a scope-estimation mismatch, not an infra outage. Route it
    # to scope_related so operators see it in the correct analytics bucket.
    if base == SCOPE_OVERFLOW:
        return TAXONOMY_SCOPE_RELATED
    if base in INFRA_CLASSES:
        return TAXONOMY_INFRA_RELATED
    # Migration errors are code defects (the schema is wrong / migration missing),
    # not review-quality or scope issues — route to code_defect bucket directly.
    if base == MIGRATION_ERROR:
        return TAXONOMY_CODE_DEFECT

    status_msg = (task.latest_status_message or "").lower()
    ctx = task.context or {}

    if ctx.get("failure_detail") == "review_cap_exhausted":
        return TAXONOMY_REVIEW_QUALITY

    if any(kw in status_msg for kw in _REVIEW_QUALITY_KEYWORDS):
        return TAXONOMY_REVIEW_QUALITY

    expansion_ratio = ctx.get("expansion_ratio")
    has_scope_evidence = (
        any(kw in status_msg for kw in _SCOPE_KEYWORDS)
        or (isinstance(expansion_ratio, (int, float)) and expansion_ratio > 1.5)
    )
    # scope_calibrated_from alone is not evidence — a calibrated task can fail
    # for unrelated code defects. Require corroborating scope evidence.
    if has_scope_evidence:
        return TAXONOMY_SCOPE_RELATED

    return TAXONOMY_CODE_DEFECT


# Task types excluded from code_defect investigation triggers — investigate tasks
# would recurse, and smoketests are exploratory probes whose failures don't
# represent a code defect worth diagnosing.
_INVESTIGATION_EXCLUDED_TYPES: frozenset[str] = frozenset({"investigate", "smoketest"})


# Minimum number of review-quality failures (in the recent sliding window on a
# direction) before the frequency-based review-quality investigation trigger
# fires. Individual review-quality failures are retried in-place; the trigger
# only fires once a pattern has formed (3+ in the window) suggesting a
# systemic issue.
REVIEW_QUALITY_STREAK_THRESHOLD = 3


def should_trigger_review_quality_investigation(task: "Task") -> bool:
    """Return True if a failed task is a candidate for a review-quality pattern
    investigation (gating only — streak check is performed at the spawn site
    because it requires an async DB query).

    Same exclusions as ``should_trigger_code_defect_investigation``: no
    direction_id → skip; investigate/smoketest types → skip. Taxonomy must be
    review_quality (classify_failure_taxonomy).
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    return classify_failure_taxonomy(task) == TAXONOMY_REVIEW_QUALITY


def should_trigger_code_defect_investigation(task: "Task") -> bool:
    """Return True if a failed task warrants spawning a CODE_ERROR investigation.

    Gates the task-level investigation trigger: only fires for CODE_ERROR
    failures with code_defect taxonomy (review_quality and scope_related have
    their own handling), skips investigate/smoketest tasks to prevent self-loops,
    and requires a direction_id so the investigation can be linked.
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    if classify_failure(task) != CODE_ERROR:
        return False
    return classify_failure_taxonomy(task) == TAXONOMY_CODE_DEFECT


# AGENT_TIMEOUT sub-types that indicate a code-caused failure (resource exhaustion
# or process crash) worth investigating. Operational sub-types (cascade, iteration
# limit, execution timeout) are excluded — they reflect orchestrator decisions or
# wall-clock limits, not defects in the code under test.
INVESTIGATABLE_TIMEOUT_SUBTYPES: frozenset[str] = frozenset({
    "oom",
    "process_crash",
    "heartbeat_lost",
})


def should_trigger_timeout_investigation(task: "Task") -> bool:
    """Return True if an AGENT_TIMEOUT failure warrants spawning an investigation.

    Mirrors ``should_trigger_code_defect_investigation`` but for the subset of
    timeout sub-types that indicate code-caused resource issues (OOM, crash,
    heartbeat loss) rather than operational cancellations.
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    if classify_failure(task) != AGENT_TIMEOUT:
        return False
    subtype = ctx.get("timeout_subtype") or classify_timeout_subtype(task)
    return subtype in INVESTIGATABLE_TIMEOUT_SUBTYPES


_AGENT_CATEGORY_TO_CLASS: dict[str, str] = {
    'clone_failed': PROVISION_FAILURE,
    # build_failed = test setup couldn't run (Postgres sidecar startup, Docker
    # build, dependency install). Agent emits this when the test environment
    # itself failed to come up, NOT when user code failed to compile — the
    # latter would surface as test_failed or claude_code_failed. Routing as
    # PROVISION_FAILURE gets the 5-min infra backoff and resurrection-sweep
    # eligibility instead of burning the code-error retry budget on something
    # the agent can't fix by editing code.
    'build_failed': PROVISION_FAILURE,
    'test_failed': CODE_ERROR,
    'lint_failed': CODE_ERROR,
    'pr_creation_failed': EXTERNAL_API,
    'claude_code_failed': CODE_ERROR,
    'parse_failed': CODE_ERROR,
    'fatal': CODE_ERROR,
    'rate_limited': RATE_LIMIT,
    'scope_overflow': SCOPE_OVERFLOW,
    'timeout': AGENT_TIMEOUT,
    # 2026-05-04: emitted by the new Claude-driven flow.
    # ``agent_self_failed`` — Claude called ``./fail "<reason>"`` because the
    # task fundamentally couldn't be completed (scope wrong, blocker, etc.).
    # The reason text is preserved as latest_status_message; the category
    # routes through the CODE_ERROR retry budget so the orchestrator's
    # diagnosis pipeline (direction-hold investigation, etc.) still fires.
    'agent_self_failed': CODE_ERROR,
    # ``incomplete`` — wrapper-fallback when Claude exited without calling
    # ./done|./fail and didn't open a PR. Distinct from claude_code_failed
    # (which is for non-zero exit) so analytics can separate "Claude bailed
    # silently" from "Claude crashed."
    'incomplete': CODE_ERROR,
}


_GENERIC_AGENT_CODE_LABELS: frozenset[str] = frozenset({
    "claude_code_failed",
    "fatal",
})


def _persist_rate_limit_signal_context(task: "Task", signal) -> None:
    """Persist rate-limit signal metadata onto task.context.

    Audit 2026-04-25 (W5/B1): defensive ``getattr`` instead of direct
    attribute access. If the provider returns a partial signal (missing
    .kind etc.) the previous version silently swallowed the AttributeError
    and downstream retry logic had no rate_limit_kind to read — RATE_LIMIT
    was returned but the cooldown key was missing, so requeue ran
    immediately instead of waiting. Now we always write *something* (kind=
    "unknown" when the source is malformed) plus an explicit
    ``rate_limit_persistence_failed`` flag so the requeue path can apply a
    conservative cooldown.
    """
    kind = getattr(signal, "kind", None)
    retry_after = getattr(signal, "retry_after_seconds", None)
    matched = getattr(signal, "matched_pattern", None)
    try:
        ctx = dict(task.context or {})
        ctx["rate_limit_kind"] = kind or "unknown"
        if retry_after is not None:
            ctx["rate_limit_retry_after_seconds"] = retry_after
        if matched is not None:
            ctx["rate_limit_matched_pattern"] = matched
        if kind is None:
            ctx["rate_limit_persistence_failed"] = True
            logger.warning(
                "[FAILURE_CLASS] Task %s rate-limit signal missing .kind; persisted with kind=unknown",
                task.id,
            )
        task.context = ctx
    except Exception:
        logger.exception("[FAILURE_CLASS] Failed to persist rate_limit_* keys")


def _text_provider_failure_override(task: "Task", combined: str) -> str | None:
    """Return a strong provider/API class from text, or None.

    Used only to override generic agent labels like "fatal"; precise agent
    categories such as test_failed/lint_failed remain authoritative.
    """
    if _provider_model_config_rejection(combined):
        return EXTERNAL_API

    signal = _task_rate_limit_signal(task, combined)
    if signal is not None:
        _persist_rate_limit_signal_context(task, signal)
        return RATE_LIMIT

    if any(kw in combined for kw in _task_rate_limit_patterns(task)):
        return RATE_LIMIT

    return None


# Explicit phrase match so status messages like
# "No heartbeat received after 300s since provisioning" (which aren't
# provision failures — they're heartbeat/startup timeouts) don't get
# misclassified just because they contain the substring "provision".
# The pattern matches the verbs/nouns that clearly mean "provisioning broke"
# ("failed to provision", "provisioning failed", "provision error", "VM provisioning …")
# while rejecting benign references like "since provisioning".
_PROVISION_PHRASE_RE = re.compile(
    r"\b("
    r"failed to provision"
    r"|provision(?:ing)? failed"
    r"|provision(?:ing)? error"
    r"|vm provisioning"
    r")\b",
    re.IGNORECASE,
)


_SEVERITY: dict[str, int] = {
    MIGRATION_ERROR: 3,
    CODE_ERROR: 3,
    AGENT_TIMEOUT: 2,
    PROVISION_FAILURE: 1,
    EXTERNAL_API: 1,
    RATE_LIMIT: 1,
    SCOPE_OVERFLOW: 1,
}


def worst_child_failure_category(children: list["Task"]) -> str:
    """Return the most severe failure category among a list of failed child tasks.

    Severity ranking: CODE_ERROR (3) > AGENT_TIMEOUT (2) > infra classes (1).
    Unclassified children and unknown categories are treated as CODE_ERROR.
    Falls back to CODE_ERROR if the list is empty.
    """
    best_rank = 0
    best_cat = CODE_ERROR
    for child in children:
        raw_cat = (child.context or {}).get("failure_category") or CODE_ERROR
        cat = raw_cat if raw_cat in _SEVERITY else CODE_ERROR
        rank = _SEVERITY[cat]
        if rank > best_rank:
            best_rank = rank
            best_cat = cat
    return best_cat


def persist_failure_category(
    task: "Task",
    failure_class: str,
    logs: str | None = None,
    confidence: float | None = None,
) -> None:
    """Write ``failure_class`` onto ``task.context['failure_category']`` in place.

    All terminal-FAILED code paths must call this (or write the key themselves)
    so direction rollups and research dedup can tell infra from code failures.
    For AGENT_TIMEOUT, also persists ``timeout_subtype`` (computed from the
    task's status message plus optional ``logs``) so downstream systems —
    direction_outcomes, scope calibration, analytics — can correlate subtype
    with retry outcomes without recomputing from the (possibly overwritten)
    status message later.
    """
    ctx = dict(task.context or {})
    ctx["failure_category"] = failure_class
    if failure_class == AGENT_TIMEOUT:
        status_msg = task.latest_status_message or ""
        ctx["timeout_subtype"] = _timeout_subtype_from_message(status_msg + " " + (logs or ""))
    if failure_class == EXTERNAL_API:
        status_msg = task.latest_status_message or ""
        ctx["external_api_subtype"] = _external_api_subtype_from_message(status_msg + " " + (logs or ""))
    task.context = ctx
    account_id = ctx.get("llm_account_id") or ctx.get("max_plan_account_id")
    if account_id:
        record_account_failure_class(str(account_id), failure_class, confidence=confidence)


def normalize_agent_failure_category(raw: str | None, task: "Task") -> str:
    """Canonicalize an agent-reported ``failure_category`` label to an uppercase class name.

    The agent posts lowercase short labels (``rate_limited``, ``claude_code_failed``, …)
    but every consumer (rollups, dedup, analytics) compares against the uppercase
    canonical names. Normalize at the write boundary so the rest of the system sees
    a single vocabulary.
    """
    if not raw:
        return classify_failure(task)
    label = raw.strip()
    label_lower = label.lower()
    mapped = _AGENT_CATEGORY_TO_CLASS.get(label_lower)
    if mapped:
        if mapped == CODE_ERROR and label_lower in _GENERIC_AGENT_CODE_LABELS:
            combined = (task.latest_status_message or "").lower()
            override = _text_provider_failure_override(task, combined)
            if override:
                return override
        return mapped
    if label in INFRA_CLASSES or label == CODE_ERROR:
        return label
    logger.warning(
        "[FAILURE_CLASS] Unknown agent failure_category=%r on task %s; falling back to classify_failure",
        raw, getattr(task, "id", "?"),
    )
    return classify_failure(task)


# AGENT_TIMEOUT sub-types — finer breakdown of the catch-all timeout class so
# digest consumers can tell a heartbeat loss from an OOM from a cascade cancel.
# Keywords mirror the same phrases classify_failure() uses to route to AGENT_TIMEOUT.
TIMEOUT_HEARTBEAT_LOST = "heartbeat_lost"
TIMEOUT_OOM = "oom"
TIMEOUT_EXECUTION = "execution_timeout"
TIMEOUT_CASCADE = "cascade_cancellation"
TIMEOUT_ITERATION_LIMIT = "iteration_limit"
TIMEOUT_PROCESS_CRASH = "process_crash"
TIMEOUT_UNKNOWN = "unknown"


def _timeout_subtype_from_message(combined: str) -> str:
    """Route a combined status+logs string to an AGENT_TIMEOUT sub-type label.

    Order matters: cascade/supersession and iteration caps are checked before
    heartbeat/OOM keywords because a cascade-cancelled task may transit through
    any state and its status message is the authoritative signal.
    """
    combined = (combined or "").lower()
    stripped = combined.lstrip()

    if stripped.startswith("cancelled:") or "superseded" in combined:
        return TIMEOUT_CASCADE
    if any(kw in combined for kw in ("iteration cap", "review not spawned",
                                     "fix not spawned", "review-fix cap reached",
                                     "review-fix cycle limit",
                                     "ci wait exceeded", "ci wait timed out")):
        return TIMEOUT_ITERATION_LIMIT
    if any(kw in combined for kw in ("out of memory", "oom-kill", "memory limit",
                                     "cannot allocate", "cgroup",
                                     "exit code 137", "oom killer")):
        return TIMEOUT_OOM
    if any(kw in combined for kw in ("no heartbeat", "reaped", "never started")):
        return TIMEOUT_HEARTBEAT_LOST
    if any(kw in combined for kw in ("[fatal:", "fatal error", "process crashed",
                                     "node process killed")):
        return TIMEOUT_PROCESS_CRASH
    if any(kw in combined for kw in ("execution timed out", "sigterm")):
        return TIMEOUT_EXECUTION
    return TIMEOUT_UNKNOWN


def classify_timeout_subtype(task: "Task", logs: str | None = None) -> str:
    """Return a sub-type label for an AGENT_TIMEOUT failure."""
    status_msg = task.latest_status_message or ""
    return _timeout_subtype_from_message(status_msg + " " + (logs or ""))


# EXTERNAL_API sub-types — finer breakdown so auto-remediation can apply
# tailored recovery patterns per failure mode.
EXTERNAL_API_SUBTYPE_PROVIDER_AUTH = "provider_auth"
EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA = "provider_quota"
EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG = "provider_model_config"
EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE = "github_pr_create"
EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY = "db_connectivity"
EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT = "network_transient"
EXTERNAL_API_SUBTYPE_OTHER = "other"


def _external_api_subtype_from_message(combined: str) -> str:
    """Route a combined status+logs string to an EXTERNAL_API sub-type label.

    Order matters: specific failure signatures are checked before generic ones.
    """
    combined = (combined or "").lower()

    # Database connectivity — checked first because psycopg/asyncpg errors can
    # contain generic "connection" substrings that would otherwise match network_transient.
    if any(kw in combined for kw in (
        "could not connect to server", "connection to server",
        "database connection", "psycopg", "asyncpg",
        "operator does not exist",
    )):
        return EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    if _provider_model_config_rejection(combined):
        return EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG

    # Provider auth — token/credential failures from any upstream API.
    if any(kw in combined for kw in (
        "401", "unauthorized", "bad credentials",
        "token expired", "token invalid", "token revoked",
        "authentication failed", "invalid api key",
    )):
        return EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    # GitHub PR creation / merge-block failures.
    if ("failed to create" in combined and "branch" in combined) or \
       any(kw in combined for kw in (
           "failed to create pull request", "rollup pr creation failed",
           "merge blocked", "merge attempt", "merge conflicts",
           "merge failed", "merge hard-cap", "merge hard cap",
           "merge check failed", "not mergeable",
           "repository rule", "rule violations",
           "required status check", "branch protection",
       )):
        return EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE

    # GitHub rate limits.
    if any(gh_kw in combined for gh_kw in ("github", "api.github.com")) and \
       any(kw in combined for kw in ("rate limit", "429", "too many requests")):
        return EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    # Provider quota / rate exhaustion / capacity (non-auth upstream limits).
    if any(kw in combined for kw in (
        "overloaded", "rate limit", "429", "too many requests",
        "context_length_exceeded", "token limit exceeded",
        "quota exceeded", "resource exhausted",
    )):
        return EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    # Transient network / TLS / DNS errors.
    if any(kw in combined for kw in (
        "connection timed out", "name resolution failed",
        "ssl", "certificate",
        "could not resolve hostname", "name or service not known",
        "network unreachable",
        "500 internal server error", "502 bad gateway", "503 service unavailable",
    )):
        return EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT

    return EXTERNAL_API_SUBTYPE_OTHER


def classify_external_api_subtype(task: "Task", logs: str | None = None) -> str:
    """Return a sub-type label for an EXTERNAL_API failure."""
    status_msg = task.latest_status_message or ""
    combined = status_msg + " " + (logs or "")
    return _external_api_subtype_from_message(combined)


# Bounded in-memory store for CODE_ERROR fallthrough fingerprints.
# record_residual_fingerprint() populates it at classify_failure()'s default
# branch; get_top_residual_fingerprints() is the query API for admin_digest.
_residual_store: dict[str, dict] = {}
_residual_seen_task_ids: set[str] = set()
_RESIDUAL_MAX_FINGERPRINTS = 300
_RESIDUAL_SEEN_MAX = 20_000

# Fingerprints whose ResidualRemediationCase has been closed organically.
# Updated by the reaper sweep; consulted by get_top_residual_fingerprints to
# prevent re-spawning remediation tasks for fingerprints that stopped occurring.
_residual_closed_fingerprints: set[str] = set()


def _mark_residual_case_closed(fingerprint: str) -> None:
    """Mark a fingerprint as having a closed ResidualRemediationCase."""
    _residual_closed_fingerprints.add(fingerprint)


def _reactivate_residual_case(fingerprint: str) -> None:
    """Remove fingerprint from the closed set (case reopened on new occurrence)."""
    _residual_closed_fingerprints.discard(fingerprint)


# Per-account failure class log — rolling 24h window, keyed account_id →
# list[(failure_class, confidence, timestamp)].
_account_failure_log: dict[str, list[tuple[str, float | None, float]]] = {}

_RE_UUID = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
    re.IGNORECASE,
)
_RE_HEX = re.compile(r'\b[0-9a-f]{7,40}\b', re.IGNORECASE)
# Accept decimal memory readings ([10.5mb], [10.5MB cg:512.4MB]) — the old
# integer-only prefix regex let decimal variants slip through as distinct
# fingerprints, creating one "residual CODE_ERROR" per memory reading.
_RE_MEM_PREFIX = re.compile(
    r'^\s*\[\d+(?:\.\d+)?\s*(?:mb|gb|kb|b)'
    r'(?:\s+cg:\d+(?:\.\d+)?\s*(?:mb|gb|kb|b))?\]\s*',
    re.IGNORECASE,
)
# Generic size/duration tokens with unit adjacency — these don't hit word
# boundaries (digit adjacent to letter) so _RE_NUM can't reach them. Strip
# before numbering so "10.5mb" and "10.4mb" collapse together rather than
# leaving stray "5mb"/"4mb" differentiators.
_RE_SIZE_TOKEN = re.compile(
    r'\b\d+(?:\.\d+)?\s*(?:gib|mib|kib|gb|mb|kb|bytes?|b|ms|s)\b',
    re.IGNORECASE,
)
_RE_NUM = re.compile(r'\b\d+\b')


# Structural patterns that mark a LINE as error-shaped. Each must match
# at line-start (with optional leading whitespace) — never substring-
# anywhere — so prose like ``avoid except Exception: blocks (BLE001)``
# or JSON prompts mentioning ``Exception`` inside string values do not
# qualify. Token-bounded class-name + ``:`` or ``(`` lookahead requires
# real error-log shape, not just word mention.
#
# Single source of truth for both _extract_error_signature (pulls a line
# out of a log blob) and _looks_like_error (predicate on a single text).
# Both keep benign heartbeats (Cloning/Rebasing/Using tool/PROVISION_PROGRESS)
# out and structured-data lines (JSON objects starting with ``{``) out.
_ERROR_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bracketed log-level prefix: [ERROR], [FATAL]
    re.compile(r'^\s*\[(error|fatal)\]', re.I),
    # ``Error:`` / ``FAIL:`` / ``FAILED:`` at line start
    re.compile(r'^\s*(error|fail(ed)?)\s*:', re.I),
    # Python tracebacks always start with this
    re.compile(r'^\s*traceback\b', re.I),
    # Filesystem / lookup negatives at line start
    re.compile(r'^\s*(no such|cannot)\s', re.I),
    # Python exception classes at line start (optionally preceded by
    # ``raise``), followed by ``:`` or ``(`` to require structural
    # error-log shape rather than just a word mention.
    re.compile(
        r'^\s*(?:raise\s+)?('
        r'AssertionError|TypeError|ValueError|KeyError|AttributeError|'
        r'NameError|IndexError|RuntimeError|ImportError|ModuleNotFoundError|'
        r'FileNotFoundError|PermissionError|ConnectionError|HTTPError|'
        r'IntegrityError|NotImplementedError|StopIteration|MemoryError|'
        r'RecursionError|SystemError|OSError|IOError|LookupError|'
        r'ArithmeticError|OverflowError|ZeroDivisionError|ReferenceError|'
        r'UnicodeError|UnicodeDecodeError|UnicodeEncodeError|EOFError|'
        r'UnboundLocalError|SyntaxError|IndentationError|EnvironmentError|'
        r'Exception'
        r')\s*[:(]',
    ),
)


def _looks_like_error(text: str) -> bool:
    """True when ``text`` is structurally shaped like an error log line.

    Strict: must match an `_ERROR_LINE_PATTERNS` pattern at line-start.
    Lines that begin with ``{`` (serialized JSON) are rejected outright
    — structured data with error keywords mentioned inside string values
    is not an error log.
    """
    if not text:
        return False
    s = text.strip()
    if not s or s[0] == '{':
        return False
    return any(p.search(s) for p in _ERROR_LINE_PATTERNS)


def _extract_error_signature(logs: str | None) -> str:
    """Pull the most fingerprint-able line out of an error log blob.

    Prefers lines matching typical error shapes (Error/Exception/Traceback/
    assertion/FAIL). Returns the first such line found, trimmed. Empty
    string when logs are missing or have no recognizable error line —
    callers must NOT record a fingerprint in that case (no fallback
    to latest_status_message: the message is usually a benign agent
    heartbeat that pollutes the residual store with success-shape
    inputs).
    """
    if not logs:
        return ''
    for line in logs.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_error(stripped):
            return stripped[:500]
    return ''


def _fingerprint_message(msg: str) -> str:
    """Normalize a status/error message into a grouping fingerprint.

    Strips memory/size/duration tokens, UUIDs, hex hashes, and standalone
    numbers so near-duplicate messages (same error, different task IDs,
    different memory readings, different line numbers) cluster into a
    single fingerprint instead of spawning redundant remediations.
    """
    s = (msg or "").lower().strip()
    s = _RE_MEM_PREFIX.sub('', s)
    s = _RE_SIZE_TOKEN.sub('<size>', s)
    s = _RE_UUID.sub('<id>', s)
    s = _RE_HEX.sub('<hash>', s)
    s = _RE_NUM.sub('<n>', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:200]


def record_residual_fingerprint(task: "Task", logs: str | None = None) -> None:
    """Record a CODE_ERROR fallthrough in the residual store.

    Two strict gates keep the store free of false positives that pollute
    auto-remediation spawn decisions:

    1. ``task.status == FAILED``. ``classify_failure`` is called from
       ~20 sites — analytics, anomaly detection, direction outcomes,
       admin API responses — many of which run on COMPLETED/CANCELLED
       tasks for read-only classification. Recording fingerprints on
       non-FAILED tasks pollutes the store with success-shape strings
       (rollup messages, plan output JSON, review approvals).

    2. ``logs`` must contain an error-shaped line (Traceback / Error: /
       Python exception class / [ERROR] / FAIL: / no such / cannot).
       No fallback to ``latest_status_message`` — that's almost always
       a benign heartbeat (``[10.5MB] Cloning…``, ``Using tool: Bash``)
       that just happened to be current when classification ran.

    The _residual_seen_task_ids dedup set prevents double-counting when
    classify_failure_taxonomy() calls classify_failure() internally.
    """
    # Gate 1: only FAILED tasks contribute residual fingerprints.
    from app.models import TaskStatus  # noqa: PLC0415 — lazy to avoid circular import
    task_status = getattr(task, 'status', None)
    if task_status != TaskStatus.FAILED:
        return
    task_id = str(getattr(task, 'id', '') or '')
    if not task_id or task_id in _residual_seen_task_ids:
        return
    # Gate 2: must have an error-shaped line in logs. No status_msg fallback.
    err_line = _extract_error_signature(logs)
    if not err_line:
        return
    # Both gates passed — claim the dedup slot now.
    if len(_residual_seen_task_ids) < _RESIDUAL_SEEN_MAX:
        _residual_seen_task_ids.add(task_id)
    status_msg = task.latest_status_message or ''
    fingerprint_source = err_line
    # Always error-shaped at this point (Gate 2 guarantees err_line is
    # non-empty + structurally matched). Field retained for backward
    # compat with autonomous_research's threshold gate; it now no-ops.
    is_error_shaped = True
    fp = _fingerprint_message(fingerprint_source) or '<empty>'
    now = time.time()
    trimmed_sample = fingerprint_source[:500]
    trimmed_status = status_msg[:500]
    # If this fingerprint had a closed case, reactivate it — the error is back.
    _reactivate_residual_case(fp)

    if fp in _residual_store:
        entry = _residual_store[fp]
        entry['count'] += 1
        entry['last_seen'] = now
        # Sticky-true: a cluster counts as error-shaped if any
        # contribution had error-shaped logs/status. Once true, keep
        # true so later heartbeat-only contributions don't downgrade
        # the cluster's threshold.
        if is_error_shaped:
            entry['is_error_shaped'] = True
        if task_id not in entry['task_ids'] and len(entry['task_ids']) < 5:
            entry['task_ids'].append(task_id)
        # Accumulate distinct samples across tasks that share this
        # fingerprint. The fingerprint is a normalized clustering key —
        # individual task error lines can still vary (line numbers,
        # param values, adjacent context) and each variation is useful
        # signal for the remediation agent. Cap at 5 distinct entries.
        samples = entry.setdefault('sample_messages', [entry.get('sample_message', '')])
        if trimmed_sample and trimmed_sample not in samples and len(samples) < 5:
            samples.append(trimmed_sample)
        statuses = entry.setdefault('sample_statuses', [entry.get('sample_status', '')])
        if trimmed_status and trimmed_status not in statuses and len(statuses) < 5:
            statuses.append(trimmed_status)
    else:
        if len(_residual_store) >= _RESIDUAL_MAX_FINGERPRINTS:
            oldest = min(_residual_store, key=lambda k: _residual_store[k]['last_seen'])
            del _residual_store[oldest]
        _residual_store[fp] = {
            'count': 1,
            'first_seen': now,
            'last_seen': now,
            'is_error_shaped': is_error_shaped,
            # First/primary samples — kept for backward-compat with code
            # that reads `sample_message`/`sample_status`.
            'sample_message': trimmed_sample,
            'sample_status': trimmed_status,
            # Distinct-sample lists (up to 5 each) built as additional
            # tasks hit this fingerprint. Richer context for remediation.
            'sample_messages': [trimmed_sample] if trimmed_sample else [],
            'sample_statuses': [trimmed_status] if trimmed_status else [],
            'task_ids': [task_id],
        }

    # Persist to DB for durability across restarts.
    project_id = getattr(task, 'project_id', None)
    if project_id is not None:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        _schedule_residual_db_upsert(project_id, fp, now_dt, trimmed_sample or trimmed_status)


async def _upsert_residual_case_async(
    project_id,
    fingerprint: str,
    now_dt: datetime,
    sample_message: str,
) -> None:
    """Upsert a ResidualRemediationCase row for durability across restarts.

    - New fingerprint → INSERT ACTIVE row, hit_count=1.
    - Existing ACTIVE / CLOSED_ORGANICALLY → bump hit_count + last_seen_at;
      reopen CLOSED_ORGANICALLY to ACTIVE.
    - IGNORED / RESOLVED → bump hit_count + last_seen_at; leave status alone.
    IntegrityError (concurrent INSERT race) is handled by retrying as an UPDATE.
    """
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import ResidualRemediationCase, ResidualCaseStatus  # noqa: PLC0415
    from sqlalchemy import update as sa_update  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415
    from sqlmodel import select  # noqa: PLC0415

    async def _atomic_increment(session, row_id, reopen: bool) -> None:
        """Increment hit_count atomically at the SQL layer (no read-modify-write)."""
        vals: dict = {
            "hit_count": ResidualRemediationCase.hit_count + 1,
            "last_seen_at": now_dt,
            "updated_at": now_dt,
        }
        if reopen:
            vals["status"] = ResidualCaseStatus.ACTIVE
        await session.execute(
            sa_update(ResidualRemediationCase)
            .where(ResidualRemediationCase.id == row_id)
            .values(**vals)
        )

    async def _apply_update(session) -> bool:
        """Return True if the row was processed; False if it was held by another worker.

        Phase A1: skip_locked=True so a contended row doesn't queue behind the
        30s lock_timeout. The next residual hit (or the in-memory cache flush
        on next failure) re-tries the upsert — losing one increment is far
        cheaper than a 500-quarantined orchestrator instance.
        """
        # SELECT FOR UPDATE acquires a row lock on Postgres, preventing
        # concurrent transactions from racing on hit_count.
        stmt = (
            select(ResidualRemediationCase)
            .where(
                ResidualRemediationCase.project_id == project_id,
                ResidualRemediationCase.fingerprint == fingerprint,
            )
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        existing = result.scalars().first()
        # If the SELECT returned nothing, distinguish "row doesn't exist" from
        # "row held under skip_locked" with a non-locking probe.
        if existing is None:
            probe = (await session.execute(
                select(ResidualRemediationCase.id).where(
                    ResidualRemediationCase.project_id == project_id,
                    ResidualRemediationCase.fingerprint == fingerprint,
                )
            )).first()
            if probe is not None:
                # Row exists but is held — skip this upsert; next hit will retry.
                logger.info(
                    "[FAILURE_CLASS] residual upsert: row %r held by peer; skipping",
                    fingerprint,
                )
                return False
            # Genuinely missing — INSERT.
            session.add(ResidualRemediationCase(
                project_id=project_id,
                fingerprint=fingerprint,
                status=ResidualCaseStatus.ACTIVE,
                first_seen_at=now_dt,
                last_seen_at=now_dt,
                hit_count=1,
                sample_message=sample_message or None,
            ))
            return True
        await _atomic_increment(
            session,
            existing.id,
            reopen=existing.status == ResidualCaseStatus.CLOSED_ORGANICALLY,
        )
        return True

    try:
        async with AsyncSessionLocal() as session:
            try:
                async with session.begin():
                    await _apply_update(session)
            except IntegrityError:
                # Concurrent INSERT won the race; retry as a pure atomic UPDATE.
                async with session.begin():
                    stmt = (
                        select(ResidualRemediationCase)
                        .where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == fingerprint,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    existing = (await session.execute(stmt)).scalars().first()
                    if existing is not None:
                        await _atomic_increment(
                            session,
                            existing.id,
                            reopen=existing.status == ResidualCaseStatus.CLOSED_ORGANICALLY,
                        )
                    # If still None here, the contending writer is mid-INSERT;
                    # leave it — they'll commit and our next hit will see it.
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Residual case DB upsert failed (fingerprint=%r)", fingerprint
        )


def _schedule_residual_db_upsert(
    project_id,
    fingerprint: str,
    now_dt: datetime,
    sample_message: str,
) -> None:
    """Schedule a fire-and-forget DB upsert when an event loop is running."""
    import asyncio  # noqa: PLC0415
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _upsert_residual_case_async(project_id, fingerprint, now_dt, sample_message)
        )
    except RuntimeError:
        pass  # No running loop (e.g. tests); in-memory store is the only path


def get_top_residual_fingerprints(window_seconds: int = 86400, top_n: int = 10) -> list[dict]:
    """Return the top-N residual CODE_ERROR fingerprints within the time window.

    Each result dict contains: fingerprint, count, last_seen (epoch float),
    sample_message (error-line or status fallback), sample_status (original
    agent heartbeat at failure time — kept as human context even though it
    doesn't drive the fingerprint), task_ids (up to 5 examples).
    """
    cutoff = time.time() - window_seconds
    results = [
        {
            'fingerprint': fp,
            'count': entry['count'],
            'last_seen': entry['last_seen'],
            'is_error_shaped': entry.get('is_error_shaped', True),
            'sample_message': entry['sample_message'],
            'sample_status': entry.get('sample_status', ''),
            'sample_messages': entry.get('sample_messages', [entry['sample_message']]),
            'sample_statuses': entry.get('sample_statuses', [entry.get('sample_status', '')]),
            'task_ids': entry['task_ids'],
        }
        for fp, entry in _residual_store.items()
        if entry['last_seen'] >= cutoff
        and fp not in _residual_closed_fingerprints
    ]
    results.sort(key=lambda x: x['count'], reverse=True)
    return results[:top_n]


def record_account_failure_class(
    account_id: str,
    failure_class: str,
    confidence: float | None = None,
) -> None:
    """Record a failure-class event for an account in the rolling 24h window."""
    if not account_id:
        return
    now = time.time()
    cutoff = now - 86400
    records = _account_failure_log.setdefault(account_id, [])
    records.append((failure_class, confidence, now))
    _account_failure_log[account_id] = [rec for rec in records if rec[2] >= cutoff]


def get_account_failure_class_records(
    account_id: str,
    window_seconds: int = 86400,
) -> list[tuple[str, float | None, float]]:
    """Return raw per-failure-class records (class, confidence, timestamp)."""
    cutoff = time.time() - window_seconds
    records = _account_failure_log.get(account_id, [])
    return [rec for rec in records if rec[2] >= cutoff]


def get_account_failure_class_counts(account_id: str, window_seconds: int = 86400) -> dict[str, int]:
    """Return per-failure-class counts for an account within the time window."""
    records = get_account_failure_class_records(account_id, window_seconds=window_seconds)
    return {
        cls: sum(1 for failure_class, _confidence, _ts in records if failure_class == cls)
        for cls in ALL_FAILURE_CLASSES
    }


def classify_failure_with_confidence(task: "Task", logs: str | None = None) -> FailureClassification:
    """Classify a failure and return a cheap rule-derived confidence score."""
    failure_class = _classify_failure_class(task, logs)
    agent_category = (task.context or {}).get("failure_category")
    combined = ((task.latest_status_message or "").lower() + " " + (logs or "").lower()).strip()
    combined_lower = combined.lower()
    if agent_category:
        return FailureClassification(failure_class=failure_class, confidence=0.9)
    if failure_class == MIGRATION_ERROR:
        if any(kw in combined_lower for kw in _MIGRATION_PATTERNS):
            return FailureClassification(failure_class=failure_class, confidence=0.9)
        if "downgrade" in combined_lower and any(
            kw in combined_lower for kw in ("notimplementederror", "operationalerror")
        ):
            return FailureClassification(failure_class=failure_class, confidence=0.9)
        return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == RATE_LIMIT:
        if _task_rate_limit_signal(task, combined_lower) is not None:
            return FailureClassification(failure_class=failure_class, confidence=0.8)
        if any(kw in combined_lower for kw in _task_rate_limit_patterns(task)):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == PROVISION_FAILURE and _PROVISION_PHRASE_RE.search(combined):
        return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == EXTERNAL_API:
        if any(kw in combined_lower for kw in (
            "could not connect to server", "connection to server", "database connection",
            "psycopg", "asyncpg", "operator does not exist",
        )):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
        if _provider_model_config_rejection(combined_lower):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == AGENT_TIMEOUT and any(
        kw in combined_lower for kw in ("no heartbeat", "reaped", "never started", "cancelled:", "superseded")
    ):
        return FailureClassification(failure_class=failure_class, confidence=0.7)
    if failure_class == CODE_ERROR:
        return FailureClassification(failure_class=failure_class, confidence=0.5)
    return FailureClassification(failure_class=failure_class, confidence=0.7)


def _classify_failure_class(task: "Task", logs: str | None = None) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API"""

    # Fast path: agent already reported a structured category — prefer it over regex.
    # Accept both the legacy lowercase labels (pre-normalization writes) and the
    # canonical uppercase names written by normalize_agent_failure_category().
    agent_category = (task.context or {}).get('failure_category')
    if agent_category:
        agent_label = str(agent_category).strip()
        agent_lower = agent_label.lower()
        if agent_lower in _AGENT_CATEGORY_TO_CLASS:
            mapped = _AGENT_CATEGORY_TO_CLASS[agent_lower]
            if mapped == CODE_ERROR and agent_lower in _GENERIC_AGENT_CODE_LABELS:
                combined = (task.latest_status_message or "").lower() + " " + (logs or "").lower()
                override = _text_provider_failure_override(task, combined)
                if override:
                    return override
            return mapped
        if agent_category in INFRA_CLASSES or agent_category == CODE_ERROR:
            return agent_category
        # Orchestrator-internal: task auto-failed after too many retry_after cycles
        # because all accounts were rate-limited. Root cause is RATE_LIMIT.
        if agent_category == "retry_after_exhausted":
            return RATE_LIMIT

    status_msg = (task.latest_status_message or "").lower()
    logs_lower = (logs or "").lower()
    combined = status_msg + " " + logs_lower

    # Empty/whitespace-only combined log — agent crashed or never communicated.
    # Classify as AGENT_TIMEOUT (infra) rather than falling through to CODE_ERROR.
    if not combined.strip():
        return AGENT_TIMEOUT

    # Migration-specific failures — alembic chain/head errors, missing migration
    # column drift, schema-revision mismatches, downgrade failures. Checked before
    # the database-connection EXTERNAL_API bank because UndefinedColumn errors
    # surface via psycopg/asyncpg but indicate a missing migration, not a DB outage.
    if any(kw in combined for kw in _MIGRATION_PATTERNS):
        return MIGRATION_ERROR
    if "downgrade" in combined and any(
        kw in combined for kw in ("notimplementederror", "operationalerror")
    ):
        return MIGRATION_ERROR

    # Database / internal service unavailable — checked before provision failures because
    # PostgreSQL errors like "could not connect to server: Connection refused" contain
    # "connection refused" which would otherwise be misclassified as PROVISION_FAILURE.
    if any(kw in combined for kw in ["could not connect to server", "connection to server",
                                      "database connection", "psycopg", "asyncpg",
                                      "operator does not exist"]):
        return "EXTERNAL_API"

    # Provision failures — GCP quota, network, VM startup issues, SSH/init script errors, disk exhaustion.
    # "provision" is matched via _PROVISION_PHRASE_RE so benign mentions like
    # "No heartbeat received after 300s since provisioning" (heartbeat timeout, not
    # provision failure) don't get misclassified.
    #
    # NOTE: Bare "quota" used to be in this bank, but it's far too greedy —
    # Gemini/OpenAI/Anthropic quota-exhausted errors also contain "quota"
    # and were being misclassified as PROVISION_FAILURE, which bypasses
    # the rate-limit requeue path in agent_status.py (RATE_LIMIT/
    # EXTERNAL_API get no-retry-burn requeue; PROVISION_FAILURE burns
    # attempt_count). Result: a Gemini daily-quota hit would eat all 3
    # retries in one sitting. Rely on the specific GCP quota keywords
    # (cpus_all_regions, in_use_addresses) and `compute.googleapis.com`
    # for actual provisioning-quota errors; LLM-provider quota errors
    # fall through to the rate-limit signal detection below.
    if _PROVISION_PHRASE_RE.search(combined) or any(
        kw in combined for kw in [
            "cpus_all_regions", "in_use_addresses",
            "vm failed to start",
            "connection refused", "connection reset by peer",
            "timed out waiting for operation", "sshd", "startup script",
            "no space left on device", "disk quota exceeded",
            "filesystem is full",
            "address already in use", "failed to connect",
            "service is temporarily unavailable", "compute.googleapis.com",
            "resource not found", "container killed",
        ]
    ):
        return PROVISION_FAILURE

    # Anthropic 529 overload — server-side capacity, NOT account-specific. Must
    # requeue transiently but MUST NOT disable the account.
    if "overloaded" in combined:
        return "EXTERNAL_API"

    # GitHub rate limits — external API issue, NOT Claude account exhaustion.
    # RATE_LIMIT_PATTERNS is now specific enough that a plain "API rate limit
    # exceeded" from GitHub can't match it, but we still route GitHub errors to
    # EXTERNAL_API so they don't fall through to CODE_ERROR.
    if any(gh_kw in combined for gh_kw in ["github", "api.github.com"]) and \
       any(kw in combined for kw in ["rate limit", "429", "too many requests"]):
        return "EXTERNAL_API"

    # Claude API rate limit — exact Claude Code / Anthropic API error strings only.
    # See RATE_LIMIT_PATTERNS docstring for why broad keywords were removed.
    # Prefer structured signal (provider + kind + retry-hint) when resolvable;
    # fall back to legacy pattern-list scan for tasks without provider context.
    signal = _task_rate_limit_signal(task, combined)
    if signal is not None:
        _persist_rate_limit_signal_context(task, signal)
        return "RATE_LIMIT"
    rate_limit_patterns = _task_rate_limit_patterns(task)
    if any(kw in combined for kw in rate_limit_patterns):
        return "RATE_LIMIT"

    # Pipeline-internal rate-limit status messages — backend-generated strings written by
    # the orchestrator and reaper on tasks held or requeued due to provider account
    # exhaustion. These contain "rate limit" or "rate-limit" but are not from GitHub
    # (already caught above) and don't match provider-specific API error patterns because
    # they're internal status strings, not API responses. Classify as RATE_LIMIT so they
    # don't fall through to CODE_ERROR and create spurious auto-remediation fingerprints.
    # Examples:
    #   "Rate limited — requeued, waiting for account cooldown"
    #   "All accounts rate-limited — waiting for cooldown"
    #   "Auto-failed after N retry_after cycles; Pool state: all accounts in rate-limit cooldown"
    if any(rl in combined for rl in ("rate limited", "rate-limit")) and \
            any(pl in combined for pl in ("requeued", "cooldown")):
        return RATE_LIMIT

    # Workflow limits — iteration caps, spawn failures due to pipeline state (not code errors).
    # Checked before merge-blocking EXTERNAL_API so a message like "merge blocked, fix not spawned
    # (iteration cap)" is correctly classified as AGENT_TIMEOUT rather than EXTERNAL_API.
    if any(kw in combined for kw in ["iteration cap", "review not spawned", "fix not spawned",
                                      "review-fix cap reached", "review-fix cycle limit",
                                      "ci wait exceeded", "ci wait timed out", "ci in progress",
                                      "stuck in ci-wait",
                                      "backstop timeout", "evaluation timeout"]):
        return "AGENT_TIMEOUT"

    # Merge-blocking GitHub responses — branch protection, rule violations, not-mergeable states.
    # These are GitHub operation blocks, not agent code defects.
    if any(kw in combined for kw in ["merge blocked", "merge attempt", "merge conflicts",
                                      "merge failed", "merge hard-cap", "merge hard cap",
                                      "merge check failed", "not mergeable",
                                      "repository rule", "rule violations",
                                      "required status check", "branch protection",
                                      "closed without merging"]):
        return "EXTERNAL_API"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors.
    # Also catches branch creation failures (GitHub API errors from _ensure_branch).
    # Provider model-config rejections (model-not-found, unsupported service_tier,
    # unsupported-auth-for-model) land here too: they're upstream 400s on the
    # provider's model surface, not code defects, so we don't want them
    # penalizing direction health or triggering code-defect investigations.
    # The OpenAI/Gemini adapters gate known-bad combinations at settings_to_cli
    # time, but the classifier is the last line of defense if a new
    # incompatibility lands before the gate list is updated.
    if _provider_model_config_rejection(combined):
        return "EXTERNAL_API"
    if ("failed to create" in combined and "branch" in combined) or \
       any(kw in combined for kw in ["api.github.com", "api.anthropic.com",
                                      "500 internal server error",
                                      "502 bad gateway", "503 service unavailable",
                                      "github api error", "claude api",
                                      "connection timed out", "name resolution failed",
                                      "ssl", "certificate",
                                      "context_length_exceeded", "token limit exceeded",
                                      "could not resolve hostname", "name or service not known",
                                      "network unreachable",
                                      "failed to create pull request",
                                      "rollup pr creation failed",
                                      "staging branch",
                                      "401", "unauthorized", "bad credentials",
                                      "token expired", "token invalid", "token revoked"]):
        return "EXTERNAL_API"

    # Agent timeout — reaped by heartbeat, execution timeout, process killed by OS/orchestrator, OOM.
    # "[fatal:" matches agent _crashReport() labels: [FATAL:uncaughtException], [FATAL:unhandledRejection], etc.
    # NOTE: "timed out" and "killed" intentionally omitted — too broad and cause misclassification.
    # Only use specific forms: "execution timed out", "node process killed", "oom killer".
    # "creating pull request for blueprint/" is the agent's heartbeat during PR push — never an error
    # message. Tasks that die in this state (OOM, SIGKILL) leave this stale heartbeat as
    # latest_status_message; they must not create CODE_ERROR fingerprints in the residual store.
    # "using tool: " is the agent's heartbeat during a Claude Code tool call (Grep, Read, Edit, etc.).
    # Tasks reaped mid-tool-call (VM kill, network drop, OOM of the Claude subprocess) leave this
    # as latest_status_message; they are infrastructure deaths, not code defects.
    if any(kw in combined for kw in ["no heartbeat", "reaped", "never started",
                                      "execution timed out", "sigterm",
                                      "out of memory", "oom-kill", "memory limit exceeded",
                                      "cannot allocate memory",
                                      "exit code 137", "exit code 143",
                                      "exit code 124", "exit code 139",
                                      "node process killed",
                                      "oom killer", "killed by", "cgroup",
                                      "[fatal:", "fatal error", "process crashed",
                                      "creating pull request for blueprint/",
                                      "using tool: "]):
        return "AGENT_TIMEOUT"

    # Orchestration constraints — dependency waits are not code defects
    if any(kw in combined for kw in ["waiting on", "dependencies cleared",
                                      "serializing migration"]):
        return "AGENT_TIMEOUT"

    # Cascade cancellations — any task cancelled by the orchestrator/services is an infrastructure
    # decision, not a code defect. Matches: "Cancelled: dependency task failed",
    # "Cancelled: parent dependency failed", "Cancelled: parent evaluation timeout", etc.
    if combined.startswith("cancelled:"):
        return "AGENT_TIMEOUT"

    # System-initiated supersession — review pipeline cancels stale tasks with
    # "Superseded by fix on PR #N: ...". Same family as "cancelled:" above.
    if "superseded" in combined:
        return AGENT_TIMEOUT

    # Default: code error — record fingerprint for residual-pattern analysis.
    # Pass `logs` so the fingerprint keys on the actual error signature
    # (Traceback/Error/FAIL line) when available, not on whatever agent
    # heartbeat happened to be in latest_status_message.
    record_residual_fingerprint(task, logs=logs)
    return "CODE_ERROR"


def classify_failure(task: "Task", logs: str | None = None) -> str:
    """Backward-compatible shim that returns only the failure class string."""
    return classify_failure_with_confidence(task, logs).failure_class
