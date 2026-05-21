"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)

`cache_first_attempt_classification` is intended to be invoked at
first-review-rejection time by the lifecycle pipeline (sibling cluster wires it
in) so subsequent reads from admin_digest can skip the keyword scan.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime
from datetime import timedelta
from typing import Any, Literal, Optional

from app.shared_state import DEFAULT_TASK_SCOPE

from sqlalchemy import cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Alert,
    AlertSeverity,
    Task,
    Project,
    StrategicCycle,
    TaskStatus,
    _utcnow,
)
from app.sr_blind_spots import _REVIEWER_ORIGINS

# lone_checker_recovery is set by the fresh-reviewer pass when it runs because SR
# is the sole checker for a category.  Extend the reviewer-origin set locally so
# catch-rate attribution treats these findings as reviewer saves, not author SR hits.
_LONE_CHECKER_RECOVERY_ORIGIN = "lone_checker_recovery"
_ALL_REVIEWER_ORIGINS = _REVIEWER_ORIGINS | frozenset({_LONE_CHECKER_RECOVERY_ORIGIN})

# In-app Alert row title prefixes for SR catch-rate alerts. The router that
# powers AlertsBanner returns Alert rows verbatim; we use these prefixes both
# to format new rows and to find/dismiss prior matching rows on clear-transition.
_SR_REGRESSION_ALERT_TITLE_PREFIX = "Self-review catch-rate regression"
_SR_FLOOR_ALERT_TITLE_PREFIX = "Self-review catch-rate below floor"
_SR_QUADRANT_ALERT_TITLE_PREFIX = "Self-review precision/recall shift"
_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX = "PASS reasoning thinning"
# Cap how many regressing categories/severities we name in an Alert title
# before truncating; the full breakdown lives in `Alert.message`.
_SR_ALERT_TITLE_MAX_PARTS = 5


def _format_sr_alert_title(prefix: str, parts: list[str]) -> str:
    """Compose an Alert title from a prefix and a list of regressing identifiers."""
    if not parts:
        return prefix
    if len(parts) > _SR_ALERT_TITLE_MAX_PARTS:
        head = ", ".join(parts[:_SR_ALERT_TITLE_MAX_PARTS])
        joined = f"{head}, +{len(parts) - _SR_ALERT_TITLE_MAX_PARTS} more"
    else:
        joined = ", ".join(parts)
    return f"{prefix}: {joined}"


# ---------------------------------------------------------------------------
# Lifecycle constants
# ---------------------------------------------------------------------------

# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.  Leaves ~5 KB headroom below the 30 KB
# total limit.
_PLAYBOOK_CAPACITY_BYTES = 25 * 1024

# Cap on playbook_archive length. The archive exists for operator review of
# retired playbook entries; there is no functional reason to keep more than
# the most-recent N. Without this cap the archive grows monotonically
# (observed: 517 entries / 351 KB on a single project) and can bloat the
# design_doc past the agent payload budget. Configurable via runtime_config.
_PLAYBOOK_ARCHIVE_MAX_DEFAULT = 50

# Entries older than this (in days) with fewer than _STALE_CONFIRM_THRESHOLD
# confirmed uses are deprioritised during the claim sort.
_STALE_AGE_DAYS = 30
_STALE_CONFIRM_THRESHOLD = 3

# Minimum followed_count before success rate replaces raw confirmed_count in
# archival ranking and retirement decisions.
_MIN_FOLLOWED_FOR_RATE = 3

# Auto-retirement: entries followed ≥ _RETIRE_MIN_FOLLOWED times with a success
# rate below _RETIRE_MAX_SUCCESS_RATE are archived with a _retirement_reason tag.
_RETIRE_MIN_FOLLOWED = 5
_RETIRE_MAX_SUCCESS_RATE = 0.20

# Time-decay retirement for auto-generated playbook entries: entries whose
# last_referenced_at (or updated_at fallback) is older than _IDLE_RETIRE_DAYS
# get archived. Operator-curated entries (source != 'auto-generated') are
# EXEMPT — only entries with source == 'auto-generated' decay on idleness.
_IDLE_RETIRE_DAYS = 30

# Review-extracted playbook entries are refreshed in-place when their
# updated_at exceeds _REVIEW_PLAYBOOK_REFRESH_DAYS, and flagged
# potentially_stale once they exceed _REVIEW_PLAYBOOK_STALE_DAYS without
# being observed again.
_REVIEW_PLAYBOOK_REFRESH_DAYS = 30
_REVIEW_PLAYBOOK_STALE_DAYS = 60

# Execution-evidence SR check categories. These track whether the agent
# actually ran verification commands (pytest, lint, typecheck) and got exit 0.
# Miss rate = fraction of tasks where the agent reported passed: false.
_EXECUTION_EVIDENCE_CATEGORIES = frozenset(
    ["tests_executed_cleanly", "lint_executed_cleanly", "typecheck_executed_cleanly"]
)

# Regex patterns that match source-file paths embedded in playbook entry content.
# Used to detect references to files that may have been renamed or removed.
_FILE_PATH_RE = re.compile(
    r"\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b"
)

# ---------------------------------------------------------------------------
# Review feedback category keywords
# ---------------------------------------------------------------------------

_REVIEW_CATEGORIES: dict[str, list[str]] = {
    # NOTE: regression_risk MUST come first — _categorize_review_issues_keyword uses
    # first-match-wins, and the broader 'test' keyword in test-coverage and the
    # 'assertion'/'invariant' keywords in correctness would otherwise capture
    # regression-flavored phrases like "broke test_foo" or "previously-passing
    # assertion now fails". The key spelling matches the agent's self-review
    # check_results category in agent/src/prompt.ts (underscore form).
    "regression_risk": [
        "regress",
        "regression",
        "regresses",
        "regressed",
        "previously passing",
        "previously-passing",
        "was passing",
        "broke the test",
        "breaks the test",
        "broke test",
        "breaks test",
        "broken test",
        "broke a test",
        "breaks a test",
        "now failing",
        "no longer passes",
        "no longer works",
    ],
    "test-coverage": [
        "test",
        "coverage",
        "assert",
        "unit test",
        "integration test",
        "spec",
        "test case",
        "test suite",
        "missing test",
        "untested",
    ],
    "error-handling": [
        "error handling",
        "exception",
        "try/catch",
        "try except",
        "fallback",
        "guard",
        "null check",
        "none check",
        "unhandled",
        "raise",
    ],
    "security": [
        "security",
        "injection",
        "xss",
        "csrf",
        "sanitize",
        "validate input",
        "auth",
        "permission",
        "secret",
        "credential",
        "escape",
    ],
    "style-conventions": [
        "style",
        "format",
        "naming",
        "convention",
        "whitespace",
        "indentation",
        "pep8",
        "lint",
        "camelcase",
        "snake_case",
        "consistent",
    ],
    "type-safety": [
        "type hint",
        "type annotation",
        "typing",
        "typevar",
        "optional",
        "union",
        "cast",
        "mypy",
        "type error",
        "untyped",
    ],
    "documentation": [
        "docstring",
        "documentation",
        "comment",
        "readme",
        "inline comment",
        "missing doc",
        "undocumented",
    ],
    "performance": [
        "performance",
        "optimize",
        "cache",
        "slow",
        "n+1",
        "index",
        "memory",
        "efficiency",
        "bottleneck",
    ],
    "architecture": [
        "refactor",
        "separation of concerns",
        "coupling",
        "dependency",
        "abstraction",
        "pattern",
        "interface",
        "modularity",
        "cohesion",
    ],
    "correctness": [
        "logic",
        "bug",
        "incorrect",
        "wrong",
        "off-by-one",
        "return value",
        "edge case",
        "boundary",
        "race condition",
        "deadlock",
        "missing import",
        "undefined",
        "null reference",
        "assertion",
        "invariant",
        "constraint",
    ],
    "scope-drift": [
        "scope",
        "scope drift",
        "diff size",
        "too large",
        "too many files",
        "bloat",
        "beyond scope",
        "out of scope",
        "unrelated changes",
        "exceeds scope",
        "scope creep",
        "too broad",
        "unrelated files",
    ],
    "cross-system-contract": [
        "url mismatch",
        "route mismatch",
        "endpoint mismatch",
        "path mismatch",
        "dto mismatch",
        "cross-system",
        "cross system",
        "missing route",
        "missing endpoint",
        "api contract",
        "contract drift",
        "unmatched path",
        "frontend calls",
        "backend exposes",
    ],
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


def _extract_file_paths(content: str) -> list[str]:
    """Return unique source-file paths found in *content*.

    Matches paths of the form ``backend/app/...``, ``agent/src/...``,
    ``admin-ui/src/...``, etc.
    """
    return list(dict.fromkeys(_FILE_PATH_RE.findall(content)))


def check_and_flag_stale_refs(doc: dict, repo_root: str) -> dict:
    """Scan each playbook entry in *doc* for file-path references and set
    ``stale_file_refs: True`` on entries that reference files which no longer
    exist under *repo_root*.

    Only marks an entry stale when at least one referenced path is confirmed
    missing.  Entries with no file-path references are left untouched.
    The check is conservative: if *repo_root* is not a valid directory the
    function returns *doc* unchanged.

    Returns the (possibly mutated) doc dict.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return doc

    updated = False
    for k, v in doc.items():
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        content = v.get("content", "")
        paths = _extract_file_paths(content)
        if not paths:
            continue
        any_missing = any(not os.path.isfile(os.path.join(repo_root, p)) for p in paths)
        if any_missing and not v.get("stale_file_refs"):
            entry = dict(v)
            entry["stale_file_refs"] = True
            doc[k] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing paths: %s)",
                k,
                [p for p in paths if not os.path.isfile(os.path.join(repo_root, p))],
            )
        elif not any_missing and v.get("stale_file_refs"):
            # All referenced files now exist — clear the flag
            entry = dict(v)
            entry["stale_file_refs"] = False
            doc[k] = entry
            updated = True

    if updated:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "stale-ref-check"
        doc["_meta"] = meta

    return doc


def _playbook_size_bytes(doc: dict) -> int:
    """Return the JSON-serialised byte size of all playbook entries in *doc*."""
    playbook_subset = {k: v for k, v in doc.items() if k.startswith("playbook/")}
    return len(json.dumps(playbook_subset).encode())


async def finalize_pending_playbook_entries(
    task: Task,
    session: AsyncSession,
    *,
    succeeded: bool,
) -> int:
    """Resolve playbook entries left ``pending_source=<task.id>`` by *task*.

    Anti-poisoning: agents may call ``./playbook set`` while their task is
    still IN_PROGRESS. Those writes carry a ``pending_source`` field stamped
    by ``main.update_playbook`` so peers can't see them yet. This helper
    runs at terminal status and:

    * ``succeeded=True`` (task COMPLETED) — clears ``pending_source`` from
      every entry whose source is *task*, making them visible to peers.
    * ``succeeded=False`` (task FAILED / CANCELLED) — deletes those entries
      entirely, so a task that never landed code can't leave a lie behind
      claiming "X exists in the codebase."

    Returns the number of entries finalised (cleared or removed). No-op when
    the project has no design_doc or no matching pending entries.
    """
    project = await session.get(Project, task.project_id)
    if project is None or not project.design_doc:
        return 0

    doc = dict(project.design_doc)
    my_id = str(task.id)
    changed = 0
    to_delete: list[str] = []

    for k, v in list(doc.items()):
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        if v.get("pending_source") != my_id:
            continue
        if succeeded:
            new_entry = dict(v)
            new_entry.pop("pending_source", None)
            doc[k] = new_entry
        else:
            to_delete.append(k)
        changed += 1

    for k in to_delete:
        doc.pop(k, None)

    if changed:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = (
            "playbook-finalize-success" if succeeded else "playbook-finalize-failure"
        )
        doc["_meta"] = meta
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        logger.info(
            "[PLAYBOOK] %s %d pending entr%s for terminal task %s (succeeded=%s)",
            "Finalised" if succeeded else "Removed",
            changed,
            "y" if changed == 1 else "ies",
            task.id,
            succeeded,
        )
    return changed


def _archive_least_useful_entry(doc: dict) -> dict:
    """Move the least-useful playbook entry to ``playbook_archive`` in *doc*.

    "Least useful" is determined by a sort over:
    1. ``stale_file_refs`` flag (stale first)
    2. Effectiveness: inverse success rate (confirmed/followed) when
       ``followed_count ≥ _MIN_FOLLOWED_FOR_RATE``, otherwise inverse
       ``confirmed_count`` (least confirmed first)
    3. Entry age (oldest first, using ``updated_at``)
    4. Key name (tie-break)

    The archived entry is removed from the ``playbook/`` namespace and appended
    to the ``playbook_archive`` list (preserving it for operator review).

    Returns the mutated doc.  If there are no archiveable entries, returns doc
    unchanged.
    """
    now = _utcnow()
    candidates: list[tuple] = []
    for k, v in doc.items():
        if not k.startswith("playbook/") or not isinstance(v, dict):
            continue
        confirmed = v.get("confirmed_count", 0)
        followed = v.get("followed_count", 0)
        stale = 1 if v.get("stale_file_refs") else 0
        updated_at_str = v.get("updated_at", "")
        age_days = 0
        if updated_at_str:
            try:
                updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                # _utcnow() returns naive UTC; strip tzinfo so comparison is always naive
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)
                age_days = (now - updated).days
            except (ValueError, TypeError):
                pass
        # Use inverse success rate when enough observations exist so low-performing
        # entries surface before unobserved ones; fall back to raw confirmed_count.
        if followed >= _MIN_FOLLOWED_FOR_RATE:
            effectiveness = -(confirmed / followed)
        else:
            effectiveness = -confirmed
        # Sort: stale first, then least-effective, then oldest, then key
        candidates.append((stale, effectiveness, age_days, k))

    if not candidates:
        return doc

    candidates.sort(
        reverse=True
    )  # highest (stale, -confirmed, age) first — oldest first within same tier
    _, _, _, victim_key = candidates[0]
    victim_entry = dict(doc.pop(victim_key))
    victim_entry["_archived_key"] = victim_key
    victim_entry["_archived_at"] = now.isoformat()

    archive = list(doc.get("playbook_archive") or [])
    archive.append(victim_entry)
    # Trim oldest entries past the cap so the archive can't grow without
    # bound — see the payload budget rationale at _PLAYBOOK_ARCHIVE_MAX_DEFAULT.
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    if archive_max > 0 and len(archive) > archive_max:
        dropped = len(archive) - archive_max
        archive = archive[-archive_max:]
        logger.info(
            "[PLAYBOOK] Dropped %d oldest archive entries to stay within cap of %d",
            dropped,
            archive_max,
        )
    doc["playbook_archive"] = archive

    logger.info(
        "[PLAYBOOK] Archived entry '%s' to playbook_archive (capacity cleanup)",
        victim_key,
    )
    return doc


def _mark_stale_review_entries(doc: dict) -> bool:
    """Flag review-extracted entries whose ``updated_at`` exceeds
    ``_REVIEW_PLAYBOOK_STALE_DAYS`` with ``potentially_stale: True``.

    Returns True if any entry was changed.
    """
    now = _utcnow()
    changed = False
    for k, v in doc.items():
        if not k.startswith("playbook/review-") or not isinstance(v, dict):
            continue
        if v.get("source") != "review-extracted":
            continue
        updated_at_str = v.get("updated_at") or ""
        try:
            dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        is_stale = (now - dt).days >= _REVIEW_PLAYBOOK_STALE_DAYS
        if is_stale and not v.get("potentially_stale"):
            entry = dict(v)
            entry["potentially_stale"] = True
            doc[k] = entry
            changed = True
    return changed


def maybe_archive_for_capacity(doc: dict, new_entry_estimate: int = 0) -> dict:
    """Archive the least-useful playbook entry if total size would exceed capacity.

    Call this *before* adding a new entry.  *new_entry_estimate* is the
    approximate byte size of the entry that is about to be added.

    Returns the (possibly mutated) doc.
    """
    if _playbook_size_bytes(doc) + new_entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        doc = _archive_least_useful_entry(doc)
    return doc


def record_playbook_applied(doc: dict, key: str) -> None:
    """Increment followed_count when a playbook entry is applied to a task prompt.

    *key* may be the short form (e.g. ``"success-create-small"``) or the full
    ``playbook/<key>`` form.  No-op if the entry is not present.
    """
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        e = dict(entry)
        e["followed_count"] = e.get("followed_count", 0) + 1
        doc[full_key] = e


def record_playbook_confirmed(doc: dict, key: str) -> None:
    """Increment confirmed_count when a task that used this entry completes successfully.

    *key* may be the short form or the full ``playbook/<key>`` form.
    No-op if the entry is not present.
    """
    full_key = key if key.startswith("playbook/") else f"playbook/{key}"
    entry = doc.get(full_key)
    if isinstance(entry, dict):
        e = dict(entry)
        e["confirmed_count"] = e.get("confirmed_count", 0) + 1
        doc[full_key] = e


def retire_ineffective_patterns(
    doc: dict,
    *,
    min_followed: int = _RETIRE_MIN_FOLLOWED,
    max_success_rate: float = _RETIRE_MAX_SUCCESS_RATE,
) -> list[str]:
    """Auto-archive playbook entries whose success rate has fallen below the threshold.

    Only considers entries with ``followed_count >= min_followed`` so the rate
    is statistically meaningful.  Archived entries receive a ``_retirement_reason``
    tag with the computed success rate for operator review.

    Returns a list of full playbook keys that were archived.
    """
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    now = _utcnow()
    retired: list[str] = []

    for k in list(doc.keys()):
        if not k.startswith("playbook/") or not isinstance(doc.get(k), dict):
            continue
        entry = doc[k]
        followed = entry.get("followed_count", 0)
        if followed < min_followed:
            continue
        confirmed = entry.get("confirmed_count", 0)
        success_rate = confirmed / followed
        if success_rate >= max_success_rate:
            continue

        victim = dict(doc.pop(k))
        victim["_archived_key"] = k
        victim["_archived_at"] = now.isoformat()
        victim["_retirement_reason"] = f"low_success_rate:{success_rate:.2f}"
        archive = list(doc.get("playbook_archive") or [])
        archive.append(victim)
        if archive_max > 0 and len(archive) > archive_max:
            archive = archive[-archive_max:]
        doc["playbook_archive"] = archive
        retired.append(k)
        logger.info(
            "[PLAYBOOK] Retired '%s' success_rate=%.2f followed=%d confirmed=%d",
            k,
            success_rate,
            followed,
            confirmed,
        )

    return retired


def retire_idle_auto_entries(
    doc: dict,
    *,
    idle_days: int = _IDLE_RETIRE_DAYS,
) -> list[str]:
    """Auto-archive auto-generated playbook entries that have not been referenced
    for at least ``idle_days``.

    Operator-curated entries (``source != 'auto-generated'``) are EXEMPT; only
    entries with ``source == 'auto-generated'`` decay on idleness.

    Reuses the existing ``last_referenced_at`` field as the freshness signal
    (it is stamped in routers/agent.py whenever a playbook entry is shown to
    a claim). Falls back to ``updated_at`` for entries that were never
    referenced, so historical entries don't all retire on the first run.
    Entries lacking both timestamps are skipped (defensive — brand-new
    entries written without an ``updated_at`` would otherwise look idle).

    Archived entries receive ``_retirement_reason='idle_auto_entry:<N>d'``
    so the digest counter can distinguish idle-decay retirements from
    low-success-rate retirements.

    Returns the list of full playbook keys that were archived.
    """
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415

    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    now = _utcnow()
    threshold = timedelta(days=idle_days)
    retired: list[str] = []

    for k in list(doc.keys()):
        if not k.startswith("playbook/"):
            continue
        entry = doc.get(k)
        if not isinstance(entry, dict):
            continue
        if entry.get("source") != "auto-generated":
            continue

        ts_str = entry.get("last_referenced_at") or entry.get("updated_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if (now - ts) < threshold:
            continue

        victim = dict(doc.pop(k))
        victim["_archived_key"] = k
        victim["_archived_at"] = now.isoformat()
        victim["_retirement_reason"] = f"idle_auto_entry:{idle_days}d"
        archive = list(doc.get("playbook_archive") or [])
        archive.append(victim)
        if archive_max > 0 and len(archive) > archive_max:
            archive = archive[-archive_max:]
        doc["playbook_archive"] = archive
        retired.append(k)
        logger.info(
            "[PLAYBOOK] Retired idle auto-entry '%s' (idle >= %dd)",
            k,
            idle_days,
        )

    return retired


def count_idle_auto_retired_within(doc: dict, *, hours: int = 24) -> int:
    """Count playbook_archive entries retired by ``retire_idle_auto_entries``
    within the last ``hours``. Used by admin_digest to surface accidental
    over-retirement to operators.
    """
    if not doc:
        return 0
    archive = doc.get("playbook_archive") or []
    if not archive:
        return 0
    now = _utcnow()
    cutoff = now - timedelta(hours=hours)
    count = 0
    for entry in archive:
        if not isinstance(entry, dict):
            continue
        reason = entry.get("_retirement_reason") or ""
        if not reason.startswith("idle_auto_entry"):
            continue
        ts_str = entry.get("_archived_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------


def _extract_failure_module(error_lower: str) -> Optional[str]:
    """Return the base module name from the innermost traceback frame, if present.

    For example, given a traceback containing ``File "backend/app/auth.py"``,
    returns ``"auth"``.  Returns ``None`` when no ``File "..."`` pattern is found.
    """
    frame_matches = re.findall(r'file "([^"]+\.py)"', error_lower)
    if not frame_matches:
        return None
    innermost = frame_matches[-1]
    base = re.sub(r".*/", "", innermost)  # strip directory prefix
    return re.sub(r"\.py$", "", base) or None  # strip .py extension


def _extract_failure_signature(
    failure_class: str,
    error_summary: str,
    task_type: str = "",
    scope: str = "",
) -> str:
    """Return a short, normalised string that identifies the failure pattern.

    The goal is reproducibility: the same root cause should produce the same
    signature across different tasks so the counter in ``_maybe_auto_playbook``
    accumulates correctly.

    For Python exceptions with a traceback, the module name from the innermost
    frame is embedded in the signature to distinguish per-module failures:
    e.g. ``"importerror: auth: cannot import name foo"`` rather than
    ``"importerror: cannot import name foo"``.
    """
    error_lower = (error_summary or "").lower()

    # Timeouts are identified by (type, scope) — the error text varies too much
    if failure_class == "AGENT_TIMEOUT":
        return f"timeout-{task_type or 'unknown'}-{scope or 'medium'}"

    if failure_class == "CODE_ERROR":
        # Extract module from innermost traceback frame for module-aware signatures
        module_name = _extract_failure_module(error_lower)

        # Well-known Python exception names give stable signatures
        python_exc = re.search(
            r"(modulenotfounderror|importerror|typeerror|attributeerror|"
            r"valueerror|nameerror|keyerror|syntaxerror|assertionerror)"
            r"[:\s][^\n]{0,60}",
            error_lower,
        )
        if python_exc:
            raw = python_exc.group(0)
            # Drop file-path tokens (contain /) and line-number suffixes
            raw = re.sub(r"\S*/\S+", "", raw)
            raw = re.sub(r":\d+", ":", raw)
            clean = re.sub(r"\s+", " ", raw).strip()
            if module_name:
                # Inject module context: "importerror: auth: cannot import name foo"
                colon_idx = clean.find(":")
                if colon_idx >= 0:
                    exc_type = clean[:colon_idx].strip()
                    rest = clean[colon_idx + 1 :].strip()
                    clean = f"{exc_type}: {module_name}: {rest}"
            return clean[:80]

        # Test-runner failure lines: "FAILED backend/testing/test_foo.py"
        fail_match = re.search(r"fail(?:ed)?[\s:]+[\w/_.-]+\.py", error_lower)
        if fail_match:
            raw = fail_match.group(0)
            raw = re.sub(r"\S+/", "", raw)  # keep filename only
            return raw.strip()[:60]

    # Generic fallback: first meaningful line, stripped of paths/numbers
    for line in error_lower.split("\n"):
        line = line.strip()
        if line:
            line = re.sub(r"\S+/", "", line)  # strip path prefixes
            line = re.sub(r":\d+", ":", line)  # strip line numbers
            return line[:80]

    return f"{failure_class.lower()}-unknown"


def _signature_to_key(signature: str) -> str:
    """Convert a signature string to a valid playbook key (1-50 alphanumeric/hyphen chars)."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", signature.lower())
    normalized = normalized.strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        normalized = "unknown"
    # Prefix with "auto" so the key always starts with an alphanumeric char
    key = ("auto-" + normalized)[:50].rstrip("-")
    return key


