"""Align QualificationDimensionResult schema with the Phase 1 SQLModel.

The table was created by migration 0469 (already on automain).  This migration
applies the schema-level deltas between the Phase 1a initial table (0469) and
the refined Phase 1 model:

  - Drop ``ix_qualificationdimensionresult_project_computed`` — the model no
    longer defines this composite index (project-level look-ups use the single-
    column ``ix_qualificationdimensionresult_project_id`` instead).
  - Make ``candidate_sha`` nullable — the model allows ``None`` (to match
    ``QualificationVerdict`` and migration-linter conventions); ``0469``
    declared it ``NOT NULL``.
  - Rename the unique constraint from
    ``uq_qualificationdimensionresult_version_sha_dim`` (long, asymmetrical)
    to ``uq_qualdimresult_version_sha_dimension`` (concise, matches model).

Revision ID: 0475
Revises: 0474
Create Date: 2026-07-10 00:00:00.000000

"""

from alembic import op

revision = "0475"
down_revision = "0474"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the composite index not defined by the model.
    op.drop_index(
        "ix_qualificationdimensionresult_project_computed",
        table_name="qualificationdimensionresult",
    )
    # 2. Make candidate_sha nullable to match the SQLModel.
    op.alter_column(
        "qualificationdimensionresult",
        "candidate_sha",
        nullable=True,
    )
    # 3. Rename the unique constraint to match the model name.
    op.drop_constraint(
        "uq_qualificationdimensionresult_version_sha_dim",
        "qualificationdimensionresult",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_qualdimresult_version_sha_dimension",
        "qualificationdimensionresult",
        ["release_version_id", "candidate_sha", "dimension"],
    )


def downgrade() -> None:
    # Reverse order of upgrade.
    op.drop_constraint(
        "uq_qualdimresult_version_sha_dimension",
        "qualificationdimensionresult",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_qualificationdimensionresult_version_sha_dim",
        "qualificationdimensionresult",
        ["release_version_id", "candidate_sha", "dimension"],
    )
    op.alter_column(
        "qualificationdimensionresult",
        "candidate_sha",
        nullable=False,
    )
    op.create_index(
        "ix_qualificationdimensionresult_project_computed",
        "qualificationdimensionresult",
        ["project_id", "computed_at"],
    )
