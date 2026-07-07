"""ui_load_timing_table

Create ui_load_timing_sample table to persist admin-UI initial-load
timing samples across process restarts.  Replaces the in-memory ring
buffer so before/after deploy comparisons survive deploys.

Revision ID: 0406
Revises: 0397
"""

import sqlalchemy as sa
from alembic import op

revision = "0406"
down_revision = "0397"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_load_timing_sample",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("surface", sa.String(64), nullable=False),
        sa.Column("page_ready_ms", sa.Integer(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ui_load_timing_sample_surface",
        "ui_load_timing_sample",
        ["surface"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ui_load_timing_sample_surface",
        table_name="ui_load_timing_sample",
    )
    op.drop_table("ui_load_timing_sample")
