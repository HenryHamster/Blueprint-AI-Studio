"""Tests for backend/app/auth.py — email-verification enforcement."""

import asyncio
import os

import pytest
from datetime import timedelta
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.main import app
from app.models import AllowedUser, Project, ProjectMember, Task, _utcnow
from app.admin_helpers import pending_project_member_user_id
from app.auth import (
    get_current_user,
    _persist_first_signin_claim_critical,
    _persist_auth_metadata_best_effort,
    _LAST_LOGIN_UPDATE_INTERVAL,
)


@pytest.fixture(autouse=True)
def _undo_auth_module_reload():
    """Undo the ``importlib.reload(app.auth)`` calls this module performs.

    Tests here reload ``app.auth`` to re-evaluate its module-level env reads
    (``ADMIN_JWT_SECRET`` and friends) under a monkeypatched environment. The
    reload re-executes the module *in place*, which leaks two ways once the
    test finishes:

    1. Every top-level name is rebound to a **new** object. Routes registered
       on ``app.main.app`` still hold the originals, so identity comparisons
       against ``app.auth.require_admin`` / ``get_current_user`` /
       ``get_metrics_reader`` stop matching -- ``test_invariants`` then reports
       that admin-surface routes have no recognized auth dependency.
    2. Env-derived module constants keep the *test's* values (the last reload
       leaves ``_CRANKED_admin_jwt_secret`` holding a throwaway secret), so
       later admin-authenticated requests in other files fail with 401.

    Snapshotting and restoring ``__dict__`` reverses the reload completely.
    """
    from app import auth as auth_mod

    original = dict(auth_mod.__dict__)
    yield
    auth_mod.__dict__.clear()
    auth_mod.__dict__.update(original)


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
    async def test_first_signin_claims_pending_and_merges_duplicate_strongest_role(
        self, db_session
    ):
        email = "invited@example.com"
        allowed = AllowedUser(email=email, role="invited")
        claimed_project = Project(name="Pending only", user_id="other-owner")
        duplicate_project = Project(name="Pending and real", user_id="other-owner")
        db_session.add_all([allowed, claimed_project, duplicate_project])
        await db_session.commit()
        pending_id = pending_project_member_user_id(allowed.id)
        claimed = ProjectMember(
            project_id=claimed_project.id, user_id=pending_id, role="member"
        )
        pending_duplicate = ProjectMember(
            project_id=duplicate_project.id, user_id=pending_id, role="owner"
        )
        real_duplicate = ProjectMember(
            project_id=duplicate_project.id, user_id="claimed-uid", role="viewer"
        )
        db_session.add_all([claimed, pending_duplicate, real_duplicate])
        await db_session.commit()
        claimed_member_id = claimed.id
        real_member_id = real_duplicate.id

        decoded = _make_decoded_token(email=email, uid="claimed-uid", name=None)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["user_id"] == "claimed-uid"
        assert result["role"] == "user"
        await db_session.refresh(allowed)
        assert allowed.firebase_uid == "claimed-uid"
        assert allowed.role == "user"
        claimed_rows = (
            await db_session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == claimed_project.id
                )
            )
        ).scalars().all()
        assert len(claimed_rows) == 1
        assert claimed_rows[0].id == claimed_member_id
        assert claimed_rows[0].user_id == "claimed-uid"
        assert claimed_rows[0].role == "member"
        merged_rows = (
            await db_session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == duplicate_project.id
                )
            )
        ).scalars().all()
        assert len(merged_rows) == 1
        assert merged_rows[0].id == real_member_id
        assert merged_rows[0].user_id == "claimed-uid"
        assert merged_rows[0].role == "owner"

    @pytest.mark.asyncio
    async def test_persist_contention_does_not_fail_auth(self, db_session):
        """The core outage fix: a DB error while persisting metadata degrades —
        auth still returns the user/role instead of 500-ing the request."""
        email = "degrade@example.com"
        db_session.add(
            AllowedUser(email=email, firebase_uid="uid-3", role="admin")
        )
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
    async def test_invited_claim_commit_failure_returns_503_and_rolls_back_access(
        self, db_session
    ):
        email = "degraded-invite@example.com"
        allowed = AllowedUser(email=email, role="invited")
        project = Project(name="Critical claim", user_id="other-owner")
        db_session.add_all([allowed, project])
        await db_session.commit()
        pending_id = pending_project_member_user_id(allowed.id)
        pending = ProjectMember(
            project_id=project.id, user_id=pending_id, role="member"
        )
        db_session.add(pending)
        await db_session.commit()
        allowed_id = allowed.id
        pending_member_id = pending.id

        decoded = _make_decoded_token(email=email, uid="degraded-invite-uid", name=None)
        with (
            patch("app.auth.firebase_auth.verify_id_token", return_value=decoded),
            patch.object(
                db_session,
                "commit",
                side_effect=SQLAlchemyError("simulated lock timeout"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await get_current_user(credentials=_creds(), session=db_session)

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers == {"Retry-After": "1"}
        # The default SQLite harness disables ORM expiry suite-wide, so
        # ``expire_all`` could leave the pre-rollback identity-map objects in
        # place and make this assertion vacuous. Detach them and force SELECTs.
        db_session.expunge_all()
        persisted = await db_session.get(AllowedUser, allowed_id)
        persisted_member = await db_session.get(ProjectMember, pending_member_id)
        assert persisted is not None
        assert persisted.firebase_uid is None
        assert persisted.role == "invited"
        assert persisted_member is not None
        assert persisted_member.user_id == pending_id

    @pytest.mark.asyncio
    async def test_claim_commit_failure_never_reaches_protected_route(
        self, client, db_session
    ):
        email = "route-claim-failure@example.com"
        allowed = AllowedUser(email=email, role="admin")
        db_session.add(allowed)
        await db_session.commit()
        saved = app.dependency_overrides.pop(get_current_user, None)
        decoded = _make_decoded_token(email=email, uid="route-claim-uid", name=None)
        try:
            with (
                patch("app.auth.firebase_auth.verify_id_token", return_value=decoded),
                patch(
                    "sqlalchemy.ext.asyncio.AsyncSession.commit",
                    new_callable=AsyncMock,
                    side_effect=SQLAlchemyError("simulated critical commit failure"),
                ),
            ):
                response = await client.get(
                    "/api/admin/users",
                    headers={"Authorization": "Bearer fake-firebase-token"},
                )
        finally:
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
        reason="concurrent UID claim serialization requires PostgreSQL",
    )
    async def test_concurrent_different_uid_claim_loser_is_denied_for_admin_row(
        self, db_session
    ):
        from conftest import TestingSessionLocal

        allowed = AllowedUser(email="admin-race@example.com", role="admin")
        db_session.add(allowed)
        await db_session.commit()
        allowed_id = allowed.id

        async def claim(uid: str):
            async with TestingSessionLocal() as session:
                try:
                    result = await _persist_first_signin_claim_critical(
                        session, allowed_id, uid
                    )
                    return ("ok", uid, result.role)
                except HTTPException as exc:
                    return ("denied", uid, exc.status_code)

        results = await asyncio.gather(claim("race-uid-a"), claim("race-uid-b"))

        assert sorted(result[0] for result in results) == ["denied", "ok"]
        winner = next(result for result in results if result[0] == "ok")
        loser = next(result for result in results if result[0] == "denied")
        assert winner[2] == "admin"
        assert loser[2] == 403
        db_session.expire_all()
        persisted = await db_session.get(AllowedUser, allowed_id)
        assert persisted is not None
        assert persisted.firebase_uid == winner[1]
        assert persisted.role == "admin"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
        reason="concurrent UID claim serialization requires PostgreSQL",
    )
    async def test_concurrent_same_uid_claim_is_idempotent(self, db_session):
        from conftest import TestingSessionLocal

        allowed = AllowedUser(email="same-uid-race@example.com", role="invited")
        db_session.add(allowed)
        await db_session.commit()
        allowed_id = allowed.id

        async def claim():
            async with TestingSessionLocal() as session:
                result = await _persist_first_signin_claim_critical(
                    session, allowed_id, "same-race-uid"
                )
                return result.role

        assert await asyncio.gather(claim(), claim()) == ["user", "user"]
        db_session.expire_all()
        persisted = await db_session.get(AllowedUser, allowed_id)
        assert persisted is not None
        assert persisted.firebase_uid == "same-race-uid"
        assert persisted.role == "user"

    @pytest.mark.asyncio
    async def test_case_variant_rows_prefer_current_uid_binding(self, db_session):
        db_session.add_all(
            [
                AllowedUser(
                    email="Bound@Example.com",
                    firebase_uid="current-uid",
                    role="admin",
                ),
                AllowedUser(
                    email="bound@example.com",
                    firebase_uid="other-uid",
                    role="user",
                ),
            ]
        )
        await db_session.commit()

        decoded = _make_decoded_token(
            email="BOUND@example.com", uid="current-uid", name=None
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_case_variant_distinct_bindings_fail_closed(self, db_session):
        db_session.add_all(
            [
                AllowedUser(
                    email="Ambiguous@Example.com",
                    firebase_uid="first-uid",
                    role="admin",
                ),
                AllowedUser(
                    email="ambiguous@example.com",
                    firebase_uid="second-uid",
                    role="user",
                ),
            ]
        )
        await db_session.commit()

        decoded = _make_decoded_token(
            email="AMBIGUOUS@example.com", uid="unrelated-uid", name=None
        )
        with (
            patch("app.auth.firebase_auth.verify_id_token", return_value=decoded),
            pytest.raises(HTTPException) as exc_info,
        ):
            await get_current_user(credentials=_creds(), session=db_session)

        assert exc_info.value.status_code == 409
        assert "Conflicting allowlist" in exc_info.value.detail

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
    async def test_task_scoped_agent_jwt_accepted(self, db_session):
        """A valid task-scoped agent JWT returns role=metrics_reader."""
        from app.auth import create_agent_token, get_metrics_reader

        project = Project(name="Metrics task scope", user_id="metrics-owner")
        db_session.add(project)
        await db_session.flush()
        task = Task(
            project_id=project.id,
            vm_instance_id="vm-def-456",
            payloads=[{"type": "research"}],
        )
        db_session.add(task)
        await db_session.commit()
        token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id=task.vm_instance_id,
            task_type="research",
            project_id=str(project.id),
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds, session=db_session)
        assert result["role"] == "metrics_reader"
        assert result["sub"] == str(task.id)
        assert result["vm_instance_id"] == "vm-def-456"

    @pytest.mark.asyncio
    async def test_vm_scoped_agent_jwt_rejected(self, db_session):
        """A VM-scoped JWT has no persisted task authority for metrics."""
        from app.auth import create_vm_scoped_token, get_metrics_reader

        token = create_vm_scoped_token(
            vm_instance_id="pool-vm-001", project_id="proj-111"
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as rejected:
            await get_metrics_reader(credentials=creds, session=db_session)
        assert rejected.value.status_code == 403

    @pytest.mark.asyncio
    async def test_tool_asset_scoped_jwt_rejected(self):
        """A tool-asset-scoped JWT is rejected with 403 (out of scope).

        Regression for finding bbd2610e22e1779b: tool-asset tokens carry a
        ``scope`` claim that was NOT enforced, so a token minted for tool-asset
        access could authenticate to the metrics reader.
        """
        from fastapi import HTTPException
        from app.auth import get_metrics_reader
        from app.routers.admin_project_tools import _create_tool_asset_token

        token = _create_tool_asset_token("proj-222", "some-tool")
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with pytest.raises(HTTPException) as exc_info:
            await get_metrics_reader(credentials=creds)
        assert exc_info.value.status_code == 403

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
    async def test_metrics_reader_accepts_agent_jwt(self, db_session):
        """Acceptance test: freshly minted agent JWT is accepted by get_metrics_reader.

        This is the named acceptance gate for the METRICS_READER_ENDPOINTS feature.
        """
        from app.auth import create_agent_token, get_metrics_reader

        project = Project(name="Metrics acceptance", user_id="metrics-owner")
        db_session.add(project)
        await db_session.flush()
        task = Task(
            project_id=project.id,
            vm_instance_id="acceptance-vm-001",
            payloads=[{"type": "research"}],
        )
        db_session.add(task)
        await db_session.commit()
        token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id="acceptance-vm-001",
            task_type="research",
            project_id=str(project.id),
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds, session=db_session)
        assert result["role"] == "metrics_reader"
        assert result["sub"] == str(task.id)
        assert result["vm_instance_id"] == "acceptance-vm-001"


class TestValidateJwtSecret:
    def test_32_byte_secret_passes(self):
        from app.auth import _validate_jwt_secret

        _validate_jwt_secret("a" * 32, "TEST_SECRET")  # must not raise

    def test_exactly_32_bytes_passes(self):
        from app.auth import _validate_jwt_secret

        _validate_jwt_secret("x" * 32, "TEST_SECRET")

    def test_short_secret_raises_value_error(self):
        from app.auth import _validate_jwt_secret

        with pytest.raises(ValueError, match="JWT secret must be at least 32 bytes"):
            _validate_jwt_secret("short", "AGENT_JWT_SECRET")

    def test_31_byte_secret_raises(self):
        from app.auth import _validate_jwt_secret

        with pytest.raises(ValueError, match="JWT secret must be at least 32 bytes"):
            _validate_jwt_secret("a" * 31, "AGENT_JWT_SECRET")

    def test_multibyte_chars_counted_by_bytes_not_chars(self):
        from app.auth import _validate_jwt_secret

        # 16 × 2-byte chars = 32 bytes — must pass
        _validate_jwt_secret("é" * 16, "TEST_SECRET")

    def test_error_message_includes_env_var_name(self):
        from app.auth import _validate_jwt_secret

        with pytest.raises(ValueError, match="WS_TICKET_SECRET"):
            _validate_jwt_secret("tooshort", "WS_TICKET_SECRET")


class TestAdminJwt:
    """create_admin_token / verify_admin_token for the short-lived admin JWT."""

    def test_requires_admin_jwt_secret(self, monkeypatch):
        """create_admin_token raises ValueError when ADMIN_JWT_SECRET is not configured."""
        monkeypatch.delenv("ADMIN_JWT_SECRET", raising=False)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        with pytest.raises(ValueError, match="ADMIN_JWT_SECRET"):
            auth_mod.create_admin_token(user_id="any")

    def test_create_and_verify_token(self, monkeypatch):
        """Round-trip: a token minted by create_admin_token is accepted by verify_admin_token."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "a" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)

        token = auth_mod.create_admin_token(user_id="admin-uid-001")
        payload = auth_mod.verify_admin_token(token)
        assert payload is not None
        assert payload["sub"] == "admin-uid-001"
        assert payload["role"] == "admin"
        assert payload["token_type"] == "admin_api_exchange"

    def test_verify_admin_token_none_when_secret_not_set(self):
        """verify_admin_token returns None when ADMIN_JWT_SECRET is not configured."""
        from app.auth import verify_admin_token

        assert verify_admin_token("some-token") is None

    def test_verify_admin_token_rejects_expired(self, monkeypatch):
        """An expired admin JWT returns None."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "b" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        import jwt as pyjwt
        from datetime import datetime, timezone, timedelta

        expired = pyjwt.encode(
            {
                "sub": "old",
                "role": "admin",
                "token_type": "admin_api_exchange",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            },
            auth_mod.ADMIN_JWT_SECRET,
            algorithm=auth_mod.ADMIN_JWT_ALGORITHM,
        )
        assert auth_mod.verify_admin_token(expired) is None

    def test_verify_admin_token_rejects_wrong_token_type(self, monkeypatch):
        """A JWT with token_type != admin_api_exchange is rejected."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "c" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        import jwt as pyjwt
        from datetime import datetime, timezone, timedelta

        not_admin = pyjwt.encode(
            {
                "sub": "user-1",
                "role": "admin",
                "token_type": "agent",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "iat": datetime.now(timezone.utc),
            },
            auth_mod.ADMIN_JWT_SECRET,
            algorithm=auth_mod.ADMIN_JWT_ALGORITHM,
        )
        assert auth_mod.verify_admin_token(not_admin) is None

    def test_verify_admin_token_rejects_wrong_role(self, monkeypatch):
        """A JWT with role != admin is rejected."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "d" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        import jwt as pyjwt
        from datetime import datetime, timezone, timedelta

        non_admin = pyjwt.encode(
            {
                "sub": "user-1",
                "role": "user",
                "token_type": "admin_api_exchange",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "iat": datetime.now(timezone.utc),
            },
            auth_mod.ADMIN_JWT_SECRET,
            algorithm=auth_mod.ADMIN_JWT_ALGORITHM,
        )
        assert auth_mod.verify_admin_token(non_admin) is None

    @pytest.mark.asyncio
    async def test_admin_jwt_works_in_get_current_user(self, override_get_session, db_session, monkeypatch):
        """An admin JWT is accepted by get_current_user the same as ADMIN_API_KEY."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "e" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        from app.auth import get_current_user

        token = auth_mod.create_admin_token(user_id="admin-uid-002")
        creds = _creds(token)
        result = await get_current_user(credentials=creds, session=db_session)
        assert result["role"] == "admin"
        assert result["user_id"] == "admin-uid-002"

    @pytest.mark.asyncio
    async def test_admin_jwt_works_in_get_metrics_reader(self, db_session, monkeypatch):
        """An admin JWT is accepted by get_metrics_reader."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "f" * 32)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        from app.auth import get_metrics_reader
        from fastapi.security import HTTPAuthorizationCredentials

        token = auth_mod.create_admin_token(user_id="admin-metrics-001")
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds)
        assert result["role"] == "admin"


