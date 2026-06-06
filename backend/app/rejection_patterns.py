"""Per-task-type first-review rejection patterns for create/fix prompt context.

The review pipeline already records WHY first attempts get rejected —
``task.context["_first_attempt_rejection_categories"]`` is written on the
original task by the review-completion path in ``task_lifecycle`` (category
vocabulary in ``review_learning._REVIEW_CATEGORIES``), and the digest
aggregates it per type in
``digest_metrics._compute_first_review_approval_breakdowns`` — but the next
create/fix agent never saw the patterns. This module closes that loop:

* :func:`aggregate_rejection_patterns` scans recent tasks and produces a
  compact, time-decayed top-categories summary per task type.
* :func:`build_rejection_patterns_block` slices out the claiming task's own
  type for claim-time ``project_context`` injection (``routers/agent.py``,
  alongside the other SR/review context fields).
* :func:`inject_rejection_patterns` is the LOW-priority budget gate: the
  block ships only when the fully-assembled project_context leaves room
  under a soft byte budget, so under budget pressure it is the first field
  to drop and never displaces mission-critical context.

Framing matters: this is CONTEXT, not a checklist. The agent-side renderer
(``agent/src/prompts/context-prefix.ts:formatRejectionPatternsSection``)
presents the categories as observed frequencies from other tasks with one
line of guidance each, plus an explicit instruction not to over-index on
the listed categories.

NOTE: like ``sr_blind_spots``, there is no cached aggregate today — callers
scan up to ``_TASK_SCAN_CAP`` recent tasks per request. Acceptable at
current claim volumes; the ``sr_catch_rate_trends`` precompute pattern is
the future optimization if this shows up in claim latency.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Task
from app.time_utils import _coerce_naive_utc, _utcnow

logger = logging.getLogger(__name__)

# Recent window the aggregation covers. Deliberately short (vs the 14-day
# SR windows): rejection patterns shift as prompts/reviewers evolve, and a
# stale prior is worse than none.
_WINDOW_DAYS = 7

# Cap on recent-task scan, mirroring sr_blind_spots._TASK_SCAN_CAP.
_TASK_SCAN_CAP = 2000

# Top categories surfaced per task type — keeps the injected block (and the
# rendered prompt section) compact: a few lines, not a taxonomy dump.
_TOP_CATEGORIES = 3

# Minimum rejected-first-attempt tasks of a type inside the window before
# its patterns are surfaced. Below this the "pattern" is one or two tasks'
# noise, not a prior worth steering on.
_MIN_REJECTED_SAMPLE = 3

# Soft byte budget for the assembled claim-time project_context (mirrors
# payload_schemas.pack_under_budget's default 120 KB budget). The rejection
# block is packed LOW priority against this: it is injected last, only when
# everything already assembled leaves room.
_PROJECT_CONTEXT_SOFT_BUDGET_BYTES = 120 * 1024

# One line of actionable guidance per rejection category. Keys match the
# review_learning._REVIEW_CATEGORIES vocabulary (plus "other"); unknown or
# future categories fall back to _DEFAULT_GUIDANCE so the block never
# crashes on vocabulary drift.
_CATEGORY_GUIDANCE: Dict[str, str] = {
    "regression_risk": (
        "Run the tests that exercise the code you changed and confirm "
        "previously-passing behavior still holds before declaring done."
    ),
    "test-coverage": (
        "Add or update tests for the behavior you change — untested diffs "
        "are a leading first-review rejection."
    ),
    "error-handling": (
        "Handle the failure paths (exceptions, None/null, fallbacks) of any "
        "new I/O, parsing, or external calls you introduce."
    ),
    "security": (
        "Validate inputs and re-check auth/permission boundaries on any new "
        "or changed endpoint or data path."
    ),
    "style-conventions": (
        "Match the surrounding file's existing style and run the project's "
        "linter on touched files before submitting."
    ),
    "type-safety": (
        "Keep type annotations accurate and run the type checker on the "
        "files you touched."
    ),
    "documentation": (
        "Update docstrings and comments your change makes stale; document "
        "non-obvious new behavior."
    ),
    "performance": (
        "Watch for N+1 queries, unbounded scans, and per-request recompute "
        "on hot paths you touch."
    ),
    "architecture": (
        "Respect existing module boundaries — extend the seam that already "
        "owns the concern instead of reaching across layers."
    ),
    "correctness": (
        "Re-check edge cases and boundary conditions in the logic you "
        "changed; trace at least one end-to-end path by hand."
    ),
    "scope-drift": (
        "Keep the diff focused on the task — unrelated changes get PRs "
        "rejected even when the core change is right."
    ),
    "cross-system-contract": (
        "When a change spans frontend/backend, verify both sides of the "
        "contract (routes, DTOs, paths) actually agree."
    ),
    "other": (
        "These rejections didn't fit a standard category — read your "
        "direction's recent review feedback for specifics."
    ),
}

_DEFAULT_GUIDANCE = (
    "Recent first reviews flagged this repeatedly — give it an explicit "
    "pass before submitting."
)


def guidance_for_category(category: str) -> str:
    """One-line guidance for *category*, falling back for unknown vocabulary."""
    return _CATEGORY_GUIDANCE.get(category, _DEFAULT_GUIDANCE)


def _decay_weight(age_days: float, half_life_days: float) -> float:
    """Exponential time-decay weight for a rejection *age_days* old."""
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


async def aggregate_rejection_patterns(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int = _WINDOW_DAYS,
    top_n: int = _TOP_CATEGORIES,
    min_sample: int = _MIN_REJECTED_SAMPLE,
    now: datetime | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate first-review rejection categories per task type.

    Scans tasks created within the last *days* days (capped at
    ``_TASK_SCAN_CAP``) whose context carries
    ``_first_attempt_rejection_categories`` — the marker written by review
    completion when the FIRST review of a task's PR rejected it. Task status
    is irrelevant: the rejection already happened, however the task ended.

    Each rejected task contributes once per category it was rejected for
    (issue counts within a task deliberately don't inflate the pattern),
    weighted by exponential time decay with a half-life of ``days / 2`` so
    yesterday's rejection outweighs last week's.

    Returns ``{task_type: entry}`` where entry is::

        {
            "window_days": int,
            "sample_size": int,   # rejected first attempts of this type
            "categories": [        # top_n, ordered by decayed weight desc
                {
                    "category": str,
                    "weight": float,      # decayed task-weight sum (2 dp)
                    "task_count": int,    # raw tasks rejected for this cat
                    "share": float,       # task_count / sample_size (2 dp)
                    "guidance": str,
                },
                ...
            ],
        }

    Types with fewer than *min_sample* rejected tasks are omitted entirely.
    """
    current = _coerce_naive_utc(now) if now is not None else _utcnow()
    cutoff = current - timedelta(days=days)
    half_life_days = max(days / 2.0, 0.5)

    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    rows = (await session.execute(stmt)).scalars().all()

    # {task_type: {"sample_size": int, "cats": {cat: {"weight": float, "task_count": int}}}}
    by_type: Dict[str, Dict[str, Any]] = {}
    for t in rows:
        ctx = t.context or {}
        rejection_cats = ctx.get("_first_attempt_rejection_categories")
        if not isinstance(rejection_cats, dict) or not rejection_cats:
            continue
        task_type = (t.first_payload or {}).get("type", "create")
        created = _coerce_naive_utc(t.created_at) if t.created_at else None
        age_days = (
            max((current - created).total_seconds(), 0.0) / 86400.0
            if created
            else 0.0
        )
        weight = _decay_weight(age_days, half_life_days)
        entry = by_type.setdefault(task_type, {"sample_size": 0, "cats": {}})
        entry["sample_size"] += 1
        for cat in rejection_cats:
            cat_entry = entry["cats"].setdefault(
                str(cat), {"weight": 0.0, "task_count": 0}
            )
            cat_entry["weight"] += weight
            cat_entry["task_count"] += 1

    result: Dict[str, Dict[str, Any]] = {}
    for task_type, entry in by_type.items():
        sample_size = entry["sample_size"]
        if sample_size < min_sample:
            continue
        ranked = sorted(
            entry["cats"].items(),
            key=lambda item: (-item[1]["weight"], -item[1]["task_count"], item[0]),
        )[:top_n]
        if not ranked:
            continue
        result[task_type] = {
            "window_days": days,
            "sample_size": sample_size,
            "categories": [
                {
                    "category": cat,
                    "weight": round(data["weight"], 2),
                    "task_count": data["task_count"],
                    "share": round(data["task_count"] / sample_size, 2),
                    "guidance": guidance_for_category(cat),
                }
                for cat, data in ranked
            ],
        }
    return result


