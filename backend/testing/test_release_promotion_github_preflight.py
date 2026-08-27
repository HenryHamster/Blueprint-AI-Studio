"""Promotion GitHub Actions capability preflight regressions."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_preflight_refuses_app_without_actions_write(mocker):
    from app import github
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mint = mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(
            side_effect=github.GitHubInstallationPermissionError(
                "missing actions", missing_permissions=("actions",)
            )
        ),
    )

    token, error, detail = await _preflight_promotion_rerun_token(
        "stale-token",
        "acme",
        "widget",
        credential_source="app",
        installation_id="42",
    )

    assert token is None
    assert error == "github_actions_write_required"
    assert "Actions: Read and write" in detail
    mint.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_reports_non_actions_promotion_permissions(mocker):
    from app import github
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(
            side_effect=github.GitHubInstallationPermissionError(
                "missing contents", missing_permissions=("contents",)
            )
        ),
    )

    token, error, detail = await _preflight_promotion_rerun_token(
        "stale-token", "acme", "widget", credential_source="app"
    )

    assert token is None
    assert error == "github_promotion_permissions_required"
    assert detail.endswith("contents (write)")


@pytest.mark.asyncio
async def test_preflight_remints_token_after_verified_actions_grant(mocker):
    from app.release_promotion import (
        _PROMOTION_GITHUB_APP_PERMISSIONS,
        _preflight_promotion_rerun_token,
    )

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mint = mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(return_value="fresh-token"),
    )

    result = await _preflight_promotion_rerun_token(
        "stale-token",
        "acme",
        "widget",
        credential_source="app",
        installation_id="42",
    )

    assert result == ("fresh-token", None, None)
    mint.assert_awaited_once_with(
        "acme",
        "widget",
        _PROMOTION_GITHUB_APP_PERMISSIONS,
        installation_id="42",
    )


@pytest.mark.asyncio
async def test_preflight_preserves_selected_pat_without_consulting_app(mocker):
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mint = mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(),
    )

    assert await _preflight_promotion_rerun_token(
        "pat-token", "acme", "widget", credential_source="pat"
    ) == ("pat-token", None, None)
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_is_disabled_with_zero_rerun_budget(mocker):
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=0,
    )
    mint = mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions", new=AsyncMock()
    )

    assert await _preflight_promotion_rerun_token(
        "existing-token", "acme", "widget"
    ) == ("existing-token", None, None)
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_fails_closed_when_permission_lookup_is_unknown(mocker):
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(side_effect=RuntimeError("GitHub unavailable")),
    )

    token, error, detail = await _preflight_promotion_rerun_token(
        "existing-token",
        "acme",
        "widget",
        credential_source="app",
    )

    assert token is None
    assert error == "github_actions_permission_preflight_unavailable"
    assert detail == (
        "GitHub App permission preflight failed: RuntimeError: GitHub unavailable"
    )


@pytest.mark.asyncio
async def test_preflight_fails_closed_when_selected_app_installation_is_absent(mocker):
    from app.release_promotion import _preflight_promotion_rerun_token

    mocker.patch(
        "app.release_promotion_ci_rerun._promotion_ci_rerun_budget",
        return_value=1,
    )
    mocker.patch(
        "app.github.mint_repo_installation_token_with_permissions",
        new=AsyncMock(return_value=None),
    )

    token, error, detail = await _preflight_promotion_rerun_token(
        "old-app-token",
        "acme",
        "widget",
        credential_source="app",
    )

    assert token is None
    assert error == "github_actions_permission_preflight_unavailable"
    assert "installation is unavailable" in detail


@pytest.mark.asyncio
async def test_credential_resolver_preserves_pat_after_app_discovery_failure(
    db_session, mocker
):
    from app.models import Project, UserIntegration
    from app.release_promotion import _resolve_github_credential_with_session

    project = Project(
        name="PAT project",
        user_id="pat-user",
        github_owner="acme",
        github_repo="widget",
    )
    integration = UserIntegration(
        user_id="pat-user",
        github_pat="encrypted-secret-ref",
    )
    db_session.add_all([project, integration])
    await db_session.commit()
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=RuntimeError("App mint unavailable")),
    )
    mocker.patch("app.field_encryption.decrypt_field", return_value="secret-ref")
    mocker.patch(
        "app.secrets_manager.get_user_secret",
        new=AsyncMock(return_value="github_pat_selected"),
    )

    credential = await _resolve_github_credential_with_session(db_session, project)

    assert credential is not None
    assert credential.token == "github_pat_selected"
    assert credential.source == "pat"
    assert credential.installation_id is None


@pytest.mark.asyncio
async def test_credential_resolver_retains_stored_installation_identity(
    db_session, mocker
):
    from app.models import Project, UserIntegration
    from app.release_promotion import _resolve_github_credential_with_session

    project = Project(
        name="Stored installation project",
        user_id="app-user",
        github_owner="acme",
        github_repo="widget",
    )
    integration = UserIntegration(
        user_id="app-user",
        github_installation_id="98765",
    )
    db_session.add_all([project, integration])
    await db_session.commit()
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value=None),
    )
    mint = mocker.patch(
        "app.github.generate_installation_access_token",
        new=AsyncMock(return_value="stored-installation-token"),
    )

    credential = await _resolve_github_credential_with_session(db_session, project)

    assert credential is not None
    assert credential.token == "stored-installation-token"
    assert credential.source == "app"
    assert credential.installation_id == "98765"
    mint.assert_awaited_once_with("98765", repositories=["widget"])
