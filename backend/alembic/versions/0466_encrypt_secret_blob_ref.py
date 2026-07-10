"""Encrypt persisted MaxPlanAccount credential fields.

The application encrypts ``oauth_token``, ``claude_config_json``, and
``secret_blob_ref`` on write and decrypts them on read. Existing rows predate
that boundary and therefore need a one-time backfill.

This migration is self-contained because the pre-promotion migration-apply gate
runs candidate migrations inside the currently deployed backend process. It
must not depend on candidate application modules being importable.

Revision ID: 0466
Revises: 0459
Create Date: 2026-07-10 00:00:00.000000

"""

import logging
import os

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"
_CREDENTIAL_COLUMNS = (
    "oauth_token",
    "claude_config_json",
    "secret_blob_ref",
)

revision = "0466"
down_revision = "0459"
branch_labels = None
depends_on = None


def _get_fernet():
    """Return a Fernet instance, or ``None`` when encryption is unavailable."""
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        logger.warning(
            "%s not set - migration 0466 leaves credential values unchanged",
            ENCRYPTION_KEY_ENV,
        )
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cryptography is unavailable - migration 0466 leaves credential "
            "values unchanged: %s",
            exc,
        )
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        logger.exception("Invalid %s", ENCRYPTION_KEY_ENV)
        return None


def _credential_rows(bind, column: str):
    # ``column`` only comes from the fixed allowlist above; values remain bound.
    return bind.execute(
        sa.text(
            f"SELECT id, {column} FROM maxplanaccount "
            f"WHERE {column} IS NOT NULL"
        )
    ).fetchall()


def _update_credential(bind, column: str, row_id, value: str) -> None:
    bind.execute(
        sa.text(f"UPDATE maxplanaccount SET {column} = :value WHERE id = :id"),
        {"value": value, "id": row_id},
    )


def _decrypt_if_encrypted(fernet, value: str) -> str | None:
    """Return plaintext for this key's ciphertext, else ``None``."""
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001 - plaintext is the expected miss
        return None


def upgrade() -> None:
    bind = op.get_bind()
    fernet = _get_fernet()
    if fernet is None:
        return

    updated_by_column: dict[str, int] = {}
    for column in _CREDENTIAL_COLUMNS:
        updated = 0
        for row_id, value in _credential_rows(bind, column):
            # App writes may have landed during a rolling deploy. Do not wrap
            # ciphertext a second time when it already decrypts with this key.
            if _decrypt_if_encrypted(fernet, value) is not None:
                continue
            _update_credential(bind, column, row_id, fernet.encrypt(value.encode()).decode())
            updated += 1
        updated_by_column[column] = updated

    logger.info("Migration 0466 encrypted MaxPlanAccount credentials: %s", updated_by_column)


def downgrade() -> None:
    bind = op.get_bind()
    fernet = _get_fernet()
    if fernet is None:
        return

    updated_by_column: dict[str, int] = {}
    for column in _CREDENTIAL_COLUMNS:
        updated = 0
        for row_id, value in _credential_rows(bind, column):
            plaintext = _decrypt_if_encrypted(fernet, value)
            if plaintext is None:
                continue
            _update_credential(bind, column, row_id, plaintext)
            updated += 1
        updated_by_column[column] = updated

    logger.info(
        "Migration 0466 downgrade decrypted MaxPlanAccount credentials: %s",
        updated_by_column,
    )
