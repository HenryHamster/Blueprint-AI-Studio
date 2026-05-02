"""Add Task.version column for SQLAlchemy optimistic concurrency control.

Audit 2026-04-29 (DB comprehensive remediation, Phase C4 / finding M-C4):
``DeploymentState.version`` already used the optimistic-version idiom but
``Task`` — the workhorse mutable row — relied on heavyweight ``SELECT FOR
UPDATE`` locks held across long post-commit windows to prevent lost
writes. Phase C reduced lock-while-call windows; this migration adds the
last piece — a dedicated ``version`` column wired through SQLAlchemy's
``__mapper_args__["version_id_col"]`` so every UPDATE adds
``WHERE version = :v AND ...`` and bumps the version, with a concurrent
stale write raising ``sqlalchemy.exc.StaleDataError`` instead of silently
clobbering a peer's mutation.

Default 1 (not 0) so freshly-constructed in-memory rows match what the
server default emits on raw INSERTs that bypass the ORM — matters for
the backfill UPDATE below and for any future raw-SQL admin tooling.

Revision ID: 0059
Revises: 0058
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'task',
        sa.Column(
            'version',
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_column('task', 'version')
