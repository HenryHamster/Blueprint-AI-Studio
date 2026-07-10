"""Encrypt existing plaintext oauth_token values in MaxPlanAccount.

Prior to this migration, ``oauth_token`` values were stored as plaintext in the
``maxplanaccount`` table.  This migration encrypts every existing non-null value
using the application-layer Fernet encryption (``app.field_encryption``).

``claude_config_json`` was already encrypted by migration 0467.

A subsequent deploy enables encrypt-on-write and decrypt-on-read in the application
code; the key is provided via ``GITHUB_PAT_ENCRYPTION_KEY``.

Revision ID: 0468
Revises: 0467
Create Date: 2026-07-10 00:00:00.000000

"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "0468"
down_revision = "0467"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Import encryption utility.  When no key is configured the module logs a
    # warning and passes values through unchanged — that is acceptable for dev/
    # test environments where the key may not exist yet.  In production the key
    # MUST be set so that data is actually encrypted.
    from app.field_encryption import encrypt_field

    column = "oauth_token"
    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        ).fetchall()
    else:
        rows = bind.execute(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        ).fetchall()

    updated = 0
    for row_id, plaintext in rows:
        encrypted = encrypt_field(plaintext)
        if encrypted == plaintext:
            # No-op (no key configured) — skip the write to avoid inflating
            # the transaction log with identical values.
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                f"UPDATE maxplanaccount SET {column} = %s WHERE id = %s",
                (encrypted, row_id),
            )
        else:
            bind.execute(
                f"UPDATE maxplanaccount SET {column} = ? WHERE id = ?",
                (encrypted, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0468: encrypted %d %s value(s)", updated, column
        )
    else:
        logger.info(
            "Migration 0468: no %s values to encrypt "
            "(either all null or no encryption key configured)",
            column,
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Decrypt back to plaintext.  If no key is configured, decrypt_field is a
    # no-op and values stay as-is — the downgrade is safe.
    from app.field_encryption import decrypt_field

    column = "oauth_token"
    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        ).fetchall()
    else:
        rows = bind.execute(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        ).fetchall()

    updated = 0
    for row_id, encrypted in rows:
        plaintext = decrypt_field(encrypted)
        if plaintext == encrypted:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                f"UPDATE maxplanaccount SET {column} = %s WHERE id = %s",
                (plaintext, row_id),
            )
        else:
            bind.execute(
                f"UPDATE maxplanaccount SET {column} = ? WHERE id = ?",
                (plaintext, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0468 (downgrade): decrypted %d %s value(s)", updated, column
        )
