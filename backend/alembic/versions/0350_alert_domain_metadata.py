"""Add alert domain metadata.

Revision ID: 0350
Revises: 0349
"""

from alembic import op
import sqlalchemy as sa


revision = "0350"
down_revision = "0349"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("domain", sa.String(), nullable=False, server_default="core"),
    )
    op.add_column("alert", sa.Column("event_type", sa.String(), nullable=True))
    op.add_column("alert", sa.Column("source", sa.String(), nullable=True))
    op.add_column("alert", sa.Column("dedupe_key", sa.String(), nullable=True))
    op.add_column(
        "alert",
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("alert", sa.Column("last_seen_at", sa.DateTime(), nullable=True))
    op.create_index("ix_alert_project_domain", "alert", ["project_id", "domain"])
    op.create_index(
        "ix_alert_project_event_type", "alert", ["project_id", "event_type"]
    )
    op.create_index(
        "ux_alert_active_dedupe_key",
        "alert",
        ["project_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND dismissed_at IS NULL"),
        sqlite_where=sa.text("dedupe_key IS NOT NULL AND dismissed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_alert_active_dedupe_key", table_name="alert")
    op.drop_index("ix_alert_project_event_type", table_name="alert")
    op.drop_index("ix_alert_project_domain", table_name="alert")
    op.drop_column("alert", "last_seen_at")
    op.drop_column("alert", "occurrence_count")
    op.drop_column("alert", "dedupe_key")
    op.drop_column("alert", "source")
    op.drop_column("alert", "event_type")
    op.drop_column("alert", "domain")
