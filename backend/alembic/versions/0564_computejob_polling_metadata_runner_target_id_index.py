"""Add expression index on computejob(polling_metadata ->> 'runner_target_id').

Revision ID: 0564
Revises: 0563
Create Date: 2026-07-14

The runner_stats endpoint (admin_runners.py) filters ComputeJob rows with
  polling_metadata["runner_target_id"].as_string() == runner_id
which translates to the JSONB ->> operator.  Without a functional index Postgres
must evaluate that expression against every row in the project after the
(project_id, status) B-tree narrows the set — a sequential per-project scan.

Add a B-tree expression index on (polling_metadata ->> 'runner_target_id') to
directly accelerate this equality predicate.  Created CONCURRENTLY on Postgres
so production rollouts don't block writes; CONCURRENTLY cannot run inside a
transaction so we COMMIT first to release Alembic's wrapping transaction.

"""

from alembic import op

revision = "0564"
down_revision = "0563"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_computejob_polling_metadata_runner_target_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON computejob ((polling_metadata ->> 'runner_target_id'))"
        )
    else:
        op.create_index(_INDEX_NAME, "computejob", ["polling_metadata"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    else:
        op.drop_index(_INDEX_NAME, table_name="computejob")
