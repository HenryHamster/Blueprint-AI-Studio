"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, Project, TaskStatus, _utcnow

# ---------------------------------------------------------------------------
# Lifecycle constants
# ---------------------------------------------------------------------------

# Total playbook size (JSON-serialised) above which the least-useful entry is
# archived before a new one is added.  Leaves ~5 KB headroom below the 30 KB
# total limit.
_PLAYBOOK_CAPACITY_BYTES = 25 * 1024

# Entries older than this (in days) with fewer than _STALE_CONFIRM_THRESHOLD
# confirmed uses are deprioritised during the claim sort.
_STALE_AGE_DAYS = 30
_STALE_CONFIRM_THRESHOLD = 3

# Regex patterns that match source-file paths embedded in playbook entry content.
# Used to detect references to files that may have been renamed or removed.
_FILE_PATH_RE = re.compile(
    r'\b((?:backend|agent|admin-ui)/(?:[\w.-]+/)*[\w.-]+\.\w+)\b'
)

# ---------------------------------------------------------------------------
# Review feedback category keywords
# ---------------------------------------------------------------------------

_REVIEW_CATEGORIES: dict[str, list[str]] = {
    "test-coverage": [
        "test", "coverage", "assert", "unit test", "integration test",
        "spec", "test case", "test suite", "missing test", "untested",
    ],
    "error-handling": [
        "error handling", "exception", "try/catch", "try except", "fallback",
        "guard", "null check", "none check", "unhandled", "raise",
    ],
    "security": [
        "security", "injection", "xss", "csrf", "sanitize", "validate input",
        "auth", "permission", "secret", "credential", "escape",
    ],
    "style-conventions": [
        "style", "format", "naming", "convention", "whitespace", "indentation",
        "pep8", "lint", "camelcase", "snake_case", "consistent",
    ],
    "type-safety": [
        "type hint", "type annotation", "typing", "typevar", "optional",
        "union", "cast", "mypy", "type error", "untyped",
    ],
    "documentation": [
        "docstring", "documentation", "comment", "readme", "inline comment",
        "missing doc", "undocumented",
    ],
    "performance": [
        "performance", "optimize", "cache", "slow", "n+1", "index", "memory",
        "efficiency", "bottleneck",
    ],
    "architecture": [
        "refactor", "separation of concerns", "coupling", "dependency",
        "abstraction", "pattern", "interface", "modularity", "cohesion",
    ],
    "correctness": [
        "logic", "bug", "incorrect", "wrong", "off-by-one", "return value",
        "edge case", "boundary", "race condition", "deadlock", "missing import",
        "undefined", "null reference", "assertion", "invariant", "constraint",
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
        any_missing = any(
            not os.path.isfile(os.path.join(repo_root, p)) for p in paths
        )
        if any_missing and not v.get("stale_file_refs"):
            entry = dict(v)
            entry["stale_file_refs"] = True
            doc[k] = entry
            updated = True
            logger.debug(
                "[PLAYBOOK] Flagged '%s' as stale_file_refs (missing paths: %s)",
                k, [p for p in paths if not os.path.isfile(os.path.join(repo_root, p))],
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


def _archive_least_useful_entry(doc: dict) -> dict:
    """Move the least-useful playbook entry to ``playbook_archive`` in *doc*.

    "Least useful" is determined by a sort over:
    1. ``stale_file_refs`` flag (stale first)
    2. Inverse ``confirmed_count`` (least confirmed first)
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
        # Sort: stale first, then least-confirmed, then oldest, then key
        candidates.append((stale, -confirmed, age_days, k))

    if not candidates:
        return doc

    candidates.sort(reverse=True)  # highest (stale, -confirmed, age) first — oldest first within same tier
    _, _, _, victim_key = candidates[0]
    victim_entry = dict(doc.pop(victim_key))
    victim_entry["_archived_key"] = victim_key
    victim_entry["_archived_at"] = now.isoformat()

    archive = list(doc.get("playbook_archive") or [])
    archive.append(victim_entry)
    doc["playbook_archive"] = archive

    logger.info(
        "[PLAYBOOK] Archived entry '%s' to playbook_archive (capacity cleanup)",
        victim_key,
    )
    return doc


def maybe_archive_for_capacity(doc: dict, new_entry_estimate: int = 0) -> dict:
    """Archive the least-useful playbook entry if total size would exceed capacity.

    Call this *before* adding a new entry.  *new_entry_estimate* is the
    approximate byte size of the entry that is about to be added.

    Returns the (possibly mutated) doc.
    """
    if _playbook_size_bytes(doc) + new_entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        doc = _archive_least_useful_entry(doc)
    return doc


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------

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
    """
    error_lower = (error_summary or "").lower()

    # Timeouts are identified by (type, scope) — the error text varies too much
    if failure_class == "AGENT_TIMEOUT":
        return f"timeout-{task_type or 'unknown'}-{scope or 'medium'}"

    if failure_class == "CODE_ERROR":
        # Well-known Python exception names give stable signatures
        python_exc = re.search(
            r'(modulenotfounderror|importerror|typeerror|attributeerror|'
            r'valueerror|nameerror|keyerror|syntaxerror|assertionerror)'
            r'[:\s][^\n]{0,60}',
            error_lower,
        )
        if python_exc:
            raw = python_exc.group(0)
            # Drop file-path tokens (contain /) and line-number suffixes
            raw = re.sub(r'\S*/\S+', '', raw)
            raw = re.sub(r':\d+', ':', raw)
            return re.sub(r'\s+', ' ', raw).strip()[:80]

        # Test-runner failure lines: "FAILED backend/testing/test_foo.py"
        fail_match = re.search(r'fail(?:ed)?[\s:]+[\w/_.-]+\.py', error_lower)
        if fail_match:
            raw = fail_match.group(0)
            raw = re.sub(r'\S+/', '', raw)  # keep filename only
            return raw.strip()[:60]

    # Generic fallback: first meaningful line, stripped of paths/numbers
    for line in error_lower.split('\n'):
        line = line.strip()
        if line:
            line = re.sub(r'\S+/', '', line)   # strip path prefixes
            line = re.sub(r':\d+', ':', line)  # strip line numbers
            return line[:80]

    return f"{failure_class.lower()}-unknown"


def _signature_to_key(signature: str) -> str:
    """Convert a signature string to a valid playbook key (1-50 alphanumeric/hyphen chars)."""
    normalized = re.sub(r'[^a-zA-Z0-9]+', '-', signature.lower())
    normalized = normalized.strip('-')
    normalized = re.sub(r'-+', '-', normalized)
    if not normalized:
        normalized = "unknown"
    # Prefix with "auto" so the key always starts with an alphanumeric char
    key = ("auto-" + normalized)[:50].rstrip('-')
    return key


def _hint_for_failure_class(failure_class: str) -> str:
    """Return a short actionable hint for the given failure class."""
    return {
        "CODE_ERROR": (
            "Check imports, syntax, and test assertions. "
            "Run lint before committing."
        ),
        "AGENT_TIMEOUT": (
            "Reduce scope. Focus on one critical change. "
            "Commit working changes early."
        ),
        "PROVISION_FAILURE": (
            "Infrastructure issue. "
            "Check GCP quota and zone availability."
        ),
        "EXTERNAL_API": (
            "External service failure. "
            "May be transient — retry after checking API status."
        ),
        "RATE_LIMIT": (
            "API rate limit hit. "
            "Reduce call frequency or add exponential backoff."
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
    task_scope = (task.context or {}).get("scope", "medium")
    signature = _extract_failure_signature(
        failure_class, error_summary, task_type, task_scope
    )

    ctx = dict(task.context or {})
    ctx["_failure_signature"] = signature
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)

    # ── 2. Count recent tasks with same signature (excluding current task) ──
    window_start = _utcnow() - timedelta(days=window_days)
    result = await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            Task.created_at >= window_start,
            Task.id != task.id,
        )
    )
    recent_tasks = result.scalars().all()

    matching = [
        t for t in recent_tasks
        if (t.context or {}).get("_failure_signature") == signature
    ]
    total_count = len(matching) + 1  # +1 for the current task

    if total_count < threshold:
        logger.debug(
            "[AUTO_PLAYBOOK] project=%s signature='%s' seen %d/%d — no entry yet",
            task.project_id, signature, total_count, threshold,
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
            task.project_id, key,
        )
        return None

    # ── 4. Create auto-generated playbook entry ─────────────────────────────
    recent_ids = [str(t.id) for t in matching[-2:]] + [str(task.id)]
    hint = _hint_for_failure_class(failure_class)
    content = (
        f"Repeated failure pattern ({total_count}x in {window_days}d): {signature}. "
        f"Recent tasks: {', '.join(recent_ids)}. "
        f"Common fix: {hint}"
    )

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
        task.project_id, key, signature, total_count,
    )
    return key


# ---------------------------------------------------------------------------
# Review-feedback extraction
# ---------------------------------------------------------------------------

def _categorize_review_issues(review_history: list) -> dict[str, list[dict]]:
    """Scan review_history entries and bucket issues by category.

    Returns a mapping of {category_name: [{"desc": ..., "severity": ..., "iteration": ...}]}.
    Only CRITICAL/MAJOR/MINOR severity issues are considered.
    Each issue is assigned to the *first* matching category.
    """
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
                    buckets.setdefault(cat_name, []).append({
                        "desc": desc,
                        "severity": severity,
                        "iteration": entry.get("iteration", 0),
                    })
                    matched = True
                    break  # first matching category wins
            if not matched:
                buckets.setdefault("other", []).append({
                    "desc": desc,
                    "severity": severity,
                    "iteration": entry.get("iteration", 0),
                })

    return buckets


# ---------------------------------------------------------------------------
# Success-pattern capture
# ---------------------------------------------------------------------------

_PLAYBOOK_DEFAULT_TYPES = frozenset({
    "create", "fix", "refactor", "review", "qa", "smoketest",
    "deps", "migrate", "debug", "docs",
})


async def capture_success_pattern(
    task: Task,
    session: AsyncSession,
) -> Optional[str]:
    """Capture a positive pattern from a first-attempt success as a playbook entry.

    Only creates an entry when the task type already has failure-derived playbook
    entries (``source='auto-generated'``, ``source='review-extracted'``, or a
    ``playbook/auto-*`` key written by ``_check_recurring_failures``).  This
    sampling gate ensures the playbook only records successes for task types that
    are known to be challenging, avoiding noise from routine completions.

    One entry per task type (key ``playbook/success-{task_type}``) is maintained.
    If the entry already exists its ``success_count`` is incremented and the
    content refreshed.  Entries are tagged ``source='success-pattern'`` to
    distinguish them from failure-derived entries and participate in the same
    ``confirmed_count``-based priority ordering.

    Returns the short key (e.g. ``"success-create"``) if an entry was created or
    updated, ``None`` if the sampling gate was not met.
    """
    task_type = task.first_payload.get("type", "")
    if not task_type:
        return None

    project = await session.get(Project, task.project_id)
    if not project:
        return None

    doc = dict(project.design_doc or {})

    # Sampling gate: require at least one failure-derived entry applicable to
    # this task type before capturing a success pattern.
    has_negative_entry = False
    for pk, pv in doc.items():
        if not pk.startswith("playbook/") or not isinstance(pv, dict):
            continue
        entry_source = pv.get("source", "")
        # _check_recurring_failures writes auto-* keys but omits "source"; detect by key prefix.
        is_negative = (
            entry_source in ("auto-generated", "review-extracted")
            or pk.startswith("playbook/auto-")
        )
        if not is_negative:
            continue
        applicable = pv.get("applicable_types")
        if applicable is not None:
            if task_type in applicable:
                has_negative_entry = True
                break
        elif task_type in _PLAYBOOK_DEFAULT_TYPES:
            # Entries without applicable_types apply to all default task types.
            has_negative_entry = True
            break

    if not has_negative_entry:
        return None

    # One entry per task type — create on first observation, update thereafter.
    key = f"success-{task_type}"
    playbook_full_key = f"playbook/{key}"

    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    scope: str = (task.context or {}).get("scope") or ""
    files_note = f" Files: {', '.join(related_files[:5])}." if related_files else ""

    existing = doc.get(playbook_full_key)
    if existing and isinstance(existing, dict):
        entry = dict(existing)
        count = entry.get("success_count", 1) + 1
        entry["success_count"] = count
        entry["updated_at"] = _utcnow().isoformat()
        entry["content"] = (
            f"First-attempt success pattern for {task_type} tasks "
            f"({count} observations, latest scope: {scope or 'unspecified'}).{files_note} "
            f"These tasks completed cleanly without review iterations despite "
            f"this task type having prior failures in this project."
        )
        doc[playbook_full_key] = entry
        action = "updated"
    else:
        content = (
            f"First-attempt success pattern for {task_type} tasks "
            f"(1 observation, scope: {scope or 'unspecified'}).{files_note} "
            f"This task completed cleanly without review iterations despite "
            f"this task type having prior failures in this project."
        )
        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "success_count": 1,
            "source": "success-pattern",
            "applicable_types": [task_type],
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
        "[SUCCESS_PLAYBOOK] project=%s %s entry '%s' for task_type=%s",
        task.project_id, action, key, task_type,
    )
    return key


