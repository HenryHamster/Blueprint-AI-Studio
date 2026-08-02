"""Read helpers for the dedicated per-project SR analytics store."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectSRAnalytics


LEGACY_SR_ANALYTICS_KEYS = frozenset(
    {
        "sr_catch_rate_trends",
        "edges_trend",
        # Rolling-cutover/rollback vocabulary retained to match migration 0696.
        # Current runtime consumers ignore these retired Fresh Reviewer fields.
        "fresh_reviewer_trend",
        "sr_reviewer_disagreements",
        "sr_regression_risk_test_signal_trends",
        "sr_false_attestation_trends",
        "sr_blind_spot_signal_last_computed_at",
        "viewport_coverage_snapshot",
        "sr_catch_rate_trends_alert",
        "sr_catch_rate_floor_alert",
        "sr_quadrant_alert",
        "sr_pass_engagement_alert",
        "sr_floor_threshold",
        "sr_floor_min_windows",
        "sr_engagement_threshold",
        "sr_engagement_min_passes",
        "fix_loop_divergence_signal_last_computed_at",
        "fix_loop_divergence_alerted_window",
        "fix_loop_divergence_regressions",
        "fix_loop_divergence_detected_at",
    }
)


def legacy_sr_analytics_payload(design_doc: Any) -> dict[str, Any]:
    """Extract only documented legacy SR keys for rolling-cutover seeding."""
    if not isinstance(design_doc, dict):
        return {}
    return {
        key: design_doc[key]
        for key in LEGACY_SR_ANALYTICS_KEYS
        if key in design_doc and design_doc[key] is not None
    }


async def read_project_sr_analytics(
    session: AsyncSession,
    project: Project,
) -> dict[str, Any]:
    """Read SR analytics with a per-key legacy ``design_doc`` fallback.

    ``project_sr_analytics`` is authoritative after the dedicated-store cutover.
    The merge remains deliberately per key so an already-created partial row does
    not hide legacy values that have not been backfilled yet. Dedicated values
    always win.

    The legacy fallback is retained for rollback safety and for projects not yet
    reached by the forward backfill.
    """
    merged = legacy_sr_analytics_payload(project.design_doc)
    row = await session.get(ProjectSRAnalytics, project.id)
    if isinstance(row, ProjectSRAnalytics):
        merged.update(row.data or {})
    return merged
