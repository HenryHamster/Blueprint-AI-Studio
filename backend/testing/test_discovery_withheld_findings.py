"""The discovery packet must show what a research cycle threw away.

A research cycle can produce a finding that never becomes a child task, for two
structurally different reasons -- it was TYPE-ROUTED into durable project state
(the design doc's ``proposed_changes``, a direction's ``pending_update``), or it
was DEDUP-FILTERED against work already in flight. Both were observed live on
2026-08-25: six research parents in a single 60-row task page reported a
filtered finding, three of them auto-completed with zero tasks as a direct
result, and one hit both filters in the same run.

Neither kind of finding gets a child task, so neither carries a
``finding_hash`` into the survivors join, so neither reached ``plan_output`` --
which is the only thing the discovery decision packet reads. The reviewer saw
every ``evidence_coverage`` count at zero while the packet's own completeness
manifest reported ``missing_required_evidence: []`` and
``required_evidence_omitted: false``, and the engine escalated to a human
operator for content it was holding the whole time. A completeness signal
reporting clean because it could not look is the recurring shape this codebase
keeps paying for.

The invariant these tests hold: a finding is either turned into a task, or it
reaches the review packet as withheld-with-reason INCLUDING ITS CONTENT.
"""

from __future__ import annotations

import pytest

import app.discovery_review_packet as discovery_review_packet
import app.research_execution_authority as research_execution_authority
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY

from test_discovery_review import _cycle_with_source


def _withheld_carry(*records):
    """Freeze a parent's withheld carry exactly as research completion writes it."""
    return research_execution_authority.bounded_withheld_findings_carry(records)


def _type_routed_record(**overrides):
    finding = {
        "type": "design_doc_observation",
        "title": "Reorder _SLIM_OVERFLOW_DROPPABLE",
        "description": (
            "The droppable order spends the bounded packet budget on the "
            "collection a reviewer needs most."
        ),
        "section": "Decision packet",
    }
    finding.update(overrides.pop("finding", {}))
    kwargs = {
        "disposition": research_execution_authority.WITHHELD_DISPOSITION_TYPE_ROUTED,
        "reason": (
            "design_doc_observation appended to the project design doc's"
            " proposed_changes"
        ),
        "finding_type": "design_doc_observation",
        "routed_to": research_execution_authority.WITHHELD_ROUTE_DESIGN_DOC,
        "destination_id": "obs-1",
        "routing_applied": True,
    }
    kwargs.update(overrides)
    return research_execution_authority.build_withheld_finding_record(finding, **kwargs)


