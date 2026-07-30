"""Focused contracts for the durable discovery-review boundary."""

from __future__ import annotations

from datetime import timedelta
import json
import uuid

import pytest
from sqlmodel import select

import app.discovery_review as discovery_review
from app.discovery_admission import (
    authorize_discovery_task_execution,
    reserve_discovery_admission,
)
from app.discovery_review import (
    DiscoveryReviewResultError,
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
    resolve_discovery_cycle,
    sweep_discovery_reviews,
)
from app.models import (
    Artifact,
    ArtifactType,
    DiscoveryAdmission,
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    Project,
    ProjectMessage,
    StrategicSpawnRequest,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


async def _cycle_with_source(
    db_session,
    *,
    project: Project | None = None,
    source_status: TaskStatus = TaskStatus.COMPLETED,
    state: str = "open",
    deadline_at=None,
):
    if project is None:
        project = Project(
            name=f"discovery-review-{uuid.uuid4().hex[:8]}",
            user_id="discovery-review-test",
        )
        db_session.add(project)
        await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=f"d-{uuid.uuid4().hex[:8]}",
        outcome_kind="mixed",
        state=state,
        deadline_at=deadline_at,
        intent_snapshot={"goal": "prove useful behavior"},
        bet_snapshot={"hypothesis": "the mechanism helps"},
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
    )
    db_session.add(source)
    await db_session.flush()
    cycle.source_research_task_id = source.id
    db_session.add(cycle)
    await db_session.flush()
    return project, cycle, source