def build_rejection_patterns_block(
    aggregates: Dict[str, Dict[str, Any]],
    task_type: str,
) -> Dict[str, Any] | None:
    """Slice the claiming task's own type out of *aggregates* for injection.

    Only the claiming type ships — the block stays a few hundred tokens and
    the agent never sees other types' noise. Returns ``None`` when the type
    has no qualifying patterns.
    """
    entry = aggregates.get(task_type)
    if not entry or not entry.get("categories"):
        return None
    return {"task_type": task_type, **entry}


def inject_rejection_patterns(
    project_context: Dict[str, Any],
    block: Dict[str, Any] | None,
    *,
    budget_bytes: int = _PROJECT_CONTEXT_SOFT_BUDGET_BYTES,
) -> bool:
    """LOW-priority packing of *block* into *project_context* under a budget.

    Mutates *project_context* (adds the ``rejection_patterns`` key) ONLY when
    the already-assembled context plus the block stays under *budget_bytes*.
    Call this LAST in project_context assembly: every mission-critical field
    is then already accounted for, and this advisory block is the first —
    and only — thing dropped under budget pressure.

    Returns True when the block was injected.
    """
    if not block:
        return False
    try:
        current_bytes = len(json.dumps(project_context, default=str).encode())
        block_bytes = len(json.dumps(block, default=str).encode())
    except (TypeError, ValueError):
        logger.warning(
            "rejection_patterns: project_context not JSON-serializable — dropping block"
        )
        return False
    if current_bytes + block_bytes > budget_bytes:
        logger.info(
            "rejection_patterns dropped (low priority): project_context at %d bytes "
            "+ %d-byte block exceeds the %d-byte soft budget",
            current_bytes,
            block_bytes,
            budget_bytes,
        )
        return False
    project_context["rejection_patterns"] = block
    return True
