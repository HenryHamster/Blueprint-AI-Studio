"""Backfill legacy design-doc SR state into project_sr_analytics.

Revision ID: 0696
Revises: 0695
Create Date: 2026-07-22

The dedicated row is authoritative. Existing dedicated keys therefore win when
legacy design-doc state is merged into a row that already exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0696"
down_revision = "0695"
branch_labels = None
depends_on = None


LEGACY_SR_ANALYTICS_KEYS = frozenset(
    {
        "sr_catch_rate_trends",
        "fresh_reviewer_trend",
        "edges_trend",
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


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _legacy_sr_payload(design_doc: Any) -> dict[str, Any]:
    doc = _as_mapping(design_doc)
    return {
        key: doc[key]
        for key in LEGACY_SR_ANALYTICS_KEYS
        if key in doc and doc[key] is not None
    }


def upgrade() -> None:
    conn = op.get_bind()
    projects = conn.execute(
        sa.text("SELECT id, design_doc FROM project ORDER BY id")
    ).mappings().all()
    existing_rows = conn.execute(
        sa.text("SELECT project_id, data FROM project_sr_analytics ORDER BY project_id")
    ).mappings().all()
    existing_by_project = {
        str(row["project_id"]): _as_mapping(row["data"])
        for row in existing_rows
    }

    if conn.dialect.name == "postgresql":
        insert_stmt = sa.text(
            "INSERT INTO project_sr_analytics (project_id, data, updated_at) "
            "VALUES (CAST(:project_id AS uuid), CAST(:data AS jsonb), CURRENT_TIMESTAMP)"
        )
        update_stmt = sa.text(
            "UPDATE project_sr_analytics SET data = CAST(:data AS jsonb), "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE project_id = CAST(:project_id AS uuid)"
        )
    else:
        insert_stmt = sa.text(
            "INSERT INTO project_sr_analytics (project_id, data, updated_at) "
            "VALUES (:project_id, :data, CURRENT_TIMESTAMP)"
        )
        update_stmt = sa.text(
            "UPDATE project_sr_analytics SET data = :data, "
            "updated_at = CURRENT_TIMESTAMP WHERE project_id = :project_id"
        )

    for project in projects:
        project_id = str(project["id"])
        legacy = _legacy_sr_payload(project["design_doc"])
        if not legacy:
            continue
        existing = existing_by_project.get(project_id)
        merged = {**legacy, **(existing or {})}
        params = {
            "project_id": project_id,
            "data": json.dumps(merged, separators=(",", ":"), default=str),
        }
        conn.execute(update_stmt if existing is not None else insert_stmt, params)


def downgrade() -> None:
    # Forward-only data copy. The legacy design-doc values remain intact, and
    # deleting a live dedicated row during rollback could destroy newer writes.
    pass
