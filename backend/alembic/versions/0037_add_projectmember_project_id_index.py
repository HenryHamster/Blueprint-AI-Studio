"""add_projectmember_project_id_index

Adds a single-column index on ProjectMember.project_id to back the
authorization-path lookups used in nearly every project-scoped query
(admin.py, routers/projects.py, routers/admin_projects.py). The existing
composite unique index ix_projectmember_project_user can serve leftmost
project_id lookups, but models.py declares `index=True` on this column
and the migration-index invariant requires a matching single-column index.

Revision ID: 0037
Revises: 0036
Create Date: 2026-04-20 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_projectmember_project_id',
        'projectmember',
        ['project_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_projectmember_project_id', table_name='projectmember')
