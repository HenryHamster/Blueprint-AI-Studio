"""direction_status_staged_index

Add a partial index on direction(status) WHERE status = 'staged' to support
cross-project reaper sweeps (sweep_activate_staged_directions and
sweep_stale_staged_directions) that filter on status alone.  The existing
composite ix_direction_project_status(project_id, status) cannot be used for
a status-only predicate because project_id is the leading column.

Created CONCURRENTLY on Postgres so production rollouts don't block writes.
CONCURRENTLY cannot run inside a transaction, so we issue COMMIT first to
release Alembic's wrapping transaction. IF NOT EXISTS makes the migration
idempotent. The SQLite path uses a regular op.create_index so the invariant
test (test_model_indexes_covered_by_migrations) can detect the migration.

Revision ID: 0378
Revises: 0377
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0378"
down_revision = "0377"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_direction_status_staged
            ON direction (status)
            WHERE status = 'staged'
            """
        )
    else:
        op.create_index(
            "ix_direction_status_staged",
            "direction",
            ["status"],
            postgresql_where=sa.text("status = 'staged'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_direction_status_staged")
    else:
        op.drop_index("ix_direction_status_staged", table_name="direction")
