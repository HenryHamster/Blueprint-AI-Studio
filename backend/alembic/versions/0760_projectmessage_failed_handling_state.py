"""Add 'failed' to the ProjectMessage.handling_state CHECK constraint.

Revision ID: 0760
Revises: 0759
"""

from __future__ import annotations

from alembic import op


revision = "0760"
down_revision = "0759"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "chk_projectmessage_handling_state"
_TABLE = "projectmessage"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE,
        "handling_state IN ('pending','claimed','responded','superseded','failed')",
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE,
        "handling_state IN ('pending','claimed','responded','superseded')",
    )
