"""Add index on OrchestratorErrorAck.acknowledged_at.

Revision ID: 0310
Revises: 0309
"""

from alembic import op


revision = "0310"
down_revision = "0309"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_orchestratorerrorack_acknowledged_at",
        "orchestratorerrorack",
        ["acknowledged_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_orchestratorerrorack_acknowledged_at", table_name="orchestratorerrorack"
    )