def _valid_result(cycle, review_task, evidence_ids, **overrides):
    result = {
        "cycle_id": str(cycle.id),
        "reviewed_revision": review_task.first_payload["expected_revision"],
        "verdict": "renew",
        "confidence": 0.82,
        "decision_summary": "The bounded evidence supports one more probe.",
        "goal_progress_assessment": "The mechanism produced measurable progress.",
        "evidence_citations": [str(value) for value in evidence_ids],
        "contradictions": [],
        "next_bet": {"hypothesis": "repeat against a harder case"},
        "recommended_phase": "demonstrate",
        "recommended_limits": {
            "task_credit_limit": 3,
            "attempt_credit_limit": 5,
            "concurrency_limit": 1,
        },
        "operator_question": None,
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_terminal_zero_finding_source_spawns_one_root_review(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)

    first = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    second = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert first.spawned is True
    assert second.spawned is False
    assert second.task_id == first.task_id
    review = await db_session.get(Task, first.task_id)
    assert review is not None
    assert review.parent_task_id is None
    assert review.discovery_cycle_id == cycle.id
    assert review.first_payload["type"] == "discovery_review"
    assert cycle.state == "reviewing"
    assert cycle.review_task_id == review.id


@pytest.mark.asyncio
async def test_active_cohort_closes_admission_but_does_not_strand_work(db_session):
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

    result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert result.spawned is False
    assert result.reason == "cohort_active"
    assert cycle.state == "review_pending"
    assert active.status == TaskStatus.IN_PROGRESS

    # PARKED is shelved rather than live cohort work and must not hold the
    # review boundary forever once all runnable work has drained.
    active.status = TaskStatus.PARKED
    db_session.add(active)
    await db_session.flush()
    result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert result.spawned is True


@pytest.mark.asyncio
async def test_blocked_cohort_drains_after_deadline_grace_as_incomplete_evidence(
    db_session,
):
    deadline = _utcnow() - timedelta(hours=1)
    _, cycle, source = await _cycle_with_source(
        db_session,
        deadline_at=deadline,
    )
    blocked = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Blocked experiment",
        payloads=[{"type": "create"}],
        status=TaskStatus.BLOCKED,
        created_by="ORCHESTRATOR",
        latest_status_message="Waiting for unavailable upstream evidence.",
    )
    db_session.add(blocked)
    await db_session.flush()

    within_grace = await maybe_close_and_spawn_discovery_review(
        db_session,
        cycle.id,
        now=deadline + timedelta(minutes=90),
    )
    assert within_grace.spawned is False
    assert within_grace.reason == "cohort_blocked_in_grace"
    assert blocked.status == TaskStatus.BLOCKED

    after_grace = await maybe_close_and_spawn_discovery_review(
        db_session,
        cycle.id,
        now=deadline + timedelta(hours=2, seconds=1),
    )
    assert after_grace.spawned is True
    assert blocked.status == TaskStatus.BLOCKED
    review = await db_session.get(Task, after_grace.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["incomplete"] is True
    assert manifest["required_evidence_omitted"] is False
    assert manifest["overdue_blocked"] == {
        "count": 1,
        "included_task_ids": [str(blocked.id)],
    }
    assert "overdue_blocked_tasks" in manifest["incomplete_reasons"]
    blocked_packet_item = next(
        entry
        for entry in review.first_payload["evidence_packet"]
        if entry.get("source_id") == str(blocked.id)
    )
    assert (
        blocked_packet_item["evidence_metadata"]["snapshot"]["status"]
        == TaskStatus.BLOCKED.value
    )


@pytest.mark.asyncio
async def test_overdue_deferred_spawn_is_cancelled_and_releases_reserved_credit(
    db_session,
):
    deadline = _utcnow() + timedelta(hours=1)
    project, cycle, source = await _cycle_with_source(
        db_session,
        deadline_at=deadline,
    )
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=source.id,
        discovery_cycle_id=cycle.id,
        idempotency_key=f"deferred-{uuid.uuid4().hex}",
        task_type="create",
        specification={"title": "Deferred experiment"},
        specification_hash="a" * 64,
        state="deferred",
        not_before=_utcnow() - timedelta(minutes=1),
    )
    db_session.add(request)
    await db_session.flush()
    reservation = await reserve_discovery_admission(
        db_session,
        cycle_id=cycle.id,
        admission_key=f"spawn-request:{request.id}",
        spawn_request_id=request.id,
        mode="enforced",
    )
    assert reservation.allowed is True
    assert cycle.reserved_task_credits == 1

    cycle.deadline_at = _utcnow() - timedelta(hours=3)
    db_session.add(cycle)
    await db_session.flush()
    result = await maybe_close_and_spawn_discovery_review(
        db_session,
        cycle.id,
        now=_utcnow(),
    )

    assert result.spawned is True
    await db_session.refresh(request)
    assert request.state == "cancelled"
    assert request.admission_reason == "discovery_cycle_review_boundary"
    assert request.not_before is None
    admission = await db_session.get(DiscoveryAdmission, reservation.admission_id)
    assert admission.state == "released"
    assert cycle.reserved_task_credits == 0
    review = await db_session.get(Task, result.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["overdue_spawn_requests"] == {"count": 1}
    assert manifest["required_evidence_omitted"] is True
    assert "overdue_deferred_spawn_requests" in manifest["incomplete_reasons"]


@pytest.mark.asyncio
async def test_packet_and_artifact_authority_are_cycle_isolated(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    artifact = Artifact(
        task_id=source.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        storage_uri=f"gs://test/{source.id}/findings.json",
        logs='[{"finding":"supported"}]',
    )
    db_session.add(artifact)

    _, other_cycle, other_source = await _cycle_with_source(
        db_session, project=project
    )
    other_artifact = Artifact(
        task_id=other_source.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        storage_uri=f"gs://test/{other_source.id}/other.json",
        logs='[{"finding":"must not leak"}]',
    )
    db_session.add(other_artifact)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    packet = review.first_payload["evidence_packet"]
    source_ids = {entry.get("source_id") for entry in packet}
    authorized = {
        pointer["artifact_id"]
        for pointer in review.context["artifact_prerequisites"]
    }

    assert str(source.id) in source_ids
    assert str(artifact.id) in source_ids
    assert str(other_source.id) not in source_ids
    assert str(other_artifact.id) not in source_ids
    assert authorized == {str(artifact.id)}
    assert len(review.first_payload["packet_checksum"]) == 64
    assert (
        await db_session.execute(
            select(DiscoveryEvidenceItem).where(
                DiscoveryEvidenceItem.cycle_id == cycle.id
            )
        )
    ).scalars().all()
    assert other_cycle.review_task_id is None


@pytest.mark.asyncio
async def test_packet_limits_preserve_source_and_outcome_and_restrict_verdict(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(discovery_review, "DISCOVERY_REVIEW_MAX_TASKS", 2)
    monkeypatch.setattr(discovery_review, "DISCOVERY_REVIEW_MAX_ARTIFACTS", 1)
    project, cycle, source = await _cycle_with_source(db_session)
    extra_completed = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Completed outcome",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    failed = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Failed outcome",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(extra_completed)
    db_session.add(failed)
    await db_session.flush()
    for logical_key in ("first", "second"):
        db_session.add(
            Artifact(
                task_id=source.id,
                artifact_type=ArtifactType.RESEARCH_FINDINGS,
                logical_key=logical_key,
                storage_uri=f"gs://test/{source.id}/{logical_key}.json",
                logs=f'{{"artifact":"{logical_key}"}}',
            )
        )
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    task_source_ids = {
        entry["source_id"]
        for entry in review.first_payload["evidence_packet"]
        if entry.get("source_kind") == "task"
    }

    assert str(source.id) in task_source_ids
    assert str(failed.id) in task_source_ids
    assert str(extra_completed.id) not in task_source_ids
    assert manifest["tasks"] == {"total": 3, "included": 2, "omitted": 1}
    assert manifest["artifacts"] == {"total": 2, "included": 1, "omitted": 1}
    assert manifest["required_evidence_omitted"] is True
    assert manifest["incomplete"] is True
    assert {"task_limit", "artifact_limit"} <= set(
        manifest["incomplete_reasons"]
    )

    evidence_ids = [
        entry["evidence_id"]
        for entry in review.first_payload["evidence_packet"]
        if entry.get("evidence_id")
    ]
    with pytest.raises(
        DiscoveryReviewResultError,
        match="incomplete packet permits only",
    ):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_valid_result(cycle, review, evidence_ids),
        )
    assert cycle.state == "review_blocked"


@pytest.mark.asyncio
async def test_incomplete_limited_packet_allows_inconclusive(db_session, monkeypatch):
    monkeypatch.setattr(discovery_review, "DISCOVERY_REVIEW_MAX_TASKS", 1)
    _, cycle, source = await _cycle_with_source(db_session)
    omitted = Task(
        project_id=source.project_id,
        discovery_cycle_id=cycle.id,
        parent_task_id=source.id,
        title="Omitted outcome",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(omitted)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = [
        entry["evidence_id"]
        for entry in review.first_payload["evidence_packet"]
        if entry.get("evidence_id")
    ]
    result = _valid_result(
        cycle,
        review,
        evidence_ids,
        verdict="inconclusive",
    )

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=result,
    )

    assert cycle.state == "reviewed"
    assert cycle.verdict == "inconclusive"


@pytest.mark.asyncio
async def test_review_control_task_is_authorized_without_descendant_charge(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)

    decision = await authorize_discovery_task_execution(
        db_session,
        task=review,
        mode="enforced",
    )

    assert decision.allowed is True
    assert decision.reason == "discovery_review_control_task"
    assert decision.details == {"charged": False}
    assert cycle.consumed_task_credits == 0


@pytest.mark.asyncio
async def test_valid_result_transitions_and_notifies_without_resident(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = [
        row
        for row in (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    ]

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=json.dumps(_valid_result(cycle, review, evidence_ids)),
    )
    await db_session.flush()

    assert cycle.state == "reviewed"
    assert cycle.verdict == "renew"
    successor = (
        await db_session.execute(
            select(DiscoveryCycle).where(
                DiscoveryCycle.project_id == cycle.project_id,
                DiscoveryCycle.direction_key == cycle.direction_key,
                DiscoveryCycle.generation == cycle.generation + 1,
            )
        )
    ).scalar_one()
    assert successor.state == "open"
    assert successor.source_research_task_id is None
    assert successor.phase == "demonstrate"
    assert successor.bet_snapshot == {"hypothesis": "repeat against a harder case"}
    assert successor.task_credit_limit == 3
    message = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == cycle.project_id,
                ProjectMessage.from_task_id == review.id,
            )
        )
    ).scalar_one()
    assert message.status == "open"
    assert message.context["event_type"] == "discovery.review_completed"
    assert message.context["cycle_id"] == str(cycle.id)
    assert message.context["next_cycle_id"] == str(successor.id)
    assert message.context["review_result"]["next_bet"] == successor.bet_snapshot


