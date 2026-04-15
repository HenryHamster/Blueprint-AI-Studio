"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)
"""
import logging
import re
from datetime import timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, Project, TaskStatus, _utcnow

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
}

logger = logging.getLogger(__name__)


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
            for cat_name, keywords in _REVIEW_CATEGORIES.items():
                if any(kw in desc_lower for kw in keywords):
                    buckets.setdefault(cat_name, []).append({
                        "desc": desc,
                        "severity": severity,
                        "iteration": entry.get("iteration", 0),
                    })
                    break  # first matching category wins

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
        doc[playbook_full_key] = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "success_count": 1,
            "source": "success-pattern",
            "applicable_types": [task_type],
        }
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

        doc[playbook_full_key] = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "source": "review-extracted",
            "applicable_types": [original_task_type],
            "pr_number": pr_number,
            "iteration_count": iteration,
        }
        created_keys.append(f"review-{cat_name}")

    if created_keys:
        meta = dict(doc.get("_meta", {"revision": 0}))
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "review-playbook"
        doc["_meta"] = meta

        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

        logger.info(
            "[REVIEW_PLAYBOOK] project=%s PR #%d iteration=%d created entries: %s",
            task.project_id, pr_number, iteration, created_keys,
        )

    return created_keys