_MODULE_FEEDBACK_KEY = "module_feedback"
_MODULE_FEEDBACK_MAX_ENTRIES = 50


def _update_module_feedback_profiles(
    doc: dict,
    related_files: list[str],
    buckets: dict[str, list[dict]],
) -> None:
    """Update per-module feedback profiles in the design_doc dict (in-place).

    For each file in *related_files*, accumulates the count of issues per
    review category.  Caps the total number of tracked modules at
    ``_MODULE_FEEDBACK_MAX_ENTRIES``; when the cap is exceeded the entry with
    the oldest ``last_updated`` timestamp is evicted.
    """
    if not related_files or not buckets:
        return

    now_iso = _utcnow().isoformat()
    profiles: dict = dict(doc.get(_MODULE_FEEDBACK_KEY) or {})

    # Flatten bucket issue counts: {category: count}
    cat_counts: dict[str, int] = {cat: len(issues) for cat, issues in buckets.items() if issues}
    if not cat_counts:
        return

    for file_path in related_files:
        if not file_path:
            continue
        entry = dict(profiles.get(file_path) or {})
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
        for evict_key, _ in sorted_by_age[: len(profiles) - _MODULE_FEEDBACK_MAX_ENTRIES]:
            del profiles[evict_key]

    doc[_MODULE_FEEDBACK_KEY] = profiles


