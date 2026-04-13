"""Task failure classification for deployment monitoring in Blueprint AI Studio."""
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.models import Task


def classify_failure(task: "Task", logs: Optional[str] = None) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API"""
    from app.max_plan_pool import RATE_LIMIT_KEYWORDS  # noqa: PLC0415

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

    # Transient API overload — server-side, not account-specific (must be checked before RATE_LIMIT
    # because "overloaded" would otherwise trigger the rate limit classifier and disable accounts)
    if "overloaded" in combined and not any(kw in combined for kw in ["usage limit", "usage exhausted", "billing", "credit"]):
        return "EXTERNAL_API"

    # GitHub rate limits (429, "rate limit") — external API issue, NOT Claude account exhaustion.
    # Must be checked before generic RATE_LIMIT to prevent disabling Claude accounts for GitHub outages.
    if any(gh_kw in combined for gh_kw in ["github", "api.github.com"]) and \
       any(kw in combined for kw in ["rate limit", "429", "too many requests"]):
        return "EXTERNAL_API"

    # Rate limit — Claude API usage exhausted, account-level billing/quota issues
    if any(kw in combined for kw in RATE_LIMIT_KEYWORDS):
        return "RATE_LIMIT"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors
    if any(kw in combined for kw in ["api.github.com", "api.anthropic.com",
                                      "502 bad gateway", "503 service unavailable",
                                      "github api error", "claude api",
                                      "connection timed out", "name resolution failed",
                                      "ssl", "certificate",
                                      "context_length_exceeded", "token limit exceeded",
                                      "could not resolve hostname", "name or service not known",
                                      "network unreachable",
                                      "failed to create pull request",
                                      "rollup pr creation failed",
                                      "staging branch"]):
        return "EXTERNAL_API"

    # Agent timeout — reaped by heartbeat, execution timeout, process killed by OS/orchestrator, OOM.
    # "[fatal:" matches agent _crashReport() labels: [FATAL:uncaughtException], [FATAL:unhandledRejection], etc.
    # NOTE: "timed out" and "killed" intentionally omitted — too broad and cause misclassification.
    # "non-zero exit code" and "process exited" are code errors, not timeouts.
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

    # Default: code error
    return "CODE_ERROR"