def _hint_for_failure_class(failure_class: str) -> str:
    """Return a short actionable hint for the given failure class."""
    return {
        "CODE_ERROR": (
            "Check imports, syntax, and test assertions. Run lint before committing."
        ),
        "AGENT_TIMEOUT": (
            "Reduce scope. Focus on one critical change. Commit working changes early."
        ),
        "PROVISION_FAILURE": (
            "Infrastructure issue. Check GCP quota and zone availability."
        ),
        "EXTERNAL_API": (
            "External service failure. "
            "May be transient — retry after checking API status."
        ),
        "RATE_LIMIT": (
            "API rate limit hit. Reduce call frequency or add exponential backoff."
        ),
    }.get(failure_class, "Check the error log for details and reduce scope.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _maybe_auto_playbook(
    task: Task,
    failure_class: str,
    error_summary: str,
    session: AsyncSession,
) -> Optional[str]:
    """Store failure signature on *task* and create a playbook entry if the
    pattern has been seen ``threshold`` or more times in the last ``window_days``.

    Must be called while *task* still has ``status == FAILED`` (before it is
    cancelled by the requeue logic) so that it appears in the counting query.

    Returns the short playbook key (e.g. ``"auto-modulenotfounderror-..."``
    ) if a new entry was created, ``None`` otherwise.
    """
    import app.shared_state as _ss  # noqa: PLC0415

    threshold: int = _ss.runtime_config.get("auto_playbook_threshold", 3)
    window_days: int = _ss.runtime_config.get("auto_playbook_window_days", 7)

    # ── 1. Compute & persist failure signature ──────────────────────────────
    task_type = task.first_payload.get("type", "")
    task_scope = (task.context or {}).get("scope", DEFAULT_TASK_SCOPE)
    signature = _extract_failure_signature(
        failure_class, error_summary, task_type, task_scope
    )
    # Store module context for cross-referencing with module_feedback_profiles
    module_name = (
        _extract_failure_module((error_summary or "").lower())
        if failure_class == "CODE_ERROR"
        else None
    )

    ctx = dict(task.context or {})
    ctx["_failure_signature"] = signature
    if module_name:
        ctx["_failure_module"] = module_name
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)

    # ── 2. Count recent tasks with same signature (excluding current task) ──
    window_start = _utcnow() - timedelta(days=window_days)
    # Phase F3 (H4): push the ``_failure_signature`` filter into SQL via JSONB
    # containment so Postgres can use ``ix_task_context_gin`` instead of
    # seq-scanning every FAILED/CANCELLED task in the window and filtering in
    # Python. ``context @> {"_failure_signature": signature}`` is index-safe
    # on Postgres; on SQLite the JSONB op doesn't compile cleanly so we omit
    # the SQL-side filter and rely on the Python re-check below (audit
    # R3-W2 / H3). ``load_only`` drops heavy JSONBs we don't read here
    # (payloads, plan_output) but keeps ``status_log`` to avoid
    # MissingGreenlet from the Task event listeners (models.py ~360) under
    # async sessions. Limit caps the per-project scan; a project that hit
    # the cap had >200 same-signature failures in window and the playbook
    # entry already triggers well below that threshold.
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    where_clauses = [
        Task.project_id == task.project_id,
        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        Task.created_at >= window_start,
        Task.id != task.id,
    ]
    if dialect_name != "sqlite":
        # Postgres path: GIN containment via ``ix_task_context_gin``.
        where_clauses.append(
            Task.context.op("@>")(cast({"_failure_signature": signature}, JSONB))
        )
    # SQLite path: no SQL-side filter; Python re-check below filters the
    # candidate set. The 200-row limit caps the seq-scan worst case.
    result = await session.execute(
        select(Task)
        .where(*where_clauses)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.status_log,
            )
        )
        .limit(200)
    )
    recent_tasks = result.scalars().all()

    # Containment guarantees signature match on Postgres; on SQLite this is
    # the actual filter. Either way the Python re-check is the source of
    # truth for ``matching``.
    matching = [
        t
        for t in recent_tasks
        if (t.context or {}).get("_failure_signature") == signature
    ]
    total_count = len(matching) + 1  # +1 for the current task

    if total_count < threshold:
        logger.debug(
            "[AUTO_PLAYBOOK] project=%s signature='%s' seen %d/%d — no entry yet",
            task.project_id,
            signature,
            total_count,
            threshold,
        )
        return None

    # ── 3. Check if playbook entry already exists ───────────────────────────
    project = await session.get(Project, task.project_id)
    if not project:
        return None

    key = _signature_to_key(signature)
    playbook_full_key = f"playbook/{key}"
    doc = dict(project.design_doc or {})

    if playbook_full_key in doc:
        logger.debug(
            "[AUTO_PLAYBOOK] project=%s key='%s' already exists — skipping",
            task.project_id,
            key,
        )
        return None

    # ── 4. Create auto-generated playbook entry ─────────────────────────────
    recent_ids = [str(t.id) for t in matching[-2:]] + [str(task.id)]
    hint = _hint_for_failure_class(failure_class)
    module_note = (
        f" Repeated failures in module '{module_name}'." if module_name else ""
    )
    content = (
        f"Repeated failure pattern ({total_count}x in {window_days}d): {signature}. "
        f"Recent tasks: {', '.join(recent_ids)}. "
        f"Common fix: {hint}"
        f"{module_note}"
    )

    # Infer scope: if ALL tasks in the pattern have a direction_id and they all
    # share the same one, mark as direction-scoped.  Tasks without a direction_id
    # must NOT be silently dropped — their presence means the pattern is universal.
    all_pattern_tasks = matching + [task]
    all_have_direction = all(
        (t.context or {}).get("direction_id") for t in all_pattern_tasks
    )
    direction_ids = {
        (t.context or {}).get("direction_id")
        for t in all_pattern_tasks
        if (t.context or {}).get("direction_id")
    }
    if all_have_direction and len(direction_ids) == 1:
        entry_scope: str = "direction"
        learned_from_direction: Optional[str] = next(iter(direction_ids))
    else:
        entry_scope = "universal"
        learned_from_direction = None

    new_entry = {
        "content": content,
        "source_task_id": str(task.id),
        "updated_at": _utcnow().isoformat(),
        "confirmed_count": 0,
        "source": "auto-generated",
    }
    doc = maybe_archive_for_capacity(doc, len(json.dumps(new_entry).encode()))
    doc[playbook_full_key] = {
        "content": content,
        "source_task_id": str(task.id),
        "updated_at": _utcnow().isoformat(),
        "confirmed_count": 0,
        "source": "auto-generated",
        "scope": entry_scope,
        "learned_from_direction": learned_from_direction,
    }

    meta = dict(doc.get("_meta", {"revision": 0}))
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "auto-playbook"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[AUTO_PLAYBOOK] project=%s created entry '%s' for pattern '%s' (seen %dx)",
        task.project_id,
        key,
        signature,
        total_count,
    )
    return key


# ---------------------------------------------------------------------------
# Review-feedback extraction
# ---------------------------------------------------------------------------


def _categorize_review_issues_keyword(review_history: list) -> dict[str, list[dict]]:
    """Keyword-match implementation of review issue categorization (always offline)."""
    buckets: dict[str, list[dict]] = {}
    for entry in review_history:
        issues = entry.get("issues") or []
        for issue in issues:
            desc = (issue.get("description") or "").strip()
            severity = (issue.get("severity") or "").upper()
            if not desc or severity not in ("CRITICAL", "MAJOR", "MINOR"):
                continue
            desc_lower = desc.lower()
            matched = False
            for cat_name, keywords in _REVIEW_CATEGORIES.items():
                if any(kw in desc_lower for kw in keywords):
                    buckets.setdefault(cat_name, []).append(
                        {
                            "desc": desc,
                            "severity": severity,
                            "iteration": entry.get("iteration", 0),
                        }
                    )
                    matched = True
                    break
            if not matched:
                buckets.setdefault("other", []).append(
                    {
                        "desc": desc,
                        "severity": severity,
                        "iteration": entry.get("iteration", 0),
                    }
                )
    return buckets


def _categorize_review_issues(
    review_history: list,
    cached: Optional[dict] = None,
) -> dict[str, list[dict]]:
    """Scan review_history entries and bucket issues by category.

    Returns a mapping of {category_name: [{"desc": ..., "severity": ..., "iteration": ...}]}.
    Only CRITICAL/MAJOR/MINOR severity issues are considered.
    Each issue is assigned to the *first* matching category.

    When `cached` is provided it is returned verbatim (the LLM path already ran).
    """
    if cached is not None:
        return cached
    return _categorize_review_issues_keyword(review_history)


def resolve_sr_miss_human_truth(ctx: dict) -> set[str]:
    """Return the best available set of human-flagged review categories for a task.

    Uses the same three-tier precedence as ``_stats_for_tasks``:
    1. ``_all_iterations_rejection_categories`` (persisted across all review rounds)
    2. Keyword-categorised ``review_loop.review_history`` (covers late-iteration issues)
    3. ``_first_attempt_rejection_categories`` (backward-compatible fallback)

    Returns an empty set when none of the sources are present.
    """
    stored_all_iter = ctx.get("_all_iterations_rejection_categories") or {}
    if stored_all_iter:
        return set(stored_all_iter.keys())
    rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
    if rev_hist:
        return set(_categorize_review_issues_keyword(rev_hist).keys())
    return set(ctx.get("_first_attempt_rejection_categories") or {})


_VALID_CATEGORIES = set(_REVIEW_CATEGORIES.keys()) | {"other"}
_LLM_CATEGORIZE_MODEL = "claude-haiku-4-5-20251001"
_LLM_CATEGORIZE_TIMEOUT = 10.0


async def categorize_review_issues_llm(issues: list[dict]) -> dict[str, list[dict]]:
    """LLM-backed categorizer with keyword fallback.

    Accepts a flat list of {"description": str, "severity": str, "iteration": int}
    and returns the same bucketed shape as _categorize_review_issues.
    Falls back to keyword classification on any failure.
    """
    if os.environ.get("DISABLE_LLM_REVIEW_CATEGORIZATION", "").strip() == "1":
        # Build synthetic review_history from flat issue list so keyword helper can scan it.
        return _categorize_review_issues_keyword(
            [
                {
                    "iteration": i.get("iteration", 0),
                    "issues": [
                        {
                            "description": i.get("description", ""),
                            "severity": i.get("severity", ""),
                        }
                    ],
                }
                for i in issues
            ]
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "categorize_review_issues_llm: no ANTHROPIC_API_KEY, using keyword fallback"
        )
        return _keyword_from_flat(issues)

    valid_cats = sorted(_VALID_CATEGORIES)
    prompt = (
        "You are a code-review classifier. Categorise each issue below into exactly one of "
        f"these categories: {', '.join(valid_cats)}.\n\n"
        "Respond with ONLY valid JSON in this exact shape (no markdown, no commentary):\n"
        '{"classifications": [{"index": 0, "category": "..."}]}\n\n'
        "Issues (0-indexed):\n"
        + "\n".join(
            f"{i}. [{item.get('severity', '')}] {item.get('description', '')}"
            for i, item in enumerate(issues)
        )
    )

    for attempt in range(2):
        try:
            import anthropic as _anthropic  # noqa: PLC0415 — optional heavy dep

            client = _anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=_LLM_CATEGORIZE_MODEL,
                max_tokens=512,
                timeout=_LLM_CATEGORIZE_TIMEOUT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            data = json.loads(raw)
            classifications = data["classifications"]
            # Validate all categories before committing.
            for c in classifications:
                if c["category"] not in _VALID_CATEGORIES:
                    raise ValueError(f"unknown category: {c['category']!r}")
            # Build buckets.
            buckets: dict[str, list[dict]] = {}
            for c in classifications:
                idx = c["index"]
                if idx < 0 or idx >= len(issues):
                    continue
                item = issues[idx]
                severity = (item.get("severity") or "").upper()
                if severity not in ("CRITICAL", "MAJOR", "MINOR"):
                    continue
                cat = c["category"]
                buckets.setdefault(cat, []).append(
                    {
                        "desc": (item.get("description") or "").strip(),
                        "severity": severity,
                        "iteration": item.get("iteration", 0),
                    }
                )
            return buckets
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            if attempt == 0:
                logger.info(
                    "categorize_review_issues_llm: parse error %s, retrying once", exc
                )
                continue
            logger.info(
                "categorize_review_issues_llm: parse error on retry, using keyword fallback"
            )
            break
        except Exception as exc:  # network, auth, timeout, import error
            logger.info(
                "categorize_review_issues_llm: exception %s, using keyword fallback",
                exc,
            )
            break

    return _keyword_from_flat(issues)


def _keyword_from_flat(issues: list[dict]) -> dict[str, list[dict]]:
    """Convert flat issue list to synthetic review_history then run keyword classifier."""
    synthetic = [
        {
            "iteration": item.get("iteration", 0),
            "issues": [
                {
                    "description": item.get("description", ""),
                    "severity": item.get("severity", ""),
                }
            ],
        }
        for item in issues
    ]
    return _categorize_review_issues_keyword(synthetic)


async def cache_first_attempt_classification(
    task_context: dict,
    issues: list[dict],
) -> dict:
    """Run LLM categoriser (keyword fallback inside) and cache result in task_context.

    Writes task_context['_first_attempt_categorized_issues'] = {"buckets": {...},
    "_classification_method": "llm" | "keyword"} and returns the bucket dict.

    Intended to be invoked at first-review-rejection time by the lifecycle pipeline.
    """
    # Determine method before calling so we can record it.
    if (
        os.environ.get("DISABLE_LLM_REVIEW_CATEGORIZATION", "").strip() == "1"
        or not os.environ.get("ANTHROPIC_API_KEY", "").strip()
    ):
        method = "keyword"
        buckets = _keyword_from_flat(issues)
    else:
        try:
            import anthropic as _anthropic  # noqa: PLC0415

            # Attempt LLM; if result equals keyword fallback we still flag it as llm
            # (the LLM path ran, even if it happened to agree with keyword).
            _anthropic  # just ensure importable before we call the full helper
            buckets = await categorize_review_issues_llm(issues)
            method = "llm"
        except Exception:
            buckets = _keyword_from_flat(issues)
            method = "keyword"

    task_context["_first_attempt_categorized_issues"] = {
        "buckets": buckets,
        "_classification_method": method,
    }
    return buckets


def summarize_decomposition_outcomes(direction: dict) -> Optional[dict]:
    """Summarize decomposition_quality from the last 3 productive research cycles.

    Returns None if there is no decomposition data to summarize.
    """
    history = direction.get("research_history", []) or []
    productive = [h for h in history if h.get("tasks_created", 0) > 0][-3:]
    flat: list[dict] = []
    for cycle in productive:
        dq = cycle.get("decomposition_quality") or []
        if isinstance(dq, list):
            flat.extend(e for e in dq if isinstance(e, dict))
    if not flat:
        return None

    def _nums(key, cast=float):
        return [cast(e[key]) for e in flat if isinstance(e.get(key), (int, float))]

    merge_rates = _nums("subtask_merge_rate")
    files_per = _nums("avg_files_per_subtask")
    max_files_vals = _nums("max_files_in_subtask", int)
    dep_depths = _nums("dependency_depth", int)
    high_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) >= 2
    ]
    low_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) < 2
    ]
    high_corr_low = False
    if high_xmod and low_xmod:
        if (sum(high_xmod) / len(high_xmod)) + 0.1 <= (sum(low_xmod) / len(low_xmod)):
            high_corr_low = True

    out: dict = {
        "prior_decomposition_count": len(flat),
        "high_cross_module_correlates_with_low_merge": high_corr_low,
    }
    if merge_rates:
        out["avg_subtask_merge_rate"] = round(sum(merge_rates) / len(merge_rates), 3)
    if files_per:
        out["avg_files_per_subtask"] = round(sum(files_per) / len(files_per), 2)
    if max_files_vals:
        out["max_files_in_subtask"] = max(max_files_vals)
    if dep_depths:
        out["avg_dependency_depth"] = round(sum(dep_depths) / len(dep_depths), 2)
        out["max_dependency_depth"] = max(dep_depths)
    return out


def compute_recurring_first_review_descriptions(
    rows,
    cutoff,
    *,
    task_filter=None,
    min_tasks: int = 0,
    count_unique_tasks: bool = False,
    extra_fields: Optional[dict] = None,
) -> Optional[list[dict]]:
    """Top recurring issue descriptions from first-review (iteration 0) history.

    Scans review_history across ``rows`` and returns up to 3 descriptions that
    appear at least twice, sorted by count descending. Tasks with
    ``created_at < cutoff`` or rejected by ``task_filter`` are skipped. When
    ``count_unique_tasks`` is True, counts distinct tasks mentioning each
    description; otherwise counts total occurrences. Returns None if the
    filtered task count is below ``min_tasks`` or no description repeats.
    """
    from collections import Counter

    desc_counter: Counter = Counter()
    desc_to_cat: dict = {}
    desc_task_ids: dict = {}
    filtered_task_count = 0
    from app.time_utils import _to_naive_utc

    cutoff_naive = _to_naive_utc(cutoff)
    for ht in rows:
        ht_created = _to_naive_utc(ht.created_at)
        if ht_created and cutoff_naive and ht_created < cutoff_naive:
            continue
        if task_filter and not task_filter(ht):
            continue
        review_loop = (ht.context or {}).get("review_loop", {})
        rev_hist = review_loop.get("review_history", []) if review_loop else []
        if not rev_hist:
            continue
        filtered_task_count += 1
        for entry in rev_hist:
            if entry.get("iteration") != 0:
                continue
            for issue in entry.get("issues", []):
                desc = issue.get("description") or ""
                cat = issue.get("category", "other")
                if not desc:
                    continue
                normalized = desc.lower().strip()[:120]
                if count_unique_tasks:
                    desc_task_ids.setdefault(normalized, set()).add(ht.id)
                else:
                    desc_counter[normalized] += 1
                if normalized not in desc_to_cat:
                    desc_to_cat[normalized] = cat
    if filtered_task_count < min_tasks:
        return None
    if count_unique_tasks:
        pairs = [
            (desc, len(tids)) for desc, tids in desc_task_ids.items() if len(tids) >= 2
        ]
    else:
        pairs = [(desc, cnt) for desc, cnt in desc_counter.items() if cnt >= 2]
    if not pairs:
        return None
    pairs.sort(key=lambda x: -x[1])
    out = []
    for desc, cnt in pairs[:3]:
        entry = {
            "description": desc,
            "count": cnt,
            "category": desc_to_cat.get(desc, "other"),
        }
        if extra_fields:
            entry.update(extra_fields)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Success-pattern capture
# ---------------------------------------------------------------------------


async def capture_success_pattern(
    task: Task,
    session: AsyncSession,
) -> Optional[str]:
    """Capture a positive pattern from a first-attempt success as a playbook entry.

    Uses a total-completions noise filter: only captures a success pattern after the
    task type has accumulated at least 3 total completions (successful or failed) in the
    project.  This avoids noise from one-off tasks without requiring failures first,
    solving the cold-start problem for new task types like 'migrate' or 'perf'.

    One entry per (task_type, scope) pair — e.g. ``playbook/success-create-small``.
    Each entry tracks success_count, scope_distribution (how many successes per scope
    level), and lightweight metadata: num_files_changed, tests_added, first_attempt_success.

    Returns the short key (e.g. ``"success-create-small"``) if an entry was created or
    updated, ``None`` if the noise filter was not met.
    """
    task_type = task.first_payload.get("type", "")
    if not task_type:
        return None

    scope: str = (task.context or {}).get("scope") or "unspecified"
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    direction_id: str = (task.context or {}).get("direction_id") or ""
    num_files = len(related_files)

    # Richer implementation-shape signals: file-type distribution and test count.
    file_type_dist: dict[str, int] = {}
    test_file_count = 0
    for f in related_files:
        if (
            "/test_" in f
            or f.startswith("test_")
            or ".test." in f
            or ".spec." in f
            or "/testing/" in f
            or "/tests/" in f
        ):
            test_file_count += 1
        ext = f.rsplit(".", 1)[-1] if "." in f else ""
        if ext and len(ext) <= 6:
            file_type_dist[ext] = file_type_dist.get(ext, 0) + 1

    tests_added = test_file_count > 0

    project = await session.get(Project, task.project_id)
    if not project:
        return None

    # Noise filter: require at least 3 total completions (success or failure) for
    # this task type before capturing a success pattern.  Use a COUNT query with
    # JSONB type filter to avoid loading all project tasks into memory.
    prior_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == task.project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.id != task.id,
                Task.payloads[0]["type"].as_string() == task_type,
            )
        )
    ).scalar_one()
    same_type_count = prior_count + 1  # +1 for the current task

    if same_type_count < 3:
        logger.debug(
            "[SUCCESS_PLAYBOOK] project=%s type=%s: only %d completion(s) — noise filter not met",
            task.project_id,
            task_type,
            same_type_count,
        )
        return None

    doc = dict(project.design_doc or {})

    # One entry per (task_type, scope) pair.
    scope_slug = scope.lower().replace(" ", "-")
    key = f"success-{task_type}-{scope_slug}"
    playbook_full_key = f"playbook/{key}"
    files_note = f" Files: {', '.join(related_files[:5])}." if related_files else ""

    existing = doc.get(playbook_full_key)
    if existing and isinstance(existing, dict):
        entry = dict(existing)
        count = entry.get("success_count", 1) + 1
        scope_dist = dict(entry.get("scope_distribution") or {})
        scope_dist[scope] = scope_dist.get(scope, 0) + 1
        raw_dir_succ = entry.get("direction_successes")
        dir_succ = dict(raw_dir_succ) if isinstance(raw_dir_succ, dict) else {}
        if direction_id:
            dir_succ[direction_id] = dir_succ.get(direction_id, 0) + 1
        entry["success_count"] = count
        entry["scope_distribution"] = scope_dist
        entry["direction_successes"] = dir_succ
        entry["num_files_changed"] = num_files
        entry["tests_added"] = tests_added
        entry["test_file_count"] = test_file_count
        entry["file_type_distribution"] = file_type_dist
        entry["updated_at"] = _utcnow().isoformat()
        entry["content"] = (
            f"First-attempt success pattern for {task_type} tasks "
            f"({count} observations, latest scope: {scope}).{files_note} "
            f"Scope distribution: {scope_dist}."
        )
        doc[playbook_full_key] = entry
        action = "updated"
    else:
        content = (
            f"First-attempt success pattern for {task_type} tasks "
            f"(1 observation, scope: {scope}).{files_note} "
            f"Files changed: {num_files}, tests added: {tests_added}."
        )
        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "success_count": 1,
            "source": "success-pattern",
            "applicable_types": [task_type],
            "scope": scope,
            "num_files_changed": num_files,
            "tests_added": tests_added,
            "test_file_count": test_file_count,
            "file_type_distribution": file_type_dist,
            "direction_successes": {direction_id: 1} if direction_id else {},
            "first_attempt_success": True,
            "scope_distribution": {scope: 1},
        }
        doc = maybe_archive_for_capacity(doc, len(json.dumps(new_entry).encode()))
        doc[playbook_full_key] = new_entry
        action = "created"

    meta = dict(doc.get("_meta", {"revision": 0}))
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "success-playbook"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SUCCESS_PLAYBOOK] project=%s %s entry '%s' for task_type=%s scope=%s",
        task.project_id,
        action,
        key,
        task_type,
        scope,
    )
    return key


