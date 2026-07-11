"""Add missing FK and lease-compat indexes for perf findings.

BR-14124 / g-db-perf-missing-indexes

Previously added via PR #14124 as part of a performance audit:
  - ix_systemmaturity_source_task_id (FK join optimisation)
  - ix_residualremediationcase_last_spawned_task_id (FK join optimisation)
  - ix_evergreenfilldecisionlog_item_id (FK join optimisation)
  - ix_poolvm_lease_compat (composite covering the lease hot path)

Revision ID: 0486
Revises: 0485
Create Date: 2026-07-11 03:20:00.000000

"""

from alembic import op

revision = "0486"
down_revision = "0485"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_systemmaturity_source_task_id",
        "systemmaturity",
        ["source_task_id"],
    )
    op.create_index(
        "ix_residualremediationcase_last_spawned_task_id",
        "residualremediationcase",
        ["last_spawned_task_id"],
    )
    op.create_index(
        "ix_evergreenfilldecisionlog_item_id",
        "evergreenfilldecisionlog",
        ["item_id"],
    )
    op.create_index(
        "ix_poolvm_lease_compat",
        "poolvm",
        ["project_id", "state", "account_id", "provider", "auth_mode", "model"],
    )


def downgrade():
    op.drop_index("ix_poolvm_lease_compat", table_name="poolvm")
    op.drop_index("ix_evergreenfilldecisionlog_item_id", table_name="evergreenfilldecisionlog")
    op.drop_index("ix_residualremediationcase_last_spawned_task_id", table_name="residualremediationcase")
    op.drop_index("ix_systemmaturity_source_task_id", table_name="systemmaturity")
