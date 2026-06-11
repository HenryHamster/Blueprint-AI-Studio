"""Add completed_at column to task table.

repo_bootstrap tasks reached COMPLETED status without a completed_at timestamp
because the normal agent status path never stamped it. This column lets the
bootstrap completion handler stamp the field and makes completed tasks
queryable by completion time.

Revision ID: 0223
Revises: 0222
"""

from alembic import op
import sqlalchemy as sa

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "completed_at")
