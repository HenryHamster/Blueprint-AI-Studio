"""Encrypt existing plaintext oauth_token values in MaxPlanAccount.

Prior to this migration, ``oauth_token`` values were stored as plaintext
Secret Manager resource names.  This migration encrypts every existing
value using Fernet encryption.

``claude_config_json`` was already encrypted by migration 0464.
The subsequent migration 0466 encrypts ``secret_blob_ref``.
Migration 0471 adds per-project cap columns.

Revision ID: 0472
Revises: 0471
Create Date: 2026-07-10 00:00:00.000000

"""

import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "0472"
down_revision = "0471"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import encrypt_field

    column = "oauth_token"
    rows = bind.execute(
        sa.text(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        )
    ).fetchall()

    updated = 0
    for row_id, plaintext in rows:
        encrypted = encrypt_field(plaintext)
        if encrypted == plaintext:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text(
                    f"UPDATE maxplanaccount SET {column} = :encrypted WHERE id = :row_id"
                ),
                {"encrypted": encrypted, "row_id": row_id},
            )
        else:
            bind.execute(
                sa.text(
                    f"UPDATE maxplanaccount SET {column} = :encrypted WHERE id = :row_id"
                ),
                {"encrypted": encrypted, "row_id": row_id},
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0470: encrypted %d %s value(s)", updated, column
        )
    else:
        logger.info(
            "Migration 0470: no %s values to encrypt "
            "(either all null or no encryption key configured)",
            column,
        )


def downgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field

    column = "oauth_token"
    rows = bind.execute(
        sa.text(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        )
    ).fetchall()

    updated = 0
    for row_id, encrypted in rows:
        plaintext = decrypt_field(encrypted)
        if plaintext == encrypted:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text(
                    "UPDATE maxplanaccount SET oauth_token = :plaintext WHERE id = :row_id"
                ),
                {"plaintext": plaintext, "row_id": row_id},
            )
        else:
            bind.execute(
                sa.text(
                    "UPDATE maxplanaccount SET oauth_token = :plaintext WHERE id = :row_id"
                ),
                {"plaintext": plaintext, "row_id": row_id},
            )
        updated += 1

    if updated:
        logger.info(
            "Migration 0470 (downgrade): decrypted %d %s value(s)", updated, column
        )
