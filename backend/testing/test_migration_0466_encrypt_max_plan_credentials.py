"""Regression tests for migration 0466 MaxPlanAccount credential encryption."""

import importlib.util
import sys
from pathlib import Path

import sqlalchemy as sa
from cryptography.fernet import Fernet


_COLUMNS = ("oauth_token", "claude_config_json", "secret_blob_ref")


def _load_migration_0466():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0466_encrypt_secret_blob_ref.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0466_encrypt_secret_blob_ref", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_account(conn: sa.Connection, values: dict[str, str]) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE maxplanaccount ("
            "id TEXT PRIMARY KEY, oauth_token TEXT, claude_config_json TEXT, "
            "secret_blob_ref TEXT)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO maxplanaccount "
            "(id, oauth_token, claude_config_json, secret_blob_ref) VALUES "
            "(:id, :oauth_token, :claude_config_json, :secret_blob_ref)"
        ),
        {"id": "account-1", **values},
    )


def _stored_values(conn: sa.Connection) -> dict[str, str]:
    row = conn.execute(
        sa.text(
            "SELECT oauth_token, claude_config_json, secret_blob_ref "
            "FROM maxplanaccount WHERE id = :id"
        ),
        {"id": "account-1"},
    ).one()
    return dict(zip(_COLUMNS, row, strict=True))


def test_revision_metadata_points_to_actual_predecessor():
    migration = _load_migration_0466()
    assert migration.revision == "0466"
    assert migration.down_revision == "0459"


def test_no_key_is_noop_without_candidate_app_import(monkeypatch):
    migration = _load_migration_0466()
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "app.field_encryption", None)
    plaintext = {
        "oauth_token": "oauth-secret-ref",
        "claude_config_json": '{"onboarding":true}',
        "secret_blob_ref": "projects/p/secrets/s/versions/latest",
    }

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_account(conn, plaintext)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration.upgrade()
        migration.downgrade()
        assert _stored_values(conn) == plaintext


def test_upgrade_is_idempotent_and_downgrade_round_trips_sqlalchemy_2(monkeypatch):
    migration = _load_migration_0466()
    key = Fernet.generate_key().decode()
    fernet = Fernet(key.encode())
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    plaintext = {
        "oauth_token": "oauth-secret-ref",
        "claude_config_json": '{"onboarding":true}',
        "secret_blob_ref": "projects/p/secrets/s/versions/latest",
    }

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_account(conn, plaintext)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()
        encrypted_once = _stored_values(conn)
        for column, value in encrypted_once.items():
            assert value != plaintext[column]
            assert fernet.decrypt(value.encode()).decode() == plaintext[column]

        migration.upgrade()
        assert _stored_values(conn) == encrypted_once

        migration.downgrade()
        assert _stored_values(conn) == plaintext
