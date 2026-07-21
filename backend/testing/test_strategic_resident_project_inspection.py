import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.strategic_resident_project_inspection import (
    inspect_project_pipeline,
    inspect_project_qualification,
)
from app.strategic_resident_task_inspection import MAX_EVIDENCE_RESPONSE_BYTES


PROJECT_ID = uuid.uuid4()
OBSERVED_AT = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _state() -> dict:
    return {
        "generated_at": "2026-07-20T00:00:00Z",
        "deploy": {"status": "healthy", "current_revision": "rev-1"},
        "fleet": {"active": 0, "queued": 0, "evaluating": 0, "parked": 0},
        "version": {
            "version_number": 12,
            "phase": "qualifying",
            "status": "open",
            "phase_changed_at": "2026-07-20T00:00:00Z",
            "in_flight_task_count": 0,
            "qualification_state": {"state": "pending"},
            "qualification_provenance": {"source": "resolver"},
            "dimension_states": [{"dimension": "tests", "state": "passed"}],
            "qualification_attempts": 1,
            "qualification_retry_episode": 0,
            "active_qualification_attempt_id": None,
            "serving": {"status": "not_serving"},
        },
    }


@pytest.mark.asyncio
async def test_pipeline_inspection_is_bounded_and_content_addressed(monkeypatch):
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(id=PROJECT_ID)
    snapshot = SimpleNamespace(to_dict=lambda: _state())
    compute = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(
        "app.strategic_resident_project_inspection.compute_system_state", compute
    )

    evidence = await inspect_project_pipeline(
        session, project_id=PROJECT_ID, observed_at=OBSERVED_AT
    )

    assert evidence.kind == "pipeline"
    assert evidence.data["fleet"]["queued"] == 0
    assert "generated_at" not in evidence.data
    assert evidence.revision == f"pipeline:{evidence.checksum}"
    assert len(json.dumps(evidence.model_dump(mode="json"), default=str).encode()) <= MAX_EVIDENCE_RESPONSE_BYTES
    compute.assert_awaited_once_with(session, PROJECT_ID, include_artifacts=False)


@pytest.mark.asyncio
async def test_qualification_inspection_projects_resolver_state(monkeypatch):
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(id=PROJECT_ID)
    snapshot = SimpleNamespace(to_dict=lambda: _state())
    monkeypatch.setattr(
        "app.strategic_resident_project_inspection.compute_system_state",
        AsyncMock(return_value=snapshot),
    )

    evidence = await inspect_project_qualification(
        session, project_id=PROJECT_ID, observed_at=OBSERVED_AT
    )

    version = evidence.data["open_version"]
    assert evidence.kind == "qualification"
    assert version["qualification_state"] == {"state": "pending"}
    assert version["qualification_provenance"] == {"source": "resolver"}
    assert version["dimension_states"][0]["state"] == "passed"
    assert "artifacts" not in version
