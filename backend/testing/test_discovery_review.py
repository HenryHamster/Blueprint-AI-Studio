"""Focused contracts for the single-pass discovery decision boundary."""

from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlmodel import select

import app.discovery_review_packet as discovery_review_packet
import app.strategic_spawn_requests as strategic_spawn_requests
from app.discovery_admission import (
    DiscoveryCycleUnavailable,
    ensure_discovery_cycle_for_research,
)
from app.discovery_review import (
    DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS,
    DiscoveryReviewResultError,
    block_failed_discovery_review,
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
    parse_discovery_review_result,
    resolve_discovery_cycle,
    sweep_discovery_reviews,
)
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import (
    Artifact,
    ArtifactType,
    DiscoveryCycle,
    KnowledgeDocument,
    Project,
    ProjectMessage,
    StrategicSpawnRequest,
    TASK_ORIGINATION_RESEARCH,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


def _next_bet(*, claim: str = "Try the bounded mechanism") -> dict[str, str]:
    return {
        "mode": "build",
        "claim": claim,
        "decision_question": "Does the mechanism improve the target outcome?",
        "success_evidence": "A measured probe improves the target outcome.",
        "retirement_condition": "Retire after two probes without improvement.",
    }


async def _no_budget_guard(*_args, **_kwargs):
    return None


async def _cycle_with_source(
    db_session,
    *,
    source_status: TaskStatus = TaskStatus.COMPLETED,
    state: str = "open",
    generation: int = 1,
    deadline_at=None,
):
    direction_key = f"d-{uuid.uuid4().hex[:8]}"
    direction = {
        "id": direction_key,
        "text": "Prove the bounded mechanism",
        "status": "active",
        "priority": "medium",
        "paths": ["backend/app/discovery_review.py"],
        "context": {
            "success_evidence": "The probe improves the target outcome.",
            "retirement_condition": "Retire after two failed probes.",
            "implementation_constraints": "Reuse the current runtime.",
        },
    }
    project = Project(
        name=f"discovery-review-{uuid.uuid4().hex[:8]}",
        user_id="discovery-review-test",
        directions=[direction],
    )
    db_session.add(project)
    await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=direction_key,
        generation=generation,
        outcome_kind="mixed",
        state=state,
        deadline_at=deadline_at,
        intent_snapshot={"goal": "prove useful behavior"},
        bet_snapshot={
            "hypothesis": "the mechanism helps",
            "direction": direction,
        },
    )
    db_session.add(cycle)
    await db_session.flush()
    source = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="Research source",
        payloads=[{"type": "research"}],
        status=source_status,
        created_by="ORCHESTRATOR",
        plan_output={"findings": []},
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
    )
    db_session.add(source)
    await db_session.flush()
    cycle.source_research_task_id = source.id
    db_session.add(cycle)
    await db_session.flush()
    return project, cycle, source


def _evidence_ids(review: Task) -> list[str]:
    return [
        str(item["evidence_id"])
        for item in review.first_payload["evidence_packet"]
        if item.get("evidence_id")
    ]


def _result(cycle: DiscoveryCycle, review: Task, *, action: str = "advance"):
    result = {
        "cycle_id": str(cycle.id),
        "reviewed_revision": review.first_payload["expected_revision"],
        "action": action,
        "summary": f"Evidence supports {action}.",
        "evidence_citations": _evidence_ids(review),
        "next_bet": None,
        "park_reason": None,
        "resume_condition": None,
        "operator_question": None,
    }
    if action == "advance":
        result["next_bet"] = _next_bet()
    elif action == "park":
        result["park_reason"] = "The external benchmark is not yet available."
        result["resume_condition"] = "Resume when the benchmark is published."
    elif action == "ask_operator":
        result["evidence_citations"] = []
        result["operator_question"] = "Which product constraint should dominate?"
    return result


async def _give_the_cohort_real_evidence(db_session, cycle, source) -> Task:
    """Add one citable research outcome so the packet is not substantively empty.

    ``_cycle_with_source`` alone produces the *empty* shape -- a research parent
    that settled without a single actionable child -- which the decision
    boundary now re-commissions rather than measures. Tests that mean to
    exercise the "evidence exists but was not checked" branch must say so.
    """

    child = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Cohort research child",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        plan_output=[{"title": "A real finding", "evidence": "receipt"}],
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
    )
    db_session.add(child)
    await db_session.flush()
    return child


async def _settled_recommission(db_session, cycle, *, generation: int) -> DiscoveryCycle:
    """A terminal prior generation the engine re-commissioned instead of asking."""

    prior = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=generation,
        outcome_kind=cycle.outcome_kind,
        state="reviewed",
        verdict="advance",
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=dict(cycle.bet_snapshot or {}),
        review_result={
            "action": "advance",
            "converted_from_ask_operator": {
                "reason": "binding_decision_allowed",
                "reason_code": "discovery_parent_auto_completed_zero_tasks",
                "disposition": "recommission",
                "original_action": "ask_operator",
            },
        },
    )
    db_session.add(prior)
    await db_session.flush()
    return prior


def _withhold_binding_authority(db_session, review: Task) -> None:
    """Rewrite this decision packet's manifest as an incomplete-evidence one.

    ``ask_operator`` is only reachable through a packet that withholds binding
    authority, so coverage of the operator-resolution path builds that packet
    shape explicitly. Only the manifest field is rewritten; ``evidence_packet``
    and ``packet_checksum`` stay exactly as the server froze them, so the
    stale-packet guard still sees the same packet.
    """

    payload = dict(review.first_payload)
    payload["evidence_packet_manifest"] = {
        **dict(payload.get("evidence_packet_manifest") or {}),
        "binding_decision_allowed": False,
        "required_evidence_omitted": True,
    }
    review.payloads = [payload, *list(review.payloads or [])[1:]]
    db_session.add(review)


