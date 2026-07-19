"""Bounded, revision-linked pull-request diff evidence for Resident turns."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import resolve_async_engine
from app.github import _get_client, _record_call, _record_consumer_call
from app.github_http import (
    is_rate_limited_response,
    raise_rate_limited,
    retry_after_seconds_from_response,
)
from app.github_token_resolver import (
    get_github_token_for_task,
    invalidate_github_token_cache,
)
from app.models import Project, Task
from app.schemas import redact_command_log_text
from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_external_inspection import (
    ExternalInspectionPlan,
    execute_external_inspection,
)
from app.strategic_resident_github_bounded_read import stream_project_pr_metadata
from app.strategic_resident_inspection_receipt import citable_inspection_evidence
from app.strategic_resident_pr_inspection import validated_project_repository
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    _bounded_evidence_data,
)


MAX_DIFF_STREAM_BYTES = 64 * 1024
MAX_DIFF_FILE_IDENTITIES = 8
MAX_DIFF_PATH_BYTES = 180
MAX_DIFF_PREVIEW_BYTES = 1_600
_MAX_PROJECTED_DIFF_DATA_BYTES = 4_200
_STREAM_CHUNK_BYTES = 4 * 1024
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_REF_CHARS = 255
_PR_DIFF_METADATA_CONSUMER = "strategic_resident_pr_diff_metadata"


class ResidentPRDiffEvidence(BaseModel):
    """One exact, bounded PR diff snapshot."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pr_diff"] = "pr_diff"
    project_id: uuid.UUID
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True)
class PRDiffIdentity:
    repository: str
    pr_number: int
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str


@dataclass(frozen=True)
class BoundedDiffCapture:
    content: bytes
    content_length: int | None
    stream_truncated: bool


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validated_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned an invalid {field} SHA",
        )
    return value


def _validated_ref(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_REF_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in " ~^:?*[\\" for character in value)
        or ".." in value
        or "@{" in value
        or "//" in value
        or value == "@"
        or value.startswith("/")
        or value.endswith(("/", ".", ".lock"))
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in value.split("/")
        )
    ):
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned an invalid {field} ref",
        )
    return value


def _repository_name(value: object) -> str | None:
    repo = value if isinstance(value, Mapping) else {}
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return None
    owner, name = full_name.split("/", 1)
    try:
        owner, name = validated_project_repository(owner, name)
    except ValueError:
        return None
    return f"{owner}/{name}"


def project_pr_diff_identity(
    payload: Mapping[str, Any],
    *,
    owner: str,
    repo: str,
    pr_number: int,
) -> PRDiffIdentity:
    """Correlate one PR response to the exact bound same-repository branch pair."""

    owner, repo = validated_project_repository(owner, repo)
    if type(payload.get("number")) is not int or payload.get("number") != pr_number:
        raise HTTPException(
            status_code=502,
            detail="GitHub returned a mismatched pull request number",
        )
    expected_repository = f"{owner}/{repo}"
    base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
    head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
    base_repository = _repository_name(base.get("repo"))
    head_repository = _repository_name(head.get("repo"))
    if (
        base_repository is None
        or base_repository.casefold() != expected_repository.casefold()
    ):
        raise HTTPException(
            status_code=502,
            detail="GitHub returned a pull request from another base repository",
        )
    if head_repository is None:
        raise HTTPException(
            status_code=409,
            detail="pull request head repository identity is unavailable",
        )
    if head_repository.casefold() != expected_repository.casefold():
        raise HTTPException(
            status_code=409,
            detail="forked-head PR diffs are not supported by bounded Resident inspection",
        )
    return PRDiffIdentity(
        repository=expected_repository,
        pr_number=pr_number,
        base_ref=_validated_ref(base.get("ref"), field="base"),
        base_sha=_validated_sha(base.get("sha"), field="base"),
        head_ref=_validated_ref(head.get("ref"), field="head"),
        head_sha=_validated_sha(head.get("sha"), field="head"),
    )


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("Content-Length")
    if raw is None or len(raw) > 20:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


