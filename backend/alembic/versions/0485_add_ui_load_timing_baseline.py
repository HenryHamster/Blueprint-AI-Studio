"""Add ui_load_timing_baseline table for pinned baseline snapshots.

The ui_load_timing_sample table is capped at 200 rows per surface via FIFO
trim on insert, which means pre-improvement samples are eventually evicted.
This table stores an immutable point-in-time baseline (p50/p90/count per
surface+sample_type) that survives the cap, so the UILoadTimingPanel can
compare live "after" stats against a pinned "before" snapshot.

Revision ID: 0485
Revises: 0484
Create Date: 2026-07-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0485"
down_revision = "0484"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_load_timing_baseline",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("surface", sa.String(64), nullable=False),
        sa.Column("sample_type", sa.String(32), nullable=False),
        sa.Column("p50", sa.Integer(), nullable=False),
        sa.Column("p90", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "pinned_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ui_load_timing_baseline_surface",
        "ui_load_timing_baseline",
        ["surface"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ui_load_timing_baseline_surface",
        table_name="ui_load_timing_baseline",
    )
    op.drop_table("ui_load_timing_baseline")
