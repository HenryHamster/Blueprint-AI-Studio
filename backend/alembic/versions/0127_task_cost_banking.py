"""Add Task retry cost-banking columns.

Promotes retry-attribution banking from temporary Task.context keys to
dedicated nullable columns on task:
- vm_runtime_cost_banked
- llm_input_tokens_banked
- llm_output_tokens_banked
- llm_call_count_banked
- attempt_history

Autogenerate initially proposed many unrelated schema diffs caused by
longstanding metadata/database drift elsewhere in the repo. This revision
intentionally keeps only the additive Task columns required for this change.

Revision ID: 0127
Revises: 0126
Create Date: 2026-05-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task", sa.Column("vm_runtime_cost_banked", sa.Float(), nullable=True)
    )
    op.add_column(
        "task",
        sa.Column("llm_input_tokens_banked", sa.Integer(), nullable=True),
    )
    op.add_column(
        "task",
        sa.Column("llm_output_tokens_banked", sa.Integer(), nullable=True),
    )
    op.add_column(
        "task", sa.Column("llm_call_count_banked", sa.Integer(), nullable=True)
    )
    op.add_column("task", sa.Column("attempt_history", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("task", "attempt_history")
    op.drop_column("task", "llm_call_count_banked")
    op.drop_column("task", "llm_output_tokens_banked")
    op.drop_column("task", "llm_input_tokens_banked")
    op.drop_column("task", "vm_runtime_cost_banked")
