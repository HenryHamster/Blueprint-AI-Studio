"""Tests for backend/app/auth.py — email-verification enforcement."""

import pytest
from datetime import timedelta
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.main import app
from app.models import AllowedUser, _utcnow
from app.auth import (
    get_current_user,
    _persist_auth_metadata_best_effort,
    _LAST_LOGIN_UPDATE_INTERVAL,
)


def _creds(token="fake-firebase-token"):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _make_decoded_token(
    uid="test-uid",
    email="user@example.com",
    email_verified=True,
    name="Test User",
):
    return {
        "uid": uid,
        "email": email,
        "email_verified": email_verified,
        "name": name,
    }


class TestEmailVerificationEnforcement:
    """get_current_user must reject tokens where email_verified is falsy."""

    @pytest.mark.asyncio
    async def test_unverified_email_raises_403(self, override_get_session, db_session):
        """An otherwise-valid token with email_verified=False is rejected with 403."""
        decoded = _make_decoded_token(email_verified=False)

        # Remove any auth override so the real get_current_user runs
        saved = app.dependency_overrides.pop(get_current_user, None)
        try:
            with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as raw_client:
                    resp = await raw_client.get(
                        "/api/admin/users",
                        headers={"Authorization": "Bearer fake-firebase-token"},
                    )
        finally:
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        assert resp.status_code == 403
        assert "verified" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_unverified_email_missing_field_raises_403(
        self, override_get_session, db_session
    ):
        """email_verified absent from token (defaults False) is also rejected."""
        decoded = _make_decoded_token()
        decoded.pop("email_verified", None)

        saved = app.dependency_overrides.pop(get_current_user, None)
        try:
            with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as raw_client:
                    resp = await raw_client.get(
                        "/api/admin/users",
                        headers={"Authorization": "Bearer fake-firebase-token"},
                    )
        finally:
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        assert resp.status_code == 403
        assert "verified" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_verified_email_proceeds_past_verification_gate(
        self, override_get_session, db_session
    ):
        """A token with email_verified=True is not rejected by the verification gate."""
        email = "allowed@example.com"
        user = AllowedUser(email=email, role="user")
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(email=email, email_verified=True)

        saved = app.dependency_overrides.pop(get_current_user, None)
        try:
            with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as raw_client:
                    resp = await raw_client.get(
                        "/api/admin/users",
                        headers={"Authorization": "Bearer fake-firebase-token"},
                    )
        finally:
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        # Should not be blocked specifically for email verification
        if resp.status_code == 403:
            assert "verified" not in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_admin_api_key_bypasses_email_verification(
        self, override_get_session, db_session, monkeypatch
    ):
        """Admin API key path never touches Firebase, so email_verified is irrelevant."""
        monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-12345")

        saved = app.dependency_overrides.pop(get_current_user, None)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as raw_client:
                resp = await raw_client.get(
                    "/api/admin/users",
                    headers={"Authorization": "Bearer test-admin-key-12345"},
                )
        finally:
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        # Admin key returns 200 (or at least not blocked for email verification)
        if resp.status_code == 403:
            assert "verified" not in resp.json()["error"]["message"].lower()


