"""Packet-budget progressive degradation tests for discovery review receipts."""

import pytest

import app.discovery_review_packet as discovery_review_packet
from app.discovery_review import (
    DiscoveryReviewResultError,
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
)
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import Artifact, ArtifactType, Task, TaskStatus
from testing.test_discovery_review import _cycle_with_source, _result


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
    # This assertion read ``is False`` until 2026-08-23, when the rule it
    # encoded turned out to be a defect rather than an invariant. The omitted
    # evidence for these truncated collections does not fit the digest bound
    # *verbatim* -- but every omitted item is still reproduced in the digest,
    # far above the per-item floor, because the budget is now shared out per
    # item instead of spent front-to-back. Revoking binding authority on
    # prose length alone is exactly what stranded five discovery directions
    # at ask_operator that day, asking the operator to hand-supply the
    # evidence this very packet was carrying. Prose fidelity is still
    # measured and reported; it just no longer decides.
    coverage = snapshot["evidence_coverage"]
    for name in truncated_totals:
        assert coverage[name]["semantic_digest_item_coverage_complete"] is True, name
    for name in ("research_findings", "experiment_outcomes"):
        assert coverage[name]["semantic_digest_complete"] is False, name
    assert snapshot["binding_decision_allowed"] is True


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


def _oversized_direction(direction_key: str) -> dict:
    """A direction whose authored authority alone overflows the snapshot cap.

    Sized to match the production directions this regression stranded
    (10,359-14,489 rendered chars): the bulk is ordinary authored prose --
    direction text plus the success/retirement/constraint context an operator
    is expected to write -- not a synthetic blob.
    """

    return {
        "id": direction_key,
        "text": "Prove the bounded mechanism. " + ("Constraint detail. " * 400),
        "status": "active",
        "priority": "medium",
        "paths": ["backend/app/discovery_review.py"],
        "context": {
            "success_evidence": "The probe improves the target outcome. " * 60,
            "retirement_condition": "Retire after two probes without gain. " * 60,
            "implementation_constraints": "Reuse the current runtime. " * 60,
        },
    }


@pytest.mark.asyncio
async def test_oversized_bet_authority_still_spawns_an_operator_question_review(
    db_session,
):
    """A bet larger than the snapshot cap must bound, not strand, the cycle.

    Production regression: four Blueprint directions (d-7c851b3f from
    2026-08-19, d-4efbf4ef, d-f502e1a5, d-dec033c2) held their discovery
    cycles at revision 0 -- no review task ever spawned -- because the packet
    builder recorded snapshot truncation as *missing required evidence*. The
    "missing" evidence was the direction's own length, so no task, retry, or
    agent could ever supply it, and each cycle re-checked and re-held once a
    minute indefinitely while the fleet sat idle.

    The bounded excerpt is a usable decision input: it is deterministic,
    carries the full authority's checksum, and the manifest flags the
    truncation outright. So the packet must do what it already does for
    coverage that cannot fit -- forbid a binding action and dispatch one
    operator-question decision -- instead of waiting forever.
    """

    project, cycle, source = await _cycle_with_source(db_session)
    cycle.bet_snapshot = {
        "hypothesis": "the mechanism helps",
        "direction": _oversized_direction(cycle.direction_key),
    }
    db_session.add(cycle)
    await db_session.flush()

    # The bound itself is part of the decision contract: pin it literally so a
    # silent cap change cannot quietly stop exercising this path.
    assert discovery_review_packet.DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS == 8_000
    assert len(
        discovery_review_packet._canonical_json(cycle.bet_snapshot)
    ) > discovery_review_packet.DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True, spawned.reason
    assert spawned.reason == "spawned"

    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    # Truncation is reported, and reported as a *binding* blocker...
    assert manifest["bet_snapshot_truncated"] is True
    assert any(
        "bet authority exceeds the bounded decision contract" in blocker
        for blocker in manifest["binding_action_blockers"]
    ), manifest["binding_action_blockers"]
    # ...never as evidence still in flight, which is what stranded the cycle.
    assert manifest["missing_required_evidence"] == []

    # The safety invariant is unchanged: binding is still forbidden, and the
    # receipt's own copy of it agrees with the manifest's.
    assert manifest["binding_decision_allowed"] is False
    assert manifest["required_evidence_omitted"] is True
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert snapshot["binding_decision_allowed"] is False
    assert snapshot["missing_required_evidence"] == []

    # The reviewer sees a bounded excerpt of the real authority, not a stub.
    bet = review.first_payload["bet_snapshot"]
    assert bet["snapshot_truncated"] is True
    assert len(bet["excerpt"]) == (
        discovery_review_packet.DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS
    )
    assert bet["original_checksum"]

    with pytest.raises(DiscoveryReviewResultError, match="incomplete packet semantics"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review, action="advance"),
        )


