"""Add the operator pause switch (``project.paused``).

Introduces a first-class persisted pause state on ``project`` that is distinct
from ``autonomous_enabled`` (which only gates NEW autonomous spawning) and from
the terminal lifecycle ``status``. While ``paused`` is True the orchestrator
must not select or provision the project's QUEUED work on any dispatch path,
and the autonomous research/periodic/residual spawn entry points skip it;
already in-flight PROVISIONING/IN_PROGRESS tasks drain normally. Defaults to
False so existing projects are unaffected, and the column is non-nullable with a
``false`` server default so the backend never reads NULL on startup.

Per CLAUDE.md this migration is chained from the latest head (0135,
``project.archived``) and ``EXPECTED_SCHEMA_REVISION`` is bumped to 0136 to
match. On SQLite (local test fixture) the column is created from
``SQLModel.metadata.create_all``; this migration is the Postgres path.

Revision ID: 0136
Revises: 0135
Create Date: 2026-06-02 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("project", "paused")
