"""Add approval_gate_config to project for project-specific infra file lists.

The approval gate previously used hardcoded Blueprint-specific file paths. Now
projects can define their own sensitive file lists in approval_gate_config.
When unset, the gate falls back to Blueprint's defaults for self-repo, or uses
an empty list for other projects.

Revision ID: 0118
Revises: 0117
Create Date: 2026-05-23 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "approval_gate_config",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("project", "approval_gate_config")
