"""Persist append-only Step 21b acceptance-healing sweep outcomes.

Each row is an operational observation of one cadence-gated reaper invocation.
It retains project/version scope, per-project reconciliation pass counts, and
the acceptance-window evidence or skip branch independently of the mutable
``releaseversion.last_qualification`` projection.  The table is additive and
contains no criterion-evidence foreign key, so it cannot satisfy acceptance
criteria by itself.

This migration matches the SQLModel metadata for ``AcceptanceHealingSweepRun``.
It was reconstructed from that metadata because this task VM has no Alembic
installation and no reachable Docker daemon for the repository test image.

Revision ID: 0744
Revises: 0743
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0744"
down_revision = "0743"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(
        "ix_acceptancehealingsweeprun_executed_at",
        table_name="acceptancehealingsweeprun",
    )
    op.drop_table("acceptancehealingsweeprun")
