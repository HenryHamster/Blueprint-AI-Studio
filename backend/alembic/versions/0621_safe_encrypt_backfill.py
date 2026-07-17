"""Idempotent backfill of encrypted-at-rest columns — SQL-injection-safe rewrite.

This migration carries the same semantic payload as 0531 (idempotent re-backfill
of every plaintext row in the audited encrypted columns) but replaces raw
f-string interpolation into ``sa.text()`` with SQLAlchemy Table reflection and
parameterised ``sa.select`` / ``sa.update`` constructs.  No f-string SQL is
emitted anywhere in the upgrade or downgrade paths.

WHY A NEW MIGRATION (INSTEAD OF EDITING 0531)
---------------------------------------------
Applied migration history is immutable.  0531 already exists in the chain and
cannot be modified.  0621 supersedes it: same backfill logic, no string
interpolation, and it chains linearly from 0609 so the graph remains acyclic
and the EXPECTED_SCHEMA_REVISION stays consistent.

BACKFILL TARGETS
----------------
Same five (table, column) pairs as 0531:

  maxplanaccount.oauth_token          — 0466, 0468, 0472
  maxplanaccount.claude_config_json   — 0466, 0467
  maxplanaccount.secret_blob_ref      — 0466
  notification_dead_letter.webhook_url — 0484 (also skips empty strings)
  userintegration.github_pat          — 0457

IDEMPOTENCY GUARD
-----------------
Identical to 0531 / 0472: ``decrypt_field(value) != value`` means the value is
ALREADY ciphertext and is skipped.  The plain ``encrypt_field(value) == value``
short-circuit avoids a no-key no-op turning into a double-encrypt attempt.

Revision ID: 0621
Revises: 0609
Create Date: 2026-07-16 00:00:00.000000

"""

import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "0621"
down_revision = "0612"
branch_labels = None
depends_on = None


# (table, column, skip_empty_strings)
# skip_empty_strings=True mirrors 0484's ``AND <column> != ''`` filter; all
# other targets leave empty-string handling to the NOT NULL clause only.
# Table and column names come exclusively from this fixed allowlist — never
# from row data — so parameterised queries are sufficient for SQL safety.
_BACKFILL_TARGETS: tuple[tuple[str, str, bool], ...] = (
    ("maxplanaccount", "oauth_token", False),
    ("maxplanaccount", "claude_config_json", False),
    ("maxplanaccount", "secret_blob_ref", False),
    ("notification_dead_letter", "webhook_url", True),
    ("userintegration", "github_pat", False),
)


def _backfill_column(
    bind: sa.engine.Connection,
    table_name: str,
    column_name: str,
    skip_empty_strings: bool,
    decrypt_field,
    encrypt_field,
) -> int:
    """Encrypt genuinely-plaintext rows of ``table_name.column_name``; skip ciphertext.

    Uses Table reflection and parameterised sa.select / sa.update — no
    f-string SQL interpolation.  Returns the count of rows re-encrypted.
    Idempotent: already-ciphertext values (detected via decrypt guard) are
    left untouched, so re-running never double-encrypts.
    """
    meta = sa.MetaData()
    tbl = sa.Table(table_name, meta, autoload_with=bind)
    col = tbl.c[column_name]

    where_clause = col.isnot(None)
    if skip_empty_strings:
        where_clause = where_clause & (col != "")

    rows = bind.execute(sa.select(tbl.c.id, col).where(where_clause)).fetchall()

    updated = 0
    for row_id, value in rows:
        # ALREADY-ENCRYPTED GUARD — DO NOT REMOVE (see module docstring).
        # decrypt_field() returns its input unchanged for non-ciphertext, so a
        # value that decrypts to something DIFFERENT is already encrypted.
        if decrypt_field(value) != value:
            continue
        encrypted = encrypt_field(value)
        if encrypted == value:
            continue
        bind.execute(
            sa.update(tbl).where(tbl.c.id == row_id).values({column_name: encrypted})
        )
        updated += 1
    return updated


def backfill_encrypted_columns(bind, decrypt_field, encrypt_field) -> dict[str, int]:
    """Backfill every target column.  Shared by ``upgrade`` (and any future hook)."""
    updated_by_column: dict[str, int] = {}
    for table_name, column_name, skip_empty_strings in _BACKFILL_TARGETS:
        updated_by_column[f"{table_name}.{column_name}"] = _backfill_column(
            bind, table_name, column_name, skip_empty_strings, decrypt_field, encrypt_field
        )
    return updated_by_column


def upgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field, encrypt_field, is_encryption_active

    if not is_encryption_active():
        logger.warning(
            "0621: field encryption not configured (no GITHUB_PAT_ENCRYPTION_KEY) "
            "— leaving all encrypted-at-rest columns unchanged (matches 0472's no-op)"
        )
        return

    updated_by_column = backfill_encrypted_columns(bind, decrypt_field, encrypt_field)
    logger.info("Migration 0621 backfilled encrypted columns: %s", updated_by_column)


def downgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field, is_encryption_active

    if not is_encryption_active():
        logger.warning(
            "0621 (downgrade): field encryption not configured — leaving "
            "encrypted-at-rest columns unchanged"
        )
        return

    updated_by_column: dict[str, int] = {}
    for table_name, column_name, skip_empty_strings in _BACKFILL_TARGETS:
        meta = sa.MetaData()
        tbl = sa.Table(table_name, meta, autoload_with=bind)
        col = tbl.c[column_name]

        where_clause = col.isnot(None)
        if skip_empty_strings:
            where_clause = where_clause & (col != "")

        rows = bind.execute(sa.select(tbl.c.id, col).where(where_clause)).fetchall()
        updated = 0
        for row_id, value in rows:
            plaintext = decrypt_field(value)
            if plaintext == value:
                # Not ciphertext for this key — leave as-is.
                continue
            bind.execute(
                sa.update(tbl)
                .where(tbl.c.id == row_id)
                .values({column_name: plaintext})
            )
            updated += 1
        updated_by_column[f"{table_name}.{column_name}"] = updated

    logger.info(
        "Migration 0621 (downgrade) decrypted encrypted columns: %s",
        updated_by_column,
    )
