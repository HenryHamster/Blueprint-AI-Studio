"""Project-scoped ledger and review-time escalation for recurring concerns."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy.orm.attributes import flag_modified

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.finding_identity import audit_finding_key
from app.issue_ledger import ISSUE_MATCH_JACCARD_THRESHOLD, _jaccard
from app.models import ModuleFindingChain, Task
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

CHAIN_ESCALATION_THRESHOLD = 3
CHAIN_PR_NUMBERS_MAX_ENTRIES = 20


def _blocking(issue: Mapping[str, Any]) -> bool:
    return (
        str(issue.get("severity") or "").upper() in {"CRITICAL", "MAJOR"}
        and bool(str(issue.get("description") or "").strip())
        and bool(str(issue.get("file") or "").strip())
    )


def _review_history(context: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    loop = (context or {}).get("review_loop") or {}
    history = loop.get("review_history")
    if not isinstance(history, list):
        history = (context or {}).get("review_history")
    return [entry for entry in history or [] if isinstance(entry, dict)][
        -REVIEW_HISTORY_MAX_ENTRIES:
    ]


def resolved_review_findings(context: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return distinct blocking findings explicitly resolved in this PR."""
    ctx = context or {}
    loop = ctx.get("review_loop") or {}
    ledger = loop.get("issue_ledger") or ctx.get("issue_ledger") or {}
    resolved_ids = {
        str(item.get("issue_id") or "")
        for item in (ledger.get("resolved") or [])
        if isinstance(item, dict)
        and str(item.get("disposition") or "").lower()
        in {"fixed", "refuted", "adjudicated"}
    }
    for entry in _review_history(ctx):
        for resolution in entry.get("resolutions") or []:
            if not isinstance(resolution, dict):
                continue
            if str(resolution.get("disposition") or "").lower() in {
                "fixed",
                "refuted",
                "adjudicated",
            }:
                resolved_ids.add(str(resolution.get("issue_id") or ""))

    findings: list[dict[str, Any]] = []
    for entry in _review_history(ctx):
        for issue in entry.get("issues") or []:
            if not isinstance(issue, dict) or not _blocking(issue):
                continue
            issue_id = str(issue.get("issue_id") or "")
            if issue_id and issue_id not in resolved_ids:
                continue
            if not issue_id:
                continue
            if any(
                existing.get("file") == issue.get("file")
                and _jaccard(
                    str(existing.get("description") or ""),
                    str(issue.get("description") or ""),
                )
                >= ISSUE_MATCH_JACCARD_THRESHOLD
                for existing in findings
            ):
                continue
            findings.append(dict(issue))
    return findings


def _matching_chain(
    rows: Iterable[ModuleFindingChain], issue: Mapping[str, Any]
) -> ModuleFindingChain | None:
    file_path = str(issue.get("file") or "").strip()
    summary = str(issue.get("description") or "").strip()
    if not file_path or not summary:
        return None
    exact_key = audit_finding_key(summary, file_path)
    matches = [
        row
        for row in rows
        if row.file_path == file_path
        and (
            row.finding_key == exact_key
            or _jaccard(row.summary, summary) >= ISSUE_MATCH_JACCARD_THRESHOLD
        )
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda row: (
            row.finding_key == exact_key,
            _jaccard(row.summary, summary),
        ),
    )


