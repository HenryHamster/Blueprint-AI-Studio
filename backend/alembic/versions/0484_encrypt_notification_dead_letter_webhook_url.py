"""Encrypt persisted notification_dead_letter webhook_url values.

Notification webhook URLs embed bearer tokens (Slack, Discord, PagerDuty) in
the URL path and must be encrypted at rest — the last credential-bearing field
stored in plaintext after GITHUB_PAT, oauth_token, secret_blob_ref, and
claude_config_json were encrypted by migrations 0457-0468.

The migration is self-contained because the pre-promotion migration-apply gate
runs candidate migrations inside the currently deployed backend process.

Revision ID: 0484
Revises: 0475
Create Date: 2026-07-11 00:00:00.000000

"""

import logging
import os

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"

revision = "0484"
down_revision = "0475"
branch_labels = None
depends_on = None


def _get_fernet():
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        logger.warning(
            "%s not set - migration 0484 leaves webhook_url unchanged",
            ENCRYPTION_KEY_ENV,
        )
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cryptography is unavailable - migration 0484 leaves "
            "webhook_url unchanged: %s",
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
            "SELECT id, webhook_url FROM notification_dead_letter "
            "WHERE webhook_url IS NOT NULL AND webhook_url != ''"
        )
    ).fetchall()


def _update(bind, row_id, value: str) -> None:
    bind.execute(
        sa.text(
            "UPDATE notification_dead_letter SET webhook_url = :value WHERE id = :id"
        ),
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
    logger.info("Migration 0484 encrypted %d webhook_url value(s)", updated)


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
    logger.info(
        "Migration 0484 downgrade decrypted %d webhook_url value(s)", updated
    )
