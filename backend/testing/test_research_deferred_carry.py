"""Security regressions for durable research finding carry acknowledgements."""

from __future__ import annotations

import pytest

from app.models import Project, Task, TaskStatus
from app.research_deferred_carry import (
    DEFERRED_FINDING_CONSUMPTION_ACKS_KEY,
    acknowledge_consumed_deferred_findings,
    unacknowledged_deferred_findings,
)


@pytest.mark.asyncio
async def test_deferred_ack_cannot_prune_a_foreign_project_producer(db_session):
    """A forged carry envelope cannot acknowledge another tenant's output."""
    producer_project = Project(name="Deferred producer tenant", user_id="producer")
    consumer_project = Project(name="Deferred consumer tenant", user_id="consumer")
    db_session.add_all([producer_project, consumer_project])
    await db_session.flush()

    finding = {
        "type": "improvement",
        "title": "Foreign durable finding",
        "description": "This finding belongs only to the producer tenant.",
    }
    producer = Task(
        project_id=producer_project.id,
        title="Foreign producer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={"deferred_findings": [finding]},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    consumer = Task(
        project_id=consumer_project.id,
        title="Unrelated consumer",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add_all([producer, consumer])
    await db_session.commit()
    await db_session.refresh(producer)
    await db_session.refresh(consumer)

    forged_foreign_carry = unacknowledged_deferred_findings(producer)
    assert forged_foreign_carry

    stamped = await acknowledge_consumed_deferred_findings(
        db_session,
        consumer_task=consumer,
        carried_findings=forged_foreign_carry,
    )
    await db_session.commit()
    await db_session.refresh(producer)

    assert stamped == 0
    assert producer.context["deferred_findings"] == [finding]
    assert DEFERRED_FINDING_CONSUMPTION_ACKS_KEY not in producer.context
