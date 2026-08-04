import uuid
from unittest.mock import AsyncMock

import pytest

from app import release_qualifier
from app.models import ReleaseVersion
from app.release_qualifier_audit import _spawn_release_audit_task


class _RecordingSession:
    def __init__(self, locked_version=None) -> None:
        self.added = []
        self.locked_version = locked_version

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement):
        class _Result:
            def __init__(self, value):
                self.value = value

            def scalars(self):
                return self

            def first(self):
                return self.value

        return _Result(self.locked_version)


@pytest.mark.asyncio
async def test_fresh_qualification_requires_strategic_or_operator_request():
    project_id = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project_id,
        version_number=1,
        phase="stabilize",
    )

    result = await release_qualifier.run_qualification(
        _RecordingSession(),  # type: ignore[arg-type]
        project_id,
        version,
        explicit_request=False,
    )

    assert result == {"status": "explicit_request_required"}


@pytest.mark.asyncio
async def test_promotion_disabled_returns_cheap_external_blocker(monkeypatch):
    project_id = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project_id,
        version_number=1,
        phase="stabilize",
    )
    session = _RecordingSession(version)
    monkeypatch.setattr(
        release_qualifier,
        "resolve_project_promotion_enabled",
        lambda _project_id: False,
    )
    monkeypatch.setattr(
        release_qualifier,
        "_qualification_in_flight",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.qualification_state_resolver.resolve_newest_qualification_verdict",
        AsyncMock(return_value=None),
    )

    result = await release_qualifier.run_qualification(
        session,  # type: ignore[arg-type]
        project_id,
        version,
        explicit_request=True,
    )

    assert result == {
        "status": "external_blocker",
        "blocker": "promotion_disabled",
        "blocker_class": "external_delivery",
    }
    assert version.last_qualification["status"] == "external_blocker"
    assert version.last_qualification["blocker"] == "promotion_disabled"
    assert session.added == [version]


@pytest.mark.asyncio
async def test_holistic_review_carries_goal_and_acceptance_contract():
    session = _RecordingSession()
    project_id = uuid.uuid4()
    task = await _spawn_release_audit_task(
        session,  # type: ignore[arg-type]
        project_id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range="main...abc123",
        candidate_sha="abc123",
        qualification_attempt_id=uuid.uuid4(),
        version_goal="Deliver the useful outcome",
        categories=[{"category_key": "outcome", "text": "Outcome works"}],
        rubric_fingerprint="rubric-1",
    )

    payload = task.first_payload
    assert payload["type"] == "release_audit"
    assert payload["version_goal"] == "Deliver the useful outcome"
    assert payload["categories"] == [
        {"category_key": "outcome", "text": "Outcome works"}
    ]
    assert payload["rubric_fingerprint"] == "rubric-1"
    assert "Release review" in task.title
