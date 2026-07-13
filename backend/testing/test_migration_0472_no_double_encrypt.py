"""Regression test: migration 0472 must NEVER double-encrypt oauth_token.

Migration 0468 ("Encrypt persisted MaxPlanAccount oauth_token values") is ALREADY
APPLIED ON ``main`` — so by the time 0472 runs against a production database, every
``oauth_token`` is already Fernet ciphertext.

0472 originally called ``encrypt_field(stored)`` with no already-encrypted guard.
Its only check, ``if encrypted == plaintext: continue``, cannot catch this —
encrypting ciphertext yields DIFFERENT ciphertext, so the check never fires. The
result is ciphertext-of-ciphertext: a single ``decrypt_field`` then returns the
INNER CIPHERTEXT rather than the token, and every MaxPlan pool account credential
is permanently unusable.

This test pins the guard. It is a data-destruction regression, so it asserts the
end-to-end property that matters: after 0468 THEN 0472, the stored value must still
decrypt in ONE step back to the original token.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from cryptography.fernet import Fernet

_TOKEN = "oauth-secret-ref-abc123"


def _load_migration(revision: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{revision}_dbl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_account(conn: sa.Connection, oauth_token: str) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE maxplanaccount ("
            "id TEXT PRIMARY KEY, oauth_token TEXT, claude_config_json TEXT, "
            "secret_blob_ref TEXT)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO maxplanaccount (id, oauth_token) VALUES (:id, :tok)"
        ),
        {"id": "account-1", "tok": oauth_token},
    )


def _stored(conn: sa.Connection) -> str:
    return conn.execute(
        sa.text("SELECT oauth_token FROM maxplanaccount WHERE id = :id"),
        {"id": "account-1"},
    ).scalar_one()


def test_0472_after_0468_does_not_double_encrypt(monkeypatch):
    """0468 then 0472 — the token must still decrypt in ONE step.

    Reverting the guard in 0472 makes this fail: the stored value decrypts to the
    INNER CIPHERTEXT instead of the token.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    fe._get_fernet.cache_clear() if hasattr(fe._get_fernet, "cache_clear") else None

    mig_0468 = _load_migration("0468", "0468_encrypt_maxplanaccount_credentials.py")
    mig_0472 = _load_migration("0472", "0472_encrypt_oauth_token.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_account(conn, _TOKEN)

        # 0468 — the state a production DB is ALREADY in (it is on `main`).
        monkeypatch.setattr(mig_0468.op, "get_bind", lambda: conn)
        mig_0468.upgrade()
        after_0468 = _stored(conn)
        assert after_0468 != _TOKEN, "0468 must have encrypted the token"
        assert fe.decrypt_field(after_0468) == _TOKEN

        # 0472 — must be a NO-OP on an already-encrypted value.
        monkeypatch.setattr(mig_0472.op, "get_bind", lambda: conn)
        mig_0472.upgrade()
        after_0472 = _stored(conn)

    # THE PROPERTY THAT MATTERS: still ONE layer of encryption.
    assert fe.decrypt_field(after_0472) == _TOKEN, (
        "oauth_token was DOUBLE-ENCRYPTED — decrypt_field returned the inner "
        "ciphertext instead of the token. Every MaxPlan account credential would "
        "be permanently destroyed on deploy."
    )
    assert after_0472 == after_0468, "0472 must not rewrite an already-encrypted value"


def test_0472_still_encrypts_a_genuinely_plaintext_value(monkeypatch):
    """The guard must not disable the migration's actual job.

    A DB that somehow holds PLAINTEXT (never ran 0468) must still get encrypted —
    otherwise the fix would trade a data-destruction bug for a plaintext-secret bug.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

    import app.field_encryption as fe

    mig_0472 = _load_migration("0472", "0472_encrypt_oauth_token.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_account(conn, _TOKEN)  # plaintext, 0468 never ran
        monkeypatch.setattr(mig_0472.op, "get_bind", lambda: conn)
        mig_0472.upgrade()
        stored = _stored(conn)

    assert stored != _TOKEN, "a genuinely plaintext token must still be encrypted"
    assert fe.decrypt_field(stored) == _TOKEN
