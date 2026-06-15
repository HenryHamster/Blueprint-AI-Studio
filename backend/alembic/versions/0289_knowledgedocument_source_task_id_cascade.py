"""Add ON DELETE CASCADE to knowledgedocument.source_task_id FK.

When a project is deleted its tasks are removed via the task_project_id_fkey
CASCADE (added in migration 0274).  KnowledgeDocument rows reference tasks via
source_task_id — a NOT NULL FK with no ondelete action — so deleting a task
that sourced a knowledge document raises:

    asyncpg.ForeignKeyViolationError: update or delete on table "task" violates
    foreign key constraint "knowledgedocument_source_task_id_fkey" ...

This migration drops and recreates the constraint with ON DELETE CASCADE so
that removing a source task automatically removes the derived knowledge
document (correct semantics: the document is provenance-bound to the task).

Revision ID: 0289
Revises: 0288
Create Date: 2026-06-15 00:00:00.000000
"""

from alembic import op


revision = "0289"
down_revision = "0288"
branch_labels = None
depends_on = None

_TABLE = "knowledgedocument"
_FK_NAME = "knowledgedocument_source_task_id_fkey"
_COL = "source_task_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "task",
        [_COL],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "task",
        [_COL],
        ["id"],
    )