async def stream_project_pr_diff(
    *,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    project_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
) -> BoundedDiffCapture:
    """Stream one SHA-pinned diff under the hard byte cap plus one byte."""

    owner, repo = validated_project_repository(owner, repo)
    base_sha = _validated_sha(base_sha, field="base")
    head_sha = _validated_sha(head_sha, field="head")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.diff",
        "Accept-Encoding": "identity",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    bounded = bytearray()
    stream_client = client or _get_client()
    try:
        _record_call("GET", url)
        _record_consumer_call("strategic_resident_pr_diff", url)
        async with stream_client.stream(
            "GET",
            url,
            headers=headers,
            timeout=30.0,
        ) as response:
            if response.status_code == 401:
                invalidate_github_token_cache(str(project_id))
                raise HTTPException(
                    status_code=401,
                    detail="GitHub authentication failed while fetching PR diff",
                )
            if response.status_code in {404, 410}:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"PR #{pr_number} diff is unavailable",
                )
            if is_rate_limited_response(response):
                raise_rate_limited(
                    f"[GITHUB] get PR diff #{pr_number} rate limited",
                    f"GitHub rate limit exhausted fetching PR diff #{pr_number}",
                    retry_after_seconds_from_response(response),
                )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"GitHub failed to return PR diff #{pr_number}",
                )
            content_type = response.headers.get("Content-Type", "").casefold()
            if (
                content_type
                and "diff" not in content_type
                and "text/plain" not in content_type
            ):
                raise HTTPException(
                    status_code=502,
                    detail="GitHub returned an invalid PR diff media type",
                )
            content_encoding = response.headers.get("Content-Encoding", "").casefold()
            if content_encoding not in {"", "identity"}:
                raise HTTPException(
                    status_code=502,
                    detail="GitHub returned an encoded PR diff stream",
                )
            declared_length = _content_length(response.headers)
            retain_limit = MAX_DIFF_STREAM_BYTES + (
                0 if declared_length and declared_length > MAX_DIFF_STREAM_BYTES else 1
            )
            async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                remaining = retain_limit - len(bounded)
                if remaining > 0:
                    bounded.extend(chunk[:remaining])
                if len(bounded) >= retain_limit:
                    break
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail="GitHub PR diff stream is temporarily unavailable",
        ) from exc
    captured = bytes(bounded[:MAX_DIFF_STREAM_BYTES])
    truncated = len(bounded) > MAX_DIFF_STREAM_BYTES or (
        declared_length is not None and declared_length > len(captured)
    )
    return BoundedDiffCapture(
        content=captured,
        content_length=declared_length,
        stream_truncated=truncated,
    )


