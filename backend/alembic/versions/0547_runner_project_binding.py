"""Add ``runner_project_binding`` — runner-level project allow-list.

A fleet ``LocalRunner`` historically pinned to exactly one project via its
``project_id`` FK (its ENROLLMENT / default project). This table adds an
additive, editable many-to-many allow-list so ONE daemon per physical box can
serve N projects under a single unified GPU-index/concurrency ledger — without
a second per-project daemon double-booking the same hardware.

This revision is the ADDITIVE, non-breaking schema foundation ONLY:
``LocalRunner.project_id`` is UNCHANGED (it stays as the anchor member of the
runner's own allow-list). Dispatch/authz/claim rewiring lands in a follow-on
that depends on this table. See ``app.models.RunnerProjectBinding`` and
``app.runner_project_binding``.

Backfill: exactly one binding row per existing ``LocalRunner`` mapping
``runner.id -> runner.project_id``, so every already-enrolled runner has an
explicit allow-list row for its enrollment project. The backfill is idempotent
(``WHERE NOT EXISTS``) so a re-run / back-merge renumber never creates a
duplicate row (which the unique constraint would reject anyway).

Reserved revision id ``0547`` is used verbatim (parallel-migration reservation);
``down_revision`` chains from the current head ``0533``
(``add_alert_project_title_index``) to keep the chain linear.
``EXPECTED_SCHEMA_REVISION`` in ``backend/app/main.py`` is bumped to match.

Revision ID: 0547
Revises: 0531
Create Date: 2026-07-14 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0547"
down_revision = "0533"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard mirroring 0528/0503: never wedge a deploy on a duplicate
    # CREATE TABLE (re-run / back-merge renumber). On a fresh DB the table does
    # not exist and is created normally.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("runner_project_binding"):
        op.create_table(
            "runner_project_binding",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "runner_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["runner_id"], ["localrunner.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["project.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "runner_id",
                "project_id",
                name="uq_runner_project_binding_runner_project",
            ),
        )
        op.create_index(
            "ix_runner_project_binding_runner_id",
            "runner_project_binding",
            ["runner_id"],
        )
        op.create_index(
            "ix_runner_project_binding_project_id",
            "runner_project_binding",
            ["project_id"],
        )

    # Backfill one binding row per existing runner mapping its enrollment
    # project. Idempotent: WHERE NOT EXISTS skips any (runner, project) pair that
    # already has a row, so a re-run adds nothing (and the unique constraint is a
    # second line of defense). ``gen_random_uuid()`` is available via pgcrypto,
    # which the schema already relies on elsewhere.
    op.execute(
        sa.text(
            """
            INSERT INTO runner_project_binding (id, runner_id, project_id, created_at)
            SELECT gen_random_uuid(), lr.id, lr.project_id, now()
            FROM localrunner AS lr
            WHERE NOT EXISTS (
                SELECT 1 FROM runner_project_binding AS rpb
                WHERE rpb.runner_id = lr.id
                  AND rpb.project_id = lr.project_id
            )
            """
        )
    )


def downgrade() -> None:
    # Mirror the upgrade guard: drop_table raises if the table is absent.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("runner_project_binding"):
        return
    op.drop_index(
        "ix_runner_project_binding_project_id", table_name="runner_project_binding"
    )
    op.drop_index(
        "ix_runner_project_binding_runner_id", table_name="runner_project_binding"
    )
    op.drop_table("runner_project_binding")
