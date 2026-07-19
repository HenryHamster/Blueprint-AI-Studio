"""Bounded, project-scoped pull-request evidence for Resident turns."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import resolve_async_engine
from app.github_pulls import get_pull_request
from app.github_token_resolver import get_github_token_for_task
from app.models import Project, Task
from app.schemas import redact_command_log_text
from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_external_inspection import (
    ExternalInspectionPlan,
    execute_external_inspection,
)
from app.strategic_resident_inspection_receipt import citable_inspection_evidence
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    _bounded_evidence_data,
    _canonical_json,
)


_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MAX_TITLE_CHARS = 1_000
_MAX_REF_CHARS = 255
_MAX_LOGIN_CHARS = 255
_MAX_LABELS = 20
_MAX_REVIEWERS = 20
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ResidentPREvidence(BaseModel):
    """One exact, content-addressed PR snapshot."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pr"] = "pr"
    project_id: uuid.UUID
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    data: dict[str, Any]


class ResidentPRRepositoryUnavailable(RuntimeError):
    """The bound project has no usable GitHub credential for exact-repo reads."""


def validated_project_repository(owner: object, repo: object) -> tuple[str, str]:
    """Return a bounded GitHub repository identity or fail closed."""

    normalized_owner = owner.strip() if isinstance(owner, str) else ""
    normalized_repo = repo.strip() if isinstance(repo, str) else ""
    if not _REPOSITORY_SEGMENT.fullmatch(normalized_owner) or not _REPOSITORY_SEGMENT.fullmatch(
        normalized_repo
    ) or normalized_owner in {".", ".."} or normalized_repo in {".", ".."}:
        raise ValueError("project has no valid GitHub repository binding")
    return normalized_owner, normalized_repo


def _bounded_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_command_log_text(value)[:limit]


def _strict_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _non_negative_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _repository_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return None
    owner, repo = full_name.split("/", 1)
    try:
        owner, repo = validated_project_repository(owner, repo)
    except ValueError:
        return None
    return f"{owner}/{repo}"


def _sha(value: object, *, field: str, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        raise ValueError(f"GitHub returned an invalid {field} SHA")
    return value


def _branch(value: object, *, field: str) -> dict[str, Any]:
    branch = value if isinstance(value, dict) else {}
    raw_ref = branch.get("ref")
    if (
        not isinstance(raw_ref, str)
        or not raw_ref.strip()
        or len(raw_ref) > _MAX_REF_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_ref)
        or any(character in " ~^:?*[\\" for character in raw_ref)
        or ".." in raw_ref
        or "@{" in raw_ref
        or "//" in raw_ref
        or raw_ref == "@"
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in raw_ref.split("/")
        )
        or raw_ref.startswith("/")
        or raw_ref.endswith(("/", ".", ".lock"))
    ):
        raise ValueError(f"GitHub returned an invalid {field} ref")
    return {
        "ref": raw_ref,
        "sha": _sha(branch.get("sha"), field=field, required=True),
        "repository": _repository_name(branch.get("repo")),
    }


def _login(value: object) -> str | None:
    user = value if isinstance(value, dict) else {}
    return _bounded_text(user.get("login"), limit=_MAX_LOGIN_CHARS)


def _logins(value: object, *, limit: int) -> list[str]:
    rows = value if isinstance(value, list) else []
    result: list[str] = []
    for row in rows[:limit]:
        login = _login(row)
        if login is not None:
            result.append(login)
    return result


def _labels(value: object) -> list[str]:
    rows = value if isinstance(value, list) else []
    result: list[str] = []
    for row in rows[:_MAX_LABELS]:
        label = _bounded_text(
            row.get("name") if isinstance(row, dict) else row,
            limit=100,
        )
        if label is not None:
            result.append(label)
    return result


