"""add_project_archived

Add a durable, reversible ``archived`` boolean to the ``project`` table so a
project can be hidden/set aside without deletion. NOT NULL with a server default
of false so existing rows backfill to not-archived (mirrors ``is_self_repo`` in
0010). Per CLAUDE.md this migration is chained from the latest head (0134) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0135 to match.

Revision ID: 0135
Revises: 0134
Create Date: 2026-06-02 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("project", "archived")
