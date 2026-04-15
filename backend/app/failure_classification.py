"""Task failure classification for deployment monitoring in Blueprint AI Studio."""
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.models import Task


_AGENT_CATEGORY_TO_CLASS: dict[str, str] = {
    'clone_failed': 'PROVISION_FAILURE',
    'build_failed': 'CODE_ERROR',
    'test_failed': 'CODE_ERROR',
    'lint_failed': 'CODE_ERROR',
    'pr_creation_failed': 'EXTERNAL_API',
    'claude_code_failed': 'CODE_ERROR',
    'parse_failed': 'CODE_ERROR',
    'fatal': 'CODE_ERROR',
    'rate_limited': 'RATE_LIMIT',
}


def classify_failure(task: "Task", logs: Optional[str] = None) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API"""
    from app.max_plan_pool import RATE_LIMIT_PATTERNS  # noqa: PLC0415

    # Fast path: agent already reported a structured category — prefer it over regex.
    agent_category = (task.context or {}).get('failure_category')
    if agent_category and agent_category in _AGENT_CATEGORY_TO_CLASS:
        return _AGENT_CATEGORY_TO_CLASS[agent_category]

    status_msg = (task.latest_status_message or "").lower()
    logs_lower = (logs or "").lower()
    combined = status_msg + " " + logs_lower

    # Database / internal service unavailable — checked before provision failures because
    # PostgreSQL errors like "could not connect to server: Connection refused" contain
    # "connection refused" which would otherwise be misclassified as PROVISION_FAILURE.
    if any(kw in combined for kw in ["could not connect to server", "connection to server",
                                      "database connection", "psycopg", "asyncpg",
                                      "operator does not exist"]):
        return "EXTERNAL_API"

    # Provision failures — GCP quota, network, VM startup issues, SSH/init script errors, disk exhaustion
    if any(kw in combined for kw in ["provision", "quota", "cpus_all_regions", "in_use_addresses",
                                      "vm failed to start", "failed to provision",
                                      "connection refused", "connection reset by peer",
                                      "timed out waiting for operation", "sshd", "startup script",
                                      "no space left on device", "disk quota exceeded",
                                      "filesystem is full",
                                      "address already in use", "failed to connect",
                                      "service is temporarily unavailable", "compute.googleapis.com",
                                      "resource not found", "container killed"]):
        return "PROVISION_FAILURE"

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
                                      "stuck in ci-wait"]):
        return "AGENT_TIMEOUT"

    # Merge-blocking GitHub responses — branch protection, rule violations, not-mergeable states.
    # These are GitHub operation blocks, not agent code defects.
    if any(kw in combined for kw in ["merge blocked", "merge attempt", "merge conflicts",
                                      "merge check failed", "not mergeable",
                                      "repository rule", "rule violations",
                                      "required status check", "branch protection"]):
        return "EXTERNAL_API"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors
    if any(kw in combined for kw in ["api.github.com", "api.anthropic.com",
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

    # Default: code error
    return "CODE_ERROR"
