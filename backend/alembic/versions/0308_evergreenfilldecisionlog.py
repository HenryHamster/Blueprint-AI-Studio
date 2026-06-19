"""Durable EvergreenFillDecisionLog ledger for windowed observability.

Adds the ``evergreenfilldecisionlog`` table — one row per (project, tick)
fill-decision — so observe-mode behavior can be reviewed across deploys.
Each row carries the full FleetSignal context at decision time, enabling
post-hoc false-alarm rate analysis (direction d-15685fb3).

project_id and item_id FK to project/evergreenbacklogitem with ON DELETE
SET NULL so ledger rows outlive their referenced rows for durable audit.

Revision ID: 0308
Revises: 0305
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


revision = "0308"
down_revision = "0305"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evergreenfilldecisionlog",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("util_now", sa.Float(), nullable=True),
        sa.Column("util_threshold", sa.Float(), nullable=False),
        sa.Column("dispatchable_queue_depth", sa.Integer(), nullable=False),
        sa.Column("vm_slots_available", sa.Integer(), nullable=True),
        sa.Column("active_evergreen_probes", sa.Integer(), nullable=False),
        sa.Column("daily_spawn_count", sa.Integer(), nullable=False),
        sa.Column("has_candidate", sa.Boolean(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("finding_key", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["evergreenbacklogitem.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_evergreenfilldecisionlog_ts"),
        "evergreenfilldecisionlog",
        ["ts"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evergreenfilldecisionlog_project_id"),
        "evergreenfilldecisionlog",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_evergreenfilldecisionlog_project_ts",
        "evergreenfilldecisionlog",
        ["project_id", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evergreenfilldecisionlog_project_ts",
        table_name="evergreenfilldecisionlog",
    )
    op.drop_index(
        op.f("ix_evergreenfilldecisionlog_project_id"),
        table_name="evergreenfilldecisionlog",
    )
    op.drop_index(
        op.f("ix_evergreenfilldecisionlog_ts"),
        table_name="evergreenfilldecisionlog",
    )
    op.drop_table("evergreenfilldecisionlog")
