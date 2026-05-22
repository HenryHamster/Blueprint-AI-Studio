"""Add compliance_followup_count to regressionremediationcase.

Running counter of compliance follow-up tasks spawned for a case.
The dispatcher compares this against a configurable max cap so a
cross-layer mandate that cannot be satisfied (e.g. requires an
unmodifiable CI_WORKFLOW layer) does not re-spawn a compliance-followup
indefinitely. Nullable/server_default=0 so existing rows are safely
backfilled without a Python-side touch.

Revision ID: 0102
Revises: 0101
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "compliance_followup_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "compliance_followup_count")
