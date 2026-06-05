"""Link knowledge documents to their strategic direction.

A ``web_research`` task is spawned with the strategic direction that motivated
it (carried on ``task.context["direction_id"]``), but that linkage was dropped
when the resulting ``KnowledgeDocument`` was persisted. This migration adds a
nullable ``direction_id`` string column (the direction KEY, e.g. ``d-1a2b3c4d``
— not a FK into a direction table, matching how the rest of the system carries
direction linkage) plus a b-tree index for per-direction browse / counts.

Per CLAUDE.md it chains from the latest head (0147) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0148 to match. The column is purely
additive: it does not change when or whether a document is stored.

Revision ID: 0148
Revises: 0147
Create Date: 2026-06-04 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Idempotent guard: skip when the column already exists (mirrors the 0145
    # has_table guard) so a re-applied / back-merged revision can still advance
    # ``alembic_version`` instead of raising on a duplicate column.
    cols = {c["name"] for c in sa.inspect(bind).get_columns("knowledgedocument")}
    if "direction_id" in cols:
        return

    op.add_column(
        "knowledgedocument",
        sa.Column("direction_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_document_direction", "knowledgedocument", ["direction_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_direction", table_name="knowledgedocument"
    )
    op.drop_column("knowledgedocument", "direction_id")
