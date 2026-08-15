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
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm.attributes import flag_modified

from app.database import resolve_dialect_name
from app.models import Project, ReleaseAuthorshipReconciliationState, ReleaseVersion


logger = logging.getLogger(__name__)
_MODULE_SIZE_GOVERNANCE_CAP: int = 1265

BOT_COMMIT_AUTHOR = "blueprintagentorchestrator[bot]"
BLUEPRINT_GITHUB_BOT_ID = 272093198
_BOT_AUTHOR_EMAILS = frozenset(
    {
        "blueprintagentorchestrator[bot]@users.noreply.github.com",
        f"{BLUEPRINT_GITHUB_BOT_ID}+blueprintagentorchestrator[bot]@users.noreply.github.com",
    }
)
_GITHUB_MERGE_COMMITTERS = frozenset({"github", "web-flow"})
_GITHUB_NOREPLY_EMAIL = "noreply@github.com"
MAX_REPORTED_OFFENDERS = 10
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_OFFENDER_SHA = re.compile(r"^[0-9a-f]{12}$")
MAX_OFFENDER_AUTHOR_LENGTH = 255
_COMMITS_PER_PAGE = 100
_MAX_COMPARE_COMMITS = 10_000
_AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS = 30
AUTHORSHIP_RECEIPT_SOURCE = "github_compare"
AUTHORSHIP_RECEIPT_SCHEMA_VERSION = 2
_LEGACY_AUTHORSHIP_RECEIPT_SCHEMA_VERSION = 1
GITHUB_WEB_FLOW_ID = 19864447
MAX_RECONCILIATION_VERSIONS = 100
_RECONCILIATION_MAX_CALLS = 25
_RECONCILIATION_MAX_SECONDS = 20
_RECONCILIATION_SCAN_PAGE_SIZE = 100
_RETRY_BASE_SECONDS = 60
_RETRY_MAX_SECONDS = 3600
_PERMANENT_AUTHORSHIP_REASONS = frozenset(
    {
        "missing_or_malformed_sha",
        "range_not_ancestor",
        "malformed_github_range",
        "range_identity_mismatch",
        "range_pagination_mismatch",
        "malformed_github_history",
        "github_history_duplicate",
        "github_history_incomplete",
        "github_range_too_large",
        "malformed_authorship_measurement",
        "provenance_untrusted",
    }
)


def serving_authorship_range(version: Any) -> tuple[str | None, str | None]:
    """Return the immutable promote-to-serving compare range for a release."""
    if isinstance(version, dict):
        promoted_sha = version.get("promoted_sha")
        served_sha = version.get("served_sha")
    else:
        promoted_sha = getattr(version, "promoted_sha", None)
        served_sha = getattr(version, "served_sha", None)
    return (
        promoted_sha if isinstance(promoted_sha, str) else None,
        served_sha if isinstance(served_sha, str) else None,
    )


def candidate_authorship_range(version: Any) -> tuple[str | None, str | None]:
    """Return the candidate's entire compare range: base main to candidate head."""
    if isinstance(version, dict):
        base_sha = version.get("base_sha")
        candidate_sha = version.get("candidate_sha")
        if candidate_sha is None:
            qualification = version.get("last_qualification")
            candidate_sha = (
                qualification.get("candidate_sha")
                if isinstance(qualification, dict)
                else None
            )
    else:
        base_sha = getattr(version, "base_sha", None)
        candidate_sha = getattr(version, "candidate_sha", None)
        if candidate_sha is None:
            qualification = getattr(version, "last_qualification", None)
            candidate_sha = (
                qualification.get("candidate_sha")
                if isinstance(qualification, dict)
                else None
            )
    return (
        base_sha if isinstance(base_sha, str) else None,
        candidate_sha if isinstance(candidate_sha, str) else None,
    )


def authorship_receipt_for_measurement(
    measurement: Any,
    base_sha: str | None,
    head_sha: str | None,
) -> dict[str, Any]:
    """Wrap a candidate measurement in the immutable receipt contract.

    Candidate evidence is measured before promotion, so it is not persisted on
    a serving row.  The qualifier still needs the same validated receipt shape
    used by category grading; keeping this adapter here prevents the hard gate
    and the category projector from inventing separate evidence rules.
    """
    return _persisted_receipt(
        measurement,
        base_sha if isinstance(base_sha, str) else "",
        head_sha if isinstance(head_sha, str) else "",
        attempt=1,
    )