@pytest.mark.asyncio
async def test_semantic_receipt_excludes_unfinalized_research_findings(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    source.plan_output = [{"title": "Rejected raw claim"}]
    source.context = {}
    db_session.add(source)
    await db_session.flush()

    packet, _, _, _, _, _ = await discovery_review_packet.build_discovery_review_packet(
        db_session, cycle
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    assert outcome["evidence_metadata"]["snapshot"]["research_findings"] == []


@pytest.mark.asyncio
async def test_review_packet_excludes_foreign_project_cycle_cohort(db_session):
    """A corrupt shared cycle id cannot leak another project's task evidence."""
    _, cycle, _ = await _cycle_with_source(db_session)
    foreign_project = Project(
        name=f"foreign-discovery-{uuid.uuid4().hex[:8]}",
        user_id="discovery-review-test",
    )
    db_session.add(foreign_project)
    await db_session.flush()
    foreign = Task(
        project_id=foreign_project.id,
        discovery_cycle_id=cycle.id,
        title="FOREIGN SECRET TASK",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        plan_output=[{"title": "FOREIGN SECRET FINDING", "evidence": "secret"}],
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
    )
    db_session.add(foreign)
    await db_session.flush()

    packet, _, _, _, _, _ = await discovery_review_packet.build_discovery_review_packet(
        db_session, cycle
    )
    serialized = discovery_review_packet._canonical_json(packet)

    assert str(foreign.id) not in serialized
    assert "FOREIGN SECRET" not in serialized


@pytest.mark.asyncio
async def test_semantic_receipt_includes_all_parallel_research_outcomes(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    parallel = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Parallel research",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        plan_output=[{"title": "Parallel finding", "evidence": "receipt"}],
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
    )
    db_session.add(parallel)
    await db_session.flush()
    web_research = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Parallel web research",
        payloads=[{"type": "web_research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(web_research)
    await db_session.flush()
    document = KnowledgeDocument(
        project_id=project.id,
        source_task_id=web_research.id,
        source_kind="web_research",
        title="Parallel external result",
        summary="The external result changes the bounded next decision.",
        body="# Result\n\nDurable research evidence.",
        topic="parallel-discovery",
        source_citations=[{"url": "https://example.com/result"}],
        forward_quality_verdict={"passed": True},
    )
    db_session.add(document)
    await db_session.flush()

    packet, _, _, _, _, _ = await discovery_review_packet.build_discovery_review_packet(
        db_session, cycle
    )
    outcome = next(item for item in packet if item["semantic_role"] == "parent_outcome")
    snapshot = outcome["evidence_metadata"]["snapshot"]

    assert any(
        item["source_task_id"] == str(parallel.id)
        and item["finding"]["title"] == "Parallel finding"
        for item in snapshot["research_findings"]
    )
    assert len(snapshot["knowledge_outcomes"]) == 1
    knowledge_outcome = snapshot["knowledge_outcomes"][0]
    assert knowledge_outcome["document_id"] == str(document.id)
    assert knowledge_outcome["source_task_id"] == str(web_research.id)
    assert knowledge_outcome["source_kind"] == "web_research"
    assert knowledge_outcome["title"] == "Parallel external result"
    assert knowledge_outcome["summary"] == (
        "The external result changes the bounded next decision."
    )
    assert "bounded next decision" in knowledge_outcome["semantic_summary"]


@pytest.mark.asyncio
async def test_terminal_source_spawns_exactly_one_medium_decision(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)

    first = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    replay = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert first.spawned is True
    assert replay.spawned is False
    assert replay.task_id == first.task_id
    review = await db_session.get(Task, first.task_id)
    assert review is not None
    assert review.parent_task_id is None
    assert review.discovery_cycle_id == cycle.id
    assert review.first_payload["type"] == "discovery_review"
    assert review.first_payload["effort"] == "medium"
    assert review.first_payload["provider_preference"] == ["anthropic"]
    assert "review_round" not in review.first_payload
    assert "confirmation_proposal" not in review.first_payload
    assert cycle.state == "reviewing"


@pytest.mark.asyncio
async def test_parallel_cohort_over_twelve_tasks_still_gets_one_decision(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    for index in range(20):
        db_session.add(
            Task(
                project_id=source.project_id,
                discovery_cycle_id=cycle.id,
                parent_task_id=source.id,
                title=f"Research child {index}",
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                latest_status_message="done",
            )
        )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)

    assert spawned.spawned is True
    assert review.first_payload["evidence_packet_manifest"]["task_count"] == 21
    assert [
        item["semantic_role"] for item in review.first_payload["evidence_packet"]
    ] == ["packet_manifest", "parent_outcome"]
    review_tasks = list(
        (
            await db_session.execute(
                select(Task).where(Task.discovery_cycle_id == cycle.id)
            )
        )
        .scalars()
        .all()
    )
    assert [
        task.id
        for task in review_tasks
        if task.first_payload.get("type") == "discovery_review"
    ] == [review.id]


@pytest.mark.asyncio
async def test_active_cohort_waits_without_cancelling_parallel_work(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    active = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Still building",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(active)
    await db_session.flush()

    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert waiting.reason == "cohort_active"
    assert waiting.spawned is False
    assert active.status == TaskStatus.IN_PROGRESS

    active.status = TaskStatus.COMPLETED
    active.context = {"landed_on_integration": True, "merge_commit_sha": "a" * 40}
    db_session.add(active)
    await db_session.flush()
    assert (await maybe_close_and_spawn_discovery_review(db_session, cycle.id)).spawned


@pytest.mark.asyncio
async def test_post_commit_materialization_must_settle_before_review(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    source.post_commit_pending = True
    db_session.add(source)
    await db_session.flush()

    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert waiting.reason == "source_research_post_commit_pending"
    assert waiting.spawned is False

    source.post_commit_pending = False
    materializing = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Materializing parallel findings",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
        created_by="ORCHESTRATOR",
    )
    db_session.add_all([source, materializing])
    await db_session.flush()

    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert waiting.reason == "cohort_post_commit_pending"
    assert waiting.spawned is False

    materializing.post_commit_pending = False
    db_session.add(materializing)
    await db_session.flush()
    assert (await maybe_close_and_spawn_discovery_review(db_session, cycle.id)).spawned


@pytest.mark.asyncio
async def test_paused_project_authors_no_latent_discovery_review(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    project.paused = True
    db_session.add(project)
    await db_session.commit()

    result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert result.spawned is False
    assert result.reason == "project_paused"
    await db_session.flush()
    await db_session.refresh(cycle)
    assert cycle.state == "review_pending"
    cycle_tasks = (
        await db_session.execute(
            select(Task).where(Task.discovery_cycle_id == cycle.id)
        )
    ).scalars().all()
    assert all(
        task.first_payload.get("type") != "discovery_review" for task in cycle_tasks
    )


@pytest.mark.asyncio
async def test_missing_delivery_receipt_stays_evidence_pending_without_agent_cost(
    db_session,
):
    _, cycle, source = await _cycle_with_source(db_session)
    db_session.add(
        Task(
            project_id=source.project_id,
            discovery_cycle_id=cycle.id,
            parent_task_id=source.id,
            title="Unconfirmed write",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_number": 123},
        )
    )
    await db_session.flush()

    first_checked = _utcnow()
    pending = await maybe_close_and_spawn_discovery_review(
        db_session, cycle.id, now=first_checked
    )

    assert pending.spawned is False
    assert pending.reason == "evidence_pending"
    assert cycle.state == "review_pending"
    assert cycle.review_result["status"] == "EVIDENCE_PENDING"
    reviews = list(
        (
            await db_session.execute(
                select(Task).where(Task.discovery_cycle_id == cycle.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(task.first_payload.get("type") != "discovery_review" for task in reviews)

    escalated = await maybe_close_and_spawn_discovery_review(
        db_session,
        cycle.id,
        now=first_checked + timedelta(hours=2, seconds=1),
    )
    assert escalated.reason == "evidence_pending_escalated"
    await db_session.flush()
    project_messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == cycle.project_id
                )
            )
        )
        .scalars()
        .all()
    )
    operator_message = next(
        message
        for message in project_messages
        if message.context["event_type"] == "discovery.operator_question"
    )
    assert operator_message.context["audience"] == "operator-action-needed"
    assert operator_message.context["cycle_id"] == str(cycle.id)
    assert operator_message.context["cycle_revision"] == cycle.revision

    successor = await resolve_discovery_cycle(
        db_session,
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        cycle_id=operator_message.context["cycle_id"],
        cycle_revision=operator_message.context["cycle_revision"],
        action="continue",
        next_bet=_next_bet(claim="Repair delivery evidence with a bounded follow-up"),
        note="Resident selected a bounded recovery path.",
    )
    assert successor.state == "open"
    assert cycle.verdict == "advance"


@pytest.mark.asyncio
async def test_candidate_sha_is_not_accepted_as_exact_landed_identity(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    db_session.add(
        Task(
            project_id=source.project_id,
            discovery_cycle_id=cycle.id,
            parent_task_id=source.id,
            title="Write with only a candidate SHA",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={
                "landed_on_integration": True,
                "candidate_sha": "b" * 40,
            },
        )
    )
    await db_session.flush()

    pending = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert pending.reason == "evidence_pending"
    assert any(
        "lacks exact commit identity" in reason
        for reason in cycle.review_result["missing_required_evidence"]
    )


@pytest.mark.asyncio
async def test_canonical_landing_write_resolves_commit_identity_gap(db_session):
    """A landed write task stamped through mark_landed_on_integration with a
    merge SHA no longer reports 'lacks exact commit identity'.

    This exercises the end-to-end fix: the canonical write records the exact
    merge commit, so the discovery-review receipt can resolve commit identity
    and the cycle is not pinned in EVIDENCE_PENDING.
    """
    from app.landed_signal import mark_landed_on_integration

    _, cycle, source = await _cycle_with_source(db_session)
    landed_task = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Write with recorded merge identity",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={},
    )
    db_session.add(landed_task)
    await db_session.flush()

    mark_landed_on_integration(landed_task, merge_commit_sha="c" * 40)
    db_session.add(landed_task)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert snapshot["missing_required_evidence"] == []
    assert any(
        item.get("merge_commit_sha") == "c" * 40
        for item in snapshot["landed_outcomes"]
    )


@pytest.mark.asyncio
async def test_abundant_semantic_evidence_is_bounded_without_stranding(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    source.plan_output = [
        {"title": f"Finding {index}", "evidence": "x" * 50}
        for index in range(12)
    ]
    source.context = {FINALIZED_RESEARCH_FINDINGS_KEY: True}
    parallel = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Parallel contradictory research",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        plan_output=[
            {
                "title": "Contradicting omitted finding",
                "evidence": "The parallel result reverses the source conclusion.",
            }
        ],
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
    )
    db_session.add_all([source, parallel])
    document_base = _utcnow()
    db_session.add_all(
        [
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=source.id,
                source_kind="research",
                title=f"Knowledge outcome {index}",
                summary=(
                    "Contradicting earliest knowledge outcome"
                    if index == 0
                    else f"Bounded semantic outcome {index}"
                ),
                body=f"# Outcome {index}",
                topic="bounded-discovery",
                source_citations=[],
                forward_quality_verdict={"passed": True},
                created_at=document_base + timedelta(microseconds=index),
            )
            for index in range(13)
        ]
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert len(snapshot["research_findings"]) == 6
    assert len(snapshot["knowledge_outcomes"]) == 6
    assert snapshot["missing_required_evidence"] == []
    research_coverage = snapshot["evidence_coverage"]["research_findings"]
    assert research_coverage["included_count"] == 6
    assert research_coverage["omitted_count"] == 7
    assert research_coverage["total_count"] == 13
    assert len(research_coverage["full_set_checksum"]) == 64
    assert any(
        "reverses the source conclusion" in item["semantic_excerpt"]
        and item["source_task_id"] == str(parallel.id)
        for item in research_coverage["omitted_semantic_index"]
    )
    assert "reverses the source conclusion" in research_coverage["full_semantic_digest"]
    assert research_coverage["semantic_digest_complete"] is True
    knowledge_coverage = snapshot["evidence_coverage"]["knowledge_outcomes"]
    assert knowledge_coverage["included_count"] == 6
    assert knowledge_coverage["omitted_count"] == 7
    assert knowledge_coverage["total_count"] == 13
    assert len(knowledge_coverage["full_set_checksum"]) == 64
    assert "Contradicting earliest knowledge outcome" in knowledge_coverage[
        "omitted_semantic_index"
    ][0]["semantic_excerpt"]


def test_semantic_overflow_index_is_bounded_and_samples_stable_range():
    values = [
        {
            "source_task_id": str(uuid.uuid4()),
            "finding": {"title": f"Outcome {index}"},
        }
        for index in range(100)
    ]
    coverage = discovery_review_packet._bounded_collection_coverage(
        values,
        values[:12],
        omitted=values[12:],
        selection="first_12_in_stable_task_order",
    )

    assert coverage["semantic_index_total_count"] == 88
    assert coverage["semantic_index_included_count"] == 3
    assert [
        item["semantic_excerpt"] for item in coverage["omitted_semantic_index"]
    ] == ["Outcome 12", "Outcome 56", "Outcome 99"]

    contradiction_values = []
    for index in range(10):
        finding = {
            "title": f"Experiment outcome {index}",
            "evidence": "CONTRADICTION" if index == 5 else "support",
            "risk": "high" if index == 5 else "low",
        }
        contradiction_values.append(
            {
                "source_task_id": str(uuid.uuid4()),
                "finding": finding,
                "semantic_summary": discovery_review_packet._canonical_semantic_value(
                    finding
                ),
            }
        )
    contradiction_coverage = discovery_review_packet._bounded_collection_coverage(
        contradiction_values,
        [],
        omitted=contradiction_values,
        selection="none",
    )
    assert contradiction_coverage["semantic_digest_complete"] is True
    assert "CONTRADICTION" in contradiction_coverage["full_semantic_digest"]

    project_id = uuid.uuid4()
    verification_values = [
        discovery_review_packet._verification_summary(
            Task(
                project_id=project_id,
                title=f"QA {index}",
                payloads=[{"type": "qa"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                latest_status_message="Verification completed",
                context={
                    "acceptance_verdict": {
                        "a_detail": "x" * 700 if index == 2 else "",
                        "summary": "REJECT: critical regression"
                        if index == 2
                        else "PASS"
                    },
                    "acceptance_evidence_result": {"probe": f"qa-{index}"},
                },
            )
        )
        for index in range(10)
    ]
    verification_coverage = discovery_review_packet._bounded_collection_coverage(
        verification_values,
        verification_values[-6:],
        omitted=verification_values[:-6],
        selection="latest_6_in_stable_task_order",
    )
    assert verification_coverage["semantic_digest_complete"] is True
    assert "REJECT: critical regression" in verification_coverage[
        "full_semantic_digest"
    ]


def test_receipt_budget_includes_stored_metadata_wrapper():
    receipt = {
        "payload": "x"
        * (discovery_review_packet.DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS - 30)
    }
    assert len(discovery_review_packet._canonical_json(receipt)) <= (
        discovery_review_packet.DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS
    )
    assert discovery_review_packet._receipt_fits_evidence_metadata(receipt) is False


@pytest.mark.asyncio
async def test_landed_outcomes_are_bounded_without_stranding(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    db_session.add_all(
        [
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
            for index in range(20)
        ]
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    assert "evidence_metadata_truncated" not in outcome
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert len(snapshot["landed_outcomes"]) == 4
    coverage = snapshot["evidence_coverage"]["landed_outcomes"]
    assert coverage["total_count"] == 20
    assert coverage["omitted_count"] == 16
    assert coverage["semantic_index_included_count"] == 3
    assert snapshot["missing_required_evidence"] == []


@pytest.mark.asyncio
async def test_landed_outcome_preserves_every_artifact_semantic(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    landed_task = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Landed write with artifact history",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "landed_on_integration": True,
            "merge_commit_sha": "a" * 40,
        },
    )
    db_session.add(landed_task)
    await db_session.flush()
    artifact_base = _utcnow()
    db_session.add_all(
        [
            Artifact(
                task_id=landed_task.id,
                artifact_type=ArtifactType.RESEARCH_FINDINGS,
                logical_key=f"landed-artifact-{index}",
                storage_uri=f"gs://bucket/landed-{index}.json",
                logs=(
                    "REJECT: oldest artifact exposes a critical failure"
                    if index == 0
                        else f"artifact {index} passed"
                    ),
                    created_at=artifact_base + timedelta(microseconds=index),
                )
            for index in range(5)
        ]
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    landed = outcome["evidence_metadata"]["snapshot"]["landed_outcomes"][0]
    assert len(landed["artifacts"]) == 5
    assert "oldest artifact exposes a critical failure" in landed["artifacts"][0][
        "semantic_summary"
    ]


@pytest.mark.asyncio
async def test_long_complete_evidence_shrinks_to_total_packet_budget(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    document_base = _utcnow()
    db_session.add_all(
        [
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=source.id,
                source_kind="research",
                title=f"Long outcome {index}",
                summary=f"Outcome {index}: " + ("evidence " * 260),
                body="# Full durable result",
                topic="bounded-discovery",
                source_citations=[],
                forward_quality_verdict={"passed": True},
                created_at=document_base + timedelta(microseconds=index),
            )
            for index in range(12)
        ]
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    snapshot = outcome["evidence_metadata"]["snapshot"]
    assert snapshot["missing_required_evidence"] == []
    assert len(discovery_review_packet._canonical_json(snapshot)) <= (
        discovery_review_packet.DISCOVERY_REVIEW_EVIDENCE_METADATA_CHARS
    )
    assert all(len(item["summary"]) <= 500 for item in snapshot["knowledge_outcomes"])
    manifest = review.first_payload["evidence_packet_manifest"]
    # Until 2026-08-23 this asserted the opposite -- no binding decision, and
    # complete_discovery_review rejecting anything but ask_operator. That
    # encoded the defect the fleet-idle incident exposed: the evidence in
    # this cohort is *complete*, the word in this test's own name, merely
    # long, and shrinking it to the packet budget is the intended behaviour,
    # not a loss of authority. Gating authority on the omitted-evidence
    # digest reproducing every item's prose verbatim is what stranded five
    # directions at ask_operator, each asking the operator to hand back
    # evidence the engine already held. The digest now shares its budget out
    # per item, so every omitted knowledge outcome is still represented above
    # the readable floor and the binding decision is permitted to land.
    knowledge_coverage = snapshot["evidence_coverage"]["knowledge_outcomes"]
    assert knowledge_coverage["semantic_digest_complete"] is False
    assert knowledge_coverage["semantic_digest_item_coverage_complete"] is True
    assert manifest["binding_action_blockers"] == []
    assert manifest["binding_decision_allowed"] is True
    assert manifest["required_evidence_omitted"] is False
    completed = await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review),
    )
    await db_session.flush()
    assert completed.id == cycle.id
    assert cycle.state == "reviewed"
    assert cycle.verdict == "advance"


@pytest.mark.asyncio
async def test_included_knowledge_preserves_full_authored_semantics(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    decisive_tail = "REJECT: hidden regression after display prefix"
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=source.id,
            source_kind="research",
            title="Full authored outcome",
            summary=("x" * 700) + decisive_tail,
            body="# Durable result",
            topic="semantic-completeness",
            source_citations=[],
            forward_quality_verdict={"passed": True},
        )
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    document = outcome["evidence_metadata"]["snapshot"]["knowledge_outcomes"][0]
    assert len(document["summary"]) == 500
    assert decisive_tail in document["semantic_summary"]


def test_parser_requires_one_exact_total_action():
    cycle_id = str(uuid.uuid4())
    base = {
        "cycle_id": cycle_id,
        "reviewed_revision": 2,
        "action": "advance",
        "summary": "Advance on the evidence.",
        "evidence_citations": [str(uuid.uuid4())],
        "next_bet": _next_bet(),
        "park_reason": None,
        "resume_condition": None,
        "operator_question": None,
    }
    assert parse_discovery_review_result(base)["action"] == "advance"
    with pytest.raises(DiscoveryReviewResultError, match="exactly"):
        parse_discovery_review_result({**base, "review_round": 2})
    with pytest.raises(DiscoveryReviewResultError, match="invalid"):
        parse_discovery_review_result({**base, "action": "inconclusive"})
    with pytest.raises(DiscoveryReviewResultError, match="evidence citations"):
        parse_discovery_review_result({**base, "evidence_citations": []})


@pytest.mark.asyncio
async def test_decision_is_invalidated_if_cohort_changes_after_packet(db_session):
    _, cycle, source = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    result = _result(cycle, review)
    review.status = TaskStatus.COMPLETED
    late_materialization = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Late materialized finding",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
        created_by="ORCHESTRATOR",
    )
    db_session.add_all([review, late_materialization])
    await db_session.flush()

    invalidated = await complete_discovery_review(
        db_session,
        task=review,
        raw_result=result,
    )
    assert invalidated.id == cycle.id
    assert cycle.state == "review_pending"
    assert cycle.review_task_id is None
    assert cycle.review_result == {
        "status": "REVIEW_PACKET_INVALIDATED",
        "reason": "cohort_post_commit_pending",
        "stale_review_task_id": str(review.id),
    }

    late_materialization.post_commit_pending = False
    db_session.add(late_materialization)
    await db_session.flush()
    replacement = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert replacement.spawned is True
    assert replacement.task_id != review.id


@pytest.mark.asyncio
async def test_binding_action_must_cite_parent_outcome_not_only_manifest(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest_id = next(
        item["evidence_id"]
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "packet_manifest"
    )
    result = _result(cycle, review)
    result["evidence_citations"] = [manifest_id]

    with pytest.raises(DiscoveryReviewResultError, match="parent outcome"):
        await complete_discovery_review(db_session, task=review, raw_result=result)
    assert cycle.state == "review_blocked"


@pytest.mark.asyncio
async def test_advance_atomically_creates_successor_and_dispatch_request(
    db_session, monkeypatch
):
    import app.shared_state as shared_state
    from app.max_plan_pool import resolve_task_routing

    project, cycle, source = await _cycle_with_source(db_session, generation=99)
    project.problem_model = {
        "capabilities": [
            {"name": "Successor Probe", "status": "working"},
            {"name": "Future Harness", "status": "planned"},
        ]
    }
    project.capability_proof_records = {
        "successor-probe": [{"proof_kind": "named_test", "commit_sha": "abc123"}]
    }
    monkeypatch.setitem(
        shared_state.runtime_config,
        "agent_prompt_suffixes",
        {"research": "Preserve the configured successor research instructions."},
    )
    monkeypatch.setitem(
        shared_state.runtime_config,
        "task_type_models",
        {"research": "legacy-low-tier-model"},
    )
    monkeypatch.setitem(
        shared_state.runtime_config,
        "task_type_efforts",
        {"research": "low"},
    )
    monkeypatch.setitem(
        shared_state.runtime_config,
        "task_type_routing",
        {"research": {"effort": "max"}},
    )
    source.context = {
        **(source.context or {}),
        "deferred_findings": [
            {
                "finding_hash": f"deferred-{index}",
                "type": "improvement" if index < 10 else "direction_note",
                "title": f"Deferred {index}",
                "description": f"Carry {index} forward",
                **(
                    {"direction_id": ""}
                    if index == 1
                    else {"direction_id": "   "}
                    if index == 2
                    else {"direction_id": cycle.direction_key}
                    if index
                    else {}
                ),
                "deferred_count": index,
            }
            for index in range(20)
        ]
        + [
            {
                "finding_hash": "agent-forged-collision",
                "type": "direction_note",
                "title": "First distinct colliding carry",
                "description": "Preserve the first independent observation.",
                "direction_id": cycle.direction_key,
                "deferred_count": 21,
            },
            {
                "finding_hash": "agent-forged-collision",
                "type": "direction_note",
                "title": "Second distinct colliding carry",
                "description": "Preserve the second independent observation.",
                "direction_id": cycle.direction_key,
                "deferred_count": 20,
            },
            {
                # A different raw lineage hash cannot defeat canonical dedup;
                # stable first-wins ordering keeps the earlier matching body.
                "finding_hash": "different-raw-hash-same-body",
                "type": "direction_note",
                "title": "First distinct colliding carry",
                "description": "Preserve the first independent observation.",
                "direction_id": cycle.direction_key,
                "deferred_count": 22,
            },
            {
                "finding_hash": "other-direction",
                "title": "Do not carry",
                "direction_id": "unrelated",
            },
        ],
    }
    db_session.add(source)
    await db_session.commit()
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)

    completed = await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review),
    )
    await db_session.flush()

    assert completed.id == cycle.id
    assert cycle.state == "reviewed"
    assert cycle.verdict == "advance"
    successor = await db_session.get(
        DiscoveryCycle, uuid.UUID(cycle.review_result["next_cycle_id"])
    )
    request = await db_session.get(
        StrategicSpawnRequest,
        uuid.UUID(cycle.review_result["successor_spawn_request_id"]),
    )
    assert successor is not None
    assert successor.state == "open"
    assert successor.generation == 100
    assert successor.bet_snapshot["direction"]["context"] == cycle.bet_snapshot[
        "direction"
    ]["context"]
    assert request is not None
    assert request.state == "deferred"
    assert request.not_before is not None
    assert request.discovery_cycle_id == successor.id
    assert request.parent_task_id == review.id
    successor_task_spec = request.specification["task"]
    successor_payload = successor_task_spec["payloads"][0]
    assert successor_payload["type"] == "research"
    assert successor_payload["work_headroom"] == 5
    assert successor_payload["base_branch"] == "main"
    assert successor_payload["prompt_suffix"] == (
        "Preserve the configured successor research instructions."
    )
    assert "model" not in successor_payload
    assert "effort" not in successor_payload
    assert successor_payload["available_capabilities"] == [
        {"name": "successor-probe", "status": "working", "proof_status": "proven"},
        {"name": "future-harness", "status": "planned", "proof_status": "unproven"},
    ]
    assert successor_payload["directions"][0]["status"] == "active"
    assert successor_payload["directions"][0]["paths"] == [
        "backend/app/discovery_review.py"
    ]
    assert successor_task_spec["priority"] == 50
    carried = successor_task_spec["context"]["prior_deferred_findings"]
    assert [item["finding_hash"] for item in carried] == [
        "agent-forged-collision",
        "agent-forged-collision",
        *(f"deferred-{index}" for index in range(19, -1, -1)),
    ]
    assert [item["title"] for item in carried[:2]] == [
        "First distinct colliding carry",
        "Second distinct colliding carry",
    ]
    assert all(item["source_task_id"] == str(source.id) for item in carried)
    assert all(item["direction_id"] == cycle.direction_key for item in carried)

    monkeypatch.setattr(
        strategic_spawn_requests,
        "_budget_guard_for_request",
        _no_budget_guard,
    )
    drained = await strategic_spawn_requests.drain_deferred_spawn_requests(
        db_session,
        project_id=cycle.project_id,
    )
    assert [item.id for item in drained] == [request.id]
    assert request.state == "dispatched"
    source_task = await db_session.get(Task, request.task_id)
    assert source_task is not None
    assert source_task.discovery_cycle_id == successor.id
    assert source_task.created_by == "ORCHESTRATOR"
    assert source_task.origination == TASK_ORIGINATION_RESEARCH
    assert len((source_task.context or {})["prior_deferred_findings"]) == 22
    assert resolve_task_routing(source_task, shared_state.runtime_config)[
        "effort"
    ] == "max"
    assert successor.source_research_task_id == source_task.id


@pytest.mark.asyncio
async def test_successor_rejects_foreign_project_source_reference(db_session):
    """A corrupt source edge cannot copy foreign deferred findings forward."""
    from app.discovery_review import _create_successor_with_request

    _, cycle, decision_task = await _cycle_with_source(db_session)
    foreign_project = Project(
        name=f"foreign-successor-{uuid.uuid4().hex[:8]}",
        user_id="discovery-review-test",
    )
    db_session.add(foreign_project)
    await db_session.flush()
    foreign_source = Task(
        project_id=foreign_project.id,
        title="Foreign deferred source",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "deferred_findings": [
                {
                    "finding_hash": "foreign-secret",
                    "title": "FOREIGN SECRET DEFERRED FINDING",
                    "direction_id": cycle.direction_key,
                }
            ]
        },
    )
    db_session.add(foreign_source)
    await db_session.flush()
    cycle.source_research_task_id = foreign_source.id
    db_session.add(cycle)
    await db_session.flush()

    with pytest.raises(
        DiscoveryReviewResultError,
        match="source research task is missing or belongs to another project",
    ):
        await _create_successor_with_request(
            db_session,
            cycle=cycle,
            task=decision_task,
            result={"next_bet": _next_bet()},
            now=_utcnow(),
        )


@pytest.mark.asyncio
async def test_foreign_project_review_reference_is_not_reused(db_session):
    """Cycle-to-review and review-to-cycle edges are both tenant fenced."""
    _, cycle, _ = await _cycle_with_source(db_session, state="reviewing")
    foreign_project = Project(
        name=f"foreign-review-{uuid.uuid4().hex[:8]}",
        user_id="discovery-review-test",
    )
    db_session.add(foreign_project)
    await db_session.flush()
    foreign_review = Task(
        project_id=foreign_project.id,
        discovery_cycle_id=cycle.id,
        title="Foreign review",
        payloads=[{"type": "discovery_review", "expected_revision": cycle.revision}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add(foreign_review)
    await db_session.flush()
    cycle.review_task_id = foreign_review.id
    db_session.add(cycle)
    await db_session.flush()

    reconciled = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert reconciled.reason == "review_blocked"
    assert reconciled.task_id is None
    assert cycle.state == "review_blocked"
    assert foreign_review.status == TaskStatus.IN_PROGRESS
    with pytest.raises(DiscoveryReviewResultError, match="no longer exists"):
        await complete_discovery_review(
            db_session,
            task=foreign_review,
            raw_result={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["park", "stop"])
async def test_park_and_stop_are_terminal_decisions_without_successors(
    db_session, action
):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action=action),
    )
    await db_session.flush()
    await db_session.refresh(project)

    assert cycle.state == "reviewed"
    assert cycle.verdict == action
    assert "next_cycle_id" not in cycle.review_result
    direction = next(item for item in project.directions if item["id"] == cycle.direction_key)
    if action == "park":
        assert direction["status"] == "paused"
        assert direction["discovery_resume_condition"]
    else:
        assert direction["status"] == "retired"
        assert direction["retirement_outcome"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["advance", "park", "stop"])
async def test_binding_decision_rejects_staged_direction_authority(
    db_session, action
):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    project.directions = [
        {**direction, "status": "staged"}
        if direction["id"] == cycle.direction_key
        else direction
        for direction in project.directions
    ]
    db_session.add(project)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="changed to staged"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review, action=action),
        )

    assert cycle.state == "review_blocked"
    assert cycle.review_result["status"] == "DECISION_FAILED"
    assert "next_cycle_id" not in cycle.review_result


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["advance", "park", "stop"])
async def test_binding_decision_rejects_edited_active_direction_authority(
    db_session, action
):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    project.directions = [
        {**direction, "text": "Strategic replaced the active direction"}
        if direction["id"] == cycle.direction_key
        else direction
        for direction in project.directions
    ]
    db_session.add(project)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="authority changed"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review, action=action),
        )

    assert cycle.state == "review_blocked"
    assert cycle.review_result["status"] == "DECISION_FAILED"


