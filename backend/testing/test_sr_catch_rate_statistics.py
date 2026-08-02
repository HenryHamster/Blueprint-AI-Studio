from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_task(*, context: dict | None = None, updated_at=None, payloads=None):
    return SimpleNamespace(
        context=context or {},
        updated_at=updated_at,
        payloads=payloads or [{"type": "create"}],
        first_payload={"type": "create"},
    )


@pytest.mark.parametrize(
    "values,p,expected",
    [
        ([7], 0, 7.0),
        ([1, 3, 5, 7], 25, 2.5),
        ([1, 3, 5, 7], 50, 4.0),
        ([1, 3, 5, 7], 100, 7.0),
    ],
)
def test_percentile_interpolation_edge_cases(values, p, expected):
    from app.sr_catch_rate_statistics import _percentile

    assert _percentile(values, p) == expected


@pytest.mark.parametrize(
    "p1,n1,p2,n2,expected",
    [
        (0.55, 100, 0.50, 100, False),
        (0.70, 100, 0.50, 100, True),
        # Small samples: a two-proportion z-test treats even a delta at the
        # _TREND_DELTA_THRESHOLD (0.05) as noise. At n=10 the z-stat for a
        # 0.05 shift is ~0.72, well under _TREND_Z_MIN (1.28), so the trend
        # is NOT significant. The delta-threshold fallback only applies when
        # the pooled proportion is degenerate (0 or 1), which never holds here.
        (0.00, 10, 0.04, 10, False),
        (0.00, 10, 0.05, 10, False),
    ],
)
def test_trend_significance_threshold_boundaries(p1, n1, p2, n2, expected):
    from app.sr_catch_rate_statistics import _is_trend_significant

    assert _is_trend_significant(p1, n1, p2, n2) is expected


def test_precision_equals_one_minus_fpr_from_cat_fp_stats():
    """precision and false_positive_rate from _compute_cat_fp_stats share one fp/tp basis."""
    from app.sr_analytics_common import _compute_cat_fp_stats

    def _sr_task(sr_failed_cats, human_cats):
        return _make_task(context={
            "_self_review_outcome": {
                "check_results": {
                    cat: {"passed": False} for cat in sr_failed_cats
                },
            },
            "_first_attempt_rejection_categories": {c: 1 for c in human_cats},
        })

    tasks = [
        _sr_task(["correctness"], ["correctness"]),  # TP
        _sr_task(["correctness"], []),               # FP
        _sr_task(["correctness"], ["correctness"]),  # TP
        _sr_task(["correctness"], []),               # FP
        _sr_task(["correctness"], []),               # FP
    ]
    cat_fp, _, _ = _compute_cat_fp_stats(tasks)
    fp = cat_fp["correctness"]["fp"]
    tp = cat_fp["correctness"]["tp"]
    total = fp + tp
    fpr = round(fp / total, 3)
    precision = round(tp / total, 3)
    assert abs(precision - (1.0 - fpr)) < 0.001, (
        f"precision={precision} should equal 1 - fpr={fpr}"
    )


def test_stats_for_tasks_excludes_skipped_self_reviews():
    """A deliberately-skipped SR must not count as a miss in catch-rate denominators."""
    from app.sr_analytics_catch_rate import _stats_for_tasks

    reviewed = _make_task(
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "check_results": {"correctness": {"passed": False}},
            },
            "_first_attempt_rejection_categories": {"correctness": 1},
        }
    )
    # ./done fallback envelope: verdict=unknown + empty check_results == skip.
    skipped = _make_task(
        context={
            "_self_review_outcome": {"verdict": "unknown", "check_results": {}},
            "_first_attempt_rejection_categories": {"correctness": 1},
        }
    )

    count, cat_stats, _, _, _, _ = _stats_for_tasks([reviewed, skipped])

    # Only the reviewed task contributes; the skip is excluded entirely rather
    # than inflating human_count with a phantom miss.
    assert count == 1
    assert cat_stats["correctness"]["human_count"] == 1
    assert cat_stats["correctness"]["hit_count"] == 1


