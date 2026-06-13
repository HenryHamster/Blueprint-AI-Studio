"""Evergreen backlog ledger (utilization-triggered auto-fill).

Adds the durable ``evergreenbacklogitem`` ledger that backs the Phase-0
utilization-triggered evergreen backlog auto-fill mechanism (direction
d-2877ddc7). Each row is one curated candidate, project-scoped and keyed by a
stable ``finding_key`` so the same latent gap is never enrolled twice. The
orchestrator's throttled ``_check_evergreen_fill()`` selects an enrolled
candidate and spawns ONE low-priority research probe when the fleet is idle or
all active directions are parked/proof-waiting; the probe's outcome transitions
the row through the closed-vocabulary lifecycle (enrolled → spawned → advanced /
verified_stale / retired) via the existing research_completion gates.

Revision ID: 0269
Revises: 0266
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# Reserved revision id for this task (linearized against production at merge
# time). The reservation note named down_revision 0235, but 0236-0266 merged
# after the reservation was issued, so chaining from 0235 would BRANCH the
# revision chain. We therefore chain from the actual current head 0266 to keep
# the chain a single linear sequence — mirroring the same deviation already
# documented in 0260 (reserved against 0235, chained from 0238) and 0266
# (reserved against 0235, chained from 0260).
revision = "0269"
down_revision = "0266"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evergreenbacklogitem",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("finding_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="enrolled",
        ),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column(
            "provenance",
            sa.String(length=64),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("direction_id", sa.String(length=128), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("spawned_task_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("verified_against_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        # SET NULL: the ledger row outlives the task row for durable provenance.
        sa.ForeignKeyConstraint(
            ["spawned_task_id"],
            ["task.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "finding_key",
            name="uq_evergreenbacklogitem_project_finding",
        ),
    )
    op.create_index(
        op.f("ix_evergreenbacklogitem_project_id"),
        "evergreenbacklogitem",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evergreenbacklogitem_finding_key"),
        "evergreenbacklogitem",
        ["finding_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evergreenbacklogitem_direction_id"),
        "evergreenbacklogitem",
        ["direction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evergreenbacklogitem_spawned_task_id"),
        "evergreenbacklogitem",
        ["spawned_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_evergreenbacklogitem_project_status",
        "evergreenbacklogitem",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evergreenbacklogitem_project_status",
        table_name="evergreenbacklogitem",
    )
    op.drop_index(
        op.f("ix_evergreenbacklogitem_spawned_task_id"),
        table_name="evergreenbacklogitem",
    )
    op.drop_index(
        op.f("ix_evergreenbacklogitem_direction_id"),
        table_name="evergreenbacklogitem",
    )
    op.drop_index(
        op.f("ix_evergreenbacklogitem_finding_key"),
        table_name="evergreenbacklogitem",
    )
    op.drop_index(
        op.f("ix_evergreenbacklogitem_project_id"),
        table_name="evergreenbacklogitem",
    )
    op.drop_table("evergreenbacklogitem")
