"""missing_fk_indexes

Add missing database indexes on five foreign key columns queried in hot paths:

- StrategicCycle.project_id
- StrategicCycle.phase1_task_id
- StrategicCycle.phase2_task_id
- MigrationReservation.project_id
- TaskWakeRegistration.project_id

(ix_projectmessage_from_task_id was already added by migration 0382.)

Revision ID: 0447
Revises: 0446
"""
from __future__ import annotations

import logging

from alembic import op

revision = "0447"
down_revision = "0446"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    # StrategicCycle FK indexes
    op.create_index(
        "ix_strategiccycle_project_id", "strategiccycle", ["project_id"]
    )
    op.create_index(
        "ix_strategiccycle_phase1_task_id", "strategiccycle", ["phase1_task_id"]
    )
    op.create_index(
        "ix_strategiccycle_phase2_task_id", "strategiccycle", ["phase2_task_id"]
    )
    # MigrationReservation FK index
    op.create_index(
        "ix_migrationreservation_project_id",
        "migrationreservation",
        ["project_id"],
    )
    # TaskWakeRegistration FK index
    op.create_index(
        "ix_taskwakeregistration_project_id",
        "taskwakeregistration",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategiccycle_project_id", table_name="strategiccycle")
    op.drop_index("ix_strategiccycle_phase1_task_id", table_name="strategiccycle")
    op.drop_index("ix_strategiccycle_phase2_task_id", table_name="strategiccycle")
    op.drop_index(
        "ix_migrationreservation_project_id", table_name="migrationreservation"
    )
    op.drop_index(
        "ix_taskwakeregistration_project_id", table_name="taskwakeregistration"
    )
