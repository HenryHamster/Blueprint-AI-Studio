from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_task(*, context: dict | None = None, updated_at=None):
    return SimpleNamespace(context=context or {}, updated_at=updated_at)


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


@pytest.mark.parametrize(
    "reasoning,expected",
    [
        ("", 0.0),
        ("checked backend/app/foo.py and verified the empty path", 0.67),
        (
            "verified backend/app/foo.py and test_foo.py::test_empty works today",
            1.0,
        ),
    ],
)
def test_reasoning_quality_scoring(reasoning, expected):
    from app.sr_catch_rate_statistics import _score_reasoning_quality

    assert _score_reasoning_quality(reasoning) == pytest.approx(expected)


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
    "_score_reasoning_quality",
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
