"""Sequencing regressions at the research-to-discovery decision boundary."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.discovery_review import (
    DiscoveryReviewResultError,
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
    sweep_discovery_reviews,
)
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import DiscoveryCycle, Project, Task, TaskStatus


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


async def _cycle_with_source(
    db_session,
    *,
    source_status: TaskStatus = TaskStatus.COMPLETED,
    finalized: bool = True,
    plan_output=None,
) -> tuple[DiscoveryCycle, Task]:
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
        name=f"discovery-sequencing-{uuid.uuid4().hex[:8]}",
        user_id="discovery-sequencing-test",
        directions=[direction],
    )
    db_session.add(project)
    await db_session.flush()
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=direction_key,
        generation=1,
        outcome_kind="mixed",
        state="open",
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
        plan_output=[] if plan_output is None else plan_output,
        context={FINALIZED_RESEARCH_FINDINGS_KEY: True} if finalized else {},
    )
    db_session.add(source)
    await db_session.flush()
    cycle.source_research_task_id = source.id
    db_session.add(cycle)
    await db_session.flush()
    return cycle, source


def _research_findings(review: Task) -> list[dict]:
    outcome = next(
        item
        for item in review.first_payload["evidence_packet"]
        if item["semantic_role"] == "parent_outcome"
    )
    return outcome["evidence_metadata"]["snapshot"]["research_findings"]


@pytest.mark.asyncio
async def test_completed_source_waits_for_finalized_research_receipt(db_session):
    cycle, source = await _cycle_with_source(
        db_session,
        finalized=False,
        plan_output=[{"title": "Accepted only after quality finalization"}],
    )

    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)

    assert waiting.spawned is False
    assert waiting.reason == "source_research_findings_pending"
    assert cycle.review_task_id is None

    source.context = {FINALIZED_RESEARCH_FINDINGS_KEY: True}
    db_session.add(source)
    await db_session.flush()

    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    manifest = review.first_payload["evidence_packet_manifest"]
    assert manifest["version"] == 3
    assert manifest["source_research_findings_finalized"] is True
    assert _research_findings(review)[0]["finding"]["title"] == (
        "Accepted only after quality finalization"
    )


@pytest.mark.asyncio
async def test_finalized_zero_findings_remains_sweep_live(db_session):
    cycle, source = await _cycle_with_source(
        db_session,
        finalized=False,
        plan_output=[],
    )
    waiting = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert waiting.reason == "source_research_findings_pending"

    source.context = {FINALIZED_RESEARCH_FINDINGS_KEY: True}
    db_session.add(source)
    await db_session.flush()

    assert await sweep_discovery_reviews(db_session) == 1
    assert cycle.review_task_id is not None
    review = await db_session.get(Task, cycle.review_task_id)
    assert _research_findings(review) == []


@pytest.mark.skipif(not USE_POSTGRES, reason="race regression requires PostgreSQL")
@pytest.mark.asyncio
async def test_terminal_visibility_cannot_publish_review_before_finalization(
    db_session,
):
    """A terminal callback commit cannot outrun findings finalization."""

    cycle, source = await _cycle_with_source(
        db_session,
        source_status=TaskStatus.IN_PROGRESS,
        finalized=False,
        plan_output=[{"title": "Finding visible before finalization"}],
    )
    cycle_id = cycle.id
    source_id = source.id
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    terminal_visible = asyncio.Event()
    review_checked = asyncio.Event()

    async def _completion_flow() -> None:
        async with Session() as session:
            terminal_source = (
                await session.execute(
                    select(Task).where(Task.id == source_id).with_for_update()
                )
            ).scalar_one()
            terminal_source.status = TaskStatus.COMPLETED
            terminal_source.post_commit_pending = False
            session.add(terminal_source)
            await session.commit()
            terminal_visible.set()

            await asyncio.wait_for(review_checked.wait(), timeout=5)
            finalized_source = (
                await session.execute(
                    select(Task)
                    .where(Task.id == source_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            finalized_source.context = {FINALIZED_RESEARCH_FINDINGS_KEY: True}
            session.add(finalized_source)
            await session.commit()

    async def _review_flow():
        await asyncio.wait_for(terminal_visible.wait(), timeout=5)
        try:
            async with Session() as session:
                result = await maybe_close_and_spawn_discovery_review(
                    session, cycle_id
                )
                await session.commit()
                return result
        finally:
            review_checked.set()

    _, waiting = await asyncio.wait_for(
        asyncio.gather(_completion_flow(), _review_flow()), timeout=10
    )

    assert waiting.spawned is False
    assert waiting.reason == "source_research_findings_pending"

    # Keep the main-session Task identity stale on purpose. The guard's
    # populate_existing read must observe the separately committed marker.
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle_id)
    assert spawned.spawned is True
    review = await db_session.get(Task, spawned.task_id)
    assert _research_findings(review)[0]["finding"]["title"] == (
        "Finding visible before finalization"
    )


@pytest.mark.asyncio
async def test_packet_checksum_is_revalidated_without_retry_churn(db_session):
    cycle, source = await _cycle_with_source(db_session)
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    original_checksum = review.first_payload["packet_checksum"]

    review.status = TaskStatus.COMPLETED
    source.plan_output = [{"title": "Durably finalized after packet authoring"}]
    db_session.add_all([review, source])
    await db_session.flush()

    invalidated = await complete_discovery_review(
        db_session,
        task=review,
        raw_result={},
    )
    assert invalidated.id == cycle.id
    assert cycle.state == "review_pending"
    assert cycle.review_task_id is None
    assert cycle.review_result == {
        "status": "REVIEW_PACKET_INVALIDATED",
        "reason": "review_packet_stale",
        "stale_review_task_id": str(review.id),
    }

    invalidated_revision = cycle.revision
    invalidated_result = dict(cycle.review_result)
    with pytest.raises(
        DiscoveryReviewResultError, match="review task is stale for this cycle"
    ):
        await complete_discovery_review(
            db_session,
            task=review,
            raw_result={},
        )
    assert cycle.revision == invalidated_revision
    assert cycle.review_result == invalidated_result

    replacement = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    assert replacement.spawned is True
    assert replacement.task_id != review.id
    replacement_review = await db_session.get(Task, replacement.task_id)
    assert replacement_review.first_payload["packet_checksum"] != original_checksum
    assert _research_findings(replacement_review)[0]["finding"]["title"] == (
        "Durably finalized after packet authoring"
    )
