"""project_github_owner_repo_idx

Add composite index on Project(github_owner, github_repo) for the
frequent natural-key lookup pattern used across admin, github,
autonomous_research, and deploy_identity query sites.
Without this index every lookup triggers a sequential scan.

Revision ID: 0423
Revises: 0422
"""
from __future__ import annotations

import logging

from alembic import op

revision = "0423"
down_revision = "0422"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.create_index(
        "ix_project_github_owner_repo",
        "project",
        ["github_owner", "github_repo"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_github_owner_repo", table_name="project")