def test_prediction_accuracy_later_iteration_confirmation_not_false_alarm():
    """A category the author SR flagged that the reviewer confirmed only on a
    LATER iteration (present in _all_iterations_rejection_categories but not in
    _first_attempt_rejection_categories) must score as a hit, not a false alarm.

    Under the old first-attempt-only ground-truth window this scored 1.0 false
    alarm; the all-iterations truth set (shared with the blind-spot FP scan)
    makes it a hit, so the SR 'false alarm' figures reconcile by construction.
    """
    from app.sr_review_quality_metrics import compute_sr_prediction_accuracy

    tasks = [
        _make_task(
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": False}},
                },
                # Reviewer did NOT reject 'correctness' on the first iteration...
                "_first_attempt_rejection_categories": {},
                # ...but confirmed it across all iterations.
                "_all_iterations_rejection_categories": {"correctness": 1},
            }
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    assert result["false_alarm_rate"] == 0.0
    assert result["prediction_hit_rate"] == 1.0
    assert result["miss_rate"] == 0.0


def test_overall_hit_rate_uses_raw_counts_not_rounded_categories():
    """Overall hit rate must be derived from raw hit_count/human_count, not from
    back-reconstructing via the already-rounded per-category hit_rate.

    Drifting scenario: two categories with different sample sizes.
    - "correctness": 2 findings, 1 hit → rate = 0.5 (exact, no drift)
    - "error-handling": 1500 findings, 1 hit → rate = round(1/1500, 3) = 0.001,
      but round(0.001 * 1500) = round(1.5) = 2 (banker's rounding to even) ≠ 1.

    Old reconstruction gives 3 total hits / 1502 reviewers → 0.002.
    Raw-count sum gives 2 total hits / 1502 reviewers → 0.001.
    """
    from app.sr_analytics_catch_rate import _stats_for_tasks

    def _task(cat, *, hit: bool):
        return _make_task(
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {cat: {"passed": not hit}},
                },
                "_first_attempt_rejection_categories": {cat: 1},
            }
        )

    tasks = []
    # Category A: 2 findings, 1 hit — no reconstruction drift at this size.
    tasks.append(_task("correctness", hit=True))
    tasks.append(_task("correctness", hit=False))
    # Category B: 1500 findings, 1 hit — reconstruction drift: round(0.001*1500)=2.
    tasks.append(_task("error-handling", hit=True))
    tasks.extend(_task("error-handling", hit=False) for _ in range(1499))

    _, cat_stats, _, _, _, _ = _stats_for_tasks(tasks)

    assert cat_stats["correctness"]["hit_count"] == 1
    assert cat_stats["correctness"]["human_count"] == 2
    assert cat_stats["error-handling"]["hit_count"] == 1
    assert cat_stats["error-handling"]["human_count"] == 1500

    # Verify the drift: back-reconstruction from rounded rate gives wrong hit count.
    b_rate_rounded = round(1 / 1500, 3)  # 0.001
    b_reconstructed = round(b_rate_rounded * 1500)  # round(1.5) = 2 (banker's rounding)
    assert b_reconstructed == 2, "Drift scenario requires reconstruction to yield 2, not 1"

    # Old (broken) aggregation: reconstruct hits from rounded rates.
    a_rate_rounded = round(1 / 2, 3)  # 0.5 — no drift for category A
    a_reconstructed = round(a_rate_rounded * 2)  # 1
    old_total_hits = a_reconstructed + b_reconstructed  # 1 + 2 = 3
    old_total_reviewers = 2 + 1500  # 1502
    old_overall_rate = round(old_total_hits / old_total_reviewers, 3)  # 0.002

    # New (correct) aggregation: direct sum of raw counts.
    new_total_hits = cat_stats["correctness"]["hit_count"] + cat_stats["error-handling"]["hit_count"]  # 2
    new_total_reviewers = cat_stats["correctness"]["human_count"] + cat_stats["error-handling"]["human_count"]  # 1502
    new_overall_rate = round(new_total_hits / new_total_reviewers, 3)  # 0.001

    # The two methods disagree — the raw-count path gives the correct answer.
    assert old_overall_rate != new_overall_rate, (
        f"Drift scenario must produce different rates; got old={old_overall_rate} new={new_overall_rate}"
    )
    assert new_overall_rate == round(2 / 1502, 3)  # true overall rate


def test_sr_catch_rate_statistics_import_seam():
    backend_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    script = """
import importlib

stats = importlib.import_module("app.sr_catch_rate_statistics")
trends = importlib.import_module("app.sr_catch_rate_trends")
analytics = importlib.import_module("app.sr_analytics")

for name in (
    "_TREND_DELTA_THRESHOLD",
    "_TREND_Z_MIN",
    "_compute_reasoning_engagement",
    "_collect_prompt_versions",
    "_percentile",
    "_compute_miss_latency_stats",
    "_is_trend_significant",
):
    assert getattr(stats, name) is getattr(trends, name)
    assert getattr(analytics, name) is getattr(trends, name)
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
