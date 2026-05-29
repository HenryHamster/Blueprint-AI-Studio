"""Shared aggregator for project-wide self-review blind-spot themes.

Both ``GET /api/admin/projects/{project_id}/sr-blind-spots`` and the agent
task-fetch project_context injection consume the same aggregation, so the
SQL+normalization logic lives here to avoid drift between them.

NOTE: there is no cached aggregate today (unlike ``sr_catch_rate_trends``
which is precomputed into ``project.design_doc``). Callers scan up to
2000 recent COMPLETED tasks per request — acceptable for the digest
endpoint and the per-task agent fetch in current volumes, but a future
optimization could mirror the ``sr_catch_rate_trends`` cache pattern in
``playbook_learning.compute_sr_catch_rate_trends``.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Task, TaskStatus

_SR_SKIP_BLIND_SPOT_MARKERS = ("self-review skipped", "self-review JSON parsing failed")

# Patterns matching blind_spot entries that agents must NOT emit (per prompt.ts).
# These describe the intended-design pipeline split — agents run a targeted
# ``./check``; CI runs the full backend/docker/viewport/playwright suite — so
# "I did not run the full <X> suite" is never a regression, in any phrasing.
#
# Pattern 1 deliberately makes the trailing test-noun OPTIONAL. The dispatcher
# checks ``is_disallowed_blind_spot`` against the blind-spot *theme*, which
# ``_scan_blind_spot_counts`` truncates to its first 6 tokens — e.g. "did not
# run the full backend test suite" themes to "did not run the full backend".
# The original pattern required a "test suite"/"pytest"/"tests" suffix that the
# truncation drops, so the truncated theme never matched and intended-skip
# themes still tripped the spawn floor; entry variants with no test-noun at all
# ("did not run the full backend suite") slipped the entry-level filter too.
# Once the qualifier (full/entire/whole/complete) + a suite subject appear, the
# entry is an intended skip regardless of what noun — if any — follows.
DISALLOWED_BLIND_SPOT_PATTERNS: List[re.Pattern] = [
    re.compile(
        r"(did\s+not\s+run|didn.t\s+run|skipped|did\s+not\s+execute)\s+"
        r"(the\s+|a\s+)?"
        r"(full|entire|whole|complete)\s+"
        r"(backend|frontend|docker|viewport|playwright|integration|e2e|test)",
        re.IGNORECASE,
    ),
    # Shorter variant without a full/entire/whole/complete qualifier: an
    # explicit suite subject followed (optionally via one intervening word)
    # by a test-noun — "did not run docker pytest", "did not run integration
    # tests", "did not run the docker integration tests".
    re.compile(
        r"(did\s+not\s+run|didn.t\s+run|skipped|did\s+not\s+execute)\s+"
        r"(the\s+|a\s+)?"
        r"(docker|viewport|playwright|integration|e2e)\s+"
        r"(\w+\s+)?"
        r"(pytest|tests?|suite)",
        re.IGNORECASE,
    ),
]

# Human-readable examples from the prompt — used by contract tests to verify
# parity. Includes the truncated 6-token theme form ("did not run the full
# backend") and a no-test-noun variant ("did not run the full backend suite")
# that the original suffix-anchored pattern missed.
DISALLOWED_BLIND_SPOT_EXAMPLES: List[str] = [
    "did not run the full backend test suite",
    "did not run the full backend",
    "did not run the full backend suite",
    "did not run docker pytest",
    "did not run viewport tests",
]


def is_disallowed_blind_spot(entry: str) -> bool:
    """Return True when *entry* matches any disallowed blind_spot pattern."""
    return any(p.search(entry) for p in DISALLOWED_BLIND_SPOT_PATTERNS)


def filter_disallowed_blind_spots(sro: Dict[str, Any]) -> Dict[str, Any]:
    """Drop disallowed blind_spot entries from *sro* in-place and record the count.

    Mutates *sro* directly and returns it.  The count of dropped entries is
    stored under ``_filtered_blind_spots_count`` for diagnostics (so operators
    can see compliance failures without those entries polluting the theme
    aggregator or the dispatcher).  A zero count is still written so callers
    can distinguish "filter ran" from "filter never ran".
    """
    blind_spots = sro.get("blind_spots") or []
    kept = [
        e
        for e in blind_spots
        if not (isinstance(e, str) and is_disallowed_blind_spot(e))
    ]
    dropped = len(blind_spots) - len(kept)
    sro["blind_spots"] = kept
    sro["_filtered_blind_spots_count"] = dropped
    return sro


_TASK_SCAN_CAP = 2000
_BLIND_SPOT_EXAMPLES_PER_THEME = 3


def stamp_sr_blind_spot_computed_at(
    doc: dict,
    now: datetime | None = None,
) -> None:
    """Write the current UTC timestamp into *doc* under the sr_blind_spot
    signal_last_computed_at key consumed by the pipeline staleness watchdog.

    Centralising the write here keeps the design-doc key name canonical and
    lets callers avoid hardcoding it.  Pass *now* in tests to fix the clock.
    """
    ts = (now or datetime.utcnow()).isoformat()
    doc["sr_blind_spot_signal_last_computed_at"] = ts


def stamp_sr_catch_rate_computed_at(
    doc: dict,
    now: datetime | None = None,
    *,
    data_sufficient: bool | None = None,
) -> None:
    """Write the current UTC timestamp into *doc* under the sr_catch_rate_trends
    heartbeat key consumed by the pipeline staleness watchdog.

    Mirrors stamp_sr_blind_spot_computed_at — call on every invocation of
    compute_sr_catch_rate_trends, success or no-result alike, so the watchdog
    can distinguish idle/low-data from genuinely broken.  Pass *now* in tests
    to fix the clock.

    When *data_sufficient* is provided it is written into the trends dict so
    callers can distinguish 'job ran but had no data' (False) from 'trend
    refreshed with real data' (True).  When True, ``last_sufficient_computed_at``
    is also stamped so the staleness watchdog and UI can tell when the displayed
    trend is actually current (a frozen trend continues advancing
    ``signal_last_computed_at`` but ``last_sufficient_computed_at`` stays at the
    last real computation).
    """
    ts = (now or datetime.utcnow()).isoformat()
    trends = dict(doc.get("sr_catch_rate_trends") or {})
    trends["signal_last_computed_at"] = ts
    if data_sufficient is not None:
        trends["data_sufficient"] = data_sufficient
        if data_sufficient:
            trends["last_sufficient_computed_at"] = ts
    doc["sr_catch_rate_trends"] = trends


def is_skipped_sr_outcome(sro: Dict[str, Any] | None) -> bool:
    """A self-review outcome is a 'skip' when the agent emitted the
    bash ./done fallback envelope: verdict=='unknown' with empty
    check_results, or a blind_spots marker matching the fallback paths.
    """
    if not sro:
        return False
    blind_spots = sro.get("blind_spots") or []
    for entry in blind_spots:
        if isinstance(entry, str) and any(
            m in entry for m in _SR_SKIP_BLIND_SPOT_MARKERS
        ):
            return True
    return sro.get("verdict") == "unknown" and not (sro.get("check_results") or {})


async def _scan_blind_spot_counts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    from app.regression_layer_classifier import classify_layers

    theme_data: Dict[str, dict] = {}
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        task_id = str(t.id)
        task_type = (t.first_payload or {}).get("type") or ""
        for entry in sro.get("blind_spots") or []:
            if not isinstance(entry, str) or not entry.strip():
                continue
            # Apply disallow patterns to the *full* entry, not the 6-token
            # theme. The full entry carries more context (e.g. a trailing
            # "... on the VM"), so entry-level matching is strictly more
            # reliable. Pattern 1 now also matches the truncated theme form
            # directly (the trailing test-noun is optional), so the
            # dispatcher's per-theme defense-in-depth check works too — but
            # filtering here keeps disallowed entries out of the theme
            # aggregator entirely.
            if is_disallowed_blind_spot(entry):
                continue
            theme = " ".join(entry.lower().split()[:6])
            if theme not in theme_data:
                theme_data[theme] = {
                    "count": 0,
                    "examples": [],
                    "layer_distribution": defaultdict(int),
                }
            theme_data[theme]["count"] += 1
            if len(theme_data[theme]["examples"]) < _BLIND_SPOT_EXAMPLES_PER_THEME:
                theme_data[theme]["examples"].append(
                    {"text": entry, "task_id": task_id, "task_type": task_type}
                )
            for layer in classify_layers("SR_BLIND_SPOT", entry, None):
                theme_data[theme]["layer_distribution"][layer] += 1

    for d in theme_data.values():
        d["layer_distribution"] = dict(d["layer_distribution"])

    return theme_data


async def compute_sr_blind_spot_themes(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int = 14,
    now: datetime | None = None,
) -> None:
    """Background variant of blind-spot aggregation that stamps the design-doc
    staleness key consumed by the pipeline-regression dispatcher's
    telemetry-staleness watchdog.

    Call on task completion alongside compute_sr_catch_rate_trends so the
    dispatcher can detect when this job stops running and spawn an
    investigation automatically.
    """
    from app.shared_state import with_design_doc_locked

    await with_design_doc_locked(
        session,
        project_id,
        lambda doc: stamp_sr_blind_spot_computed_at(doc, now=now),
    )


async def aggregate_sr_blind_spot_themes(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Return up to ``limit`` recurring blind-spot themes for the project.

    Each blind_spot string is normalized to its first 6 lowercase tokens;
    skipped/fallback SR envelopes are excluded so noise from the bash
    ./done fallback path doesn't inflate counts. Result is ``[{"theme":
    str, "count": int}, ...]`` sorted by count desc.
    """
    theme_data = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_data.items(), key=lambda x: -x[1]["count"])[:limit]
    return [
        {
            "theme": t,
            "count": d["count"],
            "examples": d["examples"],
            "layer_distribution": d["layer_distribution"],
            "triage": triage_sr_blind_spot_theme(t, d["examples"]),
        }
        for t, d in top
    ]


