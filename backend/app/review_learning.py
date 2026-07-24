"""Focused review-derived learning helpers extracted from playbook_learning.

This module owns review issue categorization, first-attempt classification
caching, recurring review summaries, review-playbook extraction, and
review-effectiveness tracking. ``app.playbook_learning`` re-exports these
symbols so existing callers keep their import surface.
"""

import json
import logging
import os
import uuid
from collections import Counter
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.inline_llm import (
    call_inline_llm_json,
    inline_llm_unavailable_reason,
    resolve_inline_llm_settings,
)
from app.models import Project, Task, _utcnow
from app.playbook_capacity import maybe_archive_for_capacity, _mark_stale_review_entries
from app.playbook_sr_metrics import (
    _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
    _SR_ENGAGEMENT_MIN_PASSES,
    _SR_ENGAGEMENT_THRESHOLD,
    _SR_FLOOR_THRESHOLD,
    _SR_SEVERITY_DELTA_THRESHOLDS,
    _TREND_DELTA_THRESHOLD,
)
from app.project_telemetry import (
    get_project_telemetry,
    read_telemetry_or_doc,
    set_project_telemetry,
    strip_migrated_doc_keys,
)
from app.time_utils import _to_naive_utc

logger = logging.getLogger(__name__)

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

_VALID_CATEGORIES = set(_REVIEW_CATEGORIES.keys()) | {"other"}
_MODULE_FEEDBACK_KEY = "module_feedback"
_MODULE_FEEDBACK_MAX_ENTRIES = 50
_MODULE_FEEDBACK_DECAY_AGE_DAYS = 14
_MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS = 14

_DIRECTION_FIRST_ATTEMPT_KEY = "direction_first_attempt_feedback"
_FIRST_ATTEMPT_PLAYBOOK_THRESHOLD = 5
_DIRECTION_FIRST_ATTEMPT_DECAY_AGE_DAYS = 14
_DIRECTION_FIRST_ATTEMPT_DECAY_HALF_LIFE_DAYS = 14

_REVIEW_PLAYBOOK_REFRESH_DAYS = 30

_MIN_REVIEW_SAMPLE = 10


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
    cached: dict | None = None,
) -> dict[str, list[dict]]:
    """Scan review_history entries and bucket issues by category."""
    if cached is not None:
        return cached
    return _categorize_review_issues_keyword(review_history)


async def categorize_review_issues_llm(issues: list[dict]) -> dict[str, list[dict]]:
    """LLM-backed categorizer with keyword fallback."""
    if (
        os.environ.get("DISABLE_LLM_REVIEW_CATEGORIZATION", "").strip() == "1"
        or resolve_inline_llm_settings("review_categorize").disabled
    ):
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
    data = await call_inline_llm_json(
        "review_categorize",
        prompt,
        required_keys=("classifications",),
    )
    if data is None:
        return _keyword_from_flat(issues)
    try:
        classifications = data["classifications"]
        buckets: dict[str, list[dict]] = {}
        for classification in classifications:
            category = classification["category"]
            if category not in _VALID_CATEGORIES:
                raise ValueError(f"unknown category: {category!r}")
            idx = classification["index"]
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise ValueError(f"invalid index: {idx!r}")
            if idx < 0 or idx >= len(issues):
                continue
            item = issues[idx]
            severity = (item.get("severity") or "").upper()
            if severity not in ("CRITICAL", "MAJOR", "MINOR"):
                continue
            buckets.setdefault(category, []).append(
                {
                    "desc": (item.get("description") or "").strip(),
                    "severity": severity,
                    "iteration": item.get("iteration", 0),
                }
            )
        return buckets
    except (KeyError, TypeError, ValueError) as exc:
        logger.info("categorize_review_issues_llm: malformed LLM output (%s), using keyword fallback", exc)
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
    """Run LLM categoriser (keyword fallback inside) and cache result in task_context."""
    if (
        os.environ.get("DISABLE_LLM_REVIEW_CATEGORIZATION", "").strip() == "1"
        or inline_llm_unavailable_reason("review_categorize")
    ):
        method = "keyword"
        buckets = _keyword_from_flat(issues)
    else:
        try:
            import anthropic as _anthropic

            _anthropic
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


