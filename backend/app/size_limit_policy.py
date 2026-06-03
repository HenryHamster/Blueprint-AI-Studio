"""Canonical module size-limit bump policy — single source of truth.

When a justified change pushes a module over its line-count cap in
``backend/testing/module_size_limits.json``, the cap is bumped by a fixed
policy (never agent judgment). That policy lives here so the two consumers
agree byte-for-byte:

* the server-side ``/api/agent/annotate`` size_breach endpoint
  (``app.routers.admin_agent_ops._bump_size_limit``), and
* the standalone checker script
  (``backend/testing/check_size_limits.py``).

Stdlib-only and dependency-free (no DB, no network, no third-party imports)
so the checker can import it without any install step or runtime context.
"""


def bump_size_limit(current_size: int) -> int:
    """Return the new cap for a file that breached its size limit.

    Policy: the larger of ``current_size + 200`` (a flat floor so a small
    overrun still gets reasonable headroom) and ``int(current_size * 1.15)``
    (15% proportional growth so a large file isn't left with tiny headroom),
    rounded UP to the nearest 50 lines for stability.
    """
    floor = current_size + 200
    proportional = int(current_size * 1.15)
    raw = max(floor, proportional)
    return ((raw + 49) // 50) * 50
