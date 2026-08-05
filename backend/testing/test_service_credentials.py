"""Tests for the general service-credential (machine token) seam.

Covers spec section 6 (``service_credential_spec.md``): the admin management
router (mint/list/revoke), the ``authenticate_service_token`` auth branch
wired into ``get_current_user``, and the scope allowlist in
``app.service_credentials``. The credential is a revocable, non-admin, bearer
token that authenticates as a configurable non-human ``principal_user_id``
and is only accepted on a per-scope allowlist of endpoints (Hermes observer
is the first consumer).

Two auth postures are exercised:
  * The ``client`` fixture (admin persona via conftest's blanket override) —
    used for the platform-admin-only management endpoints (mint/list/revoke).
  * ``_real_auth_client()`` — pops the auth overrides so the REAL
    ``get_current_user`` dependency chain runs, including the new service-
    credential branch, exactly as ``testing/test_auth.py`` does for its
    real-token-path tests.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import inspect
from sqlmodel import select
from starlette.requests import Request

from app.auth import get_current_user, get_current_user_optional, require_admin
from app.main import app
from app.models import Project, ProjectMember, ServiceCredential, _utcnow
from app.service_credentials import (
    TOKEN_PREFIX,
    authenticate_service_token,
    hash_service_token,
    mint_service_token,
    request_allowed,
)
from testing.factories import make_membership

MGMT_URL = "/api/admin/service-credentials"


@asynccontextmanager
async def _real_auth_client() -> AsyncIterator[AsyncClient]:
    """Run requests through the REAL ``get_current_user`` dependency chain.

    The conftest ``client`` fixture always overrides ``get_current_user``,
    ``get_current_user_optional`` and ``require_admin`` with an admin
    persona (fine for the management-endpoint tests), which means it can
    never exercise the actual service-credential auth branch. This pops all
    three overrides, hands back a fresh raw ``AsyncClient``, and restores
    the overrides afterward so later use of ``client`` in the same test (or
    later tests) is unaffected — mirroring the pop/finally pattern in
    ``testing/test_auth.py``.
    """
    saved_user = app.dependency_overrides.pop(get_current_user, None)
    saved_user_optional = app.dependency_overrides.pop(get_current_user_optional, None)
    saved_admin = app.dependency_overrides.pop(require_admin, None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        if saved_user is not None:
            app.dependency_overrides[get_current_user] = saved_user
        if saved_user_optional is not None:
            app.dependency_overrides[get_current_user_optional] = saved_user_optional
        if saved_admin is not None:
            app.dependency_overrides[require_admin] = saved_admin


async def _make_project(db_session, name: str, owner: str = "svc-cred-owner") -> Project:
    project = Project(name=name, user_id=owner)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _seed_credential(
    db_session,
    *,
    name: str,
    principal_user_id: str = "service:test-observer",
    scopes: list[str] | tuple[str, ...] = ("observer:read",),
    expires_at=None,
    revoked_at=None,
) -> tuple[ServiceCredential, str]:
    """Persist a ``ServiceCredential`` row directly via the ORM, bypassing the
    mint endpoint — for tests exercising the auth path in isolation, per spec
    section 6 ("construct ServiceCredential rows directly via ORM +
    mint_service_token/hash_service_token helpers")."""
    token = mint_service_token()
    cred = ServiceCredential(
        name=name,
        principal_user_id=principal_user_id,
        token_hash=hash_service_token(token),
        token_prefix=token[:12],
        scopes=list(scopes),
        expires_at=expires_at,
        revoked_at=revoked_at,
        created_by="test-harness",
    )
    db_session.add(cred)
    await db_session.commit()
    await db_session.refresh(cred)
    return cred, token


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Mint round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_round_trip_creates_hashed_credential_and_membership(
    client: AsyncClient, db_session
):
    """POST mint returns a plaintext bpsc_ token, stores only its SHA256 hash,
    and idempotently creates the requested project membership."""
    project = await _make_project(db_session, "Mint round-trip project")

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "observer-bot",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token"].startswith(TOKEN_PREFIX)
    assert body.get("token_notice")
    assert body["name"] == "observer-bot"

    row = (
        await db_session.execute(
            select(ServiceCredential).where(ServiceCredential.name == "observer-bot")
        )
    ).scalar_one()
    assert row.token_hash == hash_service_token(body["token"])
    assert row.token_hash != body["token"]

    members = (
        await db_session.execute(
            select(ProjectMember).where(ProjectMember.project_id == project.id)
        )
    ).scalars().all()
    assert len(members) == 1
    assert members[0].user_id == row.principal_user_id
    assert members[0].role == "viewer"


# ---------------------------------------------------------------------------
# 2. Authorized read works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorized_scoped_endpoints_return_200_for_real_token(
    override_get_session, db_session, monkeypatch
):
    """A real service token with membership on the target project succeeds on
    all three observer:read-scoped, membership-checked GET endpoints."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Authorized read project")
    cred, token = await _seed_credential(db_session, name="observer-authorized")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        attention = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
        resource_summary = await rc.get(
            f"/api/admin/projects/{project.id}/resource-summary",
            headers=_auth_header(token),
        )
        diagnostics = await rc.get(
            f"/api/admin/projects/{project.id}/diagnostics",
            headers=_auth_header(token),
        )

    assert attention.status_code == 200, attention.text
    assert resource_summary.status_code == 200, resource_summary.text
    assert diagnostics.status_code == 200, diagnostics.text


@pytest.mark.asyncio
async def test_service_credential_observer_read_authorized_endpoints(
    override_get_session, db_session, monkeypatch
):
    """GET /api/admin/projects — the fourth observer:read allowlisted path — is
    exercised with a real service token. Unlike the three per-project endpoints
    (which 404 via _get_user_project), the list endpoint scopes via a
    ProjectMember join: the credential's own project appears, a foreign project
    it holds no membership on is silently excluded, and a viewer-role
    credential gets build_config env_vars redacted. The trailing-slash form of
    the list path is asserted at the allowlist seam (request_allowed) so the
    slash normalization in request_allowed is exercised here too."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    from sqlalchemy.orm.attributes import flag_modified

    authorized = await _make_project(db_session, "Observer list authorized project")
    authorized.build_config = {
        "env_vars": {"PRIVATE_TOKEN": "s3cr3t"},
        "node_version": "20",
    }
    flag_modified(authorized, "build_config")
    db_session.add(authorized)
    foreign = await _make_project(db_session, "Observer list foreign project")
    cred, token = await _seed_credential(db_session, name="observer-list-read")
    db_session.add(
        make_membership(authorized.id, user_id=cred.principal_user_id, role="viewer")
    )
    await db_session.commit()

    # Slash-insensitive allowlist: the bare and trailing-slash list paths are
    # both authorized once request_allowed strips the single trailing slash.
    assert request_allowed(["observer:read"], "GET", "/api/admin/projects")
    assert request_allowed(["observer:read"], "GET", "/api/admin/projects/")

    async with _real_auth_client() as rc:
        resp = await rc.get("/api/admin/projects", headers=_auth_header(token))

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    item_ids = {item["id"] for item in items}
    assert str(authorized.id) in item_ids
    assert str(foreign.id) not in item_ids

    authorized_item = next(item for item in items if item["id"] == str(authorized.id))
    assert authorized_item["my_role"] == "viewer"
    assert "env_vars" not in authorized_item["build_config"]
    assert authorized_item["build_config"].get("node_version") == "20"


