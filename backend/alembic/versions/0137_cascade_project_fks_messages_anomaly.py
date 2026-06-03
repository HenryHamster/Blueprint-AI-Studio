"""Add ON DELETE CASCADE to the four Project-FK tables created after 0058.

Migration 0058 (``project_fk_cascades``) made project deletion FK-safe by
giving every then-existing Project-FK table ``ondelete='CASCADE'`` so that a
``DELETE FROM project`` cascades to its children instead of mid-delete
FK-violating. Four tables added *after* 0058 regressed that convention — their
creation migrations declared the project FK with no ``ondelete`` clause:

  - ``anomalyfingerprintstate``  (0080)
  - ``messageslastseen``         (0099)
  - ``messagesnoiseprefs``       (0103)
  - ``messagessavedviews``       (0105)

A force-delete of a project that has rows in any of these mid-delete
FK-violates. Observed in prod 2026-06-03: repeated 500s on
``DELETE /api/admin/projects/{id}?force=true`` with
``ForeignKeyViolationError … "messageslastseen_project_id_fkey"`` — the
force path bulk-cancels the project's tasks (committed) but the project-row
delete rolls back, leaving the project half-deleted and undeletable.

This re-asserts each FK with ON DELETE CASCADE so the delete cascades cleanly,
matching the 0058 convention. DROP-IF-EXISTS + recreate keeps it idempotent
across environments. Postgres-only: SQLite (test path) does not enforce FK
ondelete the same way and does not support ALTER TABLE DROP CONSTRAINT — the
migration is the source of truth for Postgres, mirroring 0058.

Revision ID: 0137
Revises: 0136
Create Date: 2026-06-03 02:00:00.000000

"""
from alembic import op


revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


# (table, fk_constraint_name, column). Constraint names follow the Postgres
# auto-gen convention ``<table>_<col>_fkey`` (no naming_convention is set on the
# metadata, so this is the observed default — same as 0058 relies on).
_PROJECT_FKS = [
    ("anomalyfingerprintstate", "anomalyfingerprintstate_project_id_fkey", "project_id"),
    ("messageslastseen", "messageslastseen_project_id_fkey", "project_id"),
    ("messagesnoiseprefs", "messagesnoiseprefs_project_id_fkey", "project_id"),
    ("messagessavedviews", "messagessavedviews_project_id_fkey", "project_id"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
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
    # Restore the pre-0137 shape: plain project FK with no ondelete.
    for table, fk_name, col in _PROJECT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
        op.create_foreign_key(
            fk_name,
            table,
            "project",
            [col],
            ["id"],
        )
