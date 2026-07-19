"""Focused boundaries for bounded Resident pull-request diff inspection."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_pr_diff_inspection import (
    MAX_DIFF_FILE_IDENTITIES,
    MAX_DIFF_STREAM_BYTES,
    BoundedDiffCapture,
    PRDiffIdentity,
    build_pr_diff_evidence,
    inspect_project_pr_diff,
    invoke_pr_diff_inspection,
    project_pr_diff_identity,
    stream_project_pr_diff,
)
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
)


def _metadata(
    *,
    owner: str = "owner",
    repo: str = "repo",
    pr_number: int = 7,
    base_sha: str = "b" * 40,
    head_sha: str = "a" * 40,
    head_repository: str | None = None,
) -> dict:
    return {
        "number": pr_number,
        "body": "must never be projected",
        "html_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        "base": {
            "ref": "automain",
            "sha": base_sha,
            "repo": {"full_name": f"{owner}/{repo}"},
        },
        "head": {
            "ref": "feature/resident",
            "sha": head_sha,
            "repo": {
                "full_name": (
                    f"{owner}/{repo}" if head_repository is None else head_repository
                )
            },
        },
    }


def _identity() -> PRDiffIdentity:
    return PRDiffIdentity(
        repository="owner/repo",
        pr_number=7,
        base_ref="automain",
        base_sha="b" * 40,
        head_ref="feature/resident",
        head_sha="a" * 40,
    )


@pytest.mark.parametrize(
    ("payload", "status_code", "detail"),
    [
        (_metadata(owner="other"), 502, "base repository"),
        (_metadata(head_repository="fork/repo"), 409, "forked-head"),
        (_metadata(head_repository=""), 409, "head repository"),
        ({**_metadata(), "number": 8}, 502, "number"),
    ],
)
def test_identity_rejects_cross_scope_and_ambiguity(payload, status_code, detail):
    with pytest.raises(HTTPException) as rejected:
        project_pr_diff_identity(
            payload,
            owner="owner",
            repo="repo",
            pr_number=7,
        )
    assert rejected.value.status_code == status_code
    assert detail in str(rejected.value.detail)


def test_bounded_evidence_is_deterministic_exact_and_secret_free():
    diff = (
        b"diff --git a/app.py b/app.py\n"
        b"index 1111111..2222222 100644\n"
        b"--- a/app.py\n+++ b/app.py\n"
        b"+Authorization: Bearer ghp_never-project-this\n"
        b"+print('hello')\n"
    )
    capture = BoundedDiffCapture(
        content=diff,
        content_length=len(diff),
        stream_truncated=False,
    )
    project_id = uuid.uuid4()
    first = build_pr_diff_evidence(
        capture,
        project_id=project_id,
        identity=_identity(),
    )
    second = build_pr_diff_evidence(
        capture,
        project_id=project_id,
        identity=_identity(),
        observed_at=first.observed_at,
    )
    assert first.checksum == second.checksum
    assert first.revision == second.revision
    assert first.source_id == "github-pr-diff:owner/repo#7"
    assert first.data["base"]["sha"] == "b" * 40
    assert first.data["head"]["sha"] == "a" * 40
    assert first.data["files"] == ["app.py"]
    serialized = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    assert "ghp_never-project-this" not in serialized
    assert "must never be projected" not in serialized
    assert "html_url" not in serialized
    assert "Authorization=[redacted]" in serialized
    assert (
        len(json.dumps(first.data, separators=(",", ":")).encode())
        <= MAX_EVIDENCE_DATA_BYTES
    )
    assert len(serialized.encode()) <= MAX_EVIDENCE_RESPONSE_BYTES


def test_evidence_caps_files_and_marks_incomplete_prefix():
    content = b"".join(
        f"diff --git a/file-{index}.py b/file-{index}.py\n+line\n".encode()
        for index in range(MAX_DIFF_FILE_IDENTITIES + 4)
    )
    evidence = build_pr_diff_evidence(
        BoundedDiffCapture(
            content=content,
            content_length=len(content) + 100,
            stream_truncated=True,
        ),
        project_id=uuid.uuid4(),
        identity=_identity(),
    )
    assert len(evidence.data["files"]) == MAX_DIFF_FILE_IDENTITIES
    assert evidence.data["files_observed_in_prefix"] == MAX_DIFF_FILE_IDENTITIES + 4
    assert evidence.data["files_truncated"] is True
    assert evidence.data["stream_truncated"] is True


def test_hostile_escaping_shrinks_preview_without_losing_exact_identity():
    content = (b'"\\' * 1_000) + b"".join(
        (f"\ndiff --git a/{'x' * 100}-{index} b/{'x' * 100}-{index}\n").encode()
        for index in range(MAX_DIFF_FILE_IDENTITIES)
    )
    evidence = build_pr_diff_evidence(
        BoundedDiffCapture(
            content=content,
            content_length=len(content),
            stream_truncated=False,
        ),
        project_id=uuid.uuid4(),
        identity=_identity(),
    )
    assert evidence.data["base"]["sha"] == "b" * 40
    assert evidence.data["head"]["sha"] == "a" * 40
    assert evidence.data["diff_prefix_sha256"]
    assert evidence.data["patch_preview_truncated"] is True
    assert len(json.dumps(evidence.data, separators=(",", ":")).encode()) <= 4_200


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
async def test_stream_fetch_stops_at_hard_cap_without_buffering_full_diff():
    chunks = [b"x" * 4096 for _ in range(100)]
    stream = _CountingStream(chunks)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["Accept"] == "application/vnd.github.diff"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.url.path == (
            f"/repos/owner/repo/compare/{'b' * 40}...{'a' * 40}"
        )
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.github.v3.diff",
                "Content-Length": str(sum(map(len, chunks))),
            },
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        capture = await stream_project_pr_diff(
            token="secret-token",
            owner="owner",
            repo="repo",
            pr_number=7,
            base_sha="b" * 40,
            head_sha="a" * 40,
            project_id=uuid.uuid4(),
            client=client,
        )
    assert len(capture.content) == MAX_DIFF_STREAM_BYTES
    assert capture.stream_truncated is True
    assert capture.content_length == sum(map(len, chunks))
    assert stream.yielded < len(chunks)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_stream_401_invalidates_exact_project_credential(monkeypatch):
    from app import strategic_resident_pr_diff_inspection as inspection

    invalidated = MagicMock()
    monkeypatch.setattr(inspection, "invalidate_github_token_cache", invalidated)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    project_id = uuid.uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as unauthorized:
            await stream_project_pr_diff(
                token="expired",
                owner="owner",
                repo="repo",
                pr_number=7,
                base_sha="b" * 40,
                head_sha="a" * 40,
                project_id=project_id,
                client=client,
            )
    assert unauthorized.value.status_code == 401
    invalidated.assert_called_once_with(str(project_id))


@pytest.mark.asyncio
async def test_inspector_closes_credential_session_before_all_github_io(monkeypatch):
    from app import strategic_resident_pr_diff_inspection as inspection

    token_session = object()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=token_session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)
    token = AsyncMock(return_value=("credential", "owner", "repo"))
    monkeypatch.setattr(inspection, "get_github_token_for_task", token)

    async def metadata(*args, **kwargs):
        session_context.__aexit__.assert_awaited_once()
        assert kwargs["project_id"] == uuid.UUID(int=2)
        assert kwargs["consumer"] == "strategic_resident_pr_diff_metadata"
        return _metadata()

    async def stream(**kwargs):
        session_context.__aexit__.assert_awaited_once()
        assert kwargs["token"] == "credential"
        assert kwargs["base_sha"] == "b" * 40
        assert kwargs["head_sha"] == "a" * 40
        return BoundedDiffCapture(
            content=b"diff --git a/a.py b/a.py\n",
            content_length=29,
            stream_truncated=False,
        )

    evidence = await inspect_project_pr_diff(
        turn_task_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        owner="owner",
        repo="repo",
        pr_number=7,
        session_factory=session_factory,
        metadata_fetcher=AsyncMock(side_effect=metadata),
        diff_streamer=AsyncMock(side_effect=stream),
    )
    token.assert_awaited_once_with(
        uuid.UUID(int=1),
        token_session,
        release_dedicated_session_before_io=True,
    )
    session_context.__aexit__.assert_awaited_once()
    assert evidence.data["pr_number"] == 7


@pytest.mark.asyncio
async def test_inspector_sha_pins_stream_when_pr_metadata_has_an_aba_race(
    monkeypatch,
):
    from app import strategic_resident_pr_diff_inspection as inspection

    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=object())
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)
    pinned_base = "b" * 40
    pinned_head = "a" * 40
    metadata = AsyncMock(
        side_effect=[
            _metadata(base_sha=pinned_base, head_sha=pinned_head),
            # The mutable PR may have moved away and back while the exact
            # compare read ran. Equal boundary metadata cannot detect that ABA,
            # so correctness must come from the SHAs passed to the streamer.
            _metadata(base_sha=pinned_base, head_sha=pinned_head),
        ]
    )

    async def pinned_stream(**kwargs):
        assert kwargs["base_sha"] == pinned_base
        assert kwargs["head_sha"] == pinned_head
        content = b"diff --git a/pinned.py b/pinned.py\n+pinned\n"
        return BoundedDiffCapture(
            content=content,
            content_length=len(content),
            stream_truncated=False,
        )

    stream = AsyncMock(side_effect=pinned_stream)
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("credential", "owner", "repo")),
    )

    evidence = await inspect_project_pr_diff(
        turn_task_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        owner="owner",
        repo="repo",
        pr_number=7,
        session_factory=session_factory,
        metadata_fetcher=metadata,
        diff_streamer=stream,
    )

    assert metadata.await_count == 2
    stream.assert_awaited_once()
    assert evidence.data["base"]["sha"] == pinned_base
    assert evidence.data["head"]["sha"] == pinned_head
    assert "pinned" in evidence.data["patch_preview"]


@pytest.mark.asyncio
async def test_inspector_rejects_credential_scope_and_branch_races(monkeypatch):
    from app import strategic_resident_pr_diff_inspection as inspection

    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=object())
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)
    metadata = AsyncMock(side_effect=[_metadata(), _metadata(head_sha="c" * 40)])
    stream = AsyncMock(
        return_value=BoundedDiffCapture(
            content=b"diff --git a/a b/a\n",
            content_length=20,
            stream_truncated=False,
        )
    )
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("credential", "owner", "repo")),
    )
    with pytest.raises(HTTPException) as changed:
        await inspect_project_pr_diff(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.UUID(int=2),
            owner="owner",
            repo="repo",
            pr_number=7,
            session_factory=session_factory,
            metadata_fetcher=metadata,
            diff_streamer=stream,
        )
    assert "branch identity changed" in str(changed.value.detail)

    metadata.reset_mock()
    stream.reset_mock()
    monkeypatch.setattr(
        inspection,
        "get_github_token_for_task",
        AsyncMock(return_value=("credential", "other", "repo")),
    )
    with pytest.raises(HTTPException) as changed:
        await inspect_project_pr_diff(
            turn_task_id=uuid.UUID(int=1),
            project_id=uuid.UUID(int=2),
            owner="owner",
            repo="repo",
            pr_number=7,
            session_factory=session_factory,
            metadata_fetcher=metadata,
            diff_streamer=stream,
        )
    assert "credential scope changed" in str(changed.value.detail)
    metadata.assert_not_awaited()
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_routes_through_generic_external_executor(monkeypatch):
    from app import strategic_resident_pr_diff_inspection as inspection

    fake_factory = object()
    monkeypatch.setattr(
        inspection, "resolve_async_engine", MagicMock(return_value=object())
    )
    monkeypatch.setattr(
        inspection,
        "async_sessionmaker",
        MagicMock(return_value=fake_factory),
    )

    async def execute(session, **kwargs):
        assert session is sentinel_session
        assert kwargs["capability_id"] == "pr.diff.inspect"
        assert kwargs["parameters"] == {"pr_number": 7}
        assert kwargs["prepare_locked"] is inspection._prepare_pr_diff_inspection
        assert kwargs["authority_change_detail"] == (
            "project repository changed during PR diff inspection"
        )
        plan = inspection.ExternalInspectionPlan(
            expected_revisions={},
            reservation={},
            external_scope={
                "turn_task_id": str(binding.task_id),
                "project_id": str(binding.project_id),
                "owner": "owner",
                "repo": "repo",
                "pr_number": 7,
            },
        )
        evidence = await kwargs["perform"](plan)
        assert evidence.data["pr_number"] == 7
        return {"capability_id": "pr.diff.inspect", "evidence": {}}

    monkeypatch.setattr(inspection, "execute_external_inspection", execute)
    inspector = AsyncMock(
        return_value=build_pr_diff_evidence(
            BoundedDiffCapture(
                content=b"diff --git a/a b/a\n",
                content_length=20,
                stream_truncated=False,
            ),
            project_id=uuid.UUID(int=2),
            identity=_identity(),
        )
    )
    binding = ResidentAgentBinding(
        task_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        vm_instance_id="vm",
        task_type="strategic_resident_turn",
        claim_nonce="nonce",
    )
    sentinel_session = object()
    result = await invoke_pr_diff_inspection(
        sentinel_session,
        binding=binding,
        parameters={"pr_number": 7},
        inspector=inspector,
    )
    assert result["capability_id"] == "pr.diff.inspect"
    inspector.assert_awaited_once_with(
        turn_task_id=binding.task_id,
        project_id=binding.project_id,
        owner="owner",
        repo="repo",
        pr_number=7,
        session_factory=fake_factory,
    )
