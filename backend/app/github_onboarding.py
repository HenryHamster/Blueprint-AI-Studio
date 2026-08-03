"""Caller-scoped GitHub facts used by self-service onboarding.

This module deliberately composes the existing ``UserIntegration`` credential,
GitHub OAuth/PAT secret storage, and GitHub App installation APIs.  It does not
mint or persist another identity.  Repository discovery is the intersection of
what the linked GitHub user may push and what the Blueprint App can reach.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import UserIntegration

logger = logging.getLogger(__name__)

_GITHUB_ACCEPT_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def get_user_integration(
    session: AsyncSession, user_id: str
) -> UserIntegration | None:
    return (
        await session.execute(
            select(UserIntegration).where(UserIntegration.user_id == user_id)
        )
    ).scalars().first()


def integration_credential_kind(integration: UserIntegration | None) -> str | None:
    if integration and integration.github_oauth_token:
        return "oauth"
    if integration and integration.github_pat:
        return "pat"
    return None


async def resolve_caller_github_token(
    session: AsyncSession, user_id: str
) -> tuple[UserIntegration, str, str]:
    """Resolve the existing linked user credential without exposing it."""

    integration = await get_user_integration(session, user_id)
    credential_kind = integration_credential_kind(integration)
    encrypted_ref = (
        integration.github_oauth_token
        if credential_kind == "oauth"
        else integration.github_pat if integration else None
    )
    if not integration or not encrypted_ref or not credential_kind:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub identity is not linked. Connect GitHub or register a fine-grained PAT.",
        )

    try:
        from app.field_encryption import decrypt_field
        from app.secrets_manager import get_user_secret

        secret_ref = decrypt_field(encrypted_ref)
        if not secret_ref:
            raise ValueError("empty GitHub credential reference")
        token = await get_user_secret(secret_ref)
        if not token:
            raise ValueError("empty GitHub credential")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "[GITHUB ONBOARDING] linked credential could not be resolved for %s: %s",
            user_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Linked GitHub credential is unavailable. Reconnect GitHub and retry.",
        ) from exc
    return integration, token, credential_kind


def _user_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **_GITHUB_ACCEPT_HEADERS}


def _raise_if_rate_limited(response) -> None:
    from app.github import is_github_rate_limited_response

    if is_github_rate_limited_response(response):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="GitHub rate limit prevented authorization verification. Retry later.",
            headers={"Retry-After": response.headers.get("Retry-After", "60")},
        )


async def require_caller_repo_push_access(
    session: AsyncSession,
    current_user: dict,
    owner: str,
    repo: str,
) -> None:
    """Require the current non-admin GitHub identity to push the exact repo."""

    if current_user.get("role") == "admin":
        return
    _, user_token, _ = await resolve_caller_github_token(
        session, current_user["user_id"]
    )
    from app.github import _github_request

    try:
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}",
            _user_headers(user_token),
        )
    except Exception as exc:
        logger.warning(
            "[GITHUB ONBOARDING] caller repository check failed for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub could not verify caller repository authorization. Retry shortly.",
        ) from exc

    if response.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Linked GitHub credential is expired or invalid. Reconnect GitHub.",
        )
    _raise_if_rate_limited(response)
    permissions = (
        response.json().get("permissions") or {}
        if response.status_code == 200
        else {}
    )
    if response.status_code in (403, 404) or not (
        permissions.get("push") or permissions.get("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your linked GitHub identity does not have push access or admin access "
                f"to '{owner}/{repo}'."
            ),
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub could not verify caller repository authorization. Retry shortly.",
        )


async def _list_caller_writable_repositories(user_token: str) -> dict[str, dict[str, Any]]:
    from app.github import _github_request

    writable: dict[str, dict[str, Any]] = {}
    url: str | None = (
        "https://api.github.com/user/repos?per_page=100"
        "&affiliation=owner,collaborator,organization_member"
    )
    while url:
        response = await _github_request("GET", url, _user_headers(user_token))
        if response.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Linked GitHub credential is expired or invalid. Reconnect GitHub.",
            )
        _raise_if_rate_limited(response)
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to discover repositories authorized for your GitHub identity.",
            )
        for repo in response.json():
            permissions = repo.get("permissions") or {}
            full_name = repo.get("full_name")
            if isinstance(full_name, str) and (
                permissions.get("push") or permissions.get("admin")
            ):
                writable[full_name.casefold()] = {
                    "full_name": full_name,
                    "default_branch": repo.get("default_branch") or "main",
                }
        url = response.links.get("next", {}).get("url")
    return writable


async def _list_app_reachable_repository_names(
    installations: list[dict[str, Any]],
) -> set[str]:
    from app.github import _github_request, generate_installation_access_token

    reachable: set[str] = set()
    for installation in installations:
        installation_id = installation.get("id")
        if installation_id is None:
            continue
        try:
            token = await generate_installation_access_token(str(installation_id))
        except Exception as exc:
            logger.warning(
                "[GITHUB ONBOARDING] App installation %s token mint failed: %s",
                installation_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Blueprint GitHub App reachability could not be verified.",
            ) from exc
        url: str | None = "https://api.github.com/installation/repositories?per_page=100"
        headers = {"Authorization": f"Bearer {token}", **_GITHUB_ACCEPT_HEADERS}
        while url:
            response = await _github_request("GET", url, headers)
            _raise_if_rate_limited(response)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Blueprint GitHub App reachability could not be verified.",
                )
            for repo in response.json().get("repositories", []):
                full_name = repo.get("full_name")
                if isinstance(full_name, str):
                    reachable.add(full_name.casefold())
            url = response.links.get("next", {}).get("url")
    return reachable


async def list_onboarding_repositories(
    session: AsyncSession, user_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Return caller-writable repositories reachable by every visible App install."""

    _, user_token, _ = await resolve_caller_github_token(session, user_id)
    from app.github import get_user_installations

    installations = await get_user_installations(user_token)
    if not installations:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint GitHub App is not installed for this GitHub identity.",
        )
    writable = await _list_caller_writable_repositories(user_token)
    app_reachable = await _list_app_reachable_repository_names(installations)
    repositories = [
        repo for key, repo in writable.items() if key in app_reachable
    ]
    repositories.sort(key=lambda repo: repo["full_name"].lower())
    return repositories, len(installations)


async def github_onboarding_status(
    session: AsyncSession, user_id: str
) -> dict[str, Any]:
    integration = await get_user_integration(session, user_id)
    credential_kind = integration_credential_kind(integration)
    from app.github import GITHUB_APP_ID, GITHUB_PRIVATE_KEY

    app_configured = bool(GITHUB_APP_ID and GITHUB_PRIVATE_KEY)
    if not integration or not credential_kind:
        return {
            "connected": False,
            "account_name": None,
            "app_configured": app_configured,
            "app_has_installations": False,
            "identity_linked": False,
            "credential_kind": None,
            "app_installation_linked": False,
            "app_installation_reachable": False,
            "installation_count": 0,
        }

    installations = []
    if app_configured:
        _, token, _ = await resolve_caller_github_token(session, user_id)
        from app.github import get_user_installations

        installations = await get_user_installations(token)
    return {
        "connected": True,
        "account_name": integration.github_account_name,
        "app_configured": app_configured,
        "app_has_installations": bool(installations),
        "identity_linked": True,
        "credential_kind": credential_kind,
        "app_installation_linked": bool(integration.github_installation_id),
        "app_installation_reachable": bool(installations),
        "installation_count": len(installations),
    }
