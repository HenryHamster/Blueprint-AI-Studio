"""challenger_refuted_audit_findings

Add ``challenger_refuted_audit_findings`` (JSONB, nullable) to
``qualificationverdict``.  Stores per-finding records where an independent
audit challenger marked the finding ``refuted_false_positive``, together with
the challenger's auditable reason.  Distinct from ``excluded_audit_findings``
(which captures the unverified-primary substantiation gap).

This column is the durable evidence trail for the fail-closed audit-challenge
gate: only challenger-confirmed findings now deduct from the audit sub-score;
refuted findings are excluded and recorded here.

Purely additive: nullable, no server default, no backfill needed.  Existing
verdict rows carry NULL (pre-challenge-gate behaviour, fully compatible).

Per CLAUDE.md: chained from the latest head (0199, ``task_park_wake``) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0205 to match.

Revision ID: 0205
Revises: 0199
Create Date: 2026-06-09 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0205"
down_revision = "0199"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column("challenger_refuted_audit_findings", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "challenger_refuted_audit_findings")