class TestLastLoginThrottleAndBestEffort:
    """get_current_user must not write+commit on EVERY authenticated request (the
    auth-path amplifier behind the 2026-06-04 lock-contention outage), and the
    write must degrade — not 500 — when the DB is contended."""

    @pytest.mark.asyncio
    async def test_recent_last_login_is_not_rewritten(self, db_session):
        """Throttle: a recent last_login_at is left alone, so the common request
        does NO write/commit on the auth hot path."""
        email = "throttle@example.com"
        fresh = _utcnow() - timedelta(minutes=1)  # well within the interval
        db_session.add(
            AllowedUser(
                email=email, role="user", last_login_at=fresh, firebase_uid="uid-1"
            )
        )
        await db_session.commit()

        decoded = _make_decoded_token(email=email, uid="uid-1", name=None)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "user"
        row = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == email)
            )
        ).scalar_one()
        # Not bumped to ~now: still roughly a minute old.
        assert (_utcnow() - row.last_login_at) > timedelta(seconds=30)

    @pytest.mark.asyncio
    async def test_stale_last_login_is_refreshed(self, db_session):
        """A None/stale last_login_at IS refreshed (the throttle window elapsed)."""
        email = "stale@example.com"
        old = _utcnow() - (_LAST_LOGIN_UPDATE_INTERVAL + timedelta(minutes=5))
        db_session.add(
            AllowedUser(
                email=email, role="user", last_login_at=old, firebase_uid="uid-2"
            )
        )
        await db_session.commit()

        decoded = _make_decoded_token(email=email, uid="uid-2", name=None)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "user"
        row = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == email)
            )
        ).scalar_one()
        assert (_utcnow() - row.last_login_at) < timedelta(minutes=1)  # just stamped

    @pytest.mark.asyncio
    async def test_first_signin_links_firebase_uid(self, db_session):
        """First sign-in (no firebase_uid yet) links the uid and stamps login."""
        email = "link@example.com"
        db_session.add(AllowedUser(email=email, role="admin"))  # no uid, no login
        await db_session.commit()

        decoded = _make_decoded_token(email=email, uid="new-uid", name=None)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "admin"
        row = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == email)
            )
        ).scalar_one()
        assert row.firebase_uid == "new-uid"
        assert row.last_login_at is not None

    @pytest.mark.asyncio
    async def test_persist_contention_does_not_fail_auth(self, db_session):
        """The core outage fix: a DB error while persisting metadata degrades —
        auth still returns the user/role instead of 500-ing the request."""
        email = "degrade@example.com"
        db_session.add(AllowedUser(email=email, role="admin"))
        await db_session.commit()

        decoded = _make_decoded_token(email=email, uid="uid-3", name=None)
        with (
            patch("app.auth.firebase_auth.verify_id_token", return_value=decoded),
            patch.object(
                db_session,
                "commit",
                side_effect=SQLAlchemyError("simulated lock timeout"),
            ),
        ):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "admin"
        assert result["email"] == email

    @pytest.mark.asyncio
    async def test_best_effort_helper_swallows_db_error_and_rolls_back(self):
        """The helper never propagates a DB error; it rolls back and returns."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock(side_effect=SQLAlchemyError("contention"))
        session.rollback = AsyncMock()
        user = AllowedUser(email="helper@example.com", role="user")

        await _persist_auth_metadata_best_effort(session, user)  # must not raise

        session.commit.assert_awaited_once()
        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_local_lock_timeout_is_a_literal_not_a_bind_param(self):
        """Regression: asyncpg cannot bind a parameter into SET LOCAL (it is not a
        preparable statement), so a bound ``:t`` rendered as ``$1`` 500-flooded
        EVERY authenticated request on Postgres ("syntax error at or near $1").
        The lock_timeout must be inlined as a literal, and only on Postgres."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        user = AllowedUser(email="pg@example.com", role="user")

        with patch("app.auth.resolve_dialect_name", return_value="postgresql"):
            await _persist_auth_metadata_best_effort(session, user)

        # The SET LOCAL statement must have been issued with NO bound parameters
        # and the timeout inlined as a quoted literal.
        set_local_calls = [
            call.args[0]
            for call in session.execute.call_args_list
            if "SET LOCAL" in str(call.args[0])
        ]
        assert set_local_calls, "expected a SET LOCAL lock_timeout on Postgres"
        stmt = set_local_calls[0]
        assert "'1s'" in str(stmt.text)
        assert ":t" not in str(stmt.text) and "$1" not in str(stmt.text)
        # No second positional (the bound-params dict) was passed.
        for call in session.execute.call_args_list:
            assert len(call.args) == 1, "SET LOCAL must carry no bind params"

    @pytest.mark.asyncio
    async def test_set_local_skipped_on_non_postgres_dialect(self):
        """On SQLite (and other test dialects) there is no lock_timeout GUC, so
        the SET LOCAL is skipped entirely — the metadata write still commits."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        user = AllowedUser(email="sqlite@example.com", role="user")

        with patch("app.auth.resolve_dialect_name", return_value="sqlite"):
            await _persist_auth_metadata_best_effort(session, user)

        assert not any(
            "SET LOCAL" in str(call.args[0]) for call in session.execute.call_args_list
        ), "SET LOCAL must not be issued on non-Postgres dialects"
        session.commit.assert_awaited_once()


class TestGetMetricsReader:
    """get_metrics_reader must accept ADMIN_API_KEY and valid agent JWTs, reject all else.

    The dependency is intentionally narrow: it grants read-only metric access
    to agent JWTs so research VMs can probe non-mutating endpoints without
    holding an ADMIN_API_KEY.
    """

    @pytest.mark.asyncio
    async def test_admin_api_key_accepted(self, monkeypatch):
        """ADMIN_API_KEY path returns role=admin."""
        from app.auth import get_metrics_reader

        monkeypatch.setenv("ADMIN_API_KEY", "test-metrics-key-abc")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="test-metrics-key-abc"
        )
        result = await get_metrics_reader(credentials=creds)
        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_task_scoped_agent_jwt_accepted(self):
        """A valid task-scoped agent JWT returns role=metrics_reader."""
        from app.auth import create_agent_token, get_metrics_reader

        token = create_agent_token(
            task_id="task-abc-123",
            vm_instance_id="vm-def-456",
            task_type="research",
            project_id="proj-000",
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds)
        assert result["role"] == "metrics_reader"
        assert result["sub"] == "task-abc-123"
        assert result["vm_instance_id"] == "vm-def-456"

    @pytest.mark.asyncio
    async def test_vm_scoped_agent_jwt_accepted(self):
        """A valid VM-scoped (taskless) agent JWT returns role=metrics_reader."""
        from app.auth import create_vm_scoped_token, get_metrics_reader

        token = create_vm_scoped_token(
            vm_instance_id="pool-vm-001", project_id="proj-111"
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds)
        assert result["role"] == "metrics_reader"
        assert result["vm_instance_id"] == "pool-vm-001"

    @pytest.mark.asyncio
    async def test_expired_jwt_rejected(self):
        """An expired agent JWT raises 401."""
        import jwt as pyjwt
        from datetime import datetime, timezone
        from fastapi import HTTPException
        from app.auth import (
            AGENT_JWT_ALGORITHM,
            AGENT_JWT_SECRET,
            get_metrics_reader,
        )

        expired_payload = {
            "sub": "old-task",
            "vm_instance_id": "old-vm",
            "exp": datetime.now(timezone.utc) - timedelta(hours=2),
            "iat": datetime.now(timezone.utc) - timedelta(hours=3),
        }
        expired_token = pyjwt.encode(
            expired_payload, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)

        with pytest.raises(HTTPException) as exc_info:
            await get_metrics_reader(credentials=creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self):
        """A garbage/invalid token (not ADMIN_API_KEY, not a valid JWT) raises 401."""
        from fastapi import HTTPException
        from app.auth import get_metrics_reader

        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="garbage-not-a-jwt"
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_metrics_reader(credentials=creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_metrics_reader_accepts_agent_jwt(self):
        """Acceptance test: freshly minted agent JWT is accepted by get_metrics_reader.

        This is the named acceptance gate for the METRICS_READER_ENDPOINTS feature.
        """
        from app.auth import create_agent_token, get_metrics_reader

        token = create_agent_token(
            task_id="acceptance-task-001",
            vm_instance_id="acceptance-vm-001",
            task_type="research",
            project_id="00000000-0000-0000-0000-000000000001",
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds)
        assert result["role"] == "metrics_reader"
        assert result["sub"] == "acceptance-task-001"
        assert result["vm_instance_id"] == "acceptance-vm-001"
