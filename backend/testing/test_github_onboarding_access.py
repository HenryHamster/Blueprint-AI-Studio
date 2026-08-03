"""Authorization proofs for ordinary-user GitHub onboarding surfaces."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.auth import get_current_user, require_admin
from app.main import app
from app.models import Project, UserIntegration
from app.time_utils import _utcnow


def _response(url: str, payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", url),
    )


@pytest.fixture
def ordinary_user():
    previous = app.dependency_overrides.get(get_current_user)

    async def _ordinary():
        return {
            "user_id": "ordinary-onboard-user",
            "email": "ordinary@example.com",
            "role": "user",
        }

    app.dependency_overrides[get_current_user] = _ordinary
    try:
        yield _ordinary
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


@pytest.mark.asyncio
async def test_github_status_unlinked_returns_only_safe_false_facts(
    client, ordinary_user
):
    with (
        patch("app.github.GITHUB_APP_ID", None),
        patch("app.github.GITHUB_PRIVATE_KEY", None),
    ):
        response = await client.get("/api/github/status")

    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "account_name": None,
        "app_configured": False,
        "app_has_installations": False,
        "identity_linked": False,
        "credential_kind": None,
        "app_installation_linked": False,
        "app_installation_reachable": False,
        "installation_count": 0,
    }


@pytest.mark.asyncio
async def test_github_status_reports_linked_identity_and_all_visible_installations(
    client, db_session, ordinary_user
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
            github_installation_id="101",
            github_account_name="octo-user",
        )
    )
    await db_session.commit()

    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch(
            "app.secrets_manager.get_user_secret",
            new_callable=AsyncMock,
            return_value="gho_user_token",
        ),
        patch(
            "app.github.get_user_installations",
            new_callable=AsyncMock,
            return_value=[{"id": 101}, {"id": 202}],
        ),
        patch("app.github.GITHUB_APP_ID", "123"),
        patch("app.github.GITHUB_PRIVATE_KEY", "private"),
    ):
        response = await client.get("/api/github/status")

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "account_name": "octo-user",
        "app_configured": True,
        "app_has_installations": True,
        "identity_linked": True,
        "credential_kind": "oauth",
        "app_installation_linked": True,
        "app_installation_reachable": True,
        "installation_count": 2,
    }
    assert "token" not in response.text.lower()


@pytest.mark.asyncio
async def test_github_status_connected_without_app_installation(
    client, db_session, ordinary_user
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
            github_account_name="octo-user",
        )
    )
    await db_session.commit()
    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch("app.secrets_manager.get_user_secret", new_callable=AsyncMock, return_value="gho_user"),
        patch("app.github.get_user_installations", new_callable=AsyncMock, return_value=[]),
        patch("app.github.GITHUB_APP_ID", "123"),
        patch("app.github.GITHUB_PRIVATE_KEY", "private"),
    ):
        response = await client.get("/api/github/status")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["app_configured"] is True
    assert response.json()["app_has_installations"] is False


@pytest.mark.asyncio
async def test_github_status_preserves_upstream_rate_limit(
    client, db_session, ordinary_user
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
            github_account_name="octo-user",
        )
    )
    await db_session.commit()
    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch("app.secrets_manager.get_user_secret", new_callable=AsyncMock, return_value="gho_user"),
        patch(
            "app.github.get_user_installations",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=429,
                detail="GitHub rate limit prevented installation discovery",
            ),
        ),
        patch("app.github.GITHUB_APP_ID", "123"),
        patch("app.github.GITHUB_PRIVATE_KEY", "private"),
    ):
        response = await client.get("/api/github/status")

    assert response.status_code == 429
    assert "rate limit" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_limited_github_routes_remain_callable_for_admin(client):
    """Limiter request signatures must not break the legacy admin paths."""

    repo_response = _response(
        "https://api.github.com/installation/repositories?per_page=100",
        {"repositories": []},
    )
    branch_response = _response(
        "https://api.github.com/repos/acme/repo/branches?per_page=100",
        [],
    )
    fake_client = AsyncMock()
    fake_client.get.side_effect = [repo_response, branch_response]
    fake_context = MagicMock()
    fake_context.__aenter__ = AsyncMock(return_value=fake_client)
    fake_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.github.GITHUB_APP_ID", None),
        patch("app.github.GITHUB_PRIVATE_KEY", None),
        patch(
            "app.routers.admin_settings._get_installation_token_for_user",
            new_callable=AsyncMock,
            return_value="installation-token",
        ),
        patch(
            "app.routers.admin_settings.httpx.AsyncClient",
            return_value=fake_context,
        ),
    ):
        status_response = await client.get("/api/github/status")
        repos_response = await client.get("/api/admin/github/repos")
        branches_response = await client.get(
            "/api/admin/github/branches", params={"repo": "acme/repo"}
        )

    assert status_response.status_code == 200
    assert repos_response.status_code == 200
    assert repos_response.json() == {"repositories": []}
    assert branches_response.status_code == 200
    assert branches_response.json() == {"branches": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installations", "expected"),
    [
        ([{"id": 101}], ["acme/alpha"]),
        ([{"id": 101}, {"id": 202}], ["acme/alpha", "acme/beta"]),
    ],
)
async def test_repo_discovery_intersects_caller_writes_across_all_installations(
    client, db_session, ordinary_user, installations, expected
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
            github_installation_id="101",
        )
    )
    await db_session.commit()

    async def _github_request(_method: str, url: str, headers: dict, **_kwargs):
        authorization = headers.get("Authorization")
        if "/user/repos" in url:
            return _response(
                url,
                [
                    {
                        "full_name": "acme/alpha",
                        "default_branch": "main",
                        "permissions": {"push": True},
                    },
                    {
                        "full_name": "acme/beta",
                        "default_branch": "trunk",
                        "permissions": {"admin": True},
                    },
                    {
                        "full_name": "acme/read-only",
                        "default_branch": "main",
                        "permissions": {"pull": True},
                    },
                ],
            )
        if authorization == "Bearer install-101":
            return _response(
                url, {"repositories": [{"full_name": "acme/alpha"}, {"full_name": "acme/read-only"}]}
            )
        if authorization == "Bearer install-202":
            return _response(url, {"repositories": [{"full_name": "acme/beta"}]})
        raise AssertionError((url, authorization))

    async def _mint(installation_id: str):
        return f"install-{installation_id}"

    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch(
            "app.secrets_manager.get_user_secret",
            new_callable=AsyncMock,
            return_value="gho_user_token",
        ),
        patch(
            "app.github.get_user_installations",
            new_callable=AsyncMock,
            return_value=installations,
        ),
        patch("app.github.generate_installation_access_token", side_effect=_mint),
        patch("app.github._github_request", side_effect=_github_request),
    ):
        response = await client.get("/api/admin/github/repos")

    assert response.status_code == 200, response.text
    assert [repo["full_name"] for repo in response.json()["repositories"]] == expected
    assert response.json()["installation_count"] == len(installations)


@pytest.mark.asyncio
async def test_manual_repo_distinguishes_caller_unauthorized_from_app_unreachable(
    client, db_session, ordinary_user
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
            github_installation_id="101",
        )
    )
    await db_session.commit()
    app_token = AsyncMock(return_value="should-not-mint")
    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch(
            "app.secrets_manager.get_user_secret",
            new_callable=AsyncMock,
            return_value="gho_user_token",
        ),
        patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            return_value=_response(
                "https://api.github.com/repos/acme/manual",
                {"permissions": {"pull": True, "push": False}},
            ),
        ),
        patch("app.github.get_repo_installation_token", app_token),
    ):
        unauthorized = await client.get(
            "/api/admin/github/branches", params={"repo": "acme/manual"}
        )

    assert unauthorized.status_code == 403
    assert "push access" in unauthorized.json()["error"]["message"]
    app_token.assert_not_awaited()

    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch(
            "app.secrets_manager.get_user_secret",
            new_callable=AsyncMock,
            return_value="gho_user_token",
        ),
        patch(
            "app.github._github_request",
            new_callable=AsyncMock,
            return_value=_response(
                "https://api.github.com/repos/acme/manual",
                {"permissions": {"push": True}},
            ),
        ),
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        unreachable = await client.get(
            "/api/admin/github/branches", params={"repo": "acme/manual"}
        )

    assert unreachable.status_code == 422
    assert "GitHub App" in unreachable.json()["error"]["message"]


@pytest.mark.asyncio
async def test_manual_repo_rate_limit_is_not_reported_as_permission_denial(
    client, db_session, ordinary_user
):
    db_session.add(
        UserIntegration(
            user_id="ordinary-onboard-user",
            github_oauth_token="encrypted-oauth-ref",
        )
    )
    await db_session.commit()
    app_token = AsyncMock()
    limited = _response(
        "https://api.github.com/repos/acme/manual",
        {"message": "rate limit exceeded"},
        status_code=403,
    )
    limited.headers["X-RateLimit-Remaining"] = "0"
    with (
        patch("app.field_encryption.decrypt_field", return_value="oauth-ref"),
        patch("app.secrets_manager.get_user_secret", new_callable=AsyncMock, return_value="gho_user"),
        patch("app.github._github_request", new_callable=AsyncMock, return_value=limited),
        patch("app.github.get_repo_installation_token", app_token),
    ):
        response = await client.get(
            "/api/admin/github/branches", params={"repo": "acme/manual"}
        )

    assert response.status_code == 429
    assert "rate limit" in response.json()["error"]["message"].lower()
    app_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_repo_rejects_malformed_value_before_github_calls(
    client, ordinary_user
):
    with patch("app.github.get_repo_installation_token", new_callable=AsyncMock) as mint:
        response = await client.get(
            "/api/admin/github/branches", params={"repo": "https://github.com/acme/repo"}
        )
    assert response.status_code == 400
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_callback_persists_verified_identity_before_empty_install_discovery(
    client, db_session
):
    import jwt
    from app.auth import AGENT_JWT_ALGORITHM, AGENT_JWT_SECRET

    state = jwt.encode(
        {
            "type": "github_oauth",
            "sub": "oauth-pending-install-user",
            "admin_url": "https://blueprint-admin-9c6b7.web.app",
            "exp": _utcnow() + timedelta(minutes=5),
        },
        AGENT_JWT_SECRET,
        algorithm=AGENT_JWT_ALGORITHM,
    )
    with (
        patch("app.routers.github.exchange_oauth_code", new_callable=AsyncMock, return_value={"access_token": "gho_verified"}),
        patch("app.routers.github.get_github_user", new_callable=AsyncMock, return_value={"login": "verified-octo"}),
        patch("app.routers.github.get_user_installations", new_callable=AsyncMock, return_value=[]),
        patch("app.routers.github.store_user_secret", new_callable=AsyncMock, return_value="secret-ref"),
        patch("app.routers.github.encrypt_field", return_value="encrypted-ref"),
        patch("app.github.get_app_info", new_callable=AsyncMock, return_value={}),
    ):
        response = await client.get(
            "/api/github/oauth/callback", params={"code": "code", "state": state}
        )

    assert response.status_code in (302, 307)
    assert "github_error=no_installations" in response.headers["location"]
    integration = (
        await db_session.execute(
            select(UserIntegration).where(
                UserIntegration.user_id == "oauth-pending-install-user"
            )
        )
    ).scalar_one()
    assert integration.github_account_name == "verified-octo"
    assert integration.github_oauth_token == "encrypted-ref"
    assert integration.github_installation_id is None


@pytest.mark.asyncio
async def test_oauth_authorize_allows_explicit_local_port_but_not_lookalike_host(
    client, ordinary_user, monkeypatch
):
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:5173")
    with patch("app.routers.github.GITHUB_CLIENT_ID", "client-id"):
        local = await client.get(
            "/api/github/oauth/authorize",
            params={"redirect_uri": "http://localhost:5173"},
        )
        lookalike = await client.get(
            "/api/github/oauth/authorize",
            params={"redirect_uri": "https://localhost.evil.example"},
        )
    assert local.status_code == 200
    assert lookalike.status_code == 400


@pytest.mark.asyncio
async def test_non_admin_warm_pool_onboarding_is_denied_before_any_side_effect(
    client, db_session, ordinary_user
):
    before = (
        await db_session.execute(select(func.count()).select_from(Project))
    ).scalar_one()
    repo_check = AsyncMock()
    app_token = AsyncMock()
    apply_settings = AsyncMock()
    seed_pool = AsyncMock()
    with (
        patch(
            "app.github_onboarding.require_caller_repo_push_access", repo_check
        ),
        patch("app.github.get_repo_installation_token", app_token),
        patch("app.routers.admin_settings._apply_settings", apply_settings),
        patch("app.routers.admin_warm_pool._seed_project_pool", seed_pool),
    ):
        response = await client.post(
            "/api/admin/projects/onboard",
            json={
                "github_owner": "acme",
                "github_repo": "forbidden-seed",
                "warm_pool_target_size": 2,
            },
        )

    assert response.status_code == 403
    assert "platform admin" in response.json()["error"]["message"]
    repo_check.assert_not_awaited()
    app_token.assert_not_awaited()
    apply_settings.assert_not_awaited()
    seed_pool.assert_not_awaited()
    db_session.expire_all()
    after = (
        await db_session.execute(select(func.count()).select_from(Project))
    ).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_project_onboarding_requires_auth_before_any_side_effect(client):
    previous = app.dependency_overrides.get(get_current_user)

    async def _unauthenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")

    repo_check = AsyncMock()
    app_token = AsyncMock()
    app.dependency_overrides[get_current_user] = _unauthenticated
    try:
        with (
            patch(
                "app.github_onboarding.require_caller_repo_push_access", repo_check
            ),
            patch("app.github.get_repo_installation_token", app_token),
        ):
            response = await client.post(
                "/api/admin/projects/onboard",
                json={"github_owner": "acme", "github_repo": "private-repo"},
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 401
    repo_check.assert_not_awaited()
    app_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_size", [0, 51])
async def test_warm_pool_onboarding_rejects_out_of_range_target_before_side_effects(
    client, target_size
):
    repo_check = AsyncMock()
    app_token = AsyncMock()
    with (
        patch("app.github_onboarding.require_caller_repo_push_access", repo_check),
        patch("app.github.get_repo_installation_token", app_token),
    ):
        response = await client.post(
            "/api/admin/projects/onboard",
            json={
                "github_owner": "acme",
                "github_repo": "invalid-warm-pool",
                "warm_pool_target_size": target_size,
            },
        )

    assert response.status_code == 422
    repo_check.assert_not_awaited()
    app_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/settings",
        "/api/admin/max-plan-accounts/health",
        "/api/admin/max-plan-accounts/provider-model-breakdown",
        "/api/admin/pool-accounts/project-summary",
    ],
)
async def test_ordinary_user_cannot_read_global_settings_or_account_resources(
    client, ordinary_user, path
):
    previous = app.dependency_overrides.get(require_admin)

    async def _deny_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[require_admin] = _deny_admin
    try:
        response = await client.get(path)
    finally:
        if previous is None:
            app.dependency_overrides.pop(require_admin, None)
        else:
            app.dependency_overrides[require_admin] = previous

    assert response.status_code == 403
