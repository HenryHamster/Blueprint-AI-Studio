"""Regression tests for migration 0466 MaxPlanAccount credential encryption."""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet


_COLUMNS = ("oauth_token", "claude_config_json", "secret_blob_ref")


def _load_migration(revision: str, filename: str):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / filename
    )
    spec = importlib.util.spec_from_file_location(
        f"migration_{revision}_max_plan_credentials", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migrations():
    return (
        _load_migration("0466", "0466_encrypt_secret_blob_ref.py"),
        _load_migration("0467", "0467_encrypt_claude_config_json.py"),
        _load_migration("0468", "0468_encrypt_maxplanaccount_credentials.py"),
    )


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
    migration_0466, migration_0467, migration_0468 = _load_migrations()
    assert (migration_0466.revision, migration_0466.down_revision) == ("0466", "0459")
    assert (migration_0467.revision, migration_0467.down_revision) == ("0467", "0466")
    assert (migration_0468.revision, migration_0468.down_revision) == ("0468", "0467")


def test_no_key_is_noop_without_candidate_app_import(monkeypatch):
    migrations = _load_migrations()
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
        for migration in migrations:
            monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
            migration.upgrade()
        for migration in reversed(migrations):
            migration.downgrade()
        assert _stored_values(conn) == plaintext


@pytest.mark.parametrize(
    ("migration_index", "column"),
    [(1, "claude_config_json"), (2, "oauth_token")],
)
def test_followup_migration_handles_plaintext_independently(
    monkeypatch, migration_index, column
):
    migration = _load_migrations()[migration_index]
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
        encrypted = _stored_values(conn)
        assert fernet.decrypt(encrypted[column].encode()).decode() == plaintext[column]
        for untouched_column in set(_COLUMNS) - {column}:
            assert encrypted[untouched_column] == plaintext[untouched_column]

        migration.downgrade()
        assert _stored_values(conn) == plaintext


def test_upgrade_is_idempotent_and_downgrade_round_trips_sqlalchemy_2(monkeypatch):
    migration_0466, migration_0467, migration_0468 = _load_migrations()
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
        for migration in (migration_0466, migration_0467, migration_0468):
            monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration_0466.upgrade()
        encrypted_once = _stored_values(conn)
        for column, value in encrypted_once.items():
            assert value != plaintext[column]
            assert fernet.decrypt(value.encode()).decode() == plaintext[column]

        migration_0467.upgrade()
        assert _stored_values(conn) == encrypted_once
        migration_0468.upgrade()
        assert _stored_values(conn) == encrypted_once

        migration_0468.downgrade()
        after_0468_downgrade = _stored_values(conn)
        assert after_0468_downgrade["oauth_token"] == plaintext["oauth_token"]
        assert after_0468_downgrade["claude_config_json"] == encrypted_once["claude_config_json"]
        assert after_0468_downgrade["secret_blob_ref"] == encrypted_once["secret_blob_ref"]

        migration_0467.downgrade()
        after_0467_downgrade = _stored_values(conn)
        assert after_0467_downgrade["oauth_token"] == plaintext["oauth_token"]
        assert after_0467_downgrade["claude_config_json"] == plaintext["claude_config_json"]
        assert after_0467_downgrade["secret_blob_ref"] == encrypted_once["secret_blob_ref"]

        migration_0466.downgrade()
        assert _stored_values(conn) == plaintext