def compute_recurring_first_review_descriptions(
    rows,
    cutoff,
    *,
    task_filter=None,
    min_tasks: int = 0,
    count_unique_tasks: bool = False,
    extra_fields: dict | None = None,
) -> list[dict] | None:
    """Top recurring issue descriptions from first-review (iteration 0) history."""
    desc_counter: Counter = Counter()
    desc_to_cat: dict = {}
    desc_task_ids: dict = {}
    filtered_task_count = 0

    cutoff_naive = _to_naive_utc(cutoff)
    for task in rows:
        task_created = _to_naive_utc(task.created_at)
        if task_created and cutoff_naive and task_created < cutoff_naive:
            continue
        if task_filter and not task_filter(task):
            continue
        review_loop = (task.context or {}).get("review_loop", {})
        review_history = review_loop.get("review_history", []) if review_loop else []
        if not review_history:
            continue
        filtered_task_count += 1
        for entry in review_history:
            if entry.get("iteration") != 0:
                continue
            for issue in entry.get("issues", []):
                desc = issue.get("description") or ""
                cat = issue.get("category", "other")
                if not desc:
                    continue
                normalized = desc.lower().strip()[:120]
                if count_unique_tasks:
                    desc_task_ids.setdefault(normalized, set()).add(task.id)
                else:
                    desc_counter[normalized] += 1
                if normalized not in desc_to_cat:
                    desc_to_cat[normalized] = cat
    if filtered_task_count < min_tasks:
        return None
    if count_unique_tasks:
        pairs = [
            (desc, len(task_ids))
            for desc, task_ids in desc_task_ids.items()
            if len(task_ids) >= 2
        ]
    else:
        pairs = [(desc, count) for desc, count in desc_counter.items() if count >= 2]
    if not pairs:
        return None
    pairs.sort(key=lambda item: -item[1])
    out = []
    for desc, count in pairs[:3]:
        entry = {
            "description": desc,
            "count": count,
            "category": desc_to_cat.get(desc, "other"),
        }
        if extra_fields:
            entry.update(extra_fields)
        out.append(entry)
    return out


