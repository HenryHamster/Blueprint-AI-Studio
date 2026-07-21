"""No-op migration for operator inbox liveness endpoint (read-only, no schema changes).

Revision ID: 0644
Revises: 0643
Create Date: 2026-07-21
"""

revision = "0644"
down_revision = "0643"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
