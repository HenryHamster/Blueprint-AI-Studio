"""Regression tests for the optional worker-fleet freshness dimension."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.release_artifacts import (
    AGENT_IMAGE_ARTIFACT_KIND,
    BACKEND_REVISION_ARTIFACT_KIND,
    PROMOTION_MERGE_ARTIFACT_KIND,
    summarize_agent_image_freshness,
    summarize_release_artifacts,
)
from app.project_outcome_projection import build_project_outcome_projection
from app.routers.admin_release_version import _compose_serving_block


_SHA = "a" * 40


def _artifact(kind: str, status: str, **kwargs):
    return SimpleNamespace(
        artifact_kind=kind,
        serving_status=status,
        served_identifier=kwargs.get("served_identifier"),
        served_at=kwargs.get("served_at"),
        reason_code=kwargs.get("reason_code"),
    )


def _version():
    return SimpleNamespace(
        id="version-1",
        version_number=1,
        promoted_sha=_SHA,
        serving_status="serving_confirmed",
        served_sha=_SHA,
        served_at=None,
        deploy_failure_reason=None,
    )


def test_stale_fleet_image_is_reported_without_blocking_serving():
    """A stale optional image is visible, while required serving stays green."""
    stale = _artifact(
        AGENT_IMAGE_ARTIFACT_KIND,
        "confirmation_timed_out",
        reason_code="agent_image_fleet_not_yet_serving",
    )
    aggregate, evidence = summarize_release_artifacts(
        _version(),
        [
            _artifact(
                BACKEND_REVISION_ARTIFACT_KIND,
                "serving_confirmed",
                served_identifier=_SHA,
            ),
            _artifact(
                PROMOTION_MERGE_ARTIFACT_KIND,
                "serving_confirmed",
                served_identifier=_SHA,
            ),
            stale,
        ],
    )

    assert aggregate["serving_status"] == "serving_confirmed"
    assert aggregate["reason_code"] is None
    assert aggregate["fleet_freshness"] == {
        "artifact_kind": AGENT_IMAGE_ARTIFACT_KIND,
        "freshness": "stale",
        "serving_status": "confirmation_timed_out",
        "served_identifier": None,
        "served_at": None,
        "reason_code": "agent_image_fleet_not_yet_serving",
    }
    assert next(
        row for row in evidence if row["artifact_kind"] == AGENT_IMAGE_ARTIFACT_KIND
    )["required"] is False


def test_fleet_freshness_is_independently_gradeable():
    confirmed_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    report = summarize_agent_image_freshness(
        _artifact(
            AGENT_IMAGE_ARTIFACT_KIND,
            "serving_confirmed",
            served_identifier=_SHA,
            served_at=confirmed_at,
        )
    )

    assert report["freshness"] == "current"
    assert report["served_identifier"] == _SHA
    assert report["served_at"] == confirmed_at.isoformat()


def test_release_serving_read_model_surfaces_fleet_dimension():
    stale = _artifact(
        AGENT_IMAGE_ARTIFACT_KIND,
        "confirmation_timed_out",
        reason_code="agent_image_fleet_not_yet_serving",
    )
    aggregate, evidence = summarize_release_artifacts(
        _version(),
        [
            _artifact(BACKEND_REVISION_ARTIFACT_KIND, "serving_confirmed"),
            _artifact(PROMOTION_MERGE_ARTIFACT_KIND, "serving_confirmed"),
            stale,
        ],
    )

    serving = _compose_serving_block(aggregate, evidence, _version())

    assert serving["status"] == "serving_confirmed"
    assert serving["fleet_freshness"]["freshness"] == "stale"


def test_project_outcome_read_model_surfaces_fleet_dimension():
    stale = _artifact(
        AGENT_IMAGE_ARTIFACT_KIND,
        "confirmation_timed_out",
        reason_code="agent_image_fleet_not_yet_serving",
    )
    projection = build_project_outcome_projection(
        project_id="p1",
        task_facts=[],
        current_version=None,
        promoted_version=_version(),
        project=None,
        criteria=[],
        evidence=[],
        deploy_artifacts=[
            _artifact(BACKEND_REVISION_ARTIFACT_KIND, "serving_confirmed"),
            _artifact(PROMOTION_MERGE_ARTIFACT_KIND, "serving_confirmed"),
            stale,
        ],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert projection["deployment"]["value"] == "serving_confirmed"
    assert projection["deployment"]["fleet_freshness"]["freshness"] == "stale"


def test_missing_fleet_observation_is_unknown_not_stale():
    report = summarize_agent_image_freshness(None)

    assert report["freshness"] == "unknown"
    assert report["serving_status"] is None