class TestAdminApiKeyIpAllowlist:
    """Source IP allowlist for ADMIN_API_KEY."""

    def test_parse_empty_returns_empty_list(self, monkeypatch):
        """_parse_admin_ip_allowlist returns [] when env var is unset."""
        monkeypatch.delenv("ADMIN_API_KEY_ALLOWED_IPS", raising=False)
        from app.auth import _parse_admin_ip_allowlist

        assert _parse_admin_ip_allowlist() == []

    def test_parse_single_cidr(self, monkeypatch):
        """A single CIDR entry is parsed correctly."""
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "10.0.0.0/8")
        from app.auth import _parse_admin_ip_allowlist

        nets = _parse_admin_ip_allowlist()
        assert len(nets) == 1
        assert nets[0].compressed == "10.0.0.0/8"

    def test_parse_multiple_cidrs(self, monkeypatch):
        """Multiple comma-separated CIDRs are all parsed."""
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "192.168.0.0/16,10.0.0.0/8")
        from app.auth import _parse_admin_ip_allowlist

        nets = _parse_admin_ip_allowlist()
        assert len(nets) == 2

    def test_check_source_ip_no_allowlist_returns_true(self):
        """_check_admin_api_key_source_ip returns True when no allowlist configured."""
        from app.auth import _check_admin_api_key_source_ip

        assert _check_admin_api_key_source_ip(request=None) is True

    def test_check_source_ip_allowed(self, monkeypatch):
        """An IP within the allowlist passes."""
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "10.0.0.0/8")
        from app.auth import _check_admin_api_key_source_ip

        mock_request = MagicMock()
        mock_request.client.host = "10.1.2.3"
        assert _check_admin_api_key_source_ip(mock_request) is True

    def test_check_source_ip_blocked(self, monkeypatch):
        """An IP outside the allowlist is blocked."""
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "10.0.0.0/8")
        from app.auth import _check_admin_api_key_source_ip

        mock_request = MagicMock()
        mock_request.client.host = "192.168.1.1"
        assert _check_admin_api_key_source_ip(mock_request) is False

    def test_check_source_ip_none_request_returns_false_when_allowlist_set(self, monkeypatch):
        """When allowlist is configured but request is None, access is denied."""
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "10.0.0.0/8")
        from app.auth import _check_admin_api_key_source_ip

        assert _check_admin_api_key_source_ip(request=None) is False


