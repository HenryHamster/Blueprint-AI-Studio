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
    # This cohort is large enough that the progressive-degradation loop's
    # lockstep halving reduces every non-empty collection to zero before the
    # receipt fits -- this is the exact production defect (gen-2 discovery
    # cycle c9db2f97: a decision packet shipped with zero landed outcomes and
    # zero research findings even though the direction had landed work,
    # blinding the review step). The floor repair must claw each non-empty
    # collection back up to a small, prose-truncated representative sample
    # instead of leaving it at zero, and must say so explicitly so the
    # reviewer can tell "truncated" from "absent".
    truncated_totals = {
        "research_findings": 6,
        "landed_outcomes": 4,
        "experiment_outcomes": 6,
        "verification_summaries": 6,
    }
    for name, total in truncated_totals.items():
        assert 1 <= len(snapshot[name]) <= discovery_review_packet.DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS, (
            name,
            snapshot[name],
        )
    # knowledge_outcomes has no underlying KnowledgeDocument rows in this
    # cohort at all -- it must stay genuinely empty, not be dressed up with
    # floor items or a truncation marker.
    assert snapshot["knowledge_outcomes"] == []
    assert set(snapshot["evidence_truncated"]) == {
        f"{name}:kept_{len(snapshot[name])}_of_{total}"
        for name, total in truncated_totals.items()
    }
    assert not any(
        entry.startswith("knowledge_outcomes:") for entry in snapshot["evidence_truncated"]
    )
    # The full omitted-evidence digest for the truncated collections still
    # doesn't fit its own bound, so the authority invariant correctly keeps
    # this a non-binding, ask-the-operator decision -- but now the operator
    # (and the review step) can actually see that landed work and findings
    # exist, instead of being handed a packet that looks empty.
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


def _packet_test_fixture(**overrides):
    source_parent = {
        "task_id": "source-task",
        "title": "source",
        "status": "COMPLETED",
        "latest_status": "ok",
    }
    execution = {
        "task_count": 1,
        "retry_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "elapsed_seconds": 1.0,
        "estimated_cost_usd": 0.0,
    }
    fixture = {"source_parent": source_parent, "execution": execution}
    fixture.update(overrides)
    return fixture


def test_zeroed_nonempty_categories_get_a_floor_and_a_degradation_marker():
    # (a) Root-fix pin for the production defect: gen-2 discovery cycle
    # c9db2f97 produced a decision packet with zero landed outcomes and zero
    # research findings even though the direction had landed work, blinding
    # the review step. Simulate the state build_discovery_review_packet's
    # progressive-halving loop lands in once a bulkier sibling collection
    # forced every collection's detail count to zero in lockstep: two
    # non-empty, oversized collections (research_findings, landed_outcomes)
    # both at zero included items. The floor repair must bring each back to
    # at least one (up to DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS) prose-truncated
    # item and tag the packet with an explicit "evidence_truncated" marker --
    # never leave a category that has real evidence rendering as empty.
    p = discovery_review_packet

    def bulky(prefix: str, count: int) -> list[dict]:
        return [
            {"id": f"{prefix}-{index}", "note": "x" * 3_000}
            for index in range(count)
        ]

    collections: dict[str, list[dict]] = {
        "research_findings": bulky("research", 17),
        "knowledge_outcomes": [],
        "landed_outcomes": bulky("landed", 5),
        "experiment_outcomes": [],
        "verification_summaries": [],
    }
    zero_counts = {name: 0 for name in collections}
    included, evidence_coverage = p._evidence_coverage_for_level(
        collections, zero_counts, level=3
    )
    # Precondition: this mirrors exactly the bug -- both non-empty
    # collections landed at zero included items.
    assert evidence_coverage["research_findings"]["included_count"] == 0
    assert evidence_coverage["landed_outcomes"]["included_count"] == 0

    fixture = _packet_test_fixture()
    receipt, blockers = p._repair_zeroed_categories(
        collections=collections,
        included=included,
        evidence_coverage=evidence_coverage,
        source_parent=fixture["source_parent"],
        stranded_deliveries=[],
        execution=fixture["execution"],
        missing_reasons=[],
    )

    assert p._receipt_fits_evidence_metadata(receipt) is True
    assert 1 <= len(receipt["research_findings"]) <= p.DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS
    assert 1 <= len(receipt["landed_outcomes"]) <= p.DISCOVERY_REVIEW_MIN_CATEGORY_ITEMS
    assert "evidence_truncated" in receipt
    truncated_names = {entry.split(":", 1)[0] for entry in receipt["evidence_truncated"]}
    assert truncated_names == {"research_findings", "landed_outcomes"}


def test_genuinely_empty_categories_stay_empty_without_a_marker():
    # (b) Absence must remain distinguishable from truncation: a category
    # with no underlying evidence at all is not a zeroed category to repair,
    # and must never be tagged as if it had been trimmed.
    p = discovery_review_packet
    collections: dict[str, list[dict]] = {
        "research_findings": [],
        "knowledge_outcomes": [],
        "landed_outcomes": [],
        "experiment_outcomes": [],
        "verification_summaries": [],
    }
    zero_counts = {name: 0 for name in collections}
    included, evidence_coverage = p._evidence_coverage_for_level(
        collections, zero_counts, level=0
    )
    for coverage in evidence_coverage.values():
        assert coverage["total_count"] == 0
        assert coverage["included_count"] == 0

    fixture = _packet_test_fixture()
    receipt, blockers = p._repair_zeroed_categories(
        collections=collections,
        included=included,
        evidence_coverage=evidence_coverage,
        source_parent=fixture["source_parent"],
        stranded_deliveries=[],
        execution=fixture["execution"],
        missing_reasons=[],
    )

    for name in collections:
        assert receipt[name] == []
    assert "evidence_truncated" not in receipt


def test_under_budget_receipt_is_unchanged_without_a_marker():
    # (c) A receipt that never needed budget-driven degradation
    # (compacted=False, the normal full-detail path) must carry neither the
    # compaction flag nor the evidence_truncated marker -- even though some
    # collections here (a genuinely empty one, and a landed_outcomes list
    # under its per-collection detail cap) are naturally short, that is
    # steady-state shaping, not evidence loss, and must not be flagged as
    # degradation.
    p = discovery_review_packet
    collections: dict[str, list[dict]] = {
        "research_findings": [{"id": f"r-{i}", "note": "small"} for i in range(2)],
        "knowledge_outcomes": [],
        "landed_outcomes": [{"id": "l-0", "note": "small"}],
        "experiment_outcomes": [],
        "verification_summaries": [],
    }
    included, evidence_coverage = p._evidence_coverage_for_level(
        collections, p._COLLECTION_BASE_DETAIL_COUNTS, level=0
    )
    fixture = _packet_test_fixture()
    receipt, blockers = p._assemble_receipt(
        source_parent=fixture["source_parent"],
        included=included,
        stranded_deliveries=[],
        evidence_coverage=evidence_coverage,
        execution=fixture["execution"],
        missing_reasons=[],
        compacted=False,
    )

    assert blockers == []
    assert p._receipt_fits_evidence_metadata(receipt) is True
    assert receipt["research_findings"] == collections["research_findings"]
    assert receipt["landed_outcomes"] == collections["landed_outcomes"]
    assert receipt["knowledge_outcomes"] == []
    assert "receipt_compacted_to_budget" not in receipt
    assert "evidence_truncated" not in receipt
