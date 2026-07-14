"""Shared GitHub HTTP response classification for the ``github_*`` helper modules.

These primitives are needed identically by every *mutating* GitHub helper
(create-PR, merge-PR, branch writes, …), which is why they live here rather than
in any one of them:

* ``app.github._github_request`` deliberately RETURNS the raw 403/429 for a HARD
  rate limit (a reset window past the fast-fail threshold) instead of sleeping
  through it.  So each mutating helper has to recognise that response shape
  itself and convert it into a *retryable* signal — otherwise a drained quota
  bucket is misreported as a hard failure.
* A GitHub 5xx is frequently an HTML error page from the edge, so a bare
  ``response.json()`` raises ``JSONDecodeError`` — an exception type no caller in
  the pipeline handles, so it escapes as an unclassified crash.

Keeping one implementation means a caller cannot forget either rule, and the two
call sites (``github_pulls.create_pull_request`` / ``merge_pull_request``) cannot
drift apart.
"""

import time

from fastapi import HTTPException, status

from app.github import logger

# Default backoff (seconds) advertised to the caller when we must refuse a
# mutation because GitHub rate-limited it but gave us no usable reset hint. Small
# enough to retry promptly once a transient blip clears; large enough not to
# hot-loop.
RATE_LIMIT_DEFAULT_RETRY_AFTER_S = 30

# Upper bound on the Retry-After we advertise. A drained 5000/hr install-token
# bucket can be up to ~1h from reset; telling an agent to sleep that long inside
# a synchronous ``./pr`` step would starve the reaper heartbeat budget on the VM
# and get it false-reaped. Cap it and let the caller re-poll after the window.
RATE_LIMIT_MAX_RETRY_AFTER_S = 120


def retry_after_seconds_from_response(resp) -> int | None:
    """Best-effort backoff (seconds) from a rate-limited GitHub response.

    Prefers an explicit ``Retry-After`` header (integer seconds — what GitHub
    sends for secondary limits, and what the local short-circuit synthesises from
    ``X-RateLimit-Reset``). Falls back to ``X-RateLimit-Reset`` (epoch seconds)
    minus now. Returns ``None`` when neither yields a positive value.
    """
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After")
    if raw is not None:
        try:
            secs = int(str(raw).strip())
        except (TypeError, ValueError):
            secs = 0
        if secs > 0:
            return secs
    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            delta = int(str(reset).strip()) - int(time.time())
        except (TypeError, ValueError):
            delta = 0
        if delta > 0:
            return delta
    return None


def clamped_retry_after(retry_after: int | None) -> int:
    """Clamp a derived backoff into the advertised [1, MAX] window."""
    delay = retry_after or RATE_LIMIT_DEFAULT_RETRY_AFTER_S
    return max(1, min(delay, RATE_LIMIT_MAX_RETRY_AFTER_S))


def is_rate_limited_response(response) -> bool:
    """True when a GitHub response is a rate-limit refusal, not a real error.

    Mirrors ``app.github._github_request``'s own classification: GitHub uses 429
    for secondary limits and 403 for primary (5000/hr install-token) exhaustion;
    secondary abuse limits send 403 + ``Retry-After`` WITHOUT
    ``X-RateLimit-Remaining: 0``.
    """
    headers = getattr(response, "headers", None) or {}
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True
    return status_code == 403 and (
        headers.get("X-RateLimit-Remaining") == "0"
        or headers.get("Retry-After") is not None
    )


def safe_json(response) -> dict:
    """Parse a GitHub response body, returning ``{}`` when it isn't a JSON object.

    A 5xx from GitHub's edge is frequently an HTML error page, and a bare
    ``response.json()`` on it raises ``JSONDecodeError``. Always parse the body
    BEHIND the status check and through this helper.
    """
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — unparseable body (HTML 5xx page, empty 204) is "no data"
        return {}
    return data if isinstance(data, dict) else {}


def raise_rate_limited(log_message: str, detail: str, retry_after: int | None) -> None:
    """Surface a rate-limited mutation as a retryable 503 (+ ``Retry-After``).

    503 — not 500 — is what keeps a caller's backoff loop engaged rather than
    treating the outcome as a hard failure. The mutation never reached GitHub's
    machinery (the quota bucket refused it), so it simply has to be re-attempted
    after the window resets.
    """
    delay = clamped_retry_after(retry_after)
    logger.warning(f"{log_message} (retry-after={delay}s)")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers={"Retry-After": str(delay)},
    )
