"""Add ``idempotency_key`` to ``projectmessage`` for idempotent operator sends.

Operator steering/feedback messages had no idempotency protection: the
message-send endpoint inserted a row unconditionally, so a double-click or a
network retry of a request that already succeeded server-side created a
duplicate message — each one independently arming the strategist's refresh
marker. This migration adds a nullable, client-supplied ``idempotency_key``
column plus a partial unique index over ``(project_id, idempotency_key)`` so a
repeat send with the same key collapses to the existing row. Keyless legacy
sends leave the column NULL and are unconstrained (the partial predicate skips
NULLs), preserving backward compatibility.

Per CLAUDE.md this chains from the latest head (0146) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0147 to match.

Revision ID: 0147
Revises: 0146
Create Date: 2026-06-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent guard: skip when the column already exists (mirrors the 0145
    # back-merge pattern) so a re-applied revision can still advance
    # ``alembic_version`` instead of raising.
    cols = {c["name"] for c in sa.inspect(bind).get_columns("projectmessage")}
    if "idempotency_key" not in cols:
        op.add_column(
            "projectmessage",
            sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        )

    existing = {
        ix["name"] for ix in sa.inspect(bind).get_indexes("projectmessage")
    }
    if "ux_projectmessage_project_idempotency_key" not in existing:
        op.create_index(
            "ux_projectmessage_project_idempotency_key",
            "projectmessage",
            ["project_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )


def downgrade() -> None:
    op.drop_index(
        "ux_projectmessage_project_idempotency_key", table_name="projectmessage"
    )
    op.drop_column("projectmessage", "idempotency_key")
