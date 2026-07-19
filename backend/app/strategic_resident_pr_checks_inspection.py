"""Bounded, project-scoped PR check-run evidence for Resident turns."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import resolve_async_engine
from app.github_token_resolver import get_github_token_for_task
from app.models import Project, Task
from app.schemas import redact_command_log_text
from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_external_inspection import (
    ExternalInspectionPlan,
    execute_external_inspection,
)
from app.strategic_resident_github_bounded_read import (
    MAX_PR_METADATA_BYTES,
    stream_github_json_object,
    stream_project_pr_metadata,
)
from app.strategic_resident_inspection_receipt import citable_inspection_evidence
from app.strategic_resident_pr_inspection import validated_project_repository
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    _bounded_evidence_data,
    _canonical_json,
)


_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CHECKS_PER_PAGE = 100
_MAX_CHECK_PAGES = 2
_MAX_FETCHED_CHECKS = _CHECKS_PER_PAGE * _MAX_CHECK_PAGES
MAX_CHECK_PAGE_BYTES = 1_000_000
_MAX_PROJECTED_CHECKS = 24
_MAX_REPORTED_CHECKS = 1_000_000
_MAX_CHECK_NAME_CHARS = 255
_MAX_APP_SLUG_CHARS = 100
_KNOWN_STATUSES = frozenset({"queued", "in_progress", "completed"})
_PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_FAILING_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)
_KNOWN_CONCLUSIONS = _PASSING_CONCLUSIONS | _FAILING_CONCLUSIONS


class ResidentPRChecksEvidence(BaseModel):
    """One exact, content-addressed check-run snapshot for a PR head."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pr_check"] = "pr_check"
    project_id: uuid.UUID
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    data: dict[str, Any]


class ResidentPRChecksRepositoryUnavailable(RuntimeError):
    """The bound project has no usable GitHub credential for check reads."""


def validated_head_sha(value: object) -> str:
    """Return one exact lowercase Git object id or fail closed."""

    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        raise ValueError("head_sha must be an exact lowercase Git SHA")
    return value


def _validated_pr_number(value: object) -> int:
    if type(value) is not int or not 1 <= value < 2**31:
        raise ValueError("pr_number must be an integer between 1 and 2147483647")
    return value


def _non_negative_int(value: object, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"GitHub returned an invalid {field}")
    return value


def _bounded_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_command_log_text(value)[:limit]


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _repository_full_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return None
    try:
        owner, repo = validated_project_repository(*full_name.split("/", 1))
    except ValueError:
        return None
    return f"{owner}/{repo}"


