"""Project-scoped ledger and review-time escalation for recurring concerns."""

from __future__ import annotations

import logging
import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy.orm.attributes import flag_modified

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.finding_identity import audit_finding_key
from app.issue_ledger import ISSUE_MATCH_JACCARD_THRESHOLD, _jaccard
from app.models import ModuleFindingChain, ProjectMessage, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

CHAIN_ESCALATION_THRESHOLD = 3
CHAIN_PR_NUMBERS_MAX_ENTRIES = 20
CHAIN_SOURCE_TASK_IDS_MAX_ENTRIES = 20
GUARD_SUPPRESSION_CHAIN_KIND = "guard_suppression"
_TASK_RECORDED_GUARD_KEYS = "_guard_suppression_recorded_keys"
_TASK_RECORDED_GUARD_KEYS_MAX_ENTRIES = 1024
GUARD_SUPPRESSION_RECEIPT_FRESHNESS = timedelta(days=1)
GUARD_SUPPRESSION_ELECTION_BATCH = 25


def _guard_chain_key(lane: str, identity: str) -> str:
    """Return the stable key for one structured guard FailureIdentity.

    Guard output text is intentionally excluded: it can contain counts and
    paths that change while the failure remains inherited. The lane plus the
    route-keyed identity is the durable identity emitted by the guard itself.
    """
    return hashlib.sha256(f"guard-suppression\0{lane}\0{identity}".encode()).hexdigest()


def extract_guard_suppression_identities(
    receipt: Mapping[str, Any] | None,
    *,
    require_fresh: bool = True,
) -> list[tuple[str, str]] | None:
    """Extract complete structured suppressions, or ``None`` on uncertainty.

    A missing guard projection on a validated passing receipt is an explicit
    clean result. Truncated/malformed data returns ``None`` rather than a
    partial list, because partial data must neither manufacture an owner nor
    prove a remediation clean. Older backend-pytest attribution is accepted
    only when its identity is namespaced; rendered failure details are never
    used as a fallback identity.

    ``require_fresh`` gates the recency window. It is on for ingestion, where a
    long-dead receipt must not manufacture a new occurrence or owner. It is off
    for the resolution path: that receipt is written by the remediation task's
    own ``./check`` run and read when that same task's PR merges, so it is
    already scoped to one task lifetime. A PR that sits in review or CI for
    longer than the window — or an agent VM whose clock skews ``recorded_at``
    slightly into the future — must not strand its chain permanently open.
    """
    if (
        not isinstance(receipt, Mapping)
        or not str(receipt.get("status") or "").startswith("passed")
        or (require_fresh and not _guard_suppression_receipt_is_fresh(receipt))
        or not _guard_suppression_receipt_is_parseable(receipt)
    ):
        return None
    extracted: set[tuple[str, str]] = set()
    guard = receipt.get("guard_suppressions")
    if guard is not None:
        counts = receipt.get("guard_suppressions_counts")
        if (
            not isinstance(guard, Mapping)
            or not _has_complete_count(counts, len(guard), "observed_lanes", "kept_lanes")
        ):
            return None
        for raw_lane, raw_lane_record in guard.items():
            if not isinstance(raw_lane, str) or not raw_lane or not isinstance(raw_lane_record, Mapping):
                return None
            for bucket in ("suppressed", "disappeared", "skipped"):
                entries = raw_lane_record.get(bucket)
                if entries is None and bucket != "suppressed":
                    continue
                if not isinstance(entries, Mapping):
                    return None
                observed = raw_lane_record.get(f"{bucket}_observed_count", len(entries))
                truncated = raw_lane_record.get(f"{bucket}_truncated", False)
                if (
                    isinstance(observed, bool)
                    or not isinstance(observed, int)
                    or observed != len(entries)
                    or not isinstance(truncated, bool)
                    or truncated
                ):
                    return None
            for identity in raw_lane_record["suppressed"]:
                if not isinstance(identity, str) or not identity:
                    return None
                extracted.add((raw_lane, identity))

    # Compatible pre-cross-lane receipts retain a backend pytest suppression
    # map. It is a fallback only: current receipts deliberately persist both
    # projections, but their namespace-form legacy identity differs from the
    # canonical cross-lane identity and must not form a second chain.
    if guard is None:
        legacy = receipt.get("suppressed_failures")
        if legacy is not None:
            if not isinstance(legacy, Mapping):
                return None
            attribution_counts = receipt.get("attribution_counts")
            if attribution_counts is not None and not _has_complete_count(
                attribution_counts.get("suppressed_failures")
                if isinstance(attribution_counts, Mapping)
                else None,
                len(legacy),
                "observed",
                "kept",
            ):
                return None
            for identity in legacy:
                if not isinstance(identity, str) or not identity:
                    return None
                lane = _publication_lane(identity) or "backend-pytest"
                extracted.add((lane, identity))
        elif isinstance(receipt.get("attribution_counts"), Mapping) and "suppressed_failures" in receipt["attribution_counts"]:
            # A retained count row without the corresponding map means sanitization
            # discarded an unknown number of legacy identities.
            return None
    return sorted(extracted)


