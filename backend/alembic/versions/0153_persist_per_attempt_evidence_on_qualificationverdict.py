"""Add per-attempt evidence columns to ``qualificationverdict``.

The ``QualificationVerdict`` SQLModel gained 12 new nullable columns recording
per-attempt evidence from qualification runs (migration safety verdicts, audit
findings, test details, and promotion context):

- ``migration_safety`` (string, nullable): migration-safety verdict
- ``migration_safety_detail`` (text, nullable): migration-safety detail
- ``migration_apply_safety`` (string, nullable): migration-apply-safety verdict
- ``migration_apply_safety_detail`` (text, nullable): migration-apply-safety detail
- ``audit_findings`` (JSONB, nullable): audit findings list
- ``excluded_audit_findings`` (JSONB, nullable): excluded audit findings list
- ``test_erosion_detail`` (JSONB, nullable): test erosion structured detail
- ``smoketest_detail`` (JSONB, nullable): smoketest structured detail
- ``audit_reconciliation`` (JSONB, nullable): audit reconciliation detail
- ``promotion_pr_url`` (string, nullable): promotion merge PR URL
- ``promotion_conflict`` (boolean, nullable): whether merge hit a conflict
- ``promotion_error_detail`` (text, nullable): promotion error detail string

All are nullable so existing rows remain valid. This mirrors evidence from
``last_qualification`` that was previously dropped when the verdict was written,
allowing blocked/held attempts to retain full context on re-qualification.

Per CLAUDE.md this chains from the latest head (0152) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0153 to match. Purely additive and
reversible.

Revision ID: 0153
Revises: 0152
Create Date: 2026-06-06 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column("migration_safety", sa.String(32), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("migration_safety_detail", sa.Text(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("migration_apply_safety", sa.String(32), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("migration_apply_safety_detail", sa.Text(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("audit_findings", JSONB(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("excluded_audit_findings", JSONB(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("test_erosion_detail", JSONB(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("smoketest_detail", JSONB(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("audit_reconciliation", JSONB(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("promotion_pr_url", sa.String(512), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("promotion_conflict", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column("promotion_error_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "promotion_error_detail")
    op.drop_column("qualificationverdict", "promotion_conflict")
    op.drop_column("qualificationverdict", "promotion_pr_url")
    op.drop_column("qualificationverdict", "audit_reconciliation")
    op.drop_column("qualificationverdict", "smoketest_detail")
    op.drop_column("qualificationverdict", "test_erosion_detail")
    op.drop_column("qualificationverdict", "excluded_audit_findings")
    op.drop_column("qualificationverdict", "audit_findings")
    op.drop_column("qualificationverdict", "migration_apply_safety_detail")
    op.drop_column("qualificationverdict", "migration_apply_safety")
    op.drop_column("qualificationverdict", "migration_safety_detail")
    op.drop_column("qualificationverdict", "migration_safety")
