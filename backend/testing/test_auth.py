"""Tests for backend/app/auth.py — email-verification enforcement."""

import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import AllowedUser
from app.auth import get_current_user


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