# ---------------------------------------------------------------------------
# 3. Project isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_isolation_denies_unauthorized_project(
    override_get_session, db_session, monkeypatch
):
    """The same credential, valid on its own project, is denied (404 — tenant
    boundary) on a second project it holds no membership on."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    authorized_project = await _make_project(db_session, "Isolation authorized project")
    unauthorized_project = await _make_project(db_session, "Isolation foreign project")
    cred, token = await _seed_credential(db_session, name="observer-isolated")
    db_session.add(
        make_membership(authorized_project.id, user_id=cred.principal_user_id, role="viewer")
    )
    await db_session.commit()

    async with _real_auth_client() as rc:
        attention = await rc.get(
            f"/api/admin/operator-attention?project_id={unauthorized_project.id}",
            headers=_auth_header(token),
        )
        resource_summary = await rc.get(
            f"/api/admin/projects/{unauthorized_project.id}/resource-summary",
            headers=_auth_header(token),
        )
        diagnostics = await rc.get(
            f"/api/admin/projects/{unauthorized_project.id}/diagnostics",
            headers=_auth_header(token),
        )

    assert attention.status_code == 404, attention.text
    assert resource_summary.status_code == 404, resource_summary.text
    assert diagnostics.status_code == 404, diagnostics.text


# ---------------------------------------------------------------------------
# 4. Endpoint allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_allowlist_rejects_out_of_scope_get_and_mutating_post(
    override_get_session, db_session, monkeypatch
):
    """The allowlist rejects BEFORE any handler runs: a membership-scoped GET
    outside observer:read's paths, and a mutating POST, are both 403 even
    though the credential genuinely holds membership on the target project."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Allowlist project")
    cred, token = await _seed_credential(db_session, name="observer-allowlist")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="member"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        # GET but not one of the four allowlisted paths.
        out_of_scope_get = await rc.get(
            f"/api/admin/projects/{project.id}/auto-remediation",
            headers=_auth_header(token),
        )
        # A mutating admin endpoint — method alone excludes it (allowlist is
        # GET-only for observer:read).
        mutating_post = await rc.post(
            f"/api/admin/projects/{project.id}/circuit-breaker/reset",
            headers=_auth_header(token),
        )

    assert out_of_scope_get.status_code == 403, out_of_scope_get.text
    assert mutating_post.status_code == 403, mutating_post.text


