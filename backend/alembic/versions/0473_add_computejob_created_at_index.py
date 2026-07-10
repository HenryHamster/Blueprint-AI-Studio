"""Add standalone created_at index on ComputeJob.

Several query sites ORDER BY ComputeJob.created_at without filtering on
project_id or status, which forces a full-table sort. A standalone index
allows these queries to use an index-order scan instead.

Revision ID: 0473
Revises: 0472
Create Date: 2026-07-10 00:00:00.000000

"""

from alembic import op

revision = "0473"
down_revision = "0472"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_computejob_created_at", "computejob", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_computejob_created_at", table_name="computejob")