@pytest.mark.asyncio
async def test_consequential_verdict_requires_matching_independent_review(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    first_review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )

    await complete_discovery_review(
        db_session,
        task=first_review,
        raw_result=_valid_result(
            cycle,
            first_review,
            evidence_ids,
            verdict="pivot",
            next_bet={"hypothesis": "try the paired constructor mechanism"},
        ),
    )
    confirmation = await db_session.get(Task, cycle.review_task_id)

    assert confirmation.id != first_review.id
    assert confirmation.first_payload["review_round"] == 2
    assert confirmation.first_payload["prior_review_task_id"] == str(first_review.id)
    assert confirmation.first_payload["confirmation_proposal"]["verdict"] == "pivot"
    assert cycle.state == "reviewing"
    assert cycle.verdict is None

    await complete_discovery_review(
        db_session,
        task=confirmation,
        raw_result=_valid_result(
            cycle,
            confirmation,
            evidence_ids,
            verdict="pivot",
            next_bet={"hypothesis": "try the paired constructor mechanism"},
        ),
    )

    assert cycle.state == "reviewed"
    assert cycle.verdict == "pivot"
    successor = (
        await db_session.execute(
            select(DiscoveryCycle).where(
                DiscoveryCycle.project_id == cycle.project_id,
                DiscoveryCycle.direction_key == cycle.direction_key,
                DiscoveryCycle.generation == 2,
            )
        )
    ).scalar_one()
    assert successor.bet_snapshot == {
        "hypothesis": "try the paired constructor mechanism"
    }