# ---------------------------------------------------------------------------
# 5. Never admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_credential_never_satisfies_require_admin(
    override_get_session, db_session, monkeypatch
):
    """A real service-credential request to a require_admin, platform-global
    endpoint (GET /api/admin/diagnostics) is rejected 403 — the credential's
    role is always "user", and the endpoint is outside observer:read's
    per-project allowlist regardless."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    cred, token = await _seed_credential(db_session, name="observer-never-admin")

    async with _real_auth_client() as rc:
        resp = await rc.get("/api/admin/diagnostics", headers=_auth_header(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 6. Revocation immediate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_immediately_blocks_real_token(
    client: AsyncClient, db_session, monkeypatch
):
    """A token that authenticates successfully stops working the instant its
    credential is revoked via the management endpoint — no caching/TTL."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Revocation project")

    mint_resp = await client.post(
        MGMT_URL,
        json={
            "name": "observer-revocable",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )
    assert mint_resp.status_code == 201, mint_resp.text
    body = mint_resp.json()
    token = body["token"]
    cred_id = body["id"]

    async with _real_auth_client() as rc:
        before = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert before.status_code == 200, before.text

    revoke_resp = await client.post(f"{MGMT_URL}/{cred_id}/revoke")
    assert revoke_resp.status_code == 200, revoke_resp.text

    async with _real_auth_client() as rc:
        after = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert after.status_code == 401, after.text


# ---------------------------------------------------------------------------
# 7. Expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_credential_returns_401(
    override_get_session, db_session, monkeypatch
):
    """A credential whose expires_at is already in the past is rejected 401,
    even though it is not explicitly revoked."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Expiry project")
    cred, token = await _seed_credential(
        db_session,
        name="observer-expired",
        expires_at=_utcnow() - timedelta(days=1),
    )
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()

    async with _real_auth_client() as rc:
        resp = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )

    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 8. Unknown bpsc_ token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_bpsc_token_returns_401_without_reaching_firebase(
    override_get_session, db_session, monkeypatch
):
    """A syntactically valid bpsc_-prefixed token with no matching DB row is
    rejected 401 with the service-credential-specific detail message — proof
    it never fell through to Firebase verification (which would instead
    produce "Invalid authentication credentials")."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    unknown_token = mint_service_token()  # never persisted anywhere

    async with _real_auth_client() as rc:
        resp = await rc.get(
            "/api/admin/operator-attention?project_id=00000000-0000-0000-0000-000000000000",
            headers=_auth_header(unknown_token),
        )

    assert resp.status_code == 401, resp.text
    # All HTTPExceptions are wrapped by main.py's global handler into
    # {"error": {"code": ..., "message": ...}} — never bare {"detail": ...}
    # (see app/main.py http_exception_handler). The spec pins the literal
    # detail text "Invalid service credential"; if it reached Firebase
    # verification instead, the message would be "Invalid authentication
    # credentials" (app/auth.py's existing Firebase-branch failure text).
    detail = resp.json()["error"]["message"]
    assert "invalid" in detail.lower()
    assert "service credential" in detail.lower()
    assert "authentication credentials" not in detail.lower()


