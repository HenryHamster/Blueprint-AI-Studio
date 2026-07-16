"""Fail-closed reconciliation of every field-encrypted database value.

This is intentionally a hand-authored data migration: Alembic autogenerate
cannot infer row encryption or the nested JSON credential reconciliation.
Unlike the applied 0531 backfill, this revision refuses to run without a real
Fernet key, so Alembic cannot stamp a no-op reconciliation as complete.

The migration is self-contained because candidate migrations are replayed by
the older serving engine during release qualification.  In particular, do not
import ``app.field_encryption`` or any other candidate-side app module here.

Revision ID: 0609
Revises: 0608
Create Date: 2026-07-16
"""

from __future__ import annotations

import binascii
import json
import logging
import os

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken

revision = "0609"
down_revision = "0608"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"
_DEAD_LETTER_TABLE = "notification_dead_letter"
_DEAD_LETTER_WEBHOOK_COLUMN = "webhook_url"

# These are the scalar storage locations written through
# app.field_encryption.encrypt_field.  All are required: 0609 is chained after
# 0608, which follows 0607 and therefore includes github_oauth_token, so the
# column is always present by the time this migration runs.
_SCALAR_TARGETS = (
    ("maxplanaccount", "oauth_token", False),
    ("maxplanaccount", "claude_config_json", False),
    ("maxplanaccount", "secret_blob_ref", False),
    ("notification_dead_letter", "webhook_url", False),
    ("userintegration", "github_pat", False),
    ("userintegration", "github_oauth_token", False),
)


def _load_fernet() -> Fernet:
    """Return the configured Fernet cipher or fail before any row is read."""
    raw_key = os.getenv(_ENCRYPTION_KEY_ENV)
    if not raw_key:
        raise RuntimeError(
            "Migration 0609 requires GITHUB_PAT_ENCRYPTION_KEY to be set to "
            "the live Fernet key before encrypted-column reconciliation; "
            "ALLOW_PLAINTEXT_CREDENTIALS does not permit this migration to stamp."
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise RuntimeError(
            "Migration 0609 requires GITHUB_PAT_ENCRYPTION_KEY to contain a "
            "valid Fernet key; no rows were touched and the revision was not stamped."
        ) from exc


def _encrypt_plaintext_once(fernet: Fernet, value: str) -> str | None:
    """Encrypt plaintext; return ``None`` for same-key Fernet ciphertext."""
    try:
        fernet.decrypt(value.encode("utf-8"))
    except InvalidToken:
        return fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return None


def _columns_for(bind: sa.Connection, table_name: str) -> set[str] | None:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return None
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _widen_notification_dead_letter_webhook(bind: sa.Connection) -> None:
    """Make room for Fernet expansion before encrypting legacy webhook URLs."""
    columns = _columns_for(bind, _DEAD_LETTER_TABLE)
    if columns is None or _DEAD_LETTER_WEBHOOK_COLUMN not in columns:
        raise RuntimeError(
            "Migration 0609 cannot reconcile encrypted values because required "
            "column notification_dead_letter.webhook_url is absent"
        )

    # SQLite does not enforce VARCHAR lengths and cannot perform this ALTER
    # directly. It is used only by the self-contained candidate-migration test;
    # production and qualification databases are PostgreSQL.
    if bind.dialect.name == "sqlite":
        return

    op.alter_column(
        _DEAD_LETTER_TABLE,
        _DEAD_LETTER_WEBHOOK_COLUMN,
        existing_type=sa.String(length=2048),
        type_=sa.Text(),
        existing_nullable=False,
    )


def _backfill_scalar_column(
    bind: sa.Connection,
    fernet: Fernet,
    table_name: str,
    column_name: str,
) -> int:
    table = sa.table(
        table_name,
        sa.column("id"),
        sa.column(column_name, sa.Text()),
    )
    column = table.c[column_name]
    rows = bind.execute(
        sa.select(table.c.id, column).where(column.is_not(None))
    ).fetchall()

    updated = 0
    for row_id, value in rows:
        if not isinstance(value, str) or not value:
            continue
        encrypted = _encrypt_plaintext_once(fernet, value)
        if encrypted is None:
            continue
        bind.execute(
            sa.update(table)
            .where(table.c.id == row_id)
            .values({column_name: encrypted})
        )
        updated += 1
    return updated


def _backfill_runtime_notification_webhook(
    bind: sa.Connection,
    fernet: Fernet,
) -> int:
    """Encrypt the webhook nested in DeploymentState.runtime_config_json."""
    columns = _columns_for(bind, "deploymentstate")
    if columns is None or "runtime_config_json" not in columns:
        raise RuntimeError(
            "Migration 0609 cannot reconcile encrypted values because required "
            "column deploymentstate.runtime_config_json is absent"
        )

    table = sa.table(
        "deploymentstate",
        sa.column("id"),
        sa.column("runtime_config_json", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(table.c.id, table.c.runtime_config_json).where(
            table.c.runtime_config_json.is_not(None)
        )
    ).fetchall()

    updated = 0
    for row_id, raw_config in rows:
        config = raw_config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (TypeError, ValueError):
                continue
        if not isinstance(config, dict):
            continue
        notification = config.get("notification_config")
        if not isinstance(notification, dict):
            continue
        webhook = notification.get("webhook_url")
        if not isinstance(webhook, str) or not webhook:
            continue
        encrypted = _encrypt_plaintext_once(fernet, webhook)
        if encrypted is None:
            continue

        reconciled = {
            **config,
            "notification_config": {**notification, "webhook_url": encrypted},
        }
        bind.execute(
            sa.update(table)
            .where(table.c.id == row_id)
            .values(runtime_config_json=reconciled)
        )
        updated += 1
    return updated


def backfill_encrypted_columns(bind: sa.Connection, fernet: Fernet) -> dict[str, int]:
    """Reconcile all current encrypted-at-rest locations idempotently."""
    updated: dict[str, int] = {}
    for table_name, column_name, optional in _SCALAR_TARGETS:
        columns = _columns_for(bind, table_name)
        if columns is None or column_name not in columns:
            if optional:
                updated[f"{table_name}.{column_name}"] = 0
                continue
            raise RuntimeError(
                "Migration 0609 cannot reconcile encrypted values because required "
                f"column {table_name}.{column_name} is absent"
            )
        updated[f"{table_name}.{column_name}"] = _backfill_scalar_column(
            bind, fernet, table_name, column_name
        )

    updated["deploymentstate.runtime_config_json.notification_config.webhook_url"] = (
        _backfill_runtime_notification_webhook(bind, fernet)
    )
    return updated


def upgrade() -> None:
    # Key construction deliberately precedes op.get_bind(): a missing/invalid
    # key cannot read or mutate a row, even when plaintext mode is allowed.
    fernet = _load_fernet()
    bind = op.get_bind()
    _widen_notification_dead_letter_webhook(bind)
    updated = backfill_encrypted_columns(bind, fernet)
    logger.info("Migration 0609 reconciled encrypted columns: %s", updated)


def downgrade() -> None:
    raise NotImplementedError(
        "Migration 0609 is intentionally irreversible: downgrading would decrypt "
        "credentials back to plaintext. Restore from a protected backup only if "
        "an operational rollback requires pre-0609 data semantics."
    )
