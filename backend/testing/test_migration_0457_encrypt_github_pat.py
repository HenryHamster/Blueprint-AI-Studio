"""Regression tests for migration 0457 github_pat encryption.

The release qualifier's migration-apply gate runs candidate migration files
inside the currently deployed backend process. Migration 0457 must therefore not
depend on candidate-only app modules being importable.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet


def _load_migration_0457():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0457_encrypt_github_pat.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0457_encrypt_github_pat", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_userintegration(conn: sa.Connection, github_pat: str) -> None:
    conn.execute(
        sa.text("CREATE TABLE userintegration (id TEXT PRIMARY KEY, github_pat TEXT)")
    )
    conn.execute(
        sa.text("INSERT INTO userintegration (id, github_pat) VALUES (:id, :pat)"),
        {"id": "integration-1", "pat": github_pat},
    )


def _stored_pat(conn: sa.Connection) -> str:
    return conn.execute(
        sa.text("SELECT github_pat FROM userintegration WHERE id = :id"),
        {"id": "integration-1"},
    ).scalar_one()


def test_upgrade_does_not_import_candidate_app_module(monkeypatch):
    migration = _load_migration_0457()
    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "app.field_encryption", None)

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_userintegration(conn, "secret-manager-resource")
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()

        assert _stored_pat(conn) == "secret-manager-resource"


def test_upgrade_and_irreversible_downgrade_with_sqlalchemy_2(monkeypatch):
    migration = _load_migration_0457()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_userintegration(conn, "secret-manager-resource")
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()
        encrypted = _stored_pat(conn)
        assert encrypted != "secret-manager-resource"

        with pytest.raises(NotImplementedError, match="rollback not supported"):
            migration.downgrade()
        assert _stored_pat(conn) == encrypted
