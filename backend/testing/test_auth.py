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