@pytest.mark.asyncio
async def test_oversized_intent_authority_blocks_binding_without_stranding(db_session):
    """Same contract for the intent snapshot, the other truncated authority."""

    project, cycle, source = await _cycle_with_source(db_session)
    cycle.intent_snapshot = {
        "goal": "prove useful behavior",
        "direction": _oversized_direction(cycle.direction_key),
    }
    db_session.add(cycle)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True, spawned.reason

    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["intent_snapshot_truncated"] is True
    assert manifest["bet_snapshot_truncated"] is False
    assert any(
        "intent authority exceeds the bounded decision contract" in blocker
        for blocker in manifest["binding_action_blockers"]
    ), manifest["binding_action_blockers"]
    assert manifest["missing_required_evidence"] == []
    assert manifest["binding_decision_allowed"] is False


# ---------------------------------------------------------------------------
# Omitted-evidence digest: per-item coverage, not front-to-back prose.
#
# 2026-08-23 production incident -- the whole Blueprint research fleet went
# idle. Five discovery directions stranded at ask_operator, each asking the
# human operator to hand-supply research evidence the engine was already
# holding in its own database. The stranded packets carried
# evidence_truncated: ['research_findings:kept_1_of_8',
# 'landed_outcomes:kept_1_of_8'].
#
# The defect was in the omitted-evidence digest -- the one field that is
# supposed to account for *every* item budget pressure pushed out of
# `included` (the omitted_semantic_index beside it samples only
# DISCOVERY_REVIEW_SEMANTIC_INDEX_ITEMS of them). It was built by joining
# every omitted item's untruncated text and hard-cutting the join at
# DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS, which spends the budget
# front-to-back. Two consequences, both seen live:
#
#   1. one verbose entry pushed the tail entries out of the rendered digest
#      entirely -- exactly the silent absence the field exists to prevent;
#      and
#   2. binding_decision_allowed was gated on semantic_digest_complete, i.e.
#      on full *prose* fidelity, so a single long research finding revoked
#      binding authority over a whole cohort of evidence the engine held.
#
# The fix shares the budget out per item (max-min fair) and moves the
# authority gate onto semantic_digest_item_coverage_complete: authority is
# revoked only when an omitted item is genuinely unaccounted for, never
# merely because an entry's prose had to be shortened. These tests pin both
# halves -- that the fix restores authority on the incident's cohort, and
# that the floor which earns that authority cannot be quietly dissolved.
# ---------------------------------------------------------------------------

_DIGEST_COLLECTION_NAMES = (
    "research_findings",
    "knowledge_outcomes",
    "landed_outcomes",
    "experiment_outcomes",
    "verification_summaries",
)


def _short_research_finding(index: int) -> dict:
    return {
        "id": f"research-{index}",
        "semantic_summary": f"finding {index}: the probe moved the target metric",
    }


def _short_landed_outcome(index: int) -> dict:
    return {
        "task_id": f"landed-{index}",
        "semantic_summary": f"landed {index}: merged into integration cleanly",
    }


def _digest_collections(**overrides) -> dict:
    collections = {name: [] for name in _DIGEST_COLLECTION_NAMES}
    collections.update(overrides)
    return collections


def _digest_detail_counts(**overrides) -> dict:
    counts = dict.fromkeys(_DIGEST_COLLECTION_NAMES, 0)
    counts.update(overrides)
    return counts


