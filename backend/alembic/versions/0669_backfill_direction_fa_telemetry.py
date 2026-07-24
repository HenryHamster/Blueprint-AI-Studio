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

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0669"
down_revision = "0668"
branch_labels = None
depends_on = None

_KEY = "direction_first_attempt_feedback"


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(
        sa.text(
            "SELECT id, design_doc -> :key AS value"
            " FROM project"
            " WHERE design_doc ? :key"
            " ORDER BY id"
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
                " VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), :key,"
                " CAST(:value AS jsonb), CURRENT_TIMESTAMP)"
                " ON CONFLICT ON CONSTRAINT uq_project_telemetry_project_key"
                " DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "key": _KEY,
                "value": json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        )


def downgrade() -> None:
    # Rows for this key may have been created or updated by the live dual writer,
    # and the schema does not record migration provenance. Deleting by key would
    # therefore erase authoritative telemetry that 0669 did not create.
    pass