# Task types where surfacing 'recent successes' patterns at spawn time is useful.
# Other types (review, plan, qa, audit, etc.) treat this signal as noise.
RECENT_SUCCESSES_TASK_TYPES: frozenset[str] = frozenset({"create", "fix", "research"})


def summarize_recent_successes(
    design_doc: dict,
    task_type: str,
    direction_id: Optional[str],
    scope: Optional[str],
    *,
    max_bullets: int = 5,
    char_budget: int = 600,
) -> Optional[str]:
    """Return a markdown 'Recent successes' section drawn from playbook/success-* entries.

    Helper is task-type-agnostic — call sites should gate on
    ``RECENT_SUCCESSES_TASK_TYPES``. Reads only from *design_doc*; performs no
    I/O. Returns None when nothing qualifies so callers can omit the section.

    Ranking: direction-affinity (direction_successes[direction_id] > 0), then
    exact (task_type, scope) match, then ``success_count``, then ``updated_at``
    recency. Entries with success_count < 2 are filtered out.

    Output is hard-capped at *char_budget* by dropping trailing bullets — never
    truncated mid-line. Returns None if even the first bullet doesn't fit.
    """
    if not isinstance(design_doc, dict) or not task_type:
        return None
    prefix = f"playbook/success-{task_type}-"
    candidates: list[tuple[str, dict]] = []
    for k, v in design_doc.items():
        if not isinstance(v, dict) or not k.startswith(prefix):
            continue
        if int(v.get("success_count") or 0) < 2:
            continue
        candidates.append((k, v))
    if not candidates:
        return None

    scope_slug = (scope or "").lower().replace(" ", "-")
    exact_key = f"playbook/success-{task_type}-{scope_slug}" if scope_slug else None

    def _rank(item: tuple[str, dict]) -> tuple:
        k, v = item
        dir_succ = v.get("direction_successes") or {}
        affinity = (
            int(dir_succ.get(direction_id, 0))
            if direction_id and isinstance(dir_succ, dict)
            else 0
        )
        scope_match = 1 if exact_key and k == exact_key else 0
        return (
            affinity,
            scope_match,
            int(v.get("success_count") or 0),
            v.get("updated_at") or "",
        )

    candidates.sort(key=_rank, reverse=True)

    bullets: list[str] = []
    for k, v in candidates[:max_bullets]:
        scope_label = k[len(prefix) :] or "unspecified"
        sc = int(v.get("success_count") or 0)
        parts = [f"{task_type}-{scope_label} ({sc}x)"]
        ftd = v.get("file_type_distribution")
        if isinstance(ftd, dict) and ftd:
            top_exts = sorted(ftd.items(), key=lambda kv: -int(kv[1] or 0))[:3]
            parts.append("files: " + ", ".join(f"{ext}:{n}" for ext, n in top_exts))
        if v.get("tests_added"):
            tfc = int(v.get("test_file_count") or 0)
            parts.append(f"tests added ({tfc})" if tfc else "tests added")
        dir_succ = v.get("direction_successes")
        if direction_id and isinstance(dir_succ, dict):
            local = int(dir_succ.get(direction_id, 0))
            if local > 0:
                parts.append(f"this direction: {local}")
        bullets.append("- " + "; ".join(parts))

    if not bullets:
        return None

    header = "## Recent successes (what has compounded here)"
    out_lines: list[str] = [header]
    out_len = len(header)
    for b in bullets:
        added = len(b) + 1  # +1 for the joining newline
        if out_len + added > char_budget:
            break
        out_lines.append(b)
        out_len += added

    if len(out_lines) == 1:
        return None
    return "\n".join(out_lines)


_MODULE_FEEDBACK_KEY = "module_feedback"
_MODULE_FEEDBACK_MAX_ENTRIES = 50
# Grace window (in days) before stale module-feedback entries start to decay.
_MODULE_FEEDBACK_DECAY_AGE_DAYS = 14
# Half-life (in days) of the exponential decay applied past the grace window.
_MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS = 14

_DIRECTION_FIRST_ATTEMPT_KEY = "direction_first_attempt_feedback"
_FIRST_ATTEMPT_PLAYBOOK_THRESHOLD = 5
# Decay parameters for direction first-attempt tallies — mirror module feedback
# so recent rejections dominate old, possibly-resolved patterns.
_DIRECTION_FIRST_ATTEMPT_DECAY_AGE_DAYS = 14
_DIRECTION_FIRST_ATTEMPT_DECAY_HALF_LIFE_DAYS = 14


def _decay_counts_entry(
    entry: dict, now: datetime, grace_days: float, half_life_days: float
) -> dict:
    """Return *entry* with integer category counts exponentially decayed by age.

    No-op when ``last_updated`` is missing/unparseable or the entry is still
    within the grace window. Otherwise multiplies each integer count (and
    ``total``) by ``0.5 ** (overage_days / half_life_days)``; categories that
    round to zero are dropped so resolved patterns fade out completely.
    """
    last_str = entry.get("last_updated")
    if not last_str:
        return entry
    try:
        last_dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return entry
    if last_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=None)
    overage = (now - last_dt).total_seconds() / 86400.0 - grace_days
    if overage <= 0:
        return entry
    factor = 0.5 ** (overage / half_life_days)
    decayed: dict = {}
    new_total = 0
    for k, v in entry.items():
        if k in ("total", "last_updated"):
            continue
        if not isinstance(v, int):
            decayed[k] = v
            continue
        new_count = round(v * factor)
        if new_count > 0:
            decayed[k] = new_count
            new_total += new_count
    decayed["total"] = new_total
    return decayed


def _decay_module_feedback_entry(entry: dict, now: datetime) -> dict:
    return _decay_counts_entry(
        entry,
        now,
        _MODULE_FEEDBACK_DECAY_AGE_DAYS,
        _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
    )


def _update_module_feedback_profiles(
    doc: dict,
    related_files: list[str],
    buckets: dict[str, list[dict]],
) -> None:
    """Update per-module feedback profiles in the design_doc dict (in-place).

    For each file in *related_files*, accumulates the count of issues per
    review category.  Caps the total number of tracked modules at
    ``_MODULE_FEEDBACK_MAX_ENTRIES``; when the cap is exceeded the entry with
    the oldest ``last_updated`` timestamp is evicted. Existing entries are
    decayed before the new counts are added so old, resolved patterns fade
    out instead of accumulating indefinitely.
    """
    if not related_files or not buckets:
        return

    now = _utcnow()
    now_iso = now.isoformat()
    profiles: dict = dict(doc.get(_MODULE_FEEDBACK_KEY) or {})

    # Flatten bucket issue counts: {category: count}
    cat_counts: dict[str, int] = {
        cat: len(issues) for cat, issues in buckets.items() if issues
    }
    if not cat_counts:
        return

    for file_path in related_files:
        if not file_path:
            continue
        existing = profiles.get(file_path)
        entry = _decay_module_feedback_entry(dict(existing), now) if existing else {}
        for cat, count in cat_counts.items():
            entry[cat] = entry.get(cat, 0) + count
        entry["total"] = entry.get("total", 0) + sum(cat_counts.values())
        entry["last_updated"] = now_iso
        profiles[file_path] = entry

    # Evict oldest entries if over cap
    if len(profiles) > _MODULE_FEEDBACK_MAX_ENTRIES:
        sorted_by_age = sorted(
            profiles.items(),
            key=lambda kv: kv[1].get("last_updated", ""),
        )
        for evict_key, _ in sorted_by_age[
            : len(profiles) - _MODULE_FEEDBACK_MAX_ENTRIES
        ]:
            del profiles[evict_key]

    doc[_MODULE_FEEDBACK_KEY] = profiles


def _accumulate_direction_first_attempt(
    doc: dict,
    direction_key: str,
    buckets: dict[str, list[dict]],
) -> None:
    """Accumulate first-attempt review rejection counts at the direction level (in-place).

    Stores a running tally under ``doc["direction_first_attempt_feedback"][direction_key]``
    with one integer count per review category.  This is a lightweight structure — not a
    full playbook entry — designed for high-frequency updates (every 1-iteration review).
    """
    if not buckets:
        return
    now = _utcnow()
    now_iso = now.isoformat()
    tally: dict = dict(doc.get(_DIRECTION_FIRST_ATTEMPT_KEY) or {})
    existing = tally.get(direction_key) or {}
    entry: dict = (
        _decay_counts_entry(
            dict(existing),
            now,
            _DIRECTION_FIRST_ATTEMPT_DECAY_AGE_DAYS,
            _DIRECTION_FIRST_ATTEMPT_DECAY_HALF_LIFE_DAYS,
        )
        if existing
        else {}
    )
    for cat, issues in buckets.items():
        if issues:
            entry[cat] = entry.get(cat, 0) + len(issues)
    entry["total"] = entry.get("total", 0) + sum(len(v) for v in buckets.values() if v)
    entry["last_updated"] = now_iso
    tally[direction_key] = entry
    doc[_DIRECTION_FIRST_ATTEMPT_KEY] = tally


def _maybe_auto_playbook_from_first_attempt(
    doc: dict,
    direction_key: str,
    task_type: str,
    task_id: str,
    threshold: int = _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
) -> list[str]:
    """Create playbook entries for direction categories that hit the first-attempt threshold.

    Returns the list of newly created playbook keys (short form, without ``playbook/`` prefix).
    Skips categories that already have a ``playbook/first-attempt-*`` entry.
    """
    tally = (doc.get(_DIRECTION_FIRST_ATTEMPT_KEY) or {}).get(direction_key) or {}
    created: list[str] = []
    for cat, count in tally.items():
        if cat in ("total", "last_updated") or not isinstance(count, int):
            continue
        if count < threshold:
            continue
        # Use a short direction slug to keep keys readable
        dir_slug = direction_key[:8].replace("-", "").replace("_", "") or "proj"
        key = f"first-attempt-{dir_slug}-{cat}"
        full_key = f"playbook/{key}"
        if full_key in doc:
            continue
        content = (
            f"First-attempt rejection pattern (direction {direction_key[:16]}): "
            f"{cat.replace('-', ' ')} issues have been flagged {count}x on first try. "
            f"Agents in this direction consistently miss {cat.replace('-', ' ')} requirements "
            f"before submitting. Pay special attention to {cat.replace('-', ' ')} before opening a PR."
        )
        doc[full_key] = {
            "content": content,
            "source_task_id": task_id,
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "source": "first-attempt-pattern",
            "applicable_types": [task_type],
            "direction_key": direction_key,
            "rejection_count": count,
        }
        created.append(key)
    return created


async def extract_review_feedback_to_playbook(
    task: Task,
    review_loop: dict,
    pr_number: int,
    original_task_type: str,
    session: AsyncSession,
) -> list[str]:
    """Extract review feedback and accumulate per-direction learning on every merge.

    Behaviour by iteration count:
    - Any iteration (≥1): update per-module feedback profiles; accumulate direction-level
      first-attempt rejection tallies from the iteration-0 review entry.
    - iteration ≥ 3: additionally create up to 2 full ``review-extracted`` playbook entries
      (the existing high-signal path for multi-cycle reviews).
    - iteration < 1 or no review_history: no-op.

    When a direction accumulates ``_FIRST_ATTEMPT_PLAYBOOK_THRESHOLD`` (default 5)
    first-attempt rejections in a single category, a targeted ``first-attempt-pattern``
    playbook entry is auto-generated.

    Returns the list of created playbook keys (may be empty).
    """
    iteration = review_loop.get("iteration", 0)
    review_history = review_loop.get("review_history") or []

    if not review_history:
        return []

    project = await session.get(Project, task.project_id)
    if not project:
        return []

    doc = dict(project.design_doc or {})

    # Always update per-module feedback profiles regardless of iteration count
    buckets = _categorize_review_issues(review_history)
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    module_profiles_updated = bool(related_files and buckets)
    _update_module_feedback_profiles(doc, related_files, buckets)

    # Accumulate direction-level first-attempt rejection data (iteration-0 entry only)
    direction_key = str(
        (task.context or {}).get("direction_summary", {}).get("id") or "_project"
    )
    first_review_entries = [e for e in review_history if e.get("iteration", 999) == 0]
    first_attempt_keys: list[str] = []
    direction_tally_updated = False
    if first_review_entries:
        first_attempt_buckets = _categorize_review_issues(first_review_entries)
        if first_attempt_buckets:
            _accumulate_direction_first_attempt(
                doc, direction_key, first_attempt_buckets
            )
            direction_tally_updated = True
            first_attempt_keys = _maybe_auto_playbook_from_first_attempt(
                doc, direction_key, original_task_type, str(task.id)
            )

    # Determine provenance and scope for review-extracted entries (used in >= 3 iteration path)
    direction_id = (task.context or {}).get("direction_id")
    entry_scope = "module" if related_files else "universal"

    created_keys: list[str] = list(first_attempt_keys)

    # Flag review-extracted entries that have not been observed for >60 days.
    # Runs on every invocation so coaching guidance reflects current patterns.
    review_entries_touched = _mark_stale_review_entries(doc)

    # Create or refresh full playbook entries only for 3+ iteration reviews
    if iteration >= 3 and buckets:
        # Pick the top-2 categories by severity-weighted score (CRITICAL=3, MAJOR=2, MINOR=1)
        _severity_weights = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
        top_cats = sorted(
            buckets.items(),
            key=lambda kv: sum(_severity_weights.get(i["severity"], 1) for i in kv[1]),
            reverse=True,
        )[:2]

        for cat_name, issues in top_cats:
            playbook_full_key = f"playbook/review-{cat_name}"

            # Surface up to 3 highest-severity examples
            severity_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
            top_issues = sorted(
                issues, key=lambda x: severity_order.get(x["severity"], 3)
            )[:3]
            examples = "; ".join(i["desc"] for i in top_issues if i.get("desc"))

            content = (
                f"Review pattern from PR #{pr_number} ({iteration} iterations): "
                f"{cat_name.replace('-', ' ')} issues were repeatedly flagged. "
                f"Examples: {examples[:300]}. "
                f"Applicable to: {original_task_type} tasks."
            )

            existing = doc.get(playbook_full_key)
            if isinstance(existing, dict):
                # Refresh existing entry: always bump hit_count; rewrite content
                # and reset timestamp only when older than the refresh threshold.
                entry = dict(existing)
                entry["hit_count"] = entry.get("hit_count", 1) + 1

                updated_at_str = entry.get("updated_at") or ""
                is_stale = True
                try:
                    dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                    is_stale = (_utcnow() - dt).days >= _REVIEW_PLAYBOOK_REFRESH_DAYS
                except (ValueError, TypeError):
                    pass

                if is_stale:
                    entry["content"] = content
                    entry["updated_at"] = _utcnow().isoformat()
                    entry["source_task_id"] = str(task.id)
                    entry["pr_number"] = pr_number
                    entry["iteration_count"] = iteration
                    entry.pop("potentially_stale", None)

                doc[playbook_full_key] = entry
                review_entries_touched = True
                continue

            entry: dict = {
                "content": content,
                "source_task_id": str(task.id),
                "updated_at": _utcnow().isoformat(),
                "confirmed_count": 0,
                "hit_count": 1,
                "source": "review-extracted",
                "applicable_types": [original_task_type],
                "pr_number": pr_number,
                "iteration_count": iteration,
                "scope": entry_scope,
                "learned_from_direction": direction_id,
            }
            if entry_scope == "module":
                entry["related_files"] = related_files
            doc = maybe_archive_for_capacity(doc, len(json.dumps(entry).encode()))
            doc[playbook_full_key] = entry
            created_keys.append(f"review-{cat_name}")

    if (
        created_keys
        or module_profiles_updated
        or direction_tally_updated
        or review_entries_touched
    ):
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "review-playbook"
        doc["_meta"] = meta

        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

        if created_keys:
            logger.info(
                "[REVIEW_PLAYBOOK] project=%s PR #%d iteration=%d created entries: %s",
                task.project_id,
                pr_number,
                iteration,
                created_keys,
            )

    return created_keys


# ---------------------------------------------------------------------------
# Review effectiveness tracking
# ---------------------------------------------------------------------------

# Minimum number of tasks with review history required in each window for the
# trend computation to be meaningful.
_MIN_REVIEW_SAMPLE = 10

# Rate-of-change threshold below which a category is considered "stable" vs
# "decreasing" / "increasing".  A 5pp shift is distinguishable from noise.
_TREND_DELTA_THRESHOLD = 0.05
# Threshold for precision/recall quadrant classification (5pp absolute change).
_PRECISION_RECALL_QUADRANT_THRESHOLD = 0.05
_SR_SEVERITY_DELTA_THRESHOLDS: dict[str, float] = {
    "CRITICAL": 0.03,
    "MAJOR": 0.05,
    "MINOR": 0.10,
}

# Baseline-floor alert: fire when a category's hit-rate stays below this
# threshold for at least _FLOOR_MIN_CONSECUTIVE_WINDOWS measurement windows.
_SR_FLOOR_THRESHOLD = 0.50
_FLOOR_MIN_CONSECUTIVE_WINDOWS = 2

# PASS reasoning thinning alert: fire when a non-regressing category has
# engagement below this threshold with at least _SR_ENGAGEMENT_MIN_PASSES passes.
_SR_ENGAGEMENT_THRESHOLD = 0.5
_SR_ENGAGEMENT_MIN_PASSES = 10


