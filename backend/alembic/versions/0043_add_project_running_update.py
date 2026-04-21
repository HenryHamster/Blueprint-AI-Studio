"""add_project_running_update

Adds ``running_update`` (JSONB list) and ``running_update_acknowledged_at``
(nullable timestamp) columns to ``project``. The strategic agent appends
one entry per cycle to running_update, and the user clears it by
acknowledging — giving the user a persistent "what's changed since I
last checked in" journal that replaces per-cycle message posts.

Revision ID: 0043
Revises: 0042
Create Date: 2026-04-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'project',
        sa.Column(
            'running_update',
            JSONB,
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        'project',
        sa.Column(
            'running_update_acknowledged_at',
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('project', 'running_update_acknowledged_at')
    op.drop_column('project', 'running_update')
