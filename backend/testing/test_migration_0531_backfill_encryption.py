"""Tests for migration 0531 — idempotent multi-column encryption backfill.

Migration 0531 re-does the at-rest encryption backfill for EVERY column whose
source migration no-ops when ``GITHUB_PAT_ENCRYPTION_KEY`` is absent (0457,
0466/0467/0468, 0472, 0484). The prod migrate job ran keyless, so those rows
are stamped-applied yet still plaintext; 0531 encrypts them once a key exists.

The properties pinned here mirror ``test_migration_0472_no_double_encrypt.py``:
  (a) genuinely-plaintext rows get encrypted and decrypt back in ONE step;
  (b) already-ciphertext rows are left UNTOUCHED (no double-encryption);
  (c) a keyless environment no-ops without raising and leaves values untouched.

Coverage spans MULTIPLE audited columns across two tables
(``maxplanaccount.oauth_token`` / ``.claude_config_json`` / ``.secret_blob_ref``,
``notification_dead_letter.webhook_url``, ``userintegration.github_pat``) so the
multi-column backfill is exercised for real.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from cryptography.fernet import Fernet

# One representative value per audited column: (table, column, id, value).
_PLAINTEXTS = {
    "oauth_token": "oauth-secret-ref-abc123",
    "claude_config_json": '{"config": "plaintext-json-blob"}',
    "secret_blob_ref": "projects/p/secrets/s/versions/1",
    "webhook_url": "https://hooks.example.com/T000/B000/xoxb-secret-token",
    "github_pat": "ghp_plaintextPersonalAccessToken123",
}


def _load_migration(revision: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{revision}_bf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_schema(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE maxplanaccount ("
            "id TEXT PRIMARY KEY, oauth_token TEXT, claude_config_json TEXT, "
            "secret_blob_ref TEXT)"
        )
    )
    conn.execute(
        sa.text("CREATE TABLE notification_dead_letter (id TEXT PRIMARY KEY, webhook_url TEXT)")
    )
    conn.execute(
        sa.text("CREATE TABLE userintegration (id TEXT PRIMARY KEY, github_pat TEXT)")
    )


def _seed_plaintext(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO maxplanaccount (id, oauth_token, claude_config_json, secret_blob_ref) "
            "VALUES (:id, :ot, :cc, :sb)"
        ),
        {
            "id": "acct-1",
            "ot": _PLAINTEXTS["oauth_token"],
            "cc": _PLAINTEXTS["claude_config_json"],
            "sb": _PLAINTEXTS["secret_blob_ref"],
        },
    )
    conn.execute(
        sa.text("INSERT INTO notification_dead_letter (id, webhook_url) VALUES (:id, :w)"),
        {"id": "ndl-1", "w": _PLAINTEXTS["webhook_url"]},
    )
    # Empty-string webhook must be skipped (matches 0484's ``!= ''`` filter).
    conn.execute(
        sa.text("INSERT INTO notification_dead_letter (id, webhook_url) VALUES (:id, :w)"),
        {"id": "ndl-empty", "w": ""},
    )
    conn.execute(
        sa.text("INSERT INTO userintegration (id, github_pat) VALUES (:id, :p)"),
        {"id": "ui-1", "p": _PLAINTEXTS["github_pat"]},
    )


def _stored(conn: sa.Connection, table: str, column: str, row_id: str):
    return conn.execute(
        sa.text(f"SELECT {column} FROM {table} WHERE id = :id"),
        {"id": row_id},
    ).scalar_one()


_COLUMNS = (
    ("maxplanaccount", "oauth_token", "acct-1"),
    ("maxplanaccount", "claude_config_json", "acct-1"),
    ("maxplanaccount", "secret_blob_ref", "acct-1"),
    ("notification_dead_letter", "webhook_url", "ndl-1"),
    ("userintegration", "github_pat", "ui-1"),
)


def test_plaintext_rows_are_encrypted_ciphertext_untouched_keyless_noop(monkeypatch):
    """The named acceptance test — all three properties across multiple columns.

    (a) plaintext rows encrypt and decrypt back in ONE step;
    (b) already-ciphertext rows are left byte-for-byte UNTOUCHED (no double
        encryption) and still decrypt in ONE step;
    (c) a keyless environment no-ops without raising and leaves values as-is.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    mig = _load_migration("0531", "0531_backfill_encrypted_columns.py")

    # ---- (a) genuinely-plaintext rows get encrypted, decrypt back in one step.
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_schema(conn)
        _seed_plaintext(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()

        for table, column, row_id in _COLUMNS:
            stored = _stored(conn, table, column, row_id)
            assert stored != _PLAINTEXTS[column], (
                f"{table}.{column} must be encrypted, still plaintext"
            )
            assert fe.decrypt_field(stored) == _PLAINTEXTS[column], (
                f"{table}.{column} must decrypt back in ONE step"
            )
        # Empty-string webhook is skipped, not encrypted.
        assert _stored(conn, "notification_dead_letter", "webhook_url", "ndl-empty") == ""

        # ---- (b) re-running is a NO-OP on already-ciphertext values.
        snapshot = {
            (t, c, rid): _stored(conn, t, c, rid) for t, c, rid in _COLUMNS
        }
        mig.upgrade()  # second pass
        for (t, c, rid), before in snapshot.items():
            after = _stored(conn, t, c, rid)
            assert after == before, (
                f"{t}.{c} was rewritten on re-run — double-encryption risk"
            )
            assert fe.decrypt_field(after) == _PLAINTEXTS[c], (
                f"{t}.{c} no longer decrypts in ONE step — double-encrypted"
            )

    # ---- (c) keyless environment no-ops without raising, values untouched.
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    assert fe.is_encryption_active() is False

    engine2 = sa.create_engine("sqlite:///:memory:")
    with engine2.begin() as conn:
        _create_schema(conn)
        _seed_plaintext(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()  # must NOT raise
        for table, column, row_id in _COLUMNS:
            assert _stored(conn, table, column, row_id) == _PLAINTEXTS[column], (
                f"keyless migrate must leave {table}.{column} untouched"
            )


def test_downgrade_decrypts_back_and_no_ops_keyless(monkeypatch):
    """Symmetric downgrade decrypts back with a key, no-ops without one."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    mig = _load_migration("0531", "0531_backfill_encrypted_columns.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_schema(conn)
        _seed_plaintext(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()
        # sanity: encrypted
        assert _stored(conn, "userintegration", "github_pat", "ui-1") != _PLAINTEXTS["github_pat"]

        mig.downgrade()
        for table, column, row_id in _COLUMNS:
            assert _stored(conn, table, column, row_id) == _PLAINTEXTS[column], (
                f"downgrade must restore plaintext for {table}.{column}"
            )

    # Keyless downgrade: no raise, values untouched.
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    assert fe.is_encryption_active() is False
    engine2 = sa.create_engine("sqlite:///:memory:")
    with engine2.begin() as conn:
        _create_schema(conn)
        _seed_plaintext(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.downgrade()  # must NOT raise
        assert _stored(conn, "maxplanaccount", "oauth_token", "acct-1") == _PLAINTEXTS["oauth_token"]
