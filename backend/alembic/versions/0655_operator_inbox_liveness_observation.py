"""Add operator_inbox_liveness_observation table for durable leak persistence.

Revision ID: 0655
Revises: 0645
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0655"
down_revision = "0645"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operator_inbox_liveness_observation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'flagged'"),
        ),
        sa.Column(
            "forward_leaked",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "badge_invisible",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "info_alert_noise",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "tick_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_olo_project_id_created_at_desc",
        "operator_inbox_liveness_observation",
        ["project_id", sa.text("created_at desc")],
    )
    op.create_index(
        op.f("ix_operator_inbox_liveness_observation_project_id"),
        "operator_inbox_liveness_observation",
        ["project_id"],
    )


def downgrade():
    op.drop_index(
        op.f("ix_operator_inbox_liveness_observation_project_id"),
        table_name="operator_inbox_liveness_observation",
    )
    op.drop_index(
        "ix_olo_project_id_created_at_desc",
        table_name="operator_inbox_liveness_observation",
    )
    op.drop_table("operator_inbox_liveness_observation")