@pytest.mark.asyncio
async def test_ask_operator_wakes_resident_and_does_not_spawn_more_review(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    # ask_operator survives only for a packet that withholds binding authority;
    # a complete packet converts it into a bounded evidence bet instead.
    _withhold_binding_authority(db_session, review)
    await db_session.flush()

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="ask_operator"),
    )
    await db_session.flush()

    assert cycle.state == "reviewed"
    assert cycle.verdict == "ask_operator"
    messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(messages) == 2
    operator_message = next(
        message
        for message in messages
        if message.context["event_type"] == "discovery.operator_question"
    )
    assert operator_message.context["resident_wake_barrier"] is True
    assert operator_message.context["policy_status"] == "ask_operator"
    assert operator_message.context["audience"] == "operator-action-needed"


@pytest.mark.asyncio
async def test_evidence_question_is_dispatched_not_escalated(db_session):
    """A packet that grants authority never hands its question back.

    Verbatim from direction d-1210b978 cycle 81ba8010 on 2026-08-24: the
    reviewer correctly refused to grade a claim it had not verified, correctly
    named the remedy -- re-dispatch a verification task -- and then asked the
    operator for permission to do the thing it was already authorised to do.
    The direction sat at discovery_ask_operator_awaiting_resolution for ~16
    hours. ask_operator is for preference, authority, scope, and priority
    calls only.

    Pinned here on a cohort that really holds evidence, which is what a bounded
    ``measure`` successor is the right answer to.
    """
    project, cycle, source = await _cycle_with_source(db_session)
    await _give_the_cohort_real_evidence(db_session, cycle, source)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["binding_decision_allowed"] is True
    assert manifest["required_evidence_omitted"] is False
    # This branch is specifically "evidence exists, the reviewer did not check
    # it". The empty-cohort variant of the same escalation is a different
    # disposition entirely -- see
    # test_empty_research_result_is_recommissioned_not_escalated.
    assert manifest["evidence_substantively_empty"] is False

    question = (
        "The packet carries a research finding for this bet, but nothing in it "
        "actually checked whether the named liveness tests exist and pass at "
        "fad98a14. Should we re-dispatch a direct verification task before this "
        "bet can be graded?"
    )
    reviewer_summary = (
        "The manifest formally marks binding_decision_allowed true, but that "
        "reflects packet completeness, not substantive verification."
    )
    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = question
    escalation["summary"] = reviewer_summary

    completed = await complete_discovery_review(
        db_session, task=review, raw_result=escalation
    )
    await db_session.flush()

    assert completed.id == cycle.id
    assert cycle.state == "reviewed"
    # Literal pin: the escalation vocabulary is unreachable for this packet.
    assert cycle.verdict == "advance"
    assert cycle.verdict != "ask_operator"
    assert cycle.review_result["operator_question"] is None

    # It continues with a bounded bet that goes and gets the missing evidence,
    # and does not fabricate a grade for the bet it could not verify.
    next_bet = cycle.review_result["next_bet"]
    assert next_bet["mode"] == "measure"
    assert question in next_bet["decision_question"]
    assert reviewer_summary in cycle.review_result["summary"]
    conversion = cycle.review_result["converted_from_ask_operator"]
    assert conversion["original_action"] == "ask_operator"
    assert conversion["original_operator_question"] == question

    # The successor is really dispatched, not merely recorded.
    successor = await db_session.get(
        DiscoveryCycle, uuid.UUID(cycle.review_result["next_cycle_id"])
    )
    assert successor is not None
    assert successor.generation == cycle.generation + 1
    assert successor.bet_snapshot["mode"] == "measure"
    request = await db_session.get(
        StrategicSpawnRequest,
        uuid.UUID(cycle.review_result["successor_spawn_request_id"]),
    )
    assert request is not None
    assert request.discovery_cycle_id == successor.id

    # Nothing woke the operator and the direction stays live.
    messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        message.context["event_type"] != "discovery.operator_question"
        for message in messages
    )
    await db_session.refresh(project)
    direction = next(
        item for item in project.directions if item["id"] == cycle.direction_key
    )
    assert direction["status"] == "active"


