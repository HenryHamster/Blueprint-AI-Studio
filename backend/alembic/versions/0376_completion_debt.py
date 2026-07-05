"""completion_debt_ledger

Adds the durable completion-debt ledger that makes merged-but-unfinished work
first-class:

  * ``completiondebtentry`` table — one row per outstanding obligation (a knob
    merged default-off/observe, a capability in observe/trial mode without a
    decided enforce verdict, a no-CI merge gate that fell open to an UNVERIFIED
    merge, a strategic retirement residual/deferral, or a seeded backlog item).

Idempotency is enforced structurally by a UNIQUE constraint on ``dedupe_key``
so repeated recorder/seed invocations upsert the same logical obligation rather
than duplicating rows. ``project_id`` is nullable (deployment-global debt such
as dormant runtime knobs has no owning project) with an ON DELETE CASCADE FK so
project deletion cleans up its debt.

Revision ID: 0376
Revises: 0375
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0376"
down_revision = "0375"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "completiondebtentry",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="open",
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_completiondebt_dedupe_key"),
    )
    op.create_index(
        "ix_completiondebt_status",
        "completiondebtentry",
        ["status"],
    )
    op.create_index(
        "ix_completiondebt_kind_status",
        "completiondebtentry",
        ["kind", "status"],
    )
    op.create_index(
        "ix_completiondebt_project",
        "completiondebtentry",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_completiondebt_project", table_name="completiondebtentry")
    op.drop_index("ix_completiondebt_kind_status", table_name="completiondebtentry")
    op.drop_index("ix_completiondebt_status", table_name="completiondebtentry")
    op.drop_table("completiondebtentry")
