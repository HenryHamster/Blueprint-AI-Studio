"""poolvm_hot_path_indexes

Add indexes on PoolVm hot-path columns: state (standalone for queries
that filter on state without project_id), leased_task_id (JOIN path in
warm_pool.py), and idle_since (idle-sweeper ordering/filtering).

Revision ID: 0421
Revises: 0420
"""

from alembic import op

revision = "0421"
down_revision = "0420"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_poolvm_state", "poolvm", ["state"])
    op.create_index("ix_poolvm_leased_task_id", "poolvm", ["leased_task_id"])
    op.create_index("ix_poolvm_idle_since", "poolvm", ["idle_since"])


def downgrade() -> None:
    op.drop_index("ix_poolvm_state", table_name="poolvm")
    op.drop_index("ix_poolvm_leased_task_id", table_name="poolvm")
    op.drop_index("ix_poolvm_idle_since", table_name="poolvm")
