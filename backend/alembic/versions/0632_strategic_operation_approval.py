"""Add StrategicOperationApproval table.

Durable operator approvals for explicit-approval Resident operations (pr.merge,
deployment.*). An operator grants an approval scoped to a project + capability +
an approval_key derived from the exact operation parameters; a fenced operation
consumes exactly one matching unexpired approval.

Revision ID: 0632
Revises: 0631
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0632"
down_revision = "0631"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategicoperationapproval",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("strategicresident.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(120), nullable=False),
        sa.Column("approval_key", sa.String(400), nullable=False),
        sa.Column("granted_by", sa.String(255), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_operation_id", sa.String(128)),
    )
    op.create_index(
        "ix_strategicoperationapproval_lookup",
        "strategicoperationapproval",
        ["project_id", "capability", "approval_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategicoperationapproval_lookup",
        table_name="strategicoperationapproval",
    )
    op.drop_table("strategicoperationapproval")
