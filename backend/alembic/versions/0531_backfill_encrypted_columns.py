"""Idempotent backfill of every encrypted-at-rest column left plaintext.

WHY THIS MIGRATION EXISTS
-------------------------
Every encryption backfill migration — 0457 (``userintegration.github_pat``),
0466/0467/0468 and 0472 (``maxplanaccount.oauth_token`` /
``claude_config_json`` / ``secret_blob_ref``), and 0484
(``notification_dead_letter.webhook_url``) — GUARDS on
``GITHUB_PAT_ENCRYPTION_KEY`` being present and NO-OPS when it is absent
(0472 was hotfixed to this behaviour in #14660 to unblock the prod deploy).

The production migrate job ran WITHOUT the key, so every one of those
revisions is stamped as applied while the historical rows are still stored
in PLAINTEXT. Once the operator configures the key, new writes encrypt but
Alembic will never re-run an already-applied revision, so the old rows would
stay plaintext forever. This migration re-does the backfill for ALL of the
affected columns in one place.

WHY A MIGRATION (JUSTIFY-THE-CHOICE)
------------------------------------
A migration is the right primary vehicle: it matches every sibling
encryption backfill and the repo's atomic-schema convention, and — because
the already-encrypted guard below makes it fully idempotent — it is safe to
run in every environment and safe to re-run.

Operationally, 0531 does its real work on the ONE run in which it is applied
while the key is present. Because it is a NEW, not-yet-applied revision, its
first application performs the genuine backfill as long as
``GITHUB_PAT_ENCRYPTION_KEY`` is set for that migrate run. The operator must
therefore run the migrate job for this deploy WITH the key configured (the
service already has the key per the deploy BLOCKER; the migrate job must be
given it too). If the migrate job is ever run keyless again, 0531 no-ops and
is stamped applied — exactly like its siblings — and the operator would then
need to re-run the backfill against a key-present environment (e.g. a one-off
``alembic upgrade`` after re-stamping ``down`` to 0530, or a manual invocation
of this module's ``backfill_encrypted_columns`` helper from a key-present
shell). Given that the running service already carries the key, requiring the
migrate job to carry it too is a config action fully in the operator's
control, so the migration alone is sufficient and NO separate service/admin
hook is added here (keeping the change focused and low-risk, and avoiding a
second code path that could diverge from this one).

CORRECTNESS: the already-encrypted guard is the SAME one proven correct in
0472 and pinned by ``test_migration_0472_no_double_encrypt.py``:
``decrypt_field(value) != value`` means the value is ALREADY ciphertext, so
it is SKIPPED. A plain ``encrypt_field(value) == value`` check cannot catch
double-encryption (encrypting ciphertext yields DIFFERENT ciphertext), which
was the original 0472 data-destruction bug — so we skip on the decrypt guard
BEFORE encrypting.

This module imports ``app.field_encryption`` exactly as 0472 does; the
migration-apply gate tolerates that import because ``field_encryption`` has no
candidate-code dependencies.

Revision ID: 0531
Revises: 0530
Create Date: 2026-07-14 00:00:00.000000

"""

import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision = "0531"
down_revision = "0530"
branch_labels = None
depends_on = None


# (table, column, extra_where) — extra_where is appended verbatim to the
# ``WHERE <column> IS NOT NULL`` clause and mirrors each source migration's
# filter (0484 additionally skips empty-string webhook_url values). Table and
# column names come only from this fixed allowlist, never from row data.
_BACKFILL_TARGETS = (
    ("maxplanaccount", "oauth_token", ""),          # 0466, 0468, 0472
    ("maxplanaccount", "claude_config_json", ""),    # 0466, 0467
    ("maxplanaccount", "secret_blob_ref", ""),       # 0466
    ("notification_dead_letter", "webhook_url", " AND webhook_url != ''"),  # 0484
    ("userintegration", "github_pat", ""),           # 0457
)


def _backfill_column(bind, table, column, extra_where, decrypt_field, encrypt_field) -> int:
    """Encrypt genuinely-plaintext rows of ``table.column``; skip ciphertext.

    Returns the number of rows re-encrypted. Idempotent: a value that already
    decrypts to something different from itself is already ciphertext and is
    left untouched, so re-running this never double-encrypts.
    """
    rows = bind.execute(
        sa.text(
            f"SELECT id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL{extra_where}"
        )
    ).fetchall()

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
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
            {"value": encrypted, "id": row_id},
        )
        updated += 1
    return updated


def backfill_encrypted_columns(bind, decrypt_field, encrypt_field) -> dict[str, int]:
    """Backfill every target column. Shared by ``upgrade`` (and any future hook)."""
    updated_by_column: dict[str, int] = {}
    for table, column, extra_where in _BACKFILL_TARGETS:
        updated_by_column[f"{table}.{column}"] = _backfill_column(
            bind, table, column, extra_where, decrypt_field, encrypt_field
        )
    return updated_by_column


def upgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field, encrypt_field, is_encryption_active

    # GRACEFUL NO-OP when encryption is not configured — identical to 0472's
    # guard. Keeps this safe in the keyless prod migrate job, dev with
    # ALLOW_PLAINTEXT_CREDENTIALS, and CI. Re-running once the key is present
    # then performs the real backfill.
    if not is_encryption_active():
        logger.warning(
            "0531: field encryption not configured (no GITHUB_PAT_ENCRYPTION_KEY) "
            "— leaving all encrypted-at-rest columns unchanged (matches 0472's no-op)"
        )
        return

    updated_by_column = backfill_encrypted_columns(bind, decrypt_field, encrypt_field)
    logger.info("Migration 0531 backfilled encrypted columns: %s", updated_by_column)


def downgrade() -> None:
    bind = op.get_bind()

    from app.field_encryption import decrypt_field, is_encryption_active

    # Symmetric decrypt-back (like 0466/0472), guarded by the same no-op so a
    # keyless downgrade leaves values untouched rather than raising.
    if not is_encryption_active():
        logger.warning(
            "0531 (downgrade): field encryption not configured — leaving "
            "encrypted-at-rest columns unchanged"
        )
        return

    updated_by_column: dict[str, int] = {}
    for table, column, extra_where in _BACKFILL_TARGETS:
        rows = bind.execute(
            sa.text(
                f"SELECT id, {column} FROM {table} "
                f"WHERE {column} IS NOT NULL{extra_where}"
            )
        ).fetchall()
        updated = 0
        for row_id, value in rows:
            plaintext = decrypt_field(value)
            if plaintext == value:
                # Not ciphertext for this key — leave as-is.
                continue
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                {"value": plaintext, "id": row_id},
            )
            updated += 1
        updated_by_column[f"{table}.{column}"] = updated

    logger.info(
        "Migration 0531 (downgrade) decrypted encrypted columns: %s",
        updated_by_column,
    )
