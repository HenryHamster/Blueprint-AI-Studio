"""Add ``authoring_branch`` and ``head_sha`` to ``knowledgedocument``.

The ``KnowledgeDocument`` SQLModel gained two nullable columns recording the
git branch and short HEAD commit the producing research/design task ran against:
- ``authoring_branch`` (string, nullable): the branch name where the task ran
- ``head_sha`` (string, nullable): the short SHA of HEAD at task-run time

This mirrors the playbook entry provenance stamping (branch, head_sha) that
handlers now emit on strategic writes. The columns are nullable so legacy rows
and older agents that don't supply them remain valid.

Per CLAUDE.md this chains from the latest head (0149) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0150 to match. Purely additive and
reversible.

Revision ID: 0150
Revises: 0149
Create Date: 2026-06-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledgedocument",
        sa.Column("authoring_branch", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("head_sha", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledgedocument", "head_sha")
    op.drop_column("knowledgedocument", "authoring_branch")
