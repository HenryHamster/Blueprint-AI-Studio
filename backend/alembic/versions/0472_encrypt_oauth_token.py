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

    from app.field_encryption import decrypt_field, encrypt_field, is_encryption_active

    # GRACEFUL NO-OP when encryption is not configured — matches migration 0468,
    # which guards the same column with `if _get_fernet() is None: return`.
    #
    # The prod migrate job runs WITHOUT GITHUB_PAT_ENCRYPTION_KEY (and without
    # ALLOW_PLAINTEXT_CREDENTIALS), so `encrypt_field` / `decrypt_field` RAISE
    # (_require_encryption_available). The original 0472 called `encrypt_field`
    # directly and therefore ALSO crashed the deploy's migrate step here — 0468
    # survived only because it no-ops when the key is absent. Mirror that: if
    # encryption is inactive there is nothing to encrypt, so leave oauth_token
    # untouched rather than crashing `alembic upgrade head`.
    if not is_encryption_active():
        logger.warning(
            "0472: field encryption not configured (no GITHUB_PAT_ENCRYPTION_KEY) "
            "— leaving oauth_token unchanged, matching migration 0468's no-op guard"
        )
        return

    column = "oauth_token"
    rows = bind.execute(
        sa.text(
            f"SELECT id, {column} FROM maxplanaccount WHERE {column} IS NOT NULL"
        )
    ).fetchall()

    updated = 0
    for row_id, plaintext in rows:
        # ALREADY-ENCRYPTED GUARD — DO NOT REMOVE.
        #
        # Migration 0468 (already applied on `main`) ALSO encrypts oauth_token:
        # "Encrypt persisted MaxPlanAccount oauth_token values."  So by the time
        # this migration runs on a production database, every value is ALREADY
        # ciphertext.  Passing that ciphertext to encrypt_field() treats it as
        # plaintext and produces ciphertext-of-ciphertext — a single decrypt then
        # yields the inner ciphertext, not the token, and EVERY MaxPlan account
        # credential is permanently destroyed.
        #
        # The pre-existing `encrypted == plaintext` check below cannot catch this:
        # encrypting ciphertext yields DIFFERENT ciphertext, so it never fires.
        #
        # decrypt_field() returns its input UNCHANGED when the value is not valid
        # ciphertext (see its docstring — that fallback exists so a rolling deploy
        # does not break pre-encryption plaintext).  So a value that decrypts to
        # something DIFFERENT from itself is already encrypted → skip it.  This
        # mirrors the `_decrypt_if_encrypted` guard migration 0468 already uses.
        if decrypt_field(plaintext) != plaintext:
            continue
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