class TestAdminApiKeyAuditLogging:
    """Structured audit logging for ADMIN_API_KEY usage."""

    def test_audit_log_emits_warning(self):
        """_audit_log_admin_api_key emits a WARNING-level log with audit_event field."""
        from app.auth import _audit_log_admin_api_key
        from unittest.mock import MagicMock, patch

        mock_request = MagicMock()
        mock_request.client.host = "10.0.0.1"
        mock_request.headers.get.return_value = "test-agent"

        with patch("app.auth.logger.warning") as mock_warning:
            _audit_log_admin_api_key(mock_request, action="direct_use")

        mock_warning.assert_called_once()
        args, kwargs = mock_warning.call_args
        assert "ADMIN_API_KEY" in args[0]
        extra = kwargs.get("extra", {})
        assert extra.get("audit_event") == "admin_api_key_usage"
        assert extra.get("action") == "direct_use"
        assert extra.get("source_ip") == "10.0.0.1"

    def test_audit_log_without_request(self):
        """_audit_log_admin_api_key handles missing request gracefully."""
        from app.auth import _audit_log_admin_api_key
        from unittest.mock import patch

        with patch("app.auth.logger.warning") as mock_warning:
            _audit_log_admin_api_key(None, action="token_exchange")

        mock_warning.assert_called_once()
        args, kwargs = mock_warning.call_args
        extra = kwargs.get("extra", {})
        assert extra.get("source_ip") == "unknown"


