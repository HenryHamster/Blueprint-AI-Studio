"""poolvm_state_project_index

Add reversed composite index on PoolVm(state, project_id) for
per-project-by-state queries that filter on state then project_id.
The standalone ix_poolvm_state was already added by 0421_poolvm_hot_path_indexes.

Revision ID: 0422
Revises: 0421
"""
from __future__ import annotations

import logging

from alembic import op

revision = "0422"
down_revision = "0421"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    # Reversed composite for per-project-by-state queries.
    op.create_index("ix_poolvm_state_project", "poolvm", ["state", "project_id"])


def downgrade() -> None:
    op.drop_index("ix_poolvm_state_project", table_name="poolvm")
