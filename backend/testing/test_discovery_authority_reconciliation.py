"""Regression contracts for discovery direction-authority reconciliation."""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from app.discovery_admission import ensure_discovery_cycle_for_research
from app.discovery_review import (
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
    sweep_discovery_reviews,
)
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import (
    DiscoveryCycle,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)


async def _cycle_with_source(
    db_session,
    *,
    state: str = "open",
) -> tuple[Project, DiscoveryCycle, Task]:
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
        name=f"discovery-authority-{uuid.uuid4().hex[:8]}",
        user_id="discovery-authority-test",
        directions=[direction],
    )
    db_session.add(project)
    await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=direction_key,
        generation=1,
        outcome_kind="mixed",
        state=state,
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
        status=TaskStatus.COMPLETED,
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


def _advance_result(cycle: DiscoveryCycle, review: Task) -> dict:
    evidence_citations = [
        str(item["evidence_id"])
        for item in review.first_payload["evidence_packet"]
        if item.get("evidence_id")
    ]
    return {
        "cycle_id": str(cycle.id),
        "reviewed_revision": review.first_payload["expected_revision"],
        "action": "advance",
        "summary": "Evidence supports advance.",
        "evidence_citations": evidence_citations,
        "next_bet": {
            "mode": "build",
            "claim": "Try the bounded mechanism",
            "decision_question": "Does the mechanism improve the target outcome?",
            "success_evidence": "A measured probe improves the target outcome.",
            "retirement_condition": "Retire after two probes without improvement.",
        },
        "park_reason": None,
        "resume_condition": None,
        "operator_question": None,
    }


@pytest.mark.asyncio
async def test_d_121_mutable_research_evidence_cannot_self_invalidate_authority(
    db_session,
):
    """d-121: normal evidence accumulation is not a direction-authority edit."""

    project, cycle, _ = await _cycle_with_source(db_session)
    frozen_direction = {
        **project.directions[0],
        "id": "d-121",
        "research_context": "Evidence before the commissioned research parent.",
    }
    project.directions = [frozen_direction]
    cycle.direction_key = "d-121"
    cycle.bet_snapshot = {
        **dict(cycle.bet_snapshot or {}),
        "direction": frozen_direction,
    }
    db_session.add(project)
    db_session.add(cycle)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    project.directions = [
        {
            **frozen_direction,
            "research_context": (
                "Evidence rebuilt by clean research completion, including new "
                "findings and receipts."
            ),
            "recent_findings_evidence": [{"finding": "measured"}],
        }
    ]
    db_session.add(project)
    await db_session.flush()

    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_advance_result(cycle, review),
    )

    assert cycle.state == "reviewed"
    assert cycle.verdict == "advance"
    assert cycle.review_result["action"] == "advance"
    assert cycle.review_result["next_cycle_id"]


@pytest.mark.asyncio
async def test_false_authority_block_resumes_within_bounded_review_budget(db_session):
    project, cycle, source = await _cycle_with_source(db_session)
    frozen_direction = {
        **project.directions[0],
        "id": "d-121",
        "research_context": "old evidence",
    }
    project.directions = [
        {**frozen_direction, "research_context": "new accumulated evidence"}
    ]
    cycle.direction_key = "d-121"
    cycle.state = "review_blocked"
    cycle.bet_snapshot = {
        **dict(cycle.bet_snapshot or {}),
        "direction": frozen_direction,
    }
    stale_review = Task(
        project_id=project.id,
        discovery_cycle_id=cycle.id,
        title="Stale d-121 decision",
        payloads=[{"type": "discovery_review"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(stale_review)
    await db_session.flush()
    cycle.review_task_id = stale_review.id
    cycle.review_result = {
        "status": "DECISION_FAILED",
        "error": "direction authority changed before advance",
    }
    db_session.add(project)
    db_session.add(cycle)
    await db_session.flush()

    changed = await sweep_discovery_reviews(db_session, limit=10)
    await db_session.refresh(cycle)

    assert changed == 1
    assert cycle.state == "reviewing"
    assert cycle.review_task_id != stale_review.id
    messages = list(
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        message.context.get("event_type") == "discovery.authority_reconciled"
        for message in messages
    )
    assert source.id == cycle.source_research_task_id


@pytest.mark.asyncio
async def test_false_authority_reconciliation_budget_fails_closed(db_session):
    project, cycle, _ = await _cycle_with_source(db_session)
    cycle.state = "review_blocked"
    cycle.review_result = {
        "status": "DECISION_FAILED",
        "error": "direction authority changed before advance",
    }
    attempts = [
        Task(
            project_id=project.id,
            discovery_cycle_id=cycle.id,
            title=f"Prior decision {attempt}",
            payloads=[{"type": "discovery_review"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
        )
        for attempt in range(3)
    ]
    db_session.add_all(attempts)
    await db_session.flush()
    cycle.review_task_id = attempts[-1].id
    db_session.add(cycle)
    await db_session.flush()

    result = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert result.reason == "authority_reconciliation_budget_exhausted"
    assert result.spawned is False
    assert cycle.state == "review_blocked"
    assert cycle.review_result["reconciliation_attempts"] == 3
    assert cycle.review_result["next_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "verdict"),
    [("review_pending", None), ("review_blocked", None), ("reviewed", "ask_operator")],
)
async def test_true_authority_change_supersedes_then_rebases_one_live_cycle(
    db_session,
    state,
    verdict,
):
    project, cycle, _ = await _cycle_with_source(db_session, state=state)
    cycle.verdict = verdict
    if state == "review_blocked":
        cycle.review_result = {
            "status": "DECISION_FAILED",
            "error": "direction authority changed before advance",
        }
    project.directions = [
        {
            **project.directions[0],
            "text": "A genuinely new direction commission",
        }
    ]
    db_session.add(project)
    db_session.add(cycle)
    await db_session.flush()

    reconciled = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert reconciled.reason == "authority_superseded"
    assert cycle.state == "superseded"
    assert cycle.review_result["status"] == "AUTHORITY_SUPERSEDED"
    successor = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=cycle.direction_key,
        direction=project.directions[0],
    )
    assert successor.generation == cycle.generation + 1
    assert successor.state == "open"
    assert successor.bet_snapshot["direction"]["text"] == (
        "A genuinely new direction commission"
    )
    live = list(
        (
            await db_session.execute(
                select(DiscoveryCycle).where(
                    DiscoveryCycle.project_id == project.id,
                    DiscoveryCycle.direction_key == cycle.direction_key,
                    DiscoveryCycle.state.in_(
                        {
                            "open",
                            "admission_closed",
                            "review_pending",
                            "reviewing",
                            "review_blocked",
                        }
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert [item.id for item in live] == [successor.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["park", "stop"])
async def test_sweep_revisits_policy_blocking_verdict_after_authority_edit(
    db_session,
    verdict,
):
    """The shared admission promise has a matching bounded sweep transition."""

    project, cycle, _ = await _cycle_with_source(db_session, state="reviewed")
    cycle.verdict = verdict
    project.directions = [
        {
            **project.directions[0],
            "text": f"Reactivated after stale {verdict} disposition",
        }
    ]
    db_session.add(project)
    db_session.add(cycle)
    await db_session.flush()

    changed = await sweep_discovery_reviews(db_session, limit=10)
    await db_session.refresh(cycle)

    assert changed == 1
    assert cycle.state == "superseded"
    assert cycle.review_result["status"] == "AUTHORITY_SUPERSEDED"
    assert cycle.review_result["superseded_verdict"] == verdict
