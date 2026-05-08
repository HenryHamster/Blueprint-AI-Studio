"""Add DispatcherCycleRecord table and signal_healthy_since_at column.

Persists dispatcher cycle records (cycles, signals_tripped, spawns_emitted,
rejections_by_reason) and the per-case ``signal_healthy_since_at`` recovery
clock so both survive worker restarts and stay consistent across multiple
workers.  Replaces the prior process-local
``_dispatcher_cycle_records`` deque and the
``_signal_healthy_since`` / ``_signal_first_tripped`` dicts in
``app.pipeline_regression_dispatcher``.

The table is intentionally small (~10K rows worst-case at 60s ticks for 30
days) and is pruned in-process every cycle; the column is nullable so legacy
``regressionremediationcase`` rows continue to function.

Revision ID: 0087
Revises: 0086
Create Date: 2026-05-08 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "signal_healthy_since_at",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )

    op.create_table(
        "dispatchercyclerecord",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("cycle_finished_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column(
            "signals_evaluated",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "signals_tripped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "spawns_emitted",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rejections_by_reason",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("mttd_p50_sec", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dispatchercyclerecord_project_id",
        "dispatchercyclerecord",
        ["project_id"],
    )
    op.create_index(
        "ix_dispatchercyclerecord_project_started",
        "dispatchercyclerecord",
        ["project_id", "cycle_started_at"],
        postgresql_using="btree",
        postgresql_ops={"cycle_started_at": "DESC"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dispatchercyclerecord_project_started",
        table_name="dispatchercyclerecord",
    )
    op.drop_index(
        "ix_dispatchercyclerecord_project_id",
        table_name="dispatchercyclerecord",
    )
    op.drop_table("dispatchercyclerecord")
    op.drop_column("regressionremediationcase", "signal_healthy_since_at")
