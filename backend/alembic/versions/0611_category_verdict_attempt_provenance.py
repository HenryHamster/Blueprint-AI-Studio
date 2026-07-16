"""Add qualification_attempt_id to CategoryVerdict and ResidualVerdict + persist category verdict snapshot.

Following the attempt-contract freeze (0607) which added attempt provenance to
QualificationVerdict and QualificationDimensionResult, this migration:

1. Adds ``qualification_attempt_id`` to ``categoryverdict`` — attempt-scoped
   querying and cross-attempt isolation for re-gradeable category verdicts.
2. Adds ``qualification_attempt_id`` to ``residualverdict`` — same for the
   per-candidate residual gap.
3. Replaces the old unique constraints with attempt-scoped ones so same-SHA
   re-grades from different attempts each retain their own row.
4. Adds ``category_verdicts`` (JSONB) and ``residual_uncaptured_gap`` (Text)
   to ``qualificationverdict`` so the immutable verdict history carries the
   snapshot of which category/residual facts drove the criterion_coverage gate.

All new columns are nullable so pre-existing rows (created before this migration)
remain readable and backward-compatible.  Indexes on the attempt columns support
the attempt-scoped query path in ``category_verdict_ledger``.

Revision ID: 0611
Revises: 0610
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

revision = "0611"
down_revision = "0610"
branch_labels = None
depends_on = None

GUID = sqlmodel.sql.sqltypes.GUID


def upgrade() -> None:
    # ----- categoryverdict -----
    op.add_column(
        "categoryverdict",
        sa.Column("qualification_attempt_id", GUID, nullable=True),
    )
    op.create_index(
        op.f("ix_categoryverdict_qualification_attempt_id"),
        "categoryverdict",
        ["qualification_attempt_id"],
    )
    op.drop_constraint(
        "uq_categoryverdict_version_sha_category",
        "categoryverdict",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_categoryverdict_version_sha_category_attempt",
        "categoryverdict",
        ["version_id", "candidate_sha", "category_key", "qualification_attempt_id"],
    )

    # ----- residualverdict -----
    op.add_column(
        "residualverdict",
        sa.Column("qualification_attempt_id", GUID, nullable=True),
    )
    op.create_index(
        op.f("ix_residualverdict_qualification_attempt_id"),
        "residualverdict",
        ["qualification_attempt_id"],
    )
    op.drop_constraint(
        "uq_residualverdict_version_sha",
        "residualverdict",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_residualverdict_version_sha_attempt",
        "residualverdict",
        ["version_id", "candidate_sha", "qualification_attempt_id"],
    )

    # ----- qualificationverdict (additive, no index/constraint changes) -----
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "category_verdicts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "residual_uncaptured_gap",
            sa.Text(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # ----- qualificationverdict -----
    op.drop_column("qualificationverdict", "residual_uncaptured_gap")
    op.drop_column("qualificationverdict", "category_verdicts")

    # ----- residualverdict -----
    op.drop_constraint(
        "uq_residualverdict_version_sha_attempt",
        "residualverdict",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_residualverdict_version_sha",
        "residualverdict",
        ["version_id", "candidate_sha"],
    )
    op.drop_index(
        op.f("ix_residualverdict_qualification_attempt_id"),
        table_name="residualverdict",
    )
    op.drop_column("residualverdict", "qualification_attempt_id")

    # ----- categoryverdict -----
    op.drop_constraint(
        "uq_categoryverdict_version_sha_category_attempt",
        "categoryverdict",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_categoryverdict_version_sha_category",
        "categoryverdict",
        ["version_id", "candidate_sha", "category_key"],
    )
    op.drop_index(
        op.f("ix_categoryverdict_qualification_attempt_id"),
        table_name="categoryverdict",
    )
    op.drop_column("categoryverdict", "qualification_attempt_id")
