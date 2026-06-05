"""Add ``source_kind`` discriminator to ``knowledgedocument``.

The ``KnowledgeDocument`` SQLModel gained a ``source_kind`` column recording
which read-only task kind produced the document — ``design`` (codebase-internal
design output) vs ``web_research`` (web-grounded findings), with ``unknown`` as
the durable fallback for rows whose provenance predates the discriminator.

The column is non-nullable VARCHAR with a server default of ``'unknown'`` so
every pre-existing row backfills to the unknown-origin value (mirroring the
model's ``server_default=text("'unknown'")``). Per CLAUDE.md this chains from
the latest head (0145) and ``EXPECTED_SCHEMA_REVISION`` is bumped to 0146 to
match. Purely additive and reversible.

Revision ID: 0146
Revises: 0145
Create Date: 2026-06-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledgedocument",
        sa.Column(
            "source_kind",
            sa.String(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledgedocument", "source_kind")
