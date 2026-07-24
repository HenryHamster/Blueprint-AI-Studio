"""Add project_telemetry table for relocated design_doc reserved blobs.

Revision ID: 0659
Revises: 0658
Create Date: 2026-07-22

Moves large code-written blobs (efficiency_baseline, lane_baselines,
cochange_index, cochange_applied_prs, module_feedback, sr_catch_rate_trends,
retired_diagnoses_archive, playbook_archive, proposed_changes) out of the
monolithic project.design_doc JSONB into a dedicated table so agent writes
have more headroom and project row contention is reduced.

Writers dual-write to both design_doc and project_telemetry during the
transition; readers continue to use design_doc until explicitly migrated.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0659"
down_revision = "0658"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_telemetry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "key", name="uq_project_telemetry_project_key"
        ),
    )
    op.create_index(
        "ix_project_telemetry_project_id",
        "project_telemetry",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_telemetry_project_id", table_name="project_telemetry")
    op.drop_table("project_telemetry")
