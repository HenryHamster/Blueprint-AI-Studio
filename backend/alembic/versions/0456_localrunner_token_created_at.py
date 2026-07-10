"""localrunner_token_created_at

Add ``token_created_at`` column to ``local_runner`` table for tracking
when a runner's token was last issued/rotated. Used by the runner health
monitors to detect and alert on stale tokens (>90 days old).

Revision ID: 0456
Revises: 0451
"""

from alembic import op
import sqlalchemy as sa

revision = "0456"
down_revision = "0452"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "localrunner",
        sa.Column("token_created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("localrunner", "token_created_at")
