"""Durable, producer-specific acknowledgements for deferred research work.

Deferred findings live on the COMPLETED task that produced them.  A later
research task receives copies through ``prior_deferred_findings``.  Consuming a
copy needs a compact receipt and producer-local pruning; otherwise every
autonomous scan rediscovers the same old entry forever, and fully consumed rows
can saturate the scanner's bounded producer query before older live work is
reached.

Receipts are keyed by ``(source_task_id, finding_hash)``.  The producer id is
part of the identity so acknowledging an old copy never suppresses a copy that
was re-deferred onto a newer task.  A scanner always restamps the immediate
producer before handing a copy forward, preserving that property across any
number of cycles.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.finding_deduplication import canonical_finding_body, finding_hash
from app.models import Task, TaskStatus
from app.research_output_contract import (
    RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY,
    normalize_research_output_contract_version,
    research_output_contract_version,
)
from app.time_utils import _utcnow


DEFERRED_FINDING_CONSUMPTION_ACKS_KEY = "deferred_finding_consumption_acks"
# A producer's deferred_findings list has the same 200-entry count backstop.
# Keeping one compact acknowledgement per possible producer entry is therefore
# sufficient without allowing task.context to grow without bound.
DEFERRED_FINDING_CONSUMPTION_ACKS_MAX = 200


def canonical_deferred_finding_hash(finding: Mapping[str, Any]) -> str:
    """Return the server-derived routing identity for one deferred finding.

    ``finding_hash`` crosses an agent-controlled artifact boundary, so it is
    deliberately ignored here.  Trusting a stored value would let two distinct
    findings alias and be pruned by a single consumption acknowledgement.
    """

    return finding_hash(
        str(finding.get("title") or ""),
        canonical_finding_body(dict(finding)),
    )


def deferred_carry_pair(
    finding: Mapping[str, Any],
    *,
    source_task_id: uuid.UUID | str | None = None,
) -> tuple[str, str] | None:
    """Return ``(immediate producer, canonical finding hash)`` when usable."""

    source = source_task_id or finding.get("source_task_id")
    source_text = str(source or "").strip()
    if not source_text:
        return None
    finding_key = canonical_deferred_finding_hash(finding)
    if not finding_key:
        return None
    return source_text, finding_key


def acknowledged_deferred_finding_hashes(source_task: Task) -> set[str]:
    """Read only acknowledgements that belong to ``source_task`` itself."""

    source_id = str(source_task.id)
    raw_records = (source_task.context or {}).get(
        DEFERRED_FINDING_CONSUMPTION_ACKS_KEY, []
    )
    if not isinstance(raw_records, list):
        return set()
    return {
        str(record.get("finding_hash") or "").strip()
        for record in raw_records
        if isinstance(record, Mapping)
        and str(record.get("source_task_id") or "").strip() == source_id
        and str(record.get("finding_hash") or "").strip()
    }


def unacknowledged_deferred_findings(source_task: Task) -> list[dict[str, Any]]:
    """Return recoverable copies, restamped to their immediate producer.

    The original lineage is retained separately for audit, but
    ``source_task_id`` always names the row whose ``deferred_findings`` entry is
    being consumed now.  That makes a re-deferred copy on a newer task a new,
    recoverable pair even when the same canonical finding hash is unchanged.
    """

    raw_findings = (source_task.context or {}).get("deferred_findings", [])
    if not isinstance(raw_findings, list):
        return []
    source_id = str(source_task.id)
    source_direction_id = str(
        (source_task.context or {}).get("direction_id") or ""
    ).strip()
    source_contract_version = research_output_contract_version(source_task.context)
    acknowledged = acknowledged_deferred_finding_hashes(source_task)
    result: list[dict[str, Any]] = []
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        finding_key = canonical_deferred_finding_hash(finding)
        if finding_key in acknowledged:
            continue
        previous_source = str(finding.get("source_task_id") or "").strip()
        if previous_source and previous_source != source_id:
            finding.setdefault("original_source_task_id", previous_source)
        finding["source_task_id"] = source_id
        # ``finding_hash`` can carry legitimate routing lineage across a retitle
        # and is therefore not an acknowledgement credential. Keep it intact;
        # every receipt/prune decision below independently recomputes
        # ``finding_key`` from the current title/body.
        # Preserve an origin version already stamped when work is re-deferred;
        # otherwise stamp it from the immediate producer's server-owned claim
        # context. Invalid metadata falls back to that trusted producer value.
        finding[RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY] = (
            normalize_research_output_contract_version(
                finding.get(RESEARCH_FINDING_ORIGIN_CONTRACT_VERSION_KEY),
                fallback=source_contract_version,
            )
        )
        # Completion treats a missing finding direction as the owning research
        # task's direction. Apply the same rule at the durable producer boundary
        # so autonomous's direction-scoped scan does not silently discard a
        # legacy parent-scoped entry. An explicit different direction remains
        # untouched and is filtered by the caller as before.
        if not str(finding.get("direction_id") or "").strip() and source_direction_id:
            finding["direction_id"] = source_direction_id
        result.append(finding)
    return result


async def acknowledge_consumed_deferred_findings(
    session,
    *,
    consumer_task: Task,
    carried_findings: Iterable[Mapping[str, Any]],
    excluded_pairs: set[tuple[str, str]] | None = None,
) -> int:
    """Stamp producer receipts for a terminally finalized consumer.

    This helper mutates the caller's transaction and never commits.  Callers
    must invoke it only on the final success path, in the same transaction as
    their authoritative finalized marker.  ``excluded_pairs`` is used when a
    re-deferred copy could not be durably persisted (for example a pathological
    context-count eviction); its old producer remains recoverable in that case.
    """

    excluded_pairs = excluded_pairs or set()
    requested: dict[uuid.UUID, set[str]] = {}
    for finding in carried_findings:
        if not isinstance(finding, Mapping):
            continue
        pair = deferred_carry_pair(finding)
        if pair is None or pair in excluded_pairs:
            continue
        source_text, finding_key = pair
        if source_text == str(consumer_task.id):
            continue
        try:
            source_id = uuid.UUID(source_text)
        except (TypeError, ValueError, AttributeError):
            continue
        requested.setdefault(source_id, set()).add(finding_key)

    if not requested:
        return 0

    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.id.in_(set(requested)),
                    Task.project_id == consumer_task.project_id,
                    Task.status == TaskStatus.COMPLETED,
                )
                .order_by(Task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    consumed_at = _utcnow().isoformat()
    stamped = 0
    for producer in rows:
        source_id = str(producer.id)
        # Do not let a forged source_task_id acknowledge arbitrary work.  The
        # same hash must still exist on the named producer row.
        raw_deferred = (producer.context or {}).get("deferred_findings", [])
        if not isinstance(raw_deferred, list):
            raw_deferred = []
        producer_hashes = {
            canonical_deferred_finding_hash(finding)
            for finding in raw_deferred
            if isinstance(finding, Mapping)
        }
        hashes = requested.get(producer.id, set()) & producer_hashes
        if not hashes:
            continue

        context = dict(producer.context or {})
        raw_existing = context.get(DEFERRED_FINDING_CONSUMPTION_ACKS_KEY, [])
        existing = raw_existing if isinstance(raw_existing, list) else []
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in existing:
            if not isinstance(record, Mapping):
                continue
            pair = (
                str(record.get("source_task_id") or "").strip(),
                str(record.get("finding_hash") or "").strip(),
            )
            if pair[0] != source_id or not pair[1] or pair in seen:
                continue
            seen.add(pair)
            normalized.append(dict(record))

        for finding_key in sorted(hashes):
            pair = (source_id, finding_key)
            if pair in seen:
                continue
            seen.add(pair)
            normalized.append(
                {
                    "source_task_id": source_id,
                    "finding_hash": finding_key,
                    "consumed_by_task_id": str(consumer_task.id),
                    "consumed_at": consumed_at,
                }
            )
            stamped += 1

        context[DEFERRED_FINDING_CONSUMPTION_ACKS_KEY] = normalized[
            -DEFERRED_FINDING_CONSUMPTION_ACKS_MAX:
        ]
        # Prune only the acknowledged hashes from THIS producer. Keeping the
        # compact receipts preserves auditability, while removing consumed
        # entries makes the row stop matching autonomous's ``deferred_findings
        # length > 0`` query. A re-deferred copy lives on the newer consumer row
        # and therefore remains independently recoverable.
        context["deferred_findings"] = [
            finding
            for finding in raw_deferred
            if not (
                isinstance(finding, Mapping)
                and canonical_deferred_finding_hash(finding) in hashes
            )
        ]
        producer.context = context
        flag_modified(producer, "context")
        session.add(producer)

    return stamped