@pytest.mark.asyncio
async def test_type_routed_finding_reaches_the_packet_with_its_content(db_session):
    """The manifest must stop reporting a clean, complete void.

    Verbatim live shape from ``d-1210b978`` generations 5 and 9, and from six
    research parents inside a single 60-row task page on 2026-08-25: the parent
    DID find something, a type router sent it to the design doc instead of a
    task, and because the packet reads only ``plan_output`` -- which is built
    from surviving child tasks -- the finding reached the reviewer as nothing at
    all, under a manifest asserting ``required_evidence_omitted: false`` with
    every coverage count at zero. The engine then asked a human about content it
    was holding the whole time.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    source.latest_status_message = (
        "Research created 0 tasks from 1 findings (1 type-filtered skipped)"
        " — auto-completed (no actionable tasks)"
    )
    source.context = {
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: _withheld_carry(
            _type_routed_record()
        ),
    }
    db_session.add(source)
    await db_session.flush()

    packet, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    snapshot = outcome["evidence_metadata"]["snapshot"]

    # The withheld finding is IN the packet, carrying the prose a reviewer
    # judges -- not merely counted, and not merely named.
    assert manifest["withheld_finding_count"] == 1
    assert manifest["withheld_findings_carried_count"] == 1
    assert manifest["withheld_findings_omitted_count"] == 0
    assert manifest["withheld_finding_content_truncated"] is False
    (carried,) = snapshot["withheld_findings"]
    assert carried["source_task_id"] == str(source.id)
    assert carried["disposition"] == "type_routed"
    assert carried["finding_type"] == "design_doc_observation"
    assert carried["routed_to"] == "project_design_doc.proposed_changes"
    assert carried["destination_id"] == "obs-1"
    assert carried["routing_applied"] is True
    assert carried["reason"].startswith("design_doc_observation appended")
    assert carried["content"]["title"] == "Reorder _SLIM_OVERFLOW_DROPPABLE"
    assert "bounded packet budget" in carried["content"]["description"]
    assert "_SLIM_OVERFLOW_DROPPABLE" in carried["semantic_summary"]

    # Nothing was omitted, so authority stays intact. Honesty here means
    # carrying the content, never revoking binding for having found something.
    assert manifest["missing_required_evidence"] == []
    assert manifest["binding_decision_allowed"] is True
    assert manifest["required_evidence_omitted"] is False

    # And the recommission path (#17932) survives the new carry: a routed
    # observation records what the cycle threw away, it does not score the bet,
    # so the packet is still substantively empty and the engine still
    # re-commissions instead of escalating.
    assert manifest["evidence_substantively_empty"] is True
    assert (
        manifest["substantive_emptiness_reason_code"]
        == "discovery_parent_auto_completed_zero_tasks"
    )


@pytest.mark.asyncio
async def test_word_overlap_filtered_finding_is_carried_the_same_way(db_session):
    """The dedup filter produces the identical lying-manifest shape.

    Live on 2026-08-25: ``4fe8e806`` auto-completed with zero tasks from one
    word-overlap-skipped finding, and ``283a2ae2`` hit the word-overlap AND the
    type filter in the same run. A dedup drop leaves no child task either, so it
    reached the packet by exactly the same absent route. Both dispositions are
    carried, and the type-routed one sorts first because it is the one with no
    other durable surface anywhere.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    source.latest_status_message = (
        "Research created 0 tasks from 2 findings (1 word-overlap,"
        " 1 type-filtered skipped) — auto-completed (no actionable tasks)"
    )
    dedup_record = research_execution_authority.build_withheld_finding_record(
        {
            "type": "improvement",
            "title": "Pin the landed HEAD claim",
            "description": "The verified landed-HEAD claim needs a literal pin.",
        },
        disposition=(
            research_execution_authority.WITHHELD_DISPOSITION_DEDUP_FILTERED
        ),
        reason="word overlap with in-flight work",
        direction_id=cycle.direction_key,
    )
    source.context = {
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: _withheld_carry(
            dedup_record, _type_routed_record()
        ),
    }
    db_session.add(source)
    await db_session.flush()

    packet, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    carried = outcome["evidence_metadata"]["snapshot"]["withheld_findings"]

    assert manifest["withheld_finding_count"] == 2
    assert manifest["withheld_findings_carried_count"] == 2
    assert [item["disposition"] for item in carried] == [
        "type_routed",
        "dedup_filtered",
    ]
    dedup = carried[1]
    assert dedup["reason"] == "word overlap with in-flight work"
    assert dedup["routed_to"] is None
    assert dedup["routing_applied"] is False
    assert dedup["content"]["title"] == "Pin the landed HEAD claim"
    assert "literal pin" in dedup["content"]["description"]


@pytest.mark.asyncio
async def test_routed_finding_whose_side_effect_no_opped_is_still_carried(db_session):
    """The no-op route is the strictly worse case, so it is recorded too.

    A ``direction_update`` naming a direction that is no longer on the project
    lands nowhere: it incremented no counter, never reached the parent's status
    string, and had no durable surface of any kind. It is recorded exactly like
    an applied route, distinguished only by ``routing_applied`` and its reason.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    source.context = {
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: _withheld_carry(
            research_execution_authority.build_withheld_finding_record(
                {
                    "type": "direction_update",
                    "title": "Retire the exhausted direction",
                    "description": "Two probes without improvement; retire it.",
                },
                disposition=(
                    research_execution_authority.WITHHELD_DISPOSITION_TYPE_ROUTED
                ),
                reason=(
                    "direction_update no-op: target direction d-gone is not on"
                    " the project"
                ),
                finding_type="direction_update",
                routed_to=(
                    research_execution_authority.WITHHELD_ROUTE_DIRECTION_PENDING_UPDATE
                ),
                routing_applied=False,
            )
        ),
    }
    db_session.add(source)
    await db_session.flush()

    packet, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    (carried,) = outcome["evidence_metadata"]["snapshot"]["withheld_findings"]

    assert manifest["withheld_finding_count"] == 1
    assert manifest["withheld_findings_carried_count"] == 1
    assert carried["routing_applied"] is False
    assert carried["destination_id"] is None
    assert "no-op" in carried["reason"]
    assert carried["content"]["title"] == "Retire the exhausted direction"


@pytest.mark.asyncio
async def test_withheld_carry_overflow_blocks_binding_without_holding_evidence(
    db_session,
):
    """Overflow past the bounded carry forbids binding -- and never waits.

    A record the parent could not fit is an item genuinely unaccounted for, so
    it must revoke binding authority. It must NOT become
    ``missing_required_evidence``: that holds the cycle in EVIDENCE_PENDING with
    no review spawned at all, and no retry can shrink a finding, so the
    direction would starve forever on evidence that cannot arrive.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    overflowing = [
        _type_routed_record(
            finding={"title": f"Routed observation {index}"},
            destination_id=f"obs-{index}",
        )
        for index in range(research_execution_authority.WITHHELD_FINDINGS_CAP + 3)
    ]
    carry = _withheld_carry(*overflowing)
    assert carry["omitted_count"] == 3
    source.context = {
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: carry,
    }
    db_session.add(source)
    await db_session.flush()

    _, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )

    assert manifest["withheld_finding_count"] == (
        research_execution_authority.WITHHELD_FINDINGS_CAP + 3
    )
    assert manifest["withheld_findings_carried_count"] == (
        research_execution_authority.WITHHELD_FINDINGS_CAP
    )
    assert manifest["withheld_findings_omitted_count"] == 3
    assert manifest["binding_decision_allowed"] is False
    assert manifest["required_evidence_omitted"] is True
    assert manifest["missing_required_evidence"] == []
    assert any(
        "withheld finding record(s) exceeded the bounded research carry" in blocker
        for blocker in manifest["binding_action_blockers"]
    )


