"""Add the idempotent discovery admission ledger.

Revision ID: 0712
Revises: 0711
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0712"
down_revision = "0711"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discoveryadmission",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admission_key", sa.String(length=512), nullable=False),
        sa.Column("admission_kind", sa.String(length=24), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "spawn_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "units",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "state",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column(
            "admission_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "admission_kind IN ('logical_task','attempt')",
            name="chk_discoveryadmission_kind",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','consumed','released')",
            name="chk_discoveryadmission_state",
        ),
        sa.CheckConstraint(
            "units > 0",
            name="chk_discoveryadmission_units",
        ),
        sa.CheckConstraint(
            "length(admission_key) > 0",
            name="chk_discoveryadmission_key_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["discoverycycle.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["spawn_request_id"],
            ["strategicspawnrequest.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_id",
            "admission_key",
            name="uq_discoveryadmission_cycle_key",
        ),
    )
    op.create_index(
        "ix_discoveryadmission_cycle_state_created",
        "discoveryadmission",
        ["cycle_id", "state", "created_at"],
    )
    op.create_index(
        "ix_discoveryadmission_task_id",
        "discoveryadmission",
        ["task_id"],
    )
    op.create_index(
        "ix_discoveryadmission_spawn_request_id",
        "discoveryadmission",
        ["spawn_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discoveryadmission_spawn_request_id",
        table_name="discoveryadmission",
    )
    op.drop_index(
        "ix_discoveryadmission_task_id",
        table_name="discoveryadmission",
    )
    op.drop_index(
        "ix_discoveryadmission_cycle_state_created",
        table_name="discoveryadmission",
    )
    op.drop_table("discoveryadmission")
