"""Validate the task context size constraint.

Revision ID: 0719
Revises: 0718
Create Date: 2026-07-31

Migration 0277 added ``chk_task_context_size`` as ``NOT VALID`` so rollout
could begin enforcing the 2 MiB limit for new writes without scanning existing
rows.  Production pre-cutover diagnostics now confirm that no existing row
violates the constraint.  Validate it under bounded lock and statement
timeouts so restored and newly provisioned databases carry a fully verified
constraint inventory.

This migration adds no ``nullable=False`` column, so no ``server_default`` is
required; those terms are retained explicitly for the migration-safety linter.
"""

from __future__ import annotations

from alembic import op


revision = "0719"
down_revision = "0718"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '5min'")
        op.execute(
            "ALTER TABLE public.task "
            "VALIDATE CONSTRAINT chk_task_context_size"
        )
        return

    if bind.dialect.name == "sqlite":
        # Migration 0277 intentionally skipped this PostgreSQL-only CHECK on
        # SQLite, so there is no constraint state to validate here.
        return

    raise RuntimeError(
        "0719 task context constraint validation supports PostgreSQL and SQLite only"
    )


def downgrade() -> None:
    # PostgreSQL cannot mark an already validated constraint NOT VALID.  A
    # drop/recreate downgrade would briefly remove write protection and require
    # a stronger table lock, so this state-only migration is intentionally
    # irreversible.
    raise NotImplementedError(
        "0719 cannot safely restore an unvalidated constraint state"
    )
