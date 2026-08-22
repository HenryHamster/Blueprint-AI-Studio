"""Add lifecycle fields to the shared module finding-chain ledger.

Guard-suppression rows share ``modulefindingchain`` with historical review rows;
all new fields are additive and retain safe defaults so those rows remain valid.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0762"
down_revision = "0761"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "modulefindingchain",
        sa.Column("chain_kind", sa.String(length=32), nullable=False, server_default="review"),
    )
    op.add_column(
        "modulefindingchain",
        sa.Column("suppression_lane", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "modulefindingchain", sa.Column("failure_identity", sa.Text(), nullable=True)
    )
    op.add_column(
        "modulefindingchain",
        sa.Column("source_task_ids", JSONB(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "modulefindingchain", sa.Column("remediation_task_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "modulefindingchain", sa.Column("resolved_by_task_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "modulefindingchain", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_modulefindingchain_remediation_task_id", "modulefindingchain", "task",
        ["remediation_task_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_modulefindingchain_resolved_by_task_id", "modulefindingchain", "task",
        ["resolved_by_task_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_modulefindingchain_remediation_task_id", "modulefindingchain",
        ["remediation_task_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_modulefindingchain_remediation_task_id", table_name="modulefindingchain")
    op.drop_constraint("fk_modulefindingchain_resolved_by_task_id", "modulefindingchain", type_="foreignkey")
    op.drop_constraint("fk_modulefindingchain_remediation_task_id", "modulefindingchain", type_="foreignkey")
    op.drop_column("modulefindingchain", "resolved_at")
    op.drop_column("modulefindingchain", "resolved_by_task_id")
    op.drop_column("modulefindingchain", "remediation_task_id")
    op.drop_column("modulefindingchain", "source_task_ids")
    op.drop_column("modulefindingchain", "failure_identity")
    op.drop_column("modulefindingchain", "suppression_lane")
    op.drop_column("modulefindingchain", "chain_kind")
