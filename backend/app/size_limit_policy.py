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

from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict


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


# ---------------------------------------------------------------------------
# Chronic-offender governance
# ---------------------------------------------------------------------------

# A module is a chronic offender when it has been bumped at least this many
# times in the rolling window, OR its cap has grown by at least this fraction
# since the first recorded bump.
CHRONIC_BUMP_THRESHOLD: int = 3
CHRONIC_WINDOW_DAYS: int = 180
CHRONIC_GROWTH_PCT: float = 0.30


class BumpRecord(TypedDict):
    commit_sha: str
    commit_date: str  # ISO-8601
    old_cap: int
    new_cap: int


class BumpHistoryUnavailableError(Exception):
    """Raised when cap-bump history could not be read.

    Distinct from an empty history: ``[]`` means the git read SUCCEEDED and
    found no cap bumps for the key, while this exception means the read itself
    failed (subprocess timeout, git not available, non-repo environment, or a
    non-zero ``git log`` exit — e.g. a truncated/shallow task-VM clone). Callers
    that gate a cap bump on the chronic-offender verdict must treat this state
    as fail-closed (refuse the bump) rather than as "no bump history".
    """


# Module-level TTL cache: (module_key, section) -> (outcome, monotonic_time)
# where ``outcome`` is either a ``list[BumpRecord]`` (read succeeded) or a
# ``BumpHistoryUnavailableError`` (read failed). Failures are cached too so a
# broken-git reconcile cycle does not re-shell (and re-timeout) per call.
_bump_history_cache: dict[
    tuple[str, str], tuple[list[BumpRecord] | BumpHistoryUnavailableError, float]
] = {}
_CACHE_TTL_SECONDS: float = 60.0

# Repo root: backend/app/ -> ../../
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MODULE_SIZE_LIMITS_RELPATH = "backend/testing/module_size_limits.json"


def parse_bump_history(module_key: str, section: str = "backend") -> list[BumpRecord]:
    """Return the cap-bump history for ``module_key`` from git log.

    Reads ``backend/testing/module_size_limits.json``'s git history, extracts
    diff hunks commit-by-commit, and returns records where the given key's
    cap value increased (old_cap -> new_cap).  Results are sorted oldest-first.

    Cached for ``_CACHE_TTL_SECONDS`` so repeated calls in a single reconcile
    cycle do not re-shell.  Returns ``[]`` ONLY when the git read succeeded and
    contains no cap bumps for the key — a genuinely empty history.  Raises
    :class:`BumpHistoryUnavailableError` when the history could not be read at
    all (subprocess timeout, git not available, non-repo environment, or a
    non-zero ``git log`` exit).  Callers that gate the chronic-offender verdict
    on this history must distinguish "no bumps on record" from "history could
    not be read" instead of treating both as empty.
    """
    cache_key = (module_key, section)
    now = time.monotonic()
    cached = _bump_history_cache.get(cache_key)
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        outcome = cached[0]
        if isinstance(outcome, BumpHistoryUnavailableError):
            raise outcome
        return outcome

    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--format=COMMIT %H %aI",
                "-p",
                "--",
                _MODULE_SIZE_LIMITS_RELPATH,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=_REPO_ROOT,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as BumpHistoryUnavailableError
        err = BumpHistoryUnavailableError(
            f"git log failed while reading bump history for {module_key!r}"
        )
        err.__cause__ = exc
        _bump_history_cache[cache_key] = (err, now)
        raise err
    if result.returncode != 0:
        err = BumpHistoryUnavailableError(
            f"git log exited {result.returncode} while reading bump history "
            f"for {module_key!r}"
        )
        _bump_history_cache[cache_key] = (err, now)
        raise err

    records = _parse_git_diff_output(result.stdout, module_key)
    _bump_history_cache[cache_key] = (records, now)
    return records


def _parse_git_diff_output(output: str, module_key: str) -> list[BumpRecord]:
    """Parse ``git log -p`` output for cap increases of ``module_key``.

    Splits the output on "COMMIT <sha> <date>" header lines, then within each
    commit's diff looks for ``-    "<key>": <int>`` (removed) and
    ``+    "<key>": <int>`` (added) lines.  Records only increases
    (new_cap > old_cap) — a decrease or reset is not a bump.
    """
    records: list[BumpRecord] = []
    # Matches +/- diff lines containing the key and a numeric cap value.
    cap_re = re.compile(rf'^([+-])\s+"{re.escape(module_key)}":\s*(\d+)', re.MULTILINE)

    # Accumulate (sha, date, diff_text) for each commit.
    chunks: list[tuple[str, str, str]] = []
    current_sha = ""
    current_date = ""
    current_lines: list[str] = []

    for line in output.split("\n"):
        if line.startswith("COMMIT "):
            if current_sha:
                chunks.append((current_sha, current_date, "\n".join(current_lines)))
            parts = line.split(" ", 2)
            current_sha = parts[1] if len(parts) > 1 else ""
            current_date = parts[2].strip() if len(parts) > 2 else ""
            current_lines = []
        else:
            current_lines.append(line)

    if current_sha:
        chunks.append((current_sha, current_date, "\n".join(current_lines)))

    for sha, date, diff_text in chunks:
        old_cap: int | None = None
        new_cap: int | None = None
        for m in cap_re.finditer(diff_text):
            sign, val_str = m.group(1), m.group(2)
            val = int(val_str)
            if sign == "-":
                old_cap = val
            elif sign == "+":
                new_cap = val
        if old_cap is not None and new_cap is not None and new_cap > old_cap:
            records.append(
                BumpRecord(
                    commit_sha=sha,
                    commit_date=date,
                    old_cap=old_cap,
                    new_cap=new_cap,
                )
            )

    records.sort(key=lambda r: r["commit_date"])
    return records