def _project_pr_data(
    payload: dict[str, Any],
    *,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    """Project a GitHub response through a fixed allowlist."""

    response_number = payload.get("number")
    if type(response_number) is not int or response_number != pr_number:
        raise ValueError("GitHub returned a mismatched pull request number")
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    expected_repository = f"{owner}/{repo}"
    actual_repository = _repository_name(base.get("repo"))
    if (
        actual_repository is None
        or actual_repository.casefold() != expected_repository.casefold()
    ):
        raise ValueError("GitHub returned a pull request from another repository")

    head = _branch(payload.get("head"), field="head")
    base_projection = _branch(base, field="base")
    head_repository = head.get("repository")
    html_url = _bounded_text(payload.get("html_url"), limit=512)
    expected_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    if html_url is not None and html_url.rstrip("/").casefold() != expected_url.casefold():
        raise ValueError("GitHub returned a mismatched pull request URL")

    milestone = payload.get("milestone")
    return {
        "pr_number": pr_number,
        "repository": expected_repository,
        "github_id": _non_negative_int(payload.get("id")),
        "node_id": _bounded_text(payload.get("node_id"), limit=128),
        "state": _bounded_text(payload.get("state"), limit=32),
        "title": _bounded_text(payload.get("title"), limit=_MAX_TITLE_CHARS),
        "draft": _strict_bool(payload.get("draft")),
        "locked": _strict_bool(payload.get("locked")),
        "merged": _strict_bool(payload.get("merged")),
        "mergeable": _strict_bool(payload.get("mergeable")),
        "mergeable_state": _bounded_text(payload.get("mergeable_state"), limit=64),
        "merge_commit_sha": _sha(
            payload.get("merge_commit_sha"), field="merge commit", required=False
        ),
        "html_url": html_url,
        "same_repository": bool(
            head_repository
            and head_repository.casefold() == expected_repository.casefold()
        ),
        "author": _login(payload.get("user")),
        "assignees": _logins(payload.get("assignees"), limit=_MAX_REVIEWERS),
        "requested_reviewers": _logins(
            payload.get("requested_reviewers"), limit=_MAX_REVIEWERS
        ),
        "labels": _labels(payload.get("labels")),
        "milestone": _bounded_text(
            milestone.get("title") if isinstance(milestone, dict) else None,
            limit=255,
        ),
        "head": head,
        "base": base_projection,
        "created_at": _timestamp(payload.get("created_at")),
        "updated_at": _timestamp(payload.get("updated_at")),
        "closed_at": _timestamp(payload.get("closed_at")),
        "merged_at": _timestamp(payload.get("merged_at")),
        "counts": {
            "commits": _non_negative_int(payload.get("commits")),
            "additions": _non_negative_int(payload.get("additions")),
            "deletions": _non_negative_int(payload.get("deletions")),
            "changed_files": _non_negative_int(payload.get("changed_files")),
            "comments": _non_negative_int(payload.get("comments")),
            "review_comments": _non_negative_int(payload.get("review_comments")),
        },
    }


def build_pr_evidence(
    payload: dict[str, Any],
    *,
    project_id: uuid.UUID,
    owner: str,
    repo: str,
    pr_number: int,
    observed_at: datetime | None = None,
) -> ResidentPREvidence:
    """Build an exact PR snapshot under the shared 4.8/5.5KB bounds."""

    owner, repo = validated_project_repository(owner, repo)
    data = _project_pr_data(payload, owner=owner, repo=repo, pr_number=pr_number)
    source_id = f"github-pr:{owner}/{repo}#{pr_number}"
    now = observed_at or datetime.now(timezone.utc)

    def build(data_budget: int) -> tuple[ResidentPREvidence, bytes]:
        bounded, encoded = _bounded_evidence_data(data, budget=data_budget)
        checksum = hashlib.sha256(encoded).hexdigest()
        evidence = ResidentPREvidence(
            project_id=project_id,
            source_id=source_id,
            revision=f"pr:{pr_number}:{checksum}",
            checksum=checksum,
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


async def inspect_project_pr(
    *,
    turn_task_id: uuid.UUID,
    project_id: uuid.UUID,
    owner: str,
    repo: str,
    pr_number: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> ResidentPREvidence:
    """Fetch one PR using the exact turn's configured project credentials."""

    owner, repo = validated_project_repository(owner, repo)
    async with session_factory() as token_session:
        token, resolved_owner, resolved_repo = await get_github_token_for_task(
            turn_task_id,
            token_session,
            release_dedicated_session_before_io=True,
        )
    if not token:
        raise ResidentPRRepositoryUnavailable(
            "project GitHub repository has no available credential"
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
            detail="project repository credential scope changed during PR inspection",
        )
    payload = await get_pull_request(
        token,
        owner,
        repo,
        pr_number,
        project_id=str(project_id),
    )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="GitHub returned an invalid PR response")
    try:
        return build_pr_evidence(
            payload,
            project_id=project_id,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _locked_project_repository(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> tuple[str, str]:
    project_row = (
        await session.execute(
            select(Project.github_owner, Project.github_repo)
        .where(Project.id == project_id)
        .with_for_update()
        )
    ).first()
    if project_row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    owner, repo = project_row
    try:
        return validated_project_repository(owner, repo)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _prepare_pr_inspection(
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


async def invoke_pr_inspection(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
    inspector: Callable[..., Awaitable[ResidentPREvidence]] = inspect_project_pr,
) -> dict[str, Any]:
    token_session_factory = async_sessionmaker(
        resolve_async_engine(session),
        expire_on_commit=False,
        autoflush=False,
    )

    async def perform(plan: ExternalInspectionPlan) -> ResidentPREvidence:
        scope = plan.external_scope
        try:
            return await inspector(
                turn_task_id=uuid.UUID(str(scope["turn_task_id"])),
                project_id=uuid.UUID(str(scope["project_id"])),
                owner=str(scope["owner"]),
                repo=str(scope["repo"]),
                pr_number=int(scope["pr_number"]),
                session_factory=token_session_factory,
            )
        except ResidentPRRepositoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return await execute_external_inspection(
        session,
        binding=binding,
        capability_id="pr.inspect",
        parameters=parameters,
        prepare_locked=_prepare_pr_inspection,
        perform=perform,
        authority_change_detail="project repository changed during PR inspection",
    )


__all__ = [
    "ResidentPREvidence",
    "ResidentPRRepositoryUnavailable",
    "build_pr_evidence",
    "inspect_project_pr",
    "invoke_pr_inspection",
    "validated_project_repository",
]
