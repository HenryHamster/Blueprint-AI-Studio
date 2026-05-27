"""Add compound index on task(account_id, provider, model, created_at).

Supports the account_model_regression query pattern:
  WHERE account_id IN (...) AND provider IS NOT NULL AND model IS NOT NULL AND created_at >= cutoff

Without this index the planner falls back to ix_task_account_id_created_at or a
seq-scan, then filters provider/model IS NOT NULL in a re-check step. The compound
index lets Postgres satisfy all four predicates in a single index range scan.

Revision ID: 0115
Revises: 0114
Create Date: 2026-05-23 00:00:00.000000

"""

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; commit first.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_account_model_provider_created "
            "ON task (account_id, provider, model, created_at)"
        )
    else:
        # SQLite (test path): plain B-tree composite so
        # test_model_indexes_covered_by_migrations can detect it via the
        # literal index name in op.create_index.
        op.create_index(
            "ix_task_account_model_provider_created",
            "task",
            ["account_id", "provider", "model", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_account_model_provider_created"
        )
    else:
        op.drop_index("ix_task_account_model_provider_created", table_name="task")
