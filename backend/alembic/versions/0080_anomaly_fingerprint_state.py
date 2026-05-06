"""Add anomalyfingerprintstate table for durable anomaly fingerprint persistence.

Provides a dedicated, queryable table for anomaly fingerprint state that
survives pod restarts — complement to the in-memory persistent_anomalies
dict in shared_state.py and the Project.design_doc snapshot approach.

Revision ID: 0080
Revises: 0079
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anomalyfingerprintstate",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint_key", sa.String(length=512), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(length=64), nullable=True),
        sa.Column("latest_details", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "fingerprint_key", name="ux_anomalyfingerprintstate_project_key"),
    )
    op.create_index("ix_anomalyfingerprintstate_project_id", "anomalyfingerprintstate", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_anomalyfingerprintstate_project_id", table_name="anomalyfingerprintstate")
    op.drop_table("anomalyfingerprintstate")
