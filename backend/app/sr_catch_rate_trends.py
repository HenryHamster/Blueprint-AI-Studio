"""Compatibility shim for SR catch-rate analytics.

The canonical implementation now lives in ``app.sr_analytics_catch_rate`` and
``app.sr_analytics_common``. This module preserves the historical import
surface needed by sibling SR trend modules.
"""

from app.sr_analytics_common import (  # noqa: F401
    _EDGE_CATS,
    _EXAMPLE_MAX_CATS,
    _EXAMPLE_MAX_DESC_LEN,
    _EXAMPLE_MAX_PER_CAT,
    _EXECUTION_EVIDENCE_CATEGORIES,
    _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    _MIN_DIRECTION_SR_SAMPLE,
    _MIN_SR_FINDINGS,
    _MIN_SR_SAMPLE,
    _PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
    _PR_REF_RE,
    _SENSITIVE_TOKENS,
    _SR_ALERT_TITLE_MAX_PARTS,
    _SR_CODE_CHANGE_TYPES,
    _SR_ENGAGEMENT_MIN_PASSES,
    _SR_ENGAGEMENT_THRESHOLD,
    _SR_FLOOR_ALERT_TITLE_PREFIX,
    _SR_FLOOR_THRESHOLD,
    _SR_QUADRANT_ALERT_TITLE_PREFIX,
    _SR_REGRESSION_ALERT_TITLE_PREFIX,
    _SR_SEVERITY_DELTA_THRESHOLDS,
    _TREND_DELTA_THRESHOLD,
    _TREND_Z_MIN,
    _URL_RE,
    _categorize_review_issues,
    _categorize_review_issues_keyword,
    _clean_example_desc,
    _collect_prompt_versions,
    _compute_cat_fp_stats,
    _compute_cat_severity_calibration,
    _compute_miss_latency_stats,
    _compute_reasoning_engagement,
    _finalize_examples,
    _format_sr_alert_title,
    _is_sensitive_desc,
    _is_trend_significant,
    _percentile,
    _sr_skip_stats,
)
from app.sr_analytics_catch_rate import (  # noqa: F401
    _compute_edges_stats,
    _stats_for_tasks,
    compute_direction_sr_breakdown_for_endpoint,
    compute_sr_catch_rate_by_direction,
    compute_sr_catch_rate_trends,
)