@pytest.mark.asyncio
async def test_dispatched_evidence_bet_does_not_block_the_direction(db_session):
    """The converted verdict must clear direction admission, not just look right."""
    from app.discovery_admission import direction_discovery_admission_decision

    project, cycle, source = await _cycle_with_source(db_session)
    await _give_the_cohort_real_evidence(db_session, cycle, source)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = (
        "Should we re-run the two named tests at that head before grading?"
    )
    await complete_discovery_review(
        db_session, task=review, raw_result=escalation
    )
    await db_session.flush()

    await db_session.refresh(project)
    direction = next(
        item for item in project.directions if item["id"] == cycle.direction_key
    )
    decision = direction_discovery_admission_decision(direction, cycle)
    # Literal pin on the reason code that stranded d-1210b978 for ~16 hours.
    assert decision.reason_code != "discovery_ask_operator_awaiting_resolution"
    assert "ask_operator" not in decision.reason_code


@pytest.mark.asyncio
async def test_empty_research_result_is_recommissioned_not_escalated(db_session):
    """An empty research result is a pipeline outcome, not an operator decision.

    2026-08-25: ``tasks autonomy-debug`` read ``can_spawn: NO (blocked)`` with
    three directions at once ineligible on
    ``discovery_ask_operator_awaiting_resolution``, every one of them with
    ``binding_allowed=yes`` and an evidence packet that was structurally
    perfect and substantively empty -- the research parent auto-completed with
    zero actionable tasks. The engine asked a human about a void it had
    produced itself, and the operator cleared all three by hand.
    """
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]

    # The precise shape of the defect: authority granted, nothing omitted, and
    # still nothing to judge.
    assert manifest["binding_decision_allowed"] is True
    assert manifest["required_evidence_omitted"] is False
    assert manifest["missing_required_evidence"] == []
    assert manifest["evidence_substantively_empty"] is True
    assert (
        manifest["substantive_emptiness_reason_code"]
        == "discovery_parent_auto_completed_zero_tasks"
    )

    question = (
        "The research task for this bet auto-completed with 0 actionable "
        "findings, so nothing in the packet actually checked whether the named "
        "liveness tests exist and pass. Should we re-dispatch before grading?"
    )
    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = question
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    # Literal pin: the escalation vocabulary is unreachable for this packet.
    assert cycle.verdict == "advance"
    assert cycle.verdict != "ask_operator"
    assert cycle.review_result["operator_question"] is None

    # It re-runs the research bet rather than measuring a void.
    next_bet = cycle.review_result["next_bet"]
    assert next_bet["mode"] == "research"
    assert question in next_bet["decision_question"]
    conversion = cycle.review_result["converted_from_ask_operator"]
    assert conversion["disposition"] == "recommission"
    assert conversion["reason_code"] == "discovery_parent_auto_completed_zero_tasks"
    assert conversion["original_action"] == "ask_operator"
    assert conversion["original_operator_question"] == question

    successor = await db_session.get(
        DiscoveryCycle, uuid.UUID(cycle.review_result["next_cycle_id"])
    )
    assert successor is not None
    assert successor.generation == cycle.generation + 1
    assert successor.bet_snapshot["mode"] == "research"

    # No human was woken and the direction stays live and admissible.
    from app.discovery_admission import direction_discovery_admission_decision

    messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(
        message.context["event_type"] != "discovery.operator_question"
        for message in messages
    )
    completion = next(
        message
        for message in messages
        if message.context["event_type"] == "discovery.review_completed"
    )
    # Surface the reason, not just the state.
    assert (
        completion.context["escalation_replacement_reason_code"]
        == "discovery_parent_auto_completed_zero_tasks"
    )
    assert completion.context["escalation_replacement_disposition"] == "recommission"
    await db_session.refresh(project)
    direction = next(
        item for item in project.directions if item["id"] == cycle.direction_key
    )
    assert direction["status"] == "active"
    decision = direction_discovery_admission_decision(direction, cycle)
    assert decision.reason_code != "discovery_ask_operator_awaiting_resolution"