def _decay_counts_entry(
    entry: dict, now: datetime, grace_days: float, half_life_days: float
) -> dict:
    """Return *entry* with integer category counts exponentially decayed by age."""
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
    for key, value in entry.items():
        if key in ("total", "last_updated"):
            continue
        if not isinstance(value, int):
            decayed[key] = value
            continue
        new_count = round(value * factor)
        if new_count > 0:
            decayed[key] = new_count
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
    """Update per-module feedback profiles in the design_doc dict (in-place)."""
    if not related_files or not buckets:
        return

    now = _utcnow()
    now_iso = now.isoformat()
    profiles: dict = dict(doc.get(_MODULE_FEEDBACK_KEY) or {})
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

    if len(profiles) > _MODULE_FEEDBACK_MAX_ENTRIES:
        sorted_by_age = sorted(
            profiles.items(),
            key=lambda item: item[1].get("last_updated", ""),
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
    """Accumulate first-attempt review rejection counts at the direction level."""
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
    """Create playbook entries for direction categories that hit the threshold."""
    from app.playbook_learning import _is_auto_playbook_suppressed

    tally = (doc.get(_DIRECTION_FIRST_ATTEMPT_KEY) or {}).get(direction_key) or {}
    created: list[str] = []
    for cat, count in tally.items():
        if cat in ("total", "last_updated") or not isinstance(count, int):
            continue
        if count < threshold:
            continue
        dir_slug = direction_key[:8].replace("-", "").replace("_", "") or "proj"
        key = f"first-attempt-{dir_slug}-{cat}"
        full_key = f"playbook/{key}"
        if full_key in doc or _is_auto_playbook_suppressed(doc, full_key):
            continue
        doc[full_key] = {
            "content": (
                f"First-attempt rejection pattern (direction {direction_key[:16]}): "
                f"{cat.replace('-', ' ')} issues have been flagged {count}x on first try. "
                f"Agents in this direction consistently miss {cat.replace('-', ' ')} requirements "
                f"before submitting. Pay special attention to {cat.replace('-', ' ')} before opening a PR."
            ),
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
    """Extract review feedback and accumulate per-direction learning on every merge."""
    iteration = review_loop.get("iteration", 0)
    review_history = review_loop.get("review_history") or []
    if not review_history:
        return []

    project_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = project_row.scalar_one_or_none()
    if not project:
        return []

    doc = dict(project.design_doc or {})

    # Hydrate archive from project_telemetry (with design_doc fallback) so
    # suppression checks and capacity-eviction see the real archive data.
    existing_archive = await get_project_telemetry(
        session, task.project_id, "playbook_archive", design_doc_fallback=doc
    )
    doc["playbook_archive"] = list(existing_archive or [])

    # Hydrate migrated telemetry keys that strip_migrated_doc_keys removes
    # from design_doc at write time. Without this, _update_module_feedback_profiles
    # and _accumulate_direction_first_attempt would start from empty data.
    existing_module_feedback = await get_project_telemetry(
        session, task.project_id, "module_feedback", design_doc_fallback=doc
    )
    if existing_module_feedback is not None:
        doc["module_feedback"] = dict(existing_module_feedback)
    existing_dfa = await get_project_telemetry(
        session, task.project_id, "direction_first_attempt_feedback", design_doc_fallback=doc
    )
    if existing_dfa is not None:
        doc["direction_first_attempt_feedback"] = dict(existing_dfa)

    buckets = _categorize_review_issues(review_history)
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    module_profiles_updated = bool(related_files and buckets)
    _update_module_feedback_profiles(doc, related_files, buckets)

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

    direction_id = (task.context or {}).get("direction_id")
    entry_scope = "module" if related_files else "universal"
    created_keys: list[str] = list(first_attempt_keys)
    review_entries_touched = _mark_stale_review_entries(doc)

    if iteration >= 3 and buckets:
        severity_weights = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
        top_cats = sorted(
            buckets.items(),
            key=lambda item: sum(
                severity_weights.get(issue["severity"], 1) for issue in item[1]
            ),
            reverse=True,
        )[:2]

        for cat_name, issues in top_cats:
            playbook_full_key = f"playbook/review-{cat_name}"
            severity_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
            top_issues = sorted(
                issues, key=lambda issue: severity_order.get(issue["severity"], 3)
            )[:3]
            examples = "; ".join(
                issue["desc"] for issue in top_issues if issue.get("desc")
            )
            content = (
                f"Review pattern from PR #{pr_number} ({iteration} iterations): "
                f"{cat_name.replace('-', ' ')} issues were repeatedly flagged. "
                f"Examples: {examples[:300]}. "
                f"Applicable to: {original_task_type} tasks."
            )

            existing = doc.get(playbook_full_key)
            if isinstance(existing, dict):
                entry = dict(existing)
                entry["hit_count"] = entry.get("hit_count", 1) + 1
                updated_at_str = entry.get("updated_at") or ""
                is_stale = True
                try:
                    updated_at = datetime.fromisoformat(
                        updated_at_str.replace("Z", "+00:00")
                    )
                    if updated_at.tzinfo is not None:
                        updated_at = updated_at.replace(tzinfo=None)
                    is_stale = (
                        _utcnow() - updated_at
                    ).days >= _REVIEW_PLAYBOOK_REFRESH_DAYS
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

            entry = {
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
        # Capture the migrated telemetry blobs BEFORE strip_migrated_doc_keys
        # removes them from doc — the set_project_telemetry writes below read
        # these values, and reading them after the strip persists empty dicts
        # (silent data loss for downstream telemetry consumers).
        updated_archive = list(doc.get("playbook_archive") or [])
        module_feedback_blob = dict(doc.get(_MODULE_FEEDBACK_KEY) or {})
        direction_tally_blob = dict(doc.get(_DIRECTION_FIRST_ATTEMPT_KEY) or {})
        doc = strip_migrated_doc_keys(doc)
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

        await set_project_telemetry(
            session,
            task.project_id,
            "playbook_archive",
            updated_archive,
        )

        if module_profiles_updated:
            await set_project_telemetry(
                session,
                task.project_id,
                _MODULE_FEEDBACK_KEY,
                module_feedback_blob,
            )

        if direction_tally_updated:
            await set_project_telemetry(
                session,
                task.project_id,
                _DIRECTION_FIRST_ATTEMPT_KEY,
                direction_tally_blob,
            )

        if created_keys:
            logger.info(
                "[REVIEW_PLAYBOOK] project=%s PR #%d iteration=%d created entries: %s",
                task.project_id,
                pr_number,
                iteration,
                created_keys,
            )

    return created_keys


async def read_direction_first_attempt_feedback(
    session: AsyncSession,
    project: Project,
) -> dict:
    """Return the direction first-attempt feedback tally, preferring the KV table.

    Prefers the ``project_telemetry`` row (key
    ``direction_first_attempt_feedback``) written by
    :func:`extract_review_feedback_to_playbook`, falling back to
    ``project.design_doc['direction_first_attempt_feedback']`` when the row is
    absent (pre-backfill or post-rollback). Returns ``{}`` when neither exists.
    """
    value = await read_telemetry_or_doc(
        session,
        project.id,
        project.design_doc,
        _DIRECTION_FIRST_ATTEMPT_KEY,
    )
    return value or {}


async def compute_review_effectiveness(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> dict[str, Any] | None:
    """Compare review issue category rates across two consecutive 14-day windows."""
    now = _utcnow()
    cutoff_14d = now - timedelta(days=14)
    cutoff_28d = now - timedelta(days=28)

    def _split_history(tasks: list, start: datetime, end: datetime) -> tuple[int, list]:
        tasks_with_reviews = 0
        history: list = []
        for task in tasks:
            if not task.created_at:
                continue
            created_at = (
                task.created_at.replace(tzinfo=None)
                if task.created_at.tzinfo
                else task.created_at
            )
            if not (start <= created_at < end):
                continue
            review_loop = (task.context or {}).get("review_loop") or {}
            review_history = review_loop.get("review_history") or []
            if review_history:
                tasks_with_reviews += 1
                history.extend(review_history)
        return tasks_with_reviews, history

    def _split_recurrence(
        tasks: list, start: datetime, end: datetime
    ) -> tuple[int, dict[str, int]]:
        total_with_rejection = 0
        cat_task_counts: dict[str, int] = {}
        for task in tasks:
            if not task.created_at:
                continue
            created_at = (
                task.created_at.replace(tzinfo=None)
                if task.created_at.tzinfo
                else task.created_at
            )
            if not (start <= created_at < end):
                continue
            rejection_cats = (task.context or {}).get(
                "_first_attempt_rejection_categories"
            ) or {}
            if not rejection_cats:
                continue
            total_with_rejection += 1
            for cat in rejection_cats:
                cat_task_counts[cat] = cat_task_counts.get(cat, 0) + 1
        return total_with_rejection, cat_task_counts

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

    curr_rejection_total, curr_cat_counts = _split_recurrence(
        all_tasks, cutoff_14d_n, sentinel_n
    )
    prev_rejection_total, prev_cat_counts = _split_recurrence(
        all_tasks, cutoff_28d_n, cutoff_14d_n
    )

    categories: dict[str, Any] = {}
    for cat in set(current_buckets.keys()) | set(previous_buckets.keys()):
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
            "recurrence_rate": (
                round(curr_cat_counts.get(cat, 0) / curr_rejection_total, 3)
                if curr_rejection_total > 0
                else None
            ),
            "previous_recurrence_rate": (
                round(prev_cat_counts.get(cat, 0) / prev_rejection_total, 3)
                if prev_rejection_total > 0
                else None
            ),
        }

    project_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = project_row.scalar_one_or_none()
    if not project:
        return None

    doc = dict(project.design_doc or {})
    existing_eff: dict = dict(doc.get("review_effectiveness") or {})
    existing_computed_at: str | None = existing_eff.get("computed_at")
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

    persistent_issues = [
        cat
        for cat, data in categories.items()
        if len(data["rate_history"]) >= 3
        and all(rate > 0.20 for rate in data["rate_history"][-3:])
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


__all__ = [
    "_DIRECTION_FIRST_ATTEMPT_KEY",
    "_FIRST_ATTEMPT_PLAYBOOK_THRESHOLD",
    "_FLOOR_MIN_CONSECUTIVE_WINDOWS",
    "_MIN_REVIEW_SAMPLE",
    "_MODULE_FEEDBACK_KEY",
    "_PRECISION_RECALL_QUADRANT_THRESHOLD",
    "_REVIEW_CATEGORIES",
    "_SR_ENGAGEMENT_MIN_PASSES",
    "_SR_ENGAGEMENT_THRESHOLD",
    "_SR_FLOOR_THRESHOLD",
    "_SR_SEVERITY_DELTA_THRESHOLDS",
    "_TREND_DELTA_THRESHOLD",
    "_VALID_CATEGORIES",
    "_accumulate_direction_first_attempt",
    "_categorize_review_issues",
    "_categorize_review_issues_keyword",
    "_decay_module_feedback_entry",
    "_keyword_from_flat",
    "_maybe_auto_playbook_from_first_attempt",
    "_update_module_feedback_profiles",
    "cache_first_attempt_classification",
    "categorize_review_issues_llm",
    "compute_recurring_first_review_descriptions",
    "compute_review_effectiveness",
    "extract_review_feedback_to_playbook",
    "read_direction_first_attempt_feedback",
]
