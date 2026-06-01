"""Drop regressionremediationcase + dispatchercyclerecord tables.

Removes the persistence layer for the detect->investigate->fix self-repair
apparatus (the regression-remediation dispatcher and its per-cycle telemetry).
The apparatus had a 0/200 clean-resolution record; every valuable signal it
once carried is now preserved as a deterministic operator Alert or as strategic
context, so the durable case/cycle tables are no longer read or written by any
code path.

Drops:
  - ``regressionremediationcase`` (the per-fingerprint remediation case rows,
    plus its unique constraint ``ux_regressionremediationcase_project_class``,
    index ``ix_regressionremediationcase_project_status_last_seen``, the
    project_id btree, and the status check constraint).
  - ``dispatchercyclerecord`` (the per-cycle dispatcher throughput records,
    plus its ``ix_dispatchercyclerecord_project_started`` and project_id
    indexes).

The ``downgrade()`` recreates both tables with their full column set (as of the
final pre-drop schema) so the rollback path restores a usable schema, matching
the substantive-downgrade invariant. The SQLite test fixture builds tables from
``SQLModel.metadata.create_all`` (the models are gone, so the tables simply no
longer exist there); this migration is what makes the Postgres CI schema match.

Revision ID: 0130
Revises: 0129
Create Date: 2026-05-31 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # drop_table removes the table together with all its own indexes and
    # constraints, so explicit drop_index calls are unnecessary here. The
    # regressionremediationcase.project_id FK is ON DELETE CASCADE / SET NULL
    # only toward parents; nothing references these tables, so a plain drop
    # is safe on both Postgres and SQLite.
    op.drop_table("dispatchercyclerecord")
    op.drop_table("regressionremediationcase")


def downgrade() -> None:
    op.create_table(
        "regressionremediationcase",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("regression_class", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ACTIVE"),
        sa.Column("first_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sample_message", sa.String(), nullable=True),
        sa.Column(
            "last_remediation_task_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_fingerprint_at_spawn", sa.String(length=12), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "auto_escalation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_remediation_pr_number", sa.Integer(), nullable=True),
        sa.Column("last_remediation_result", sa.String(), nullable=True),
        sa.Column("last_failure_class", sa.String(), nullable=True),
        sa.Column(
            "signal_snapshot_at_spawn",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "signal_snapshot_at_merge",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "fingerprint_to_fix_log",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("needs_manual_intervention_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("operator_acknowledged_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("signal_first_crossed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("signal_first_seen_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("escalation_reason", sa.String(), nullable=True),
        sa.Column("signal_healthy_since_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("false_positive_until_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("false_positive_reason", sa.String(), nullable=True),
        sa.Column(
            "symptoms_by_layer",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("investigate_spawned_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column(
            "fast_track_next_spawn",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_failure_summary", sa.Text(), nullable=True),
        sa.Column("marginal_followup_spawned_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("compliance_followup_spawned_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column(
            "compliance_followup_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("introducing_pr_number", sa.Integer(), nullable=True),
        sa.Column("introducing_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("last_unactionable_verdict", sa.String(length=64), nullable=True),
        sa.Column("last_unactionable_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REMEDIATING', 'MERGED', 'CLOSED', "
            "'NEEDS_MANUAL_INTERVENTION', 'FALSE_POSITIVE')",
            name="ck_regressionremediationcase_status",
        ),
    )
    op.create_unique_constraint(
        "ux_regressionremediationcase_project_class",
        "regressionremediationcase",
        ["project_id", "regression_class"],
    )
    op.create_index(
        "ix_regressionremediationcase_project_status_last_seen",
        "regressionremediationcase",
        ["project_id", "status", "last_seen_at"],
    )

    op.create_table(
        "dispatchercyclerecord",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("cycle_started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("cycle_finished_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column(
            "signals_evaluated",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "signals_tripped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "spawns_emitted",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rejections_by_reason",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("mttd_p50_sec", sa.Float(), nullable=True),
        sa.Column(
            "marginal_recoveries_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "confirmed_recoveries_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("avg_prevention_rate_7d", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dispatchercyclerecord_project_started",
        "dispatchercyclerecord",
        ["project_id", "cycle_started_at"],
    )