async def _scan_fp_stats(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, int]]:
    """Return per-category {sr_failed, fp_count, hit_count} counts.

    A false positive: SR marks category C as FAILED but no human review
    iteration ever confirmed C (checked via _all_iterations_rejection_categories
    falling back to _first_attempt_rejection_categories).
    A hit: SR marks C as FAILED and a human confirmed C in any iteration.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    cat_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"sr_failed": 0, "fp_count": 0, "hit_count": 0}
    )
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        sr_failed = {c for c, r in check_results.items() if not r.get("passed", True)}
        if not sr_failed:
            continue
        all_human = set(ctx.get("_all_iterations_rejection_categories") or {})
        if not all_human:
            all_human = set(ctx.get("_first_attempt_rejection_categories") or {})
        for cat in sr_failed:
            cat_stats[cat]["sr_failed"] += 1
            if cat in all_human:
                cat_stats[cat]["hit_count"] += 1
            else:
                cat_stats[cat]["fp_count"] += 1
    return cat_stats


async def aggregate_sr_fp_rates(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, Any]]:
    """Per-category false-positive rates for self-review check_results.

    For each category where SR emitted at least one FAILED verdict:
    - ``false_positive_rate``: fraction of SR-fails with no human confirmation
    - ``hit_rate``: fraction of SR-fails confirmed by a human reviewer
    - ``precision``: hit_count / (hit_count + fp_count); None when both are 0

    Skipped/fallback SR envelopes are excluded. Categories with zero
    SR-failed tasks are omitted.
    """
    cat_stats = await _scan_fp_stats(session, project_id, days=days)
    result: Dict[str, Dict[str, Any]] = {}
    for cat, stats in cat_stats.items():
        sr_failed = stats["sr_failed"]
        if sr_failed == 0:
            continue
        fp_count = stats["fp_count"]
        hit_count = stats["hit_count"]
        precision = (
            hit_count / (hit_count + fp_count) if (hit_count + fp_count) > 0 else None
        )
        result[cat] = {
            "sr_failed_count": sr_failed,
            "fp_count": fp_count,
            "hit_count": hit_count,
            "false_positive_rate": round(fp_count / sr_failed, 4),
            "hit_rate": round(hit_count / sr_failed, 4),
            "precision": round(precision, 4) if precision is not None else None,
        }
    return result


async def aggregate_sr_blind_spot_envelope(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> Dict[str, Any]:
    """Same data as :func:`aggregate_sr_blind_spot_themes` but wrapped in
    the response envelope the digest endpoint exposes.

    Kept separate so the agent injection can use the bare list while the
    HTTP endpoint preserves its ``window_days`` / ``total_distinct_themes``
    contract.
    """
    theme_data = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_data.items(), key=lambda x: -x[1]["count"])[:limit]
    return {
        "window_days": days,
        "total_distinct_themes": len(theme_data),
        "themes": [
            {
                "theme": t,
                "count": d["count"],
                "examples": d["examples"],
                "layer_distribution": d["layer_distribution"],
                "triage": triage_sr_blind_spot_theme(t, d["examples"]),
            }
            for t, d in top
        ],
    }


_SR_CODE_CHANGE_TYPES = frozenset({"create", "fix", "refactor", "migrate"})

_EXECUTION_EVIDENCE_CATEGORIES = (
    "tests_executed_cleanly",
    "lint_executed_cleanly",
    "typecheck_executed_cleanly",
)


async def aggregate_sr_execution_evidence_miss_rates(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate miss-rates for execution-evidence SR categories from the DB.

    For each task's ``_self_review_outcome.check_results``, counts presence
    and failures for tests_executed_cleanly, lint_executed_cleanly, and
    typecheck_executed_cleanly. Returns miss_rate, miss_count, sample_count
    per category; omits categories with zero samples. Skipped/fallback SR
    envelopes are excluded.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    totals: Dict[str, Dict[str, int]] = {}
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        for cat in _EXECUTION_EVIDENCE_CATEGORIES:
            entry = check_results.get(cat)
            if entry is None:
                continue
            if cat not in totals:
                totals[cat] = {"sample_count": 0, "miss_count": 0}
            totals[cat]["sample_count"] += 1
            if not entry.get("passed", True):
                totals[cat]["miss_count"] += 1

    out: Dict[str, Dict[str, Any]] = {}
    for cat, s in totals.items():
        if s["sample_count"] == 0:
            continue
        out[cat] = {
            "miss_rate": round(s["miss_count"] / s["sample_count"], 3),
            "miss_count": s["miss_count"],
            "sample_count": s["sample_count"],
        }
    return out


# Mirror playbook_learning._MIN_SR_SAMPLE — update both if the threshold changes.
_SR_MIN_SAMPLE = 5


def has_test_loop_signal(task) -> bool:
    """Return True when the task's test fix-loop ran non-trivially.

    'Non-trivial' means ≥2 test attempts, or a single attempt whose
    exit_reason is not 'passed' (divergence, max-attempts, etc.).
    """
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


def aggregate_sr_regression_risk_test_signal(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
) -> Dict[str, Any] | None:
    """Synchronous aggregation of regression_risk vs test-signal for a task list.

    Classifies each qualifying code-change task (non-skip SR outcome with
    ``check_results.regression_risk.passed`` present) into one of four buckets:

    - ``sr_passed_no_signal``   — true-pass: SR ok, test loop quiet
    - ``sr_passed_with_signal`` — miss-by-test-signal: SR ok, test loop ran
    - ``sr_failed_with_signal`` — hit-by-test-signal: SR flagged, test loop ran
    - ``sr_failed_no_signal``   — false-flag: SR flagged, test loop quiet

    Returns ``None`` when fewer than *min_sample* tasks qualify.
    """
    qualifying = []
    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        rr = (sro.get("check_results") or {}).get("regression_risk")
        if not isinstance(rr, dict) or "passed" not in rr:
            continue
        qualifying.append(t)

    if len(qualifying) < min_sample:
        return None

    stats: Dict[str, int] = {
        "sr_passed_no_signal": 0,
        "sr_passed_with_signal": 0,
        "sr_failed_with_signal": 0,
        "sr_failed_no_signal": 0,
    }
    for t in qualifying:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        rr = (sro.get("check_results") or {}).get("regression_risk") or {}
        sr_passed = bool(rr.get("passed"))
        signal = has_test_loop_signal(t)
        if sr_passed and signal:
            stats["sr_passed_with_signal"] += 1
        elif sr_passed:
            stats["sr_passed_no_signal"] += 1
        elif signal:
            stats["sr_failed_with_signal"] += 1
        else:
            stats["sr_failed_no_signal"] += 1

    numer = stats["sr_passed_with_signal"]
    denom = stats["sr_passed_no_signal"] + stats["sr_passed_with_signal"]
    rate = round(numer / denom, 3) if denom > 0 else None
    return {
        **stats,
        "miss_by_test_signal_rate": rate,
        "numerator": numer,
        "denominator": denom,
        "sample_size": len(qualifying),
    }


def aggregate_sr_scope_drift_vs_expansion(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
) -> Dict[str, Any] | None:
    """Cross-validate SR scope-drift verdict against the objective expansion_ratio signal.

    Qualifying tasks: code-change type, non-skip SR outcome with a
    ``check_results["scope-drift"]`` entry, and a pre-computed
    ``expansion_ratio`` in task.context (set by the agent status router when
    changed_files and related_files are both present on PR submission).

    Four buckets:

    - ``consistent``        — SR passed + ratio ≤ 1.5 (true-pass, not a miss)
    - ``miss_by_expansion`` — SR passed + ratio ≥ 2.0 (SR missed scope drift)
    - ``hit_by_expansion``  — SR failed scope-drift + ratio ≥ 1.5 (SR caught it)
    - ``fp_candidate``      — SR failed scope-drift + ratio ≤ 1.0 (likely false positive)
    - ``gray_zone``         — SR passed + 1.5 < ratio < 2.0 (ambiguous; not counted in rate)

    Returns ``None`` when fewer than *min_sample* tasks qualify.
    """
    qualifying = []
    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        sd = (sro.get("check_results") or {}).get("scope-drift")
        if not isinstance(sd, dict) or "passed" not in sd:
            continue
        if ctx.get("expansion_ratio") is None:
            continue
        qualifying.append(t)

    if len(qualifying) < min_sample:
        return None

    stats: Dict[str, int] = {
        "consistent": 0,
        "miss_by_expansion": 0,
        "hit_by_expansion": 0,
        "fp_candidate": 0,
        "gray_zone": 0,
    }
    for t in qualifying:
        sro = (t.context or {}).get("_self_review_outcome") or {}
        sd = (sro.get("check_results") or {}).get("scope-drift") or {}
        sr_passed = bool(sd.get("passed"))
        ratio = max(float((t.context or {}).get("expansion_ratio") or 1.0), 1.0)
        if sr_passed:
            if ratio <= 1.5:
                stats["consistent"] += 1
            elif ratio >= 2.0:
                stats["miss_by_expansion"] += 1
            else:
                stats["gray_zone"] += 1
        else:
            if ratio >= 1.5:
                stats["hit_by_expansion"] += 1
            elif ratio <= 1.0:
                stats["fp_candidate"] += 1

    numer = stats["miss_by_expansion"]
    denom = stats["consistent"] + stats["miss_by_expansion"]
    rate = round(numer / denom, 3) if denom > 0 else None
    return {
        **stats,
        "miss_by_expansion_rate": rate,
        "numerator": numer,
        "denominator": denom,
        "sample_size": len(qualifying),
    }


# Canonical set of every NON-author origin the agent stamps on a fresh-reviewer
# /recovery check_results entry (see agent/src/handlers/standard.ts). Findings
# with these origins are reviewer SAVES, never author self-review hits, and must
# be excluded from author SR false-positive attribution. The author's own
# entries use origin "sr" (and edges use "author"). test_invariants.py pins this
# set to the exact origins emitted by standard.ts to guard against silent drift.
_REVIEWER_ORIGINS = frozenset(
    (
        "reviewer",
        "reviewer_disagreement",
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    )
)

_SEVERITY_LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

_REVIEWER_EXAMPLE_CAP = 3
_REVIEWER_EXAMPLE_CHAR_CAP = 256


def _max_severity(sev_counts: Dict[str, int]) -> str | None:
    for s in _SEVERITY_LEVELS:
        if sev_counts.get(s, 0) > 0:
            return s
    return None


def _count_reviewer_disagreement_directions(tasks) -> Dict[tuple, dict]:
    counts: Dict[tuple, dict] = defaultdict(
        lambda: {
            "miss": 0,
            # Breakdown by origin (orthogonal to the aggregate miss counter):
            # reading gap — agent admitted incomplete file coverage
            "miss_recovery_incomplete": 0,
            # judgment gap — agent scored category but reviewer disagreed
            "miss_disagreement": 0,
            # coverage gap — agent's SR had no entry for the category
            "miss_coverage_gap": 0,
            "tp": 0,
            "fp": 0,
            "severity": dict.fromkeys(_SEVERITY_LEVELS, 0),
            "examples": [],
        }
    )
    for t in tasks:
        task_type = (t.first_payload or {}).get("type", "")
        if task_type not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        reviewer_findings = sro.get("reviewer_findings") or []
        reviewer_cats = {
            finding.get("category")
            for finding in reviewer_findings
            if isinstance(finding, dict) and finding.get("category")
        }

        # Use structured origin to identify reviewer-rescued categories.
        # Fall back to the absent/passed heuristic for pre-origin outcomes.
        origin_rescued = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict) and result.get("origin") in _REVIEWER_ORIGINS
        }
        if origin_rescued:
            missed_cats = origin_rescued
        else:
            missed_cats = {
                cat
                for cat in reviewer_cats
                if check_results.get(cat) is None
                or (check_results.get(cat) or {}).get("passed", True)
            }

        # Author-flagged failures: exclude reviewer-rescued entries so they
        # don't double-count as TP/FP when origin_rescued is present.
        sr_failed = {
            cat
            for cat, result in check_results.items()
            if isinstance(result, dict)
            and not result.get("passed", True)
            and result.get("origin") not in _REVIEWER_ORIGINS
        }

        for cat in missed_cats:
            counts[(cat, task_type)]["miss"] += 1
            origin = (check_results.get(cat) or {}).get("origin")
            if origin == "reviewer_recovery_after_incomplete_sr":
                counts[(cat, task_type)]["miss_recovery_incomplete"] += 1
            elif origin == "reviewer_disagreement":
                counts[(cat, task_type)]["miss_disagreement"] += 1
            else:
                counts[(cat, task_type)]["miss_coverage_gap"] += 1
            matching_finding = next(
                (
                    f
                    for f in reviewer_findings
                    if isinstance(f, dict) and f.get("category") == cat
                ),
                None,
            )
            sev = (
                matching_finding.get("severity")
                if matching_finding
                and matching_finding.get("severity") in _SEVERITY_LEVELS
                else "LOW"
            )
            counts[(cat, task_type)]["severity"][sev] += 1
            bucket_examples = counts[(cat, task_type)]["examples"]
            if len(bucket_examples) < _REVIEWER_EXAMPLE_CAP and matching_finding:
                desc = (matching_finding.get("description") or "")[
                    :_REVIEWER_EXAMPLE_CHAR_CAP
                ]
                if desc:
                    example: Dict[str, Any] = {
                        "severity": sev,
                        "description": desc,
                        "task_id": str(t.id),
                        "task_type": task_type,
                    }
                    pr_url = ctx.get("_pr_url")
                    if pr_url:
                        example["pr_url"] = pr_url
                    bucket_examples.append(example)
        for cat in sr_failed:
            bucket = counts[(cat, task_type)]
            if cat in reviewer_cats:
                bucket["tp"] += 1
            else:
                bucket["fp"] += 1
    return counts


# ---------------------------------------------------------------------------
# Stale-assertion triage
# ---------------------------------------------------------------------------

# Phrases characteristic of test-assertion language rather than feature-behavior language.
# The theme string is already lowercased (first-6-token normalisation), so all entries
# here must be lowercase.
_STALE_ASSERTION_PHRASES = (
    "expects ordering",
    "expects to contain",
    "index of",
    "tobe(",  # .toBe( lowercased
    "toequal(",  # .toEqual( lowercased
    "assertion",
    "expected to",
    "now expects",  # "test_foo.py now expects …" pattern
)

# Test-file path fragments: *.test.ts, *.spec.tsx, test_*.py, etc.
_TEST_FILE_RE = re.compile(
    r"[\w./\-]+\.(?:test|spec)\.(?:ts|tsx|js|jsx)"
    r"|test_\w+\.py",
    re.IGNORECASE,
)


def triage_sr_blind_spot_theme(theme: str, examples: list) -> dict:
    """Classify a recurring blind-spot theme as likely stale assertion or prompt regression.

    Heuristics (multiple signals raise confidence):
    - Theme text itself references a test-file path (*.test.ts, test_*.py, …)
    - Theme or example texts contain test-assertion language keywords
    - All examples consistently trace back to the same single test file

    Returns::

        {
            "verdict": "likely_stale_assertion" | "ambiguous" | "likely_prompt_regression",
            "signals": [...],      # signal names that fired
            "triage_context": "…", # human-readable note for spawned investigation
        }

    A verdict of ``"likely_stale_assertion"`` means ≥2 signals fired; ``"ambiguous"``
    means exactly 1 signal; ``"likely_prompt_regression"`` means no signals.
    """
    signals: List[str] = []
    theme_lower = theme.lower()
    example_texts = [
        ex.get("text", "")
        for ex in (examples or [])
        if isinstance(ex, dict) and ex.get("text")
    ]
    combined = " ".join([theme_lower] + [t.lower() for t in example_texts])

    # Signal 1: theme itself names a test file path
    if _TEST_FILE_RE.search(theme):
        signals.append("theme_references_test_file")

    # Signal 2: assertion-language keywords present anywhere in theme or examples
    for phrase in _STALE_ASSERTION_PHRASES:
        if phrase in combined:
            signals.append(f"assertion_phrase:{phrase}")
            break  # one phrase is sufficient to fire this signal

    # Signal 3: all examples come from a single test file (consistent source cluster)
    test_file_refs: set = set()
    for text in example_texts:
        for m in _TEST_FILE_RE.finditer(text):
            test_file_refs.add(m.group(0).lower())
    if test_file_refs and len(test_file_refs) == 1:
        signals.append("single_test_file_cluster")

    if len(signals) >= 2:
        verdict = "likely_stale_assertion"
    elif signals:
        verdict = "ambiguous"
    else:
        verdict = "likely_prompt_regression"

    triage_context = (
        f"Stale-assertion triage ({verdict}): {', '.join(signals)}. "
        "Verify whether the test assertion at the referenced file is stale "
        "relative to current behavior before treating this as a prompt regression."
        if signals
        else ""
    )

    return {"verdict": verdict, "signals": signals, "triage_context": triage_context}


def aggregate_sr_false_attestation_rates(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
) -> Dict[str, Any] | None:
    """Cross-check SR execution-evidence pass claims against actual CI outcomes.

    For each code-change task whose SR marks an execution-evidence category
    ``passed=True``, checks the task's CI result via
    ``context["review_loop"]["ci_passed"]``.  A *false attestation* is counted
    when SR claimed passed but CI recorded ``ci_passed=False``.  Tasks without
    a CI outcome (no review_loop or ci_passed not set) are tallied separately.

    Returns ``None`` when fewer than ``min_sample`` tasks have both an SR claim
    and a CI outcome across all three categories.

    Per-category shape::

        {
            "sr_claimed_passed": int,
            "ci_confirmed": int,
            "false_attested": int,
            "ci_data_missing": int,
            "false_attestation_rate": float | None,
        }
    """
    cats: Dict[str, Dict[str, int]] = {
        cat: {
            "sr_claimed_passed": 0,
            "false_attested": 0,
            "ci_confirmed": 0,
            "ci_data_missing": 0,
        }
        for cat in _EXECUTION_EVIDENCE_CATEGORIES
    }

    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = sro.get("check_results") or {}
        review_loop = ctx.get("review_loop") or {}
        ci_passed_val = review_loop.get("ci_passed")  # True, False, or None

        for cat in _EXECUTION_EVIDENCE_CATEGORIES:
            entry = check_results.get(cat)
            if not isinstance(entry, dict) or not entry.get("passed", False):
                continue
            cats[cat]["sr_claimed_passed"] += 1
            if ci_passed_val is None:
                cats[cat]["ci_data_missing"] += 1
            elif ci_passed_val:
                cats[cat]["ci_confirmed"] += 1
            else:
                cats[cat]["false_attested"] += 1

    total_with_ci = sum(s["ci_confirmed"] + s["false_attested"] for s in cats.values())
    if total_with_ci < min_sample:
        return None

    result: Dict[str, Any] = {}
    for cat, s in cats.items():
        if s["sr_claimed_passed"] == 0:
            continue
        denom = s["ci_confirmed"] + s["false_attested"]
        rate = round(s["false_attested"] / denom, 3) if denom > 0 else None
        result[cat] = {**s, "false_attestation_rate": rate}
    return result or None


def aggregate_sr_co_failure_pairs(
    tasks,
    *,
    min_sample: int = _SR_MIN_SAMPLE,
    min_co_count: int = 2,
    top_n: int = 5,
) -> List[Dict[str, Any]] | None:
    """Pairwise co-failure rates for SR check_result categories.

    For each ordered pair (cat_a, cat_b), computes:
        lift = P(cat_b miss | cat_a miss) / P(cat_b miss)

    High lift signals that cat_a and cat_b share a reasoning gap addressable
    by a single prompt-level fix.  Qualifying tasks: code-change type,
    non-skip SR outcome.  Returns ``None`` when fewer than ``min_sample``
    qualifying tasks exist.

    Each entry:
        {"cat_a": str, "cat_b": str, "co_fail_count": int,
         "p_b_given_a": float, "base_rate_b": float, "lift": float}
    """
    total = 0
    cat_miss_counts: Dict[str, int] = defaultdict(int)
    pair_counts: Dict[tuple, int] = defaultdict(int)

    for t in tasks:
        if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        total += 1
        check_results = sro.get("check_results") or {}
        failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict) and not r.get("passed", True)
        }
        for cat in failed:
            cat_miss_counts[cat] += 1
        for cat_a in failed:
            for cat_b in failed:
                if cat_a != cat_b:
                    pair_counts[(cat_a, cat_b)] += 1

    if total < min_sample:
        return None

    pairs = []
    for (cat_a, cat_b), co_count in pair_counts.items():
        if co_count < min_co_count:
            continue
        miss_a = cat_miss_counts[cat_a]
        miss_b = cat_miss_counts[cat_b]
        if miss_a == 0 or miss_b == 0:
            continue
        p_b_given_a = co_count / miss_a
        base_rate_b = miss_b / total
        lift = p_b_given_a / base_rate_b
        pairs.append(
            {
                "cat_a": cat_a,
                "cat_b": cat_b,
                "co_fail_count": co_count,
                "p_b_given_a": round(p_b_given_a, 4),
                "base_rate_b": round(base_rate_b, 4),
                "lift": round(lift, 4),
            }
        )

    pairs.sort(key=lambda x: -x["lift"])
    return pairs[:top_n]


def classify_theme_example_layers(examples: list) -> list:
    """Return layer tags derived from verbatim blind-spot example texts.

    Runs ``classify_layers`` on each example's ``text`` field individually with
    no stage constraint (stage=None), so content patterns can surface
    CI_WORKFLOW, AGENT_VM, AGENT_VERB, and BASELINE_GATE tags beyond the
    REVIEWER_PASS that the signal-level ``stage='review'`` always contributes.

    The dispatcher includes example texts in the combined ``theme_extras``
    message (giving classify_layers full context), but calling this function
    separately lets callers record per-example layer observations in
    ``symptoms_by_layer`` independently from the signal-level observation.
    """
    from app.regression_layer_classifier import classify_layers

    layers: set = set()
    for ex in examples or []:
        text = (ex.get("text") or "").strip()
        if text:
            layers.update(classify_layers("SR_BLIND_SPOT", text, None))
    return sorted(layers)


_PERSISTENT_BLIND_SPOT_THRESHOLD = 0.25
_PERSISTENT_BLIND_SPOT_MIN_SAMPLE = 3


def compute_persistent_sr_blind_spot_hint(
    tasks,
    *,
    threshold: float = _PERSISTENT_BLIND_SPOT_THRESHOLD,
    min_sample: int = _PERSISTENT_BLIND_SPOT_MIN_SAMPLE,
    window_count: int = 2,
    window_days: int = 7,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Return a persistent_blind_spots hint for injection into the SR prompt.

    Splits *tasks* into *window_count* consecutive *window_days*-day buckets
    (most-recent-first).  A category is 'persistent' when its reviewer-disagreement
    rate (reviewer_misses / qualifying_tasks_in_window) exceeds *threshold* in
    every bucket that has at least *min_sample* qualifying tasks.

    Returns::

        {
            "categories": [{"category": str, "task_type": str, "rate": float}, ...],
            "hint": str | None,
        }

    Empty ``categories`` and ``hint=None`` when no persistent categories exist.
    Pass *now* in tests to pin the clock.
    """
    _now = now or datetime.utcnow()

    def _qualifying(window_tasks):
        out = []
        for t in window_tasks:
            if (t.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
                continue
            sro = (t.context or {}).get("_self_review_outcome")
            if not sro or is_skipped_sr_outcome(sro):
                continue
            out.append(t)
        return out

    window_results: List[Dict[tuple, float]] = []
    for i in range(window_count):
        cutoff_end = _now - timedelta(days=i * window_days)
        cutoff_start = _now - timedelta(days=(i + 1) * window_days)
        bucket = [
            t
            for t in tasks
            if t.created_at is not None and cutoff_start <= t.created_at < cutoff_end
        ]
        qual = _qualifying(bucket)
        if len(qual) < min_sample:
            window_results.append({})
            continue
        counts = _count_reviewer_disagreement_directions(qual)
        total = len(qual)
        rates: Dict[tuple, float] = {}
        for key, data in counts.items():
            miss = data["miss"]
            if miss > 0:
                rates[key] = round(miss / total, 4)
        window_results.append(rates)

    all_keys: set = set()
    for wr in window_results:
        all_keys.update(wr.keys())

    persistent: List[Dict[str, Any]] = []
    for key in all_keys:
        per_window = [wr.get(key) for wr in window_results]
        if all(r is not None and r >= threshold for r in per_window):
            cat, ttype = key
            avg_rate = sum(per_window) / len(per_window)  # type: ignore[arg-type]
            persistent.append(
                {"category": cat, "task_type": ttype, "rate": round(avg_rate, 4)}
            )

    if not persistent:
        return {"categories": [], "hint": None}

    persistent.sort(key=lambda x: -x["rate"])
    cat_phrases = ", ".join(
        f"{p['category']} ({p['rate'] * 100:.0f}%)" for p in persistent
    )
    hint = (
        f"Persistent self-review blind spots (reviewer-disagreement rate over"
        f" {window_count} x {window_days}-day windows): {cat_phrases}."
        " Spend extra attention on these categories — reviewers have consistently"
        " flagged issues the self-review pass missed."
    )
    return {"categories": persistent, "hint": hint}


def count_reviewer_disagreements_in_tasks(tasks) -> Dict[tuple, int]:
    """Single source of truth for the (category, task_type) -> count tally.

    The caller is responsible for filtering tasks by status and by created_at
    cutoff (each call site has slightly different fetch semantics — DB vs.
    pre-fetched playbook list — but the disagreement-counting rule itself
    must not drift between them).

    A *disagreement* is counted for every reviewer_finding category that
    self-review either omitted or marked ``passed: true``. Skipped/fallback
    self-review envelopes are excluded so the bash ./done fallback does not
    inflate counts.
    """
    counts = _count_reviewer_disagreement_directions(tasks)
    return {key: bucket["miss"] for key, bucket in counts.items() if bucket["miss"] > 0}


async def aggregate_sr_reviewer_disagreements(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Aggregate categories where self-review passed but the fresh-reviewer flagged an issue.

    For each completed code-change task that has both ``check_results`` and
    ``reviewer_findings``, a *disagreement* is recorded for every category
    where the fresh-reviewer raised a finding but self-review either omitted
    the category or marked it ``passed: true``.

    Returns up to ``limit`` entries sorted by count descending:
      [{"category": str, "task_type": str, "count": int}, ...]
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    direction_counts = _count_reviewer_disagreement_directions(all_tasks)
    top = [
        item
        for item in sorted(direction_counts.items(), key=lambda x: -x[1]["miss"])
        if item[1]["miss"] > 0
    ][:limit]
    return [
        {
            "category": cat,
            "task_type": ttype,
            "count": bucket["miss"],
            "severity_breakdown": dict(bucket["severity"]),
            "max_severity": _max_severity(bucket["severity"]),
            "false_positive_count": bucket["fp"],
            "true_positive_count": bucket["tp"],
            "false_positive_rate": round(
                bucket["fp"] / (bucket["fp"] + bucket["tp"]), 3
            )
            if (bucket["fp"] + bucket["tp"]) > 0
            else None,
            "examples": bucket["examples"],
        }
        for (cat, ttype), bucket in top
    ]