def _bounded_utf8(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _diff_file_paths(content: bytes) -> tuple[list[str], int, bool]:
    paths: list[str] = []
    observed = 0
    for raw_line in content.splitlines():
        if not raw_line.startswith(b"diff --git "):
            continue
        observed += 1
        if len(paths) >= MAX_DIFF_FILE_IDENTITIES:
            continue
        line = raw_line[: 2 * MAX_DIFF_PATH_BYTES + 64].decode(
            "utf-8", errors="replace"
        )
        try:
            fields = shlex.split(line)
        except ValueError:
            fields = []
        if len(fields) != 4 or not fields[3].startswith("b/"):
            path = f"unparsed:{hashlib.sha256(raw_line).hexdigest()}"
        else:
            path = fields[3][2:]
            path = redact_command_log_text(path)
            path = _bounded_utf8(path, max_bytes=MAX_DIFF_PATH_BYTES)
        paths.append(path)
    return paths, observed, observed > len(paths)


def build_pr_diff_evidence(
    capture: BoundedDiffCapture,
    *,
    project_id: uuid.UUID,
    identity: PRDiffIdentity,
    observed_at: datetime | None = None,
) -> ResidentPRDiffEvidence:
    """Build deterministic, bounded evidence from one stable PR diff capture."""

    prefix_checksum = hashlib.sha256(capture.content).hexdigest()
    paths, files_observed, files_truncated = _diff_file_paths(capture.content)
    preview = redact_command_log_text(
        capture.content[:MAX_DIFF_PREVIEW_BYTES].decode("utf-8", errors="replace")
    )
    raw_preview_bytes = len(preview.encode("utf-8"))
    preview = _bounded_utf8(preview, max_bytes=MAX_DIFF_PREVIEW_BYTES)
    data = {
        "repository": identity.repository,
        "pr_number": identity.pr_number,
        "base": {"ref": identity.base_ref, "sha": identity.base_sha},
        "head": {"ref": identity.head_ref, "sha": identity.head_sha},
        "diff_prefix_sha256": prefix_checksum,
        "captured_bytes": len(capture.content),
        "content_length": capture.content_length,
        "stream_limit_bytes": MAX_DIFF_STREAM_BYTES,
        "stream_truncated": capture.stream_truncated,
        "files": paths,
        "files_observed_in_prefix": files_observed,
        "files_truncated": files_truncated or capture.stream_truncated,
        "patch_preview": preview,
        "patch_preview_truncated": (
            len(capture.content) > MAX_DIFF_PREVIEW_BYTES
            or raw_preview_bytes > MAX_DIFF_PREVIEW_BYTES
        ),
    }
    while len(_canonical_json(data)) > _MAX_PROJECTED_DIFF_DATA_BYTES:
        current_preview = str(data["patch_preview"])
        if current_preview:
            preview_bytes = current_preview.encode("utf-8")
            data["patch_preview"] = preview_bytes[
                : max(0, len(preview_bytes) // 2)
            ].decode("utf-8", errors="ignore")
            data["patch_preview_truncated"] = True
            continue
        current_files = data["files"]
        if isinstance(current_files, list) and current_files:
            current_files.pop()
            data["files_truncated"] = True
            continue
        raise HTTPException(
            status_code=409,
            detail="PR diff identity metadata exceeds its evidence budget",
        )
    source_id = f"github-pr-diff:{identity.repository}#{identity.pr_number}"
    revision = (
        f"pr-diff:{identity.pr_number}:{identity.base_sha}:"
        f"{identity.head_sha}:{prefix_checksum}"
    )
    now = observed_at or datetime.now(timezone.utc)

    def build(data_budget: int) -> tuple[ResidentPRDiffEvidence, bytes]:
        bounded, encoded = _bounded_evidence_data(data, budget=data_budget)
        evidence = ResidentPRDiffEvidence(
            project_id=project_id,
            source_id=source_id,
            revision=revision,
            checksum=hashlib.sha256(encoded).hexdigest(),
            observed_at=now,
            data=bounded,
        )
        return evidence, encoded

    evidence, encoded = build(MAX_EVIDENCE_DATA_BYTES)
    complete = _canonical_json(
        citable_inspection_evidence(evidence.model_dump(mode="json"))
    )
    if len(complete) > MAX_EVIDENCE_RESPONSE_BYTES:
        envelope = len(complete) - len(encoded)
        evidence, _ = build(max(0, MAX_EVIDENCE_RESPONSE_BYTES - envelope))
    return evidence


MetadataFetcher = Callable[..., Awaitable[dict[str, Any]]]
DiffStreamer = Callable[..., Awaitable[BoundedDiffCapture]]


async def inspect_project_pr_diff(
    *,
    turn_task_id: uuid.UUID,
    project_id: uuid.UUID,
    owner: str,
    repo: str,
    pr_number: int,
    session_factory: async_sessionmaker[AsyncSession],
    metadata_fetcher: MetadataFetcher = stream_project_pr_metadata,
    diff_streamer: DiffStreamer = stream_project_pr_diff,
) -> ResidentPRDiffEvidence:
    """Fetch one stable, same-repository PR diff without retaining an unbounded body."""

    owner, repo = validated_project_repository(owner, repo)
    async with session_factory() as token_session:
        token, resolved_owner, resolved_repo = await get_github_token_for_task(
            turn_task_id,
            token_session,
            release_dedicated_session_before_io=True,
        )
    if not token:
        raise HTTPException(
            status_code=503,
            detail="project GitHub repository has no available credential",
        )
    try:
        resolved_repository = validated_project_repository(
            resolved_owner,
            resolved_repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if resolved_repository != (owner, repo):
        raise HTTPException(
            status_code=409,
            detail="project repository credential scope changed during PR diff inspection",
        )
    before_payload = await metadata_fetcher(
        token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        project_id=project_id,
        consumer=_PR_DIFF_METADATA_CONSUMER,
    )
    if not isinstance(before_payload, Mapping):
        raise HTTPException(
            status_code=502, detail="GitHub returned invalid PR metadata"
        )
    before = project_pr_diff_identity(
        before_payload,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
    )
    capture = await diff_streamer(
        token=token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        base_sha=before.base_sha,
        head_sha=before.head_sha,
        project_id=project_id,
    )
    after_payload = await metadata_fetcher(
        token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        project_id=project_id,
        consumer=_PR_DIFF_METADATA_CONSUMER,
    )
    if not isinstance(after_payload, Mapping):
        raise HTTPException(
            status_code=502, detail="GitHub returned invalid PR metadata"
        )
    after = project_pr_diff_identity(
        after_payload,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
    )
    if before != after:
        raise HTTPException(
            status_code=409,
            detail="pull request branch identity changed during diff inspection",
        )
    return build_pr_diff_evidence(
        capture,
        project_id=project_id,
        identity=after,
    )


async def _locked_project_repository(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> tuple[str, str]:
    row = (
        await session.execute(
            select(Project.github_owner, Project.github_repo)
            .where(Project.id == project_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return validated_project_repository(row[0], row[1])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _prepare_pr_diff_inspection(
    session: AsyncSession,
    task: Task,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
) -> ExternalInspectionPlan:
    owner, repo = await _locked_project_repository(
        session,
        project_id=binding.project_id,
    )
    pr_number = int(parameters["pr_number"])
    return ExternalInspectionPlan(
        expected_revisions={
            "repository": f"{owner}/{repo}",
            "pr_number": pr_number,
        },
        reservation={
            "repository": f"{owner}/{repo}",
            "pr_number": pr_number,
        },
        external_scope={
            "turn_task_id": str(task.id),
            "project_id": str(task.project_id),
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
        },
    )


async def invoke_pr_diff_inspection(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
    inspector: Callable[
        ..., Awaitable[ResidentPRDiffEvidence]
    ] = inspect_project_pr_diff,
) -> dict[str, Any]:
    """Execute ``pr.diff.inspect`` through the shared three-phase state machine."""

    token_session_factory = async_sessionmaker(
        resolve_async_engine(session),
        expire_on_commit=False,
        autoflush=False,
    )

    async def perform(plan: ExternalInspectionPlan) -> ResidentPRDiffEvidence:
        scope = plan.external_scope
        return await inspector(
            turn_task_id=uuid.UUID(str(scope["turn_task_id"])),
            project_id=uuid.UUID(str(scope["project_id"])),
            owner=str(scope["owner"]),
            repo=str(scope["repo"]),
            pr_number=int(scope["pr_number"]),
            session_factory=token_session_factory,
        )

    return await execute_external_inspection(
        session,
        binding=binding,
        capability_id="pr.diff.inspect",
        parameters=dict(parameters),
        prepare_locked=_prepare_pr_diff_inspection,
        perform=perform,
        authority_change_detail="project repository changed during PR diff inspection",
    )


__all__ = [
    "MAX_DIFF_FILE_IDENTITIES",
    "MAX_DIFF_STREAM_BYTES",
    "BoundedDiffCapture",
    "PRDiffIdentity",
    "ResidentPRDiffEvidence",
    "build_pr_diff_evidence",
    "inspect_project_pr_diff",
    "invoke_pr_diff_inspection",
    "project_pr_diff_identity",
    "stream_project_pr_diff",
]
