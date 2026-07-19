"""Add ComputeJob.ran_on_runner_id (authoritative claiming-runner record).

The only signals for which local runner owns a runner-subprocess compute job
today are the best-effort heartbeat-reported ``active_compute_job_ids`` list and
the JSON ``polling_metadata['runner_target_id']`` DISPATCH PIN. Neither is an
authoritative record of the runner that truly CLAIMED and ran the job. This adds
a durable, first-class column stamped at claim time (``/next-compute-job``).

Additive and backward compatible: a new nullable, indexed FK column on the
existing ``computejob`` table. Existing rows get NULL. ``ondelete="SET NULL"``
mirrors the existing ``task_id`` / ``artifact_id`` FKs on ComputeJob — a compute
job is durable history that must outlive a deleted runner. No consumer behavior
changes here (capacity counting / per-runner stats untouched); a follow-on task
switches consumers over.

NOTE: hand-written to match ``models.ComputeJob.ran_on_runner_id`` — ``alembic``
is not importable in the authoring environment, so ``--autogenerate`` could not
be run. Column type/nullability/index mirror the model exactly.

Revision ID: 0563
Revises: 0562
Create Date: 2026-07-14 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0563"
down_revision = "0562"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "computejob",
        sa.Column("ran_on_runner_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_computejob_ran_on_runner_id",
        "computejob",
        ["ran_on_runner_id"],
    )
    op.create_foreign_key(
        "fk_computejob_ran_on_runner_id_localrunner",
        "computejob",
        "localrunner",
        ["ran_on_runner_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_computejob_ran_on_runner_id_localrunner",
        "computejob",
        type_="foreignkey",
    )
    op.drop_index("ix_computejob_ran_on_runner_id", table_name="computejob")
    op.drop_column("computejob", "ran_on_runner_id")