async def compute_review_effectiveness(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Compare review issue category rates across two consecutive 14-day windows.

    For each category in ``_REVIEW_CATEGORIES`` (plus "other"), the function
    computes the fraction of total issues it represents in the *current* 14-day
    window and the *previous* 14-day window, then classifies the change as one of:

    - ``"decreasing"`` — rate dropped ≥ 5pp (surfacing the category is helping)
    - ``"increasing"`` — rate rose ≥ 5pp (situation is getting worse)
    - ``"stable"``      — change < 5pp in either direction

    Returns ``None`` when either window has fewer than :data:`_MIN_REVIEW_SAMPLE`
    tasks with non-empty review history.

    **Side effect**: writes the result to
    ``project.design_doc["review_effectiveness"]`` and maintains a per-category
    ``rate_history`` list (capped at 5 entries) so that persistent issues can be
    detected across multiple consecutive windows.

    A *persistent issue* is a category whose ``rate_history`` tail of 3 entries
    are each > 20 %, indicating it has been surfaced to agents for at least 3
    windows yet remains common.
    """
    now = _utcnow()
    cutoff_14d = now - timedelta(days=14)
    cutoff_28d = now - timedelta(days=28)

    def _split_history(tasks: list, start: datetime, end: datetime) -> tuple[int, list]:
        """Return (task_count_with_reviews, combined_review_history_entries).

        Compares naive datetimes; callers must pass naive *start*/*end* values.
        """
        tasks_with_rev = 0
        history: list = []
        for t in tasks:
            if not t.created_at:
                continue
            # Normalise to naive for comparison regardless of DB driver timezone handling.
            ts = (
                t.created_at.replace(tzinfo=None)
                if t.created_at.tzinfo
                else t.created_at
            )
            if not (start <= ts < end):
                continue
            review_loop = (t.context or {}).get("review_loop") or {}
            rev_hist = review_loop.get("review_history") or []
            if rev_hist:
                tasks_with_rev += 1
                history.extend(rev_hist)
        return tasks_with_rev, history

    def _split_recurrence(
        tasks: list, start: datetime, end: datetime
    ) -> tuple[int, dict[str, int]]:
        """Return (total_tasks_with_any_rejection, {cat: distinct_task_count}).

        Counts distinct tasks that have each category in
        ``_first_attempt_rejection_categories`` within the window.  Only tasks
        whose ``created_at`` falls in [start, end) are considered.  Callers
        must pass naive *start*/*end* values.
        """
        total_with_rejection = 0
        cat_task_counts: dict[str, int] = {}
        for t in tasks:
            if not t.created_at:
                continue
            ts = (
                t.created_at.replace(tzinfo=None)
                if t.created_at.tzinfo
                else t.created_at
            )
            if not (start <= ts < end):
                continue
            rejection_cats = (t.context or {}).get(
                "_first_attempt_rejection_categories"
            ) or {}
            if not rejection_cats:
                continue
            total_with_rejection += 1
            for cat in rejection_cats:
                cat_task_counts[cat] = cat_task_counts.get(cat, 0) + 1
        return total_with_rejection, cat_task_counts

    # Use a far-future sentinel with no tzinfo to handle both naive and aware datetimes.
    # The cutoff datetimes from _utcnow() are tz-aware; strip tzinfo for comparison so
    # the function works with both naive (DB default) and aware created_at values.
    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    cutoff_14d_n = _naive(cutoff_14d)
    cutoff_28d_n = _naive(cutoff_28d)
    sentinel_n = datetime(9999, 12, 31)

    current_count, current_history = _split_history(all_tasks, cutoff_14d_n, sentinel_n)
    previous_count, previous_history = _split_history(
        all_tasks, cutoff_28d_n, cutoff_14d_n
    )

    if current_count < _MIN_REVIEW_SAMPLE or previous_count < _MIN_REVIEW_SAMPLE:
        return None

    current_buckets = _categorize_review_issues(current_history)
    previous_buckets = _categorize_review_issues(previous_history)

    current_total = sum(len(v) for v in current_buckets.values())
    previous_total = sum(len(v) for v in previous_buckets.values())

    if current_total == 0 or previous_total == 0:
        return None

    # Recurrence: fraction of tasks (with any first-attempt rejection) that had
    # each category flagged.  Computed across the same two windows independently.
    curr_rejection_total, curr_cat_counts = _split_recurrence(
        all_tasks, cutoff_14d_n, sentinel_n
    )
    prev_rejection_total, prev_cat_counts = _split_recurrence(
        all_tasks, cutoff_28d_n, cutoff_14d_n
    )

    all_cats = set(current_buckets.keys()) | set(previous_buckets.keys())
    categories: dict[str, Any] = {}
    for cat in all_cats:
        current_rate = round(len(current_buckets.get(cat, [])) / current_total, 2)
        previous_rate = round(len(previous_buckets.get(cat, [])) / previous_total, 2)

        delta = current_rate - previous_rate
        if delta <= -_TREND_DELTA_THRESHOLD:
            trend = "decreasing"
        elif delta >= _TREND_DELTA_THRESHOLD:
            trend = "increasing"
        else:
            trend = "stable"

        recurrence_rate: Optional[float] = (
            round(curr_cat_counts.get(cat, 0) / curr_rejection_total, 3)
            if curr_rejection_total > 0
            else None
        )
        previous_recurrence_rate: Optional[float] = (
            round(prev_cat_counts.get(cat, 0) / prev_rejection_total, 3)
            if prev_rejection_total > 0
            else None
        )

        categories[cat] = {
            "current_rate": current_rate,
            "previous_rate": previous_rate,
            "trend": trend,
            "current_sample_size": current_count,
            "previous_sample_size": previous_count,
            "recurrence_rate": recurrence_rate,
            "previous_recurrence_rate": previous_recurrence_rate,
        }

    # Update per-category window history for persistent-issue tracking.
    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing_eff: dict = dict(doc.get("review_effectiveness") or {})
    existing_computed_at: Optional[str] = existing_eff.get("computed_at")

    is_new_window = True
    if existing_computed_at:
        try:
            last_dt = datetime.fromisoformat(existing_computed_at)
            is_new_window = (now - last_dt).days >= 14
        except (ValueError, TypeError):
            pass

    existing_cats: dict = dict(existing_eff.get("categories") or {})
    for cat, data in categories.items():
        prev_cat: dict = dict(existing_cats.get(cat) or {})
        rate_history: list = list(prev_cat.get("rate_history") or [])
        if is_new_window or not rate_history:
            rate_history.append(data["current_rate"])
            rate_history = rate_history[-5:]
        data["rate_history"] = rate_history

        recurrence_history: list = list(prev_cat.get("recurrence_history") or [])
        if (is_new_window or not recurrence_history) and data[
            "recurrence_rate"
        ] is not None:
            recurrence_history.append(data["recurrence_rate"])
            recurrence_history = recurrence_history[-5:]
        data["recurrence_history"] = recurrence_history

    # Categories above 20 % in each of their last 3 stored windows are flagged as
    # persistent — a signal that structural changes may be needed rather than
    # relying on agent-level awareness.
    persistent_issues = [
        cat
        for cat, data in categories.items()
        if len(data["rate_history"]) >= 3
        and all(r > 0.20 for r in data["rate_history"][-3:])
    ]

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "categories": categories,
        "persistent_issues": persistent_issues,
    }

    doc["review_effectiveness"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[REVIEW_EFFECTIVENESS] project=%s categories=%d persistent=%s",
        project_id,
        len(categories),
        persistent_issues,
    )
    return result


# ---------------------------------------------------------------------------
# SR catch-rate trend tracking
# ---------------------------------------------------------------------------

# Minimum number of tasks with BOTH self-review check_results AND human
# rejection categories required per window.  Half the human-review threshold
# since SR data is sparser.
_MIN_SR_SAMPLE = 5
# Minimum total reviewer findings (across categories in a window) required for
# the catch-rate and its cycle-over-cycle trend to be considered well-sampled.
# Matches _MIN_SR_SAMPLE so the gate is on the true catch-rate denominator
# (finding count) rather than task count; a window with ≥_MIN_SR_SAMPLE tasks
# may still have fewer findings when most tasks flag zero or one category.
_MIN_SR_FINDINGS = 5
# Minimum total reviewer findings (across tasks in a window) required to emit
# a hallucination-rate value; prevents noisy rates on tiny windows.
_HALLUCINATION_RATE_MIN_FINDINGS = 10
_MIN_DIRECTION_SR_SAMPLE = 3

# Example-collection caps for miss-example surfacing in the agent prompt.
_EXAMPLE_MAX_PER_CAT = 3
_EXAMPLE_MAX_CATS = 5
_EXAMPLE_MAX_DESC_LEN = 100

# Tokens that indicate a description may contain sensitive material.
# Case-insensitive substring match; keep as tuple for fast `in` checks.
_SENSITIVE_TOKENS: tuple[str, ...] = (
    "password",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "bearer",
    "authorization:",
    "private_key",
    "sk-ant-",
    "eyJ",
)

_URL_RE = re.compile(r"https?://\S+")
_PR_REF_RE = re.compile(r"\b(?:PR|pr)\s*#?\s*\d+\b|#\s*\d+\b")


def _is_sensitive_desc(desc: str) -> bool:
    """Return True if *desc* appears to mention credentials or secrets."""
    lower = desc.lower()
    return any(tok in lower for tok in _SENSITIVE_TOKENS)


def _clean_example_desc(desc: str) -> Optional[str]:
    """Strip URLs and PR refs, collapse whitespace, truncate to _EXAMPLE_MAX_DESC_LEN."""
    desc = _URL_RE.sub("", desc)
    desc = _PR_REF_RE.sub("", desc)
    desc = " ".join(desc.split())
    if not desc:
        return None
    if len(desc) > _EXAMPLE_MAX_DESC_LEN:
        desc = desc[: _EXAMPLE_MAX_DESC_LEN - 3].rstrip() + "..."
    return desc


def _finalize_examples(raw_examples: list[dict]) -> list[dict]:
    """Sort by severity (CRITICAL first), filter sensitive, clean, deduplicate, cap."""
    _sev_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    sorted_ex = sorted(raw_examples, key=lambda x: _sev_order.get(x["severity"], 3))
    seen: set[str] = set()
    result: list[dict] = []
    for ex in sorted_ex:
        cleaned = _clean_example_desc(ex["desc"])
        if not cleaned or _is_sensitive_desc(cleaned):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append({"severity": ex["severity"], "desc": cleaned})
        if len(result) >= _EXAMPLE_MAX_PER_CAT:
            break
    return result


_SR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}
_EDGE_CATS = {"correctness", "error-handling"}


def _partition_edges_by_origin(edges: list) -> "tuple[list, list]":
    """Split edges_considered into (author, reviewer) entries.

    Entries without an explicit ``origin`` field count as author entries.
    Non-dict entries are dropped.
    """
    author: list = []
    reviewer: list = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if e.get("origin") == "reviewer":
            reviewer.append(e)
        else:
            author.append(e)
    return author, reviewer


def _reviewer_caught_author_miss(author_entries: list, reviewer_entries: list) -> bool:
    """Whether the reviewer caught a case the author missed in this task.

    A catch is either a reviewer entry whose ``case`` (lowercased + trimmed)
    is absent from the author entries, OR a reviewer entry that flags
    ``handled: false`` for a case the author had marked ``handled: true``.
    Entries with missing/non-string ``case`` are skipped.
    """

    def _norm(c: Any) -> Optional[str]:
        return c.strip().lower() if isinstance(c, str) and c.strip() else None

    author_handled: dict[str, bool] = {}
    for e in author_entries:
        c = _norm(e.get("case"))
        if c is None:
            continue
        h = bool(e.get("handled", True))
        author_handled[c] = author_handled.get(c, False) or h

    for e in reviewer_entries:
        c = _norm(e.get("case"))
        if c is None:
            continue
        if c not in author_handled:
            return True
        if author_handled[c] and not bool(e.get("handled", True)):
            return True
    return False


def _compute_edges_stats(tasks: list) -> Optional[dict[str, Any]]:
    """Compute edges_considered rolling metrics for a task list.

    Returns None when fewer than 3 qualifying code-change tasks with
    ``_self_review_outcome`` exist. When sufficient data is present, returns::

        {
          "sample_size": int,
          "tasks_with_edges_rate": float,
          "avg_edges_per_task": float | None,
          "unhandled_edge_flagged_rate": float | None,
          "correctness_rejection_rate_with_edges": float | None,
          "correctness_rejection_rate_without_edges": float | None,
        }

    The last two fields correlate tasks that enumerated edges vs. those that
    didn't against subsequent correctness/error-handling reviewer flags,
    enabling validation that edges_considered actually improves catch rate.
    """
    code_tasks = [
        t
        for t in tasks
        if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
        and "_self_review_outcome" in (t.context or {})
    ]
    if len(code_tasks) < 3:
        return None

    with_edges: list = []
    without_edges: list = []
    with_unhandled_flag: list = []
    with_all_handled: list = []
    total_edges = 0
    unhandled_flagged = 0
    total_author_edges = 0
    total_reviewer_edges = 0
    n_with_author = 0
    n_with_reviewer = 0
    reviewer_caught = 0
    for t in code_tasks:
        edges = (t.context or {}).get("_self_review_outcome", {}).get(
            "edges_considered"
        ) or []
        if not isinstance(edges, list):
            edges = []
        author_e, reviewer_e = _partition_edges_by_origin(edges)
        total_author_edges += len(author_e)
        total_reviewer_edges += len(reviewer_e)
        if author_e:
            n_with_author += 1
            if any(not e.get("handled", True) for e in author_e):
                with_unhandled_flag.append(t)
            else:
                with_all_handled.append(t)
        if reviewer_e:
            n_with_reviewer += 1
            if _reviewer_caught_author_miss(author_e, reviewer_e):
                reviewer_caught += 1
        if edges:
            with_edges.append(t)
            total_edges += len(edges)
            if any(not e.get("handled", True) for e in edges if isinstance(e, dict)):
                unhandled_flagged += 1
        else:
            without_edges.append(t)

    def _rejection_rate(task_list: list) -> Optional[float]:
        measured = [
            t
            for t in task_list
            if "_first_attempt_rejection_categories" in (t.context or {})
        ]
        if not measured:
            return None
        flagged = sum(
            1
            for t in measured
            if _EDGE_CATS
            & set(t.context.get("_first_attempt_rejection_categories") or [])
        )
        return round(flagged / len(measured), 3)

    n_with = len(with_edges)
    n_total = len(code_tasks)
    return {
        "sample_size": n_total,
        "tasks_with_edges_rate": round(n_with / n_total, 3),
        "avg_edges_per_task": round(total_edges / n_with, 2) if n_with else None,
        "unhandled_edge_flagged_rate": round(unhandled_flagged / n_with, 3)
        if n_with
        else None,
        "correctness_rejection_rate_with_edges": _rejection_rate(with_edges),
        "correctness_rejection_rate_without_edges": _rejection_rate(without_edges),
        "correctness_rejection_rate_with_unhandled_flag": _rejection_rate(
            with_unhandled_flag
        ),
        "correctness_rejection_rate_with_all_handled": _rejection_rate(
            with_all_handled
        ),
        "author_avg_edges_per_task": round(total_author_edges / n_with_author, 2)
        if n_with_author
        else None,
        "reviewer_avg_edges_per_task": round(total_reviewer_edges / n_with_reviewer, 2)
        if n_with_reviewer
        else None,
        "tasks_with_reviewer_edges_count": n_with_reviewer,
        "tasks_with_reviewer_edges_rate": round(n_with_reviewer / n_total, 3),
        "reviewer_caught_count": reviewer_caught,
        "reviewer_caught_rate": round(reviewer_caught / n_total, 3),
    }


def _stats_for_tasks(
    tasks: list,
) -> tuple[
    int,
    dict[str, dict],
    dict[str, dict[str, dict]],
    dict[str, dict],
    dict[str, dict],
    dict[str, dict[str, dict]],
]:
    """Compute SR stats for a pre-filtered task list (no date filtering).

    Returns (count, cat_stats, type_cat_stats, all_iter_cat_stats, sev_stats,
    scope_cat_stats). ``all_iter_cat_stats`` uses ALL review iterations as the
    human-truth set (not just iteration 0). Falls back to first-attempt
    categories when ``review_loop.review_history`` is absent (backward compat).
    ``sev_stats`` accumulates per-severity hit/miss counts from review_history
    issues across all categories. ``scope_cat_stats`` mirrors
    ``type_cat_stats`` keyed by task scope (small|medium|large) instead of
    task type, using ``task.context['scope']`` (default: small).
    """
    _sw = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}

    def _origin_bucket(
        entry: dict[str, Any] | None,
    ) -> Literal["sr_alone", "reviewer_save"]:
        origin = entry.get("origin") if isinstance(entry, dict) else None
        if isinstance(origin, str) and origin in _ALL_REVIEWER_ORIGINS:
            return "reviewer_save"
        return "sr_alone"

    count = 0
    cat_stats: dict[str, dict] = {}
    type_cat_stats: dict[str, dict[str, dict]] = {}
    all_iter_cat_stats: dict[str, dict] = {}
    sev_stats: dict[str, dict] = {}
    scope_cat_stats: dict[str, dict[str, dict]] = {}
    for t in tasks:
        ctx = t.context or {}
        if "_self_review_outcome" not in ctx:
            continue
        check_results = ctx["_self_review_outcome"].get("check_results") or {}
        # Combined view (author SR + reviewer save) — feeds hit/origin-split
        # counters below where _origin_bucket separates `sr_alone` vs
        # `reviewer_save`, so reviewer-stamped entries must remain present.
        sr_failed = {c for c, r in check_results.items() if not r.get("passed", True)}
        # Author-only view — used for FP/sr_failed_count attribution. A
        # reviewer-stamped failure does not represent something the author SR
        # caught, so it must not inflate the precision/false-positive metrics.
        sr_failed_author = {
            c
            for c in sr_failed
            if (check_results.get(c) or {}).get("origin") not in _ALL_REVIEWER_ORIGINS
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        cat_weights: dict[str, int] = {}
        rev_cats: dict[str, list[dict]] = {}
        if rev_hist:
            rev_cats = _categorize_review_issues(rev_hist)
            for cat_name, issues in rev_cats.items():
                cat_weights[cat_name] = max(_sw.get(i["severity"], 1) for i in issues)
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})

        # FP tracking: among SR-failed categories, count those never confirmed by
        # any human reviewer across all review iterations (first rejection OR later).
        # Runs for every task (even those with no first-attempt rejection) because
        # those are exactly the cases where SR may have flagged a false positive.
        # Uses the author-only view so reviewer-stamped overrides are not
        # falsely attributed to the author SR's precision.
        all_human_flagged = set(rev_cats.keys()) | human_cats
        for cat in sr_failed_author:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "sr_alone_hit_count": 0,
                    "reviewer_save_hit_count": 0,
                    "human_count": 0,
                    "w_hit": 0.0,
                    "w_total": 0.0,
                    "miss_examples": [],
                    "sr_failed_count": 0,
                    "fp_count": 0,
                }
            cat_stats[cat].setdefault("sr_failed_count", 0)
            cat_stats[cat].setdefault("fp_count", 0)
            cat_stats[cat]["sr_failed_count"] += 1
            if cat not in all_human_flagged:
                cat_stats[cat]["fp_count"] += 1

        if not human_cats:
            continue
        count += 1
        for cat_name, issues in rev_cats.items():
            for issue in issues:
                sev = issue.get("severity", "")
                if sev not in _sw:
                    continue
                if sev not in sev_stats:
                    sev_stats[sev] = {"hit_count": 0, "human_count": 0}
                sev_stats[sev]["human_count"] += 1
                if cat_name in sr_failed:
                    sev_stats[sev]["hit_count"] += 1
        task_type = t.first_payload.get("type", "")
        task_scope = (t.context or {}).get("scope", DEFAULT_TASK_SCOPE)
        for cat in human_cats:
            if cat not in cat_stats:
                cat_stats[cat] = {
                    "hit_count": 0,
                    "sr_alone_hit_count": 0,
                    "reviewer_save_hit_count": 0,
                    "human_count": 0,
                    "w_hit": 0.0,
                    "w_total": 0.0,
                    "miss_examples": [],
                    "sr_failed_count": 0,
                    "fp_count": 0,
                }
            cat_stats[cat]["human_count"] += 1
            w = cat_weights.get(cat, 1)
            cat_stats[cat]["w_total"] += w
            if cat in sr_failed:
                cat_stats[cat]["hit_count"] += 1
                cat_stats[cat]["w_hit"] += w
                bucket = _origin_bucket(check_results.get(cat))
                if bucket == "sr_alone":
                    cat_stats[cat]["sr_alone_hit_count"] += 1
                else:
                    cat_stats[cat]["reviewer_save_hit_count"] += 1
            else:
                for issue in rev_cats.get(cat, []):
                    cat_stats[cat]["miss_examples"].append(
                        {"desc": issue["desc"], "severity": issue["severity"]}
                    )
            if task_type:
                if task_type not in type_cat_stats:
                    type_cat_stats[task_type] = {}
                if cat not in type_cat_stats[task_type]:
                    type_cat_stats[task_type][cat] = {
                        "hit_count": 0,
                        "sr_alone_hit_count": 0,
                        "reviewer_save_hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                type_cat_stats[task_type][cat]["human_count"] += 1
                if cat in sr_failed:
                    type_cat_stats[task_type][cat]["hit_count"] += 1
                    bucket = _origin_bucket(check_results.get(cat))
                    if bucket == "sr_alone":
                        type_cat_stats[task_type][cat]["sr_alone_hit_count"] += 1
                    else:
                        type_cat_stats[task_type][cat]["reviewer_save_hit_count"] += 1
                else:
                    for issue in rev_cats.get(cat, []):
                        type_cat_stats[task_type][cat]["miss_examples"].append(
                            {"desc": issue["desc"], "severity": issue["severity"]}
                        )
            if task_scope:
                if task_scope not in scope_cat_stats:
                    scope_cat_stats[task_scope] = {}
                if cat not in scope_cat_stats[task_scope]:
                    scope_cat_stats[task_scope][cat] = {
                        "hit_count": 0,
                        "sr_alone_hit_count": 0,
                        "reviewer_save_hit_count": 0,
                        "human_count": 0,
                        "miss_examples": [],
                    }
                scope_cat_stats[task_scope][cat]["human_count"] += 1
                if cat in sr_failed:
                    scope_cat_stats[task_scope][cat]["hit_count"] += 1
                    bucket = _origin_bucket(check_results.get(cat))
                    if bucket == "sr_alone":
                        scope_cat_stats[task_scope][cat]["sr_alone_hit_count"] += 1
                    else:
                        scope_cat_stats[task_scope][cat]["reviewer_save_hit_count"] += 1
                else:
                    for issue in rev_cats.get(cat, []):
                        scope_cat_stats[task_scope][cat]["miss_examples"].append(
                            {"desc": issue["desc"], "severity": issue["severity"]}
                        )
        # All-iterations human truth: prefer persisted _all_iterations_rejection_categories
        # (written by task_lifecycle on every rejection iteration); fall back to
        # categorising review_history in-line, then to first-attempt categories.
        _stored_all_iter = ctx.get("_all_iterations_rejection_categories") or {}
        if _stored_all_iter:
            all_iter_cats = set(_stored_all_iter.keys())
        elif rev_hist:
            all_iter_cats = set(_categorize_review_issues_keyword(rev_hist).keys())
        else:
            all_iter_cats = human_cats
        for cat in all_iter_cats:
            if cat not in all_iter_cat_stats:
                all_iter_cat_stats[cat] = {"hit_count": 0, "human_count": 0}
            all_iter_cat_stats[cat]["human_count"] += 1
            if cat in sr_failed:
                all_iter_cat_stats[cat]["hit_count"] += 1
    return (
        count,
        cat_stats,
        type_cat_stats,
        all_iter_cat_stats,
        sev_stats,
        scope_cat_stats,
    )


def _sr_skip_stats(
    tasks: list,
) -> tuple[Optional[float], Optional[dict[str, int]]]:
    """Compute skip-rate and reason distribution over tasks-with-SR-outcome.

    Denominator: tasks that have ``_self_review_outcome`` in their context.
    Numerator: tasks whose outcome is a fallback envelope (structured
    ``skip_reason`` present OR verdict=='unknown' with empty check_results).
    Returns (None, None) when the sample is below _MIN_SR_SAMPLE.
    """
    total = 0
    dist: dict[str, int] = {}
    for t in tasks:
        ctx = t.context or {}
        outcome = ctx.get("_self_review_outcome")
        if outcome is None:
            continue
        total += 1
        reason = outcome.get("skip_reason")
        if reason:
            dist[str(reason)] = dist.get(str(reason), 0) + 1
        elif outcome.get("verdict") == "unknown" and not (
            outcome.get("check_results") or {}
        ):
            dist["legacy_unknown"] = dist.get("legacy_unknown", 0) + 1
    if total < _MIN_SR_SAMPLE:
        return None, None
    skipped = sum(dist.values())
    return round(skipped / total, 3), dist


def _compute_execution_evidence_miss_rates(
    tasks: list,
) -> dict[str, dict[str, Any]]:
    """Aggregate miss-rates for execution-evidence SR categories.

    For each task that has ``_self_review_outcome`` with ``check_results``,
    count how many include each execution-evidence category and how many
    reported ``passed: false``. Returns miss_rate, miss_count, sample_count
    per category; omits categories with zero samples.
    """
    totals: dict[str, dict[str, int]] = {}
    for t in tasks:
        ctx = t.context or {}
        check_results = (ctx.get("_self_review_outcome") or {}).get(
            "check_results"
        ) or {}
        for cat in _EXECUTION_EVIDENCE_CATEGORIES:
            result = check_results.get(cat)
            if result is None:
                continue
            if cat not in totals:
                totals[cat] = {"sample_count": 0, "miss_count": 0}
            totals[cat]["sample_count"] += 1
            if not result.get("passed", True):
                totals[cat]["miss_count"] += 1
    out: dict[str, dict[str, Any]] = {}
    for cat, s in totals.items():
        if s["sample_count"] == 0:
            continue
        out[cat] = {
            "miss_rate": round(s["miss_count"] / s["sample_count"], 3),
            "miss_count": s["miss_count"],
            "sample_count": s["sample_count"],
        }
    return out


def compute_sr_catch_rate_by_direction(
    all_tasks: list,
    *,
    existing_by_direction: Optional[dict] = None,
    is_new_window: bool = True,
    direction_text_map: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-direction SR catch-rate breakdown.

    Groups *all_tasks* by ``context.direction_id`` and, for each direction with
    at least :data:`_MIN_DIRECTION_SR_SAMPLE` qualifying tasks (both
    ``_self_review_outcome`` and ``_first_attempt_rejection_categories`` present),
    computes category-level miss rates and sample counts.

    When *existing_by_direction* is provided (a previously persisted
    ``by_direction`` snapshot), each entry is enriched with cycle-over-cycle
    trend fields: ``previous_miss_rate``, ``trend`` ('increasing' | 'decreasing'
    | 'stable'), and ``rate_history`` (list of recent miss_rates, capped at 5).
    *is_new_window* controls whether the current measurement is appended to
    ``rate_history`` (mirrors the per-category rate_history logic).

    When *direction_text_map* is provided, each entry gains a ``direction_text``
    field (truncated to 120 chars) so investigation prompts can reference the
    human-readable direction title without a secondary lookup.

    Returns a dict keyed by direction_id::

        {
          "d-abc123": {
            "sample_count": 5,
            "miss_rate": 0.6,
            "direction_text": "...",  # only when direction_text_map provided
            "categories": {
              "regression_risk": {
                "hit_rate": 0.4,
                "miss_rate": 0.6,
                "sample_count": 5,
                "examples": [...]   # only when misses exist
              },
              ...
            }
          },
          ...
        }

    Directions with fewer than _MIN_DIRECTION_SR_SAMPLE qualifying tasks are
    omitted (insufficient signal). Both directions and categories are sorted
    worst-first by miss rate for stable admin/UI rendering.
    """
    by_direction: dict[str, list] = {}
    for t in all_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            by_direction.setdefault(did, []).append(t)

    result: dict[str, dict[str, Any]] = {}
    for direction_id, tasks in by_direction.items():
        count, cat_stats, _, _, _, _ = _stats_for_tasks(tasks)
        if count < _MIN_DIRECTION_SR_SAMPLE:
            continue
        categories: dict[str, Any] = {}
        for cat, stats in cat_stats.items():
            if stats["human_count"] == 0:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
            }
            ex = _finalize_examples(stats.get("miss_examples", []))
            if ex:
                entry["examples"] = ex
            categories[cat] = entry
        if categories:
            categories = dict(
                sorted(
                    categories.items(),
                    key=lambda item: (-item[1]["miss_rate"], item[0]),
                )
            )
            total_samples = sum(entry["sample_count"] for entry in categories.values())
            weighted_miss_rate = (
                round(
                    sum(
                        entry["miss_rate"] * entry["sample_count"]
                        for entry in categories.values()
                    )
                    / total_samples,
                    3,
                )
                if total_samples
                else None
            )
            overall_catch_rate = (
                round(1 - weighted_miss_rate, 3)
                if weighted_miss_rate is not None
                else None
            )
            entry: dict[str, Any] = {
                "sample_count": count,
                "miss_rate": weighted_miss_rate,
                "overall_catch_rate": overall_catch_rate,
                "categories": categories,
            }
            if direction_text_map:
                raw_text = direction_text_map.get(direction_id, "")
                if raw_text:
                    entry["direction_text"] = raw_text[:120]
            if existing_by_direction is not None:
                prev_entry: dict = dict(existing_by_direction.get(direction_id) or {})
                prev_miss_rate = prev_entry.get("miss_rate")
                rate_history: list = list(prev_entry.get("rate_history") or [])
                if (
                    is_new_window or not rate_history
                ) and weighted_miss_rate is not None:
                    rate_history.append(weighted_miss_rate)
                    rate_history = rate_history[-5:]
                if prev_miss_rate is not None and weighted_miss_rate is not None:
                    delta = weighted_miss_rate - prev_miss_rate
                    if delta >= _TREND_DELTA_THRESHOLD:
                        dir_trend = "increasing"
                    elif delta <= -_TREND_DELTA_THRESHOLD:
                        dir_trend = "decreasing"
                    else:
                        dir_trend = "stable"
                else:
                    dir_trend = "stable"
                entry["previous_miss_rate"] = prev_miss_rate
                entry["trend"] = dir_trend
                entry["rate_history"] = rate_history
            result[direction_id] = entry
    return dict(
        sorted(result.items(), key=lambda item: (-(item[1]["miss_rate"] or 0), item[0]))
    )