def _one_verbose_item_cohort() -> tuple[dict, dict]:
    """The 2026-08-23 shape: 8 authored items per collection, 1 kept, 7 omitted,
    exactly one of the omitted being long-form prose."""

    research = (
        [_short_research_finding(0)]
        + [
            {
                "id": "research-1",
                "semantic_summary": (
                    "finding 1: "
                    + ("the probe moved the target metric and here is the writeup. " * 40)
                ),
            }
        ]
        + [_short_research_finding(index) for index in range(2, 8)]
    )
    landed = (
        [_short_landed_outcome(0)]
        + [
            {
                "task_id": "landed-1",
                "semantic_summary": (
                    "landed 1: " + ("merge notes and rollout detail. " * 60)
                ),
            }
        ]
        + [_short_landed_outcome(index) for index in range(2, 8)]
    )
    collections = _digest_collections(
        research_findings=research, landed_outcomes=landed
    )
    counts = _digest_detail_counts(research_findings=1, landed_outcomes=1)
    return collections, counts


def test_one_verbose_omitted_item_neither_starves_the_cohort_nor_revokes_authority():
    # The regression itself. One long-form finding among seven omitted ones
    # used to consume the entire digest and, worse, revoke the packet's
    # binding authority -- the engine then asked Henry to paste back evidence
    # it already had. Reproduce the incident's exact cohort shape
    # (kept_1_of_8 on both research_findings and landed_outcomes) against the
    # same pure helpers build_discovery_review_packet calls at each
    # degradation round, and pin the two properties that closed it: every
    # omitted item is still represented, and prose shortening alone no longer
    # touches authority.
    p = discovery_review_packet
    collections, counts = _one_verbose_item_cohort()
    included, coverage = p._evidence_coverage_for_level(
        collections, counts, level=3
    )
    fixture = _packet_test_fixture()
    receipt, blockers = p._assemble_receipt(
        source_parent=fixture["source_parent"],
        included=included,
        stranded_deliveries=[],
        evidence_coverage=coverage,
        execution=fixture["execution"],
        missing_reasons=[],
        compacted=True,
    )

    # Precondition -- this really is the stranding cohort: both collections
    # were trimmed to one included item out of eight, and the untruncated
    # join of what fell out overflows the digest bound. That overflow *is*
    # the old semantic_digest_complete=False condition, so under the previous
    # behaviour this receipt came back with binding_decision_allowed False.
    assert receipt["evidence_truncated"] == [
        "research_findings:kept_1_of_8",
        "landed_outcomes:kept_1_of_8",
    ]
    for name in ("research_findings", "landed_outcomes"):
        assert coverage[name]["semantic_digest_source_chars"] > (
            p.DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS
        )
        assert coverage[name]["semantic_digest_complete"] is False

    research_digest = coverage["research_findings"]["full_semantic_digest"]
    landed_digest = coverage["landed_outcomes"]["full_semantic_digest"]
    # Every short omitted item is still reproduced word for word: none of
    # them was pushed out by the verbose neighbour, which is what made an
    # omitted item read to the reviewer as an item that never existed.
    # research_findings keeps the *first* authored item, so its omitted set is
    # indexes 1..7 with the verbose finding at index 1; landed_outcomes keeps
    # the *latest*, so its omitted set is indexes 0..6, verbose one again at
    # index 1. Both selections are part of the contract being exercised.
    for index in range(2, 8):
        assert _short_research_finding(index)["semantic_summary"] in research_digest
    for index in (0, 2, 3, 4, 5, 6):
        assert _short_landed_outcome(index)["semantic_summary"] in landed_digest
    assert len(research_digest.split("\n")) == 7
    assert len(landed_digest.split("\n")) == 7
    # ...and the one entry that did have to be shortened says so, so a
    # shortened entry is never mistaken for a short-but-complete one.
    assert "\u2026" in research_digest
    assert "\u2026" in landed_digest
    for name in ("research_findings", "landed_outcomes"):
        assert coverage[name]["semantic_digest_elided_item_count"] == 1
        assert coverage[name]["semantic_digest_item_coverage_complete"] is True

    # The authority half of the fix: every omitted item is accounted for, so
    # there is no blocker and the decision stays binding. Under the old gate
    # this same cohort produced binding_decision_allowed False and forced
    # ask_operator.
    assert blockers == []
    assert receipt["binding_decision_allowed"] is True


