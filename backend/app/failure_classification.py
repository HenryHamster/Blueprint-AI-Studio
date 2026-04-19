"""Task failure classification for deployment monitoring in Blueprint AI Studio."""
import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.models import Task

logger = logging.getLogger(__name__)


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
    "scope too large", "complexity", "exceeds scope",
)


def classify_failure_taxonomy(task: "Task") -> str:
    """Return a 4-category taxonomy label for a failed task.

    Buckets CODE_ERROR failures into review_quality / scope_related / code_defect
    based on status-message signals and task context; all INFRA_CLASSES map to
    infra_related. Reuses classify_failure() for the base class.
    """
    base = classify_failure(task)
    if base in INFRA_CLASSES:
        return TAXONOMY_INFRA_RELATED

    status_msg = (task.latest_status_message or "").lower()
    ctx = task.context or {}

    if ctx.get("failure_detail") == "review_cap_exhausted":
        return TAXONOMY_REVIEW_QUALITY

    if any(kw in status_msg for kw in _REVIEW_QUALITY_KEYWORDS):
        return TAXONOMY_REVIEW_QUALITY

    expansion_ratio = ctx.get("expansion_ratio")
    if (
        ctx.get("scope_calibrated_from")
        or any(kw in status_msg for kw in _SCOPE_KEYWORDS)
        or (isinstance(expansion_ratio, (int, float)) and expansion_ratio > 1.5)
    ):
        return TAXONOMY_SCOPE_RELATED

    return TAXONOMY_CODE_DEFECT


_AGENT_CATEGORY_TO_CLASS: dict[str, str] = {
    'clone_failed': PROVISION_FAILURE,
    'build_failed': CODE_ERROR,
    'test_failed': CODE_ERROR,
    'lint_failed': CODE_ERROR,
    'pr_creation_failed': EXTERNAL_API,
    'claude_code_failed': CODE_ERROR,
    'parse_failed': CODE_ERROR,
    'fatal': CODE_ERROR,
    'rate_limited': RATE_LIMIT,
    'scope_overflow': SCOPE_OVERFLOW,
    'timeout': AGENT_TIMEOUT,
}


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


def persist_failure_category(task: "Task", failure_class: str) -> None:
    """Write ``failure_class`` onto ``task.context['failure_category']`` in place.

    All terminal-FAILED code paths must call this (or write the key themselves)
    so direction rollups and research dedup can tell infra from code failures.
    """
    ctx = dict(task.context or {})
    ctx["failure_category"] = failure_class
    task.context = ctx


def normalize_agent_failure_category(raw: Optional[str], task: "Task") -> str:
    """Canonicalize an agent-reported ``failure_category`` label to an uppercase class name.

    The agent posts lowercase short labels (``rate_limited``, ``claude_code_failed``, …)
    but every consumer (rollups, dedup, analytics) compares against the uppercase
    canonical names. Normalize at the write boundary so the rest of the system sees
    a single vocabulary.
    """
    if not raw:
        return classify_failure(task)
    label = raw.strip()
    mapped = _AGENT_CATEGORY_TO_CLASS.get(label.lower())
    if mapped:
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


def classify_timeout_subtype(task: "Task", logs: Optional[str] = None) -> str:
    """Return a sub-type label for an AGENT_TIMEOUT failure."""
    status_msg = task.latest_status_message or ""
    return _timeout_subtype_from_message(status_msg + " " + (logs or ""))


def classify_failure(task: "Task", logs: Optional[str] = None) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API"""
    from app.max_plan_pool import RATE_LIMIT_PATTERNS  # noqa: PLC0415

    # Fast path: agent already reported a structured category — prefer it over regex.
    # Accept both the legacy lowercase labels (pre-normalization writes) and the
    # canonical uppercase names written by normalize_agent_failure_category().
    agent_category = (task.context or {}).get('failure_category')
    if agent_category:
        if agent_category in _AGENT_CATEGORY_TO_CLASS:
            return _AGENT_CATEGORY_TO_CLASS[agent_category]
        if agent_category in INFRA_CLASSES or agent_category == CODE_ERROR:
            return agent_category

    status_msg = (task.latest_status_message or "").lower()
    logs_lower = (logs or "").lower()
    combined = status_msg + " " + logs_lower

    # Empty/whitespace-only combined log — agent crashed or never communicated.
    # Classify as AGENT_TIMEOUT (infra) rather than falling through to CODE_ERROR.
    if not combined.strip():
        return AGENT_TIMEOUT

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
    if _PROVISION_PHRASE_RE.search(combined) or any(
        kw in combined for kw in [
            "quota", "cpus_all_regions", "in_use_addresses",
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
    if any(kw in combined for kw in RATE_LIMIT_PATTERNS):
        return "RATE_LIMIT"

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
                                      "merge check failed", "not mergeable",
                                      "repository rule", "rule violations",
                                      "required status check", "branch protection",
                                      "closed without merging"]):
        return "EXTERNAL_API"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors.
    # Also catches branch creation failures (GitHub API errors from _ensure_branch).
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
    if any(kw in combined for kw in ["no heartbeat", "reaped", "never started",
                                      "execution timed out", "sigterm",
                                      "out of memory", "oom-kill", "memory limit exceeded",
                                      "cannot allocate memory",
                                      "exit code 137", "exit code 143",
                                      "exit code 124", "exit code 139",
                                      "node process killed",
                                      "oom killer", "killed by", "cgroup",
                                      "[fatal:", "fatal error", "process crashed"]):
        return "AGENT_TIMEOUT"

    # Orchestration constraints — dependency waits are not code defects
    if any(kw in combined for kw in ["waiting on", "dependencies cleared"]):
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

    # Default: code error
    return "CODE_ERROR"