def compute_direction_sr_breakdown_for_endpoint(
    all_tasks: list,
    direction_text_map: dict[str, str],
    *,
    existing_by_direction: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """Endpoint-shaped per-direction SR breakdown.

    Wraps :func:`compute_sr_catch_rate_by_direction` and decorates each entry
    with ``direction_text`` (looked up from *direction_text_map*), exposes
    ``sample_size`` (alias of canonical ``sample_count``), and adds
    ``worst_category`` derived from the canonical worst-first ordering of
    categories. Returned list preserves canonical worst-first direction
    ordering.
    """
    canonical = compute_sr_catch_rate_by_direction(
        all_tasks, existing_by_direction=existing_by_direction
    )
    out: list[dict[str, Any]] = []
    for direction_id, entry in canonical.items():
        categories = entry.get("categories") or {}
        worst_category = next(iter(categories), None)
        out.append(
            {
                "direction_id": direction_id,
                "direction_text": direction_text_map.get(direction_id, ""),
                "sample_size": entry["sample_count"],
                "overall_catch_rate": entry.get("overall_catch_rate"),
                "categories": categories,
                "worst_category": worst_category,
            }
        )
    return out


def select_sr_miss_examples_for_injection(
    sr_catch_rate_trends: dict,
    direction_id: Optional[str] = None,
    *,
    max_cats: int = 3,
    max_examples_per_cat: int = 3,
    max_total_chars: int = 1200,
) -> dict[str, list[dict]]:
    """Select miss examples for agent SR-prompt injection.

    When *direction_id* is provided and the per-direction snapshot in
    ``by_direction`` contains at least one category with non-empty examples,
    those direction-scoped categories are used as candidates.  Falls back to
    the global top categories when the direction has no qualifying examples.

    Returns ``{category: [{severity, desc}, ...]}`` bounded by *max_cats*,
    *max_examples_per_cat*, and *max_total_chars*.
    """
    global_cats = sr_catch_rate_trends.get("categories") or {}
    if not isinstance(global_cats, dict):
        return {}

    candidate_cats: dict = global_cats
    if direction_id:
        dir_entry = (sr_catch_rate_trends.get("by_direction") or {}).get(
            str(direction_id)
        ) or {}
        dir_cats = dir_entry.get("categories")
        if isinstance(dir_cats, dict) and any(
            isinstance(v, dict)
            and isinstance(v.get("examples"), list)
            and v["examples"]
            for v in dir_cats.values()
        ):
            candidate_cats = dir_cats

    ranked = sorted(
        [
            (cat, data)
            for cat, data in candidate_cats.items()
            if isinstance(data, dict)
            and isinstance(data.get("examples"), list)
            and data["examples"]
        ],
        key=lambda x: x[1].get("weighted_miss_rate") or x[1].get("miss_rate") or 0.0,
        reverse=True,
    )[:max_cats]

    result: dict[str, list[dict]] = {}
    total_chars = 0
    for cat, data in ranked:
        cat_exs: list[dict] = []
        for ex in data["examples"][:max_examples_per_cat]:
            desc = (str(ex.get("desc") or ""))[:200]
            if not desc:
                continue
            if total_chars + len(desc) > max_total_chars:
                break
            total_chars += len(desc)
            cat_exs.append({"severity": ex.get("severity", "MINOR"), "desc": desc})
        if cat_exs:
            result[cat] = cat_exs
        if total_chars >= max_total_chars:
            break
    return result


def _compute_cat_fp_stats(
    tasks: list,
) -> tuple[
    dict[str, dict[str, int]],
    dict[str, dict[str, dict[str, int]]],
    dict[str, dict[str, dict[str, int]]],
]:
    """Return per-category fp/tp counts for SR failures.

    A category counted as TP when ANY downstream signal independently
    confirms the SR failure: reviewer_findings, _first_attempt_rejection_categories,
    or review_loop.review_history (keyword-categorised). Otherwise FP.
    Returns (cat_fp, type_cat_fp, scope_cat_fp) where keys are category,
    (task_type, category), and (task_scope, category) respectively.
    """
    cat_fp: dict[str, dict[str, int]] = {}
    type_cat_fp: dict[str, dict[str, dict[str, int]]] = {}
    scope_cat_fp: dict[str, dict[str, dict[str, int]]] = {}
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro:
            continue
        check_results = sro.get("check_results") or {}
        # Author SR failures only — exclude reviewer-stamped overrides so a
        # reviewer rescue is never attributed to the author SR as a TP/FP.
        sr_failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict)
            and not r.get("passed", True)
            and r.get("origin") not in _ALL_REVIEWER_ORIGINS
        }
        if not sr_failed:
            continue
        reviewer_cats = {
            f.get("category")
            for f in (sro.get("reviewer_findings") or [])
            if isinstance(f, dict) and f.get("category")
        }
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        rev_hist_cats: set[str] = set()
        if rev_hist:
            rev_hist_cats = set(_categorize_review_issues_keyword(rev_hist).keys())
        confirmed = reviewer_cats | human_cats | rev_hist_cats
        task_scope = ctx.get("scope", DEFAULT_TASK_SCOPE)
        for cat in sr_failed:
            bucket = cat_fp.setdefault(cat, {"fp": 0, "tp": 0})
            tbucket = type_cat_fp.setdefault(task_type, {}).setdefault(
                cat, {"fp": 0, "tp": 0}
            )
            sbucket = scope_cat_fp.setdefault(task_scope, {}).setdefault(
                cat, {"fp": 0, "tp": 0}
            )
            if cat in confirmed:
                bucket["tp"] += 1
                tbucket["tp"] += 1
                sbucket["tp"] += 1
            else:
                bucket["fp"] += 1
                tbucket["fp"] += 1
                sbucket["fp"] += 1
    return cat_fp, type_cat_fp, scope_cat_fp


def _compute_cat_severity_calibration(
    tasks: list,
) -> dict[str, dict[str, int]]:
    """Return per-category severity calibration counts.

    For categories where the agent SR flagged a concern (passed=False, has
    severity) AND the reviewer independently flagged the same category in
    review_history, compare the agent's severity against the reviewer's
    maximum severity for that category.

    Returns {cat: {"under_rated": n, "over_rated": n, "matched": n, "total": n}}.
    Under-rated = agent severity numerically lower than reviewer's — more
    dangerous than over-rating because it suppresses escalation.
    """
    _sw = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    result: dict[str, dict[str, int]] = {}
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro:
            continue
        check_results = sro.get("check_results") or {}
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        if not rev_hist:
            continue
        rev_cats = _categorize_review_issues(rev_hist)
        for cat, rev_issues in rev_cats.items():
            cr = check_results.get(cat)
            if not isinstance(cr, dict):
                continue
            if cr.get("passed", True):
                continue
            if cr.get("origin") in _ALL_REVIEWER_ORIGINS:
                continue
            agent_sev = (cr.get("severity") or "").upper()
            if agent_sev not in _sw:
                continue
            sev_vals = [
                _sw[i.get("severity", "")]
                for i in rev_issues
                if isinstance(i, dict) and i.get("severity", "") in _sw
            ]
            if not sev_vals:
                continue
            reviewer_max_val = max(sev_vals)
            agent_val = _sw[agent_sev]
            bucket = result.setdefault(
                cat,
                {"under_rated": 0, "over_rated": 0, "matched": 0, "total": 0},
            )
            bucket["total"] += 1
            if agent_val < reviewer_max_val:
                bucket["under_rated"] += 1
            elif agent_val > reviewer_max_val:
                bucket["over_rated"] += 1
            else:
                bucket["matched"] += 1
    return result


def _task_prompt_fingerprint(task: Task) -> str:
    """Resolve the task's prompt fingerprint with legacy context fallback."""
    if isinstance(task.prompt_fingerprint, str) and task.prompt_fingerprint:
        return task.prompt_fingerprint
    ctx = task.context or {}
    legacy = ctx.get("prompt_fingerprint")
    if isinstance(legacy, str) and legacy:
        return legacy
    return "unknown"


def compute_sr_quality_by_fingerprint(
    tasks: list,
    *,
    min_sample_size: int = 3,
    top_disagreement_categories: int = 3,
) -> dict[str, dict[str, Any]]:
    """Compute additive SR-quality metrics for prompt fingerprint analytics."""
    from app.sr_blind_spots import is_skipped_sr_outcome  # noqa: PLC0415

    buckets: dict[str, dict[str, Any]] = {}
    project_catch_hits = 0
    project_catch_total = 0
    for task in tasks:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        fp = _task_prompt_fingerprint(task)
        bucket = buckets.setdefault(
            fp,
            {
                "tasks": [],
                "sr_sample_size": 0,
                "catch_hits": 0,
                "catch_total": 0,
                "reviewer_rescues": 0,
                "reviewer_rescue_total": 0,
                "disagreements": {},
            },
        )
        bucket["tasks"].append(task)
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        bucket["sr_sample_size"] += 1
        check_results = sro.get("check_results") or {}
        sr_failed = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict)
            and not result.get("passed", True)
            and result.get("origin") not in _ALL_REVIEWER_ORIGINS
        }
        reviewer_cats = {
            finding.get("category")
            for finding in (sro.get("reviewer_findings") or [])
            if isinstance(finding, dict) and finding.get("category")
        }
        rev_hist = (ctx.get("review_loop") or {}).get("review_history") or []
        confirmed = (
            reviewer_cats
            | set(ctx.get("_first_attempt_rejection_categories") or {})
            | set(_categorize_review_issues(rev_hist).keys())
        )
        hits = len(confirmed & sr_failed)
        misses = confirmed - sr_failed
        bucket["catch_hits"] += hits
        bucket["catch_total"] += len(confirmed)
        bucket["reviewer_rescue_total"] += len(misses)
        bucket["reviewer_rescues"] += len(misses & reviewer_cats)
        project_catch_hits += hits
        project_catch_total += len(confirmed)
        for cat in reviewer_cats - sr_failed:
            entry = bucket["disagreements"].setdefault(
                cat, {"reviewer_overrode_sr_count": 0, "sr_overrode_reviewer_count": 0}
            )
            entry["reviewer_overrode_sr_count"] += 1
        for cat in sr_failed - reviewer_cats:
            entry = bucket["disagreements"].setdefault(
                cat, {"reviewer_overrode_sr_count": 0, "sr_overrode_reviewer_count": 0}
            )
            entry["sr_overrode_reviewer_count"] += 1

    project_catch_rate = (
        round(project_catch_hits / project_catch_total, 3)
        if project_catch_total
        else None
    )
    results: dict[str, dict[str, Any]] = {}
    for fp, bucket in buckets.items():
        result: dict[str, Any] = {"sr_sample_size": bucket["sr_sample_size"]}
        if bucket["sr_sample_size"] < min_sample_size:
            result.update(
                {
                    "sr_catch_rate": None,
                    "sr_catch_sample_size": bucket["catch_total"],
                    "reviewer_rescue_rate": None,
                    "reviewer_rescue_sample_size": bucket["reviewer_rescue_total"],
                    "sr_false_positive_rate": None,
                    "sr_false_positive_sample_size": 0,
                    "sr_false_positive_rate_by_category": [],
                    "sr_reviewer_disagreement_top_categories": [],
                    "sr_catch_rate_delta_vs_project_mean": None,
                }
            )
            results[fp] = result
            continue
        cat_fp_stats, _, _ = _compute_cat_fp_stats(bucket["tasks"])
        fp_total = sum(stats.get("fp", 0) for stats in cat_fp_stats.values())
        tp_total = sum(stats.get("tp", 0) for stats in cat_fp_stats.values())
        catch_rate = (
            round(bucket["catch_hits"] / bucket["catch_total"], 3)
            if bucket["catch_total"]
            else None
        )
        result.update(
            {
                "sr_catch_rate": catch_rate,
                "sr_catch_sample_size": bucket["catch_total"],
                "reviewer_rescue_rate": (
                    round(
                        bucket["reviewer_rescues"] / bucket["reviewer_rescue_total"], 3
                    )
                    if bucket["reviewer_rescue_total"]
                    else None
                ),
                "reviewer_rescue_sample_size": bucket["reviewer_rescue_total"],
                "sr_false_positive_rate": (
                    round(fp_total / (fp_total + tp_total), 3)
                    if (fp_total + tp_total)
                    else None
                ),
                "sr_false_positive_sample_size": fp_total + tp_total,
                "sr_false_positive_rate_by_category": [
                    {
                        "category": cat,
                        "sample_size": stats.get("fp", 0) + stats.get("tp", 0),
                        "false_positive_rate": round(
                            stats.get("fp", 0)
                            / (stats.get("fp", 0) + stats.get("tp", 0)),
                            3,
                        ),
                        "fp_count": stats.get("fp", 0),
                        "tp_count": stats.get("tp", 0),
                    }
                    for cat, stats in sorted(
                        cat_fp_stats.items(),
                        key=lambda item: (
                            -(item[1].get("fp", 0) + item[1].get("tp", 0)),
                            item[0],
                        ),
                    )
                ],
                "sr_reviewer_disagreement_top_categories": [
                    {
                        "category": cat,
                        "sample_size": counts["reviewer_overrode_sr_count"]
                        + counts["sr_overrode_reviewer_count"],
                        **counts,
                    }
                    for cat, counts in sorted(
                        bucket["disagreements"].items(),
                        key=lambda item: (
                            -(
                                item[1]["reviewer_overrode_sr_count"]
                                + item[1]["sr_overrode_reviewer_count"]
                            ),
                            item[0],
                        ),
                    )[:top_disagreement_categories]
                ],
                "sr_catch_rate_delta_vs_project_mean": (
                    round(catch_rate - project_catch_rate, 3)
                    if catch_rate is not None and project_catch_rate is not None
                    else None
                ),
            }
        )
        results[fp] = result
    return results


def _score_reasoning_quality(reasoning: str) -> float:
    """Score reasoning quality on a 0.0–1.0 scale based on specificity signals.

    Rewards: file path citation, test/function name mention, minimum word count (≥6).
    """
    import re

    if not reasoning or not isinstance(reasoning, str):
        return 0.0

    text = reasoning.strip()
    if not text:
        return 0.0

    score = 0.0
    signals = 0

    # Signal 1: file path citation (e.g., "backend/app/foo.py" or "app/foo.py")
    if re.search(r"\b(?:[\w\-./]*[\w\-/]+\.[\w]+)\b", text):
        score += 0.33
        signals += 1

    # Signal 2: test/function name mention (e.g., "test_foo", "test_*.py", "::function")
    if re.search(r"(?:test_\w+|_test\.py|\:\:\w+|\w+::)", text):
        score += 0.33
        signals += 1

    # Signal 3: minimum word count (≥6 words)
    word_count = len(text.split())
    if word_count >= 6:
        score += 0.34
        signals += 1

    # Normalize to 0.0–1.0 range
    return min(1.0, score)


def _compute_reasoning_engagement(
    tasks: list,
    cats: set[str],
) -> dict[str, dict[str, Any]]:
    """For each category in *cats*, compute the fraction of passed entries
    that include a non-trivial reasoning string, plus a quality score.

    A reasoning is 'non-trivial' when it is a non-empty string of more than
    10 characters.  Only tasks with a non-skip SR outcome contribute.

    Returns {cat: {"engagement_rate": float|None, "reasoning_quality_rate": float|None,
                   "with_reasoning": int, "with_quality": int, "total_passes": int}}.
    """
    from app.sr_blind_spots import is_skipped_sr_outcome  # noqa: PLC0415

    stats: dict[str, dict[str, int | float]] = {
        cat: {
            "with_reasoning": 0,
            "with_quality": 0,
            "total_passes": 0,
            "quality_scores": [],
        }
        for cat in cats
    }
    for t in tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        for cat in cats:
            entry = check_results.get(cat)
            if not isinstance(entry, dict) or not entry.get("passed", True):
                continue
            stats[cat]["total_passes"] += 1
            reasoning = entry.get("reasoning") or ""
            if isinstance(reasoning, str) and len(reasoning.strip()) > 10:
                stats[cat]["with_reasoning"] += 1

            # Compute quality score
            quality_score = _score_reasoning_quality(reasoning)
            stats[cat]["quality_scores"].append(quality_score)
            if quality_score > 0.5:
                stats[cat]["with_quality"] += 1

    result: dict[str, dict[str, Any]] = {}
    for cat, s in stats.items():
        total = s["total_passes"]
        quality_scores = s.get("quality_scores", [])
        avg_quality = (
            round(sum(quality_scores) / len(quality_scores), 3)
            if quality_scores
            else None
        )
        result[cat] = {
            "engagement_rate": round(s["with_reasoning"] / total, 3)
            if total > 0
            else None,
            "reasoning_quality_rate": avg_quality,
            "with_reasoning": s["with_reasoning"],
            "with_quality": s["with_quality"],
            "total_passes": total,
        }
    return result


def _collect_prompt_versions(tasks: list) -> dict:
    """Return a Counter of prompt_version values seen in _self_review_outcome."""
    from collections import Counter  # noqa: PLC0415

    c: Counter = Counter()
    for t in tasks:
        pv = (t.context or {}).get("_self_review_outcome", {}).get("prompt_version")
        if pv is not None:
            c[str(pv)] += 1
    return c


def _percentile(values: list, p: float) -> float:
    """Compute p-th percentile (0–100) of a list using linear interpolation."""
    sv = sorted(values)
    n = len(sv)
    if n == 1:
        return float(sv[0])
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sv[-1])
    frac = idx - lo
    return sv[lo] + frac * (sv[hi] - sv[lo])


def _compute_miss_latency_stats(tasks: list) -> dict:
    """Return per-category time-to-detect latency stats for SR misses.

    An SR miss is a category where check_results[cat].passed == True but the
    same category appears in _first_attempt_rejection_categories.  Latency =
    rejection timestamp − SR completion timestamp.

    Skips samples where both timestamps fall back to the same value (task.updated_at),
    because the latency would be 0 and not a real signal.
    """

    def _parse_iso(s: object) -> Optional[datetime]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(str(s))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, TypeError):
            return None

    def _updated_naive(t: object) -> Optional[datetime]:
        ua = getattr(t, "updated_at", None)
        if ua is None:
            return None
        return ua.replace(tzinfo=None) if ua.tzinfo else ua  # type: ignore[return-value]

    _LONE_CHECKER_P95_THRESHOLD = 7 * 86400  # 7 days in seconds

    # per category: list of latency seconds
    latencies: dict[str, list[float]] = {}

    for task in tasks:
        ctx = task.context or {}
        sr_outcome = ctx.get("_self_review_outcome") or {}
        check_results = sr_outcome.get("check_results") or {}
        rejection_cats = ctx.get("_first_attempt_rejection_categories") or {}

        if not check_results or not rejection_cats:
            continue

        missed_cats = [
            cat
            for cat, cr in check_results.items()
            if isinstance(cr, dict)
            and cr.get("passed") is True
            and cat in rejection_cats
        ]
        if not missed_cats:
            continue

        # Resolve SR timestamp
        sr_ts: Optional[datetime] = None
        for field in ("completed_at", "computed_at", "stamped_at"):
            sr_ts = _parse_iso(sr_outcome.get(field))
            if sr_ts is not None:
                break
        sr_fallback = sr_ts is None
        if sr_ts is None:
            sr_ts = _updated_naive(task)

        # Resolve rejection timestamp
        rej_ts: Optional[datetime] = None
        for field in (
            "_first_attempt_rejection_at",
            "_first_attempt_rejection_categories_at",
        ):
            rej_ts = _parse_iso(ctx.get(field))
            if rej_ts is not None:
                break
        rej_fallback = rej_ts is None
        if rej_ts is None:
            rej_ts = _updated_naive(task)

        # Skip if both timestamps came from the same fallback — no real signal
        if sr_fallback and rej_fallback:
            continue

        if sr_ts is None or rej_ts is None:
            continue

        latency = (rej_ts - sr_ts).total_seconds()
        if latency <= 0:
            continue

        for cat in missed_cats:
            latencies.setdefault(cat, []).append(latency)

    result: dict[str, dict] = {}
    for cat, lats in latencies.items():
        n = len(lats)
        p50 = int(_percentile(lats, 50))
        p95 = int(_percentile(lats, 95))
        result[cat] = {
            "time_to_miss_detect_p50_sec": p50,
            "time_to_miss_detect_p95_sec": p95,
            "time_to_miss_detect_sample_count": n,
            "time_to_miss_detect_lone_checker": n >= 2
            and p95 > _LONE_CHECKER_P95_THRESHOLD,
        }
    return result


