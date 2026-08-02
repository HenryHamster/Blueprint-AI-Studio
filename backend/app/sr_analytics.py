"""Self-review analytics, alerts, and persisted trend payloads.

Thin re-export shim. Logic lives in focused submodules:
  - sr_analytics_catch_rate: SR catch-rate analytics helpers and trend tracking
  - sr_edges_trends: author edges-considered cycle trends
  - sr_signal_trends: regression-risk, false-attestation, and alert drilldown

``app.playbook_learning`` re-exports these names for backward compatibility.
"""

from app.playbook_learning import (  # noqa: F401
    check_fix_loop_divergence_regression,
    compute_sr_quality_by_fingerprint,
    select_sr_miss_examples_for_injection,
)
from app.sr_analytics_catch_rate import (  # noqa: F401
    _compute_edges_stats,
    _stats_for_tasks,
    compute_direction_sr_breakdown_for_endpoint,
    compute_sr_catch_rate_by_direction,
    compute_sr_catch_rate_trends,
)
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
    _PR_REF_RE,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
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
    _task_prompt_fingerprint,
)
from app.sr_edges_trends import compute_edges_trend  # noqa: F401
from app.sr_signal_trends import (  # noqa: F401
    _FALSE_ATTESTATION_ALERT_TITLE_PREFIX,
    _FALSE_ATTESTATION_RATE_THRESHOLD,
    compute_sr_false_attestation_trends,
    compute_sr_regression_risk_test_signal_trends,
    get_sr_alert_drilldown_tasks,
)
