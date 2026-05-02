"""Add expression index on payloads[0]->>pr_number for review pipeline lookup.

Audit 2026-04-29 (DB comprehensive remediation, Phase B1 / finding C2):
``review_pipeline._spawn_pipeline_task`` previously loaded ALL terminal
tasks (COMPLETED/FAILED, non-archived) for a project and filtered in
Python by ``payloads[0]["pr_number"]``. At 1k users this was a 50-200 MB
single fetch on every review/fix spawn — the dominant melt risk on the
hot path.

This migration adds a partial expression index on the JSONB extract
``(payloads -> 0 ->> 'pr_number')`` filtered to ``archived = false`` so
the SQL filter pushed down in the same commit can resolve via index
rather than seq-scan-then-extract.

Created CONCURRENTLY on Postgres so production rollouts don't block
writes on the ``task`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first to release Alembic's wrapping
transaction, then issue the DDL on the now-autocommit connection.

The SQLite branch creates a regular partial B-tree index on the
``payloads`` column — it doesn't capture the JSONB-extract expression
(SQLite's JSONB support differs) but lets the test invariant
(``test_model_indexes_covered_by_migrations``) verify the index is
declared. The Postgres path is the source of truth for prod.

Revision ID: 0057
Revises: 0056
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Postgres source-of-truth path: real expression index, created
        # CONCURRENTLY so writers aren't blocked on a long build. CONCURRENTLY
        # cannot run inside a transaction, so we commit Alembic's wrapping
        # transaction first. IF NOT EXISTS makes the migration idempotent
        # across re-runs and across environments where the index may have
        # been hand-created.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_payloads_pr_number "
            "ON task ((payloads -> 0 ->> 'pr_number')) "
            "WHERE archived = false"
        )
    else:
        # SQLite (test path): create a partial B-tree on payloads column so
        # the index name + table is registered against metadata. Static
        # analysis of this migration (test_model_indexes_covered_by_migrations)
        # uses this op.create_index call to verify the metadata Index in
        # models.py has a corresponding migration entry.
        op.create_index(
            "ix_task_payloads_pr_number",
            "task",
            ["payloads"],
            sqlite_where=sa.text("archived = false"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_payloads_pr_number")
    else:
        op.drop_index("ix_task_payloads_pr_number", table_name="task")
