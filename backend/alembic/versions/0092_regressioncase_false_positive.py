"""Add FALSE_POSITIVE regression-case status + suppression clock.

Lets the investigate agent mark a tripped pipeline-regression signal as a
false positive (intended behavior, expected variance, known-incomplete
pipeline step, etc.) so the dispatcher silently suppresses respawning the
same regression class for a TTL window. After the TTL lapses the next
dispatcher tick re-arms the case to ACTIVE — a genuine recurrence of the
same signal is still caught.

Adds:
- ``regressionremediationcase.false_positive_until_at`` (nullable timestamp)
- ``regressionremediationcase.false_positive_reason`` (nullable string)
- Replaces ``ck_regressionremediationcase_status`` to allow FALSE_POSITIVE.

Revision ID: 0092
Revises: 0091
Create Date: 2026-05-08 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


_OLD_STATUS_VALUES = "'ACTIVE', 'REMEDIATING', 'MERGED', 'CLOSED', 'NEEDS_MANUAL_INTERVENTION'"
_NEW_STATUS_VALUES = (
    "'ACTIVE', 'REMEDIATING', 'MERGED', 'CLOSED', "
    "'NEEDS_MANUAL_INTERVENTION', 'FALSE_POSITIVE'"
)


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "false_positive_until_at",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "false_positive_reason",
            sa.String(),
            nullable=True,
        ),
    )

    op.drop_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        type_="check",
    )
    op.create_check_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        f"status IN ({_NEW_STATUS_VALUES})",
    )


def downgrade() -> None:
    # Demote any FALSE_POSITIVE rows back to ACTIVE so the narrower
    # check-constraint can be re-applied without violations.
    op.execute(
        "UPDATE regressionremediationcase SET status = 'ACTIVE' "
        "WHERE status = 'FALSE_POSITIVE'"
    )
    op.drop_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        type_="check",
    )
    op.create_check_constraint(
        "ck_regressionremediationcase_status",
        "regressionremediationcase",
        f"status IN ({_OLD_STATUS_VALUES})",
    )

    op.drop_column("regressionremediationcase", "false_positive_reason")
    op.drop_column("regressionremediationcase", "false_positive_until_at")
