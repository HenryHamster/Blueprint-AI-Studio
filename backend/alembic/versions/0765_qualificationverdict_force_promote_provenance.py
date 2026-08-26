"""Persist force-promote actor, timestamp, and justification on verdict history.

The force-promote path originally stored these fields only in the mutable
``ReleaseVersion.last_qualification`` JSONB record. Qualification retries and
auto-retrigger pending stubs overwrite that record, so the operator's release
mechanic action was not recoverable from immutable history. These nullable
columns preserve the provenance on every ``QualificationVerdict`` row while
remaining compatible with existing rows.

Revision ID: 0765
Revises: 0764
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0765"
down_revision = "0764"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column("forced_promote_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("forced_promote_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("forced_promote_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "forced_promote_reason")
    op.drop_column("qualificationverdict", "forced_promote_at")
    op.drop_column("qualificationverdict", "forced_promote_by")