def _pr_number(value: int | str | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def record_merged_pr_finding_chains(
    task: Task,
    session: AsyncSession,
    *,
    pr_number: int | str | None = None,
) -> None:
    """Best-effort savepoint-scoped write for a merged PR's finding chains."""
    try:
        async with session.begin_nested():
            with session.no_autoflush:
                touched = await _record_merged_pr_finding_chains(
                    task, session, pr_number=pr_number
                )
            if touched:
                await session.flush(touched)
    except IntegrityError:
        # A concurrent landing may have won the unique project/key race. The
        # landing itself remains authoritative; the next replay can observe the
        # durable row and update it without poisoning the caller transaction.
        logger.warning(
            "module-finding-chain upsert raced for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )


async def _record_merged_pr_finding_chains(
    task: Task,
    session: AsyncSession,
    *,
    pr_number: int | str | None = None,
) -> list[object]:
    """Upsert resolved blocking concerns once for an integration landing."""
    if (task.context or {}).get("pr_closed_empty"):
        return []
    if pr_number is None:
        from app.ci_merge_helpers import effective_pr_number

        pr_number = effective_pr_number(task)
    number = _pr_number(pr_number)
    if number is None:
        return []
    context = dict(task.context or {})
    if context.get("_module_finding_chain_recorded_pr") == number:
        return []

    findings = resolved_review_findings(context)
    if not findings:
        context["_module_finding_chain_recorded_pr"] = number
        task.context = context
        flag_modified(task, "context")
        session.add(task)
        return [task]

    rows = list(
        (
            await session.execute(
                select(ModuleFindingChain).where(
                    ModuleFindingChain.project_id == task.project_id
                )
            )
        )
        .scalars()
        .all()
    )
    verified_paths = {
        path
        for path in (context.get("_pr_changed_files") or [])
        if isinstance(path, str) and path.strip()
    }
    files_verified = context.get("_pr_changed_files_github_verified") is True
    touched: list[object] = []
    for issue in findings:
        file_path = str(issue.get("file") or "").strip()
        matched = _matching_chain(rows, issue)
        verified = files_verified and file_path in verified_paths
        if matched is None:
            matched = ModuleFindingChain(
                project_id=task.project_id,
                finding_key=audit_finding_key(
                    str(issue.get("description") or ""), file_path
                ),
                file_path=file_path,
                summary=str(issue.get("description") or "").strip(),
                occurrences=0,
                verified_occurrences=0,
                pr_numbers=[],
                first_seen_pr=number,
                last_seen_pr=number,
                last_seen_at=_utcnow(),
                status="open",
                file_provenance="verified" if verified else "unverified",
            )
            rows.append(matched)
        pr_numbers = [int(item) for item in (matched.pr_numbers or []) if item]
        if number in pr_numbers:
            continue
        matched.occurrences = int(matched.occurrences or 0) + 1
        if verified:
            matched.verified_occurrences = int(
                matched.verified_occurrences or 0
            ) + 1
        pr_numbers.append(number)
        matched.pr_numbers = pr_numbers[-CHAIN_PR_NUMBERS_MAX_ENTRIES:]
        if not matched.first_seen_pr:
            matched.first_seen_pr = number
        matched.last_seen_pr = number
        matched.last_seen_at = _utcnow()
        if verified:
            matched.file_provenance = "verified"
        session.add(matched)
        touched.append(matched)

    context["_module_finding_chain_recorded_pr"] = number
    task.context = context
    flag_modified(task, "context")
    session.add(task)
    touched.append(task)
    return touched


async def find_and_mark_chain_escalation(
    session: AsyncSession,
    *,
    project_id: Any,
    issues: Iterable[Mapping[str, Any]],
    authoritative_files: list[dict] | None,
) -> list[dict[str, Any]]:
    """Find every distinct recurring concern past threshold on this PR.

    A PR can legitimately raise two unrelated concerns that have each
    independently crossed the escalation threshold in different chains; both
    must reach the operator, so this returns one candidate per distinct
    matched chain rather than stopping at the first blocking issue. Noise
    control for repeated escalation of the SAME concern (re-entry on the same
    PR, or a new PR with no new merged occurrence) is the downstream alert
    dedup (``alert_dispatch._maybe_create_escalation_alert``), keyed on the
    concern's identity and occurrence count rather than the PR number.
    """
    rows = list(
        (
            await session.execute(
                select(ModuleFindingChain).where(
                    ModuleFindingChain.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    changed_paths = {
        str(item.get("filename") or "").strip()
        for item in (authoritative_files or [])
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    }
    candidates: list[dict[str, Any]] = []
    seen_chain_ids: set[int] = set()
    for issue in issues:
        if not _blocking(issue):
            continue
        matched = _matching_chain(rows, issue)
        if matched is None:
            continue
        if id(matched) in seen_chain_ids:
            continue
        if int(matched.verified_occurrences or 0) < CHAIN_ESCALATION_THRESHOLD:
            continue
        seen_chain_ids.add(id(matched))
        candidates.append(
            {
                "status": "chain_escalation",
                "finding_key": matched.finding_key,
                "file_path": matched.file_path,
                "occurrences": matched.occurrences,
                "verified_occurrences": matched.verified_occurrences,
                "pr_numbers": list(matched.pr_numbers or []),
                "threshold": CHAIN_ESCALATION_THRESHOLD,
                "file_provenance": (
                    "verified"
                    if str(issue.get("file") or "").strip() in changed_paths
                    else "unverified"
                ),
            }
        )
    return candidates