@pytest.mark.asyncio
async def test_disagreeing_confirmation_routes_to_operator_without_continuing(
    db_session,
):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    first_review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    await complete_discovery_review(
        db_session,
        task=first_review,
        raw_result=_valid_result(
            cycle,
            first_review,
            evidence_ids,
            verdict="expand",
        ),
    )
    confirmation = await db_session.get(Task, cycle.review_task_id)

    await complete_discovery_review(
        db_session,
        task=confirmation,
        raw_result=_valid_result(
            cycle,
            confirmation,
            evidence_ids,
            verdict="pivot",
        ),
    )
    await db_session.flush()

    assert cycle.state == "review_blocked"
    assert cycle.verdict == "ask_operator"
    assert cycle.review_result["error"] == "independent reviews disagree"
    message = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.from_task_id == confirmation.id
            )
        )
    ).scalar_one()
    assert message.context["event_type"] == "discovery.operator_question"


@pytest.mark.asyncio
async def test_generation_cap_prevents_unbounded_autonomous_renewal(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    cycle.generation = discovery_review.MAX_AUTONOMOUS_DISCOVERY_GENERATIONS
    await db_session.flush()
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_valid_result(cycle, review, evidence_ids),
    )
    await db_session.flush()

    successors = (
        await db_session.execute(
            select(DiscoveryCycle).where(
                DiscoveryCycle.project_id == cycle.project_id,
                DiscoveryCycle.direction_key == cycle.direction_key,
                DiscoveryCycle.generation > cycle.generation,
            )
        )
    ).scalars().all()
    assert successors == []
    message = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
        )
    ).scalar_one()
    assert message.context["event_type"] == "discovery.operator_question"
    assert (
        message.context["policy_status"]
        == "autonomous_generation_cap_reached"
    )


