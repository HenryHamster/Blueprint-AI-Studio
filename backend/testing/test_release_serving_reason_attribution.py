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
    deploy_failure_reason = "legacy_unknown_reason"
    deploy_confirm_alerted_at = None


class _AlertSession:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_no_new_revision_reports_backend_artifact_not_workflow(mocker):
    """A stale serving signal identifies the backend artifact, not deploy.yml."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    workflow_lookup = mocker.patch(
        "app.github.get_workflow_run_for_sha",
        new=AsyncMock(side_effect=AssertionError("serving is not workflow-backed")),
    )

    decision = await _check_backend_revision_serving(
        _Session(),
        _Version(),
        _Artifact(),
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert decision == (
        "confirmation_timed_out",
        "backend_revision_serving_unobservable",
        None,
    )
    workflow_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_legacy_alert_uses_generic_recovery(mocker):
    """An unknown legacy row uses generic recovery guidance and keeps its code diagnostic."""
    mocker.patch(
        "app.release_promotion._notify_operator",
        new=AsyncMock(return_value=None),
    )
    session = _AlertSession()

    assert await _alert_promotion_deploy_failed(session, _AlertVersion()) is True

    alert = next(value for value in session.added if isinstance(value, Alert))
    assert "reason=legacy_unknown_reason" in alert.message
    assert "the deploy failure reason is unclassified" in alert.message
    assert "Investigate the deploy / roll forward a fix." in alert.message
    assert "Approve activation" not in alert.message
