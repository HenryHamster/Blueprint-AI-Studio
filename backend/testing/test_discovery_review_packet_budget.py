"""Packet-budget progressive degradation tests for discovery review receipts."""

import pytest

import app.discovery_review_packet as discovery_review_packet
from app.discovery_review import maybe_close_and_spawn_discovery_review
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import Artifact, ArtifactType, Task, TaskStatus
from testing.test_discovery_review import _cycle_with_source


@pytest.mark.asyncio
async def test_mixed_large_receipt_compacts_instead_of_waiting_for_evidence(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    source.plan_output = [
        {"title": f"Research {index}", "evidence": "r" * 2_000}
        for index in range(6)
    ]
    source.context = {FINALIZED_RESEARCH_FINDINGS_KEY: True}
    tasks = []
    for index in range(4):
        tasks.append(
            Task(
                project_id=project.id,
                discovery_cycle_id=cycle.id,
                parent_task_id=source.id,
                title=f"Landed write {index}",
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                context={
                    "landed_on_integration": True,
                    "merge_commit_sha": f"{index + 1:040x}",
                },
            )
        )
    for index in range(6):
        tasks.append(
            Task(
                project_id=project.id,
                discovery_cycle_id=cycle.id,
                parent_task_id=source.id,
                title=f"Compute experiment {index}",
                payloads=[{"type": "compute_experiment"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                context={
                    "acceptance_verdict": {"summary": "v" * 4_000},
                    "acceptance_evidence_result": {"summary": "e" * 4_000},
                },
            )
        )
    db_session.add_all([source, *tasks])
    await db_session.flush()
    long_uri = "gs://bucket/" + ("u" * 990)
    for task in tasks[:4]:
        db_session.add_all(
            [
                Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.RESEARCH_FINDINGS,
                    logical_key=f"{artifact_index}-" + ("k" * 230),
                    storage_uri=long_uri,
                    pr_url="https://example.test/" + ("p" * 970),
                )
                for artifact_index in range(4)
            ]
        )
    for index, task in enumerate(tasks[4:]):
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.COMPUTE_RESULT,
                logical_key=f"compute-{index}-" + ("k" * 220),
                storage_uri=long_uri,
                pr_url="https://example.test/" + ("p" * 970),
            )
        )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    assert spawned.reason == "spawned"
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    assert "evidence_metadata_truncated" not in outcome
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert snapshot["receipt_compacted_to_budget"] is True
    assert snapshot["missing_required_evidence"] == []
    assert len(discovery_review_packet._canonical_json(snapshot)) <= (
        discovery_review_packet.DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS
    )
    # This cohort is large enough that even the progressive-degradation
    # loop's last (all-zero-detail) level still doesn't have room for any
    # per-collection details, so every collection lands on the pathological
    # zero-detail fallback -- the pre-existing behavior this fix must not
    # regress even though it now tries several reduced-but-nonzero levels
    # first.
    for name in (
        "research_findings",
        "knowledge_outcomes",
        "landed_outcomes",
        "experiment_outcomes",
        "verification_summaries",
    ):
        assert snapshot[name] == []
    assert snapshot["binding_decision_allowed"] is False


def test_progressive_degradation_retains_partial_detail_when_it_fits_budget():
    # A live incident showed the previous all-or-nothing compaction: the
    # moment the full-detail receipt overflowed the packet budget, every
    # collection was zeroed out in one shot, which flipped
    # semantic_digest_complete False for collections whose full omitted
    # digest didn't fit either -- forcing ask_operator on a healthy cohort.
    # This constructs a cohort whose full-detail (level 0) receipt overflows
    # DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS purely on the bulk of
    # experiment_outcomes' non-semantic fields (mirroring a real artifact's
    # large storage_uri/logs), while every collection's own semantic_summary
    # stays small -- so a halved-count (level 1) receipt both fits the
    # budget *and* keeps every collection's authored semantic coverage
    # complete. It exercises the same pure helpers
    # build_discovery_review_packet calls at each round.
    p = discovery_review_packet

    def small_collection(prefix: str, count: int) -> list[dict]:
        return [{"id": f"{prefix}-{index}", "note": "small"} for index in range(count)]

    def bulky_experiment_outcomes(count: int) -> list[dict]:
        return [
            {
                "artifact_id": f"artifact-{index:08x}",
                "task_id": f"task-{index:08x}",
                "type": "COMPUTE_RESULT",
                "logical_key": f"compute-{index}",
                "commit_hash": None,
                "pr_url": None,
                # Large but semantically-inert bulk (an oversized storage
                # pointer), decoupled from the compact digest text below --
                # exactly the shape a real overflowing receipt takes.
                "storage_uri": "gs://bucket/" + ("u" * 5_000),
                "semantic_summary": f"experiment {index} outcome: passed",
            }
            for index in range(count)
        ]

    collections: dict[str, list[dict]] = {
        "research_findings": small_collection("research", 6),
        "knowledge_outcomes": small_collection("knowledge", 6),
        "landed_outcomes": small_collection("landed", 4),
        "experiment_outcomes": bulky_experiment_outcomes(6),
        "verification_summaries": small_collection("verification", 6),
    }
    source_parent = {
        "task_id": "source-task",
        "title": "source",
        "status": "COMPLETED",
        "latest_status": "ok",
    }
    execution = {
        "task_count": 22,
        "retry_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "elapsed_seconds": 1.0,
        "estimated_cost_usd": 0.0,
    }

    included, coverage = p._evidence_coverage_for_level(
        collections, p._COLLECTION_BASE_DETAIL_COUNTS, level=0
    )
    full_receipt, full_blockers = p._assemble_receipt(
        source_parent=source_parent,
        included=included,
        stranded_deliveries=[],
        evidence_coverage=coverage,
        execution=execution,
        missing_reasons=[],
        compacted=False,
    )
    assert full_blockers == []
    assert p._receipt_fits_evidence_metadata(full_receipt) is False

    levels = p._progressive_detail_levels(p._COLLECTION_BASE_DETAIL_COUNTS)
    level_one_counts = levels[0]
    assert level_one_counts == {
        "research_findings": 3,
        "knowledge_outcomes": 3,
        "landed_outcomes": 2,
        "experiment_outcomes": 3,
        "verification_summaries": 3,
    }
    reduced_included, reduced_coverage = p._evidence_coverage_for_level(
        collections, level_one_counts, level=1
    )
    reduced_receipt, reduced_blockers = p._assemble_receipt(
        source_parent=source_parent,
        included=reduced_included,
        stranded_deliveries=[],
        evidence_coverage=reduced_coverage,
        execution=execution,
        missing_reasons=[],
        compacted=True,
    )

    assert p._receipt_fits_evidence_metadata(reduced_receipt) is True
    # Partial detail is retained -- this is not the zero-detail fallback.
    assert len(reduced_receipt["experiment_outcomes"]) == 3
    assert all(
        len(reduced_receipt[name]) > 0 for name in collections
    )
    # The authority invariant is untouched: every collection's semantic
    # coverage is still complete at this reduced level, so there are no
    # binding blockers and the decision stays binding.
    assert all(
        cov["semantic_digest_complete"] for cov in reduced_coverage.values()
    )
    assert reduced_blockers == []
    assert reduced_receipt["binding_decision_allowed"] is True
    assert reduced_receipt["receipt_compacted_to_budget"] is True
    for cov in reduced_coverage.values():
        assert "reduced_to_fit_total_packet_budget(level=1)" in cov["selection"]
