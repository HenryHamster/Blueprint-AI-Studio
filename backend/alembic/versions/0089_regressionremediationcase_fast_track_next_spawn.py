"""Add fast_track_next_spawn + last_failure_summary to regressionremediationcase.

Failure-driven re-arm flag: when a remediation task fails (review-rejected,
CI-failed, merge-blocked), the lifecycle hook calls ``case.re_arm()`` which
flips ``fast_track_next_spawn`` to True. The dispatcher then bypasses the 6h
class-level cooldown on the next cycle (provided ``attempts > 0``), spawns a
fresh investigation, and clears the flag back to False — one-shot per failure.

``last_failure_summary`` carries a brief excerpt (≤500 chars) of the prior
remediation's terminal message — review feedback, CI error tail, or
merge-conflict note — used to populate the next investigation's prompt with
a 'PRIOR REMEDIATION FAILURE CONTEXT' block. Null falls back to the prior
task's ``latest_status_message`` at spawn time.

Revision ID: 0089
Revises: 0088
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "fast_track_next_spawn",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "last_failure_summary",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "last_failure_summary")
    op.drop_column("regressionremediationcase", "fast_track_next_spawn")
