"""projectmessage resident_session_id (audience/session identity)

Adds a nullable ``resident_session_id`` VARCHAR(255) column to ``projectmessage``
so every operator message can be tagged with the resident Workday session it was
sent into. This lets each resident session scope its inbound poll, its
unattended-cap clock, and its work-state pacing signal to its OWN conversation
instead of every open operator message project-wide (design b182b46e).

``NULL`` means the row predates the feature (all existing rows) or was created
outside an active session (cadence path). NULL is always treated as
unscoped/visible by the scoping queries, so no pre-migration backlog is dropped.
Zero-downtime: nullable, no default change, no backfill.

A composite index ``(project_id, resident_session_id, created_at)`` serves both
the scoped poll/cap/pacing aggregates and the session-blind legacy queries.

Revision ID: 0396
Revises: 0384
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
# revision id "0396" is the RESERVED slot for this task (linearized against the
# production chain at merge time). down_revision chains from the current on-disk
# head (0384) so the branch's alembic chain stays single-headed — the exactly-one-
# head invariant enforced by test_expected_revision_matches_latest_migration /
# validate_standalone_chain. Merge-time reconciliation renumbers as needed.
revision = "0396"
down_revision = "0384"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectmessage",
        sa.Column("resident_session_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_projectmessage_project_session_created",
        "projectmessage",
        ["project_id", "resident_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projectmessage_project_session_created",
        table_name="projectmessage",
    )
    op.drop_column("projectmessage", "resident_session_id")
