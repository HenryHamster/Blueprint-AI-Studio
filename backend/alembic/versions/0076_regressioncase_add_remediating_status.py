"""Add REMEDIATING to RegressionRemediationCase status check constraint.

The list endpoint default filter references REMEDIATING as a distinct state
(case has a live remediation task, not yet merged). The previous constraint
only allowed ACTIVE/MERGED/CLOSED/NEEDS_MANUAL_INTERVENTION.

Revision ID: 0076
Revises: 0075
Create Date: 2026-05-05 00:00:00.000000

"""
from alembic import op


revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        type_="check",
    )
    op.create_check_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        "status IN ('ACTIVE', 'REMEDIATING', 'MERGED', 'CLOSED', 'NEEDS_MANUAL_INTERVENTION')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        type_="check",
    )
    op.create_check_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        "status IN ('ACTIVE', 'MERGED', 'CLOSED', 'NEEDS_MANUAL_INTERVENTION')",
    )