@pytest.mark.asyncio
async def test_platform_killed_research_parent_is_recommissioned(db_session):
    """A platform kill carries no verdict about the work, so it is re-run.

    "Re-evaluation limit reached (2 attempts) -- children consistently failing"
    and the 2h approval-gate timeout are defects in the engine, not findings
    about the bet. Handing that to the operator asks them to adjudicate an
    infrastructure failure.
    """
    _, cycle, _ = await _cycle_with_source(
        db_session, source_status=TaskStatus.FAILED
    )
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["binding_decision_allowed"] is True
    assert (
        manifest["substantive_emptiness_reason_code"]
        == "discovery_parent_platform_failed"
    )

    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = (
        "The research parent failed outright. Should we retry this direction?"
    )
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    assert cycle.verdict == "advance"
    conversion = cycle.review_result["converted_from_ask_operator"]
    assert conversion["reason_code"] == "discovery_parent_platform_failed"
    assert conversion["disposition"] == "recommission"
    assert cycle.review_result["next_bet"]["mode"] == "research"


@pytest.mark.asyncio
async def test_cohort_that_ran_and_produced_nothing_is_recommissioned(db_session):
    """Children ran, nothing citable came back: still empty, still not a question."""
    _, cycle, source = await _cycle_with_source(db_session)
    db_session.add(
        Task(
            project_id=source.project_id,
            discovery_cycle_id=cycle.id,
            parent_task_id=source.id,
            title="Child that produced nothing",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            plan_output=[],
            context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
        )
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["task_count"] == 2
    assert (
        manifest["substantive_emptiness_reason_code"]
        == "discovery_empty_evidence_coverage"
    )

    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = "Nothing came back. What should we do?"
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    assert cycle.verdict == "advance"
    assert (
        cycle.review_result["converted_from_ask_operator"]["reason_code"]
        == "discovery_empty_evidence_coverage"
    )


@pytest.mark.asyncio
async def test_type_filtered_only_cycle_is_recommissioned_not_escalated(db_session):
    """A cycle whose only finding was type-filtered is a recommission candidate.

    Live shape, ``d-1210b978`` gen=5 and again gen=9: the research DID produce a
    finding, but it was a ``design_doc_observation`` / ``direction_update``, so
    ``research_completion`` routed it to the design doc or the direction row
    instead of spawning a task. It therefore never joined the surviving-child
    set that ``finalized_research_findings_for_survivors`` builds ``plan_output``
    from, so the packet's coverage is all zeros while the manifest -- which can
    only report on evidence it knows about -- says nothing was omitted.

    Making that manifest honest about the filtered finding is a separate change
    in ``research_completion``/``discovery_review_packet``. This pins the part
    the decision boundary owns: however the finding went missing, the engine
    re-commissions the bet instead of asking a human about a packet it cannot
    read. The status string is a verbatim literal from the live incident so a
    reworded emission cannot silently un-pin this path.
    """
    _, cycle, source = await _cycle_with_source(db_session)
    source.latest_status_message = (
        "Research created 0 tasks from 1 findings (1 type-filtered skipped)"
        " — auto-completed (no actionable tasks)"
    )
    db_session.add(
        Task(
            project_id=source.project_id,
            discovery_cycle_id=cycle.id,
            parent_task_id=source.id,
            title="Sibling that also produced nothing citable",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            plan_output=[],
            context={FINALIZED_RESEARCH_FINDINGS_KEY: True},
        )
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    # More than one task ran, so the reason code comes from the auto-completed
    # marker, not from the "cohort never fanned out" shortcut.
    assert manifest["task_count"] == 2
    assert manifest["binding_decision_allowed"] is True
    assert (
        manifest["substantive_emptiness_reason_code"]
        == "discovery_parent_auto_completed_zero_tasks"
    )

    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = (
        "The packet is empty but the parent says it had one finding. "
        "Do the named liveness tests exist?"
    )
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    assert cycle.verdict == "advance"
    assert cycle.review_result["operator_question"] is None
    conversion = cycle.review_result["converted_from_ask_operator"]
    assert conversion["disposition"] == "recommission"
    assert conversion["reason_code"] == "discovery_parent_auto_completed_zero_tasks"


@pytest.mark.asyncio
async def test_recommission_ceiling_parks_instead_of_re_firing(db_session):
    """The replacement for an unbounded escalation must not be an unbounded loop.

    Two consecutive engine re-commissions is the whole budget; the third empty
    settle is evidence about the bet, not about the run. It degrades to a park
    carrying a terminal reason code -- never a loop, and never a quiet second
    escalation.
    """
    assert DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS == 2
    project, cycle, _ = await _cycle_with_source(
        db_session, generation=DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS + 1
    )
    for generation in range(1, DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS + 1):
        await _settled_recommission(db_session, cycle, generation=generation)

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    assert review.first_payload["evidence_packet_manifest"][
        "evidence_substantively_empty"
    ] is True

    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = "Still nothing. Should we keep going?"
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    # Terminal and legible, not another successor and not another question.
    assert cycle.verdict == "park"
    assert cycle.verdict != "ask_operator"
    assert cycle.review_result.get("next_cycle_id") is None
    assert cycle.review_result["operator_question"] is None
    conversion = cycle.review_result["converted_from_ask_operator"]
    assert conversion["reason_code"] == "discovery_recommission_ceiling_reached"
    assert conversion["disposition"] == "recommission_ceiling_park"
    assert (
        conversion["consecutive_recommissions"]
        == DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS
    )
    assert "discovery_recommission_ceiling_reached" not in cycle.review_result[
        "park_reason"
    ]
    assert "re-commission ceiling reached" in cycle.review_result["park_reason"]
    assert (
        await db_session.execute(
            select(StrategicSpawnRequest).where(
                StrategicSpawnRequest.direction_id == cycle.direction_key
            )
        )
    ).scalars().first() is None

    await db_session.refresh(project)
    direction = next(
        item for item in project.directions if item["id"] == cycle.direction_key
    )
    assert direction["status"] == "paused"


@pytest.mark.asyncio
async def test_a_progressing_direction_never_accumulates_a_ceiling(db_session):
    """Only an unbroken run of engine re-commissions counts toward the ceiling."""
    _, cycle, _ = await _cycle_with_source(
        db_session, generation=DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS + 1
    )
    # The most recent prior generation was the reviewer's own call, which
    # breaks the run no matter how many re-commissions preceded it.
    for generation in range(1, DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS):
        await _settled_recommission(db_session, cycle, generation=generation)
    reviewer_decided = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=DISCOVERY_MAX_CONSECUTIVE_RECOMMISSIONS,
        outcome_kind=cycle.outcome_kind,
        state="reviewed",
        verdict="advance",
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=dict(cycle.bet_snapshot or {}),
        review_result={"action": "advance"},
    )
    db_session.add(reviewer_decided)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    escalation = _result(cycle, review, action="ask_operator")
    escalation["operator_question"] = "Nothing came back again. What now?"
    await complete_discovery_review(db_session, task=review, raw_result=escalation)
    await db_session.flush()

    assert cycle.verdict == "advance"
    assert (
        cycle.review_result["converted_from_ask_operator"]["disposition"]
        == "recommission"
    )


@pytest.mark.asyncio
async def test_empty_evidence_without_authority_still_escalates(db_session):
    """Narrow ask_operator; do not remove it.

    A packet that withholds binding authority is the genuine judgement-call
    shape, and it keeps reaching the operator even when the cohort is empty --
    the emptiness classifier must not silently swallow it.
    """
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    assert review.first_payload["evidence_packet_manifest"][
        "evidence_substantively_empty"
    ] is True
    _withhold_binding_authority(db_session, review)
    await db_session.flush()

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="ask_operator"),
    )
    await db_session.flush()

    assert cycle.verdict == "ask_operator"
    assert "converted_from_ask_operator" not in cycle.review_result
    operator_messages = [
        message
        for message in (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
            )
        )
        .scalars()
        .all()
        if message.context["event_type"] == "discovery.operator_question"
    ]
    assert len(operator_messages) == 1


