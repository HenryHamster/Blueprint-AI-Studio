"""Regression tests for /api/deploy project creation."""

import pytest
from unittest.mock import AsyncMock, patch


class TestDeployBranchColumnInit:
    @pytest.mark.asyncio
    async def test_deploy_calls_backfill_branch_columns(self, client, db_session):
        """POST /api/deploy must call backfill_branch_columns on the new project.

        Regression: projects created via the deploy endpoint used to skip the
        branch-column initialization step, causing integration_branch /
        promotion_target to remain NULL. Any base-branch resolution then
        silently fell back to 'main', breaking repos whose default branch
        differs (e.g. 'master', 'trunk', 'develop').
        """
        backfill_result = {
            "filled": 2,
            "skipped": 0,
            "errored": 0,
            "self_repo_skipped": 0,
        }

        with patch(
            "app.routers.projects.backfill_branch_columns",
            new_callable=AsyncMock,
            return_value=backfill_result,
        ) as mock_backfill:
            resp = await client.post(
                "/api/deploy",
                json={
                    "name": "test-project",
                    "github_owner": "acme",
                    "github_repo": "myrepo",
                    "payloads": [
                        {"type": "create", "prompt": "hello", "title": "Init task"}
                    ],
                },
            )

        assert resp.status_code == 200
        created_project_id = resp.json()["project_id"]

        mock_backfill.assert_called_once()
        called_project_id = mock_backfill.call_args.kwargs["project_id"]
        assert str(called_project_id) == str(created_project_id)
