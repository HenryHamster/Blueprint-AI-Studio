"""Encrypt existing plaintext github_pat values in UserIntegration.

Prior to this migration, ``github_pat`` values were stored as plaintext GCP
Secret Manager resource names.  This migration encrypts every existing value
using Fernet encryption.

A subsequent deploy enables encrypt-on-write and decrypt-on-read in the
application code; the key is provided via ``GITHUB_PAT_ENCRYPTION_KEY``.

Revision ID: 0457
Revises: 0456
Create Date: 2026-07-10 00:00:00.000000

"""

import logging
import os

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"

revision = "0457"
down_revision = "0456"
branch_labels = None
depends_on = None


def _get_fernet():
    """Return a Fernet instance, or None when encryption is unavailable.

    This migration must be self-contained because Blueprint's pre-promotion
    migration-apply gate runs candidate migration files inside the currently
    deployed backend process. Importing candidate app modules here would make the
    gate fail before the candidate code is promoted.
    """
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        logger.warning(
            "%s not set - migration 0457 leaves github_pat values unchanged",
            ENCRYPTION_KEY_ENV,
        )
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cryptography is unavailable - migration 0457 leaves github_pat "
            "values unchanged: %s",
            exc,
        )
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        logger.exception("Invalid %s", ENCRYPTION_KEY_ENV)
        return None


def _encrypt_field(value: str | None) -> str | None:
    if value is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        return value
    return fernet.encrypt(value.encode()).decode()


def _decrypt_field(value: str | None) -> str | None:
    if value is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001 - plaintext/corrupt values remain readable
        return value


def _github_pat_rows(bind):
    return bind.execute(
        sa.text(
            "SELECT id, github_pat FROM userintegration "
            "WHERE github_pat IS NOT NULL"
        )
    ).fetchall()


def _update_github_pat(bind, row_id, value: str) -> None:
    bind.execute(
        sa.text("UPDATE userintegration SET github_pat = :value WHERE id = :id"),
        {"value": value, "id": row_id},
    )


def upgrade() -> None:
    bind = op.get_bind()

    # Fetch all rows with a non-null github_pat.
    rows = _github_pat_rows(bind)

    updated = 0
    for row_id, plaintext in rows:
        encrypted = _encrypt_field(plaintext)
        if encrypted == plaintext:
            # No-op (no key configured) — skip the write to avoid inflating
            # the transaction log with identical values.
            continue
        _update_github_pat(bind, row_id, encrypted)
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

    # Decrypt back to plaintext.  If no key/dependency is configured, values
    # stay as-is, so the downgrade is safe in scratch and local environments.
    rows = _github_pat_rows(bind)

    updated = 0
    for row_id, encrypted in rows:
        plaintext = _decrypt_field(encrypted)
        if plaintext == encrypted:
            continue
        _update_github_pat(bind, row_id, plaintext)
        updated += 1

    if updated:
        logger.info(
            "Migration 0457 (downgrade): decrypted %d github_pat value(s)", updated
        )