@pytest.mark.asyncio
async def test_manifest_reports_substance_when_the_cohort_has_any(db_session):
    """The classifier must not fire on a packet that carries real evidence."""
    _, cycle, source = await _cycle_with_source(db_session)
    await _give_the_cohort_real_evidence(db_session, cycle, source)

    _, _, _, manifest, _, _ = (
        await discovery_review_packet.build_discovery_review_packet(db_session, cycle)
    )

    assert manifest["evidence_substantively_empty"] is False
    assert manifest["substantive_emptiness_reason_code"] is None


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_invalid_or_failed_decision_blocks_visibly(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    bad = _result(cycle, review)
    bad["reviewed_revision"] += 1

    with pytest.raises(DiscoveryReviewResultError, match="reviewed_revision"):
        await complete_discovery_review(db_session, task=review, raw_result=bad)
    assert cycle.state == "review_blocked"
    assert cycle.review_result["status"] == "DECISION_FAILED"

    _, second_cycle, _ = await _cycle_with_source(db_session)
    second_spawn = await maybe_close_and_spawn_discovery_review(
        db_session, second_cycle.id
    )
    second_review = await db_session.get(Task, second_spawn.task_id)
    second_review.status = TaskStatus.FAILED
    db_session.add(second_review)
    await db_session.flush()
    assert await block_failed_discovery_review(
        db_session, task=second_review, reason="provider retries exhausted"
    )
    # A transient decision-task death retries before failing the cycle closed.
    assert second_cycle.state == "review_pending"
    assert second_cycle.review_result["status"] == "DECISION_RETRY"
    assert second_cycle.review_result["error"] == "provider retries exhausted"


@pytest.mark.asyncio
async def test_transient_decision_death_retries_then_blocks(db_session):
    """A decision task that dies without deciding respawns (bounded), then blocks."""
    _, cycle, _ = await _cycle_with_source(db_session)
    first = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert first.spawned is True
    seen_review_ids = {first.task_id}

    for _attempt in (1, 2):
        review = await db_session.get(Task, cycle.review_task_id)
        review.status = TaskStatus.CANCELLED
        db_session.add(review)
        await db_session.flush()
        result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
        assert result.spawned is True
        assert cycle.state == "reviewing"
        assert cycle.review_task_id not in seen_review_ids
        seen_review_ids.add(cycle.review_task_id)

    review = await db_session.get(Task, cycle.review_task_id)
    review.status = TaskStatus.CANCELLED
    db_session.add(review)
    await db_session.flush()
    final = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert final.spawned is False
    assert final.reason == "review_blocked"
    assert cycle.state == "review_blocked"
    assert cycle.review_result["status"] == "DECISION_FAILED"


@pytest.mark.asyncio
async def test_failed_parent_stranded_delivery_reviews_instead_of_pending(db_session):
    """A FAILED research parent's unlanded child PR is settled evidence, not a hold.

    The parent's staging branch dies with it, so the child PR can never gain a
    landed receipt — the review must spawn and judge the stranded delivery
    rather than the cycle waiting in EVIDENCE_PENDING forever.
    """
    _, cycle, source = await _cycle_with_source(
        db_session, source_status=TaskStatus.FAILED
    )
    db_session.add(
        Task(
            project_id=source.project_id,
            discovery_cycle_id=cycle.id,
            parent_task_id=source.id,
            title="Stranded write",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_number": 123},
        )
    )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["stranded_delivery_count"] == 1
    assert not any(
        "without landed receipt" in item
        for item in manifest["missing_required_evidence"]
    )


@pytest.mark.asyncio
async def test_unapplicable_direction_decision_blocks_instead_of_half_applying(
    db_session,
):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    project.directions = []
    db_session.add(project)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="missing from project"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review, action="stop"),
        )

    assert cycle.state == "review_blocked"
    assert cycle.verdict is None
    assert cycle.review_result["status"] == "DECISION_FAILED"


