"""Audit the operator/strategic acceptance-contract binding.

Revision ID: 0755
Revises: 0754
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0755"
down_revision = "0754"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "versionacceptancecriterion",
        sa.Column(
            "evidence_contract_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "versionacceptancecriterion",
        sa.Column("evidence_contract_updated_by", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("versionacceptancecriterion", "evidence_contract_updated_by")
    op.drop_column("versionacceptancecriterion", "evidence_contract_updated_at")
