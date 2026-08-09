"""Remove the unused Step 21b acceptance-healing sweep ledger.

The ledger was observational only: no gate, branch, threshold, admission
check, or scoring input read it.  The healing sweep remains active, but its
database telemetry and retention sweep are removed.

Revision ID: 0751
Revises: 0750
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0751"
down_revision = "0750"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_acceptancehealingsweeprun_executed_at",
        table_name="acceptancehealingsweeprun",
    )
    op.drop_table("acceptancehealingsweeprun")


def downgrade() -> None:
    op.create_table(
        "acceptancehealingsweeprun",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "projects_examined",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "open_versions_examined",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "project_pass_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "project_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_rows_recorded",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_acceptancehealingsweeprun_executed_at",
        "acceptancehealingsweeprun",
        ["executed_at"],
        unique=False,
    )
