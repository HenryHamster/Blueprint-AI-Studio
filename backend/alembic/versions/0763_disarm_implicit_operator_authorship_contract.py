"""Remove the implicit zero-budget authorship gate.

Revision ID: 0763
Revises: 0762

Migration 0759 briefly armed every open release version with a system-created
zero-budget authorship criterion.  Those rows measured immutable history and
could therefore permanently block promotion.  Remove only untouched defaults;
an operator-edited or otherwise explicit contract remains policy.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0763"
down_revision = "0762"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM versionacceptancecriterion
            WHERE criterion_key = :criterion_key
              AND status = 'active'
              AND created_by = 'system'
              AND evidence_contract_updated_by = 'system:release_default'
              AND evidence_contract = jsonb_build_object(
                  'type', 'operator_authored_commits',
                  'predicate', jsonb_build_object('max_count', 0)
              )
            """
        ).bindparams(criterion_key="c-operator-authorship")
    )


def downgrade() -> None:
    """Do not recreate a promotion-latching policy on downgrade."""
