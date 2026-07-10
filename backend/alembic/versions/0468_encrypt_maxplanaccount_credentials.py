"""Encrypt persisted MaxPlanAccount oauth_token values.

Migration 0466 already encrypts every MaxPlanAccount credential column. This
follow-up remains independently safe for databases produced by the original
candidate chain and idempotently skips ciphertext written by 0466.

The migration is self-contained because the pre-promotion migration-apply gate
runs candidate migrations inside the currently deployed backend process.

Revision ID: 0468
Revises: 0467
Create Date: 2026-07-10 00:00:00.000000

"""

import logging
import os

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"

revision = "0468"
down_revision = "0467"
branch_labels = None
depends_on = None


def _get_fernet():
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        logger.warning(
            "%s not set - migration 0468 leaves oauth_token unchanged",
            ENCRYPTION_KEY_ENV,
        )
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cryptography is unavailable - migration 0468 leaves oauth_token "
            "unchanged: %s",
            exc,
        )
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        logger.exception("Invalid %s", ENCRYPTION_KEY_ENV)
        return None


def _rows(bind):
    return bind.execute(
        sa.text(
            "SELECT id, oauth_token FROM maxplanaccount "
            "WHERE oauth_token IS NOT NULL"
        )
    ).fetchall()


def _update(bind, row_id, value: str) -> None:
    bind.execute(
        sa.text("UPDATE maxplanaccount SET oauth_token = :value WHERE id = :id"),
        {"value": value, "id": row_id},
    )


def _decrypt_if_encrypted(fernet, value: str) -> str | None:
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001 - plaintext is the expected miss
        return None


def upgrade() -> None:
    bind = op.get_bind()
    fernet = _get_fernet()
    if fernet is None:
        return

    updated = 0
    for row_id, value in _rows(bind):
        if _decrypt_if_encrypted(fernet, value) is not None:
            continue
        _update(bind, row_id, fernet.encrypt(value.encode()).decode())
        updated += 1
    logger.info("Migration 0468 encrypted %d oauth_token value(s)", updated)


def downgrade() -> None:
    bind = op.get_bind()
    fernet = _get_fernet()
    if fernet is None:
        return

    updated = 0
    for row_id, value in _rows(bind):
        plaintext = _decrypt_if_encrypted(fernet, value)
        if plaintext is None:
            continue
        _update(bind, row_id, plaintext)
        updated += 1
    logger.info("Migration 0468 downgrade decrypted %d oauth_token value(s)", updated)
