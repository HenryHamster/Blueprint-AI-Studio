"""Route strategic memory through KnowledgeDocument rows.

Design 2812f1ed: the ``set_memory`` strategic op now writes each behavioral
fact as an individually-addressable, trigram-searchable ``KnowledgeDocument``
row (``source_kind='strategic_memory'``) instead of appending to the capped
``Project.strategic_memory_notes`` JSONB list.

``source_kind`` is a plain String column (no DB enum type), so adding the new
``strategic_memory`` value needs no DDL. The only schema change is on the
``source_task_id`` foreign key:

Strategic-memory facts must OUTLIVE the strategic task that wrote them — they
are durable behavioral memory, not task artifacts. The FK was ON DELETE CASCADE
(migration 0289), which would delete a memory fact when its producing task is
cleaned up. This migration relaxes it to ON DELETE SET NULL and makes the column
nullable, so task deletion nulls the provenance FK but preserves the document.
``source_citations`` retains the immutable provenance for every row.

This is backward-compatible: every existing row has a non-null source_task_id;
relaxing NOT NULL and CASCADE cannot invalidate them. Read/serialization paths
already treat source_task_id as optional (isouter join +
``str(doc.source_task_id) if doc.source_task_id else None``).

Revision ID: 0680
Revises: 0679
Create Date: 2026-07-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0680"
down_revision = "0679"
branch_labels = None
depends_on = None

_TABLE = "knowledgedocument"
_FK_NAME = "knowledgedocument_source_task_id_fkey"
_COL = "source_task_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (test create_all) already reflects the model definition; no
        # ALTER needed and SQLite cannot ALTER a FK in place anyway.
        return

    # Relax NOT NULL so ON DELETE SET NULL is legal.
    op.alter_column(
        _TABLE,
        _COL,
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    # Swap CASCADE -> SET NULL so strategic-memory facts survive task cleanup.
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "task",
        [_COL],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Restore CASCADE. NOTE: any rows with a NULL source_task_id (produced after
    # a task cleanup nulled the FK) would violate the restored NOT NULL — this
    # downgrade assumes no such rows exist. Delete/repair them first if so.
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "task",
        [_COL],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        _TABLE,
        _COL,
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