@pytest.mark.asyncio
async def test_unfinalized_withheld_carry_is_not_read_as_evidence(db_session):
    """A pre-gate carry is not authority; a rewritable copy must not bind.

    Gated exactly like ``finalized_research_findings``: without the finalized
    marker the carry could still be rewritten by a retry, and evidence a
    reviewer binds on must not move under it.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    source.context = {
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: _withheld_carry(
            _type_routed_record()
        )
    }
    db_session.add(source)
    await db_session.flush()

    packet, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")

    assert manifest["withheld_finding_count"] == 0
    assert outcome["evidence_metadata"]["snapshot"]["withheld_findings"] == []


@pytest.mark.asyncio
async def test_oversized_withheld_finding_names_what_it_could_not_carry(db_session):
    """A bounded excerpt must announce its own cut, in the manifest too.

    The packet has a fixed evidence budget, so a withheld finding's prose is
    bounded -- but a bound that quietly drops a field would trade one silent
    omission for another, which is the whole defect being fixed here. The
    record keeps a checksum of the FULL finding, flags the cut, and names every
    field it could not afford; the manifest reports that a cut happened at all.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    oversized = research_execution_authority.build_withheld_finding_record(
        {
            "type": "design_doc_observation",
            "title": "T" * 400,
            "description": "D" * 900,
            "section": "architecture",
            "rationale": "R" * 400,
        },
        disposition=research_execution_authority.WITHHELD_DISPOSITION_TYPE_ROUTED,
        reason="design_doc_observation appended",
        routed_to=research_execution_authority.WITHHELD_ROUTE_DESIGN_DOC,
        routing_applied=True,
    )
    assert oversized["content_truncated"] is True
    # Budget spent front-to-back in decision-value order, so the reviewer keeps
    # the title and the description and is TOLD about the rest.
    assert oversized["content"]["title"] == "T" * 400
    assert len(oversized["content"]["description"]) == (
        research_execution_authority.WITHHELD_FINDING_CONTENT_CHARS - 400
    )
    assert oversized["content_omitted_keys"] == ["section", "rationale"]
    # The checksum still covers the whole original finding, so the excerpt is
    # provably an excerpt of that exact finding rather than a rewrite.
    assert oversized["finding_checksum"] == (
        research_execution_authority._withheld_checksum(
            {
                "type": "design_doc_observation",
                "title": "T" * 400,
                "description": "D" * 900,
                "section": "architecture",
                "rationale": "R" * 400,
            }
        )
    )

    source.context = {
        FINALIZED_RESEARCH_FINDINGS_KEY: True,
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY: _withheld_carry(
            oversized
        ),
    }
    db_session.add(source)
    await db_session.flush()

    packet, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    (carried,) = outcome["evidence_metadata"]["snapshot"]["withheld_findings"]

    assert manifest["withheld_finding_content_truncated"] is True
    assert carried["content_truncated"] is True
    assert carried["content_omitted_keys"] == ["section", "rationale"]
    # Bounded, but still per-field structure -- not collapsed into an excerpt
    # blob by the packet's own serializer.
    assert set(carried["content"]) == {"title", "description"}


def test_withheld_findings_carry_is_server_owned_context():
    """A member or agent must not be able to author the reviewer's evidence.

    The packet surfaces this carry as content a binding advance/park/stop is
    made against, so an untrusted writer could fabricate the very material the
    review reads.
    """
    assert research_execution_authority.is_server_owned_research_context_key(
        research_execution_authority.WITHHELD_FINDINGS_CONTEXT_KEY
    )
