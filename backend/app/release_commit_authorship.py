"""Fail-closed, immutable release authorship evidence.

The receipt is deliberately stored on the promoted release version.  GitHub is
used only by the write/reconciliation path; consumers validate the stored
receipt against that version's immutable identity and never call GitHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models import Project, ReleaseVersion


logger = logging.getLogger(__name__)

BOT_COMMIT_AUTHOR = "blueprintagentorchestrator[bot]"
MAX_REPORTED_OFFENDERS = 10
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMITS_PER_PAGE = 100
_MAX_COMPARE_COMMITS = 10_000
_AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS = 30
AUTHORSHIP_RECEIPT_SOURCE = "github_compare"
AUTHORSHIP_RECEIPT_SCHEMA_VERSION = 1
MAX_RECONCILIATION_VERSIONS = 100


def _unresolved(
    base_sha: str | None,
    head_sha: str | None,
    reason: str,
) -> dict[str, Any]:
    """Return the unadorned measurement shape used before persistence."""
    return {
        "state": "unresolved",
        "count": None,
        "offenders": [],
        "range": {"base_sha": base_sha, "head_sha": head_sha},
        "reason": reason,
    }


def unresolved_authorship_receipt(
    base_sha: str | None, head_sha: str | None, reason: str
) -> dict[str, Any]:
    """Return a safe read-path fallback, never an authoritative result."""
    return _unresolved(base_sha, head_sha, reason)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_sha(left: Any, right: Any) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def _receipt_identity(receipt: Any) -> str | None:
    if not isinstance(receipt, dict):
        return None
    try:
        return json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def validate_authorship_receipt(
    receipt: Any,
    base_sha: str | None,
    head_sha: str | None,
) -> dict[str, Any] | None:
    """Validate a server-generated current-schema receipt for one release.

    This validator is intentionally strict.  In particular, a legacy or
    tampered blob is not upgraded at read time and can never be treated as a
    resolved result.
    """
    if not isinstance(receipt, dict):
        return None
    if set(receipt) != {
        "source",
        "schema_version",
        "state",
        "count",
        "offenders",
        "range",
        "measured_at",
        "retry",
    } and set(receipt) != {
        "source",
        "schema_version",
        "state",
        "count",
        "offenders",
        "range",
        "reason",
        "measured_at",
        "retry",
    }:
        return None
    if receipt.get("source") != AUTHORSHIP_RECEIPT_SOURCE:
        return None
    if receipt.get("schema_version") != AUTHORSHIP_RECEIPT_SCHEMA_VERSION:
        return None
    if receipt.get("state") not in {"resolved", "unresolved"}:
        return None
    if receipt.get("range") != {"base_sha": base_sha, "head_sha": head_sha}:
        return None
    measured_at = receipt.get("measured_at")
    if not isinstance(measured_at, str):
        return None
    try:
        parsed_measured_at = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_measured_at.tzinfo is None:
        return None
    retry = receipt.get("retry")
    if not isinstance(retry, dict) or set(retry) != {"attempt", "retryable"}:
        return None
    attempt = retry.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    if not isinstance(retry.get("retryable"), bool):
        return None
    if receipt.get("state") == "resolved" and retry["retryable"]:
        return None
    if receipt.get("state") == "unresolved" and not retry["retryable"]:
        return None
    count = receipt.get("count")
    offenders = receipt.get("offenders")
    if not isinstance(offenders, list) or len(offenders) > MAX_REPORTED_OFFENDERS:
        return None
    for offender in offenders:
        if not isinstance(offender, dict):
            return None
        if not isinstance(offender.get("sha"), str) or not isinstance(
            offender.get("author"), str
        ):
            return None
    if receipt.get("state") == "resolved":
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if "reason" in receipt:
            return None
    elif count is not None or not isinstance(receipt.get("reason"), str):
        return None
    return receipt


def _next_attempt(receipt: Any) -> int:
    retry = receipt.get("retry") if isinstance(receipt, dict) else None
    attempt = retry.get("attempt") if isinstance(retry, dict) else None
    return attempt + 1 if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else 1


def _persisted_receipt(
    measurement: Any,
    base_sha: str,
    head_sha: str,
    *,
    attempt: int,
) -> dict[str, Any]:
    """Stamp untrusted measurement output with the server receipt contract."""
    state = measurement.get("state") if isinstance(measurement, dict) else None
    report_range = measurement.get("range") if isinstance(measurement, dict) else None
    valid_range = report_range == {"base_sha": base_sha, "head_sha": head_sha}
    if state == "resolved" and valid_range:
        count = measurement.get("count")
        offenders = measurement.get("offenders")
        if (
            not isinstance(count, bool)
            and isinstance(count, int)
            and count >= 0
            and isinstance(offenders, list)
            and len(offenders) <= MAX_REPORTED_OFFENDERS
            and all(
                isinstance(item, dict)
                and isinstance(item.get("sha"), str)
                and isinstance(item.get("author"), str)
                for item in offenders
            )
        ):
            return {
                "source": AUTHORSHIP_RECEIPT_SOURCE,
                "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
                "state": "resolved",
                "count": count,
                "offenders": offenders,
                "range": {"base_sha": base_sha, "head_sha": head_sha},
                "measured_at": _now_iso(),
                "retry": {"attempt": attempt, "retryable": False},
            }
    reason = (
        measurement.get("reason")
        if isinstance(measurement, dict) and isinstance(measurement.get("reason"), str)
        else "malformed_authorship_measurement"
    )
    return {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "unresolved",
        "count": None,
        "offenders": [],
        "range": {"base_sha": base_sha, "head_sha": head_sha},
        "reason": reason,
        "measured_at": _now_iso(),
        "retry": {"attempt": attempt, "retryable": True},
    }


async def _count_non_bot_authored_commits_from_github(
    base_sha: str,
    head_sha: str,
    *,
    session: Any,
    project: Any,
) -> dict[str, Any]:
    """Resolve the exact immutable release range through GitHub Compare."""
    from app import github
    from app.release_promotion import _resolve_github_with_session

    try:
        resolved = await _resolve_github_with_session(session, project)
    except Exception:  # noqa: BLE001 - credentials are fail-closed evidence
        return _unresolved(base_sha, head_sha, "github_credentials_unavailable")
    if resolved is None:
        return _unresolved(base_sha, head_sha, "github_credentials_unavailable")
    token, owner, repo = resolved
    project_id = str(project.id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    canonical_base = base_sha.lower()
    canonical_head = head_sha.lower()
    try:
        expected_total: int | None = None
        records: list[dict[str, str]] = []
        seen_shas: set[str] = set()
        final_page_last_sha: str | None = None
        page_number = 1
        while True:
            response = await github._github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
                headers,
                params={"per_page": _COMMITS_PER_PAGE, "page": page_number},
                project_id=project_id,
                consumer="release_commit_authorship",
            )
            if response.status_code != 200:
                return _unresolved(base_sha, head_sha, "github_range_unavailable")
            comparison_data = response.json()
            if not isinstance(comparison_data, dict):
                return _unresolved(base_sha, head_sha, "malformed_github_range")
            status = comparison_data.get("status")
            ahead_by = comparison_data.get("ahead_by")
            total_commits = comparison_data.get("total_commits")
            base_commit = comparison_data.get("base_commit")
            merge_base = comparison_data.get("merge_base_commit")
            merge_base_sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
            base_commit_sha = base_commit.get("sha") if isinstance(base_commit, dict) else None
            if status not in {"identical", "ahead"}:
                return _unresolved(base_sha, head_sha, "range_not_ancestor")
            if (
                isinstance(ahead_by, bool)
                or not isinstance(ahead_by, int)
                or ahead_by < 0
                or isinstance(total_commits, bool)
                or not isinstance(total_commits, int)
                or total_commits != ahead_by
                or not _same_sha(base_commit_sha, canonical_base)
                or not _same_sha(merge_base_sha, canonical_base)
            ):
                return _unresolved(base_sha, head_sha, "malformed_github_range")
            if status == "identical" and (
                not _same_sha(base_sha, head_sha) or total_commits != 0
            ):
                return _unresolved(base_sha, head_sha, "range_identity_mismatch")
            if status == "ahead" and (not total_commits or _same_sha(base_sha, head_sha)):
                return _unresolved(base_sha, head_sha, "range_identity_mismatch")
            if total_commits > _MAX_COMPARE_COMMITS:
                return _unresolved(base_sha, head_sha, "github_range_too_large")
            if expected_total is None:
                expected_total = total_commits
            elif expected_total != total_commits:
                return _unresolved(base_sha, head_sha, "range_pagination_mismatch")
            page = comparison_data.get("commits")
            if not isinstance(page, list) or expected_total is None:
                return _unresolved(base_sha, head_sha, "malformed_github_history")
            remaining = expected_total - len(seen_shas)
            if remaining < 0 or len(page) != min(_COMMITS_PER_PAGE, remaining):
                return _unresolved(base_sha, head_sha, "github_history_incomplete")
            for commit in page:
                if not isinstance(commit, dict):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                sha = commit.get("sha")
                author = (commit.get("commit") or {}).get("author")
                name = author.get("name") if isinstance(author, dict) else None
                if not isinstance(sha, str) or not _FULL_SHA.fullmatch(sha):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                normalized_sha = sha.lower()
                if not isinstance(name, str):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                if normalized_sha in seen_shas:
                    return _unresolved(base_sha, head_sha, "github_history_duplicate")
                if normalized_sha == canonical_base:
                    return _unresolved(base_sha, head_sha, "range_identity_mismatch")
                seen_shas.add(normalized_sha)
                identity = commit.get("author")
                verification = (commit.get("commit") or {}).get("verification")
                trusted_bot = (
                    name.casefold() == BOT_COMMIT_AUTHOR.casefold()
                    and isinstance(identity, dict)
                    and identity.get("login") == BOT_COMMIT_AUTHOR
                    and identity.get("type") == "Bot"
                    and isinstance(verification, dict)
                    and verification.get("verified") is True
                )
                if not trusted_bot:
                    records.append({"sha": normalized_sha, "author": name})
            if page:
                final_page_last_sha = page[-1].get("sha", "").lower()
            page_number += 1
            if len(seen_shas) == expected_total:
                break
            if page_number > (_MAX_COMPARE_COMMITS // _COMMITS_PER_PAGE) + 1:
                return _unresolved(base_sha, head_sha, "github_history_incomplete")
        if expected_total is None or len(seen_shas) != expected_total:
            return _unresolved(base_sha, head_sha, "github_history_incomplete")
        if status == "ahead" and (
            canonical_head not in seen_shas or final_page_last_sha != canonical_head
        ):
            return _unresolved(base_sha, head_sha, "range_identity_mismatch")
        if status == "identical" and (seen_shas or final_page_last_sha is not None):
            return _unresolved(base_sha, head_sha, "range_identity_mismatch")
        offenders = [
            {"sha": record["sha"][:12], "author": record["author"]}
            for record in records[:MAX_REPORTED_OFFENDERS]
        ]
        return {
            "state": "resolved",
            "count": len(records),
            "offenders": offenders,
            "range": {"base_sha": base_sha, "head_sha": head_sha},
        }
    except Exception:  # noqa: BLE001 - network/API/schema failures are unresolved
        return _unresolved(base_sha, head_sha, "github_history_unavailable")


async def count_non_bot_authored_commits(
    base_sha: str | None,
    head_sha: str | None,
    *,
    session: Any | None = None,
    project: Any | None = None,
) -> dict[str, Any]:
    """Count non-bot authors in ``base_sha..head_sha`` without trusting zero."""
    if not (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        return _unresolved(base_sha, head_sha, "missing_or_malformed_sha")
    if session is None or project is None:
        return _unresolved(base_sha, head_sha, "github_context_unavailable")
    return await _count_non_bot_authored_commits_from_github(
        base_sha, head_sha, session=session, project=project
    )


async def persist_serving_authorship_evidence(session: Any, version: Any) -> None:
    """Measure and CAS-write evidence without holding the serving row lock.

    A pending current-schema receipt is committed while the serving transition
    is locked.  GitHub measurement then runs after that commit.  The final
    write reacquires the row briefly and refuses stale results if any serving
    or receipt identity changed meanwhile.
    """
    base_sha = version.base_sha
    served_sha = version.served_sha
    if (
        version.status != "promoted"
        or version.serving_status != "serving_confirmed"
        or not isinstance(base_sha, str)
        or not isinstance(served_sha, str)
    ):
        return
    before_qualification = dict(version.last_qualification or {})
    before_report = before_qualification.get("operator_authored_commits")
    validated_before = validate_authorship_receipt(before_report, base_sha, served_sha)
    if validated_before is not None and validated_before.get("state") == "resolved":
        return
    project = await session.get(Project, version.project_id)
    attempt = _next_attempt(before_report)
    pending = _persisted_receipt(
        _unresolved(base_sha, served_sha, "authorship_measurement_pending"),
        base_sha,
        served_sha,
        attempt=attempt,
    )
    pending_qualification = dict(before_qualification)
    pending_qualification["operator_authored_commits"] = pending
    version.last_qualification = pending_qualification
    flag_modified(version, "last_qualification")
    session.add(version)
    await session.flush()
    await session.commit()  # releases the serving row lock before GitHub I/O
    pending_identity = _receipt_identity(pending)

    if project is None:
        measurement = _unresolved(base_sha, served_sha, "project_unavailable")
    else:
        try:
            measurement = await asyncio.wait_for(
                count_non_bot_authored_commits(
                    base_sha, served_sha, session=session, project=project
                ),
                timeout=_AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            measurement = _unresolved(base_sha, served_sha, "github_authorship_timeout")
        except Exception:  # noqa: BLE001 - confirmation remains truthful
            measurement = _unresolved(base_sha, served_sha, "github_authorship_unavailable")
    receipt = _persisted_receipt(measurement, base_sha, served_sha, attempt=attempt)

    current = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(ReleaseVersion.id == version.id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if current is None:
        return
    current_report = (current.last_qualification or {}).get("operator_authored_commits")
    if (
        current.status != "promoted"
        or current.serving_status != "serving_confirmed"
        or current.base_sha != base_sha
        or current.served_sha != served_sha
        or _receipt_identity(current_report) != pending_identity
    ):
        await session.rollback()
        return
    current_qualification = dict(current.last_qualification or {})
    current_qualification["operator_authored_commits"] = receipt
    current.last_qualification = current_qualification
    flag_modified(current, "last_qualification")
    session.add(current)
    await session.commit()


async def reconcile_serving_authorship_evidence(
    session_factory=None,
    *,
    limit: int = MAX_RECONCILIATION_VERSIONS,
) -> int:
    """Boundedly retry missing, legacy, and unresolved promoted receipts."""
    if session_factory is None:
        from app.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    bounded_limit = max(1, min(int(limit), MAX_RECONCILIATION_VERSIONS))
    async with session_factory() as scan_session:
        version_ids = list(
            (
                await scan_session.execute(
                    select(ReleaseVersion.id)
                    .where(
                        ReleaseVersion.status == "promoted",
                        ReleaseVersion.serving_status == "serving_confirmed",
                        ReleaseVersion.base_sha.is_not(None),
                        ReleaseVersion.served_sha.is_not(None),
                    )
                    .order_by(ReleaseVersion.version_number.desc())
                    .limit(bounded_limit)
                )
            )
            .scalars()
            .all()
        )
    attempted = 0
    for version_id in version_ids:
        async with session_factory() as worker_session:
            version = (
                (
                    await worker_session.execute(
                        select(ReleaseVersion).where(ReleaseVersion.id == version_id)
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                continue
            attempted += 1
            try:
                await persist_serving_authorship_evidence(worker_session, version)
            except Exception:  # noqa: BLE001 - one stale release cannot stop repair
                await worker_session.rollback()
                logger.exception(
                    "authorship evidence reconciliation failed for release %s",
                    version_id,
                )
    return attempted
