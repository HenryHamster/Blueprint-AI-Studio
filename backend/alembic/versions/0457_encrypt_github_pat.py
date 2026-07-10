"""Encrypt existing plaintext github_pat values in UserIntegration.

Prior to this migration, ``github_pat`` values were stored as plaintext GCP
Secret Manager resource names.  This migration encrypts every existing value
using the application-layer Fernet encryption (``app.field_encryption``).

A subsequent deploy enables encrypt-on-write and decrypt-on-read in the
application code; the key is provided via ``GITHUB_PAT_ENCRYPTION_KEY``.

Revision ID: 0457
Revises: 0456
Create Date: 2026-07-10 00:00:00.000000

"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "0457"
down_revision = "0456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Import encryption utility.  When no key is configured the module logs a
    # warning and passes values through unchanged — that is acceptable for dev/
    # test environments where the key may not exist yet.  In production the key
    # MUST be set so that data is actually encrypted.
    from app.field_encryption import encrypt_field

    # Fetch all rows with a non-null github_pat.
    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            "SELECT id, github_pat FROM userintegration WHERE github_pat IS NOT NULL"
        ).fetchall()
    else:
        # SQLite (test environments)
        rows = bind.execute(
            "SELECT id, github_pat FROM userintegration WHERE github_pat IS NOT NULL"
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
                "UPDATE userintegration SET github_pat = %s WHERE id = %s",
                (encrypted, row_id),
            )
        else:
            bind.execute(
                "UPDATE userintegration SET github_pat = ? WHERE id = ?",
                (encrypted, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0457: encrypted %d github_pat value(s)", updated
        )
    else:
        logger.info(
            "Migration 0457: no github_pat values to encrypt "
            "(either all null or no encryption key configured)"
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Decrypt back to plaintext.  If no key is configured, decrypt_field is a
    # no-op and values stay as-is — the downgrade is safe.
    from app.field_encryption import decrypt_field

    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            "SELECT id, github_pat FROM userintegration WHERE github_pat IS NOT NULL"
        ).fetchall()
    else:
        rows = bind.execute(
            "SELECT id, github_pat FROM userintegration WHERE github_pat IS NOT NULL"
        ).fetchall()

    updated = 0
    for row_id, encrypted in rows:
        plaintext = decrypt_field(encrypted)
        if plaintext == encrypted:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                "UPDATE userintegration SET github_pat = %s WHERE id = %s",
                (plaintext, row_id),
            )
        else:
            bind.execute(
                "UPDATE userintegration SET github_pat = ? WHERE id = ?",
                (plaintext, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0457 (downgrade): decrypted %d github_pat value(s)", updated
        )
