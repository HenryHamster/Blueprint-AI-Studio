"""maxplanaccount_rate_limit_usage

Add rate_limit_usage JSONB column to MaxPlanAccount for persisting
rate-limit/usage headroom snapshots (latest probe data) so the data
survives orchestrator restarts.

Revision ID: 0424
Revises: 0423
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0424"
down_revision = "0423"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.add_column(
        "maxplanaccount",
        sa.Column("rate_limit_usage", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("maxplanaccount", "rate_limit_usage")
