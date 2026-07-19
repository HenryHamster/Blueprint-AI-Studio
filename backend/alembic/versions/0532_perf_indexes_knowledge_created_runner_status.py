"""Add perf indexes: knowledgedocument (project_id, created_at) and localrunner (status).

Revision ID: 0532
Revises: 0531
Create Date: 2026-07-14

Two composite/standalone b-tree indexes for hot read paths surfaced by the
performance audit:

- ``ix_knowledge_document_project_created`` on
  ``knowledgedocument (project_id, created_at)`` — covers the common
  document-list pattern (filter by project_id then ORDER BY created_at) when
  no lifecycle_status predicate is present. The existing
  ``ix_knowledge_document_project_lifecycle_created`` only serves queries that
  also filter on lifecycle_status.
- ``ix_localrunner_status`` on ``localrunner (status)`` — covers the
  cross-project fleet-health scan (``WHERE status != 'deleted'`` with no
  project_id predicate), which the composite
  ``ix_localrunner_project_status`` cannot serve.

Both are created CONCURRENTLY on Postgres so production rollouts don't block
writes. CONCURRENTLY cannot run inside a transaction, so we COMMIT first to
release Alembic's wrapping transaction, then issue the DDL on the now
autocommit connection.
"""

from alembic import op

revision = "0532"
down_revision = "0531"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_knowledge_document_project_created "
            "ON knowledgedocument (project_id, created_at)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_localrunner_status ON localrunner (status)"
        )
    else:
        op.create_index(
            "ix_knowledge_document_project_created",
            "knowledgedocument",
            ["project_id", "created_at"],
        )
        op.create_index("ix_localrunner_status", "localrunner", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_localrunner_status"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_knowledge_document_project_created"
        )
    else:
        op.drop_index("ix_localrunner_status", table_name="localrunner")
        op.drop_index(
            "ix_knowledge_document_project_created", table_name="knowledgedocument"
        )
