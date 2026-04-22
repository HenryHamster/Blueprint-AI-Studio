"""running_update from JSONB list to TEXT markdown document

Replaces ``project.running_update`` (JSONB array of per-cycle summary
entries) with a single TEXT markdown document the strategic agent
rewrites each cycle. The prior per-cycle list content is discarded —
the strategic cycle that poisoned it (see the executor-hallucination
fix landing alongside this migration) invalidated anything stored so
far, and the next cycle will populate the new shape.

Revision ID: 0044
Revises: 0043
Create Date: 2026-04-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the JSONB array column and re-add as nullable TEXT. The data
    # is not backfilled — see module docstring.
    op.drop_column('project', 'running_update')
    op.add_column(
        'project',
        sa.Column('running_update', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('project', 'running_update')
    op.add_column(
        'project',
        sa.Column(
            'running_update',
            JSONB,
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