@pytest.mark.asyncio
async def test_advance_revalidates_current_direction_authority(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    directions = list(project.directions)
    directions[0] = {**directions[0], "status": "retired"}
    project.directions = directions
    db_session.add(project)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="authority changed"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review),
        )
    assert cycle.state == "review_blocked"
    requests = list(
        (
            await db_session.execute(
                select(StrategicSpawnRequest).where(
                    StrategicSpawnRequest.discovery_cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert requests == []


@pytest.mark.asyncio
async def test_park_revalidates_current_direction_authority(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    directions = list(project.directions)
    directions[0] = {**directions[0], "status": "retired"}
    project.directions = directions
    db_session.add(project)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="authority changed"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_result(cycle, review, action="park"),
        )
    await db_session.refresh(project)
    direction = next(item for item in project.directions if item["id"] == cycle.direction_key)
    assert direction["status"] == "retired"
    assert cycle.state == "review_blocked"


@pytest.mark.asyncio
async def test_operator_can_close_an_automatically_parked_cycle(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="park"),
    )
    expected_revision = cycle.revision

    closed = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        cycle_id=cycle.id,
        cycle_revision=expected_revision,
        action="close",
        note="The operator is retiring the parked direction.",
    )
    await db_session.flush()
    await db_session.refresh(project)

    assert closed.id == cycle.id
    assert cycle.verdict == "stop"
    direction = next(
        item for item in project.directions if item["id"] == cycle.direction_key
    )
    assert direction["status"] == "retired"


@pytest.mark.asyncio
async def test_operator_continue_uses_same_atomic_successor_contract(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    _withhold_binding_authority(db_session, review)
    await db_session.flush()
    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="ask_operator"),
    )
    expected_revision = cycle.revision
    selected_bet = _next_bet(claim="Try the operator-selected bounded alternative")
    selected_note = "Prefer the lab-compatible path."
    project.directions = [
        {**direction, "text": "Operator-refined active direction"}
        if direction["id"] == cycle.direction_key
        else direction
        for direction in project.directions
    ]
    db_session.add(project)
    await db_session.flush()

    successor = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        cycle_id=cycle.id,
        cycle_revision=expected_revision,
        action="continue",
        next_bet=selected_bet,
        note=selected_note,
    )
    await db_session.flush()

    assert cycle.verdict == "advance"
    assert cycle.review_result["operator_resolution"]["action"] == "continue"
    request = await db_session.get(
        StrategicSpawnRequest,
        uuid.UUID(cycle.review_result["successor_spawn_request_id"]),
    )
    assert request is not None
    assert request.discovery_cycle_id == successor.id
    assert successor.bet_snapshot["direction"]["text"] == (
        "Operator-refined active direction"
    )
    replay = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        cycle_id=cycle.id,
        cycle_revision=expected_revision,
        action="continue",
        next_bet=selected_bet,
        note=selected_note,
    )
    assert replay.id == successor.id
    with pytest.raises(ValueError, match="conflicting"):
        await resolve_discovery_cycle(
            db_session,
            project_id=project.id,
            direction_key=cycle.direction_key,
            cycle_id=cycle.id,
            cycle_revision=expected_revision,
            action="continue",
            next_bet=_next_bet(claim="A different replay must not mutate authority"),
            note=selected_note,
        )


