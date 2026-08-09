"""Add LocalRunner agent_source_sha for fleet-serving provenance.

Self-hosted checkout runners (``scripts/local_runner.py``) are a first-class
fleet member class with no ``LOCAL_MODE`` flag, but the ``agent_image``
release-serving gate only observed warm-pool ``PoolVm`` rows and a task-bound
in-memory cache — a fleet made entirely of durable operator-hardware runners
could never satisfy it.

This migration adds a durable, nullable ``agent_source_sha`` column to
``localrunner``, mirroring ``PoolVm.agent_source_sha`` (String(64)). The value
is the full git commit the runner's agent is serving, reported by the agent
over its heartbeat (via ``AGENT_SOURCE_SHA``) and persisted by
``compute.persist_runner_agent_source_sha``. ``fleet_serving_source_sha``
folds active, live runners with a reported SHA into the same
observed-sources union as warm-pool VMs, so the serving gate confirms the
promoted commit regardless of how a fleet member came to run it (baked image
vs. live git checkout). Distinct from the display-only ``version`` describe
string. Additive-only: nullable column, old runners heartbeating without the
field are unaffected.

Revision ID: 0740
Revises: 0739
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0740"
down_revision = "0739"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "localrunner",
        sa.Column("agent_source_sha", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("localrunner", "agent_source_sha")
