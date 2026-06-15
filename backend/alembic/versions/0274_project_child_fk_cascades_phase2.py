"""Add ON DELETE CASCADE to remaining project-child FKs.

Phase 2 of the project-FK cascade audit (Phase 1 was migration 0058).
Migration 0058 covered the tables added before it (alert, projectmember,
projectmessage, strategiccycle, residualremediationcase, systemmaturity).
Every table added AFTER 0058 that uses the plain SQLModel
``foreign_key="project.id"`` shorthand (no ondelete kwarg) carries no
ondelete action, so a project DELETE FK-violates on any such table that
holds rows.

Observed production failure: asyncpg ForeignKeyViolationError on
``messageslastseen_project_id_fkey`` when ``DELETE /api/admin/projects/{id}``
was called on a project with message-panel history.

This migration adds ON DELETE CASCADE to every remaining project-child FK
that lacks it.  Tables whose creation migration already declared
``ForeignKey("project.id", ondelete="CASCADE")`` (direction, releaseversion,
qualificationverdict, auditfindingresolution, improvementlane,
evergreenbacklogitem) are left untouched — their FK is already correct.

The migration is idempotent: DROP CONSTRAINT IF EXISTS before each recreate
means running it twice leaves the schema unchanged.

Revision ID: 0274
Revises: 0273
Create Date: 2026-06-13 00:00:00.000000
"""

from alembic import op


revision = "0274"
down_revision = "0273"
branch_labels = None
depends_on = None


# Tables that need CASCADE added. Each entry is
# (table_name, fk_constraint_name, column_name).
# Constraint names follow the Postgres auto-gen convention
# "<table>_<col>_fkey" (no naming_convention set on metadata).
_PROJECT_FKS = [
    ("messageslastseen", "messageslastseen_project_id_fkey", "project_id"),
    ("messagesnoiseprefs", "messagesnoiseprefs_project_id_fkey", "project_id"),
    ("messagessavedviews", "messagessavedviews_project_id_fkey", "project_id"),
    ("task", "task_project_id_fkey", "project_id"),
    ("computejob", "computejob_project_id_fkey", "project_id"),
    ("poolvm", "poolvm_project_id_fkey", "project_id"),
    ("poolidlecostledger", "poolidlecostledger_project_id_fkey", "project_id"),
    ("poolidlespan", "poolidlespan_project_id_fkey", "project_id"),
    ("knowledgedocument", "knowledgedocument_project_id_fkey", "project_id"),
    (
        "anomalyfingerprintstate",
        "anomalyfingerprintstate_project_id_fkey",
        "project_id",
    ),
    ("migrationreservation", "migrationreservation_project_id_fkey", "project_id"),
    ("taskwakeregistration", "taskwakeregistration_project_id_fkey", "project_id"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (test path) does not enforce FK ondelete and does not support
        # ALTER TABLE DROP CONSTRAINT. The migration is Postgres-only; SQLite
        # tests rely on explicit Python-level cleanup or FK non-enforcement.
        return

    for table, fk_name, col in _PROJECT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
        op.create_foreign_key(
            fk_name,
            table,
            "project",
            [col],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Restore to plain FK (no ondelete) — this is the pre-0274 state.
    for table, fk_name, col in _PROJECT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
        op.create_foreign_key(
            fk_name,
            table,
            "project",
            [col],
            ["id"],
        )