@pytest.mark.asyncio
async def test_resident_can_resolve_discovery_through_strategic_op(
    db_session, mocker
):
    from httpx import ASGITransport, AsyncClient

    from app.database import get_session
    from app.main import app
    from app.routers.agent import verify_agent_token

    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    _withhold_binding_authority(db_session, review)
    await db_session.flush()
    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="ask_operator"),
    )
    expected_revision = cycle.revision
    resident = Task(
        project_id=project.id,
        task_type="strategic_review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review"}],
    )
    db_session.add(resident)
    await db_session.commit()

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(resident.id),
        "task_type": "strategic_review",
    }
    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.post(
                "/api/agent/strategic",
                json={
                    "task_id": str(resident.id),
                    "op": "resolve_discovery",
                    "data": {
                        "direction_id": cycle.direction_key,
                        "cycle_id": str(cycle.id),
                        "cycle_revision": expected_revision,
                        "action": "continue",
                        "next_bet": _next_bet(claim="Run the operator-selected bet"),
                        "note": "Use the lab-compatible path.",
                    },
                },
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(verify_agent_token, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["op"] == "resolve_discovery"
    assert body["action"] == "continue"
    await db_session.refresh(cycle)
    assert cycle.verdict == "advance"
    assert cycle.review_result["successor_spawn_request_id"]


@pytest.mark.asyncio
async def test_operator_can_resume_a_parked_direction(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, action="park"),
    )
    expected_revision = cycle.revision
    await db_session.flush()
    parked_message = next(
        message
        for message in (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.from_task_id == review.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if message.context["event_type"] == "discovery.direction_parked"
    )
    assert parked_message.context["resident_wake_barrier"] is True
    assert parked_message.context["cycle_revision"] == expected_revision

    successor = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        cycle_id=cycle.id,
        cycle_revision=expected_revision,
        action="continue",
        next_bet=_next_bet(claim="Resume after the dependency arrived"),
        note="The recorded resume condition is now satisfied.",
    )
    await db_session.flush()
    await db_session.refresh(project)

    direction = next(item for item in project.directions if item["id"] == cycle.direction_key)
    assert direction["status"] == "active"
    assert "discovery_resume_condition" not in direction
    assert successor.state == "open"
    assert cycle.verdict == "advance"


@pytest.mark.asyncio
async def test_legacy_requestless_successor_requires_one_explicit_next_bet(db_session):
    project, predecessor, source = await _cycle_with_source(db_session)
    predecessor.state = "reviewed"
    predecessor.verdict = "advance"
    predecessor.review_task_id = source.id
    predecessor.review_result = {"action": "advance"}
    successor = DiscoveryCycle(
        project_id=project.id,
        direction_key=predecessor.direction_key,
        generation=predecessor.generation + 1,
        outcome_kind="mixed",
        state="open",
        intent_snapshot={"goal": "legacy continuation"},
        bet_snapshot={"hypothesis": "legacy free-form continuation"},
    )
    db_session.add(predecessor)
    db_session.add(successor)
    await db_session.flush()

    reconciled = await maybe_close_and_spawn_discovery_review(db_session, successor.id)
    await db_session.flush()

    assert reconciled.reason == "legacy_successor_needs_next_bet"
    assert successor.state == "reviewed"
    assert successor.verdict == "ask_operator"
    assert successor.review_result["status"] == "LEGACY_SUCCESSOR_NEEDS_NEXT_BET"
    message = next(
        message
        for message in (
            (
                await db_session.execute(
                    select(ProjectMessage).where(ProjectMessage.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        if message.context["event_type"] == "discovery.operator_question"
    )
    assert message.context["cycle_id"] == str(successor.id)
    assert message.context["cycle_revision"] == successor.revision

    replacement = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=successor.direction_key,
        cycle_id=successor.id,
        cycle_revision=successor.revision,
        action="continue",
        next_bet=_next_bet(claim="Replace the legacy continuation explicitly"),
        note="Recovered once after controller migration.",
    )
    assert replacement.generation == successor.generation + 1
    assert successor.verdict == "advance"


@pytest.mark.asyncio
async def test_sweep_surfaces_legacy_advisory_disposition_once(db_session):
    project, cycle, source = await _cycle_with_source(db_session, state="reviewed")
    cycle.verdict = "ask_operator"
    cycle.review_task_id = source.id
    cycle.review_result = {
        "status": "LEGACY_DISPOSITION_REQUIRES_RESOLUTION",
        "legacy_advisory_verdict": "park",
    }
    db_session.add(cycle)
    await db_session.flush()

    assert await sweep_discovery_reviews(db_session, limit=10) == 1
    await db_session.refresh(cycle)
    assert cycle.review_result["status"] == "LEGACY_DISPOSITION_AWAITING_RESOLUTION"
    messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    question = next(
        message
        for message in messages
        if message.context["event_type"] == "discovery.operator_question"
    )
    assert question.context["cycle_id"] == str(cycle.id)
    assert question.context["cycle_revision"] == cycle.revision
    assert question.context["legacy_advisory_verdict"] == "park"

    assert await sweep_discovery_reviews(db_session, limit=10) == 0


@pytest.mark.asyncio
async def test_legacy_advance_cycle_can_restart_but_explicit_dispositions_block(
    db_session,
):
    project, cycle, _ = await _cycle_with_source(db_session, state="reviewed")
    cycle.verdict = "advance"
    await db_session.flush()
    direction = project.directions[0]

    restarted = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=cycle.direction_key,
        direction=direction,
    )
    assert restarted is not None
    assert restarted.generation == cycle.generation + 1

    restarted.state = "reviewed"
    restarted.verdict = "ask_operator"
    await db_session.flush()
    with pytest.raises(DiscoveryCycleUnavailable, match="awaiting policy resolution"):
        await ensure_discovery_cycle_for_research(
            db_session,
            project=project,
            direction_key=cycle.direction_key,
            direction=direction,
        )


@pytest.mark.asyncio
async def test_sweep_repairs_missed_terminal_trigger(db_session):
    _, cycle, _ = await _cycle_with_source(
        db_session, deadline_at=_utcnow() - timedelta(minutes=1)
    )
    changed = await sweep_discovery_reviews(db_session, limit=10)
    assert changed >= 1
    assert cycle.state == "reviewing"


@pytest.mark.asyncio
async def test_sweep_requeues_terminal_legacy_review_and_blocks_cancelled_current(
    db_session,
):
    _, legacy_cycle, _ = await _cycle_with_source(db_session)
    legacy_spawn = await maybe_close_and_spawn_discovery_review(
        db_session, legacy_cycle.id
    )
    legacy_task = await db_session.get(Task, legacy_spawn.task_id)
    legacy_task.payloads = [{**legacy_task.first_payload, "review_round": 2}]
    legacy_task.status = TaskStatus.COMPLETED
    legacy_cycle.state = "review_pending"
    db_session.add(legacy_task)
    db_session.add(legacy_cycle)
    await db_session.flush()

    replacement = await maybe_close_and_spawn_discovery_review(
        db_session, legacy_cycle.id
    )
    assert replacement.spawned is True
    assert replacement.task_id != legacy_task.id

    _, current_cycle, _ = await _cycle_with_source(db_session)
    current_spawn = await maybe_close_and_spawn_discovery_review(
        db_session, current_cycle.id
    )
    current_task = await db_session.get(Task, current_spawn.task_id)
    current_task.status = TaskStatus.CANCELLED
    db_session.add(current_task)
    await db_session.flush()

    # A cancelled current-format decision task retries (bounded) rather than
    # failing the cycle closed on the first transient death.
    retried = await maybe_close_and_spawn_discovery_review(
        db_session, current_cycle.id
    )
    assert retried.spawned is True
    assert retried.task_id != current_task.id
    assert current_cycle.state == "reviewing"


@pytest.mark.asyncio
async def test_deadline_never_cancels_pending_descendant_intent(db_session):
    _, cycle, source = await _cycle_with_source(
        db_session,
        deadline_at=_utcnow() - timedelta(days=2),
    )
    request = StrategicSpawnRequest(
        project_id=cycle.project_id,
        parent_task_id=source.id,
        idempotency_key=f"pending-{uuid.uuid4()}",
        task_type="create",
        specification={
            "task": {
                "title": "Pending closure work",
                "payloads": [{"type": "create"}],
            }
        },
        specification_hash="a" * 64,
        direction_id=cycle.direction_key,
        discovery_cycle_id=cycle.id,
        state="deferred",
        not_before=_utcnow() - timedelta(days=1),
    )
    db_session.add(request)
    await db_session.flush()

    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert waiting.reason == "spawn_requests_pending"
    assert request.state == "deferred"


@pytest.mark.asyncio
async def test_missing_source_is_structural_evidence_gap(db_session):
    project = Project(
        name=f"discovery-missing-source-{uuid.uuid4().hex[:8]}",
        user_id="discovery-review-test",
    )
    db_session.add(project)
    await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key="missing-source",
        outcome_kind="mixed",
        intent_snapshot={},
        bet_snapshot={},
    )
    db_session.add(cycle)
    await db_session.flush()

    result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    await db_session.flush()

    assert result.reason == "evidence_pending_escalated"
    assert cycle.state == "review_pending"
    assert cycle.review_result["status"] == "EVIDENCE_PENDING"
    message = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert message.context["resident_wake_barrier"] is True
