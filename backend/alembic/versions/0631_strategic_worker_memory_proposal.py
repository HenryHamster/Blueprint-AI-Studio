"""Add StrategicWorkerMemoryProposal intake table.

Durable intake for bounded-worker strategic-memory proposals. A worker inserts a
``pending`` row; Resident reconciliation drains it, admits it only while its
revision-link is current and its evidence trusted, and marks it
``admitted``/``rejected``. Workers never mutate strategic memory directly.

Revision ID: 0631
Revises: 0630
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0631"
down_revision = "0630"
branch_labels = None
depends_on = None


def _json_type(*, postgresql_dialect: bool) -> sa.types.TypeEngine:
    return postgresql.JSONB() if postgresql_dialect else sa.JSON()


def _json_default(value: str, *, postgresql_dialect: bool) -> sa.TextClause:
    suffix = "::jsonb" if postgresql_dialect else ""
    return sa.text(f"'{value}'{suffix}")


def upgrade() -> None:
    postgresql_dialect = op.get_bind().dialect.name == "postgresql"
    json_type = _json_type(postgresql_dialect=postgresql_dialect)
    empty_object = _json_default("{}", postgresql_dialect=postgresql_dialect)
    empty_list = _json_default("[]", postgresql_dialect=postgresql_dialect)

    op.create_table(
        "strategicworkermemoryproposal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("strategicresident.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposal_id", sa.String(120), nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("updates", json_type, nullable=False, server_default=empty_object),
        sa.Column("rationale", sa.String(4000), nullable=False),
        sa.Column("evidence", json_type, nullable=False, server_default=empty_list),
        sa.Column("target_checkpoint_version", sa.BigInteger(), nullable=False),
        sa.Column("target_checkpoint_hash", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("rejection_reason", sa.String(500)),
        sa.Column("source_cycle_id", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id",
            "proposal_id",
            name="uq_strategicworkermemoryproposal_project_proposal",
        ),
        sa.CheckConstraint(
            "layer IN ('charter','working_strategy')",
            name="chk_strategicworkermemoryproposal_layer",
        ),
        sa.CheckConstraint(
            "status IN ('pending','admitted','rejected')",
            name="chk_strategicworkermemoryproposal_status",
        ),
    )
    op.create_index(
        "ix_strategicworkermemoryproposal_project_status",
        "strategicworkermemoryproposal",
        ["project_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategicworkermemoryproposal_project_status",
        table_name="strategicworkermemoryproposal",
    )
    op.drop_table("strategicworkermemoryproposal")
