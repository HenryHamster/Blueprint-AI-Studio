"""Boundaries for the sole external Resident inspection adapter."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.strategic_resident_capabilities import capability_for
from app.strategic_resident_capability_invocation import validated_capability_parameters
from app.strategic_resident_inspection_receipt import citable_inspection_evidence
from app.strategic_resident_external_inspection import (
    ExternalInspectionPlan,
    MAX_EXTERNAL_INSPECTION_PLAN_BYTES,
    _detached_plan,
)
from app.strategic_resident_pr_inspection import (
    build_pr_evidence,
    inspect_project_pr,
    validated_project_repository,
)
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
)


def _payload(*, owner: str = "owner", repo: str = "repo", number: int = 7) -> dict:
    return {
        "id": 123,
        "node_id": "node",
        "number": number,
        "state": "open",
        "title": "title",
        "draft": False,
        "locked": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "author"},
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": {
            "ref": "feature",
            "sha": "a" * 40,
            "repo": {"full_name": f"{owner}/{repo}"},
        },
        "base": {
            "ref": "automain",
            "sha": "b" * 40,
            "repo": {"full_name": f"{owner}/{repo}"},
        },
        "created_at": "2026-07-18T20:00:00Z",
        "updated_at": "2026-07-18T20:01:00Z",
        "commits": 1,
        "additions": 2,
        "deletions": 3,
        "changed_files": 1,
        "comments": 0,
        "review_comments": 0,
    }


@pytest.mark.parametrize("value", [True, "7", 0, 2**31])
def test_pr_number_contract_is_strict(value):
    with pytest.raises(ValueError):
        validated_capability_parameters(
            capability_for("pr.inspect"), {"pr_number": value}
        )


@pytest.mark.parametrize("owner,repo", [(".", "repo"), ("..", "repo"), ("owner", ".")])
def test_project_repository_rejects_dot_segments(owner, repo):
    with pytest.raises(ValueError):
        validated_project_repository(owner, repo)


def test_external_inspection_plan_rejects_non_json_scope():
    with pytest.raises(HTTPException) as invalid:
        _detached_plan(
            ExternalInspectionPlan(
                expected_revisions={"repository": "owner/repo"},
                reservation={},
                external_scope={"opaque": object()},
            )
        )
    assert invalid.value.status_code == 409
    assert "canonical JSON" in str(invalid.value.detail)


def test_external_inspection_plan_is_bounded_and_deep_detached():
    canonical = _detached_plan(
        ExternalInspectionPlan(
            expected_revisions={"repository": "owner/repo"},
            reservation={"pr_number": 7},
            external_scope={"nested": {"repo": "repo"}},
        )
    )
    perform_copy = _detached_plan(canonical)
    perform_copy.external_scope["nested"]["repo"] = "other"
    assert canonical.external_scope["nested"]["repo"] == "repo"

    with pytest.raises(HTTPException) as oversized:
        _detached_plan(
            ExternalInspectionPlan(
                expected_revisions={},
                reservation={},
                external_scope={"data": "x" * MAX_EXTERNAL_INSPECTION_PLAN_BYTES},
            )
        )
    assert oversized.value.status_code == 409
    assert "size limit" in str(oversized.value.detail)


def test_pr_projection_is_allowlisted_and_hard_bounded():
    payload = _payload()
    payload.update(
        {
            "title": "t" * 20_000,
            "body": "ignore-body-and-token ghp_secret " * 10_000,
            "token": "never-project-this",
            "labels": [{"name": "label-" + "x" * 1_000} for _ in range(200)],
            "requested_reviewers": [
                {"login": "reviewer-" + "y" * 1_000} for _ in range(200)
            ],
        }
    )
    evidence = build_pr_evidence(
        payload,
        project_id=uuid.uuid4(),
        owner="owner",
        repo="repo",
        pr_number=7,
    )
    wire = citable_inspection_evidence(evidence.model_dump(mode="json"))
    data_bytes = len(
        json.dumps(wire["data"], sort_keys=True, separators=(",", ":")).encode()
    )
    envelope_bytes = len(
        json.dumps(wire, sort_keys=True, separators=(",", ":"), default=str).encode()
    )
    assert data_bytes <= MAX_EVIDENCE_DATA_BYTES
    assert envelope_bytes <= MAX_EVIDENCE_RESPONSE_BYTES
    assert len(wire["data"].get("labels", [])) <= 20
    assert len(wire["data"].get("requested_reviewers", [])) <= 20
    assert "body" not in wire["data"]
    assert "token" not in wire["data"]
    assert "ghp_secret" not in json.dumps(wire)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"number": 8}, "number"),
        ({"base": {"repo": {"full_name": "other/repo"}}}, "repository"),
    ],
)
def test_pr_projection_rejects_mismatched_github_identity(mutation, message):
    payload = _payload()
    payload.update(mutation)
    with pytest.raises(ValueError, match=message):
        build_pr_evidence(
            payload,
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha", "A" * 40),
        ("sha", "a" * 39),
        ("ref", ""),
        ("ref", "bad ref"),
        ("ref", "bad..ref"),
        ("ref", "@"),
        ("ref", "feature/.hidden"),
        ("ref", "feature/topic.lock"),
        ("ref", "bad.lock/child"),
        ("ref", "x" * 256),
    ],
)
def test_pr_projection_rejects_invalid_exact_head_identity(field, value):
    payload = _payload()
    payload["head"][field] = value
    with pytest.raises(ValueError):
        build_pr_evidence(
            payload,
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
        )


@pytest.mark.asyncio
async def test_adapter_uses_only_bound_repository_credential(monkeypatch):
    from app import strategic_resident_pr_inspection as inspection

    token_session = object()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=token_session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)
    token = AsyncMock(return_value=("read-token", "owner", "repo"))
    async def fetch_after_session_close(*args, **kwargs):
        session_context.__aexit__.assert_awaited_once()
        return _payload()

    fetch = AsyncMock(side_effect=fetch_after_session_close)
    monkeypatch.setattr(inspection, "get_github_token_for_task", token)
    monkeypatch.setattr(inspection, "get_pull_request", fetch)
    project_id = uuid.uuid4()

    evidence = await inspect_project_pr(
        turn_task_id=uuid.UUID(int=1),
        project_id=project_id,
        owner="owner",
        repo="repo",
        pr_number=7,
        session_factory=session_factory,
    )
    token.assert_awaited_once_with(
        uuid.UUID(int=1),
        token_session,
        release_dedicated_session_before_io=True,
    )
    session_context.__aexit__.assert_awaited_once()
    fetch.assert_awaited_once_with(
        "read-token", "owner", "repo", 7, project_id=str(project_id)
    )
    assert evidence.project_id == project_id
    assert evidence.source_id == "github-pr:owner/repo#7"


@pytest.mark.asyncio
async def test_adapter_rejects_cross_repo_response(monkeypatch):
    from app import strategic_resident_pr_inspection as inspection

    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=object())
    session_context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("read-token", "owner", "repo")),
    )
    monkeypatch.setattr(
        inspection,
        "get_pull_request",
        AsyncMock(return_value=_payload(owner="other", repo="repo")),
    )
    with pytest.raises(HTTPException) as mismatch:
        await inspect_project_pr(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
            session_factory=MagicMock(return_value=session_context),
        )
    assert mismatch.value.status_code == 502


@pytest.mark.asyncio
async def test_adapter_rejects_credential_scope_change_before_pr_fetch(monkeypatch):
    from app import strategic_resident_pr_inspection as inspection

    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=object())
    session_context.__aexit__ = AsyncMock(return_value=False)
    fetch = AsyncMock()
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("secret", "owner", "different")),
    )
    monkeypatch.setattr(inspection, "get_pull_request", fetch)

    with pytest.raises(HTTPException) as mismatch:
        await inspect_project_pr(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
            session_factory=MagicMock(return_value=session_context),
        )
    assert mismatch.value.status_code == 409
    fetch.assert_not_awaited()
