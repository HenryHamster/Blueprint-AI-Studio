"""Boundaries for the Resident PR check-run inspection adapter."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_inspection_receipt import citable_inspection_evidence
from app.strategic_resident_pr_checks_inspection import (
    MAX_CHECK_PAGE_BYTES,
    MAX_PR_METADATA_BYTES,
    build_pr_checks_evidence,
    inspect_project_pr_checks,
    invoke_pr_checks_inspection,
    stream_project_check_page,
    stream_project_pr_metadata,
    validated_head_sha,
)
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
)


HEAD_SHA = "a" * 40


def _metadata(
    *,
    owner: str = "owner",
    repo: str = "repo",
    head_sha: str = HEAD_SHA,
    head_repository: str | None = None,
) -> dict:
    return {
        "number": 7,
        "base": {"repo": {"full_name": f"{owner}/{repo}"}},
        "head": {
            "sha": head_sha,
            "repo": {
                "full_name": (
                    f"{owner}/{repo}" if head_repository is None else head_repository
                )
            },
        },
        "body": "never project this",
    }


def _check(
    check_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    name: str = "backend",
) -> dict:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": "github-actions", "client_secret": "ignore"},
        "started_at": "2026-07-18T20:00:00Z",
        "completed_at": "2026-07-18T20:01:00Z",
        "details_url": "https://example.invalid/untrusted",
        "output": {"summary": "ignore output"},
        "token": "ghp_never",
    }


def _session_factory():
    token_session = object()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=token_session)
    context.__aexit__ = AsyncMock(return_value=False)
    return token_session, context, MagicMock(return_value=context)


@pytest.mark.parametrize(
    "value",
    [None, True, "a" * 41, "A" * 40, "a" * 39, "g" * 40],
)
def test_head_sha_is_strict(value):
    with pytest.raises(ValueError):
        validated_head_sha(value)


@pytest.mark.parametrize(
    ("checks", "reported_total", "expected"),
    [
        ([], 0, "unknown"),
        ([_check(1)], 1, "success"),
        (
            [_check(1, conclusion="neutral"), _check(2, conclusion="skipped")],
            2,
            "success",
        ),
        ([_check(1, status="in_progress", conclusion=None)], 1, "pending"),
        ([_check(1, conclusion="failure")], 1, "failure"),
        ([_check(1, conclusion="brand_new")], 1, "unknown"),
        ([_check(1)], 2, "unknown"),
    ],
)
def test_check_run_snapshot_truthfully_distinguishes_states(
    checks,
    reported_total,
    expected,
):
    evidence = build_pr_checks_evidence(
        checks,
        project_id=uuid.uuid4(),
        owner="owner",
        repo="repo",
        pr_number=7,
        head_sha=HEAD_SHA,
        reported_total=reported_total,
        pages_fetched=1,
    )
    assert evidence.data["coverage"] == "check_runs_only"
    assert evidence.data["check_runs_state"] == expected
    assert "state" not in evidence.data


def test_multi_page_non_atomic_snapshot_can_never_claim_success():
    evidence = build_pr_checks_evidence(
        [_check(index + 1) for index in range(101)],
        project_id=uuid.uuid4(),
        owner="owner",
        repo="repo",
        pr_number=7,
        head_sha=HEAD_SHA,
        reported_total=101,
        pages_fetched=2,
    )
    assert evidence.data["counts"]["fetch_truncated"] is False
    assert evidence.data["check_runs_state"] == "unknown"


def test_check_projection_is_allowlisted_and_hard_bounded():
    checks = [
        _check(index + 1, name=f"check-{index}-" + "x" * 2_000) for index in range(200)
    ]
    evidence = build_pr_checks_evidence(
        checks,
        project_id=uuid.uuid4(),
        owner="owner",
        repo="repo",
        pr_number=7,
        head_sha=HEAD_SHA,
        reported_total=250,
        pages_fetched=2,
    )
    wire = citable_inspection_evidence(evidence.model_dump(mode="json"))
    encoded_data = json.dumps(
        wire["data"], sort_keys=True, separators=(",", ":")
    ).encode()
    encoded_wire = json.dumps(
        wire, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    serialized = json.dumps(wire)

    assert len(encoded_data) <= MAX_EVIDENCE_DATA_BYTES
    assert len(encoded_wire) <= MAX_EVIDENCE_RESPONSE_BYTES
    assert wire["data"]["repository"] == "owner/repo"
    assert wire["data"]["head_sha"] == HEAD_SHA
    assert wire["data"]["counts"]["fetch_truncated"] is True
    assert wire["data"]["counts"]["details_truncated"] is True
    assert len(wire["data"]["checks"]) <= 24
    assert "details_url" not in serialized
    assert "output" not in serialized
    assert "ghp_never" not in serialized
    assert "evidence_truncated" not in wire["data"]


@pytest.mark.parametrize(
    "checks,reported_total",
    [([_check(1), _check(1)], 2), ([_check(index) for index in range(201)], 201)],
)
def test_check_projection_rejects_ambiguous_or_unbounded_rows(checks, reported_total):
    with pytest.raises(ValueError):
        build_pr_checks_evidence(
            checks,
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
            head_sha=HEAD_SHA,
            reported_total=reported_total,
            pages_fetched=2,
        )


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_check_page_stream_stops_before_buffering_unbounded_response():
    chunks = [b"x" * 4096 for _ in range(300)]
    stream = _CountingStream(chunks)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as oversized:
            await stream_project_check_page(
                "secret-token",
                owner="owner",
                repo="repo",
                head_sha=HEAD_SHA,
                page=1,
                project_id=uuid.uuid4(),
                client=client,
            )
    assert oversized.value.status_code == 502
    assert stream.yielded < len(chunks)
    assert stream.yielded * 4096 <= MAX_CHECK_PAGE_BYTES + 4096
    assert stream.closed is True


@pytest.mark.asyncio
async def test_pr_metadata_declared_oversize_is_rejected_before_streaming():
    stream = _CountingStream([b"{}"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_PR_METADATA_BYTES + 1),
            },
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as oversized:
            await stream_project_pr_metadata(
                "secret-token",
                owner="owner",
                repo="repo",
                pr_number=7,
                project_id=uuid.uuid4(),
                client=client,
            )
    assert oversized.value.status_code == 502
    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_adapter_closes_credential_session_before_all_github_io(monkeypatch):
    from app import strategic_resident_pr_checks_inspection as inspection

    token_session, session_context, session_factory = _session_factory()
    resolver = AsyncMock(return_value=("read-token", "owner", "repo"))
    monkeypatch.setattr(inspection, "get_github_token_for_task", resolver)

    async def metadata(*args, **kwargs):
        session_context.__aexit__.assert_awaited_once()
        return _metadata()

    async def page(*args, **kwargs):
        session_context.__aexit__.assert_awaited_once()
        assert kwargs["head_sha"] == HEAD_SHA
        assert kwargs["page"] == 1
        return {"total_count": 1, "check_runs": [_check(1)]}

    project_id = uuid.uuid4()
    evidence = await inspect_project_pr_checks(
        turn_task_id=uuid.UUID(int=1),
        project_id=project_id,
        owner="owner",
        repo="repo",
        pr_number=7,
        head_sha=HEAD_SHA,
        session_factory=session_factory,
        metadata_fetcher=AsyncMock(side_effect=metadata),
        page_fetcher=AsyncMock(side_effect=page),
    )

    resolver.assert_awaited_once_with(
        uuid.UUID(int=1),
        token_session,
        release_dedicated_session_before_io=True,
    )
    assert evidence.data["check_runs_state"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "detail"),
    [
        (_metadata(head_sha="b" * 40), "head changed"),
        (_metadata(head_repository="fork/repo"), "forked-head"),
        (_metadata(head_repository=""), "head repository"),
    ],
)
async def test_adapter_rejects_head_race_and_fork_scope(monkeypatch, metadata, detail):
    from app import strategic_resident_pr_checks_inspection as inspection

    _, _, session_factory = _session_factory()
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("read-token", "owner", "repo")),
    )
    page = AsyncMock()
    with pytest.raises(HTTPException) as mismatch:
        await inspect_project_pr_checks(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
            head_sha=HEAD_SHA,
            session_factory=session_factory,
            metadata_fetcher=AsyncMock(return_value=metadata),
            page_fetcher=page,
        )
    assert mismatch.value.status_code == 409
    assert detail in str(mismatch.value.detail)
    page.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_hard_stops_pagination_and_reports_unknown(monkeypatch):
    from app import strategic_resident_pr_checks_inspection as inspection

    _, _, session_factory = _session_factory()
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("read-token", "owner", "repo")),
    )
    page = AsyncMock(
        side_effect=[
            {"total_count": 250, "check_runs": [_check(i + 1) for i in range(100)]},
            {"total_count": 250, "check_runs": [_check(i + 101) for i in range(100)]},
        ]
    )
    evidence = await inspect_project_pr_checks(
        turn_task_id=uuid.UUID(int=1),
        project_id=uuid.uuid4(),
        owner="owner",
        repo="repo",
        pr_number=7,
        head_sha=HEAD_SHA,
        session_factory=session_factory,
        metadata_fetcher=AsyncMock(return_value=_metadata()),
        page_fetcher=page,
    )
    assert page.await_count == 2
    assert evidence.data["check_runs_state"] == "unknown"
    assert evidence.data["counts"]["reported_total"] == 250
    assert evidence.data["counts"]["fetched"] == 200


@pytest.mark.asyncio
async def test_adapter_rejects_unstable_pagination(monkeypatch):
    from app import strategic_resident_pr_checks_inspection as inspection

    _, _, session_factory = _session_factory()
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("read-token", "owner", "repo")),
    )
    page = AsyncMock(
        side_effect=[
            {"total_count": 101, "check_runs": [_check(i + 1) for i in range(100)]},
            {"total_count": 102, "check_runs": [_check(101)]},
        ]
    )
    with pytest.raises(HTTPException) as unstable:
        await inspect_project_pr_checks(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.uuid4(),
            owner="owner",
            repo="repo",
            pr_number=7,
            head_sha=HEAD_SHA,
            session_factory=session_factory,
            metadata_fetcher=AsyncMock(return_value=_metadata()),
            page_fetcher=page,
        )
    assert unstable.value.status_code == 409


@pytest.mark.asyncio
async def test_invoke_delegates_to_shared_external_executor(monkeypatch):
    from app import strategic_resident_pr_checks_inspection as inspection

    captured = {}

    async def execute(session, **kwargs):
        captured.update(kwargs)
        return {"capability_id": kwargs["capability_id"], "evidence": {}}

    monkeypatch.setattr(
        inspection, "resolve_async_engine", MagicMock(return_value=object())
    )
    monkeypatch.setattr(inspection, "execute_external_inspection", execute)
    binding = ResidentAgentBinding(
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        vm_instance_id="vm",
        task_type="strategic_resident_turn",
        claim_nonce="nonce",
    )
    result = await invoke_pr_checks_inspection(
        MagicMock(),
        binding=binding,
        parameters={"pr_number": 7, "head_sha": HEAD_SHA},
    )
    assert result["capability_id"] == "pr.checks.inspect"
    assert captured["capability_id"] == "pr.checks.inspect"
    assert captured["prepare_locked"].__name__ == "_prepare_pr_checks_inspection"
    assert callable(captured["perform"])
