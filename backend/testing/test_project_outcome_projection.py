from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.project_outcome_projection import build_project_outcome_projection
from app.routers.admin_projects import get_project_outcome_projection


NOW = datetime(2026, 7, 29, 12, 0, 0)


def _item(**kwargs):
    return SimpleNamespace(**kwargs)


def test_projection_keeps_delivery_axes_separate():
    version_id = uuid4()
    criterion_id = uuid4()
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[("completed", 1, NOW)],
        current_version=_item(
            id=version_id,
            version_number=1,
            status="promoted",
            opened_at=NOW,
        ),
        promoted_version=_item(
            id=version_id,
            version_number=1,
            promoted_sha="abc123",
            promoted_at=NOW,
            serving_status="unconfirmed",
            served_sha=None,
            served_at=None,
            deploy_failure_reason=None,
        ),
        project=None,
        criteria=[_item(id=criterion_id, status="active")],
        evidence=[],
        deploy_artifacts=[
            _item(
                artifact_kind="backend_revision",
                serving_status="unconfirmed",
                served_identifier=None,
                updated_at=NOW,
                served_at=None,
                reason_code=None,
            )
        ],
        now=NOW,
    )

    assert projection["task_activity"]["value"] == "completed_present"
    assert projection["integration"]["value"] == "promoted_present"
    assert projection["deployment"]["value"] == "unconfirmed"
    assert projection["evidence"]["value"] == "unproven"


def test_projection_reports_unknown_and_stale_without_inventing_success():
    criterion_id = uuid4()
    old = NOW - timedelta(days=31)
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=None,
        promoted_version=None,
        project=None,
        criteria=[_item(id=criterion_id, status="active")],
        evidence=[
            _item(
                criterion_id=criterion_id,
                source_kind="task_merge",
                detail={"proven": False},
                recorded_at=old,
            ),
        ],
        deploy_artifacts=[],
        now=NOW,
    )

    assert projection["task_activity"]["value"] == "unknown"
    assert projection["integration"]["value"] == "unknown"
    assert projection["deployment"]["value"] == "not_applicable"
    assert projection["evidence"]["value"] == "stale"
    assert projection["evidence"]["freshness"] == "stale"


def test_inactive_criterion_evidence_cannot_freshen_active_evidence():
    active_id = uuid4()
    inactive_id = uuid4()
    old = NOW - timedelta(days=31)
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=_item(
            id=uuid4(),
            version_number=1,
            status="open",
            opened_at=old,
        ),
        promoted_version=None,
        project=None,
        criteria=[
            _item(id=active_id, status="active"),
            _item(id=inactive_id, status="retracted"),
        ],
        evidence=[
            _item(
                criterion_id=active_id,
                source_kind="task_merge",
                detail={"proven": False},
                recorded_at=old,
            ),
            _item(
                criterion_id=inactive_id,
                source_kind="task_merge",
                detail={"proven": True},
                recorded_at=NOW,
            ),
        ],
        deploy_artifacts=[],
        now=NOW,
    )

    assert projection["evidence"]["value"] == "stale"
    assert projection["evidence"]["source_timestamp"] == old.isoformat()


def test_legacy_serving_scalar_has_truthful_freshness_and_participating_count():
    served_at = NOW - timedelta(hours=2)
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=None,
        promoted_version=_item(
            id=uuid4(),
            version_number=1,
            promoted_sha="legacy-sha",
            promoted_at=NOW - timedelta(hours=3),
            serving_status="serving_confirmed",
            served_sha="legacy-sha",
            served_at=served_at,
            deploy_failure_reason=None,
        ),
        project=None,
        criteria=[],
        evidence=[],
        deploy_artifacts=[],
        now=NOW,
    )

    assert projection["deployment"]["value"] == "serving_confirmed"
    assert projection["deployment"]["freshness"] == "current"
    assert projection["deployment"]["source"] == "release_artifact_registry"
    assert projection["deployment"]["source_timestamp"] == served_at.isoformat()
    assert projection["deployment"]["participating_required_artifact_count"] == 1


def test_deployment_freshness_uses_oldest_participating_required_signal():
    old = NOW - timedelta(days=31)
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=None,
        promoted_version=_item(
            id=uuid4(),
            version_number=1,
            promoted_sha="serving-sha",
            promoted_at=old,
            serving_status="serving_confirmed",
            served_sha="serving-sha",
            served_at=old,
            deploy_failure_reason=None,
        ),
        project=None,
        criteria=[],
        evidence=[],
        deploy_artifacts=[
            _item(
                artifact_kind="backend_revision",
                serving_status="serving_confirmed",
                served_identifier="serving-sha",
                updated_at=old,
                served_at=old,
                reason_code=None,
            ),
            _item(
                artifact_kind="promotion_merge",
                serving_status="serving_confirmed",
                served_identifier="serving-sha",
                updated_at=NOW,
                served_at=NOW,
                reason_code=None,
            ),
        ],
        now=NOW,
    )

    assert projection["deployment"]["value"] == "serving_confirmed"
    assert projection["deployment"]["freshness"] == "stale"
    assert projection["deployment"]["source_timestamp"] == old.isoformat()
    assert projection["deployment"]["participating_required_artifact_count"] == 2


def test_new_open_version_does_not_hide_last_promoted_serving_truth():
    promoted_id = uuid4()
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=_item(
            id=uuid4(),
            version_number=3,
            status="open",
            opened_at=NOW,
        ),
        promoted_version=_item(
            id=promoted_id,
            version_number=2,
            promoted_sha="serving-sha",
            promoted_at=NOW - timedelta(hours=1),
            serving_status="serving_confirmed",
            served_sha="serving-sha",
            served_at=NOW,
            deploy_failure_reason=None,
        ),
        project=None,
        criteria=[],
        evidence=[],
        deploy_artifacts=[
            _item(
                artifact_kind="backend_revision",
                serving_status="serving_confirmed",
                served_identifier="serving-sha",
                updated_at=NOW,
                served_at=NOW,
                reason_code=None,
            )
        ],
        now=NOW,
    )

    assert projection["integration"]["current_version"]["version_number"] == 3
    assert projection["integration"]["latest_promoted"] == {
        "version_id": str(promoted_id),
        "version_number": 2,
        "commit_sha": "serving-sha",
        "promoted_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    assert projection["deployment"]["value"] == "serving_confirmed"


@pytest.mark.asyncio
async def test_endpoint_checks_project_access_before_reading_projection_facts():
    project_id = uuid4()
    session = _item(execute=AsyncMock())
    denied = HTTPException(status_code=404, detail="Project not found")

    with patch(
        "app.routers.admin_projects._get_user_project",
        new=AsyncMock(side_effect=denied),
    ) as access_check:
        with pytest.raises(HTTPException) as exc_info:
            await get_project_outcome_projection(
                request=_item(),
                project_id=project_id,
                session=session,
                current_user={"user_id": "other-user"},
            )

    assert exc_info.value is denied
    access_check.assert_awaited_once_with(project_id, "other-user", session)
    session.execute.assert_not_awaited()
