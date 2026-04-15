"""add_updated_at_to_task

Adds updated_at column to the task table. Auto-populated on insert via
SQLModel's default_factory and on update via SQLAlchemy's onupdate hook
(both call _utcnow for naive UTC — consistent with the rest of the schema).

Consumed by reaper.py's EVALUATING stale parent sweep and rollup_management.py
age calculations to distinguish "last touched" from "first started". Without
this column, those checks were using started_at as a semantic stopgap (see
commit 6e47142) after an earlier AttributeError spam incident.

Existing rows are backfilled with the server's current time so the NOT NULL
constraint is satisfied and stale-sweep checks remain safe (a fresh
updated_at means "we don't know, assume recent" — the reaper will re-check
in 15min if the task is genuinely stuck).

Revision ID: 0033
Revises: 0032
Create Date: 2026-04-15 22:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'task',
        sa.Column(
            'updated_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column('task', 'updated_at')