# ---------------------------------------------------------------------------
# 9. last_used tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_used_tracking_populated_after_successful_call(
    override_get_session, db_session, monkeypatch
):
    """A successful authenticated call stamps last_used_at/last_used_path on
    the credential row."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    project = await _make_project(db_session, "Last-used project")
    cred, token = await _seed_credential(db_session, name="observer-last-used")
    db_session.add(make_membership(project.id, user_id=cred.principal_user_id, role="viewer"))
    await db_session.commit()
    assert cred.last_used_at is None
    assert cred.last_used_path is None

    async with _real_auth_client() as rc:
        resp = await rc.get(
            f"/api/admin/operator-attention?project_id={project.id}",
            headers=_auth_header(token),
        )
    assert resp.status_code == 200, resp.text

    db_session.expunge_all()
    refreshed = (
        await db_session.execute(
            select(ServiceCredential).where(ServiceCredential.id == cred.id)
        )
    ).scalar_one()
    assert refreshed.last_used_at is not None
    assert refreshed.last_used_path is not None
    assert "operator-attention" in refreshed.last_used_path


@pytest.mark.asyncio
async def test_authentication_survives_failed_last_used_telemetry_commit(
    db_session, monkeypatch, real_orm_expiry
):
    """A telemetry rollback must not invalidate an otherwise valid principal."""
    cred, token = await _seed_credential(db_session, name="observer-telemetry-failure")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/projects/00000000-0000-0000-0000-000000000000/diagnostics",
            "query_string": b"",
            "headers": [],
        }
    )

    async def commit_then_fail():
        await db_session.flush()
        raise RuntimeError("telemetry commit failure")

    monkeypatch.setattr(db_session, "commit", commit_then_fail)

    principal = await authenticate_service_token(token, db_session, request)

    assert inspect(cred).expired, "rollback must exercise real ORM expiry semantics"
    assert principal == {
        "user_id": "service:test-observer",
        "email": "observer-telemetry-failure@service.blueprint.local",
        "role": "user",
        "service_credential": "observer-telemetry-failure",
    }


# ---------------------------------------------------------------------------
# 10. Management auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_management_mint_requires_authentication(unauthenticated_client: AsyncClient):
    """The mint endpoint is platform-admin-only; an unauthenticated caller
    (no Firebase credentials, no admin override) is rejected."""
    resp = await unauthenticated_client.post(
        MGMT_URL,
        json={"name": "unauthorized-mint", "scopes": ["observer:read"]},
    )
    assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# 11. Mint validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_rejects_unknown_scope(client: AsyncClient, db_session):
    resp = await client.post(
        MGMT_URL,
        json={"name": "bad-scope-bot", "scopes": ["not-a-real-scope"]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_rejects_owner_role_in_project_memberships(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, "Owner-rejected project")
    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-role-bot",
            "scopes": ["observer:read"],
            "project_memberships": [
                {"project_id": str(project.id), "role": "owner"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_rejects_principal_user_id_matching_project_owner(
    client: AsyncClient, db_session
):
    """A caller-supplied principal_user_id that IS an existing project owner
    (Project.user_id) is rejected: the credential would authenticate as that
    owner and inherit owner-level membership, bypassing the no-owner rule."""
    owner_uid = "firebase-uid-project-owner"
    await _make_project(db_session, "Owner-principal project", owner=owner_uid)

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-principal-bot",
            "scopes": ["observer:read"],
            "principal_user_id": owner_uid,
        },
    )

    assert resp.status_code == 422, resp.text
    remaining = (
        await db_session.execute(
            select(ServiceCredential).where(
                ServiceCredential.name == "owner-principal-bot"
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_mint_rejects_principal_user_id_with_owner_membership(
    client: AsyncClient, db_session
):
    """A caller-supplied principal_user_id already holding an owner-role
    ProjectMember row is rejected for the same impersonation reason."""
    owner_uid = "firebase-uid-owner-member"
    project = await _make_project(db_session, "Owner-member project")
    db_session.add(make_membership(project.id, user_id=owner_uid, role="owner"))
    await db_session.commit()

    resp = await client.post(
        MGMT_URL,
        json={
            "name": "owner-member-bot",
            "scopes": ["observer:read"],
            "principal_user_id": owner_uid,
        },
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mint_accepts_custom_non_owner_principal_user_id(
    client: AsyncClient, db_session
):
    """A custom non-owner principal_user_id is honored and minted normally."""
    project = await _make_project(db_session, "Custom-principal project")
    resp = await client.post(
        MGMT_URL,
        json={
            "name": "custom-principal-bot",
            "scopes": ["observer:read"],
            "principal_user_id": "custom-machine-principal-1",
            "project_memberships": [
                {"project_id": str(project.id), "role": "viewer"}
            ],
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["principal_user_id"] == "custom-machine-principal-1"


@pytest.mark.asyncio
async def test_mint_rejects_duplicate_unrevoked_name(client: AsyncClient, db_session):
    first = await client.post(
        MGMT_URL, json={"name": "dupe-name-bot", "scopes": ["observer:read"]}
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        MGMT_URL, json={"name": "dupe-name-bot", "scopes": ["observer:read"]}
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_mint_allows_name_reuse_after_revocation(
    client: AsyncClient, db_session
):
    """A revoked name may be re-minted: the uniqueness rule only guards ACTIVE
    (non-revoked) credentials, so reusing a revoked name must succeed instead
    of tripping the DB-level unique constraint (regression for migration 0723's
    full uq_servicecredential_name, which turned the intended reuse into a
    409 IntegrityError)."""
    first = await client.post(
        MGMT_URL, json={"name": "reusable-bot", "scopes": ["observer:read"]}
    )
    assert first.status_code == 201, first.text
    first_token = first.json()["token"]

    revoke = await client.post(f"{MGMT_URL}/{first.json()['id']}/revoke")
    assert revoke.status_code == 200, revoke.text

    second = await client.post(
        MGMT_URL, json={"name": "reusable-bot", "scopes": ["observer:read"]}
    )
    assert second.status_code == 201, second.text
    assert second.json()["token"] != first_token


# ---------------------------------------------------------------------------
# 12. List endpoint never returns token/hash fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_endpoint_never_returns_token_or_hash_fields(
    client: AsyncClient, db_session
):
    mint_resp = await client.post(
        MGMT_URL,
        json={"name": "list-safety-bot", "scopes": ["observer:read"]},
    )
    assert mint_resp.status_code == 201, mint_resp.text
    plaintext_token = mint_resp.json()["token"]
    token_hash = hash_service_token(plaintext_token)

    list_resp = await client.get(MGMT_URL)
    assert list_resp.status_code == 200, list_resp.text
    entries = list_resp.json()["service_credentials"]
    assert isinstance(entries, list)
    entry = next(e for e in entries if e["name"] == "list-safety-bot")
    assert "token" not in entry
    assert "token_hash" not in entry

    serialized = list_resp.text
    assert plaintext_token not in serialized
    assert token_hash not in serialized