def _require_pr_head_correlation(
    payload: dict[str, Any],
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> None:
    if type(payload.get("number")) is not int or payload["number"] != pr_number:
        raise ValueError("GitHub returned a mismatched pull request number")
    expected_repository = f"{owner}/{repo}"
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    actual_repository = _repository_full_name(base.get("repo"))
    if (
        actual_repository is None
        or actual_repository.casefold() != expected_repository.casefold()
    ):
        raise ValueError("GitHub returned a pull request from another repository")
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    head_repository = _repository_full_name(head.get("repo"))
    if head_repository is None:
        raise ValueError("pull request head repository identity is unavailable")
    if head_repository.casefold() != expected_repository.casefold():
        raise ValueError(
            "forked-head PR checks are not supported by bounded inspection"
        )
    actual_head = validated_head_sha(head.get("sha"))
    if actual_head != head_sha:
        raise ValueError("pull request head changed before check inspection")


def _project_check_run(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned a malformed check run")
    check_id = _non_negative_int(
        payload.get("id"), field="check-run id", maximum=2**63 - 1
    )
    raw_status = payload.get("status")
    check_status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
    raw_conclusion = payload.get("conclusion")
    conclusion = (
        raw_conclusion
        if raw_conclusion is None or raw_conclusion in _KNOWN_CONCLUSIONS
        else "unknown"
    )
    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    return {
        "id": check_id,
        "name": _bounded_text(payload.get("name"), limit=_MAX_CHECK_NAME_CHARS),
        "status": check_status,
        "conclusion": conclusion,
        "app": _bounded_text(app.get("slug"), limit=_MAX_APP_SLUG_CHARS),
        "started_at": _timestamp(payload.get("started_at")),
        "completed_at": _timestamp(payload.get("completed_at")),
    }


def _snapshot_state(
    checks: list[dict[str, Any]],
    *,
    truncated: bool,
    pages_fetched: int,
) -> str:
    # REST pagination is not snapshot-isolated: replacement/reordering between
    # page reads can preserve total_count while mixing generations. Counts remain
    # useful observations, but a multi-page aggregate can never claim a verdict.
    if pages_fetched > 1:
        return "unknown"
    conclusions = {check["conclusion"] for check in checks}
    statuses = {check["status"] for check in checks}
    if conclusions.intersection(_FAILING_CONCLUSIONS):
        return "failure"
    if statuses.intersection({"queued", "in_progress"}):
        return "pending"
    if (
        not checks
        or "unknown" in statuses
        or "unknown" in conclusions
        or None in conclusions
    ):
        return "unknown"
    if truncated:
        return "unknown"
    if statuses == {"completed"} and conclusions.issubset(_PASSING_CONCLUSIONS):
        return "success"
    return "unknown"


def _counts(values: list[object], categories: tuple[str, ...]) -> dict[str, int]:
    return {category: values.count(category) for category in categories}


def _project_checks_data(
    check_runs: list[object],
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    reported_total: int,
    pages_fetched: int,
) -> dict[str, Any]:
    if len(check_runs) > _MAX_FETCHED_CHECKS:
        raise ValueError("GitHub check-run snapshot exceeds the fetch bound")
    if not 0 <= pages_fetched <= _MAX_CHECK_PAGES:
        raise ValueError("GitHub check-run page count exceeds the fetch bound")
    reported_total = _non_negative_int(
        reported_total,
        field="check-run total_count",
        maximum=_MAX_REPORTED_CHECKS,
    )
    if reported_total < len(check_runs):
        raise ValueError("GitHub returned inconsistent check-run counts")

    projected = [_project_check_run(row) for row in check_runs]
    ids = [row["id"] for row in projected]
    if len(ids) != len(set(ids)):
        raise ValueError("GitHub returned duplicate check runs across pages")
    truncated = reported_total > len(projected)
    statuses = [row["status"] for row in projected]
    conclusions = [
        row["conclusion"] if row["conclusion"] is not None else "pending"
        for row in projected
    ]
    details = projected[:_MAX_PROJECTED_CHECKS]
    return {
        "repository": f"{owner}/{repo}",
        "pr_number": pr_number,
        "head_sha": head_sha,
        "coverage": "check_runs_only",
        "check_runs_state": _snapshot_state(
            projected,
            truncated=truncated,
            pages_fetched=pages_fetched,
        ),
        "counts": {
            "reported_total": reported_total,
            "fetched": len(projected),
            "projected": len(details),
            "pages_fetched": pages_fetched,
            "fetch_truncated": truncated,
            "details_truncated": len(details) < len(projected),
            "statuses": _counts(
                statuses, ("queued", "in_progress", "completed", "unknown")
            ),
            "conclusions": _counts(
                conclusions,
                (
                    "success",
                    "failure",
                    "neutral",
                    "skipped",
                    "cancelled",
                    "timed_out",
                    "action_required",
                    "stale",
                    "startup_failure",
                    "pending",
                    "unknown",
                ),
            ),
        },
        "checks": details,
    }


def _fit_check_details(
    data: dict[str, Any], *, budget: int
) -> tuple[dict[str, Any], bytes]:
    fitted = {**data, "counts": dict(data["counts"]), "checks": list(data["checks"])}
    while True:
        fitted["counts"]["projected"] = len(fitted["checks"])
        fitted["counts"]["details_truncated"] = (
            len(fitted["checks"]) < fitted["counts"]["fetched"]
        )
        bounded, encoded = _bounded_evidence_data(fitted, budget=budget)
        if "evidence_truncated" not in bounded:
            return bounded, encoded
        if not fitted["checks"]:
            raise ValueError("PR check summary exceeds the evidence bound")
        fitted["checks"].pop()


def build_pr_checks_evidence(
    check_runs: list[object],
    *,
    project_id: uuid.UUID,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    reported_total: int,
    pages_fetched: int,
    observed_at: datetime | None = None,
) -> ResidentPRChecksEvidence:
    """Build an allowlisted check snapshot under the shared 4.8/5.5KB bounds."""

    owner, repo = validated_project_repository(owner, repo)
    pr_number = _validated_pr_number(pr_number)
    head_sha = validated_head_sha(head_sha)
    data = _project_checks_data(
        check_runs,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        reported_total=reported_total,
        pages_fetched=pages_fetched,
    )
    now = observed_at or datetime.now(timezone.utc)

    def build(data_budget: int) -> tuple[ResidentPRChecksEvidence, bytes]:
        bounded, encoded = _fit_check_details(data, budget=data_budget)
        checksum = hashlib.sha256(encoded).hexdigest()
        evidence = ResidentPRChecksEvidence(
            project_id=project_id,
            source_id=f"github-pr-checks:{owner}/{repo}#{pr_number}@{head_sha}",
            revision=f"pr-checks:{pr_number}:{head_sha}:{checksum}",
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
        evidence, _ = build(
            max(0, MAX_EVIDENCE_RESPONSE_BYTES - (len(complete) - len(encoded)))
        )
    return evidence


async def stream_project_check_page(
    token: str,
    *,
    owner: str,
    repo: str,
    head_sha: str,
    page: int,
    project_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    return await stream_github_json_object(
        token,
        url=(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
            f"?per_page={_CHECKS_PER_PAGE}&page={page}"
        ),
        project_id=project_id,
        max_bytes=MAX_CHECK_PAGE_BYTES,
        consumer="strategic_resident_pr_checks",
        client=client,
    )


MetadataFetcher = Callable[..., Awaitable[dict[str, Any]]]
CheckPageFetcher = Callable[..., Awaitable[dict[str, Any]]]


async def _fetch_bounded_check_runs(
    token: str,
    *,
    owner: str,
    repo: str,
    head_sha: str,
    project_id: uuid.UUID,
    page_fetcher: CheckPageFetcher,
) -> tuple[list[object], int, int]:
    check_runs: list[object] = []
    reported_total: int | None = None
    pages_fetched = 0
    for page in range(1, _MAX_CHECK_PAGES + 1):
        payload = await page_fetcher(
            token,
            owner=owner,
            repo=repo,
            head_sha=head_sha,
            page=page,
            project_id=project_id,
        )
        try:
            page_total = _non_negative_int(
                payload.get("total_count"),
                field="check-run total_count",
                maximum=_MAX_REPORTED_CHECKS,
            )
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        rows = payload.get("check_runs")
        if not isinstance(rows, list) or len(rows) > _CHECKS_PER_PAGE:
            raise HTTPException(
                status_code=502, detail="GitHub returned an invalid check-run page"
            )
        if reported_total is None:
            reported_total = page_total
        elif page_total != reported_total:
            raise HTTPException(
                status_code=409, detail="PR checks changed during pagination"
            )
        pages_fetched += 1
        check_runs.extend(rows)
        if len(check_runs) >= reported_total:
            break
        if len(rows) != _CHECKS_PER_PAGE:
            raise HTTPException(
                status_code=409, detail="PR checks changed during pagination"
            )
    return check_runs, reported_total or 0, pages_fetched


async def inspect_project_pr_checks(
    *,
    turn_task_id: uuid.UUID,
    project_id: uuid.UUID,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    session_factory: async_sessionmaker[AsyncSession],
    metadata_fetcher: MetadataFetcher = stream_project_pr_metadata,
    page_fetcher: CheckPageFetcher = stream_project_check_page,
) -> ResidentPRChecksEvidence:
    """Read exact-head PR checks after releasing the credential session."""

    owner, repo = validated_project_repository(owner, repo)
    pr_number = _validated_pr_number(pr_number)
    head_sha = validated_head_sha(head_sha)
    async with session_factory() as token_session:
        token, resolved_owner, resolved_repo = await get_github_token_for_task(
            turn_task_id,
            token_session,
            release_dedicated_session_before_io=True,
        )
    if not token:
        raise ResidentPRChecksRepositoryUnavailable(
            "project GitHub repository has no available credential"
        )
    try:
        resolved_repository = validated_project_repository(
            resolved_owner, resolved_repo
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if resolved_repository != (owner, repo):
        raise HTTPException(
            status_code=409,
            detail="project repository credential scope changed during PR check inspection",
        )

    pr_payload = await metadata_fetcher(
        token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        project_id=project_id,
    )
    if not isinstance(pr_payload, dict):
        raise HTTPException(
            status_code=502, detail="GitHub returned an invalid PR response"
        )
    try:
        _require_pr_head_correlation(
            pr_payload,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    check_runs, reported_total, pages_fetched = await _fetch_bounded_check_runs(
        token,
        owner=owner,
        repo=repo,
        head_sha=head_sha,
        project_id=project_id,
        page_fetcher=page_fetcher,
    )
    try:
        return build_pr_checks_evidence(
            check_runs,
            project_id=project_id,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            reported_total=reported_total,
            pages_fetched=pages_fetched,
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
    try:
        return validated_project_repository(*project_row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _prepare_pr_checks_inspection(
    session: AsyncSession,
    task: Task,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
) -> ExternalInspectionPlan:
    owner, repo = await _locked_project_repository(
        session, project_id=binding.project_id
    )
    try:
        pr_number = _validated_pr_number(parameters.get("pr_number"))
        head_sha = validated_head_sha(parameters.get("head_sha"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    target = {
        "repository": f"{owner}/{repo}",
        "pr_number": pr_number,
        "head_sha": head_sha,
    }
    return ExternalInspectionPlan(
        expected_revisions=target,
        reservation=target,
        external_scope={
            "turn_task_id": str(task.id),
            "project_id": str(task.project_id),
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
        },
    )


async def invoke_pr_checks_inspection(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
    inspector: Callable[
        ..., Awaitable[ResidentPRChecksEvidence]
    ] = inspect_project_pr_checks,
) -> dict[str, Any]:
    token_session_factory = async_sessionmaker(
        resolve_async_engine(session),
        expire_on_commit=False,
        autoflush=False,
    )

    async def perform(plan: ExternalInspectionPlan) -> ResidentPRChecksEvidence:
        scope = plan.external_scope
        try:
            return await inspector(
                turn_task_id=uuid.UUID(str(scope["turn_task_id"])),
                project_id=uuid.UUID(str(scope["project_id"])),
                owner=str(scope["owner"]),
                repo=str(scope["repo"]),
                pr_number=int(scope["pr_number"]),
                head_sha=str(scope["head_sha"]),
                session_factory=token_session_factory,
            )
        except ResidentPRChecksRepositoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return await execute_external_inspection(
        session,
        binding=binding,
        capability_id="pr.checks.inspect",
        parameters=dict(parameters),
        prepare_locked=_prepare_pr_checks_inspection,
        perform=perform,
        authority_change_detail="project repository or PR head changed during check inspection",
    )


__all__ = [
    "MAX_CHECK_PAGE_BYTES",
    "MAX_PR_METADATA_BYTES",
    "ResidentPRChecksEvidence",
    "ResidentPRChecksRepositoryUnavailable",
    "build_pr_checks_evidence",
    "inspect_project_pr_checks",
    "invoke_pr_checks_inspection",
    "stream_project_check_page",
    "stream_project_pr_metadata",
    "validated_head_sha",
]
