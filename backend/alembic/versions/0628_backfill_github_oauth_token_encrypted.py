"""Idempotent encryption backfill for userintegration.github_oauth_token.

The column was added in migration 0566 (``github_oauth_token TEXT``).  New
writes are encrypted via ``app.field_encryption.encrypt_field`` (github.py:291
and github.py:298), but the column was absent from every prior backfill
migration (0531 / 0621 / 0626) because those migrations were authored before
0566 landed.  Any rows written during a keyless deployment window, or rows
that existed at 0566 migration time with a non-null value, may have remained
in plaintext.  This migration backfills them.

BLUEPRINT_DATA_MIGRATION_PROOF: upgrade() runs DML via bind.execute(sa.update(
...)) on the single target (userintegration, github_oauth_token).  Safe because:
(1) idempotent-per-row — decrypt_field(value) != value skips already-ciphertext
rows; (2) wrong-key guard — assert_plaintext_or_raise() halts if undecryptable
ciphertext is encountered; (3) fixed allowlist — table/column names are string
literals, not row data; (4) parameterised queries — no f-string SQL; (5)
fail-closed at both upgrade() and _backfill_column() levels via
require_encryption_active().

WHY NO app.* IMPORTS
--------------------
The promotion apply gate executes candidate migration files in-process on the
DEPLOYED engine.  Any ``from app.*`` import resolves against the serving tree,
so missing symbols cause ImportError and infra-fail every qualification attempt.
All encryption helpers are inlined here as module-local frozen copies — no
runtime dependency on ``app.field_encryption``.

Revision ID: 0628
Revises: 0627
Create Date: 2026-07-17 00:00:00.000000
"""

import logging
import os

from alembic import op
import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# ── Inline field-encryption helpers (no app.* imports allowed in migrations) ──
_ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"
_ALLOW_PLAINTEXT_CREDENTIALS_ENV = "ALLOW_PLAINTEXT_CREDENTIALS"
_FERNET_CIPHERTEXT_PREFIX = "gAAAAA"


def _get_fernet():
    key = os.getenv(_ENCRYPTION_KEY_ENV)
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def is_encryption_active() -> bool:
    return _get_fernet() is not None


def require_encryption_active(context: str) -> None:
    key = os.getenv(_ENCRYPTION_KEY_ENV)
    if key:
        try:
            Fernet(key.encode())
        except Exception as exc:
            raise RuntimeError(
                f"{context}: {_ENCRYPTION_KEY_ENV} is set but contains an invalid "
                f"Fernet key — encryption cannot proceed. "
                f"Supply a valid key generated with Fernet.generate_key(). "
                f"(Error: {exc})"
            ) from exc
        return
    if not os.getenv(_ALLOW_PLAINTEXT_CREDENTIALS_ENV):
        raise RuntimeError(
            f"{context}: {_ENCRYPTION_KEY_ENV} is not set and "
            f"{_ALLOW_PLAINTEXT_CREDENTIALS_ENV} is not set — "
            f"refusing to proceed without confirmed encryption or explicit "
            f"plaintext development opt-in. "
            f"Set {_ALLOW_PLAINTEXT_CREDENTIALS_ENV}=true for keyless dev environments."
        )


def encrypt_field(value):
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        if not os.getenv(_ALLOW_PLAINTEXT_CREDENTIALS_ENV):
            raise RuntimeError(
                f"{_ENCRYPTION_KEY_ENV} is not available and "
                f"{_ALLOW_PLAINTEXT_CREDENTIALS_ENV} is not set."
            )
        return value
    return f.encrypt(value.encode()).decode()


def decrypt_field(value):
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        if not os.getenv(_ALLOW_PLAINTEXT_CREDENTIALS_ENV):
            raise RuntimeError(
                f"{_ENCRYPTION_KEY_ENV} is not available and "
                f"{_ALLOW_PLAINTEXT_CREDENTIALS_ENV} is not set."
            )
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return value


def assert_plaintext_or_raise(value) -> None:
    if value is None:
        return
    if value.startswith(_FERNET_CIPHERTEXT_PREFIX) and decrypt_field(value) == value:
        raise RuntimeError(
            f"Wrong-key ciphertext detected: value starts with '{_FERNET_CIPHERTEXT_PREFIX}' "
            f"but decrypt_field returned it unchanged — the wrong encryption key may be "
            f"configured. Check {_ENCRYPTION_KEY_ENV}. Refusing to treat "
            "undecryptable ciphertext as plaintext."
        )
# ── End inline helpers ────────────────────────────────────────────────────────

revision = "0628"
down_revision = "0627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    require_encryption_active("Migration 0628")

    if not is_encryption_active():
        # ALLOW_PLAINTEXT_CREDENTIALS is set — genuine no-op for keyless dev.
        logger.warning(
            "0628: ALLOW_PLAINTEXT_CREDENTIALS is set without an encryption key "
            "— skipping backfill for keyless development environment"
        )
        return

    bind = op.get_bind()
    meta = sa.MetaData()
    tbl = sa.Table("userintegration", meta, autoload_with=bind)
    col = tbl.c["github_oauth_token"]

    rows = bind.execute(sa.select(tbl.c.id, col).where(col.isnot(None))).fetchall()
    updated = 0
    for row_id, value in rows:
        require_encryption_active("Migration 0628 _backfill_column")
        if decrypt_field(value) != value:
            continue
        assert_plaintext_or_raise(value)
        encrypted = encrypt_field(value)
        if encrypted == value:
            continue
        bind.execute(
            sa.update(tbl).where(tbl.c.id == row_id).values({"github_oauth_token": encrypted})
        )
        updated += 1

    logger.info("Migration 0628 backfilled userintegration.github_oauth_token: %d rows", updated)


def downgrade() -> None:
    if not is_encryption_active():
        logger.warning(
            "0628 (downgrade): field encryption not configured — leaving "
            "github_oauth_token column unchanged"
        )
        return

    bind = op.get_bind()
    meta = sa.MetaData()
    tbl = sa.Table("userintegration", meta, autoload_with=bind)
    col = tbl.c["github_oauth_token"]

    rows = bind.execute(sa.select(tbl.c.id, col).where(col.isnot(None))).fetchall()
    updated = 0
    for row_id, value in rows:
        plaintext = decrypt_field(value)
        if plaintext == value:
            continue
        bind.execute(
            sa.update(tbl).where(tbl.c.id == row_id).values({"github_oauth_token": plaintext})
        )
        updated += 1

    logger.info(
        "Migration 0628 (downgrade) decrypted userintegration.github_oauth_token: %d rows",
        updated,
    )
