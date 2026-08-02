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
from app.time_utils import _utcnow

_SR_SKIP_BLIND_SPOT_MARKERS = ("self-review skipped", "self-review JSON parsing failed")
_REVIEWER_CONFIRMED_CATEGORY_EXCLUSIONS = frozenset({"other"})

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


def safe_check_results(sro: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``sro['check_results']`` only when it is a dict, else ``{}``.

    Agents occasionally emit ``check_results`` as a JSON list instead of the
    expected category->record dict. The outer-only ``Dict[str, Any]`` schema
    validation does not catch this, and ``sro.get("check_results") or {}`` lets
    a non-empty list through (truthy) — downstream ``.items()/.keys()/.get()``
    calls then raise AttributeError. Route dict-assuming reads through this
    helper to degrade gracefully to an empty mapping.
    """
    cr = sro.get("check_results")
    return cr if isinstance(cr, dict) else {}


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
    # Some agents emit ``check_results`` as a JSON list instead of the expected
    # category->record dict. The outer-only ``Dict[str, Any]`` schema validation
    # lets it through, and a non-empty list is truthy so the ubiquitous
    # ``sro.get("check_results") or {}`` guard does not catch it — downstream
    # ``.items()/.keys()/.get()`` calls then raise AttributeError. Folding a list
    # back into records is unsafe (the shapes are not unambiguously
    # ``{category, passed}``), so we drop to an empty dict: correct graceful
    # degradation. A valid dict is left untouched.
    if "check_results" in sro and not isinstance(sro.get("check_results"), dict):
        sro["check_results"] = {}
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

    Audit C10: stamp with the project-standard _utcnow() (naive UTC) so the
    heartbeat string round-trips through the staleness watchdog's naive parse
    identically to other timestamps, and the bare datetime.utcnow() DTZ
    deprecation clears.
    """
    ts = (now or _utcnow()).isoformat()
    doc["sr_blind_spot_signal_last_computed_at"] = ts


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
    cutoff = _utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id, Task.context, Task.payloads, Task.created_at, raiseload=True
            )
        )
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    from app.failure_layer_classifier import classify_layers

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


def reviewer_confirmed_categories(ctx: Dict[str, Any]) -> set:
    """Canonical reviewer-LLM-confirmed category truth set for one task.

    The reviewer-confirmed truth set is persisted in task context, not
    reconstructed from review history: a category is "confirmed" when some
    reviewer iteration rejected the work for it. Prefers the all-iterations
    superset
    (``_all_iterations_rejection_categories``, stamped on every rejection
    iteration) and falls back to the first-attempt set
    (``_first_attempt_rejection_categories``) only when the all-iterations
    field is absent.

    Shared by every SR false-positive surface — the blind-spot FP scan
    (:func:`_scan_fp_stats`) and the prediction-accuracy
    false-alarm/hit/miss computation — so a category the author flagged that
    the reviewer confirmed only on a *later* iteration is never miscounted as a
    false alarm, and the surfaces agree by construction.

    ``other`` is deliberately excluded even if persisted in the rejection
    categories. The author self-review vocabulary cannot emit ``other``, so
    treating it as canonical reviewer-confirmed truth would create impossible
    misses/false alarms and depress catch-rate denominators across every shared
    analytics surface.
    """
    confirmed = set(ctx.get("_all_iterations_rejection_categories") or {})
    if not confirmed:
        confirmed = set(ctx.get("_first_attempt_rejection_categories") or {})
    return confirmed - _REVIEWER_CONFIRMED_CATEGORY_EXCLUSIONS


def has_reviewer_confirmed_categories(ctx: Dict[str, Any]) -> bool:
    """Sample-membership predicate for prediction-accuracy scoring.

    Returns True when :func:`reviewer_confirmed_categories` would return a
    non-empty set. This is the single source of the "is this task part of the
    self-review prediction-accuracy sample?" contract: a task belongs to the
    sample iff its reviewer-confirmed truth set (the same set the scorer reads)
    is non-empty.

    Mirrors the prefer-all-iterations / fall-back-to-first-attempt logic of
    :func:`reviewer_confirmed_categories` so the sample filter and the scorer's
    ground truth cannot drift. In particular a task whose reviewer-confirmed
    categories appear *only* on a later iteration
    (``_all_iterations_rejection_categories`` set, no
    ``_first_attempt_rejection_categories``) is in-sample — it must not be
    dropped just because the first review approved.
    """
    return bool(reviewer_confirmed_categories(ctx))


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


_EXECUTION_EVIDENCE_CATEGORIES = (
    "tests_executed_cleanly",
    "lint_executed_cleanly",
    "typecheck_executed_cleanly",
)

# Retired: DB-backed execution-evidence miss rates over tests/lint/typecheck
# check_results had no real consumer beyond compatibility shims.


# Mirror playbook_learning._MIN_SR_SAMPLE — update both if the threshold changes.
_SR_MIN_SAMPLE = 5


# Legacy-only vocabulary for historical outcomes written by the retired
# auxiliary reviewer. These values are no longer produced, but excluding them
# prevents old records from being misattributed as author self-review catches.
_REVIEWER_ORIGINS = frozenset(
    (
        "reviewer",
        "reviewer_disagreement",
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    )
)

# Lazy-loaded canonical SR code-change type filter to avoid circular import.
# sr_analytics_common imports from this module, so we defer the import until first use.
_SR_CODE_CHANGE_TYPES = None


def _get_sr_code_change_types():
    """Load canonical SR code-change types on first use, avoiding circular import."""
    global _SR_CODE_CHANGE_TYPES
    if _SR_CODE_CHANGE_TYPES is None:
        from app.sr_analytics_common import _SR_CODE_CHANGE_TYPES as imported
        _SR_CODE_CHANGE_TYPES = frozenset(imported)
    return _SR_CODE_CHANGE_TYPES


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
        if (t.first_payload or {}).get("type") not in _get_sr_code_change_types():
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        check_results = safe_check_results(sro)
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
        if (t.first_payload or {}).get("type") not in _get_sr_code_change_types():
            continue
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        total += 1
        check_results = safe_check_results(sro)
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
