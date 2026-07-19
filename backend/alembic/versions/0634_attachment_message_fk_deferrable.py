"""Make projectmessageattachmentupload.message_id FK deferrable.

``create_operator_project_message`` stages the attachment-receipt ``message_id``
UPDATE (via its ``precommit_validate`` callback) that FK-references the message
row, but the message is INSERTed later in the same transaction; the
sequence-reservation's explicit ``flush`` emits that UPDATE first. With an
immediate (non-deferrable) FK, PostgreSQL rejects the UPDATE because the
referenced ``projectmessage`` row does not exist yet
(``projectmessageattachmentupload_message_id_fkey`` violation), even though it
exists by COMMIT. SQLite does not enforce the FK there, so this only bit on the
PostgreSQL CI shard.

Making the constraint ``DEFERRABLE INITIALLY DEFERRED`` moves the check to
COMMIT time (by which point the message row exists) without weakening it — the
FK is still enforced, just at transaction boundary. Postgres-only; SQLite has no
matching constraint object to alter (its FKs are declared inline on the table
and the test path builds the schema from SQLModel metadata there).

Revision ID: 0634
Revises: 0633
"""

from alembic import op

revision = "0634"
down_revision = "0633"
branch_labels = None
depends_on = None

_TABLE = "projectmessageattachmentupload"
_CONSTRAINT = "projectmessageattachmentupload_message_id_fkey"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (and any non-Postgres test dialect) does not carry this named
        # constraint object; the metadata-built schema has no separate FK to
        # alter, and SQLite FK timing differs. Nothing to do.
        return
    # Recreate the FK as DEFERRABLE INITIALLY DEFERRED, preserving ON DELETE SET
    # NULL (see models.ProjectMessageAttachmentUpload.message_id).
    op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}"')
    op.execute(
        f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_CONSTRAINT}" '
        f"FOREIGN KEY (message_id) REFERENCES projectmessage(id) "
        f"ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}"')
    op.execute(
        f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_CONSTRAINT}" '
        f"FOREIGN KEY (message_id) REFERENCES projectmessage(id) "
        f"ON DELETE SET NULL"
    )