def test_pathological_omitted_cardinality_still_revokes_binding_authority():
    # The security pin for the fix above. Loosening the authority gate is
    # only safe because a floor still has to be paid for *every* omitted
    # item: DISCOVERY_REVIEW_SEMANTIC_DIGEST_MIN_ITEM_CHARS characters each,
    # or verbatim reproduction. A cohort numerous enough that even that floor
    # cannot be paid genuinely does leave items unaccounted for, and must
    # still refuse a binding decision -- otherwise this fix would have traded
    # one silent-absence defect for a louder one.
    #
    # The floor is pinned with literals here on purpose. Deriving the item
    # count from the constant it is testing would let a future change to the
    # constant re-tune the test to itself and self-certify, and this test is
    # the only thing standing between a lowered floor and an authority claim
    # over evidence nobody can read.
    p = discovery_review_packet
    assert p.DISCOVERY_REVIEW_SEMANTIC_DIGEST_MIN_ITEM_CHARS == 40
    assert p.DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS == 2_000

    # One item more than the 2000-character budget can seat at the 40-char
    # floor, spelled out in bare literals rather than derived from the
    # constants above.
    item_count = 2_000 // 40 + 1
    findings = [
        {
            "id": f"research-{index}",
            "semantic_summary": f"finding {index}: " + ("evidence detail. " * 12),
        }
        for index in range(item_count)
    ]
    # No item is short enough to be rescued by verbatim reproduction, so the
    # floor is the only thing that could cover them.
    assert all(
        len(p._semantic_text(finding)) > 40 for finding in findings
    )

    collections = _digest_collections(research_findings=findings)
    included, coverage = p._evidence_coverage_for_level(
        collections, _digest_detail_counts(), level=4
    )
    fixture = _packet_test_fixture()
    receipt, blockers = p._assemble_receipt(
        source_parent=fixture["source_parent"],
        included=included,
        stranded_deliveries=[],
        evidence_coverage=coverage,
        execution=fixture["execution"],
        missing_reasons=[],
        compacted=True,
    )

    research_coverage = coverage["research_findings"]
    assert research_coverage["semantic_digest_item_count"] == item_count
    assert research_coverage["semantic_digest_min_item_chars"] < 40
    assert research_coverage["semantic_digest_item_coverage_complete"] is False
    assert blockers == [
        "complete authored semantic coverage exceeds the bounded decision "
        "packet for: research_findings"
    ]
    assert receipt["binding_decision_allowed"] is False


def test_semantic_digest_never_exceeds_its_own_bound():
    # The digest is embedded once per collection inside a receipt that must
    # itself fit DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS, so its own cap is
    # load-bearing for the whole packet budget -- including the newline
    # separators between entries, which the per-item shares have to pay for.
    # Pinned literally: a cap raised without re-deriving the per-item shares
    # would silently start overflowing the packet.
    p = discovery_review_packet
    assert p.DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS == 2_000

    collections, counts = _one_verbose_item_cohort()
    _, coverage = p._evidence_coverage_for_level(collections, counts, level=3)
    for name in ("research_findings", "landed_outcomes"):
        assert len(coverage[name]["full_semantic_digest"]) <= 2_000

    # Cardinality regimes, including the one where the separators alone would
    # exceed the cap (more than DISCOVERY_REVIEW_SEMANTIC_DIGEST_CHARS + 1
    # omitted items leaves every per-item share at zero, so a naive join is a
    # wall of newlines longer than the bound).
    for item_count, prose_chars in (
        (1, 9_000),
        (8, 400),
        (51, 200),
        (1_999, 10),
        (2_002, 30),
        (2_500, 0),
        (3_000, 5),
    ):
        omitted = [
            {"semantic_summary": "e" * prose_chars} for _ in range(item_count)
        ]
        digest = p._fit_semantic_digest(omitted)
        assert len(digest["digest"]) <= 2_000, (item_count, prose_chars)
        assert digest["elided_item_count"] >= 0
        assert digest["min_item_chars"] >= 0

    # An empty omitted set is vacuously covered and renders nothing.
    empty = p._fit_semantic_digest([])
    assert empty["digest"] == ""
    assert empty["source_chars"] == 0
    assert empty["prose_complete"] is True
    assert empty["item_coverage_complete"] is True
    assert empty["elided_item_count"] == 0


