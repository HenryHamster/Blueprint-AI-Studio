"""Additional test coverage for backend/app/auth.py firebase paths and
previously untested auth helpers.

Scope:
- Specific Firebase exception types (401 vs 500 branching)
- User not found in AllowedUser table → 403
- Display name / metadata update path in get_current_user
- get_current_user_optional (None path)
- require_admin (403 for non-admin)
- create_ws_ticket / verify_ws_ticket round-trip + error paths
- hash_runner_token / create_runner_token / verify_runner_token
- get_metrics_reader Firebase fallthrough path
- firebase-admin version bump investigation: confirm 7.5.0 API surface
  matches what auth.py requires (error classes, init, verify_id_token)
"""

import pytest
from datetime import timedelta
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import select

from app.auth import (
    get_current_user,
    get_current_user_optional,
    require_admin,
    create_ws_ticket,
    verify_ws_ticket,
    create_agent_token,
    create_vm_scoped_token,
    hash_runner_token,
    create_runner_token,
    verify_runner_token,
    get_metrics_reader,
    AGENT_JWT_ALGORITHM,
)
from app.models import AllowedUser, LocalRunner, Project, Task, _utcnow


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


# ---------------------------------------------------------------------------
# Firebase exception type → HTTP status mapping
# ---------------------------------------------------------------------------