async def extract_review_feedback_to_playbook(
    task: Task,
    review_loop: dict,
    pr_number: int,
    original_task_type: str,
    session: AsyncSession,
) -> list[str]:
    """Extract recurring review feedback themes and persist them as playbook entries.

    Called after a PR merges following a multi-iteration review cycle (3+ iterations).
    Creates at most 2 entries per completed review cycle, tagged with
    ``source='review-extracted'`` so they are distinguishable from failure-pattern
    and manually written entries.

    Deduplicates by playbook key: if ``playbook/review-<category>`` already exists
    for the project the entry is skipped.

    Returns the list of created playbook keys (may be empty).
    """
    iteration = review_loop.get("iteration", 0)
    review_history = review_loop.get("review_history") or []

    if iteration < 3 or not review_history:
        return []

    buckets = _categorize_review_issues(review_history)
    if not buckets:
        return []

    project = await session.get(Project, task.project_id)
    if not project:
        return []

    doc = dict(project.design_doc or {})

    # Update per-module feedback profiles with files from this task
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    module_profiles_updated = bool(related_files and buckets)
    _update_module_feedback_profiles(doc, related_files, buckets)

    # Pick the top-2 categories by severity-weighted score (CRITICAL=3, MAJOR=2, MINOR=1)
    _severity_weights = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    top_cats = sorted(
        buckets.items(),
        key=lambda kv: sum(_severity_weights.get(i["severity"], 1) for i in kv[1]),
        reverse=True,
    )[:2]

    created_keys: list[str] = []
    for cat_name, issues in top_cats:
        playbook_full_key = f"playbook/review-{cat_name}"

        if playbook_full_key in doc:
            logger.debug(
                "[REVIEW_PLAYBOOK] project=%s key='%s' already exists — skipping",
                task.project_id, playbook_full_key,
            )
            continue

        # Surface up to 3 highest-severity examples
        severity_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
        top_issues = sorted(issues, key=lambda x: severity_order.get(x["severity"], 3))[:3]
        examples = "; ".join(i["desc"] for i in top_issues if i.get("desc"))

        content = (
            f"Review pattern from PR #{pr_number} ({iteration} iterations): "
            f"{cat_name.replace('-', ' ')} issues were repeatedly flagged. "
            f"Examples: {examples[:300]}. "
            f"Applicable to: {original_task_type} tasks."
        )

        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "source": "review-extracted",
            "applicable_types": [original_task_type],
            "pr_number": pr_number,
            "iteration_count": iteration,
        }
        doc = maybe_archive_for_capacity(doc, len(json.dumps(new_entry).encode()))
        doc[playbook_full_key] = new_entry
        created_keys.append(f"review-{cat_name}")

    if created_keys or module_profiles_updated:
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
                task.project_id, pr_number, iteration, created_keys,
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
            ts = t.created_at.replace(tzinfo=None) if t.created_at.tzinfo else t.created_at
            if not (start <= ts < end):
                continue
            review_loop = (t.context or {}).get("review_loop") or {}
            rev_hist = review_loop.get("review_history") or []
            if rev_hist:
                tasks_with_rev += 1
                history.extend(rev_hist)
        return tasks_with_rev, history

    # Use a far-future sentinel with no tzinfo to handle both naive and aware datetimes.
    # The cutoff datetimes from _utcnow() are tz-aware; strip tzinfo for comparison so
    # the function works with both naive (DB default) and aware created_at values.
    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    cutoff_14d_n = _naive(cutoff_14d)
    cutoff_28d_n = _naive(cutoff_28d)
    sentinel_n = datetime(9999, 12, 31)

    current_count, current_history = _split_history(all_tasks, cutoff_14d_n, sentinel_n)
    previous_count, previous_history = _split_history(all_tasks, cutoff_28d_n, cutoff_14d_n)

    if current_count < _MIN_REVIEW_SAMPLE or previous_count < _MIN_REVIEW_SAMPLE:
        return None

    current_buckets = _categorize_review_issues(current_history)
    previous_buckets = _categorize_review_issues(previous_history)

    current_total = sum(len(v) for v in current_buckets.values())
    previous_total = sum(len(v) for v in previous_buckets.values())

    if current_total == 0 or previous_total == 0:
        return None

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

        categories[cat] = {
            "current_rate": current_rate,
            "previous_rate": previous_rate,
            "trend": trend,
            "current_sample_size": current_count,
            "previous_sample_size": previous_count,
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

    # Categories above 20 % in each of their last 3 stored windows are flagged as
    # persistent — a signal that structural changes may be needed rather than
    # relying on agent-level awareness.
    persistent_issues = [
        cat for cat, data in categories.items()
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
        project_id, len(categories), persistent_issues,
    )
    return result
