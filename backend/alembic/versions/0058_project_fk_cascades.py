"""Add ON DELETE CASCADE/SET NULL to Project-FK and Task-FK constraints.

Audit 2026-04-29 (DB comprehensive remediation, Phase A2 / finding C1+M-S3):
project deletion would FK-violate because a subset of Project-FK tables
(``alert``, ``residualremediationcase``) declared their FK without an
``ondelete`` clause. ORM cascade only covers ``Project.tasks``; everything
else relied on caller-side manual deletion (admin_projects.delete_project_admin
hand-deleted Alert rows but no other table). First production project delete
with messages or remediation cases would mid-delete FK-violate.

Tables that already had ``ondelete='CASCADE'`` from their creation migration
(``projectmember`` 0024, ``projectmessage`` 0030, ``strategiccycle`` 0047,
``systemmaturity`` 0052) are re-asserted here with ``DROP CONSTRAINT IF EXISTS``
+ recreate so the schema is uniform regardless of how a deployed environment
got there. Idempotent on dev/staging/prod.

Also closes M-S3: ``alert.task_id`` was a UUID column with no FK declaration
at all (so referencing-task delete paths leaked into orphan rows). Add the
FK with ``ondelete='SET NULL'`` so a parent task delete clears the linkage
without dropping the alert (alert messages should survive task GC for the
admin alert UI).

The composite index ``ix_alert_task_id_dismissed`` (added in 0036) already
satisfies the FK-cascade lookup via Postgres left-prefix match, so a
dedicated ``ix_alert_task_id`` is intentionally not added here.

Revision ID: 0056
Revises: 0055
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = '0056'
down_revision = '0055'
branch_labels = None
depends_on = None


# (table_name, fk_constraint_name, column_name, ondelete) tuples for the
# Project-FK rewrites. Constraint names follow Postgres auto-gen convention
# `<table>_<col>_fkey` (no naming_convention set on metadata, so this is the
# observed default).
_PROJECT_FKS = [
    ("alert",                     "alert_project_id_fkey",                     "project_id", "CASCADE"),
    ("projectmember",             "projectmember_project_id_fkey",             "project_id", "CASCADE"),
    ("projectmessage",            "projectmessage_project_id_fkey",            "project_id", "CASCADE"),
    ("strategiccycle",            "strategiccycle_project_id_fkey",            "project_id", "CASCADE"),
    ("residualremediationcase",   "residualremediationcase_project_id_fkey",   "project_id", "CASCADE"),
    ("systemmaturity",            "systemmaturity_project_id_fkey",            "project_id", "CASCADE"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite (test path) doesn't enforce FK ondelete the same way and
        # ALTER TABLE DROP CONSTRAINT is not supported. Tables created via
        # SQLModel.metadata.create_all reflect models.py, which in this
        # commit declares the same FKs without ondelete in the SQLModel
        # idiom (sa_column-wrapped FKs). The migration is the source of
        # truth for Postgres — SQLite tests rely on Python-level cleanup.
        return

    # Re-assert Project-FK constraints with ON DELETE CASCADE. DROP IF EXISTS
    # makes this idempotent across (a) tables that already had cascade from
    # their creation migration and (b) tables that had no ondelete at all.
    for table, fk_name, col, ondelete in _PROJECT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
        op.create_foreign_key(
            fk_name, table, "project",
            [col], ["id"],
            ondelete=ondelete,
        )

    # M-S3: add the missing alert.task_id → task.id FK with SET NULL.
    # Alert messages should survive task GC for the admin alert UI, so
    # SET NULL (not CASCADE).
    op.execute("ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_task_id_fkey")
    op.create_foreign_key(
        "alert_task_id_fkey", "alert", "task",
        ["task_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # Drop the alert.task_id FK entirely (it didn't exist pre-0056).
    op.execute("ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_task_id_fkey")

    # Restore Project-FK constraints to their pre-0056 shape:
    # the four tables that had CASCADE from their creation migration get
    # CASCADE re-asserted; the two that had no ondelete (alert,
    # residualremediationcase) get the no-ondelete variant restored.
    _PRE_0056_PROJECT_FKS = [
        ("alert",                     "alert_project_id_fkey",                     "project_id", None),
        ("projectmember",             "projectmember_project_id_fkey",             "project_id", "CASCADE"),
        ("projectmessage",            "projectmessage_project_id_fkey",            "project_id", "CASCADE"),
        ("strategiccycle",            "strategiccycle_project_id_fkey",            "project_id", "CASCADE"),
        ("residualremediationcase",   "residualremediationcase_project_id_fkey",   "project_id", None),
        ("systemmaturity",            "systemmaturity_project_id_fkey",            "project_id", "CASCADE"),
    ]
    for table, fk_name, col, ondelete in _PRE_0056_PROJECT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk_name}")
        kwargs = {}
        if ondelete is not None:
            kwargs["ondelete"] = ondelete
        op.create_foreign_key(
            fk_name, table, "project",
            [col], ["id"],
            **kwargs,
        )
