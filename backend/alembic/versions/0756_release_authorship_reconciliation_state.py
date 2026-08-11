"""Persist the fleet-wide release authorship reconciliation cursor.

Revision ID: 0756
Revises: 0755
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0756"
down_revision = "0755"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releaseauthorshipreconciliationstate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cursor_partition", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("null_cursor_id", sa.Uuid(), nullable=True),
        sa.Column("served_at_cursor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("served_id_cursor", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO releaseauthorshipreconciliationstate "
            "(id, cursor_partition, generation) VALUES (1, 0, 0)"
        )
    )
    # Existing non-null contracts were written before audit metadata existed.
    # Their own immutable creation fields are the only safe provenance source;
    # the explicit marker is not a fabricated user identity.
    op.execute(
        sa.text(
            "UPDATE versionacceptancecriterion "
            "SET evidence_contract_updated_at = created_at, "
            "evidence_contract_updated_by = COALESCE(created_by, 'legacy:created_at') "
            "WHERE evidence_contract IS NOT NULL "
            "AND evidence_contract_updated_at IS NULL "
            "AND evidence_contract_updated_by IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_table("releaseauthorshipreconciliationstate")
