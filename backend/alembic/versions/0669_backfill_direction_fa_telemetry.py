"""Backfill direction_first_attempt_feedback from design_doc into project_telemetry.

Revision ID: 0669
Revises: 0668
Create Date: 2026-07-22

Data migration: copies the ``direction_first_attempt_feedback`` blob from
``project.design_doc`` into the ``project_telemetry`` table for all projects
that carry the key.  Writers in ``review_learning.py`` now dual-write to both
locations; this migration seeds the telemetry table for projects that were
written before the dual-write landed so readers can migrate away from
design_doc in a future step.

No schema changes — ``project_telemetry`` was created by migration 0659.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0669"
down_revision = "0668"
branch_labels = None
depends_on = None

_KEY = "direction_first_attempt_feedback"


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(tz=timezone.utc)

    rows = conn.execute(
        sa.text(
            "SELECT id, design_doc -> :key AS value"
            " FROM project"
            " WHERE design_doc ? :key"
        ),
        {"key": _KEY},
    ).fetchall()

    if not rows:
        return

    for row in rows:
        project_id = row[0]
        value = row[1]
        if value is None:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO project_telemetry (id, project_id, key, value, updated_at)"
                " VALUES (:id, :project_id, :key, :value::jsonb, :updated_at)"
                " ON CONFLICT ON CONSTRAINT uq_project_telemetry_project_key"
                " DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"
            ),
            {
                "id": str(uuid.uuid4()),
                "project_id": str(project_id),
                "key": _KEY,
                "value": value if isinstance(value, str) else str(value),
                "updated_at": now,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM project_telemetry WHERE key = :key"),
        {"key": _KEY},
    )
