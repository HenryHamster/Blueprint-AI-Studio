"""Tests for migration 0609 — fail-closed encrypted-column reconciliation.

Migration 0609 is the comprehensive reconciliation migration that:
  (a) includes userintegration.github_oauth_token (added by 0566) in its targets;
  (b) refuses to run without a valid Fernet key (fail-closed, no ALLOW_PLAINTEXT opt-out);
  (c) encrypts genuinely-plaintext rows and leaves ciphertext unchanged (idempotent);
  (d) reconciles the nested webhook_url inside deploymentstate.runtime_config_json;
  (e) raises RuntimeError when a required column is absent.

Properties pinned here address Salvage finding #1 (fail_closed_on_keyless_backfill):
  — github_oauth_token IS in the backfill targets (covering the 0566 column).
  — a missing or malformed key raises BEFORE any row is read (no silent no-op).
  — ALLOW_PLAINTEXT_CREDENTIALS does NOT suppress the error (unconditional).
"""

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0609_reconcile_encrypted_columns_fail_closed.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0609", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_engine():
    return sa.create_engine("sqlite:///:memory:")


def _create_schema(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE userintegration ("
            "  id TEXT PRIMARY KEY,"
            "  github_pat TEXT,"
            "  github_oauth_token TEXT"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE maxplanaccount ("
            "  id TEXT PRIMARY KEY,"
            "  oauth_token TEXT,"
            "  claude_config_json TEXT,"
            "  secret_blob_ref TEXT"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE notification_dead_letter ("
            "  id TEXT PRIMARY KEY,"
            "  webhook_url TEXT"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE deploymentstate ("
            "  id TEXT PRIMARY KEY,"
            "  runtime_config_json TEXT"
            ")"
        )
    )


def _seed(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO userintegration (id, github_pat, github_oauth_token) "
            "VALUES ('ui-1', 'ghp_plaintext', 'oauth_plaintext')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO maxplanaccount (id, oauth_token, claude_config_json, secret_blob_ref) "
            "VALUES ('ma-1', 'oauth_token_plain', '{\"key\": \"value\"}', 'blob_ref_plain')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO notification_dead_letter (id, webhook_url) "
            "VALUES ('ndl-1', 'https://hooks.example.com/secret')"
        )
    )
    ds_config = json.dumps(
        {"notification_config": {"webhook_url": "https://hooks.example.com/nested"}}
    )
    conn.execute(
        sa.text(
            "INSERT INTO deploymentstate (id, runtime_config_json) VALUES ('ds-1', :cfg)"
        ),
        {"cfg": ds_config},
    )


def _stored(conn: sa.Connection, table: str, column: str, row_id: str) -> str | None:
    row = conn.execute(
        sa.text(f"SELECT {column} FROM {table} WHERE id = :id"),
        {"id": row_id},
    ).one_or_none()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Fail-closed: key must be present and valid
# --------------------------------------------------------------------------- #


def test_absent_key_raises_before_any_row_is_read(monkeypatch):
    """Missing GITHUB_PAT_ENCRYPTION_KEY raises RuntimeError unconditionally.

    Finding #1 — fail-closed on keyless backfill: unlike 0531 (which called
    is_encryption_active() and no-oped silently), 0609 must raise before any row
    is read — even when ALLOW_PLAINTEXT_CREDENTIALS is set.
    """
    mig = _load_migration()
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    # ALLOW_PLAINTEXT_CREDENTIALS must NOT suppress the error.
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
        mig._load_fernet()

    engine = _make_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _seed(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            mig.upgrade()
        # Rows must be untouched — no data access happened before the raise.
        assert _stored(conn, "userintegration", "github_pat", "ui-1") == "ghp_plaintext"
        assert (
            _stored(conn, "userintegration", "github_oauth_token", "ui-1")
            == "oauth_plaintext"
        )


def test_malformed_key_raises_before_any_row_is_read(monkeypatch):
    """A set-but-malformed GITHUB_PAT_ENCRYPTION_KEY raises RuntimeError.

    Finding #1 — fail-closed on malformed key: a malformed key makes
    is_encryption_active() return False (because _get_fernet catches the
    ValueError), so 0531-style code would silently no-op. 0609 must raise.
    """
    mig = _load_migration()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
        mig._load_fernet()

    engine = _make_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _seed(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            mig.upgrade()
        # Rows untouched.
        assert _stored(conn, "userintegration", "github_oauth_token", "ui-1") == "oauth_plaintext"


# --------------------------------------------------------------------------- #
# github_oauth_token is in the backfill targets
# --------------------------------------------------------------------------- #


def test_github_oauth_token_is_in_scalar_targets():
    """Finding #1: userintegration.github_oauth_token must be in _SCALAR_TARGETS.

    This column was added by migration 0566 and was absent from 0531's
    _BACKFILL_TARGETS. 0609 covers it.
    """
    mig = _load_migration()
    targets = {(t, c) for t, c, _ in mig._SCALAR_TARGETS}
    assert ("userintegration", "github_oauth_token") in targets, (
        "userintegration.github_oauth_token must be in _SCALAR_TARGETS "
        "(it was absent from 0531 and is the column 0566 added)"
    )


def test_github_oauth_token_plaintext_encrypted_in_upgrade(monkeypatch):
    """Plaintext github_oauth_token rows get encrypted by 0609."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    mig = _load_migration()
    fernet = Fernet(key.encode())

    engine = _make_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _seed(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()

        stored = _stored(conn, "userintegration", "github_oauth_token", "ui-1")
        assert stored is not None and stored != "oauth_plaintext", (
            "github_oauth_token must be encrypted after upgrade"
        )
        # Must decrypt back to the original value in ONE step.
        assert fernet.decrypt(stored.encode()).decode() == "oauth_plaintext"

        # github_pat is also encrypted.
        pat_stored = _stored(conn, "userintegration", "github_pat", "ui-1")
        assert pat_stored is not None and pat_stored != "ghp_plaintext"
        assert fernet.decrypt(pat_stored.encode()).decode() == "ghp_plaintext"


# --------------------------------------------------------------------------- #
# Idempotency: re-running skips already-ciphertext values
# --------------------------------------------------------------------------- #


def test_upgrade_is_idempotent(monkeypatch):
    """Re-running 0609 must leave already-ciphertext values unchanged."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    mig = _load_migration()

    engine = _make_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _seed(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)

        mig.upgrade()
        snapshot_oauth = _stored(conn, "userintegration", "github_oauth_token", "ui-1")
        snapshot_pat = _stored(conn, "userintegration", "github_pat", "ui-1")

        # Second run must not change the already-ciphertext values.
        mig.upgrade()
        assert _stored(conn, "userintegration", "github_oauth_token", "ui-1") == snapshot_oauth
        assert _stored(conn, "userintegration", "github_pat", "ui-1") == snapshot_pat


# --------------------------------------------------------------------------- #
# Nested webhook in deploymentstate.runtime_config_json
# --------------------------------------------------------------------------- #


def test_nested_webhook_in_deploymentstate_is_encrypted(monkeypatch):
    """The webhook_url nested in deploymentstate.runtime_config_json is encrypted."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    mig = _load_migration()
    fernet = Fernet(key.encode())

    engine = _make_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _seed(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()

        stored_json = _stored(conn, "deploymentstate", "runtime_config_json", "ds-1")
        assert stored_json is not None
        config = json.loads(stored_json)
        stored_webhook = config["notification_config"]["webhook_url"]
        assert stored_webhook != "https://hooks.example.com/nested", (
            "nested webhook_url must be encrypted"
        )
        assert (
            fernet.decrypt(stored_webhook.encode()).decode()
            == "https://hooks.example.com/nested"
        )


# --------------------------------------------------------------------------- #
# Missing required column raises
# --------------------------------------------------------------------------- #


def test_missing_required_column_raises(monkeypatch):
    """If a required target column is absent, upgrade raises RuntimeError."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    mig = _load_migration()

    engine = _make_engine()
    with engine.begin() as conn:
        # Schema WITHOUT github_oauth_token column.
        conn.execute(
            sa.text(
                "CREATE TABLE userintegration (id TEXT PRIMARY KEY, github_pat TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE maxplanaccount ("
                "  id TEXT PRIMARY KEY,"
                "  oauth_token TEXT,"
                "  claude_config_json TEXT,"
                "  secret_blob_ref TEXT"
                ")"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE notification_dead_letter (id TEXT PRIMARY KEY, webhook_url TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE deploymentstate (id TEXT PRIMARY KEY, runtime_config_json TEXT)"
            )
        )
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        with pytest.raises(RuntimeError, match="github_oauth_token"):
            mig.upgrade()


# --------------------------------------------------------------------------- #
# Downgrade is intentionally irreversible
# --------------------------------------------------------------------------- #


def test_downgrade_raises_not_implemented():
    """0609 is intentionally irreversible — downgrade must raise NotImplementedError."""
    mig = _load_migration()
    with pytest.raises(NotImplementedError):
        mig.downgrade()