async def compute_sr_catch_rate_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Compare SR prediction hit-rate per category, bucketed by strategic cycle.

    Primary mode: uses the two most recently COMPLETED strategic cycles as
    "current" and "previous" buckets (tasks keyed by ``strategic_cycle_id``).
    Rolls further back through completed cycles if either bucket has fewer than
    :data:`_MIN_SR_SAMPLE` qualifying tasks (both ``_self_review_outcome`` and
    ``_first_attempt_rejection_categories`` present).

    Fallback mode: when fewer than two completed cycles exist *or* no pair of
    consecutive cycles has enough tasks, falls back to the existing 14-day
    calendar windows so the metric is non-blank during onboarding and for
    tasks that were never assigned a ``strategic_cycle_id``.

    **Side effect**: writes the result to ``project.design_doc["sr_catch_rate_trends"]``
    and maintains a per-category ``rate_history`` list (capped at 5 entries).
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_count = 0
    previous_count = 0
    current_stats: dict[str, dict] = {}
    previous_stats: dict[str, dict] = {}
    current_type_stats: dict[str, dict[str, dict]] = {}
    current_window_tasks: list = []
    previous_window_tasks: list = []
    current_all_iter_stats: dict[str, dict] = {}
    previous_all_iter_stats: dict[str, dict] = {}
    current_sev_stats: dict[str, dict] = {}
    previous_sev_stats: dict[str, dict] = {}

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            (
                c_count,
                c_stats,
                c_type_stats,
                c_all_iter,
                c_sev,
                c_scope_stats,
            ) = _stats_for_tasks(task_by_cycle.get(c_cycle.id, []))
            p_count, p_stats, _, p_all_iter, p_sev, _ = _stats_for_tasks(
                task_by_cycle.get(p_cycle.id, [])
            )
            if c_count >= _MIN_SR_SAMPLE and p_count >= _MIN_SR_SAMPLE:
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_count = c_count
                previous_count = p_count
                current_stats = c_stats
                previous_stats = p_stats
                current_type_stats = c_type_stats
                current_scope_stats = c_scope_stats
                current_window_tasks = task_by_cycle.get(c_cycle.id, [])
                previous_window_tasks = task_by_cycle.get(p_cycle.id, [])
                current_all_iter_stats = c_all_iter
                previous_all_iter_stats = p_all_iter
                current_sev_stats = c_sev
                previous_sev_stats = p_sev
                break

    if not use_cycle_mode:
        # Calendar fallback for onboarding / tasks without strategic_cycle_id.
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        cal_current = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        cal_previous = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]
        (
            current_count,
            current_stats,
            current_type_stats,
            current_all_iter_stats,
            current_sev_stats,
            current_scope_stats,
        ) = _stats_for_tasks(cal_current)
        (
            previous_count,
            previous_stats,
            _,
            previous_all_iter_stats,
            previous_sev_stats,
            _,
        ) = _stats_for_tasks(cal_previous)
        current_window_tasks = cal_current
        previous_window_tasks = cal_previous

    # Gate on finding count (true catch-rate denominator), not task count.
    # finding_count = total reviewer-flagged (task, category) pairs in the window.
    current_finding_count = sum(s.get("human_count", 0) for s in current_stats.values())
    previous_finding_count = sum(
        s.get("human_count", 0) for s in previous_stats.values()
    )
    if (
        current_finding_count < _MIN_SR_FINDINGS
        or previous_finding_count < _MIN_SR_FINDINGS
    ):
        return None

    all_cats = (
        set(current_stats.keys())
        | set(previous_stats.keys())
        | set(current_all_iter_stats.keys())
    )
    if not all_cats:
        return None

    _empty_cat: dict[str, Any] = {
        "hit_count": 0,
        "sr_alone_hit_count": 0,
        "reviewer_save_hit_count": 0,
        "human_count": 0,
        "w_hit": 0.0,
        "w_total": 0.0,
        "sr_failed_count": 0,
        "fp_count": 0,
    }
    categories: dict[str, Any] = {}
    for cat in all_cats:
        curr = current_stats.get(cat, _empty_cat)
        prev = previous_stats.get(cat, _empty_cat)
        current_hit_rate = (
            round(curr["hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )
        previous_hit_rate = (
            round(prev["hit_count"] / prev["human_count"], 3)
            if prev["human_count"] > 0
            else None
        )
        miss_rate = (
            round(1 - current_hit_rate, 3) if current_hit_rate is not None else None
        )
        sr_alone_hit_rate = (
            round(curr["sr_alone_hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )
        reviewer_save_rate = (
            round(curr["reviewer_save_hit_count"] / curr["human_count"], 3)
            if curr["human_count"] > 0
            else None
        )

        # Only compute trend when both windows have enough findings for the
        # delta to be meaningful; fewer findings make the delta pure noise.
        _curr_findings = curr.get("human_count", 0)
        _prev_findings = prev.get("human_count", 0)
        if (
            current_hit_rate is not None
            and previous_hit_rate is not None
            and _curr_findings >= _MIN_SR_FINDINGS
            and _prev_findings >= _MIN_SR_FINDINGS
        ):
            delta = current_hit_rate - previous_hit_rate
            if delta >= _TREND_DELTA_THRESHOLD:
                trend = "increasing"
            elif delta <= -_TREND_DELTA_THRESHOLD:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "stable"

        w_total = curr.get("w_total", 0.0)
        weighted_miss_rate = (
            round(1.0 - curr["w_hit"] / w_total, 3) if w_total > 0 else miss_rate
        )

        curr_sr_failed = curr.get("sr_failed_count", 0)
        curr_fp = curr.get("fp_count", 0)
        prev_sr_failed = prev.get("sr_failed_count", 0)
        prev_fp = prev.get("fp_count", 0)
        current_fp_rate = (
            round(curr_fp / curr_sr_failed, 3) if curr_sr_failed > 0 else None
        )
        previous_fp_rate = (
            round(prev_fp / prev_sr_failed, 3) if prev_sr_failed > 0 else None
        )
        if current_fp_rate is not None and previous_fp_rate is not None:
            fp_delta = current_fp_rate - previous_fp_rate
            if fp_delta >= _TREND_DELTA_THRESHOLD:
                fp_trend = "increasing"
            elif fp_delta <= -_TREND_DELTA_THRESHOLD:
                fp_trend = "decreasing"
            else:
                fp_trend = "stable"
        else:
            fp_trend = "stable"
        precision = (
            round((curr_sr_failed - curr_fp) / curr_sr_failed, 3)
            if curr_sr_failed > 0
            else None
        )

        curr_all = current_all_iter_stats.get(cat, {"hit_count": 0, "human_count": 0})
        all_iter_hit_rate = (
            round(curr_all["hit_count"] / curr_all["human_count"], 3)
            if curr_all["human_count"] > 0
            else None
        )
        all_iter_miss_rate = (
            round(1 - all_iter_hit_rate, 3) if all_iter_hit_rate is not None else None
        )

        # Precision/recall quadrant: compare current vs previous window deltas.
        previous_precision = (
            round((prev_sr_failed - prev_fp) / prev_sr_failed, 3)
            if prev_sr_failed > 0
            else None
        )
        recall_delta: Optional[float] = (
            round(current_hit_rate - previous_hit_rate, 3)
            if current_hit_rate is not None and previous_hit_rate is not None
            else None
        )
        precision_delta: Optional[float] = (
            round(precision - previous_precision, 3)
            if precision is not None and previous_precision is not None
            else None
        )
        precision_recall_quadrant: Optional[str] = None
        if recall_delta is not None and precision_delta is not None:
            _q = _PRECISION_RECALL_QUADRANT_THRESHOLD
            p_up = precision_delta >= _q
            p_down = precision_delta <= -_q
            r_up = recall_delta >= _q
            r_down = recall_delta <= -_q
            if p_up and r_up:
                precision_recall_quadrant = "healthy"
            elif p_up and r_down:
                precision_recall_quadrant = "conservative_shift"
            elif p_down and r_up:
                precision_recall_quadrant = "noisy_shift"
            elif p_down and r_down:
                precision_recall_quadrant = "both_declining"
            elif not p_up and not p_down and not r_up and not r_down:
                precision_recall_quadrant = "flat"
            else:
                precision_recall_quadrant = "mixed"

        categories[cat] = {
            "current_hit_rate": current_hit_rate,
            "previous_hit_rate": previous_hit_rate,
            "current_human_count": int(curr.get("human_count") or 0),
            "previous_human_count": int(prev.get("human_count") or 0),
            "low_confidence_finding_count": _curr_findings < _MIN_SR_FINDINGS,
            "sr_alone_hit_count": int(curr.get("sr_alone_hit_count") or 0),
            "reviewer_save_hit_count": int(curr.get("reviewer_save_hit_count") or 0),
            "sr_alone_hit_rate": sr_alone_hit_rate,
            "reviewer_save_rate": reviewer_save_rate,
            "miss_rate": miss_rate,
            "weighted_miss_rate": weighted_miss_rate,
            "trend": trend,
            "false_positive_rate": current_fp_rate,
            "previous_fp_rate": previous_fp_rate,
            "fp_trend": fp_trend,
            "precision": precision,
            "all_iterations_catch_rate": all_iter_hit_rate,
            "all_iterations_miss_rate": all_iter_miss_rate,
            "precision_recall_quadrant": precision_recall_quadrant,
            "precision_delta": precision_delta,
            "recall_delta": recall_delta,
        }

    # Attach miss examples to top-_EXAMPLE_MAX_CATS categories by weighted_miss_rate.
    top_cats_for_examples = sorted(
        all_cats,
        key=lambda c: categories[c].get("weighted_miss_rate") or 0.0,
        reverse=True,
    )[:_EXAMPLE_MAX_CATS]
    for cat in top_cats_for_examples:
        raw = (current_stats.get(cat) or {}).get("miss_examples", [])
        examples = _finalize_examples(raw)
        if examples:
            categories[cat]["examples"] = examples

    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing: dict = dict(doc.get("sr_catch_rate_trends") or {})
    existing_computed_at: Optional[str] = existing.get("computed_at")

    is_new_window = True
    if use_cycle_mode:
        # In cycle mode a new window = new current cycle id.
        is_new_window = existing.get("current_cycle_id") != current_cycle_id
    elif existing_computed_at:
        try:
            last_dt = datetime.fromisoformat(existing_computed_at)
            is_new_window = (now - last_dt).days >= 14
        except (ValueError, TypeError):
            pass

    existing_cats: dict = dict(existing.get("categories") or {})
    for cat, data in categories.items():
        prev_cat: dict = dict(existing_cats.get(cat) or {})
        rate_history: list = list(prev_cat.get("rate_history") or [])
        if (is_new_window or not rate_history) and data["current_hit_rate"] is not None:
            rate_history.append(data["current_hit_rate"])
            rate_history = rate_history[-5:]
        data["rate_history"] = rate_history

    # Mark categories currently below the baseline floor so the UI can surface them.
    _floor_thresh: float = float(doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
    _floor_min_win: int = int(
        doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )
    for cat, data in categories.items():
        history = data.get("rate_history") or []
        data["below_floor"] = len(history) >= _floor_min_win and all(
            r < _floor_thresh for r in history[-_floor_min_win:]
        )

    # Attach reasoning engagement rate for all categories (not just regressing ones).
    # Regressing = trend=="decreasing" OR below_floor.
    regressing_cats = {
        cat
        for cat, data in categories.items()
        if data.get("trend") == "decreasing" or data.get("below_floor")
    }
    all_cats = set(categories.keys())
    eng = _compute_reasoning_engagement(current_window_tasks, all_cats)
    for cat, cat_eng in eng.items():
        categories[cat]["reasoning_engagement_rate"] = cat_eng["engagement_rate"]
        categories[cat]["reasoning_quality_rate"] = cat_eng["reasoning_quality_rate"]
        categories[cat]["reasoning_engagement_with"] = cat_eng["with_reasoning"]
        categories[cat]["reasoning_engagement_total"] = cat_eng["total_passes"]

    # Attach engagement_thinning flag for each category.
    # Mirrors the thinning Alert predicate (lines ~4918) but also fires for regressing
    # categories — the agent gate consumer takes the union of regressing+thinning itself.
    _eng_threshold: float = float(
        doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD
    )
    _eng_min_passes: int = int(
        doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES
    )
    for cat, data in categories.items():
        _rate = data.get("reasoning_engagement_rate")
        _total = data.get("reasoning_engagement_total") or 0
        data["engagement_thinning"] = bool(
            _rate is not None and _rate < _eng_threshold and _total >= _eng_min_passes
        )

    # Attach false-positive stats: SR flagged but reviewer didn't independently flag.
    # Both windows use _compute_cat_fp_stats so reviewer_findings count as confirmations
    # consistently — without this, previous_fp_rate overcounts FPs and can trigger the
    # sr_false_positive dispatcher signal spuriously after a fresh-reviewer wave.
    cat_fp_stats, type_cat_fp_stats, scope_cat_fp_stats = _compute_cat_fp_stats(
        current_window_tasks
    )
    prev_cat_fp_stats, _, _ = _compute_cat_fp_stats(previous_window_tasks)
    for cat, data in categories.items():
        fp_data = cat_fp_stats.get(cat) or {}
        fp = fp_data.get("fp", 0)
        tp = fp_data.get("tp", 0)
        data["fp_count"] = fp
        data["tp_count"] = tp
        data["false_positive_rate"] = (
            round(fp / (fp + tp), 3) if (fp + tp) > 0 else None
        )
        prev_fp_data = prev_cat_fp_stats.get(cat) or {}
        prev_fp = prev_fp_data.get("fp", 0)
        prev_tp = prev_fp_data.get("tp", 0)
        data["previous_fp_rate"] = (
            round(prev_fp / (prev_fp + prev_tp), 3) if (prev_fp + prev_tp) > 0 else None
        )

    # Attach severity calibration stats: compares agent severity vs reviewer severity
    # for categories where both sides flagged it. Under-rating is the dangerous case.
    sev_cal = _compute_cat_severity_calibration(current_window_tasks)
    for cat, data in categories.items():
        cal = sev_cal.get(cat)
        if cal:
            data["severity_calibration"] = cal

    # Attach time-to-miss-detect latency stats per category.
    miss_latency = _compute_miss_latency_stats(current_window_tasks)
    for cat, data in categories.items():
        stats = miss_latency.get(cat) or {}
        data["time_to_miss_detect_p50_sec"] = stats.get("time_to_miss_detect_p50_sec")
        data["time_to_miss_detect_p95_sec"] = stats.get("time_to_miss_detect_p95_sec")
        data["time_to_miss_detect_sample_count"] = stats.get(
            "time_to_miss_detect_sample_count", 0
        )
        data["time_to_miss_detect_lone_checker"] = stats.get(
            "time_to_miss_detect_lone_checker", False
        )

    # Compute per-task-type category breakdown from current window only.
    # Cells with fewer than _MIN_SR_SAMPLE observations are omitted.
    by_task_type: dict[str, dict[str, Any]] = {}
    for ttype, tcat_stats in current_type_stats.items():
        type_cats: dict[str, Any] = {}
        for cat, stats in tcat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            sr_alone_hit_rate = (
                round(stats["sr_alone_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            reviewer_save_rate = (
                round(stats["reviewer_save_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            raw_ex = stats.get("miss_examples", [])
            ex = _finalize_examples(raw_ex)
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
                "sr_alone_hit_count": stats["sr_alone_hit_count"],
                "reviewer_save_hit_count": stats["reviewer_save_hit_count"],
                "sr_alone_hit_rate": sr_alone_hit_rate,
                "reviewer_save_rate": reviewer_save_rate,
            }
            if ex:
                entry["examples"] = ex
            tfp = (type_cat_fp_stats.get(ttype) or {}).get(cat) or {}
            fp_t = tfp.get("fp", 0)
            tp_t = tfp.get("tp", 0)
            entry["fp_count"] = fp_t
            entry["tp_count"] = tp_t
            entry["false_positive_rate"] = (
                round(fp_t / (fp_t + tp_t), 3) if (fp_t + tp_t) > 0 else None
            )
            type_cats[cat] = entry
        if type_cats:
            by_task_type[ttype] = type_cats

    # Per-fix-iteration breakdown — bucket current_window_tasks by the
    # fix_iteration stamped on _self_review_outcome (1, 2, 3+) so operators
    # can see which categories degrade across review-fix passes vs. which
    # are weak from the start. Original tasks (no fix_iteration) bucket as "1".
    def _fix_iter_bucket(t) -> str:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        fi = sro.get("fix_iteration")
        if fi is None:
            fi_int = 1
        else:
            try:
                fi_int = int(fi)
            except (TypeError, ValueError):
                fi_int = 1
        if fi_int <= 1:
            return "1"
        if fi_int == 2:
            return "2"
        return "3+"

    by_fix_iteration: dict[str, dict[str, Any]] = {}
    fix_iter_buckets: dict[str, list] = {"1": [], "2": [], "3+": []}
    for _t in current_window_tasks:
        fix_iter_buckets[_fix_iter_bucket(_t)].append(_t)
    for bucket_key in ("1", "2", "3+"):
        bucket_tasks = fix_iter_buckets[bucket_key]
        if not bucket_tasks:
            continue
        _, b_cat_stats, _, b_all_iter_stats, _, _ = _stats_for_tasks(bucket_tasks)
        bucket_cats: dict[str, Any] = {}
        for cat, stats in b_cat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            entry: dict[str, Any] = {
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
            }
            ai = b_all_iter_stats.get(cat)
            if ai and ai["human_count"] > 0:
                ai_hit_rate = round(ai["hit_count"] / ai["human_count"], 3)
                entry["all_iterations_miss_rate"] = round(1 - ai_hit_rate, 3)
            ex = _finalize_examples(stats.get("miss_examples", []))
            if ex:
                entry["examples"] = ex
            bucket_cats[cat] = entry
        if bucket_cats:
            by_fix_iteration[bucket_key] = bucket_cats

    # Compute per-scope category breakdown from current window only.
    # Cells with fewer than _MIN_SR_SAMPLE observations are omitted.
    existing_by_scope: dict = dict(existing.get("by_scope") or {})
    by_scope: dict[str, dict[str, Any]] = {}
    for scope, scat_stats in current_scope_stats.items():
        scope_cats: dict[str, Any] = {}
        for cat, stats in scat_stats.items():
            if stats["human_count"] < _MIN_SR_SAMPLE:
                continue
            hit_rate = round(stats["hit_count"] / stats["human_count"], 3)
            sr_alone_hit_rate = (
                round(stats["sr_alone_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            reviewer_save_rate = (
                round(stats["reviewer_save_hit_count"] / stats["human_count"], 3)
                if stats["human_count"] > 0
                else None
            )
            raw_ex = stats.get("miss_examples", [])
            ex = _finalize_examples(raw_ex)
            entry: dict[str, Any] = {
                "hit_rate": hit_rate,
                "miss_rate": round(1 - hit_rate, 3),
                "sample_count": stats["human_count"],
                "sr_alone_hit_count": stats["sr_alone_hit_count"],
                "reviewer_save_hit_count": stats["reviewer_save_hit_count"],
                "sr_alone_hit_rate": sr_alone_hit_rate,
                "reviewer_save_rate": reviewer_save_rate,
            }
            if ex:
                entry["examples"] = ex
            sfp = (scope_cat_fp_stats.get(scope) or {}).get(cat) or {}
            fp_s = sfp.get("fp", 0)
            tp_s = sfp.get("tp", 0)
            entry["fp_count"] = fp_s
            entry["tp_count"] = tp_s
            entry["false_positive_rate"] = (
                round(fp_s / (fp_s + tp_s), 3) if (fp_s + tp_s) > 0 else None
            )
            # Persist rate_history and trend for this (scope, category) cell.
            prev_scope_entry: dict = dict(
                (existing_by_scope.get(scope) or {}).get(cat) or {}
            )
            scope_rate_history: list = list(prev_scope_entry.get("rate_history") or [])
            if is_new_window or not scope_rate_history:
                scope_rate_history.append(hit_rate)
                scope_rate_history = scope_rate_history[-5:]
            prev_scope_hit_rate = prev_scope_entry.get("hit_rate")
            if prev_scope_hit_rate is not None:
                s_delta = hit_rate - prev_scope_hit_rate
                if s_delta >= _TREND_DELTA_THRESHOLD:
                    scope_trend: str = "increasing"
                elif s_delta <= -_TREND_DELTA_THRESHOLD:
                    scope_trend = "decreasing"
                else:
                    scope_trend = "stable"
            else:
                scope_trend = "stable"
            entry["rate_history"] = scope_rate_history
            entry["trend"] = scope_trend
            scope_cats[cat] = entry
        if scope_cats:
            by_scope[scope] = scope_cats

    sr_skip_rate, sr_skip_dist = _sr_skip_stats(current_window_tasks)

    # Compute per-severity catch-rate breakdown across all categories.
    by_severity: dict[str, Any] = {}
    for sev in ("CRITICAL", "MAJOR", "MINOR"):
        curr_s = current_sev_stats.get(sev, {"hit_count": 0, "human_count": 0})
        prev_s = previous_sev_stats.get(sev, {"hit_count": 0, "human_count": 0})
        curr_hit = (
            round(curr_s["hit_count"] / curr_s["human_count"], 3)
            if curr_s["human_count"] > 0
            else None
        )
        prev_hit = (
            round(prev_s["hit_count"] / prev_s["human_count"], 3)
            if prev_s["human_count"] > 0
            else None
        )
        curr_miss = round(1.0 - curr_hit, 3) if curr_hit is not None else None
        prev_miss = round(1.0 - prev_hit, 3) if prev_hit is not None else None
        if curr_miss is not None and prev_miss is not None:
            sev_delta = curr_miss - prev_miss
            thr = _SR_SEVERITY_DELTA_THRESHOLDS[sev]
            sev_trend = (
                "regressing"
                if sev_delta >= thr
                else "improving"
                if sev_delta <= -thr
                else "stable"
            )
        else:
            sev_trend = "stable"
        by_severity[sev] = {
            "current_miss_rate": curr_miss,
            "previous_miss_rate": prev_miss,
            "current_hit_rate": curr_hit,
            "previous_hit_rate": prev_hit,
            "current_sample": curr_s["human_count"],
            "previous_sample": prev_s["human_count"],
            "trend": sev_trend,
        }

    edges_stats = _compute_edges_stats(all_tasks)

    # Compute overall (aggregate) hit rates across all categories, weighted by human_count.
    # This represents the true cycle-over-cycle change of the headline metric.
    current_total_hits = 0
    current_total_humans = 0
    previous_total_hits = 0
    previous_total_humans = 0
    for data in categories.values():
        curr_hit_rate = data.get("current_hit_rate")
        curr_human_count = data.get("current_human_count", 0)
        prev_hit_rate = data.get("previous_hit_rate")
        prev_human_count = data.get("previous_human_count", 0)
        if curr_hit_rate is not None and curr_human_count > 0:
            current_total_hits += round(curr_hit_rate * curr_human_count)
            current_total_humans += curr_human_count
        if prev_hit_rate is not None and prev_human_count > 0:
            previous_total_hits += round(prev_hit_rate * prev_human_count)
            previous_total_humans += prev_human_count
    overall_hit_rate = (
        round(current_total_hits / current_total_humans, 3)
        if current_total_humans > 0
        else None
    )
    previous_overall_hit_rate = (
        round(previous_total_hits / previous_total_humans, 3)
        if previous_total_humans > 0
        else None
    )

    # Prompt-version awareness: detect mixed-regime windows so the agent can
    # caveat calibration surfaces and avoid mistaking prompt rollouts for regressions.
    pv_counter = _collect_prompt_versions(current_window_tasks)
    pv_counter_prev = _collect_prompt_versions(previous_window_tasks)
    current_prompt_version: Optional[str] = (
        pv_counter.most_common(1)[0][0] if pv_counter else None
    )
    prev_prompt_version: Optional[str] = (
        pv_counter_prev.most_common(1)[0][0] if pv_counter_prev else None
    )

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "signal_last_computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "categories": categories,
        "by_task_type": by_task_type,
        "by_fix_iteration": by_fix_iteration,
        "by_scope": by_scope,
        "by_severity": by_severity,
        "edges_considered_stats": edges_stats,
        "sr_skip_rate": sr_skip_rate,
        "sr_skip_reason_distribution": sr_skip_dist if sr_skip_dist is not None else {},
        "overall_hit_rate": overall_hit_rate,
        "previous_overall_hit_rate": previous_overall_hit_rate,
        "current_finding_count": current_finding_count,
        "previous_finding_count": previous_finding_count,
    }
    if current_prompt_version is not None:
        result["prompt_version"] = current_prompt_version
    if len(pv_counter) > 1:
        result["mixed_prompt_versions"] = True
    if (
        current_prompt_version is not None
        and prev_prompt_version is not None
        and current_prompt_version != prev_prompt_version
    ):
        result["prompt_version_changed"] = True
        for _scope_cats in by_scope.values():
            for _scope_entry in _scope_cats.values():
                _scope_entry["prompt_version_changed"] = True
    if sr_skip_rate is not None:
        result["sr_skip_rate"] = sr_skip_rate
        result["sr_skip_reason_distribution"] = sr_skip_dist

    _dir_text_map: dict[str, str] = {
        str(d.get("id") or d.get("direction_id") or ""): (
            d.get("text") or d.get("description") or ""
        )
        for d in (project.directions or [])
        if d.get("id") or d.get("direction_id")
    }
    by_direction = compute_sr_catch_rate_by_direction(
        all_tasks,
        existing_by_direction=existing.get("by_direction") or {},
        is_new_window=is_new_window,
        direction_text_map=_dir_text_map or None,
    )
    result["by_direction"] = by_direction or {}

    exec_evidence = _compute_execution_evidence_miss_rates(current_window_tasks)
    result["execution_evidence_miss_rates"] = exec_evidence or {}

    from app.sr_blind_spots import (
        aggregate_sr_co_failure_pairs,
        _count_reviewer_disagreement_directions,
    )  # noqa: PLC0415

    co_pairs = aggregate_sr_co_failure_pairs(current_window_tasks)
    result["co_failure_pairs"] = co_pairs or []

    # Attach per-category failure-mode breakdown (reading_gap / judgment_gap / coverage_gap).
    # Uses origin tags set by the fresh-reviewer pass in standard.ts:441-457.
    failure_mode_dir_counts = _count_reviewer_disagreement_directions(
        current_window_tasks
    )
    cat_failure_modes: dict[str, dict[str, int]] = {}
    for (cat, _ttype), bucket in failure_mode_dir_counts.items():
        if cat not in cat_failure_modes:
            cat_failure_modes[cat] = {
                "reading_gap": 0,
                "judgment_gap": 0,
                "coverage_gap": 0,
            }
        cat_failure_modes[cat]["reading_gap"] += bucket["miss_recovery_incomplete"]
        cat_failure_modes[cat]["judgment_gap"] += bucket["miss_disagreement"]
        cat_failure_modes[cat]["coverage_gap"] += bucket["miss_coverage_gap"]
    for cat, data in categories.items():
        modes = cat_failure_modes.get(cat)
        if modes and sum(modes.values()) > 0:
            dominant = max(modes.items(), key=lambda x: x[1])
            data["miss_mode_breakdown"] = {**modes, "dominant_mode": dominant[0]}

    # Reviewer hallucination rate: proportion of reviewer findings filtered as phantom file refs.
    # Uses reviewer_findings_filtered written by the fresh-reviewer pass in standard.ts.
    curr_hall_total = 0
    curr_hall_filtered = 0
    for _t in current_window_tasks:
        _sro = (getattr(_t, "context", None) or {}).get("_self_review_outcome") or {}
        _rff = _sro.get("reviewer_findings_filtered") or {}
        _t_total = _rff.get("total_reviewer_findings")
        _t_filtered = _rff.get("filtered_count")
        if isinstance(_t_total, int) and isinstance(_t_filtered, int):
            curr_hall_total += _t_total
            curr_hall_filtered += _t_filtered
    prev_hall_total = 0
    prev_hall_filtered = 0
    for _t in previous_window_tasks:
        _sro = (getattr(_t, "context", None) or {}).get("_self_review_outcome") or {}
        _rff = _sro.get("reviewer_findings_filtered") or {}
        _t_total = _rff.get("total_reviewer_findings")
        _t_filtered = _rff.get("filtered_count")
        if isinstance(_t_total, int) and isinstance(_t_filtered, int):
            prev_hall_total += _t_total
            prev_hall_filtered += _t_filtered
    curr_hall_rate: Optional[float] = (
        round(curr_hall_filtered / curr_hall_total, 3)
        if curr_hall_total >= _HALLUCINATION_RATE_MIN_FINDINGS
        else None
    )
    prev_hall_rate: Optional[float] = (
        round(prev_hall_filtered / prev_hall_total, 3)
        if prev_hall_total >= _HALLUCINATION_RATE_MIN_FINDINGS
        else None
    )
    if curr_hall_rate is not None or prev_hall_rate is not None:
        hall_delta: Optional[float] = (
            round(curr_hall_rate - prev_hall_rate, 3)
            if curr_hall_rate is not None and prev_hall_rate is not None
            else None
        )
        result["reviewer_hallucination_rate"] = {
            "current_rate": curr_hall_rate,
            "previous_rate": prev_hall_rate,
            "current_sample": curr_hall_total,
            "previous_sample": prev_hall_total,
            "delta": hall_delta,
        }

    doc["sr_catch_rate_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_CATCH_RATE_TRENDS] project=%s categories=%d",
        project_id,
        len(categories),
    )

    # Fire notification when categories or severity buckets are regressing.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        regressing = {
            cat: data
            for cat, data in categories.items()
            if data.get("trend") == "decreasing"
        }
        regressing_severities = {
            sev: data
            for sev, data in by_severity.items()
            if data.get("trend") == "regressing"
        }
        fp_spiking = {
            cat: data
            for cat, data in categories.items()
            if data.get("fp_trend") == "increasing"
        }
        alertable = {**regressing, **fp_spiking}
        if alertable or regressing_severities:
            # Dedup: in cycle mode key off cycle_id; in calendar mode use 14-day window.
            # Dedup key includes both regressing categories and regressing severities.
            alert_state: dict = dict(doc.get("sr_catch_rate_trends_alert") or {})
            alert_computed_at: Optional[str] = alert_state.get("computed_at")
            alerted_cats: list = list(alert_state.get("categories") or [])
            alerted_sevs: list = list(alert_state.get("severities") or [])
            current_regressing_cats = sorted(alertable.keys())
            current_regressing_sevs = sorted(regressing_severities.keys())

            already_alerted = False
            if use_cycle_mode:
                already_alerted = (
                    alert_state.get("cycle_id") == current_cycle_id
                    and alerted_cats == current_regressing_cats
                    and alerted_sevs == current_regressing_sevs
                )
            elif alert_computed_at:
                try:
                    last_alert_dt = datetime.fromisoformat(alert_computed_at)
                    if (
                        (now - last_alert_dt).days < 14
                        and alerted_cats == current_regressing_cats
                        and alerted_sevs == current_regressing_sevs
                    ):
                        already_alerted = True
                except (ValueError, TypeError):
                    pass

            if not already_alerted:
                lines = ["SR catch-rate regression detected:"]
                for cat in current_regressing_cats:
                    data = alertable[cat]
                    if cat in regressing:
                        prev = data.get("previous_hit_rate")
                        curr_rate = data.get("current_hit_rate")
                        sample = current_stats.get(cat, {}).get("human_count", 0)
                        prev_str = f"{prev:.0%}" if prev is not None else "N/A"
                        curr_str = (
                            f"{curr_rate:.0%}" if curr_rate is not None else "N/A"
                        )
                        lines.append(
                            f"  • {cat}: hit-rate {prev_str} → {curr_str} (n={sample})"
                        )
                    else:
                        prev_fp = data.get("previous_fp_rate")
                        curr_fp = data.get("false_positive_rate")
                        sample = current_stats.get(cat, {}).get("sr_failed_count", 0)
                        prev_str = f"{prev_fp:.0%}" if prev_fp is not None else "N/A"
                        curr_str = f"{curr_fp:.0%}" if curr_fp is not None else "N/A"
                        lines.append(
                            f"  • {cat}: FP-rate {prev_str} → {curr_str} (n={sample})"
                        )
                if current_regressing_sevs:
                    lines.append("Severity regressions:")
                    for sev in current_regressing_sevs:
                        data_sev = regressing_severities[sev]
                        prev_miss = data_sev.get("previous_miss_rate")
                        curr_miss = data_sev.get("current_miss_rate")
                        sample_n = data_sev.get("current_sample", 0)
                        prev_str = (
                            f"{prev_miss:.0%}" if prev_miss is not None else "N/A"
                        )
                        curr_str = (
                            f"{curr_miss:.0%}" if curr_miss is not None else "N/A"
                        )
                        lines.append(
                            f"  • {sev}: {prev_str} → {curr_str} (n={sample_n})"
                        )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                # Surface the regression in AlertsBanner as a real Alert row.
                # Dedup mirror: any prior undismissed regression Alert rows
                # for this project are dismissed unless their title matches
                # what we are about to insert (defense-in-depth — same gate
                # as the design_doc dedup state above).
                title_parts = list(current_regressing_cats) + [
                    f"{s} severity" for s in current_regressing_sevs
                ]
                new_alert_title = _format_sr_alert_title(
                    _SR_REGRESSION_ALERT_TITLE_PREFIX, title_parts
                )
                existing_reg_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(
                                    f"{_SR_REGRESSION_ALERT_TITLE_PREFIX}%"
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_alert_title for a in existing_reg_alerts):
                    for a in existing_reg_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_alert_title,
                            message=description,
                        )
                    )

                webhook_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "regressing_categories": [
                        {
                            "category": cat,
                            "previous_hit_rate": alertable[cat].get(
                                "previous_hit_rate"
                            ),
                            "current_hit_rate": alertable[cat].get("current_hit_rate"),
                            "false_positive_rate": alertable[cat].get(
                                "false_positive_rate"
                            ),
                            "fp_trend": alertable[cat].get("fp_trend"),
                            "sample_size": current_stats.get(cat, {}).get(
                                "human_count", 0
                            ),
                        }
                        for cat in current_regressing_cats
                    ],
                    "regressing_severities": [
                        {
                            "severity": sev,
                            "previous_miss_rate": regressing_severities[sev].get(
                                "previous_miss_rate"
                            ),
                            "current_miss_rate": regressing_severities[sev].get(
                                "current_miss_rate"
                            ),
                            "sample_size": regressing_severities[sev].get(
                                "current_sample", 0
                            ),
                        }
                        for sev in current_regressing_sevs
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    webhook_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_catch_rate_regression",
                        payload=webhook_payload,
                    )
                )

                doc["sr_catch_rate_trends_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_regressing_cats,
                    "severities": current_regressing_sevs,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s regression_alert categories=%s severities=%s",
                    project_id,
                    current_regressing_cats,
                    current_regressing_sevs,
                )
        elif doc.get("sr_catch_rate_trends_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            # Dismiss any matching undismissed Alert rows + clear design_doc state.
            stale_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_REGRESSION_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_catch_rate_trends_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception("[SR_CATCH_RATE_TRENDS] notification failed, suppressing")

    # Baseline-floor alert: fire when a category stays below _SR_FLOOR_THRESHOLD
    # for at least _FLOOR_MIN_CONSECUTIVE_WINDOWS consecutive measurement windows.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        floor_threshold: float = float(
            (doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
        )
        floor_min_windows: int = int(
            (doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS)
        )

        below_floor = {
            cat: data
            for cat, data in categories.items()
            if (
                data.get("current_hit_rate") is not None
                and len(data.get("rate_history") or []) >= floor_min_windows
                and all(
                    r < floor_threshold
                    for r in (data.get("rate_history") or [])[-floor_min_windows:]
                )
            )
        }

        if below_floor:
            floor_alert_state: dict = dict(doc.get("sr_catch_rate_floor_alert") or {})
            floor_alerted_cats: list = list(floor_alert_state.get("categories") or [])
            current_floor_cats = sorted(below_floor.keys())

            floor_already_alerted = False
            if use_cycle_mode:
                floor_already_alerted = (
                    floor_alert_state.get("cycle_id") == current_cycle_id
                    and floor_alerted_cats == current_floor_cats
                )
            else:
                floor_alert_at: Optional[str] = floor_alert_state.get("computed_at")
                if floor_alert_at:
                    try:
                        last_floor_dt = datetime.fromisoformat(floor_alert_at)
                        if (
                            now - last_floor_dt
                        ).days < 14 and floor_alerted_cats == current_floor_cats:
                            floor_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not floor_already_alerted:
                lines = [
                    f"SR catch-rate below floor ({floor_threshold:.0%}) for {floor_min_windows}+ windows:"
                ]
                for cat in current_floor_cats:
                    data = below_floor[cat]
                    curr_rate = data.get("current_hit_rate")
                    sample = current_stats.get(cat, {}).get("human_count", 0)
                    curr_str = f"{curr_rate:.0%}" if curr_rate is not None else "N/A"
                    history = data.get("rate_history") or []
                    history_str = ", ".join(
                        f"{r:.0%}" for r in history[-floor_min_windows:]
                    )
                    lines.append(
                        f"  • {cat}: {curr_str} (last {floor_min_windows} windows: {history_str}, n={sample})"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                # Surface as ERROR Alert row in AlertsBanner (stricter than the
                # WARNING regression path — staying below floor for N consecutive
                # windows is a higher-severity signal). Defense-in-depth dedup
                # mirrors the regression path.
                new_floor_title = _format_sr_alert_title(
                    _SR_FLOOR_ALERT_TITLE_PREFIX, current_floor_cats
                )
                existing_floor_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.ERROR,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(f"{_SR_FLOOR_ALERT_TITLE_PREFIX}%"),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_floor_title for a in existing_floor_alerts):
                    for a in existing_floor_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.ERROR,
                            title=new_floor_title,
                            message=description,
                        )
                    )

                floor_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "floor_threshold": floor_threshold,
                    "below_floor_categories": [
                        {
                            "category": cat,
                            "current_hit_rate": below_floor[cat].get(
                                "current_hit_rate"
                            ),
                            "rate_history": below_floor[cat].get("rate_history"),
                            "sample_size": current_stats.get(cat, {}).get(
                                "human_count", 0
                            ),
                        }
                        for cat in current_floor_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    floor_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_catch_rate_below_floor",
                        payload=floor_payload,
                    )
                )

                doc["sr_catch_rate_floor_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_floor_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s floor_alert categories=%s",
                    project_id,
                    current_floor_cats,
                )
        elif doc.get("sr_catch_rate_floor_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            stale_floor_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.ERROR,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_FLOOR_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_floor_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_catch_rate_floor_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] floor notification failed, suppressing"
        )

    # Quadrant alert: fire when a category is classified as conservative_shift or
    # noisy_shift.  both_declining is intentionally excluded (covered by regression
    # alert); healthy/mixed/flat are silent.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        quadrant_alertable = {
            cat: data
            for cat, data in categories.items()
            if data.get("precision_recall_quadrant")
            in ("conservative_shift", "noisy_shift")
        }

        if quadrant_alertable:
            quadrant_alert_state: dict = dict(doc.get("sr_quadrant_alert") or {})
            q_alerted_cats: list = list(quadrant_alert_state.get("categories") or [])
            current_quadrant_cats = sorted(quadrant_alertable.keys())

            q_already_alerted = False
            if use_cycle_mode:
                q_already_alerted = (
                    quadrant_alert_state.get("cycle_id") == current_cycle_id
                    and q_alerted_cats == current_quadrant_cats
                )
            else:
                q_alert_at: Optional[str] = quadrant_alert_state.get("computed_at")
                if q_alert_at:
                    try:
                        last_q_dt = datetime.fromisoformat(q_alert_at)
                        if (
                            now - last_q_dt
                        ).days < 14 and q_alerted_cats == current_quadrant_cats:
                            q_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not q_already_alerted:
                lines = ["SR precision/recall shift detected:"]
                for cat in current_quadrant_cats:
                    data = quadrant_alertable[cat]
                    quadrant = data.get("precision_recall_quadrant")
                    pdelta = data.get("precision_delta")
                    rdelta = data.get("recall_delta")
                    p_str = f"{pdelta:+.0%}" if pdelta is not None else "N/A"
                    r_str = f"{rdelta:+.0%}" if rdelta is not None else "N/A"
                    lines.append(
                        f"  • {cat} [{quadrant}]: precision {p_str} / recall {r_str}"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                new_quadrant_title = _format_sr_alert_title(
                    _SR_QUADRANT_ALERT_TITLE_PREFIX, current_quadrant_cats
                )
                existing_q_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_quadrant_title for a in existing_q_alerts):
                    for a in existing_q_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_quadrant_title,
                            message=description,
                        )
                    )

                q_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "quadrant_categories": [
                        {
                            "category": cat,
                            "quadrant": quadrant_alertable[cat].get(
                                "precision_recall_quadrant"
                            ),
                            "precision_delta": quadrant_alertable[cat].get(
                                "precision_delta"
                            ),
                            "recall_delta": quadrant_alertable[cat].get("recall_delta"),
                        }
                        for cat in current_quadrant_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    q_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_precision_recall_shift",
                        payload=q_payload,
                    )
                )

                doc["sr_quadrant_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_quadrant_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s quadrant_alert categories=%s",
                    project_id,
                    current_quadrant_cats,
                )
        elif doc.get("sr_quadrant_alert"):
            # Clear transition: no longer alertable this cycle.
            stale_q_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_q_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_quadrant_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] quadrant notification failed, suppressing"
        )

    # PASS reasoning thinning alert: fire when a non-regressing category has
    # engagement below threshold with enough PASS samples.  Suppressed for
    # categories already covered by a regression or floor alert this cycle.
    try:
        import asyncio
        from app.notification_dispatch import (
            post_failure_alert_message,
            dispatch_external_notification,
        )

        eng_threshold: float = float(
            (doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD)
        )
        eng_min_passes: int = int(
            (doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES)
        )

        thinning_cats = {
            cat: data
            for cat, data in categories.items()
            if (
                cat not in regressing_cats
                and data.get("reasoning_engagement_rate") is not None
                and data["reasoning_engagement_rate"] < eng_threshold
                and (data.get("reasoning_engagement_total") or 0) >= eng_min_passes
            )
        }

        if thinning_cats:
            eng_alert_state: dict = dict(doc.get("sr_pass_engagement_alert") or {})
            eng_alerted_cats: list = list(eng_alert_state.get("categories") or [])
            current_thinning_cats = sorted(thinning_cats.keys())

            eng_already_alerted = False
            if use_cycle_mode:
                eng_already_alerted = (
                    eng_alert_state.get("cycle_id") == current_cycle_id
                    and eng_alerted_cats == current_thinning_cats
                )
            else:
                eng_alert_at: Optional[str] = eng_alert_state.get("computed_at")
                if eng_alert_at:
                    try:
                        last_eng_dt = datetime.fromisoformat(eng_alert_at)
                        if (
                            now - last_eng_dt
                        ).days < 14 and eng_alerted_cats == current_thinning_cats:
                            eng_already_alerted = True
                    except (ValueError, TypeError):
                        pass

            if not eng_already_alerted:
                lines = [
                    f"PASS reasoning thinning (engagement < {eng_threshold:.0%}, may regress soon):"
                ]
                for cat in current_thinning_cats:
                    data = thinning_cats[cat]
                    rate = data.get("reasoning_engagement_rate")
                    total = data.get("reasoning_engagement_total", 0)
                    with_r = data.get("reasoning_engagement_with", 0)
                    rate_str = f"{rate:.0%}" if rate is not None else "N/A"
                    lines.append(
                        f"  • {cat}: {with_r}/{total} PASSes with reasoning ({rate_str})"
                    )
                description = "\n".join(lines)

                # Add caveat if SR prompt changed during this window
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    description = (
                        description
                        + "\n\nNote: SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )

                await post_failure_alert_message(session, project_id, description)

                new_eng_title = _format_sr_alert_title(
                    _PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX, current_thinning_cats
                )
                existing_eng_alerts = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == project_id,
                                Alert.task_id.is_(None),
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title.like(
                                    f"{_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX}%"
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not any(a.title == new_eng_title for a in existing_eng_alerts):
                    for a in existing_eng_alerts:
                        a.dismissed_at = now
                        session.add(a)
                    session.add(
                        Alert(
                            project_id=project_id,
                            severity=AlertSeverity.WARNING,
                            title=new_eng_title,
                            message=description,
                        )
                    )

                thinning_payload: dict[str, Any] = {
                    "project_id": str(project_id),
                    "computed_at": result["computed_at"],
                    "eng_threshold": eng_threshold,
                    "thinning_categories": [
                        {
                            "category": cat,
                            "engagement_rate": thinning_cats[cat].get(
                                "reasoning_engagement_rate"
                            ),
                            "total_passes": thinning_cats[cat].get(
                                "reasoning_engagement_total", 0
                            ),
                        }
                        for cat in current_thinning_cats
                    ],
                }
                if result.get("prompt_version_changed") or result.get(
                    "mixed_prompt_versions"
                ):
                    thinning_payload["prompt_version_caveat"] = (
                        "SR prompt version changed during this window — "
                        + "attribution may be partly due to the prompt update"
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        event_type="sr_pass_reasoning_thinning",
                        payload=thinning_payload,
                    )
                )

                doc["sr_pass_engagement_alert"] = {
                    "computed_at": result["computed_at"],
                    "cycle_id": current_cycle_id,
                    "categories": current_thinning_cats,
                }
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)

                logger.info(
                    "[SR_CATCH_RATE_TRENDS] project=%s engagement_thinning_alert categories=%s",
                    project_id,
                    current_thinning_cats,
                )
        elif doc.get("sr_pass_engagement_alert"):
            # Clear transition: previously firing, no longer firing this cycle.
            stale_eng_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(
                                f"{_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX}%"
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for a in stale_eng_alerts:
                a.dismissed_at = now
                session.add(a)
            doc.pop("sr_pass_engagement_alert", None)
            project.design_doc = doc
            flag_modified(project, "design_doc")
            session.add(project)
    except Exception:
        logger.exception(
            "[SR_CATCH_RATE_TRENDS] engagement thinning notification failed, suppressing"
        )

    await session.flush()

    return result


# ---------------------------------------------------------------------------
# Fix-loop divergence regression alert
# ---------------------------------------------------------------------------


async def check_fix_loop_divergence_regression(
    project_id: uuid.UUID,
    session: AsyncSession,
) -> Optional[dict[str, Any]]:
    """Detect fix-loop divergence rate regression across two consecutive 7-day windows.

    Mirrors _MIN_SR_SAMPLE / _TREND_DELTA_THRESHOLD from compute_sr_catch_rate_trends.
    Idempotent per ISO year-week via project.design_doc["fix_loop_divergence_alerted_window"]
    so a persistent regression alerts at most once per week.
    """
    from app.notification_dispatch import (  # noqa: PLC0415
        post_fix_loop_divergence_regression_message,
    )

    now = _utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    window_key = f"{iso_year}-W{iso_week:02d}"

    project = await session.get(Project, project_id)
    if not project:
        return None
    doc = dict(project.design_doc or {})
    if doc.get("fix_loop_divergence_alerted_window") == window_key:
        doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        await session.commit()
        return None

    tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.created_at >= now - timedelta(days=14),
                )
                .options(load_only(Task.id, Task.fix_loop_history, Task.created_at))
            )
        )
        .scalars()
        .all()
    )

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    c7 = _naive(now - timedelta(days=7))
    c14 = _naive(now - timedelta(days=14))
    loops = ("lint", "test", "check")

    def _divergence_stats(start: datetime, end: datetime) -> dict[str, dict]:
        s: dict[str, dict] = {loop: {"n": 0, "d": 0} for loop in loops}
        for t in tasks:
            ts = _naive(t.created_at) if t.created_at else None
            if ts is None or not (start <= ts < end):
                continue
            for loop in loops:
                bucket = (t.fix_loop_history or {}).get(loop) or []
                if not bucket:
                    continue
                last_exit: Optional[str] = None
                for ev in reversed(bucket):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None:
                    last_exit = (
                        bucket[-1].get("exit_reason")
                        if isinstance(bucket[-1], dict)
                        else None
                    )
                s[loop]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    s[loop]["d"] += 1
        return s

    curr = _divergence_stats(c7, datetime(9999, 12, 31))
    prev = _divergence_stats(c14, c7)

    regressions: list[dict] = []
    for loop in loops:
        cn, cd = curr[loop]["n"], curr[loop]["d"]
        pn, pd = prev[loop]["n"], prev[loop]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            continue
        if cd / cn - pd / pn >= _TREND_DELTA_THRESHOLD:
            regressions.append(
                {
                    "loop": loop,
                    "prior_rate": round(pd / pn, 3),
                    "current_rate": round(cd / cn, 3),
                    "prior_count": pn,
                    "current_count": cn,
                }
            )

    if not regressions:
        return None

    await post_fix_loop_divergence_regression_message(session, project_id, regressions)
    doc["fix_loop_divergence_alerted_window"] = window_key
    doc["fix_loop_divergence_regressions"] = regressions
    doc["fix_loop_divergence_detected_at"] = now.isoformat()
    doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()
    logger.info(
        "[FIX_LOOP_DIVERGENCE] project=%s loops=%s",
        project_id,
        [r["loop"] for r in regressions],
    )
    return {"regressions": regressions}