AUTHORSHIP_EVIDENCE_CONTRACT_TYPE = "operator_authored_commits"


def normalize_authorship_evidence_contract(raw: Any) -> dict[str, Any] | None:
    """Accept only the explicit, structured authorship contract shape."""
    if not isinstance(raw, dict) or set(raw) != {"type", "predicate"}:
        return None
    if raw.get("type") != AUTHORSHIP_EVIDENCE_CONTRACT_TYPE:
        return None
    predicate = raw.get("predicate")
    if (
        not isinstance(predicate, dict)
        or set(predicate) != {"max_count"}
        or isinstance(predicate.get("max_count"), bool)
        or not isinstance(predicate.get("max_count"), int)
        or predicate["max_count"] < 0
    ):
        return None
    return {
        "type": AUTHORSHIP_EVIDENCE_CONTRACT_TYPE,
        "predicate": {"max_count": predicate["max_count"]},
    }


def normalized_authorship_prompt_evidence(
    receipt: Any,
    base_sha: str | None,
    head_sha: str | None,
    categories: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return bounded receipt facts safe to place in an agent prompt."""
    validated = validate_authorship_receipt(receipt, base_sha, head_sha)
    if validated is None:
        state = "missing_or_invalid"
        count = None
        offender_shas: list[str] = []
    else:
        state = str(validated["state"])
        count = validated.get("count") if isinstance(validated.get("count"), int) else None
        offender_shas = [
            item["sha"]
            for item in validated.get("offenders", [])
            if isinstance(item, dict) and isinstance(item.get("sha"), str)
        ]
    contracts = [
        contract
        for category in categories
        if isinstance(category, dict)
        for contract in [normalize_authorship_evidence_contract(category.get("evidence_contract"))]
        if contract is not None
    ]
    return {
        "operator_authored_commits": {
            "state": state,
            "count": count,
            "offender_shas": offender_shas[:MAX_REPORTED_OFFENDERS],
        },
        "range": {
            "base_sha": base_sha[:12] if isinstance(base_sha, str) else None,
            "head_sha": head_sha[:12] if isinstance(head_sha, str) else None,
        },
        "contracts": contracts,
    }


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


def _trusted_blueprint_bot_commit(commit: Any) -> bool:
    """Accept only an immutable GitHub bot/merge identity.

    A commit's display author is user-controlled.  The GitHub ``author`` object,
    numeric account id, raw author email, signature reason, and raw committer are
    all required so a human cannot make a bot-looking commit pass by changing
    only the visible name/login/type fields.
    """
    if not isinstance(commit, dict):
        return False
    identity = commit.get("author")
    commit_payload = commit.get("commit")
    raw_author = commit_payload.get("author") if isinstance(commit_payload, dict) else None
    raw_committer = (
        commit_payload.get("committer") if isinstance(commit_payload, dict) else None
    )
    github_committer = commit.get("committer")
    verification = (
        commit_payload.get("verification") if isinstance(commit_payload, dict) else None
    )
    if not (
        isinstance(identity, dict)
        and identity.get("login") == BOT_COMMIT_AUTHOR
        and identity.get("type") == "Bot"
        and identity.get("id") == BLUEPRINT_GITHUB_BOT_ID
        and isinstance(raw_author, dict)
        and raw_author.get("name") == BOT_COMMIT_AUTHOR
        and raw_author.get("email") in _BOT_AUTHOR_EMAILS
        and isinstance(verification, dict)
        and verification.get("verified") is True
        and verification.get("reason") == "valid"
        and isinstance(raw_committer, dict)
        and isinstance(github_committer, dict)
    ):
        return False
    # GitHub's web merge response uses GitHub/web-flow as the committer.  A
    # direct App commit uses the immutable bot identity for both sides.
    return (
        (
            isinstance(raw_committer.get("name"), str)
            and raw_committer["name"].casefold() in _GITHUB_MERGE_COMMITTERS
            and raw_committer.get("email") == _GITHUB_NOREPLY_EMAIL
            and github_committer.get("login") == "web-flow"
            and github_committer.get("id") == GITHUB_WEB_FLOW_ID
            and github_committer.get("type") == "User"
        )
        or (
            raw_committer.get("name") == BOT_COMMIT_AUTHOR
            and raw_committer.get("email") in _BOT_AUTHOR_EMAILS
            and github_committer.get("login") == BOT_COMMIT_AUTHOR
            and github_committer.get("id") == BLUEPRINT_GITHUB_BOT_ID
            and github_committer.get("type") == "Bot"
        )
    )


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
    if receipt.get("state") not in {"resolved", "unresolved", "terminal"}:
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
    if not isinstance(retry, dict) or set(retry) != {
        "attempt",
        "retryable",
        "next_retry_at",
    }:
        return None
    attempt = retry.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    if not isinstance(retry.get("retryable"), bool):
        return None
    next_retry_at = retry.get("next_retry_at")
    if next_retry_at is not None:
        if not isinstance(next_retry_at, str):
            return None
        try:
            parsed_retry_at = datetime.fromisoformat(next_retry_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed_retry_at.tzinfo is None:
            return None
    if receipt.get("state") == "resolved" and retry["retryable"]:
        return None
    if receipt.get("state") == "unresolved" and not retry["retryable"]:
        return None
    if receipt.get("state") == "terminal" and retry["retryable"]:
        return None
    if receipt.get("state") in {"resolved", "terminal"} and next_retry_at is not None:
        return None
    if receipt.get("state") == "unresolved" and next_retry_at is None:
        return None
    count = receipt.get("count")
    offenders = receipt.get("offenders")
    if not isinstance(offenders, list) or len(offenders) > MAX_REPORTED_OFFENDERS:
        return None
    for offender in offenders:
        if not isinstance(offender, dict) or set(offender) != {"sha", "author"}:
            return None
        if not (
            isinstance(offender.get("sha"), str)
            and _OFFENDER_SHA.fullmatch(offender["sha"])
            and isinstance(offender.get("author"), str)
            and 0 < len(offender["author"]) <= MAX_OFFENDER_AUTHOR_LENGTH
        ):
            return None
    if receipt.get("state") == "resolved":
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if len(offenders) != min(count, MAX_REPORTED_OFFENDERS):
            return None
        if "reason" in receipt:
            return None
    elif (
        count is not None
        or offenders
        or not isinstance(receipt.get("reason"), str)
        or not receipt["reason"]
    ):
        return None
    return receipt


def authorship_category_evidence(
    receipt: Any,
    base_sha: str | None,
    head_sha: str | None,
    categories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project a validated receipt only into explicitly contracted categories."""
    matching = [
        category
        for category in categories
        if isinstance(category, dict)
        and normalize_authorship_evidence_contract(category.get("evidence_contract"))
    ]
    if not matching:
        return []
    validated = validate_authorship_receipt(receipt, base_sha, head_sha)
    result: list[dict[str, Any]] = []
    if validated is not None:
        receipt_state = str(validated.get("state"))
        provenance_source_kind = (
            "github_compare" if receipt_state == "resolved" else "stored_receipt"
        )
        retry = validated.get("retry")
        provenance_extra = {
            "receipt_source": validated.get("source"),
            "receipt_state": receipt_state,
            "receipt_measured_at": validated.get("measured_at"),
            "receipt_attempt": retry.get("attempt") if isinstance(retry, dict) else None,
        }
    elif isinstance(receipt, dict):
        provenance_source_kind = "release_record_validation"
        provenance_extra = {"receipt_state": "missing_or_invalid"}
    else:
        provenance_source_kind = "receipt_unavailable"
        provenance_extra = {"receipt_state": "missing"}
    for category in matching:
        contract = normalize_authorship_evidence_contract(category.get("evidence_contract"))
        max_count = contract["predicate"]["max_count"] if contract else -1
        if validated is not None and validated.get("state") == "resolved":
            count = validated["count"]
            offenders = validated["offenders"]
            status = "met" if count <= max_count else "not_met"
            gap = None if status == "met" else (
                f"{count} non-Blueprint-authored commit(s) exceed the contracted "
                f"maximum of {max_count}."
            )
            citations = [
                {"kind": "diff", "ref": f"release-authorship:{item['sha']}"}
                for item in offenders
            ] or [{"kind": "diff", "ref": "release-authorship:count=0"}]
        else:
            status = "not_met"
            if isinstance(receipt, dict) and receipt.get("state") == "terminal":
                gap = "Authorship receipt is terminal and cannot support a clean qualifier verdict."
            elif isinstance(receipt, dict) and receipt.get("state") == "unresolved":
                gap = "Authorship receipt is unresolved and cannot support a clean qualifier verdict."
            else:
                gap = "Current-schema immutable authorship receipt is missing or invalid."
            citations = [{"kind": "diff", "ref": "release-authorship:unavailable"}]
        result.append(
            {
                "category_key": str(category.get("category_key") or category.get("criterion_key")),
                "status": status,
                "tier": "machine",
                "gap": gap,
                "citations": citations,
                "authoritative": True,
                "evidence_provenance": {
                    "kind": "machine",
                    "source": "release_authorship",
                    "source_kind": provenance_source_kind,
                    **provenance_extra,
                    "range": {"base_sha": base_sha, "head_sha": head_sha},
                },
            }
        )
    return result


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
    if (
        state == "resolved"
        and isinstance(measurement, dict)
        and set(measurement) == {"state", "count", "offenders", "range"}
        and valid_range
    ):
        count = measurement.get("count")
        offenders = measurement.get("offenders")
        if (
            not isinstance(count, bool)
            and isinstance(count, int)
            and count >= 0
            and isinstance(offenders, list)
            and len(offenders) <= MAX_REPORTED_OFFENDERS
            and len(offenders) == min(count, MAX_REPORTED_OFFENDERS)
            and all(
                isinstance(item, dict)
                and set(item) == {"sha", "author"}
                and isinstance(item.get("sha"), str)
                and _OFFENDER_SHA.fullmatch(item["sha"])
                and isinstance(item.get("author"), str)
                and 0 < len(item["author"]) <= MAX_OFFENDER_AUTHOR_LENGTH
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
                "retry": {"attempt": attempt, "retryable": False, "next_retry_at": None},
            }
    reason = (
        measurement.get("reason")
        if isinstance(measurement, dict) and isinstance(measurement.get("reason"), str)
        else "malformed_authorship_measurement"
    )
    retryable = reason not in _PERMANENT_AUTHORSHIP_REASONS
    retry_at = None
    if retryable:
        delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 10)))
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    return {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "unresolved" if retryable else "terminal",
        "count": None,
        "offenders": [],
        "range": {"base_sha": base_sha, "head_sha": head_sha},
        "reason": reason,
        "measured_at": _now_iso(),
        "retry": {
            "attempt": attempt,
            "retryable": retryable,
            "next_retry_at": retry_at,
        },
    }


