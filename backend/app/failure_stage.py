"""Task-failure classification helpers.

Extracted from app.task_lifecycle to keep that module under its
line-count guardrail. Small, dependency-free utilities imported from
several lifecycle/reaper paths.
"""

_TRANSIENT_SCAN_ERROR_PATTERNS = (
    "timeout", "timed out", "connection", "network", "unreachable",
    "503", "502", "504", "service unavailable", "temporary", "dns",
    "ssl", "eof occurred", "reset by peer",
)


def _is_transient_scan_error(exc: Exception) -> bool:
    """Return True if the scan exception looks like a transient infrastructure failure."""
    msg = f"{type(exc).__name__} {exc!r}".lower()
    return any(pat in msg for pat in _TRANSIENT_SCAN_ERROR_PATTERNS)


def _derive_failure_stage(failure_class: str, status_message: str) -> "str | None":
    """Derive the pipeline stage at which a task failure occurred.

    Returns one of: 'provision', 'clone', 'test', 'lint', 'build', 'pr_creation', or None.
    """
    if failure_class == "PROVISION_FAILURE":
        return "provision"
    msg = (status_message or "").lower()
    if any(kw in msg for kw in ("checkout", "git clone", "clone")):
        return "clone"
    if any(kw in msg for kw in ("test", "assert", "pytest", "jest", "vitest")):
        return "test"
    if any(kw in msg for kw in ("lint", "ruff", "eslint", "tsc", "type error")):
        return "lint"
    if any(kw in msg for kw in ("build", "compile", "webpack", "bundle")):
        return "build"
    if any(kw in msg for kw in ("pull request", "create pr", "pr creation")):
        return "pr_creation"
    return None
