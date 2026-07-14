"""Structural bridge: chains 0469 → 0470 (no-op after encryption lands).

This no-op migration bridges from the current base head (0469) so the chain
is linear.

Revision ID: 0470
Revises: 0469
Create Date: 2026-07-10 00:00:00.000000

"""

revision = "0470"
down_revision = "0469"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No DDL — this migration exists solely to restore chain linearity.
    pass


def downgrade() -> None:
    # This migration is intentionally irreversible — it is a structural
    # bridge marker with no DDL of its own.
    raise NotImplementedError(
        "No DDL to undo: 0470 is a structural bridge marker"
    )