async def _count_non_bot_authored_commits_from_github(
    base_sha: str,
    head_sha: str,
    *,
    session: Any,
    project: Any,
    github_context: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the exact immutable release range through GitHub Compare."""
    from app import github
    from app.release_promotion import _resolve_github_with_session

    if github_context is not None:
        resolved = github_context
    else:
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
                if not _trusted_blueprint_bot_commit(commit):
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
    github_context: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """Count non-bot authors in ``base_sha..head_sha`` without trusting zero."""
    if not (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        return _unresolved(base_sha, head_sha, "missing_or_malformed_sha")
    if project is None or (session is None and github_context is None):
        return _unresolved(base_sha, head_sha, "github_context_unavailable")
    return await _count_non_bot_authored_commits_from_github(
        base_sha,
        head_sha,
        session=session,
        project=project,
        github_context=github_context,
    )


async def persist_serving_authorship_evidence(
    session: Any, version: Any, *, force: bool = False
) -> dict[str, Any] | None:
    """Measure and CAS-write evidence without holding the serving row lock.

    The caller owns the serving transition transaction. This phase only stages
    the immutable range; measurement runs after the caller commits. ``force``
    is an explicit audited retry override; normal confirmation/recovery/admin
    callers leave it false.
    """
    base_sha, served_sha = serving_authorship_range(version)
    if (
        version.status != "promoted"
        or version.serving_status != "serving_confirmed"
        or not isinstance(base_sha, str)
        or not isinstance(served_sha, str)
    ):
        return None
    before_qualification = dict(version.last_qualification or {})
    before_report = before_qualification.get("operator_authored_commits")
    validated_before = validate_authorship_receipt(before_report, base_sha, served_sha)
    if validated_before is not None and validated_before.get("state") in {
        "resolved",
        "terminal",
    }:
        return None
    if validated_before is not None and validated_before.get("state") == "unresolved":
        next_retry_at = (validated_before.get("retry") or {}).get("next_retry_at")
        if not force and isinstance(next_retry_at, str):
            try:
                if datetime.fromisoformat(next_retry_at.replace("Z", "+00:00")) > datetime.now(
                    timezone.utc
                ):
                    return None
            except ValueError:
                pass
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
    return {
        "version_id": version.id,
        "base_sha": base_sha,
        "served_sha": served_sha,
        "receipt_identity": _receipt_identity(pending),
    }


async def _resolve_authorship_github_context(project: Any) -> tuple[str, str, str] | None:
    """Resolve credentials with no DB session open across external awaits."""
    owner = getattr(project, "github_owner", None)
    repo = getattr(project, "github_repo", None)
    if not owner or not repo:
        return None
    from app import github

    try:
        token = await github.get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001 - evidence remains fail-closed
        token = None
    installation_id = None
    encrypted_pat = None
    if not token and getattr(project, "user_id", None):
        from app.database import AsyncSessionLocal
        from app.models import UserIntegration

        async with AsyncSessionLocal() as metadata_session:
            integration = (
                (
                    await metadata_session.execute(
                        select(UserIntegration).where(
                            UserIntegration.user_id == project.user_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if integration is not None:
                installation_id = integration.github_installation_id
                encrypted_pat = integration.github_pat
        # Metadata is detached and the short session is closed before token or
        # Secret Manager access.
        if installation_id:
            try:
                from app.github import generate_installation_access_token

                token = await generate_installation_access_token(
                    installation_id, repositories=[repo]
                )
            except Exception:  # noqa: BLE001
                token = None
        if not token and encrypted_pat:
            try:
                from app.field_encryption import decrypt_field
                from app.secrets_manager import get_user_secret

                token = await get_user_secret(decrypt_field(encrypted_pat))
            except Exception:  # noqa: BLE001
                token = None
    return (token, owner, repo) if token else None


async def measure_serving_authorship_evidence(
    version_id: Any,
    *,
    session_factory=None,
    expected_receipt_identity: str | None = None,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
) -> bool:
    """Measure a committed pending receipt and atomically CAS its final result."""
    if session_factory is None:
        from app.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    try:
        async with _deadline_session(session_factory, deadline) as snapshot_session:
            await _set_reconciliation_timeouts(snapshot_session, deadline)
            version = (
                (
                    await asyncio.wait_for(
                        snapshot_session.execute(
                            select(ReleaseVersion)
                            .where(ReleaseVersion.id == version_id)
                            .execution_options(populate_existing=True)
                        ),
                        timeout=_remaining_reconciliation_seconds(deadline),
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            base_sha, served_sha = serving_authorship_range(version)
            report = (version.last_qualification or {}).get("operator_authored_commits")
            identity = expected_receipt_identity or _receipt_identity(report)
            if (
                version.status != "promoted"
                or version.serving_status != "serving_confirmed"
                or not isinstance(base_sha, str)
                or not isinstance(served_sha, str)
                or identity is None
                or (
                    validate_authorship_receipt(report, base_sha, served_sha) or {}
                ).get("state")
                in {"resolved", "terminal"}
            ):
                return False
            project = await asyncio.wait_for(
                snapshot_session.get(Project, version.project_id),
                timeout=_remaining_reconciliation_seconds(deadline),
            )
    except asyncio.TimeoutError:
        return False
    if project is None:
        measurement = _unresolved(base_sha, served_sha, "project_unavailable")
    else:
        effective_timeout: float = _AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS
        if timeout_seconds is not None:
            effective_timeout = min(
                effective_timeout, max(0.001, float(timeout_seconds))
            )

        async def _measure() -> dict[str, Any]:
            # Keep credential resolution inside the same hard timeout as the
            # compare request; otherwise a Secret Manager stall escapes the
            # reconciliation deadline.
            context = await _resolve_authorship_github_context(project)
            return await count_non_bot_authored_commits(
                base_sha,
                served_sha,
                project=project,
                github_context=context,
            )

        try:
            measurement = await asyncio.wait_for(_measure(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            measurement = _unresolved(base_sha, served_sha, "github_authorship_timeout")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - confirmation remains truthful
            measurement = _unresolved(base_sha, served_sha, "github_authorship_unavailable")
    pending_retry = report.get("retry") if isinstance(report, dict) else None
    pending_attempt = _next_attempt(report)
    if isinstance(pending_retry, dict):
        candidate_attempt = pending_retry.get("attempt")
        if isinstance(candidate_attempt, int) and not isinstance(candidate_attempt, bool):
            pending_attempt = candidate_attempt
    receipt = _persisted_receipt(
        measurement,
        base_sha,
        served_sha,
        attempt=pending_attempt,
    )
    try:
        async with _deadline_session(session_factory, deadline) as final_session:
            await _set_reconciliation_timeouts(final_session, deadline)
            remaining = _remaining_reconciliation_seconds(deadline)
            if remaining <= 0:
                return False
            current = (
                (
                    await asyncio.wait_for(
                        final_session.execute(
                            select(ReleaseVersion)
                            .where(ReleaseVersion.id == version_id)
                            .execution_options(populate_existing=True)
                            .with_for_update()
                        ),
                        timeout=remaining,
                    )
                )
                .scalars()
                .first()
            )
            if current is None:
                return False
            current_promoted_sha, current_served_sha = serving_authorship_range(current)
            current_report = (current.last_qualification or {}).get(
                "operator_authored_commits"
            )
            if (
                current.status != "promoted"
                or current.serving_status != "serving_confirmed"
                or current_promoted_sha != base_sha
                or current_served_sha != served_sha
                or _receipt_identity(current_report) != identity
            ):
                await asyncio.wait_for(final_session.rollback(), timeout=remaining)
                return False
            current_qualification = dict(current.last_qualification or {})
            current_qualification["operator_authored_commits"] = receipt
            current.last_qualification = current_qualification
            flag_modified(current, "last_qualification")
            final_session.add(current)
            await asyncio.wait_for(
                final_session.commit(),
                timeout=_remaining_reconciliation_seconds(deadline),
            )
        return True
    except asyncio.TimeoutError:
        return False


def _remaining_reconciliation_seconds(deadline: float | None) -> float:
    if deadline is None:
        return _RECONCILIATION_MAX_SECONDS
    return max(0.0, deadline - time.monotonic())


async def _set_reconciliation_timeouts(session: Any, deadline: float | None) -> None:
    """Apply transaction-local Postgres timeouts without affecting SQLite tests."""
    if _remaining_reconciliation_seconds(deadline) <= 0:
        raise asyncio.TimeoutError
    dialect = resolve_dialect_name(session)
    if dialect != "postgresql":
        return
    milliseconds = max(1, int(_remaining_reconciliation_seconds(deadline) * 1000))
    await asyncio.wait_for(
        session.execute(text(f"SET LOCAL statement_timeout = {milliseconds}")),
        timeout=_remaining_reconciliation_seconds(deadline),
    )
    await asyncio.wait_for(
        session.execute(text(f"SET LOCAL lock_timeout = {milliseconds}")),
        timeout=_remaining_reconciliation_seconds(deadline),
    )


@asynccontextmanager
async def _deadline_session(session_factory: Any, deadline: float | None):
    """Yield a DB session; the deadline is enforced per statement, not per close."""
    async with session_factory() as session:
        yield session


def _reconciliation_candidate_is_eligible(
    report: Any,
    base_sha: str | None,
    served_sha: str | None,
    now: datetime,
) -> bool:
    """Keep malformed JSON in the repair population; Python validation is authoritative."""
    validated = validate_authorship_receipt(report, base_sha, served_sha)
    if validated is None:
        return True
    if validated.get("state") in {"resolved", "terminal"}:
        return False
    retry = validated.get("retry")
    next_retry_at = retry.get("next_retry_at") if isinstance(retry, dict) else None
    if not isinstance(next_retry_at, str):
        return True
    try:
        return datetime.fromisoformat(next_retry_at.replace("Z", "+00:00")) <= now
    except ValueError:
        return True


async def reconcile_serving_authorship_evidence(
    session_factory=None,
    *,
    limit: int = MAX_RECONCILIATION_VERSIONS,
) -> int:
    """Boundedly, fairly retry eligible promoted receipts.

    Candidates are ordered by oldest ``served_at`` then id, resolved/terminal
    rows are filtered before the batch is consumed, and a short row-lock claim
    writes a durable lease into the receipt before any GitHub await.  Thus two
    sweepers cannot duplicate a measurement and a large set of newer resolved
    rows cannot starve an old unresolved release.
    """
    if session_factory is None:
        from app.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    bounded_limit = max(1, min(int(limit), MAX_RECONCILIATION_VERSIONS))
    deadline = time.monotonic() + _RECONCILIATION_MAX_SECONDS
    version_ids: list[Any] = []
    selected_ids: set[Any] = set()
    wrapped = False

    # The singleton state row is the fleet-wide short CAS.  No process-global
    # cursor or asyncio lock is used: a second worker either gets a different
    # committed page or skips the locked state row and lets the next tick run.
    while len(version_ids) < bounded_limit and _remaining_reconciliation_seconds(deadline) > 0:
        try:
            async with _deadline_session(session_factory, deadline) as scan_session:
                await _set_reconciliation_timeouts(scan_session, deadline)
                state = (
                    (
                        await asyncio.wait_for(
                            scan_session.execute(
                                select(ReleaseAuthorshipReconciliationState)
                                .where(ReleaseAuthorshipReconciliationState.id == 1)
                                .with_for_update(skip_locked=True)
                            ),
                            timeout=_remaining_reconciliation_seconds(deadline),
                        )
                    )
                    .scalars()
                    .first()
                )
                if state is None:
                    # SQLite/unit fixtures do not run migrations.  Production
                    # always has the singleton from 0756; this insert keeps the
                    # injected test database faithful without a process cache.
                    existing_state = (
                        (
                            await asyncio.wait_for(
                                scan_session.execute(
                                    select(ReleaseAuthorshipReconciliationState.id).where(
                                        ReleaseAuthorshipReconciliationState.id == 1
                                    )
                                ),
                                timeout=_remaining_reconciliation_seconds(deadline),
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing_state is not None:
                        return 0
                    state = ReleaseAuthorshipReconciliationState(id=1)
                    scan_session.add(state)
                    try:
                        await scan_session.flush()
                    except IntegrityError:
                        # Another first-use worker inserted the fixture row. Its
                        # committed state owns this scan tick; do not retry with
                        # a process-local fallback or duplicate its page.
                        await scan_session.rollback()
                        return 0
                conditions = [
                    ReleaseVersion.status == "promoted",
                    ReleaseVersion.serving_status == "serving_confirmed",
                    ReleaseVersion.promoted_sha.is_not(None),
                    ReleaseVersion.served_sha.is_not(None),
                ]
                if state.cursor_partition == 0:
                    conditions.append(ReleaseVersion.served_at.is_(None))
                    if state.null_cursor_id is not None:
                        conditions.append(ReleaseVersion.id > state.null_cursor_id)
                    ordering = ReleaseVersion.id.asc()
                else:
                    conditions.append(ReleaseVersion.served_at.is_not(None))
                    if state.served_at_cursor is not None and state.served_id_cursor is not None:
                        conditions.append(
                            or_(
                                ReleaseVersion.served_at > state.served_at_cursor,
                                and_(
                                    ReleaseVersion.served_at == state.served_at_cursor,
                                    ReleaseVersion.id > state.served_id_cursor,
                                ),
                            )
                        )
                    ordering = (
                        ReleaseVersion.served_at.asc(),
                        ReleaseVersion.id.asc(),
                    )
                query = select(ReleaseVersion).where(*conditions)
                query = query.order_by(*ordering) if isinstance(ordering, tuple) else query.order_by(ordering)
                query = query.limit(_RECONCILIATION_SCAN_PAGE_SIZE).with_for_update(
                    skip_locked=True
                )
                candidates = list(
                    (
                        await asyncio.wait_for(
                            scan_session.execute(query),
                            timeout=_remaining_reconciliation_seconds(deadline),
                        )
                    )
                    .scalars()
                    .all()
                )
                if not candidates and state.cursor_partition == 0:
                    state.cursor_partition = 1
                    state.null_cursor_id = None
                    state.updated_at = datetime.now(timezone.utc)
                    scan_session.add(state)
                    await asyncio.wait_for(
                        scan_session.commit(),
                        timeout=_remaining_reconciliation_seconds(deadline),
                    )
                    continue
                elif not candidates:
                    state.cursor_partition = 0
                    state.null_cursor_id = None
                    state.served_at_cursor = None
                    state.served_id_cursor = None
                    state.generation += 1
                    state.updated_at = datetime.now(timezone.utc)
                    scan_session.add(state)
                    await asyncio.wait_for(
                        scan_session.commit(),
                        timeout=_remaining_reconciliation_seconds(deadline),
                    )
                    if wrapped:
                        break
                    wrapped = True
                    continue
                if candidates:
                    # Do not advance beyond an eligible candidate that was
                    # not selected only because this sweep reached ``limit``.
                    # Otherwise a small limit skips the remainder of a page
                    # until a future EOF wrap.
                    last_inspected = candidates[-1]
                    now = datetime.now(timezone.utc)
                    for candidate in candidates:
                        report = (candidate.last_qualification or {}).get(
                            "operator_authored_commits"
                        )
                        if (
                            candidate.id not in selected_ids
                            and _reconciliation_candidate_is_eligible(
                                report, *serving_authorship_range(candidate), now
                            )
                        ):
                            selected_ids.add(candidate.id)
                            version_ids.append(candidate.id)
                            last_inspected = candidate
                            if len(version_ids) >= bounded_limit:
                                break
                    if state.cursor_partition == 0:
                        state.null_cursor_id = last_inspected.id
                    else:
                        state.served_at_cursor = last_inspected.served_at
                        state.served_id_cursor = last_inspected.id
                    state.updated_at = datetime.now(timezone.utc)
                    scan_session.add(state)
                    await asyncio.wait_for(
                        scan_session.commit(),
                        timeout=_remaining_reconciliation_seconds(deadline),
                    )
        except StaleDataError:
            # SQLite does not implement row-level ``skip_locked``. A second
            # worker can therefore race the singleton update in unit tests;
            # losing that CAS is a normal contention result, not a reason to
            # retry the page with an in-process cursor.
            break
        except asyncio.TimeoutError:
            break
        except Exception:
            logger.exception(
                "authorship evidence reconciliation stopped after scan session cleanup failure"
            )
            return 0

    attempted = 0
    for version_id in version_ids:
        if (
            attempted >= _RECONCILIATION_MAX_CALLS
            or _remaining_reconciliation_seconds(deadline) <= 0
        ):
            break
        plan: dict[str, Any] | None = None
        version: Any = None
        try:
            async with _deadline_session(session_factory, deadline) as worker_session:
                await _set_reconciliation_timeouts(worker_session, deadline)
                version = (
                    (
                        await asyncio.wait_for(
                            worker_session.execute(
                                select(ReleaseVersion)
                                .where(ReleaseVersion.id == version_id)
                                .execution_options(populate_existing=True)
                                .with_for_update(skip_locked=True)
                            ),
                            timeout=_remaining_reconciliation_seconds(deadline),
                        )
                    )
                    .scalars()
                    .first()
                )
                if version is None:
                    continue
                report = (version.last_qualification or {}).get(
                    "operator_authored_commits"
                )
                validated = validate_authorship_receipt(
                    report, *serving_authorship_range(version)
                )
                if validated is not None and validated.get("state") in {
                    "resolved",
                    "terminal",
                }:
                    continue
                retry = report.get("retry") if isinstance(report, dict) else None
                next_retry_at = retry.get("next_retry_at") if isinstance(retry, dict) else None
                if isinstance(next_retry_at, str):
                    try:
                        if datetime.fromisoformat(
                            next_retry_at.replace("Z", "+00:00")
                        ) > datetime.now(timezone.utc):
                            continue
                    except ValueError:
                        pass
                plan = await asyncio.wait_for(
                    persist_serving_authorship_evidence(worker_session, version),
                    timeout=_remaining_reconciliation_seconds(deadline),
                )
                if plan is None:
                    continue
                # Commit the durable claim, then leave the DB session before
                # GitHub/Secret Manager I/O.
                await asyncio.wait_for(
                    worker_session.commit(),
                    timeout=_remaining_reconciliation_seconds(deadline),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "authorship evidence reconciliation stopped after session cleanup failure"
            )
            break

        attempted += 1
        try:
            await asyncio.wait_for(
                measure_serving_authorship_evidence(
                    version_id,
                    session_factory=session_factory,
                    expected_receipt_identity=plan["receipt_identity"],
                    timeout_seconds=max(0.001, deadline - time.monotonic()),
                    deadline=deadline,
                ),
                timeout=_remaining_reconciliation_seconds(deadline),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "authorship evidence reconciliation failed for release %s", version_id
            )
    return attempted