def _is_chronic_offender(history: list[BumpRecord]) -> bool:
    """Return True when ``history`` meets the chronic-offender threshold.

    Two triggers — either fires the classification:

    1. **Window check**: at least ``CHRONIC_BUMP_THRESHOLD`` bumps within the
       last ``CHRONIC_WINDOW_DAYS`` days.
    2. **Cumulative-growth check**: total cap growth from the first recorded
       ``old_cap`` to the last recorded ``new_cap`` is >= ``CHRONIC_GROWTH_PCT``
       AND there are at least ``CHRONIC_BUMP_THRESHOLD`` bumps on record
       (avoids false-positives from a single jump on a fresh file).
    """
    if len(history) < CHRONIC_BUMP_THRESHOLD:
        return False

    # Trigger 1 — window check.
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHRONIC_WINDOW_DAYS)
    recent_count = sum(1 for r in history if _parse_iso(r["commit_date"]) >= cutoff)
    if recent_count >= CHRONIC_BUMP_THRESHOLD:
        return True

    # Trigger 2 — cumulative growth check.
    first_cap = history[0]["old_cap"]
    current_cap = history[-1]["new_cap"]
    return first_cap > 0 and (current_cap - first_cap) / first_cap >= CHRONIC_GROWTH_PCT


class BreachVerdict(TypedDict):
    is_chronic: bool
    suggested_new_limit: int | None  # None when chronic and no operator override
    bump_count: int
    reason: str
    history_unavailable: bool  # True when bump history could not be read


def classify_breach(
    module_key: str,
    current_size: int,
    section: str = "backend",
    *,
    override_chronic: bool = False,
) -> BreachVerdict:
    """Classify a size breach as routine or chronic-offender escalation.

    Returns a ``BreachVerdict`` with ``is_chronic=True`` and
    ``suggested_new_limit=None`` for chronic offenders (unless
    ``override_chronic=True``, the explicit operator-approval path, which
    grants the bump but records attribution).

    When bump history could not be read (truncated task-VM clone, git
    unavailable), the verdict FAILS CLOSED: ``is_chronic=True`` with
    ``suggested_new_limit=None`` and ``history_unavailable=True``, routing the
    breach to decomposition rather than silently granting a routine cap bump.
    A genuinely readable but empty history keeps the routine-bump behaviour —
    the guard distinguishes "no bumps on record" from "history could not be
    read" instead of treating both as empty.
    """
    try:
        history = parse_bump_history(module_key, section)
    except BumpHistoryUnavailableError as exc:
        # Unreadable history is NOT "no bumps on record": refuse the automatic
        # bump and route to decomposition. An explicit operator override
        # (override_chronic=True) still grants the bump — it is the documented
        # human-approval escape hatch, identical to a verified chronic offender.
        if override_chronic:
            return BreachVerdict(
                is_chronic=True,
                suggested_new_limit=bump_size_limit(current_size),
                bump_count=0,
                history_unavailable=True,
                reason=(
                    f"Bump history could not be read: {exc}. "
                    "Operator-approved bump granted despite unreadable history."
                ),
            )
        return BreachVerdict(
            is_chronic=True,
            suggested_new_limit=None,
            bump_count=0,
            history_unavailable=True,
            reason=(
                f"Bump history could not be read: {exc}. "
                "Refusing cap bump — decomposition required until history is readable."
            ),
        )

    chronic = _is_chronic_offender(history)
    bump_count = len(history)

    if chronic and not override_chronic:
        return BreachVerdict(
            is_chronic=True,
            suggested_new_limit=None,
            bump_count=bump_count,
            history_unavailable=False,
            reason=(
                f"Chronic offender: {bump_count} cap bumps on record. "
                "Decomposition required — do not bump without operator approval."
            ),
        )

    return BreachVerdict(
        is_chronic=chronic,
        suggested_new_limit=bump_size_limit(current_size),
        bump_count=bump_count,
        history_unavailable=False,
        reason=(
            "Chronic offender — operator-approved bump."
            if chronic
            else f"Routine breach ({bump_count} prior bumps)."
        ),
    )


def _parse_iso(date_str: str) -> datetime:
    """Parse an ISO-8601 datetime string to a tz-aware datetime.

    ``datetime.fromisoformat`` in Python 3.11+ handles the full ISO-8601
    format including offsets; older versions need a minor workaround for the
    ``+HH:MM`` suffix.
    """
    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        # Fallback: strip the timezone suffix and assume UTC.
        dt = datetime.fromisoformat(date_str[:19]).replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