def test_fair_share_digest_budgets_redistributes_leftover_to_the_long_items():
    # The allocation is max-min fair (water-filling), not an equal split: an
    # equal split would reserve 33 characters for each of two 10-character
    # entries and leave the long entry starved for room nobody needed. That
    # waste is what made the front-to-back digest look unavoidable.
    p = discovery_review_packet
    assert p._fair_share_digest_budgets([10, 10, 1_000], 100) == [10, 10, 80]
    # Two rounds of redistribution: the 1-char entry settles first, then the
    # 20-char one, and everything left goes to the two that are still over.
    shares = p._fair_share_digest_budgets([1, 20, 500, 500], 200)
    assert shares[0] == 1
    assert shares[1] == 20
    assert shares[2] == shares[3] > 20
    assert sum(shares) <= 200
    # Nothing that fits is ever cut, and the budget is never overspent.
    for lengths, budget in (
        ([], 100),
        ([0, 0, 0], 100),
        ([5, 5], 100),
        ([700, 700, 700], 2_000),
        ([1] * 10, 3),
    ):
        shares = p._fair_share_digest_budgets(list(lengths), budget)
        assert len(shares) == len(lengths)
        assert all(share >= 0 for share in shares)
        assert sum(shares) <= budget


def test_fair_share_digest_budgets_handles_degenerate_budgets_and_inputs():
    # Reachable in production: a collection whose omitted set is empty, and a
    # cardinality so high that the separator overhead consumes the whole
    # budget before any content is allocated. Neither may raise, loop, or
    # hand back a negative share that would slice text backwards.
    p = discovery_review_packet
    assert p._fair_share_digest_budgets([], 2_000) == []
    assert p._fair_share_digest_budgets([], 0) == []
    assert p._fair_share_digest_budgets([100, 100], 0) == [0, 0]
    assert p._fair_share_digest_budgets([100, 100], -25) == [0, 0]
    assert p._fair_share_digest_budgets([0, 0], 0) == [0, 0]


def test_semantic_digest_complete_still_means_verbatim_but_no_longer_gates_authority():
    # semantic_digest_complete keeps its historical meaning exactly -- every
    # omitted item reproduced word for word, i.e. the untruncated join fits
    # the cap -- because operators and the review step read it as a fidelity
    # signal. What changed is that it no longer *decides* anything: the
    # authority gate moved to semantic_digest_item_coverage_complete. Pin
    # both, so neither a silent redefinition of the flag nor a quiet
    # re-coupling of it to authority can pass unnoticed.
    p = discovery_review_packet

    fits = [_short_research_finding(index) for index in range(5)]
    complete = p._fit_semantic_digest(fits)
    assert complete["prose_complete"] is True
    assert complete["item_coverage_complete"] is True
    assert complete["elided_item_count"] == 0
    # Verbatim: byte-for-byte the join the pre-fix implementation produced
    # whenever it fit its bound.
    assert complete["digest"] == "\n".join(
        p._semantic_text(finding) for finding in fits
    )
    assert complete["source_chars"] == len(complete["digest"])

    # One item over the bound flips prose fidelity but not coverage...
    collections, counts = _one_verbose_item_cohort()
    included, coverage = p._evidence_coverage_for_level(
        collections, counts, level=3
    )
    assert coverage["research_findings"]["semantic_digest_complete"] is False
    assert (
        coverage["research_findings"]["semantic_digest_item_coverage_complete"]
        is True
    )
    # ...and the receipt built from it is still binding, which is the whole
    # point: the 2026-08-23 stranding was authority revoked over prose.
    fixture = _packet_test_fixture()
    receipt, blockers = p._assemble_receipt(
        source_parent=fixture["source_parent"],
        included=included,
        stranded_deliveries=[],
        evidence_coverage=coverage,
        execution=fixture["execution"],
        missing_reasons=[],
        compacted=True,
    )
    assert blockers == []
    assert receipt["binding_decision_allowed"] is True
