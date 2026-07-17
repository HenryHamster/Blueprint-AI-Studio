"""Tests for migration 0621 — SQL-injection-safe encrypted-column backfill.

Migration 0621 carries the same semantic payload as 0531 (idempotent re-backfill
of every column whose source migration no-oped when GITHUB_PAT_ENCRYPTION_KEY
was absent) but uses SQLAlchemy Table reflection + parameterised sa.select /
sa.update instead of f-string interpolation into sa.text().

Properties pinned here are the SAME as test_migration_0531_backfill_encryption.py:
  (a) genuinely-plaintext rows get encrypted and decrypt back in ONE step;
  (b) already-ciphertext rows are left UNTOUCHED (no double-encryption);
  (c) a keyless environment no-ops without raising and leaves values untouched.

Additionally pinned:
  (d) the empty-string webhook_url row is skipped (skip_empty_strings guard);
  (e) downgrade decrypts back with a key, no-ops without one.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from cryptography.fernet import Fernet

_PLAINTEXTS = {
    "oauth_token": "oauth-secret-ref-abc123",
    "claude_config_json": '{"config": "plaintext-json-blob"}',
    "secret_blob_ref": "projects/p/secrets/s/versions/1",
    "webhook_url": "https://hooks.example.com/T000/B000/xoxb-secret-token",
    "github_pat": "ghp_plaintextPersonalAccessToken123",
}


def _load_migration(revision: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{revision}_safe", path)
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
    # Empty-string webhook must be skipped (skip_empty_strings guard mirrors 0484).
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
    """Properties (a), (b), (c) — matches 0531 acceptance contract.

    (a) plaintext rows encrypt and decrypt back in ONE step;
    (b) already-ciphertext rows are left byte-for-byte UNTOUCHED (no double-enc);
    (c) keyless environment no-ops without raising.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    mig = _load_migration("0621", "0621_safe_encrypt_backfill.py")

    # ---- (a) genuinely-plaintext rows get encrypted and decrypt back.
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
        # (d) empty-string webhook is skipped, not encrypted.
        assert _stored(conn, "notification_dead_letter", "webhook_url", "ndl-empty") == ""

        # ---- (b) re-running is a NO-OP on already-ciphertext values.
        snapshot = {(t, c, rid): _stored(conn, t, c, rid) for t, c, rid in _COLUMNS}
        mig.upgrade()  # second pass
        for (t, c, rid), before in snapshot.items():
            after = _stored(conn, t, c, rid)
            assert after == before, (
                f"{t}.{c} was rewritten on re-run — double-encryption risk"
            )
            assert fe.decrypt_field(after) == _PLAINTEXTS[c], (
                f"{t}.{c} no longer decrypts in ONE step after re-run"
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
    """(e) symmetric downgrade decrypts back with a key, no-ops without one."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    mig = _load_migration("0621", "0621_safe_encrypt_backfill.py")

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


def test_no_f_string_sql_in_migration_source():
    """Migration 0621 must not use f-string interpolation into sa.text()."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0621_safe_encrypt_backfill.py"
    )
    source = path.read_text()
    # f-strings containing SQL keywords are the target pattern to eliminate.
    # This check looks for the specific anti-pattern: sa.text(f"...") or
    # sa.text(f'...') that was present in 0531.
    assert 'sa.text(f"' not in source, "f-string interpolated into sa.text() found in 0621"
    assert "sa.text(f'" not in source, "f-string interpolated into sa.text() found in 0621"
