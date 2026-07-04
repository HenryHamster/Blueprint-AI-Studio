"""collapse strategic interaction modes to chat + continuous

The strategic interaction vocabulary is collapsed from four modes
(``chat`` / ``check_in`` / ``monitor`` / ``continuous``) to two: ``chat`` and
``continuous`` ("Workday"). This rewrites any per-operator workbench state that
is currently persisted in a retired mode so no project stays parked in
``check_in`` / ``monitor``.

The workbench state lives in ``messagesnoiseprefs.prefs`` (JSONB) under the
``_strategic_workbench`` key; the active mode is
``prefs -> '_strategic_workbench' -> 'interaction_mode'``. Any row whose mode is
``check_in`` or ``monitor`` is rewritten to ``chat`` (the neutral default).
Read paths already coerce a stray legacy value to ``chat`` defensively, so this
migration simply makes the persisted state consistent with the new vocabulary.

Historical per-message strategic-session snapshots are intentionally left
untouched — they are an immutable record of the mode in effect when the message
was sent, and read paths tolerate the legacy value.

Revision ID: 0367
Revises: 0366
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0367"
down_revision = "0366"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE messagesnoiseprefs
            SET prefs = jsonb_set(
                prefs,
                '{_strategic_workbench,interaction_mode}',
                '"chat"'::jsonb,
                false
            )
            WHERE prefs #>> '{_strategic_workbench,interaction_mode}'
                IN ('check_in', 'monitor')
            """
        )
    )


def downgrade() -> None:
    # Irreversible data migration: the original check_in / monitor distinction
    # is not recoverable once collapsed to chat, and both modes are retired. No
    # schema change to reverse.
    pass