# ---------------------------------------------------------------------------
# Fresh-reviewer cycle-over-cycle trend
# ---------------------------------------------------------------------------

_FR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}


async def compute_fresh_reviewer_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for fresh-reviewer effectiveness.

    Mirrors the cycle-vs-calendar bucketing from ``compute_sr_catch_rate_trends``.
    Requires at least ``_MIN_SR_SAMPLE`` tasks *with* reviewer_findings in both
    the current and previous bucket before computing rates.

    Output keys per category: ``per_category_hit_rate``, ``added_value``,
    ``approval_rate_lift`` — each with ``current``, ``previous``, ``delta``,
    and ``trend`` (stable / improving / regressing).

    **Side effect**: writes result to ``project.design_doc["fresh_reviewer_trend"]``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    _code_tasks = [
        t
        for t in all_tasks
        if (t.first_payload or {}).get("type") in _FR_CODE_CHANGE_TYPES
    ]

    def _bucket_stats(tasks: list) -> dict[str, Any]:
        """Compute fresh-reviewer stats for a pre-filtered task list."""
        with_reviewer = [
            t
            for t in tasks
            if (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]
        without_reviewer = [
            t
            for t in tasks
            if not (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]

        def _fa_rate(ts: list) -> Optional[float]:
            measured = [
                t for t in ts if "_first_attempt_succeeded" in (t.context or {})
            ]
            if not measured:
                return None
            return round(
                sum(1 for t in measured if t.context["_first_attempt_succeeded"])
                / len(measured),
                3,
            )

        cat_flagged: dict[str, int] = {}
        cat_confirmed: dict[str, int] = {}
        cat_added_value: dict[str, int] = {}
        for t in with_reviewer:
            ctx = t.context or {}
            findings = (
                ctx.get("_self_review_outcome", {}).get("reviewer_findings") or []
            )
            human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
            check_results = (
                ctx.get("_self_review_outcome", {}).get("check_results") or {}
            )
            # Author SR failures only — a reviewer-stamped override means the
            # author SR did NOT catch the category, so it must still count
            # toward `cat_added_value` when the human confirms the rescue.
            agent_failed = {
                c
                for c, r in check_results.items()
                if isinstance(r, dict)
                and not r.get("passed", True)
                and r.get("origin") not in _ALL_REVIEWER_ORIGINS
            }
            for finding in findings:
                cat = finding.get("category") if isinstance(finding, dict) else None
                if not cat:
                    continue
                cat_flagged[cat] = cat_flagged.get(cat, 0) + 1
                if cat in human_cats:
                    cat_confirmed[cat] = cat_confirmed.get(cat, 0) + 1
                    if cat not in agent_failed:
                        cat_added_value[cat] = cat_added_value.get(cat, 0) + 1

        return {
            "sample_size": len(with_reviewer),
            "cat_flagged": cat_flagged,
            "cat_confirmed": cat_confirmed,
            "cat_added_value": cat_added_value,
            "fa_rate_with": _fa_rate(with_reviewer),
            "fa_rate_without": _fa_rate(without_reviewer),
        }

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_stats: dict[str, Any] = {}
    previous_stats: dict[str, Any] = {}

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in _code_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_stats = _bucket_stats(task_by_cycle.get(c_cycle.id, []))
            p_stats = _bucket_stats(task_by_cycle.get(p_cycle.id, []))
            if (
                c_stats["sample_size"] >= _MIN_SR_SAMPLE
                and p_stats["sample_size"] >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_stats = c_stats
                previous_stats = p_stats
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        cal_current = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        cal_previous = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]
        current_stats = _bucket_stats(cal_current)
        previous_stats = _bucket_stats(cal_previous)

    if (
        current_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
        or previous_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
    ):
        return None

    def _classify(curr: Optional[float], prev: Optional[float]) -> str:
        if curr is None or prev is None:
            return "stable"
        delta = curr - prev
        if delta >= _TREND_DELTA_THRESHOLD:
            return "improving"
        if delta <= -_TREND_DELTA_THRESHOLD:
            return "regressing"
        return "stable"

    # Per-category hit rate
    c_flagged = current_stats["cat_flagged"]
    p_flagged = previous_stats["cat_flagged"]
    all_cats = set(c_flagged.keys()) | set(p_flagged.keys())

    per_category_hit_rate: dict[str, Any] = {}
    for cat in all_cats:
        c_total = c_flagged.get(cat, 0)
        p_total = p_flagged.get(cat, 0)
        c_rate = (
            round(current_stats["cat_confirmed"].get(cat, 0) / c_total, 3)
            if c_total >= 2
            else None
        )
        p_rate = (
            round(previous_stats["cat_confirmed"].get(cat, 0) / p_total, 3)
            if p_total >= 2
            else None
        )
        per_category_hit_rate[cat] = {
            "current": c_rate,
            "previous": p_rate,
            "delta": round(c_rate - p_rate, 3)
            if c_rate is not None and p_rate is not None
            else None,
            "trend": _classify(c_rate, p_rate),
        }

    # Added value (fresh-reviewer catches what agent self-review missed)
    c_av = current_stats["cat_added_value"]
    p_av = previous_stats["cat_added_value"]
    av_cats = set(c_av.keys()) | set(p_av.keys())
    added_value: dict[str, Any] = {}
    for cat in av_cats:
        c_cnt = c_av.get(cat, 0)
        p_cnt = p_av.get(cat, 0)
        added_value[cat] = {
            "current": c_cnt,
            "previous": p_cnt,
            "trend": _classify(float(c_cnt), float(p_cnt)),
        }

    # Approval rate lift
    c_with = current_stats["fa_rate_with"]
    c_without = current_stats["fa_rate_without"]
    p_with = previous_stats["fa_rate_with"]
    p_without = previous_stats["fa_rate_without"]
    c_lift = (
        round(c_with - c_without, 3)
        if c_with is not None and c_without is not None
        else None
    )
    p_lift = (
        round(p_with - p_without, 3)
        if p_with is not None and p_without is not None
        else None
    )
    approval_rate_lift: dict[str, Any] = {
        "current": c_lift,
        "previous": p_lift,
        "delta": round(c_lift - p_lift, 3)
        if c_lift is not None and p_lift is not None
        else None,
        "trend": _classify(c_lift, p_lift),
    }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current_sample_size": current_stats["sample_size"],
        "previous_sample_size": previous_stats["sample_size"],
        "per_category_hit_rate": per_category_hit_rate,
        "added_value": added_value,
        "approval_rate_lift": approval_rate_lift,
    }

    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["fresh_reviewer_trend"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[FRESH_REVIEWER_TREND] project=%s categories=%d bucket_mode=%s",
        project_id,
        len(per_category_hit_rate),
        result["bucket_mode"],
    )

    return result


async def compute_sr_reviewer_disagreements(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
    *,
    window_days: int = 30,
    limit: int = 20,
) -> Optional[dict[str, Any]]:
    """Aggregate categories where SR passed but the fresh reviewer flagged an issue.

    Scans ``all_tasks`` (already fetched by the caller) for completed
    code-change tasks that have both ``check_results`` and
    ``reviewer_findings`` in their ``_self_review_outcome``. A disagreement
    is counted for every (category, task_type) pair where the fresh reviewer
    raised a finding but self-review either omitted the category or marked it
    ``passed: true``.

    **Side effect**: writes the result to
    ``project.design_doc["sr_reviewer_disagreements"]`` so downstream readers
    (admin digest, agent project-context) can access pre-computed data without
    re-scanning tasks.

    Returns the result dict, or ``None`` when no disagreements are found.
    """
    from app.sr_blind_spots import _count_reviewer_disagreement_directions  # noqa: PLC0415

    now = _utcnow()
    cutoff = now - timedelta(days=window_days)

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    cutoff_naive = _naive(cutoff)

    eligible = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and (not t.created_at or _naive(t.created_at) >= cutoff_naive)
    ]
    direction_counts = _count_reviewer_disagreement_directions(eligible)

    if not any(bucket["miss"] > 0 for bucket in direction_counts.values()):
        return None

    top = sorted(
        [(k, v) for k, v in direction_counts.items() if v["miss"] > 0],
        key=lambda x: -x[1]["miss"],
    )[:limit]
    entries = [
        {
            "category": cat,
            "task_type": ttype,
            "count": bucket["miss"],
            "examples": bucket["examples"],
        }
        for (cat, ttype), bucket in top
    ]
    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "window_days": window_days,
        "disagreements": entries,
    }

    project = await session.get(Project, project_id)
    if project:
        doc = dict(project.design_doc or {})
        doc["sr_reviewer_disagreements"] = result
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

    logger.info(
        "[SR_REVIEWER_DISAGREEMENTS] project=%s entries=%d",
        project_id,
        len(entries),
    )
    return result


