"""ui_load_timing_sample_type

Add sample_type column to ui_load_timing_sample to distinguish between
first-commit and data-ready samples.

Revision ID: 0409
Revises: 0408
"""

import sqlalchemy as sa
from alembic import op

revision = "0409"
down_revision = "0408"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ui_load_timing_sample",
        sa.Column("sample_type", sa.String(32), nullable=False, server_default="first_commit"),
    )


def downgrade() -> None:
    op.drop_column("ui_load_timing_sample", "sample_type")