class TestAdminTokenExchangeEndpoint:
    """Integration-style tests for POST /api/admin/token."""

    @pytest.mark.asyncio
    async def test_returns_501_when_jwt_secret_not_set(self, override_get_session, monkeypatch):
        """Without ADMIN_JWT_SECRET, the endpoint returns 501."""
        monkeypatch.delenv("ADMIN_JWT_SECRET", raising=False)
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        from app.main import app
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/token",
                headers={"Authorization": "Bearer any-key"},
            )
        assert resp.status_code == 501

    @pytest.mark.asyncio
    async def test_returns_401_for_wrong_key(self, override_get_session, monkeypatch):
        """A wrong ADMIN_API_KEY returns 401."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "g" * 32)
        monkeypatch.setenv("ADMIN_API_KEY", "real-key-12345")
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)

        from app.main import app
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/token",
                headers={"Authorization": "Bearer wrong-key"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_200_with_valid_key(self, override_get_session, db_session, monkeypatch):
        """A valid ADMIN_API_KEY + ADMIN_JWT_SECRET returns a JWT."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "h" * 32)
        monkeypatch.setenv("ADMIN_API_KEY", "valid-key-999")
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        from app.main import app
        from httpx import AsyncClient, ASGITransport

        # Seed an admin user so the UID resolution succeeds
        admin_user = AllowedUser(
            email="admin@test.local", role="admin", firebase_uid="firebase-admin-001"
        )
        db_session.add(admin_user)
        await db_session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/token",
                headers={"Authorization": "Bearer valid-key-999"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 3600  # default
        # Verify the returned JWT works for subsequent auth
        jwt_token = body["access_token"]
        creds = _creds(jwt_token)
        result = await auth_mod.get_current_user(credentials=creds, session=db_session)
        assert result["role"] == "admin"
        assert result["user_id"] == "firebase-admin-001"

    @pytest.mark.asyncio
    async def test_returns_403_from_disallowed_ip(self, override_get_session, monkeypatch):
        """When ADMIN_API_KEY_ALLOWED_IPS is set and request IP is outside, returns 403."""
        monkeypatch.setenv("ADMIN_JWT_SECRET", "i" * 32)
        monkeypatch.setenv("ADMIN_API_KEY", "ip-restricted-key")
        monkeypatch.setenv("ADMIN_API_KEY_ALLOWED_IPS", "10.0.0.0/8")
        import importlib
        from app import auth as auth_mod

        importlib.reload(auth_mod)
        from app.main import app
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/admin/token",
                headers={"Authorization": "Bearer ip-restricted-key"},
            )
        # The test client runs on 127.0.0.1 by default, which is outside 10.0.0.0/8
        assert resp.status_code == 403