# ---------------------------------------------------------------------------
# Edges-considered cycle-over-cycle trend
# ---------------------------------------------------------------------------


async def compute_edges_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for edges_considered metrics.

    Mirrors the cycle-vs-calendar bucketing from ``compute_fresh_reviewer_trend``.
    Requires at least ``_MIN_SR_SAMPLE`` code-change tasks with
    ``_self_review_outcome`` in both the current and previous bucket.

    Primary trend signal: ``tasks_with_edges_rate`` — the fraction of code tasks
    that included an ``edges_considered`` list in their self-review.  Trend is
    classified as ``'improving'`` | ``'regressing'`` | ``'stable'`` relative to the
    previous bucket using ``_TREND_DELTA_THRESHOLD``.

    **Side effect**: writes result to ``project.design_doc['edges_trend']`` and
    maintains a top-level ``rate_history`` list (capped at 5 entries).  Append
    rule mirrors ``sr_catch_rate_trends``: in cycle mode append on new
    ``current_cycle_id``; in calendar mode append when ≥14 days since last
    ``computed_at``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifying_count(tasks: list) -> int:
        return sum(
            1
            for t in tasks
            if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
            and "_self_review_outcome" in (t.context or {})
        )

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if (
        _qualifying_count(current_bucket) < _MIN_SR_SAMPLE
        or _qualifying_count(previous_bucket) < _MIN_SR_SAMPLE
    ):
        return None

    current_stats = _compute_edges_stats(current_bucket)
    previous_stats = _compute_edges_stats(previous_bucket)

    if current_stats is None or previous_stats is None:
        return None

    c_rate = current_stats["tasks_with_edges_rate"]
    p_rate = previous_stats["tasks_with_edges_rate"]
    delta = round(c_rate - p_rate, 3)
    if delta >= _TREND_DELTA_THRESHOLD:
        trend = "improving"
    elif delta <= -_TREND_DELTA_THRESHOLD:
        trend = "regressing"
    else:
        trend = "stable"

    rc_current = current_stats.get("reviewer_caught_rate")
    rc_previous = previous_stats.get("reviewer_caught_rate")
    if rc_current is not None and rc_previous is not None:
        rc_delta = round(rc_current - rc_previous, 3)
        if rc_delta >= _TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "improving"
        elif rc_delta <= -_TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "regressing"
        else:
            reviewer_caught_trend = "stable"
    else:
        rc_delta = None
        reviewer_caught_trend = None

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": current_stats,
        "previous": previous_stats,
        "tasks_with_edges_rate_delta": delta,
        "trend": trend,
        "reviewer_caught_rate_delta": rc_delta,
        "reviewer_caught_trend": reviewer_caught_trend,
    }

    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing: dict = dict(doc.get("edges_trend") or {})
    existing_computed_at: Optional[str] = existing.get("computed_at")

    is_new_window = True
    if use_cycle_mode:
        is_new_window = existing.get("current_cycle_id") != current_cycle_id
    elif existing_computed_at:
        try:
            last_dt = datetime.fromisoformat(existing_computed_at)
            is_new_window = (now - last_dt).days >= 14
        except (ValueError, TypeError):
            pass

    rate_history: list = list(existing.get("rate_history") or [])
    if is_new_window or not rate_history:
        rate_history.append(c_rate)
        rate_history = rate_history[-5:]
    result["rate_history"] = rate_history

    doc["edges_trend"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[EDGES_TREND] project=%s trend=%s bucket_mode=%s",
        project_id,
        trend,
        result["bucket_mode"],
    )

    return result


# ---------------------------------------------------------------------------
# SR regression_risk vs testFix-loop signal cross-validation
# ---------------------------------------------------------------------------


async def compute_sr_regression_risk_test_signal_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-validate SR ``regression_risk.passed`` verdicts against testFix-loop signal.

    Companion metric to ``compute_sr_catch_rate_trends`` — that one counts a
    miss only when a human reviewer raised the ``regression_risk`` category.
    This one uses the agent's own testFix loop as an objective signal: when the
    test loop ran non-trivially, real regression surface existed regardless of
    whether any human reviewer flagged it.

    Per-task classification (qualifying = code-change task with a non-skip
    self-review outcome that includes ``check_results.regression_risk.passed``)::

      sr_passed_no_signal   (true-pass)             — SR ok, test loop quiet
      sr_passed_with_signal (miss-by-test-signal)   — SR ok, test loop ran
      sr_failed_with_signal (hit-by-test-signal)    — SR flagged, test loop ran
      sr_failed_no_signal   (false-flag)            — SR flagged, test loop quiet

    Test-signal predicate: ``len(history.get("test") or []) >= 2`` OR the last
    test attempt's ``exit_reason`` is non-``passed`` (covers single-iteration
    runs that diverged or maxed out attempts).

    Mirrors :func:`compute_sr_catch_rate_trends` cycle-vs-calendar bucketing
    and the :data:`_MIN_SR_SAMPLE` floor.

    **Side effect**: writes the result to
    ``project.design_doc["sr_regression_risk_test_signal_trends"]``.
    """
    from app.sr_blind_spots import is_skipped_sr_outcome

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rr = (sro.get("check_results") or {}).get("regression_risk")
        return isinstance(rr, dict) and "passed" in rr

    def _has_test_signal(task) -> bool:
        history = getattr(task, "fix_loop_history", None) or {}
        test_attempts = history.get("test") or []
        if len(test_attempts) >= 2:
            return True
        if test_attempts:
            last = test_attempts[-1]
            if isinstance(last, dict):
                reason = last.get("exit_reason")
                if reason and reason != "passed":
                    return True
        return False

    def _aggregate(tasks: list) -> dict[str, int]:
        stats = {
            "sr_passed_no_signal": 0,
            "sr_passed_with_signal": 0,
            "sr_failed_with_signal": 0,
            "sr_failed_no_signal": 0,
        }
        for t in tasks:
            if not _qualifies(t):
                continue
            sro = (t.context or {}).get("_self_review_outcome") or {}
            rr = (sro.get("check_results") or {}).get("regression_risk") or {}
            sr_passed = bool(rr.get("passed"))
            signal = _has_test_signal(t)
            if sr_passed and signal:
                stats["sr_passed_with_signal"] += 1
            elif sr_passed:
                stats["sr_passed_no_signal"] += 1
            elif signal:
                stats["sr_failed_with_signal"] += 1
            else:
                stats["sr_failed_no_signal"] += 1
        return stats

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    current_qual = _qualifying_count(current_bucket)
    previous_qual = _qualifying_count(previous_bucket)
    if current_qual < _MIN_SR_SAMPLE or previous_qual < _MIN_SR_SAMPLE:
        return None

    current_counts = _aggregate(current_bucket)
    previous_counts = _aggregate(previous_bucket)

    def _miss_rate(stats: dict[str, int]) -> tuple[Optional[float], int, int]:
        numerator = stats["sr_passed_with_signal"]
        denominator = stats["sr_passed_no_signal"] + stats["sr_passed_with_signal"]
        if denominator == 0:
            return None, numerator, denominator
        return round(numerator / denominator, 3), numerator, denominator

    current_rate, current_numer, current_denom = _miss_rate(current_counts)
    previous_rate, previous_numer, previous_denom = _miss_rate(previous_counts)

    if current_rate is not None and previous_rate is not None:
        delta = round(current_rate - previous_rate, 3)
        if delta >= _TREND_DELTA_THRESHOLD:
            trend = "regressing"
        elif delta <= -_TREND_DELTA_THRESHOLD:
            trend = "improving"
        else:
            trend = "stable"
    else:
        delta = None
        trend = "stable"

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": {
            **current_counts,
            "miss_by_test_signal_rate": current_rate,
            "numerator": current_numer,
            "denominator": current_denom,
            "sample_size": current_qual,
        },
        "previous": {
            **previous_counts,
            "miss_by_test_signal_rate": previous_rate,
            "numerator": previous_numer,
            "denominator": previous_denom,
            "sample_size": previous_qual,
        },
        "miss_by_test_signal_rate_delta": delta,
        "trend": trend,
    }

    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["sr_regression_risk_test_signal_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_REGRESSION_RISK_TEST_SIGNAL] project=%s trend=%s miss_rate=%s n=%d",
        project_id,
        trend,
        current_rate,
        current_qual,
    )

    return result


# ---------------------------------------------------------------------------
# SR false-attestation trends
# ---------------------------------------------------------------------------

_FALSE_ATTESTATION_RATE_THRESHOLD = 0.10
_FALSE_ATTESTATION_ALERT_TITLE_PREFIX = "SR false attestation rate elevated"


async def compute_sr_false_attestation_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-check SR execution-evidence pass claims against CI outcomes cycle-over-cycle.

    For each code-change task whose SR marks tests_executed_cleanly, lint_executed_cleanly,
    or typecheck_executed_cleanly as passed=True, checks context["review_loop"]["ci_passed"].
    Counts tasks where SR claimed passed but CI recorded ci_passed=False as false attestations.

    Mirrors :func:`compute_sr_regression_risk_test_signal_trends` cycle-vs-calendar bucketing.

    **Side effect**: writes the result to
    ``project.design_doc["sr_false_attestation_trends"]`` and emits a WARNING Alert when
    any category's false attestation rate exceeds _FALSE_ATTESTATION_RATE_THRESHOLD.
    """
    from app.sr_blind_spots import (
        aggregate_sr_false_attestation_rates,
        is_skipped_sr_outcome,
    )

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rl = ctx.get("review_loop")
        return rl is not None and "ci_passed" in rl

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).scalars().all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if _qualifying_count(current_bucket) < _MIN_SR_SAMPLE:
        return None

    current_rates = aggregate_sr_false_attestation_rates(current_bucket, min_sample=1)
    previous_rates = aggregate_sr_false_attestation_rates(previous_bucket, min_sample=1)

    if not current_rates:
        return None

    per_category: dict[str, Any] = {}
    for cat, curr in current_rates.items():
        prev = (previous_rates or {}).get(cat) or {}
        curr_rate = curr.get("false_attestation_rate")
        prev_rate = prev.get("false_attestation_rate")
        if curr_rate is not None and prev_rate is not None:
            delta = round(curr_rate - prev_rate, 3)
            trend = (
                "regressing"
                if delta >= _TREND_DELTA_THRESHOLD
                else "improving"
                if delta <= -_TREND_DELTA_THRESHOLD
                else "stable"
            )
        else:
            delta = None
            trend = "stable"
        per_category[cat] = {
            **curr,
            "previous_false_attestation_rate": prev_rate,
            "delta": delta,
            "trend": trend,
        }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "per_category": per_category,
    }

    project = await session.get(Project, project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})
    doc["sr_false_attestation_trends"] = result
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SR_FALSE_ATTESTATION] project=%s bucket=%s cats=%s",
        project_id,
        "cycle" if use_cycle_mode else "calendar",
        list(per_category.keys()),
    )

    elevated = {
        cat: data
        for cat, data in per_category.items()
        if (data.get("false_attestation_rate") or 0) > _FALSE_ATTESTATION_RATE_THRESHOLD
    }
    if elevated:
        try:
            from app.notification_dispatch import post_failure_alert_message  # noqa: PLC0415

            lines = [
                "SR false attestation rate elevated (SR claimed passed but CI failed):"
            ]
            for cat, data in sorted(elevated.items()):
                rate = data.get("false_attestation_rate")
                n = data.get("false_attested", 0)
                total = data.get("ci_confirmed", 0) + n
                lines.append(f"  • {cat}: {rate:.0%} ({n}/{total})")
            description = "\n".join(lines)
            await post_failure_alert_message(session, project_id, description)

            new_title = _format_sr_alert_title(
                _FALSE_ATTESTATION_ALERT_TITLE_PREFIX, sorted(elevated.keys())
            )
            existing_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(
                                f"{_FALSE_ATTESTATION_ALERT_TITLE_PREFIX}%"
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not any(a.title == new_title for a in existing_alerts):
                for a in existing_alerts:
                    a.dismissed_at = now
                    session.add(a)
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=new_title,
                        message=description,
                    )
                )
        except Exception:
            logger.exception(
                "[SR_FALSE_ATTESTATION] alert emission failed (non-critical)"
            )

    return result


# ---------------------------------------------------------------------------
# SR alert drilldown helper
# ---------------------------------------------------------------------------


def get_sr_alert_drilldown_tasks(
    all_tasks: list,
    design_doc: dict,
    categories: list[str],
    *,
    alert_type: str = "trends",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Return tasks from the current SR alert window where SR missed the given categories.

    Finds tasks where the human reviewer flagged at least one category from
    ``categories`` but SR did not (category absent from ``check_results`` or
    ``passed`` is True).  Window is derived from the stored alert state:
    - cycle_id present -> filter by ``strategic_cycle_id``
    - no cycle_id -> last 14 calendar days (calendar fallback)

    PR URLs are not fetched here; the calling endpoint should batch-query
    Artifact rows by the returned task_id list.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    alert_key = (
        "sr_catch_rate_trends_alert"
        if alert_type == "trends"
        else "sr_catch_rate_floor_alert"
    )
    alert_state: dict = dict(design_doc.get(alert_key) or {})
    cycle_id: Optional[str] = alert_state.get("cycle_id")
    target_cats = set(categories)

    if cycle_id:
        window_tasks = [
            t for t in all_tasks if getattr(t, "strategic_cycle_id", None) == cycle_id
        ]
    else:
        cutoff = _naive(now - timedelta(days=14))
        window_tasks = [
            t for t in all_tasks if t.created_at and _naive(t.created_at) >= cutoff
        ]

    results: list[dict[str, Any]] = []
    for t in window_tasks:
        ctx = t.context or {}
        if "_self_review_outcome" not in ctx:
            continue
        check_results = ctx["_self_review_outcome"].get("check_results") or {}
        # Author SR failures only — a reviewer-rescued category should still
        # surface in the drilldown as `missed_in_target` because the SR itself
        # did not catch it.
        sr_failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict)
            and not r.get("passed", True)
            and r.get("origin") not in _ALL_REVIEWER_ORIGINS
        }
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or [])
        missed_in_target = (human_cats & target_cats) - sr_failed
        if not missed_in_target:
            continue
        caught_in_target = human_cats & target_cats & sr_failed
        payload = t.first_payload or {}
        findings_list = ctx["_self_review_outcome"].get("reviewer_findings") or []
        findings_by_cat = {
            f["category"]: f
            for f in findings_list
            if isinstance(f, dict) and "category" in f
        }
        missed_category_findings: dict[str, Any] = {}
        for cat in missed_in_target:
            finding = findings_by_cat.get(cat)
            if finding:
                desc = finding.get("description") or ""
                missed_category_findings[cat] = {
                    "severity": finding.get("severity"),
                    "description": desc[:140],
                    "has_more": len(desc) > 140,
                    "full_description": desc if len(desc) > 140 else None,
                }
            else:
                missed_category_findings[cat] = {
                    "severity": None,
                    "description": "no description recorded",
                    "has_more": False,
                    "full_description": None,
                }
        results.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": payload.get("type", ""),
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "missed_categories": sorted(missed_in_target),
                "caught_categories": sorted(caught_in_target),
                "sr_verdict": ctx["_self_review_outcome"].get("verdict"),
                "missed_category_findings": missed_category_findings,
            }
        )

    results.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return results[:page_size]
