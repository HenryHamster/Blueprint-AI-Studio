"""Semantic outcome-receipt construction for discovery decisions.

The decision controller consumes one bounded parent-level receipt rather than a
bag of child task rows.  Parallel fan-out therefore changes receipt counters,
not whether the controller is allowed to decide.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlmodel import select

from app.findings_index import finalized_research_findings
from app.landed_signal import has_landed_signal
from app.models import (
    Artifact,
    ArtifactType,
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    KnowledgeDocument,
    Task,
    TaskStatus,
)
from app.task_capabilities import WRITE_TASK_TYPES
from app.time_utils import _utcnow


DISCOVERY_REVIEW_PACKET_VERSION = 2
DISCOVERY_REVIEW_MAX_PACKET_ITEMS = 24
DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS = 32_000
DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS = 8_000
DISCOVERY_REVIEW_URI_CHARS = 1_000
DISCOVERY_REVIEW_BLOCKED_GRACE = timedelta(hours=2)
DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS = 3
DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS = 2_000
DISCOVERY_REVIEW_RESEARCH_DETAILS = 6
DISCOVERY_REVIEW_KNOWLEDGE_DETAILS = 6
DISCOVERY_REVIEW_LANDED_OUTCOMES = 4
DISCOVERY_REVIEW_EXPERIMENT_DETAILS = 6
DISCOVERY_REVIEW_VERIFICATION_DETAILS = 6

_VERIFICATION_TASK_TYPES = frozenset(
    {
        "review",
        "qa",
        "smoketest",
        "verify_tests",
        "perf",
        "security_audit",
        "compute_experiment",
        "walkthrough_e2e",
    }
)
_EXPERIMENT_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.COMPUTE_RESULT.value,
        ArtifactType.PERF_REPORT.value,
        ArtifactType.SMOKETEST_RESULT.value,
        ArtifactType.WALKTHROUGH_E2E_RESULT.value,
        ArtifactType.WALKTHROUGH_E2E_REPORT.value,
    }
)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return str(_enum_value(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_decision_snapshot(value: Any) -> tuple[dict[str, Any], bool]:
    rendered = _canonical_json(value or {})
    if len(rendered) <= DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS:
        decoded = json.loads(rendered)
        return (decoded if isinstance(decoded, dict) else {}), False
    return (
        {
            "snapshot_truncated": True,
            "excerpt": rendered[:DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS],
            "original_checksum": hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest(),
        },
        True,
    )


def _bounded_metadata(value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    rendered = _canonical_json(value)
    if len(rendered) <= DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS:
        return json.loads(rendered), False
    return (
        {
            "excerpt": rendered[:DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS],
            "original_checksum": _checksum(value),
            "truncated": True,
        },
        True,
    )


def _receipt_fits_evidence_metadata(snapshot: Mapping[str, Any]) -> bool:
    return (
        len(
            _canonical_json(
                {
                    "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
                    "snapshot": snapshot,
                }
            )
        )
        <= DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS
    )


def _bounded_json_value(value: Any, *, max_chars: int) -> Any:
    rendered = _canonical_json(value)
    if len(rendered) <= max_chars:
        return json.loads(rendered)
    return {
        "semantic_excerpt": " ".join(rendered.split())[:max_chars],
        "original_checksum": _checksum(value),
        "truncated": True,
    }


def _bounded_missing_reasons(values: list[str]) -> list[str]:
    unique = list(dict.fromkeys(str(value)[:500] for value in values if str(value)))
    if len(unique) <= 20:
        return unique
    omitted = unique[20:]
    return [
        *unique[:20],
        (
            f"{len(omitted)} additional missing-evidence reason(s); "
            f"checksum={_checksum(omitted)}"
        ),
    ]


def _canonical_semantic_value(value: Any) -> str:
    rendered = (
        str(value)
        if isinstance(value, (str, int, float, bool))
        else _canonical_json(value)
    )
    return " ".join(rendered.split())


def _task_type(task: Task) -> str:
    return str(task.first_payload.get("type") or "")


def _pr_number(task: Task) -> int | str | None:
    context = task.context or {}
    review_loop = context.get("review_loop")
    if isinstance(review_loop, Mapping) and review_loop.get("pr_number") is not None:
        return review_loop.get("pr_number")
    for source in (context, task.first_payload):
        for key in ("rollup_pr_number", "pr_number"):
            if source.get(key) is not None:
                return source.get(key)
    return None


def _merge_sha(task: Task) -> str | None:
    context = task.context or {}
    for source in (context, task.first_payload):
        for key in (
            "merge_commit_sha",
            "merged_sha",
            "deployed_commit_sha",
            "serving_commit_sha",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                return value[:64]
    return None


def _artifact_type(artifact: Artifact) -> str:
    return str(_enum_value(artifact.artifact_type))


def _artifact_semantic_projection(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "task_id": str(artifact.task_id),
        "type": _artifact_type(artifact),
        "logical_key": artifact.logical_key,
        "commit_hash": artifact.commit_hash,
        "repo_url": artifact.repo_url,
        "pr_url": artifact.pr_url,
        "storage_uri": artifact.storage_uri,
        "logs": artifact.logs,
    }


def _compact_artifact(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "task_id": str(artifact.task_id),
        "type": _artifact_type(artifact),
        "logical_key": str(artifact.logical_key or "")[:300] or None,
        "commit_hash": artifact.commit_hash,
        "pr_url": str(artifact.pr_url or "")[:DISCOVERY_REVIEW_URI_CHARS] or None,
        "storage_uri": str(artifact.storage_uri or "")[:DISCOVERY_REVIEW_URI_CHARS]
        or None,
        "semantic_summary": _canonical_semantic_value(
            _artifact_semantic_projection(artifact)
        ),
    }


def _verification_summary(task: Task) -> dict[str, Any]:
    context = task.context or {}
    raw_acceptance_verdict = context.get("acceptance_verdict")
    raw_acceptance_evidence = context.get("acceptance_evidence_result")
    acceptance_verdict = _bounded_json_value(
        raw_acceptance_verdict, max_chars=500
    )
    acceptance_evidence = _bounded_json_value(
        raw_acceptance_evidence, max_chars=500
    )
    failure_category = str(context.get("failure_category") or "")[:240] or None
    latest_status = str(task.latest_status_message or "")[:800] or None
    semantic_projection = {
        key: value
        for key, value in {
            "acceptance_verdict": raw_acceptance_verdict,
            "acceptance_evidence_result": raw_acceptance_evidence,
            "failure_category": context.get("failure_category"),
            "latest_status": task.latest_status_message,
        }.items()
        if value is not None and value != ""
    }
    summary = {
        "task_id": str(task.id),
        "type": _task_type(task),
        "status": str(_enum_value(task.status)),
        "acceptance_verdict": acceptance_verdict,
        "acceptance_evidence_result": acceptance_evidence,
        "failure_category": failure_category,
        "latest_status": latest_status,
    }
    if semantic_projection:
        summary["semantic_summary"] = _canonical_semantic_value(
            semantic_projection
        )
    return summary


def _semantic_text(value: Mapping[str, Any]) -> str:
    finding = value.get("finding")
    semantic_source = (
        value
        if value.get("semantic_summary") is not None
        else finding
        if isinstance(finding, Mapping)
        else value
    )
    candidates = [
        semantic_source.get(key)
        for key in (
            "semantic_summary",
            "conclusion",
            "summary",
            "verdict",
            "result",
            "evidence",
            "latest_status",
            "description",
            "title",
            "finding",
        )
        if semantic_source.get(key) is not None
    ]
    excerpt_source = candidates[0] if candidates else semantic_source
    return _canonical_semantic_value(excerpt_source)


def _semantic_excerpt(value: Mapping[str, Any], *, max_chars: int) -> str:
    return _semantic_text(value)[:max_chars]


def _bounded_collection_coverage(
    values: list[dict[str, Any]],
    included: list[dict[str, Any]],
    *,
    omitted: list[dict[str, Any]],
    selection: str,
) -> dict[str, Any]:
    """Keep semantic coverage for omitted bodies without treating it as absence."""

    if len(omitted) <= DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS:
        sampled_indices = list(range(len(omitted)))
    elif DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS == 1:
        sampled_indices = [len(omitted) - 1]
    else:
        last = len(omitted) - 1
        sampled_indices = [
            round(index * last / (DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS - 1))
            for index in range(DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS)
        ]
    sampled_omitted = [omitted[index] for index in sampled_indices]
    omitted_semantic_index = []
    for ordinal, value in zip(sampled_indices, sampled_omitted):
        provenance = {
            key: str(value[key])[:240]
            for key in (
                "source_task_id",
                "document_id",
                "task_id",
                "artifact_id",
                "source_kind",
                "task_type",
                "type",
                "status",
            )
            if value.get(key) is not None
        }
        omitted_semantic_index.append(
            {
                "ordinal": ordinal,
                "item_checksum": _checksum(value),
                "semantic_excerpt": _semantic_excerpt(value, max_chars=240),
                **provenance,
            }
        )
    full_semantic_digest = "\n".join(_semantic_text(value) for value in omitted)
    semantic_digest_complete = (
        len(full_semantic_digest) <= DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS
    )
    semantic_digest = full_semantic_digest[:DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS]
    return {
        "total_count": len(values),
        "included_count": len(included),
        "omitted_count": max(0, len(values) - len(included)),
        "selection": selection,
        "full_set_checksum": _checksum(values),
        "omitted_semantic_index": omitted_semantic_index,
        "semantic_index_total_count": len(omitted),
        "semantic_index_included_count": len(sampled_omitted),
        "semantic_index_selection": "evenly_spaced_stable_order",
        "full_semantic_digest": semantic_digest,
        "semantic_digest_item_count": len(omitted),
        "semantic_digest_source_chars": len(full_semantic_digest),
        "semantic_digest_complete": semantic_digest_complete,
        "semantic_digest_order": "stable_collection_order",
    }


async def _get_or_add_evidence(
    session,
    *,
    cycle: DiscoveryCycle,
    dedupe_key: str,
    semantic_role: str,
    source_kind: str,
    source_id: str,
    content_type: str,
    head_sha: str | None,
    snapshot: Mapping[str, Any],
) -> DiscoveryEvidenceItem:
    existing = (
        await session.execute(
            select(DiscoveryEvidenceItem).where(
                DiscoveryEvidenceItem.cycle_id == cycle.id,
                DiscoveryEvidenceItem.dedupe_key == dedupe_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    item = DiscoveryEvidenceItem(
        cycle_id=cycle.id,
        semantic_role=semantic_role,
        source_kind=source_kind,
        source_id=source_id,
        checksum=_checksum(snapshot),
        content_type=content_type,
        head_sha=head_sha,
        mechanism_fingerprint=cycle.mechanism_fingerprint,
        evidence_metadata={
            "packet_version": DISCOVERY_REVIEW_PACKET_VERSION,
            "snapshot": json.loads(_canonical_json(snapshot)),
        },
        dedupe_key=dedupe_key,
    )
    session.add(item)
    await session.flush()
    return item


def _evidence_projection(item: DiscoveryEvidenceItem) -> dict[str, Any]:
    metadata, truncated = _bounded_metadata(item.evidence_metadata or {})
    result = {
        "evidence_id": str(item.id),
        "semantic_role": item.semantic_role,
        "source_kind": item.source_kind,
        "checksum": item.checksum,
        "source_id": item.source_id,
        "source_uri": str(item.source_uri or "")[:DISCOVERY_REVIEW_URI_CHARS]
        or None,
        "content_type": item.content_type,
        "head_sha": item.head_sha,
        "mechanism_fingerprint": item.mechanism_fingerprint,
        "evidence_metadata": metadata,
    }
    if truncated:
        result["evidence_metadata_truncated"] = True
    return result


async def build_discovery_review_packet(
    session,
    cycle: DiscoveryCycle,
    *,
    now: datetime | None = None,
) -> tuple[
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Build one bounded semantic receipt for the settled research parent."""

    now = now or _utcnow()
    bounded_intent, intent_truncated = _bounded_decision_snapshot(
        cycle.intent_snapshot
    )
    bounded_bet, bet_truncated = _bounded_decision_snapshot(cycle.bet_snapshot)
    tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == cycle.project_id,
                    Task.discovery_cycle_id == cycle.id,
                    Task.id != cycle.review_task_id
                    if cycle.review_task_id is not None
                    else True,
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
            )
        )
        .scalars()
        .all()
    )
    source = next(
        (task for task in tasks if task.id == cycle.source_research_task_id), None
    )
    task_ids = [task.id for task in tasks]
    artifacts = (
        list(
            (
                await session.execute(
                    select(Artifact)
                    .where(Artifact.task_id.in_(task_ids))
                    .order_by(Artifact.created_at.asc(), Artifact.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if task_ids
        else []
    )
    artifacts_by_task: dict[uuid.UUID, list[Artifact]] = {}
    for artifact in artifacts:
        artifacts_by_task.setdefault(artifact.task_id, []).append(artifact)
    knowledge_documents = (
        list(
            (
                await session.execute(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.source_task_id.in_(task_ids))
                    .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if task_ids
        else []
    )

    landed: list[dict[str, Any]] = []
    stranded_deliveries: list[dict[str, Any]] = []
    missing: list[str] = []
    if intent_truncated:
        missing.append("intent authority exceeds the bounded decision contract")
    if bet_truncated:
        missing.append("bet authority exceeds the bounded decision contract")
    if source is None:
        missing.append("source research parent is missing from the discovery cohort")
    # A research parent that settled FAILED/CANCELLED never rolls its staging
    # branch up, so a child PR merged into that branch can never gain a landed
    # receipt. That absence is a settled fact for the review to judge (salvage,
    # re-bet, or stop) — not evidence still in flight — so holding the cycle in
    # EVIDENCE_PENDING on it would starve the direction forever.
    source_settled_unlanded = source is not None and source.status in {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    for task in tasks:
        task_type = _task_type(task)
        is_source_parent = task.id == cycle.source_research_task_id
        if (
            task_type not in WRITE_TASK_TYPES
            and not is_source_parent
        ) or task.status != TaskStatus.COMPLETED:
            continue
        task_artifacts = artifacts_by_task.get(task.id, [])
        commit_hash = _merge_sha(task)
        pr_number = _pr_number(task)
        landed_signal = has_landed_signal(task)
        if pr_number is not None and not landed_signal:
            if source_settled_unlanded:
                stranded_deliveries.append(
                    {
                        "task_id": str(task.id),
                        "task_type": task_type,
                        "pr_number": pr_number,
                        "reason": (
                            "research parent settled "
                            f"{_enum_value(source.status)} before rollup"
                        ),
                    }
                )
            else:
                missing.append(
                    f"delivery task {task.id} has PR {pr_number} without landed receipt"
                )
        if landed_signal and not commit_hash:
            missing.append(f"landed write task {task.id} lacks exact commit identity")
        if landed_signal:
            landed.append(
                {
                    "task_id": str(task.id),
                    "task_type": task_type,
                    "pr_number": pr_number,
                    "merge_commit_sha": commit_hash,
                    "artifacts": [
                        _compact_artifact(artifact) for artifact in task_artifacts
                    ],
                }
            )

    experiments = [
        _compact_artifact(artifact)
        for artifact in artifacts
        if _artifact_type(artifact) in _EXPERIMENT_ARTIFACT_TYPES
    ]
    missing.extend(
        f"compute experiment {task.id} lacks COMPUTE_RESULT receipt"
        for task in tasks
        if (
            _task_type(task) == "compute_experiment"
            and task.status == TaskStatus.COMPLETED
            and not any(
                artifact.task_id == task.id
                for artifact in artifacts
                if _artifact_type(artifact) == ArtifactType.COMPUTE_RESULT.value
            )
        )
    )

    timestamps = [task.created_at for task in tasks if task.created_at is not None]
    endpoints = [
        task.completed_at or task.updated_at
        for task in tasks
        if (task.completed_at or task.updated_at) is not None
    ]
    elapsed_seconds = None
    if timestamps and endpoints:
        elapsed_seconds = max(0.0, (max(endpoints) - min(timestamps)).total_seconds())
    total_cost = sum(float(task.estimated_cost_usd or 0.0) for task in tasks)
    research_findings = [
        {
            "source_task_id": str(task.id),
            "task_type": _task_type(task),
            "finding": _bounded_json_value(finding, max_chars=700),
            "semantic_summary": _canonical_semantic_value(finding),
            "finding_checksum": _checksum(finding),
        }
        for task in tasks
        for finding in finalized_research_findings(task)
    ]
    knowledge_outcomes = [
        {
            "document_id": str(document.id),
            "source_task_id": str(document.source_task_id)
            if document.source_task_id
            else None,
            "source_kind": str(document.source_kind),
            "title": str(document.title or "")[:240],
            "summary": str(document.summary or "")[:500],
            "topic": str(document.topic or "")[:240] or None,
            "citations": _bounded_json_value(
                list(document.source_citations or [])[:12], max_chars=500
            ),
            "semantic_summary": _canonical_semantic_value(
                {
                    "title": document.title,
                    "summary": document.summary,
                    "topic": document.topic,
                    "source_citations": document.source_citations,
                }
            ),
        }
        for document in knowledge_documents
    ]
    verification_summaries = [
        _verification_summary(task)
        for task in tasks
        if _task_type(task) in _VERIFICATION_TASK_TYPES
    ]
    included_research_findings = research_findings[
        :DISCOVERY_REVIEW_RESEARCH_DETAILS
    ]
    included_knowledge_outcomes = knowledge_outcomes[
        -DISCOVERY_REVIEW_KNOWLEDGE_DETAILS:
    ]
    included_landed = landed[-DISCOVERY_REVIEW_LANDED_OUTCOMES:]
    included_experiments = experiments[-DISCOVERY_REVIEW_EXPERIMENT_DETAILS:]
    included_verification_summaries = verification_summaries[
        -DISCOVERY_REVIEW_VERIFICATION_DETAILS:
    ]
    source_parent = {
        "task_id": str(source.id) if source else None,
        "title": str(source.title or "")[:500] if source else None,
        "status": str(_enum_value(source.status)) if source else None,
        "latest_status": str(source.latest_status_message or "")[:1_000]
        if source
        else None,
    }
    execution = {
        "task_count": len(tasks),
        "retry_count": sum(int(task.attempt_count or 0) for task in tasks),
        "failed_count": sum(task.status == TaskStatus.FAILED for task in tasks),
        "cancelled_count": sum(task.status == TaskStatus.CANCELLED for task in tasks),
        "elapsed_seconds": round(elapsed_seconds, 3)
        if elapsed_seconds is not None
        else None,
        "estimated_cost_usd": round(total_cost, 6),
    }
    evidence_coverage = {
        "research_findings": _bounded_collection_coverage(
            research_findings,
            included_research_findings,
            omitted=research_findings[DISCOVERY_REVIEW_RESEARCH_DETAILS:],
            selection="first_6_in_stable_task_order",
        ),
        "knowledge_outcomes": _bounded_collection_coverage(
            knowledge_outcomes,
            included_knowledge_outcomes,
            omitted=knowledge_outcomes[:-DISCOVERY_REVIEW_KNOWLEDGE_DETAILS]
            if len(knowledge_outcomes) > DISCOVERY_REVIEW_KNOWLEDGE_DETAILS
            else [],
            selection="latest_6_in_stable_document_order",
        ),
        "landed_outcomes": _bounded_collection_coverage(
            landed,
            included_landed,
            omitted=landed[:-DISCOVERY_REVIEW_LANDED_OUTCOMES]
            if len(landed) > DISCOVERY_REVIEW_LANDED_OUTCOMES
            else [],
            selection="latest_4_in_stable_task_order",
        ),
        "experiment_outcomes": _bounded_collection_coverage(
            experiments,
            included_experiments,
            omitted=experiments[:-DISCOVERY_REVIEW_EXPERIMENT_DETAILS]
            if len(experiments) > DISCOVERY_REVIEW_EXPERIMENT_DETAILS
            else [],
            selection="latest_6_in_stable_artifact_order",
        ),
        "verification_summaries": _bounded_collection_coverage(
            verification_summaries,
            included_verification_summaries,
            omitted=verification_summaries[:-DISCOVERY_REVIEW_VERIFICATION_DETAILS]
            if len(verification_summaries) > DISCOVERY_REVIEW_VERIFICATION_DETAILS
            else [],
            selection="latest_6_in_stable_task_order",
        ),
    }
    incomplete_collections = [
        name
        for name, coverage in evidence_coverage.items()
        if not coverage["semantic_digest_complete"]
    ]
    binding_action_blockers = (
        [
            "complete authored semantic coverage exceeds the bounded decision "
            f"packet for: {', '.join(incomplete_collections)}"
        ]
        if incomplete_collections
        else []
    )
    missing_reasons = _bounded_missing_reasons(missing)
    receipt = {
        "source_parent": source_parent,
        "research_findings": included_research_findings,
        "knowledge_outcomes": included_knowledge_outcomes,
        "landed_outcomes": included_landed,
        "stranded_deliveries": stranded_deliveries[:DISCOVERY_REVIEW_LANDED_OUTCOMES],
        "experiment_outcomes": included_experiments,
        "verification_summaries": included_verification_summaries,
        "evidence_coverage": evidence_coverage,
        "execution": execution,
        "missing_required_evidence": missing_reasons,
        "binding_action_blockers": binding_action_blockers,
        "binding_decision_allowed": not binding_action_blockers,
    }
    if not _receipt_fits_evidence_metadata(receipt):
        # Detail-rich receipts may exceed the fixed controller budget even when
        # every outcome exists. Rebuild from zero details: coverage counts,
        # checksums, sampled provenance, and complete authored semantic digests
        # then determine whether an autonomous binding action remains safe.
        compact_collections = {
            "research_findings": research_findings,
            "knowledge_outcomes": knowledge_outcomes,
            "landed_outcomes": landed,
            "experiment_outcomes": experiments,
            "verification_summaries": verification_summaries,
        }
        evidence_coverage = {
            name: _bounded_collection_coverage(
                values,
                [],
                omitted=values,
                selection="details_removed_to_fit_total_packet_budget",
            )
            for name, values in compact_collections.items()
        }
        incomplete_collections = [
            name
            for name, coverage in evidence_coverage.items()
            if not coverage["semantic_digest_complete"]
        ]
        binding_action_blockers = (
            [
                "complete authored semantic coverage exceeds the bounded decision "
                f"packet for: {', '.join(incomplete_collections)}"
            ]
            if incomplete_collections
            else []
        )
        receipt = {
            "source_parent": source_parent,
            "research_findings": [],
            "knowledge_outcomes": [],
            "landed_outcomes": [],
            "stranded_deliveries": stranded_deliveries[
                :DISCOVERY_REVIEW_LANDED_OUTCOMES
            ],
            "experiment_outcomes": [],
            "verification_summaries": [],
            "evidence_coverage": evidence_coverage,
            "execution": execution,
            "missing_required_evidence": missing_reasons,
            "binding_action_blockers": binding_action_blockers,
            "binding_decision_allowed": not binding_action_blockers,
            "receipt_compacted_to_budget": True,
        }
    if not _receipt_fits_evidence_metadata(receipt):
        # Pathological cardinality can also overflow the compact semantic
        # indexes. Preserve exact counts/checksums, forbid binding action, and
        # still dispatch one operator-question decision instead of stranding
        # the completed cohort behind an impossible evidence wait.
        for coverage in evidence_coverage.values():
            coverage["omitted_semantic_index"] = []
            coverage["semantic_index_included_count"] = 0
            coverage["full_semantic_digest"] = ""
            coverage["semantic_digest_complete"] = False
        binding_action_blockers = [
            "complete authored semantic coverage cannot fit the bounded decision packet"
        ]
        receipt.update(
            {
                "evidence_coverage": evidence_coverage,
                "binding_action_blockers": binding_action_blockers,
                "binding_decision_allowed": False,
                "receipt_compacted_to_budget": True,
            }
        )
    landed_sha = next(
        (
            str(item.get("merge_commit_sha"))
            for item in reversed(landed)
            if item.get("merge_commit_sha")
        ),
        None,
    )
    outcome_item = await _get_or_add_evidence(
        session,
        cycle=cycle,
        dedupe_key=f"parent-outcome:v2:{_checksum(receipt)}",
        semantic_role="parent_outcome",
        source_kind="research_parent",
        source_id=str(cycle.source_research_task_id),
        content_type="application/vnd.blueprint.discovery-outcome+json",
        head_sha=landed_sha,
        snapshot=receipt,
    )
    manifest = {
        "version": DISCOVERY_REVIEW_PACKET_VERSION,
        "source_research_task_id": str(cycle.source_research_task_id),
        "task_count": len(tasks),
        "artifact_count": len(artifacts),
        "semantic_receipt_checksum": outcome_item.checksum,
        "exact_landed_sha": landed_sha,
        "stranded_delivery_count": len(stranded_deliveries),
        "missing_required_evidence": missing_reasons,
        "binding_action_blockers": binding_action_blockers,
        "binding_decision_allowed": not binding_action_blockers,
        "required_evidence_omitted": bool(missing_reasons or binding_action_blockers),
        "intent_snapshot_truncated": intent_truncated,
        "bet_snapshot_truncated": bet_truncated,
    }
    manifest_item = await _get_or_add_evidence(
        session,
        cycle=cycle,
        dedupe_key=f"packet-manifest:v2:{_checksum(manifest)}",
        semantic_role="packet_manifest",
        source_kind="server_ledger",
        source_id=str(cycle.id),
        content_type="application/vnd.blueprint.discovery-review-manifest+json",
        head_sha=landed_sha,
        snapshot=manifest,
    )
    packet = [
        _evidence_projection(item)
        for item in [manifest_item, outcome_item]
    ]
    artifact_prerequisites = [
        {
            "artifact_id": str(artifact.id),
            "artifact_type": _artifact_type(artifact),
            "source_task_id": str(artifact.task_id),
            "storage_uri": str(artifact.storage_uri)[:DISCOVERY_REVIEW_URI_CHARS],
        }
        for artifact in artifacts
        if artifact.storage_uri
    ][:12]
    return (
        packet,
        _checksum(packet),
        artifact_prerequisites,
        manifest,
        bounded_intent,
        bounded_bet,
    )