@pytest.mark.asyncio
async def test_stop_verdict_is_operator_visible(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_valid_result(
            cycle,
            review,
            evidence_ids,
            verdict="stop",
            next_bet=None,
        ),
    )
    await db_session.flush()

    message = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.from_task_id == review.id)
        )
    ).scalar_one()
    assert cycle.state == "reviewed"
    assert message.context["event_type"] == "discovery.operator_question"
    assert message.context["policy_status"] == "operator_notified"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["reviewed", "review_blocked"])
async def test_operator_can_resume_halted_cycle_without_resident(
    db_session,
    source_state,
):
    project, cycle, _ = await _cycle_with_source(
        db_session,
        state=source_state,
    )
    cycle.verdict = "ask_operator"
    await db_session.flush()

    successor = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        action="continue",
        next_bet={"hypothesis": "operator-selected bounded alternative"},
        note="Use the paired construction path.",
    )

    assert successor.state == "open"
    assert successor.generation == cycle.generation + 1
    assert successor.bet_snapshot == {
        "hypothesis": "operator-selected bounded alternative"
    }
    assert cycle.state == "superseded"
    assert cycle.review_result["operator_resolution"]["action"] == "continue"

    replay = await resolve_discovery_cycle(
        db_session,
        project_id=project.id,
        direction_key=cycle.direction_key,
        action="continue",
        next_bet={"hypothesis": "operator-selected bounded alternative"},
        note="Repeated request.",
    )
    assert replay.id == successor.id


@pytest.mark.asyncio
async def test_corrupt_confirmation_state_fails_closed(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    first_review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    await complete_discovery_review(
        db_session,
        task=first_review,
        raw_result=_valid_result(
            cycle,
            first_review,
            evidence_ids,
            verdict="pivot",
        ),
    )
    confirmation = await db_session.get(Task, cycle.review_task_id)
    cycle.review_result = {}
    await db_session.flush()

    with pytest.raises(
        DiscoveryReviewResultError,
        match="no durable first-review",
    ):
        await complete_discovery_review(
            db_session,
            task=confirmation,
            raw_result=_valid_result(
                cycle,
                confirmation,
                evidence_ids,
                verdict="pivot",
            ),
        )

    assert cycle.state == "review_blocked"


@pytest.mark.asyncio
async def test_stale_revision_fails_closed_as_review_blocked(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    stale = _valid_result(
        cycle,
        review,
        evidence_ids,
        reviewed_revision=review.first_payload["expected_revision"] - 1,
    )

    with pytest.raises(DiscoveryReviewResultError, match="reviewed_revision"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=stale,
        )

    assert cycle.state == "review_blocked"
    assert cycle.verdict is None
    assert "reviewed_revision" in cycle.review_result["error"]


@pytest.mark.asyncio
async def test_server_owned_candidate_sha_mismatch_fails_closed(db_session):
    _, cycle, _ = await _cycle_with_source(db_session)
    cycle.candidate_sha = "a" * 40
    db_session.add(cycle)
    await db_session.flush()
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    evidence_ids = list(
        (
            await db_session.execute(
                select(DiscoveryEvidenceItem.id).where(
                    DiscoveryEvidenceItem.cycle_id == cycle.id
                )
            )
        )
        .scalars()
        .all()
    )
    tampered = dict(review.first_payload)
    tampered["candidate_sha"] = "b" * 40
    review.payloads = [tampered]
    db_session.add(review)
    await db_session.flush()

    with pytest.raises(DiscoveryReviewResultError, match="candidate_sha"):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result=_valid_result(cycle, review, evidence_ids),
        )

    assert cycle.state == "review_blocked"
    assert cycle.verdict is None


@pytest.mark.asyncio
async def test_sweep_closes_expired_cycle_and_repairs_missed_trigger(db_session):
    _, cycle, _ = await _cycle_with_source(
        db_session,
        deadline_at=_utcnow() - timedelta(minutes=1),
    )

    changed = await sweep_discovery_reviews(db_session, _utcnow())

    assert changed == 1
    await db_session.refresh(cycle)
    assert cycle.state == "reviewing"
    assert cycle.review_task_id is not None
