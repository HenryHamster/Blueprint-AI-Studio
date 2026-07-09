"""Tests for strategic_context_package.py read-model functions."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.models import KnowledgeDocument, Project, Task
from app.strategic_context_package import load_strategic_context_evidence

TEST_USER_ID = "test-user-123"


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_pending_count_is_real_aggregate(db_session):
    """pending_count must reflect ALL pending docs, not just the top-4 sample slice."""
    project = Project(name="KdTest", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create 6 pending documents — more than the .limit(4) in the items query,
    # so the OLD code would have reported at most 4 instead of 6.
    for i in range(6):
        db_session.add(
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=task.id,
                title=f"Pending doc {i}",
                summary=f"Summary {i}",
                body=f"Body {i}",
                lifecycle_status="pending",
            )
        )
    # Also add 2 consumed and 1 dismissed doc that should NOT count as pending.
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Consumed doc",
            summary="Consumed",
            body="Body consumed",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Consumed doc 2",
            summary="Consumed 2",
            body="Body consumed 2",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Dismissed doc",
            summary="Dismissed",
            body="Body dismissed",
            lifecycle_status="dismissed",
        )
    )
    await db_session.commit()

    # Verify total document count
    total_in_db = (
        await db_session.execute(
            select(func.count()).where(KnowledgeDocument.project_id == project.id)
        )
    ).scalar()
    assert total_in_db == 9  # 6 pending + 2 consumed + 1 dismissed

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    # The corrected pending_count must be 6 (all pending docs), not capped at 4.
    assert knowledge["pending_count"] == 6, (
        f"pending_count should reflect all pending docs (6), got {knowledge['pending_count']}"
    )

    # Total must be 9
    assert knowledge["total"] == 9

    # Items list is still a preview (top-N limited sample), showing only pending docs.
    # Even though 6 docs are pending, only up to 4 appear in the items list.
    assert len(knowledge["items"]) <= 4
    for item in knowledge["items"]:
        assert item["lifecycle_status"] == "pending", (
            f"Non-pending doc in items: {item.get('id')}"
        )

    # Staleness metric must be present and non-None since there are pending docs.
    assert knowledge["oldest_pending_age_hours"] is not None
    assert knowledge["oldest_pending_age_hours"] >= 0


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_no_pending_docs(db_session):
    """When no pending docs exist, pending_count is 0 and staleness is None."""
    project = Project(name="KdTestEmpty", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Only consumed / dismissed docs — no pending.
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Done",
            summary="Done",
            body="Body",
            lifecycle_status="consumed",
        )
    )
    db_session.add(
        KnowledgeDocument(
            project_id=project.id,
            source_task_id=task.id,
            title="Dismissed",
            summary="Dismissed",
            body="Body",
            lifecycle_status="dismissed",
        )
    )
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    assert knowledge["pending_count"] == 0
    assert knowledge["total"] == 2
    assert knowledge["items"] == []
    assert knowledge["oldest_pending_age_hours"] is None


@pytest.mark.asyncio
async def test_load_strategic_context_evidence_staleness_reflects_oldest_pending(db_session):
    """oldest_pending_age_hours should reflect the age of the earliest-created pending doc."""
    project = Project(name="KdTestStale", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="source",
        status="completed",
        payloads=[{"type": "research", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    now = datetime.utcnow()
    # Create a doc from 72 hours ago — should be the oldest.
    old_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title="Old pending",
        summary="Very old",
        body="Body old",
        lifecycle_status="pending",
        created_at=now - timedelta(hours=72),
        updated_at=now - timedelta(hours=72),
    )
    db_session.add(old_doc)
    # Recent doc — created just now.
    new_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        title="Recent pending",
        summary="Recent",
        body="Body recent",
        lifecycle_status="pending",
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    db_session.add(new_doc)
    await db_session.commit()

    result = await load_strategic_context_evidence(db_session, project.id)
    knowledge = result["knowledge"]

    assert knowledge["pending_count"] == 2
    assert knowledge["oldest_pending_age_hours"] is not None
    # The oldest doc was created ~72 hours ago, so the staleness should be near 72.
    assert knowledge["oldest_pending_age_hours"] >= 70
    assert knowledge["oldest_pending_age_hours"] <= 74
