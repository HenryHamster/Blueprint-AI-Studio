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

from app.shared_state import DEFAULT_TASK_SCOPE

from sqlalchemy import func
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

# Review-extracted playbook entries are refreshed in-place when their
# updated_at exceeds _REVIEW_PLAYBOOK_REFRESH_DAYS, and flagged
# potentially_stale once they exceed _REVIEW_PLAYBOOK_STALE_DAYS without
# being observed again.
_REVIEW_PLAYBOOK_REFRESH_DAYS = 30
_REVIEW_PLAYBOOK_STALE_DAYS = 60

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
    # Trim oldest entries past the cap so the archive can't grow without
    # bound — see the payload budget rationale at _PLAYBOOK_ARCHIVE_MAX_DEFAULT.
    from app.shared_state import runtime_config as _rc  # noqa: PLC0415
    archive_max = _rc.get("playbook_archive_max", _PLAYBOOK_ARCHIVE_MAX_DEFAULT)
    if archive_max > 0 and len(archive) > archive_max:
        dropped = len(archive) - archive_max
        archive = archive[-archive_max:]
        logger.info(
            "[PLAYBOOK] Dropped %d oldest archive entries to stay within cap of %d",
            dropped, archive_max,
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
    base = re.sub(r'.*/', '', innermost)       # strip directory prefix
    return re.sub(r'\.py$', '', base) or None  # strip .py extension


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
            clean = re.sub(r'\s+', ' ', raw).strip()
            if module_name:
                # Inject module context: "importerror: auth: cannot import name foo"
                colon_idx = clean.find(':')
                if colon_idx >= 0:
                    exc_type = clean[:colon_idx].strip()
                    rest = clean[colon_idx + 1:].strip()
                    clean = f"{exc_type}: {module_name}: {rest}"
            return clean[:80]

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
    task_scope = (task.context or {}).get("scope", DEFAULT_TASK_SCOPE)
    signature = _extract_failure_signature(
        failure_class, error_summary, task_type, task_scope
    )
    # Store module context for cross-referencing with module_feedback_profiles
    module_name = (
        _extract_failure_module((error_summary or "").lower())
        if failure_class == "CODE_ERROR" else None
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
    module_note = f" Repeated failures in module '{module_name}'." if module_name else ""
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
    all_have_direction = all((t.context or {}).get("direction_id") for t in all_pattern_tasks)
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
        float(e["subtask_merge_rate"]) for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) >= 2
    ]
    low_xmod = [
        float(e["subtask_merge_rate"]) for e in flat
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
    for ht in rows:
        if ht.created_at and ht.created_at < cutoff:
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
        pairs = [(desc, len(tids)) for desc, tids in desc_task_ids.items() if len(tids) >= 2]
    else:
        pairs = [(desc, cnt) for desc, cnt in desc_counter.items() if cnt >= 2]
    if not pairs:
        return None
    pairs.sort(key=lambda x: -x[1])
    out = []
    for desc, cnt in pairs[:3]:
        entry = {"description": desc, "count": cnt, "category": desc_to_cat.get(desc, "other")}
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
    prior_count = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.project_id == task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.id != task.id,
            Task.payloads[0]["type"].as_string() == task_type,
        )
    )).scalar_one()
    same_type_count = prior_count + 1  # +1 for the current task

    if same_type_count < 3:
        logger.debug(
            "[SUCCESS_PLAYBOOK] project=%s type=%s: only %d completion(s) — noise filter not met",
            task.project_id, task_type, same_type_count,
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
        task.project_id, action, key, task_type, scope,
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
        affinity = int(dir_succ.get(direction_id, 0)) if direction_id and isinstance(dir_succ, dict) else 0
        scope_match = 1 if exact_key and k == exact_key else 0
        return (affinity, scope_match, int(v.get("success_count") or 0), v.get("updated_at") or "")

    candidates.sort(key=_rank, reverse=True)

    bullets: list[str] = []
    for k, v in candidates[:max_bullets]:
        scope_label = k[len(prefix):] or "unspecified"
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
    cat_counts: dict[str, int] = {cat: len(issues) for cat, issues in buckets.items() if issues}
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
        for evict_key, _ in sorted_by_age[: len(profiles) - _MODULE_FEEDBACK_MAX_ENTRIES]:
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
            _accumulate_direction_first_attempt(doc, direction_key, first_attempt_buckets)
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
            top_issues = sorted(issues, key=lambda x: severity_order.get(x["severity"], 3))[:3]
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

    if created_keys or module_profiles_updated or direction_tally_updated or review_entries_touched:
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
            ts = t.created_at.replace(tzinfo=None) if t.created_at.tzinfo else t.created_at
            if not (start <= ts < end):
                continue
            rejection_cats = (t.context or {}).get("_first_attempt_rejection_categories") or {}
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
    previous_count, previous_history = _split_history(all_tasks, cutoff_28d_n, cutoff_14d_n)

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
    curr_rejection_total, curr_cat_counts = _split_recurrence(all_tasks, cutoff_14d_n, sentinel_n)
    prev_rejection_total, prev_cat_counts = _split_recurrence(all_tasks, cutoff_28d_n, cutoff_14d_n)

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
            if curr_rejection_total > 0 else None
        )
        previous_recurrence_rate: Optional[float] = (
            round(prev_cat_counts.get(cat, 0) / prev_rejection_total, 3)
            if prev_rejection_total > 0 else None
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
        if (is_new_window or not recurrence_history) and data["recurrence_rate"] is not None:
            recurrence_history.append(data["recurrence_rate"])
            recurrence_history = recurrence_history[-5:]
        data["recurrence_history"] = recurrence_history

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