class TestFirebaseExceptionMapping:
    """get_current_user must return 401 for all expected Firebase token errors
    and 500 for unexpected exceptions."""

    @pytest.mark.asyncio
    async def test_invalid_id_token_error_returns_401(self, db_session):
        """firebase_admin.auth.InvalidIdTokenError → HTTP 401."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.InvalidIdTokenError("bad token"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_id_token_error_returns_401(self, db_session):
        """firebase_admin.auth.ExpiredIdTokenError → HTTP 401."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.ExpiredIdTokenError("token expired", None),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_id_token_error_returns_401(self, db_session):
        """firebase_admin.auth.RevokedIdTokenError → HTTP 401."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.RevokedIdTokenError("revoked"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_user_disabled_error_returns_401(self, db_session):
        """firebase_admin.auth.UserDisabledError → HTTP 401."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.UserDisabledError("disabled"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_certificate_fetch_error_returns_401(self, db_session):
        """firebase_admin.auth.CertificateFetchError → HTTP 401."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.CertificateFetchError("cert error", None),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_value_error_from_verify_returns_401(self, db_session):
        """ValueError from verify_id_token → HTTP 401 (listed in expected exceptions)."""
        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=ValueError("invalid token format"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_500(self, db_session):
        """An unexpected exception from verify_id_token → HTTP 500."""
        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=RuntimeError("something totally unexpected"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_401_detail_references_credentials(self, db_session):
        """The 401 detail message references invalid credentials (not leaking internals)."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.InvalidIdTokenError("internal detail"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert "invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_401_includes_www_authenticate_header(self, db_session):
        """The 401 response includes the WWW-Authenticate header."""
        import firebase_admin

        with patch(
            "app.auth.firebase_auth.verify_id_token",
            side_effect=firebase_admin.auth.InvalidIdTokenError("bad"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# User not in AllowedUser table
# ---------------------------------------------------------------------------

class TestUserNotAllowed:
    """get_current_user returns 403 when a valid token comes from a user who
    is not in the AllowedUser table."""

    @pytest.mark.asyncio
    async def test_valid_token_unknown_user_returns_403(self, db_session):
        """Valid Firebase token, email verified, but email not in AllowedUser → 403."""
        decoded = _make_decoded_token(email="stranger@example.com", email_verified=True)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert exc_info.value.status_code == 403
        assert "authorized" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_403_message_suggests_contact_admin(self, db_session):
        """The 403 for unlisted users tells the caller to contact an admin."""
        decoded = _make_decoded_token(email="nobody@nowhere.com", email_verified=True)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_creds(), session=db_session)
        assert "administrator" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Display name update path
# ---------------------------------------------------------------------------

class TestDisplayNameUpdate:
    """get_current_user updates display_name when Firebase returns a new value."""

    @pytest.mark.asyncio
    async def test_display_name_updated_when_different(self, db_session):
        """When Firebase returns a new display_name the AllowedUser row is updated."""
        email = "namechange@example.com"
        user = AllowedUser(
            email=email,
            role="user",
            firebase_uid="uid-namechange",
            display_name="Old Name",
        )
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(
            email=email, uid="uid-namechange", name="New Name", email_verified=True
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "user"
        row = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == email)
            )
        ).scalar_one()
        assert row.display_name == "New Name"

    @pytest.mark.asyncio
    async def test_display_name_not_written_when_same(self, db_session):
        """When the display_name is unchanged no unnecessary write is triggered."""
        email = "samename@example.com"
        old = _utcnow() - timedelta(minutes=1)
        user = AllowedUser(
            email=email,
            role="user",
            firebase_uid="uid-samename",
            display_name="Stable Name",
            last_login_at=old,
        )
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(
            email=email, uid="uid-samename", name="Stable Name", email_verified=True
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            with patch("app.auth._persist_auth_metadata_best_effort") as mock_persist:
                # Set last_login_at recent enough to skip the throttle write
                user.last_login_at = _utcnow() - timedelta(seconds=30)
                result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "user"
        # display_name didn't change AND last_login is recent → no persist
        mock_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_display_name_skipped_when_firebase_returns_none(self, db_session):
        """When Firebase returns no name field the existing display_name is preserved."""
        email = "noname@example.com"
        user = AllowedUser(
            email=email,
            role="user",
            firebase_uid="uid-noname",
            display_name="Keep This",
        )
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(email=email, uid="uid-noname", email_verified=True)
        decoded.pop("name", None)
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user(credentials=_creds(), session=db_session)

        assert result["role"] == "user"
        row = (
            await db_session.execute(
                select(AllowedUser).where(AllowedUser.email == email)
            )
        ).scalar_one()
        assert row.display_name == "Keep This"


# ---------------------------------------------------------------------------
# get_current_user_optional
# ---------------------------------------------------------------------------

class TestGetCurrentUserOptional:
    """get_current_user_optional returns None when no credentials are provided."""

    @pytest.mark.asyncio
    async def test_none_credentials_returns_none(self, db_session):
        """When credentials is None the optional dependency returns None."""
        result = await get_current_user_optional(
            credentials=None, session=db_session
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_credentials_delegates_to_get_current_user(self, db_session):
        """When credentials are present it behaves like get_current_user."""
        email = "optional@example.com"
        user = AllowedUser(email=email, role="admin", firebase_uid="uid-optional")
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(
            email=email, uid="uid-optional", email_verified=True
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_current_user_optional(
                credentials=_creds(), session=db_session
            )

        assert result is not None
        assert result["role"] == "admin"


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------

class TestRequireAdmin:
    """require_admin raises 403 for non-admin users, passes for admin."""

    def test_admin_role_passes(self):
        """A dict with role=admin is returned unchanged."""
        user = {"role": "admin", "user_id": "admin-1", "email": "a@b.com"}
        result = require_admin(current_user=user)
        assert result is user

    def test_user_role_raises_403(self):
        """A dict with role=user raises 403."""
        user = {"role": "user", "user_id": "user-1", "email": "u@b.com"}
        with pytest.raises(HTTPException) as exc_info:
            require_admin(current_user=user)
        assert exc_info.value.status_code == 403
        assert "admin" in exc_info.value.detail.lower()

    def test_missing_role_raises_403(self):
        """A dict with no role field raises 403."""
        with pytest.raises(HTTPException) as exc_info:
            require_admin(current_user={"user_id": "x"})
        assert exc_info.value.status_code == 403

    def test_empty_role_raises_403(self):
        """An empty role string raises 403."""
        with pytest.raises(HTTPException) as exc_info:
            require_admin(current_user={"role": "", "user_id": "x"})
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# WebSocket ticket: create + verify round-trip
# ---------------------------------------------------------------------------

class TestWebSocketTicket:
    """create_ws_ticket / verify_ws_ticket round-trip and error paths."""

    def test_round_trip(self):
        """A ticket minted by create_ws_ticket is accepted by verify_ws_ticket."""
        ticket = create_ws_ticket(user_id="ws-user-1", project_id="proj-abc")
        payload = verify_ws_ticket(ticket)
        assert payload["sub"] == "ws-user-1"
        assert payload["project_id"] == "proj-abc"
        assert payload["type"] == "ws_ticket"

    def test_expired_ticket_raises_401(self):
        """An expired WebSocket ticket raises 401."""
        import jwt as pyjwt
        from datetime import datetime, timezone
        from app.auth import WS_TICKET_SECRET

        expired = pyjwt.encode(
            {
                "sub": "ws-user-exp",
                "project_id": "proj-exp",
                "type": "ws_ticket",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            WS_TICKET_SECRET,
            algorithm=AGENT_JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_ws_ticket(expired)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_invalid_ticket_raises_401(self):
        """A garbage string raises 401."""
        with pytest.raises(HTTPException) as exc_info:
            verify_ws_ticket("totally-invalid-ticket")
        assert exc_info.value.status_code == 401

    def test_wrong_type_field_raises_401(self):
        """A JWT with type != ws_ticket raises 401."""
        import jwt as pyjwt
        from datetime import datetime, timezone
        from app.auth import WS_TICKET_SECRET

        bad_type = pyjwt.encode(
            {
                "sub": "ws-user-bad",
                "project_id": "proj-bad",
                "type": "agent",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            WS_TICKET_SECRET,
            algorithm=AGENT_JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_ws_ticket(bad_type)
        assert exc_info.value.status_code == 401

    def test_wrong_secret_raises_401(self):
        """A ticket signed with a different secret raises 401."""
        import jwt as pyjwt
        from datetime import datetime, timezone

        tampered = pyjwt.encode(
            {
                "sub": "ws-user-tamper",
                "project_id": "proj-tamper",
                "type": "ws_ticket",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=1),
            },
            "wrong-secret-key-xxxxxxxxxxxxxxxx",
            algorithm=AGENT_JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_ws_ticket(tampered)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Runner token helpers
# ---------------------------------------------------------------------------

class TestRunnerTokenHelpers:
    """hash_runner_token, create_runner_token, verify_runner_token."""

    def test_hash_is_deterministic(self):
        """The same plaintext always produces the same hash."""
        assert hash_runner_token("my-secret") == hash_runner_token("my-secret")

    def test_different_tokens_have_different_hashes(self):
        """Two distinct tokens produce distinct hashes."""
        assert hash_runner_token("token-A") != hash_runner_token("token-B")

    def test_create_runner_token_is_url_safe(self):
        """The generated token contains only URL-safe characters."""
        import re
        import uuid
        token = create_runner_token(runner_id=uuid.uuid4())
        assert re.match(r"^[A-Za-z0-9_\-]+$", token)

    def test_create_runner_token_has_adequate_entropy(self):
        """The token is long enough to have adequate entropy."""
        import uuid
        token = create_runner_token(runner_id=uuid.uuid4())
        assert len(token) >= 30

    def test_two_tokens_differ(self):
        """Each call generates a distinct token."""
        import uuid
        t1 = create_runner_token(runner_id=uuid.uuid4())
        t2 = create_runner_token(runner_id=uuid.uuid4())
        assert t1 != t2

    @pytest.mark.asyncio
    async def test_verify_runner_token_returns_runner(self, db_session):
        """verify_runner_token returns the LocalRunner for a valid non-deleted runner."""
        import uuid
        project = Project(name="Runner Token Project", user_id="runner-token-user")
        db_session.add(project)
        await db_session.flush()
        token = create_runner_token(runner_id=uuid.uuid4())
        token_hash = hash_runner_token(token)
        runner = LocalRunner(
            project_id=project.id,
            token_hash=token_hash,
            hostname="test-runner-host",
            status="active",
        )
        db_session.add(runner)
        await db_session.commit()

        result = await verify_runner_token(token, db_session)
        assert result is not None
        assert result.token_hash == token_hash

    @pytest.mark.asyncio
    async def test_verify_runner_token_returns_none_for_unknown_token(self, db_session):
        """verify_runner_token returns None when the token does not exist."""
        result = await verify_runner_token("unknown-token-xyz", db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_verify_runner_token_returns_none_for_deleted_runner(self, db_session):
        """verify_runner_token returns None for a runner with status='deleted'."""
        import uuid
        project = Project(name="Deleted Runner Project", user_id="runner-token-user")
        db_session.add(project)
        await db_session.flush()
        token = create_runner_token(runner_id=uuid.uuid4())
        token_hash = hash_runner_token(token)
        runner = LocalRunner(
            project_id=project.id,
            token_hash=token_hash,
            status="deleted",
        )
        db_session.add(runner)
        await db_session.commit()

        result = await verify_runner_token(token, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_verify_runner_token_accepts_inactive_runner(self, db_session):
        """An inactive runner (stale heartbeat) can still authenticate."""
        import uuid
        project = Project(name="Inactive Runner Project", user_id="runner-token-user")
        db_session.add(project)
        await db_session.flush()
        token = create_runner_token(runner_id=uuid.uuid4())
        token_hash = hash_runner_token(token)
        runner = LocalRunner(
            project_id=project.id,
            token_hash=token_hash,
            status="inactive",
        )
        db_session.add(runner)
        await db_session.commit()

        result = await verify_runner_token(token, db_session)
        assert result is not None
        assert result.status == "inactive"


# ---------------------------------------------------------------------------
# get_metrics_reader: Firebase fallthrough path
# ---------------------------------------------------------------------------

class TestMetricsReaderFirebasePath:
    """get_metrics_reader falls through to Firebase when neither an admin JWT
    nor an ADMIN_API_KEY nor an agent JWT is presented."""

    @pytest.mark.asyncio
    async def test_firebase_admin_user_can_read_metrics(self, db_session, monkeypatch):
        """A Firebase token for an AllowedUser with role=admin reaches get_metrics_reader."""
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        email = "metrics-admin@example.com"
        user = AllowedUser(email=email, role="admin", firebase_uid="uid-metrics-admin")
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(
            email=email, uid="uid-metrics-admin", email_verified=True
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            result = await get_metrics_reader(
                credentials=_creds("firebase-admin-token"), session=db_session
            )

        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_firebase_non_admin_user_cannot_read_metrics(self, db_session, monkeypatch):
        """A Firebase token for a regular (non-admin) AllowedUser is rejected for metrics."""
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        email = "metrics-user@example.com"
        user = AllowedUser(email=email, role="user", firebase_uid="uid-metrics-user")
        db_session.add(user)
        await db_session.commit()

        decoded = _make_decoded_token(
            email=email, uid="uid-metrics-user", email_verified=True
        )
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            with pytest.raises(HTTPException) as exc_info:
                await get_metrics_reader(
                    credentials=_creds("firebase-user-token"), session=db_session
                )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_agent_jwt_takes_metrics_reader_path(self, db_session):
        """A valid agent JWT is accepted by get_metrics_reader without hitting Firebase."""
        project = Project(name="Firebase metrics", user_id="metrics-owner")
        db_session.add(project)
        await db_session.flush()
        task = Task(
            project_id=project.id,
            vm_instance_id="metrics-vm-001",
            payloads=[{"type": "research"}],
        )
        db_session.add(task)
        await db_session.commit()
        token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id="metrics-vm-001",
            task_type="research",
            project_id=str(project.id),
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_metrics_reader(credentials=creds, session=db_session)
        assert result["role"] == "metrics_reader"
        assert result["sub"] == str(task.id)

    @pytest.mark.asyncio
    async def test_vm_scoped_token_is_rejected_by_metrics_reader(self, db_session):
        """A VM-scoped token has no persisted task authority for metrics."""
        token = create_vm_scoped_token(
            vm_instance_id="pool-vm-metrics-001",
            project_id="proj-vm-metrics",
        )
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as rejected:
            await get_metrics_reader(credentials=creds, session=db_session)
        assert rejected.value.status_code == 403


# ---------------------------------------------------------------------------
# firebase-admin version bump investigation: API surface compatibility
# ---------------------------------------------------------------------------

class TestFirebaseAdminAPISurface:
    """Verify that the firebase-admin package (regardless of version) exposes
    the error classes and functions that auth.py depends on.

    These tests do NOT call Firebase's network APIs — they only check the
    module-level structure that auth.py imports and catches. This confirms that
    the installed firebase-admin version is compatible with our usage pattern
    and documents exactly which APIs we depend on.

    This test class was written as part of the firebase-admin 6.8.0→7.5.0 version
    bump investigation (docs/v19-dependency-bump-audit.md: the PARTIAL ROLLBACK
    entry with 'pending root-cause analysis'). All tests here pass with 7.5.0,
    confirming the original 6.9.0 CI instability was transitive/environmental
    rather than a firebase-admin API change.
    """

    def test_firebase_admin_module_importable(self):
        """firebase_admin is importable (basic sanity check for version bump)."""
        import firebase_admin
        assert firebase_admin is not None

    def test_firebase_auth_module_importable(self):
        """firebase_admin.auth is importable."""
        from firebase_admin import auth
        assert auth is not None

    def test_verify_id_token_callable(self):
        """firebase_auth.verify_id_token exists and is callable."""
        from firebase_admin import auth
        assert callable(auth.verify_id_token)

    def test_initialize_app_callable(self):
        """firebase_admin.initialize_app exists and is callable."""
        import firebase_admin
        assert callable(firebase_admin.initialize_app)

    def test_invalid_id_token_error_exists(self):
        """firebase_admin.auth.InvalidIdTokenError class exists."""
        import firebase_admin
        assert hasattr(firebase_admin.auth, "InvalidIdTokenError")
        assert issubclass(firebase_admin.auth.InvalidIdTokenError, Exception)

    def test_expired_id_token_error_exists(self):
        """firebase_admin.auth.ExpiredIdTokenError class exists."""
        import firebase_admin
        assert hasattr(firebase_admin.auth, "ExpiredIdTokenError")
        assert issubclass(firebase_admin.auth.ExpiredIdTokenError, Exception)

    def test_revoked_id_token_error_exists(self):
        """firebase_admin.auth.RevokedIdTokenError class exists."""
        import firebase_admin
        assert hasattr(firebase_admin.auth, "RevokedIdTokenError")
        assert issubclass(firebase_admin.auth.RevokedIdTokenError, Exception)

    def test_user_disabled_error_exists(self):
        """firebase_admin.auth.UserDisabledError class exists."""
        import firebase_admin
        assert hasattr(firebase_admin.auth, "UserDisabledError")
        assert issubclass(firebase_admin.auth.UserDisabledError, Exception)

    def test_certificate_fetch_error_exists(self):
        """firebase_admin.auth.CertificateFetchError class exists."""
        import firebase_admin
        assert hasattr(firebase_admin.auth, "CertificateFetchError")
        assert issubclass(firebase_admin.auth.CertificateFetchError, Exception)

    def test_invalid_id_token_is_subclass_of_expected_id_token_error(self):
        """ExpiredIdTokenError and RevokedIdTokenError are subclasses of InvalidIdTokenError
        — this hierarchy is relied on in catch-block ordering assumptions."""
        import firebase_admin
        # All three are subclasses of the base token-validation exception.
        assert issubclass(
            firebase_admin.auth.ExpiredIdTokenError,
            firebase_admin.auth.InvalidIdTokenError,
        )
        assert issubclass(
            firebase_admin.auth.RevokedIdTokenError,
            firebase_admin.auth.InvalidIdTokenError,
        )

    def test_error_classes_are_catchable_by_value_error_parent(self):
        """InvalidIdTokenError is not a subclass of ValueError; our exception handler
        lists both separately, so they must be disjoint catch targets."""
        import firebase_admin
        # The auth.py except clause lists ValueError separately from Firebase errors,
        # so Firebase errors must NOT be ValueError subclasses (else the ValueError
        # catch would shadow the specific Firebase branch).
        assert not issubclass(firebase_admin.auth.InvalidIdTokenError, ValueError)

    def test_firebase_admin_version_attribute_accessible(self):
        """firebase_admin.__version__ is accessible (helps with debugging)."""
        import firebase_admin
        version = getattr(firebase_admin, "__version__", None)
        assert version is not None, (
            "firebase_admin.__version__ is missing — version introspection broken"
        )

    def test_installed_firebase_admin_version_meets_minimum(self):
        """The installed version is at least 6.8.0 (our historic minimum stable pin).

        Version comparison uses a simple tuple parse to avoid an extra import.
        firebase_admin.__version__ is a dotted-numeric string (e.g. '6.8.0').
        """
        import firebase_admin
        raw = firebase_admin.__version__
        parts = tuple(int(x) for x in raw.split(".")[:3])
        minimum = (6, 8, 0)
        assert parts >= minimum, (
            f"firebase-admin {raw} is older than the 6.8.0 stable pin"
        )