def _has_complete_count(
    counts: object, expected: int, observed_key: str, kept_key: str
) -> bool:
    if not isinstance(counts, Mapping):
        return False
    observed = counts.get(observed_key)
    kept = counts.get(kept_key)
    return (
        not isinstance(observed, bool)
        and not isinstance(kept, bool)
        and isinstance(observed, int)
        and isinstance(kept, int)
        and observed == expected
        and kept == expected
    )


def _parse_receipt_recorded_at(receipt: Mapping[str, Any]) -> datetime | None:
    """Return the receipt's naive-UTC ``recorded_at``, or ``None`` if unusable."""
    recorded_at = receipt.get("recorded_at")
    if not isinstance(recorded_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _guard_suppression_receipt_is_parseable(receipt: Mapping[str, Any]) -> bool:
    """A receipt without a well-formed stamp is malformed on every path."""
    return _parse_receipt_recorded_at(receipt) is not None


def _guard_suppression_receipt_is_fresh(receipt: Mapping[str, Any]) -> bool:
    """Reject stale or malformed receipts before they can prove a new occurrence."""
    parsed = _parse_receipt_recorded_at(receipt)
    if parsed is None:
        return False
    now = _utcnow()
    return now - GUARD_SUPPRESSION_RECEIPT_FRESHNESS <= parsed <= now


def _publication_lane(identity: str) -> str | None:
    if identity.startswith("[") and "] " in identity:
        lane = identity[1 : identity.index("] ")]
        return lane or None
    return None


def _receipt_is_complete_and_clean(receipt: Mapping[str, Any] | None) -> bool:
    """Clean-landing proof for the resolution path only.

    Deliberately freshness-exempt — see ``extract_guard_suppression_identities``.
    Every other completeness/validation rule still applies.
    """
    identities = extract_guard_suppression_identities(receipt, require_fresh=False)
    return identities == []


def _guard_remediation_task(chain: ModuleFindingChain) -> Task:
    """Build the one normal queued task that owns a mature chain.

    The active-task idempotency key is the same task-admission primitive used
    elsewhere. Persisting the selected task id on the chain makes retries and
    concurrent heartbeats converge on one owner rather than notifying per
    source task.
    """
    key = f"guard-suppression:{chain.project_id}:{chain.finding_key}"
    return Task(
        project_id=chain.project_id,
        title=f"fix: resolve recurring {chain.suppression_lane or 'guard'} baseline failure",
        payloads=[
            {
                "type": "fix",
                "effort": "medium",
                "title": "Resolve recurring baseline guard suppression",
                "prompt": (
                    "Remove the named inherited guard failure and verify a clean "
                    "publication-gate receipt. Do not suppress or rename the identity."
                ),
            }
        ],
        context={
            "spawned_by": "guard_suppression_remediation",
            "guard_suppression_remediation_keys": [chain.finding_key],
            "guard_suppression_lane": chain.suppression_lane,
            "guard_suppression_failure_identity": chain.failure_identity,
        },
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REMEDIATION",
        idempotency_key=key[:255],
    )


async def _post_guard_suppression_escalation(
    session: AsyncSession, chain: ModuleFindingChain, owner: Task
) -> None:
    """Emit one aggregate, audience-classified escalation for the chain."""
    if not hasattr(session, "scalar"):
        # Lightweight unit-test sessions exercise ledger semantics without the
        # message table; production AsyncSession always owns this method.
        return
    from app.notification_dispatch import post_event_message

    message = await post_event_message(
        session,
        chain.project_id,
        "remediation_escalated",
        (
            f"Recurring baseline guard suppression reached {CHAIN_ESCALATION_THRESHOLD} "
            f"independent tasks; remediation task {owner.id} now owns it."
        ),
        context={
            "finding_key": chain.finding_key,
            "lane": chain.suppression_lane,
            "failure_identity": chain.failure_identity,
            "occurrences": chain.occurrences,
            "remediation_task_id": str(owner.id),
        },
        task_id=owner.id,
        idempotency_key=f"guard-suppression:{chain.project_id}:{chain.finding_key}:escalation",
    )
    if message is not None and message.status == "resolved":
        # The stable idempotency key is the aggregate's identity. Reopen that
        # one message for a new lifecycle instead of emitting a per-recurrence
        # operator notification.
        message.status = "open"
        message.resolution = None
        message.resolved_at = None
        context = dict(message.context or {})
        context.update(
            {
                "occurrences": chain.occurrences,
                "remediation_task_id": str(owner.id),
            }
        )
        message.context = context
        session.add(message)


async def ingest_guard_suppression_receipt(task: Task, session: AsyncSession) -> list[ModuleFindingChain]:
    """Ingest the task's current durable receipt exactly once per identity.

    This is the canonical heartbeat seam. It reads only the current receipt;
    bounded receipt history is explanatory and cannot add an occurrence again.
    """
    receipt = (task.context or {}).get("publication_gate_receipt")
    identities = extract_guard_suppression_identities(receipt if isinstance(receipt, Mapping) else None)
    if identities is None:
        # Fail closed for malformed/truncated producer data rather than
        # silently treating it as a clean result.
        return []
    if not identities:
        return []

    # A concurrent first insert races on the chain's unique identity. The
    # savepoint rollback leaves this session usable; re-read once so the task
    # whose receipt is still current is recorded against the winner rather than
    # being permanently lost behind the heartbeat's unchanged-receipt fence.
    try:
        return await _ingest_guard_suppression_receipt_once(task, session, identities)
    except IntegrityError:
        logger.info(
            "guard-suppression chain upsert raced for task %s; retrying",
            task.id,
        )
    try:
        return await _ingest_guard_suppression_receipt_once(task, session, identities)
    except IntegrityError:
        logger.warning(
            "guard-suppression chain upsert remained contended for task %s",
            task.id,
            exc_info=True,
        )
    return []


async def _ingest_guard_suppression_receipt_once(
    task: Task, session: AsyncSession, identities: list[tuple[str, str]]
) -> list[ModuleFindingChain]:
    """Record one current receipt inside a retryable savepoint."""
    # The chain keeps only bounded human-auditable task provenance. Keep the
    # exact replay fence on the source task as well, so a very old task cannot
    # increment a chain again merely because its UUID aged out of that bounded
    # chain-level list.
    context = dict(task.context or {})
    recorded = {
        value
        for value in context.get(_TASK_RECORDED_GUARD_KEYS, [])
        if isinstance(value, str)
    }
    identity_keys = {_guard_chain_key(lane, identity) for lane, identity in identities}
    pending = [item for item in identities if _guard_chain_key(*item) not in recorded]
    if not pending:
        return []
    if len(recorded | identity_keys) > _TASK_RECORDED_GUARD_KEYS_MAX_ENTRIES:
        return []

    async with session.begin_nested():
        rows = list(
            (
                await session.execute(
                    select(ModuleFindingChain).where(
                        ModuleFindingChain.project_id == task.project_id
                    )
                )
            ).scalars().all()
        )
        touched: list[ModuleFindingChain] = []
        task_id = str(task.id)
        for lane, identity in pending:
            key = _guard_chain_key(lane, identity)
            chain = next(
                (
                    row
                    for row in rows
                    if row.chain_kind == GUARD_SUPPRESSION_CHAIN_KIND
                    and row.finding_key == key
                ),
                None,
            )
            if chain is None:
                chain = ModuleFindingChain(
                    project_id=task.project_id,
                    finding_key=key,
                    file_path=f"guard:{lane}",
                    summary="Recurring baseline guard suppression",
                    chain_kind=GUARD_SUPPRESSION_CHAIN_KIND,
                    suppression_lane=lane,
                    failure_identity=identity,
                    source_task_ids=[],
                    pr_numbers=[],
                    first_seen_pr=0,
                    last_seen_pr=0,
                    occurrences=0,
                    verified_occurrences=0,
                    status="open",
                )
                rows.append(chain)
            elif chain.status == "resolved":
                # A later occurrence starts a fresh lifecycle: the prior
                # resolved owner/escalation cannot own a new recurrence.
                chain.status = "open"
                chain.remediation_task_id = None
                chain.resolved_by_task_id = None
                chain.resolved_at = None
                chain.source_task_ids = []
                chain.occurrences = 0
                chain.verified_occurrences = 0
            source_ids = [str(value) for value in (chain.source_task_ids or []) if value]
            if task_id not in source_ids:
                source_ids.append(task_id)
                chain.source_task_ids = source_ids[-CHAIN_SOURCE_TASK_IDS_MAX_ENTRIES:]
                chain.occurrences = int(chain.occurrences or 0) + 1
                chain.verified_occurrences = int(chain.verified_occurrences or 0) + 1
                chain.last_seen_at = _utcnow()
            if chain.status == "open" and chain.remediation_task_id is not None:
                # This receipt is fresh evidence that the suppression recurred
                # while an owner was supposed to be fixing it. If that owner has
                # already failed or been cancelled without resolving the chain,
                # it is never coming back: release the slot so the threshold can
                # elect a new owner. Without this the chain is stuck open
                # forever behind a dead owner and its aggregate operator message
                # can never close.
                await _release_terminal_remediation_owner(session, chain)
            # Owner election deliberately does NOT happen here; see
            # ``elect_guard_suppression_owners``. This runs inside
            # ``agent_heartbeat``, which already holds the reporting Task row
            # FOR UPDATE, so taking the Project admission lock behind it would
            # invert the canonical Project -> Task order on the fleet's
            # liveness-critical path. Ingestion only records evidence.
            session.add(chain)
            if chain not in touched:
                touched.append(chain)
        if touched:
            await session.flush(touched)
        context[_TASK_RECORDED_GUARD_KEYS] = sorted(recorded | identity_keys)
        task.context = context
        session.add(task)
        return touched


# Terminal outcomes from which an owner can never resolve its chain. COMPLETED
# is deliberately absent: a completed owner's landed-signal resolution can still
# be in flight, and releasing it there would elect a redundant second owner and
# then leave the real resolution unable to match ``remediation_task_id``.
_DEAD_REMEDIATION_OWNER_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def _release_terminal_remediation_owner(
    session: AsyncSession, chain: ModuleFindingChain
) -> None:
    """Clear a remediation owner that terminated without resolving the chain."""
    owner_id = chain.remediation_task_id
    if owner_id is None:
        return
    if not hasattr(session, "get"):
        # Ledger-only unit sessions have no task store to consult; leaving the
        # owner in place is the conservative outcome there.
        return
    owner = await session.get(Task, owner_id)
    if owner is None:
        # The owner row is gone entirely — nothing can resolve the chain
        # through it, so re-election is strictly better than staying stuck.
        chain.remediation_task_id = None
        return
    if owner.status in _DEAD_REMEDIATION_OWNER_STATUSES:
        logger.info(
            "guard-suppression chain %s releasing dead owner %s (%s)",
            chain.finding_key,
            owner_id,
            owner.status,
        )
        chain.remediation_task_id = None


def _chain_awaits_remediation_owner(chain: ModuleFindingChain) -> bool:
    """Return whether this persisted chain row is due an owner right now."""
    return (
        chain.chain_kind == GUARD_SUPPRESSION_CHAIN_KIND
        and chain.status == "open"
        and chain.remediation_task_id is None
        and int(chain.verified_occurrences or 0) >= CHAIN_ESCALATION_THRESHOLD
    )


async def elect_guard_suppression_owners(
    session: AsyncSession, *, limit: int = GUARD_SUPPRESSION_ELECTION_BATCH
) -> list[Task]:
    """Elect one remediation owner per mature chain, under Project authority.

    Election is a separate leader-owned sweep rather than part of receipt
    ingestion. Ingestion's only production caller is ``agent_heartbeat``, which
    already holds the reporting Task row ``FOR UPDATE``; acquiring the Project
    admission lock there would invert the canonical Project -> Task order on a
    liveness-critical path, and skipping the lock would insert QUEUED work into
    paused and archived projects. The election predicate is fully derivable
    from the persisted row -- open, at threshold, unowned -- so the same
    decision can be made here, a moment later, under the correct lock.

    Bounded per invocation and idempotent: a chain that already has an owner is
    no longer a candidate, and a project that is paused, archived, or
    momentarily lock-contended is simply skipped until the next sweep.
    """
    from app.task_locking import (
        AUTOMATIC_TASK_CREATION_ALLOWED,
        acquire_automatic_task_creation_authority,
    )

    candidates = list(
        (
            await session.execute(
                select(ModuleFindingChain)
                .where(
                    ModuleFindingChain.chain_kind == GUARD_SUPPRESSION_CHAIN_KIND,
                    ModuleFindingChain.status == "open",
                    ModuleFindingChain.remediation_task_id.is_(None),
                    ModuleFindingChain.verified_occurrences
                    >= CHAIN_ESCALATION_THRESHOLD,
                )
                .order_by(ModuleFindingChain.last_seen_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    by_project: dict[Any, list[ModuleFindingChain]] = {}
    for chain in candidates:
        # Re-assert the predicate in Python. The row is the authority, and this
        # keeps the decision identical whether the filtering happened in SQL or
        # the row changed between the read and this pass.
        if _chain_awaits_remediation_owner(chain):
            by_project.setdefault(chain.project_id, []).append(chain)

    elected: list[Task] = []
    for project_id, chains in by_project.items():
        decision = await acquire_automatic_task_creation_authority(session, project_id)
        if decision != AUTOMATIC_TASK_CREATION_ALLOWED:
            logger.info(
                "guard-suppression owner election held for project %s: %s",
                project_id,
                decision,
            )
            continue
        for chain in chains:
            owner = _guard_remediation_task(chain)
            chain.remediation_task_id = owner.id
            session.add(owner)
            session.add(chain)
            await _post_guard_suppression_escalation(session, chain, owner)
            elected.append(owner)
    return elected


async def resolve_landed_guard_suppression_remediation(
    task: Task, session: AsyncSession
) -> list[ModuleFindingChain]:
    """Resolve chains only when their canonical owner lands with a clean receipt."""
    if not _receipt_is_complete_and_clean((task.context or {}).get("publication_gate_receipt")):
        return []
    keys = (task.context or {}).get("guard_suppression_remediation_keys")
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        return []
    rows = list(
        (
            await session.execute(
                select(ModuleFindingChain).where(
                    ModuleFindingChain.project_id == task.project_id,
                    ModuleFindingChain.finding_key.in_(keys),
                )
            )
        ).scalars().all()
    )
    resolved: list[ModuleFindingChain] = []
    for chain in rows:
        if (
            chain.chain_kind == GUARD_SUPPRESSION_CHAIN_KIND
            and chain.finding_key in keys
            and chain.status == "open"
            and chain.remediation_task_id == task.id
        ):
            chain.status = "resolved"
            chain.resolved_by_task_id = task.id
            chain.resolved_at = _utcnow()
            session.add(chain)
            resolved.append(chain)
    if resolved:
        await _resolve_guard_suppression_escalations(session, resolved)
        await session.flush(resolved)
    return resolved


async def _resolve_guard_suppression_escalations(
    session: AsyncSession, chains: Iterable[ModuleFindingChain]
) -> None:
    """Close the one aggregate action message when its owner proves clean."""
    if not hasattr(session, "scalar"):
        # Ledger-only test sessions intentionally have no message persistence.
        return
    now = _utcnow()
    for chain in chains:
        key = f"guard-suppression:{chain.project_id}:{chain.finding_key}:escalation"
        messages = list(
            (
                await session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == chain.project_id,
                        ProjectMessage.idempotency_key == key,
                        ProjectMessage.status == "open",
                    )
                )
            ).scalars().all()
        )
        for message in messages:
            message.status = "resolved"
            message.resolution = "guard_suppression_remediated"
            message.resolved_at = now
            session.add(message)


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
