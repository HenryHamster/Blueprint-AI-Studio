"""Idempotent backfill of encrypted-at-rest columns — SQL-injection-safe rewrite.

This migration carries the same semantic payload as 0531 (idempotent re-backfill
of every plaintext row in the audited encrypted columns) but replaces raw
f-string interpolation into ``sa.text()`` with SQLAlchemy Table reflection and
parameterised ``sa.select`` / ``sa.update`` constructs.  No f-string SQL is
emitted anywhere in the upgrade or downgrade paths.

WHY A NEW MIGRATION (INSTEAD OF EDITING 0531)
---------------------------------------------
Applied migration history is immutable.  0531 already exists in the chain and
cannot be modified.  0621 supersedes it: same backfill logic, no string
interpolation, and it chains linearly from 0609 so the graph remains acyclic
and the EXPECTED_SCHEMA_REVISION stays consistent.

BACKFILL TARGETS
----------------
Same five (table, column) pairs as 0531:

  maxplanaccount.oauth_token          — 0466, 0468, 0472
  maxplanaccount.claude_config_json   — 0466, 0467
  maxplanaccount.secret_blob_ref      — 0466
  notification_dead_letter.webhook_url — 0484 (also skips empty strings)
  userintegration.github_pat          — 0457

IDEMPOTENCY GUARD
-----------------
Identical to 0531 / 0472: ``decrypt_field(value) != value`` means the value is
ALREADY ciphertext and is skipped.  The plain ``encrypt_field(value) == value``
short-circuit avoids a no-key no-op turning into a double-encrypt attempt.

# BLUEPRINT_DATA_MIGRATION_PROOF: upgrade() runs DML via bind.execute(sa.update(...))
# on 5 hardcoded (table, column) targets. Safe because: (1) idempotent-per-row —
# decrypt_field(value) != value skips ciphertext; (2) fixed allowlist — no row-data
# interpolation into table/column names; (3) parameterised queries — no f-string SQL;
# (4) fail-closed — _require_encryption_active() raises RuntimeError without valid key;
# (5) notification_dead_letter.webhook_url filters col != "". Downgrade uses the same
# invariants. All invariants hold for both upgrade and downgrade across all targets.

WHY NO app.* IMPORTS
--------------------
The promotion apply gate (backend/app/migration_apply_gate.py,
``_apply_candidate_migrations``) executes candidate migration FILES in-process
on the DEPLOYED engine.  Any ``from app.*`` import resolves against the SERVING
tree rather than the candidate, so missing symbols (e.g. ``require_encryption_active``
added after the serving SHA was deployed) cause ImportError and infra-fail every
qualification attempt.  Precedent: migration 0561 was fixed by PR #15100
(commit 1e51b910b) for the same class.  All encryption helpers are inlined here
as module-local frozen copies — no runtime dependency on ``app.field_encryption``.

Revision ID: 0621
Revises: 0609
Create Date: 2026-07-16 00:00:00.000000

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
# ── End inline helpers ────────────────────────────────────────────────────────

revision = "0621"
down_revision = "0613"
branch_labels = None
depends_on = None


# (table, column, skip_empty_strings)
# skip_empty_strings=True mirrors 0484's ``AND <column> != ''`` filter; all
# other targets leave empty-string handling to the NOT NULL clause only.
# Table and column names come exclusively from this fixed allowlist — never
# from row data — so parameterised queries are sufficient for SQL safety.
_BACKFILL_TARGETS: tuple[tuple[str, str, bool], ...] = (
    ("maxplanaccount", "oauth_token", False),
    ("maxplanaccount", "claude_config_json", False),
    ("maxplanaccount", "secret_blob_ref", False),
    ("notification_dead_letter", "webhook_url", True),
    ("userintegration", "github_pat", False),
)


def _backfill_column(
    bind: sa.engine.Connection,
    table_name: str,
    column_name: str,
    skip_empty_strings: bool,
    decrypt_field,
    encrypt_field,
) -> int:
    """Encrypt genuinely-plaintext rows of ``table_name.column_name``; skip ciphertext.

    Uses Table reflection and parameterised sa.select / sa.update — no
    f-string SQL interpolation.  Returns the count of rows re-encrypted.
    Idempotent: already-ciphertext values (detected via decrypt guard) are
    left untouched, so re-running never double-encrypts.
    """
    meta = sa.MetaData()
    tbl = sa.Table(table_name, meta, autoload_with=bind)
    col = tbl.c[column_name]

    where_clause = col.isnot(None)
    if skip_empty_strings:
        where_clause = where_clause & (col != "")

    rows = bind.execute(sa.select(tbl.c.id, col).where(where_clause)).fetchall()

    updated = 0
    for row_id, value in rows:
        # ALREADY-ENCRYPTED GUARD — DO NOT REMOVE (see module docstring).
        # decrypt_field() returns its input unchanged for non-ciphertext, so a
        # value that decrypts to something DIFFERENT is already encrypted.
        if decrypt_field(value) != value:
            continue
        encrypted = encrypt_field(value)
        if encrypted == value:
            continue
        bind.execute(
            sa.update(tbl).where(tbl.c.id == row_id).values({column_name: encrypted})
        )
        updated += 1
    return updated


def backfill_encrypted_columns(bind, decrypt_field, encrypt_field) -> dict[str, int]:
    """Backfill every target column.  Shared by ``upgrade`` (and any future hook)."""
    updated_by_column: dict[str, int] = {}
    for table_name, column_name, skip_empty_strings in _BACKFILL_TARGETS:
        updated_by_column[f"{table_name}.{column_name}"] = _backfill_column(
            bind, table_name, column_name, skip_empty_strings, decrypt_field, encrypt_field
        )
    return updated_by_column


def upgrade() -> None:
    # Fail closed before reading any row: absent key raises RuntimeError unless
    # ALLOW_PLAINTEXT_CREDENTIALS is explicitly set (development environments).
    # A malformed key always raises regardless of that flag.
    require_encryption_active("Migration 0621")

    if not is_encryption_active():
        # ALLOW_PLAINTEXT_CREDENTIALS is set — genuine no-op for keyless dev.
        logger.warning(
            "0621: ALLOW_PLAINTEXT_CREDENTIALS is set without an encryption key "
            "— skipping backfill for keyless development environment"
        )
        return

    bind = op.get_bind()
    updated_by_column = backfill_encrypted_columns(bind, decrypt_field, encrypt_field)
    logger.info("Migration 0621 backfilled encrypted columns: %s", updated_by_column)


def downgrade() -> None:
    bind = op.get_bind()

    if not is_encryption_active():
        logger.warning(
            "0621 (downgrade): field encryption not configured — leaving "
            "encrypted-at-rest columns unchanged"
        )
        return

    updated_by_column: dict[str, int] = {}
    for table_name, column_name, skip_empty_strings in _BACKFILL_TARGETS:
        meta = sa.MetaData()
        tbl = sa.Table(table_name, meta, autoload_with=bind)
        col = tbl.c[column_name]

        where_clause = col.isnot(None)
        if skip_empty_strings:
            where_clause = where_clause & (col != "")

        rows = bind.execute(sa.select(tbl.c.id, col).where(where_clause)).fetchall()
        updated = 0
        for row_id, value in rows:
            plaintext = decrypt_field(value)
            if plaintext == value:
                # Not ciphertext for this key — leave as-is.
                continue
            bind.execute(
                sa.update(tbl)
                .where(tbl.c.id == row_id)
                .values({column_name: plaintext})
            )
            updated += 1
        updated_by_column[f"{table_name}.{column_name}"] = updated

    logger.info(
        "Migration 0621 (downgrade) decrypted encrypted columns: %s",
        updated_by_column,
    )
