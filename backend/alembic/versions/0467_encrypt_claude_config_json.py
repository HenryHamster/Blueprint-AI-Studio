"""Encrypt existing plaintext claude_config_json values in MaxPlanAccount.

Prior to this migration, ``claude_config_json`` values were stored as plaintext
JSON strings. This migration encrypts every existing value using the
application-layer Fernet encryption (``app.field_encryption``).

A subsequent deploy enables encrypt-on-write and decrypt-on-read in the
application code; the key is provided via ``GITHUB_PAT_ENCRYPTION_KEY``.

Revision ID: 0467
Revises: 0466
Create Date: 2026-07-10 00:00:00.000000

"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "0467"
down_revision = "0466"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import encrypt_field

    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            "SELECT id, claude_config_json FROM maxplanaccount WHERE claude_config_json IS NOT NULL"
        ).fetchall()
    else:
        rows = bind.execute(
            "SELECT id, claude_config_json FROM maxplanaccount WHERE claude_config_json IS NOT NULL"
        ).fetchall()

    updated = 0
    for row_id, plaintext in rows:
        encrypted = encrypt_field(plaintext)
        if encrypted == plaintext:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                "UPDATE maxplanaccount SET claude_config_json = %s WHERE id = %s",
                (encrypted, row_id),
            )
        else:
            bind.execute(
                "UPDATE maxplanaccount SET claude_config_json = ? WHERE id = ?",
                (encrypted, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0467: encrypted %d claude_config_json value(s)", updated
        )
    else:
        logger.info(
            "Migration 0467: no claude_config_json values to encrypt "
            "(either all null or no encryption key configured)"
        )


def downgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field

    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            "SELECT id, claude_config_json FROM maxplanaccount WHERE claude_config_json IS NOT NULL"
        ).fetchall()
    else:
        rows = bind.execute(
            "SELECT id, claude_config_json FROM maxplanaccount WHERE claude_config_json IS NOT NULL"
        ).fetchall()

    updated = 0
    for row_id, encrypted in rows:
        plaintext = decrypt_field(encrypted)
        if plaintext == encrypted:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                "UPDATE maxplanaccount SET claude_config_json = %s WHERE id = %s",
                (plaintext, row_id),
            )
        else:
            bind.execute(
                "UPDATE maxplanaccount SET claude_config_json = ? WHERE id = ?",
                (plaintext, row_id),
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0467 (downgrade): decrypted %d claude_config_json value(s)", updated
        )
