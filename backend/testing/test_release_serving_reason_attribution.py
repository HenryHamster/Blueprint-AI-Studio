"""Regression tests for promotion serving-reason attribution."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.models import Alert, DeploymentStatus
from app.release_artifacts import _check_backend_revision_serving
from app.release_stall_alerts import _alert_promotion_deploy_failed


_SHA = "a" * 40


class _Project:
    id = 1
    github_owner = "acme"
    github_repo = "widget"


class _Version:
    project_id = 1
    version_number = 1
    promoted_sha = _SHA
    promoted_at = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    deploy_confirm_deadline = None
    serving_status = "unconfirmed"
    activation_requested_at = None


class _DeploymentState:
    status = DeploymentStatus.STABLE
    current_sha = "b" * 40
    deployed_at = datetime(2026, 1, 1, 9, 55, 0, tzinfo=timezone.utc)


class _Session:
    async def get(self, model, _primary_key):
        if model.__name__ == "DeploymentState":
            return _DeploymentState()
        return _Project()


class _Artifact:
    confirmation_deadline = None


class _AlertVersion:
    project_id = 1
    version_number = 1
    promoted_sha = _SHA
    deploy_failure_reason = "pending_operator_activation"
    deploy_confirm_alerted_at = None


class _AlertSession:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_no_new_revision_reports_pending_operator_activation(mocker):
    """A push-successful workflow did not activate the local control plane."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_workflow_run_for_sha",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
            }
        ),
    )

    decision = await _check_backend_revision_serving(
        _Session(),
        _Version(),
        _Artifact(),
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert decision == (
        "confirmation_timed_out",
        "pending_operator_activation",
        None,
    )


@pytest.mark.asyncio
async def test_pending_activation_alert_names_local_action(mocker):
    """The matching alert does not route the operator to deploy.yml."""
    mocker.patch(
        "app.release_promotion._notify_operator",
        new=AsyncMock(return_value=None),
    )
    session = _AlertSession()

    assert await _alert_promotion_deploy_failed(session, _AlertVersion()) is True

    alert = next(value for value in session.added if isinstance(value, Alert))
    assert "local control plane" in alert.message
    assert "Approve activation" in alert.message
    assert "deploy workflow" not in alert.message
