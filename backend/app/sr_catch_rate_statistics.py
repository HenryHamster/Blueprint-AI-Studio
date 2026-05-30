"""Pure statistical helpers for SR catch-rate analytics.

Thin re-export facade. The canonical implementations live in
:mod:`app.sr_analytics_common`; ``sr_catch_rate_trends`` and ``sr_analytics``
re-export the *same* objects from there. The import-seam test
(``test_sr_catch_rate_statistics_import_seam``) asserts all three surfaces
expose the identical function objects — a single source of truth, so the
statistics helpers can never silently drift between copies. Adding a second
definition here (instead of re-exporting) is exactly the regression that
test guards against.
"""

from __future__ import annotations

from app.sr_analytics_common import (  # noqa: F401 — re-exported facade surface
    _TREND_DELTA_THRESHOLD,
    _TREND_Z_MIN,
    _collect_prompt_versions,
    _compute_miss_latency_stats,
    _compute_reasoning_engagement,
    _is_trend_significant,
    _percentile,
    _score_reasoning_quality,
)
