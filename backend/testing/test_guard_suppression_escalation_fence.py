from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import ModuleFindingChain, Project, ProjectMessage, Task
from app.module_finding_chain import (
    CHAIN_ESCALATION_THRESHOLD,
    _guard_chain_key,
    _post_guard_suppression_escalation,
    resolve_landed_guard_suppression_remediation,
)
from app.time_utils import _utcnow


def _mature_chain(project_id: uuid.UUID) -> ModuleFindingChain:
    return ModuleFindingChain(
        project_id=project_id,
        finding_key=_guard_chain_key(
            "backend-pytest",
            "backend/testing/test_x.py::test_x::AssertionError",
        ),
        file_path="guard:backend-pytest",
        summary="Recurring baseline guard suppression",
        chain_kind="guard_suppression",
        suppression_lane="backend-pytest",
        failure_identity="backend/testing/test_x.py::test_x::AssertionError",
        source_task_ids=[],
        pr_numbers=[],
        first_seen_pr=0,
        last_seen_pr=0,
        occurrences=CHAIN_ESCALATION_THRESHOLD,
        verified_occurrences=CHAIN_ESCALATION_THRESHOLD,
        status="open",
    )


def _authority_snapshot(chain: ModuleFindingChain) -> ModuleFindingChain:
    """Detached state held by a caller that read the chain before a winner."""
    return ModuleFindingChain(
        id=chain.id,
        project_id=chain.project_id,
        finding_key=chain.finding_key,
        file_path=chain.file_path,
        summary=chain.summary,
        chain_kind=chain.chain_kind,
        suppression_lane=chain.suppression_lane,
        failure_identity=chain.failure_identity,
        source_task_ids=list(chain.source_task_ids or []),
        pr_numbers=list(chain.pr_numbers or []),
        first_seen_pr=chain.first_seen_pr,
        last_seen_pr=chain.last_seen_pr,
        occurrences=chain.occurrences,
        verified_occurrences=chain.verified_occurrences,
        status=chain.status,
        remediation_task_id=chain.remediation_task_id,
        escalation_version=chain.escalation_version,
        escalation_state_fingerprint=chain.escalation_state_fingerprint,
    )


def _passing_gate_receipt() -> dict:
    return {
        "status": "passed",
        "command": "./check test",
        "timeout_seconds": 600,
        "hard_timeout_seconds": 1800,
        "elapsed_seconds": 5,
        "deadline_extended": False,
        "exit_code": 0,
        "authoritative_ci_required": False,
        "recorded_at": _utcnow().isoformat(),
    }


async def _alerts(db_session, project_id: uuid.UUID) -> list[ProjectMessage]:
    return list(
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.context["event_type"].as_string()
                    == "remediation_escalated",
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_clean_owner_resolves_every_published_escalation_version(db_session):
    project = Project(name="guard resolution versions", user_id="test-user-123")
    chain = _mature_chain(project.id)
    first_owner = Task(project_id=project.id, title="first owner", payloads=[])
    clean_owner = Task(
        project_id=project.id,
        title="clean owner",
        payloads=[],
        context={
            "publication_gate_receipt": _passing_gate_receipt(),
            "guard_suppression_remediation_keys": [chain.finding_key],
        },
    )
    db_session.add_all([project, chain, first_owner, clean_owner])
    await db_session.commit()

    assert await _post_guard_suppression_escalation(
        db_session, chain, first_owner
    )
    assert await _post_guard_suppression_escalation(
        db_session, chain, clean_owner
    )
    await db_session.commit()
    assert len(await _alerts(db_session, project.id)) == 2

    resolved = await resolve_landed_guard_suppression_remediation(
        clean_owner, db_session
    )
    await db_session.commit()

    assert [row.id for row in resolved] == [chain.id]
    alerts = await _alerts(db_session, project.id)
    assert alerts
    assert all(alert.status == "resolved" for alert in alerts)
    assert any(
        alert.resolution == "guard_suppression_remediated" for alert in alerts
    )


@pytest.mark.asyncio
async def test_concurrent_distinct_versions_have_one_fenced_winner(db_session):
    """Two callers that read version zero cannot both publish distinct states."""
    project = Project(name="guard concurrent versions", user_id="test-user-123")
    chain = _mature_chain(project.id)
    first_owner = Task(project_id=project.id, title="first owner", payloads=[])
    losing_owner = Task(project_id=project.id, title="losing owner", payloads=[])
    db_session.add_all([project, chain, first_owner, losing_owner])
    await db_session.commit()

    # Both transactions read the same authority generation before either acts.
    # Committing the winner first is a deterministic interleaving of the race;
    # the loser's stale snapshot must fail the persisted version fence.
    async with AsyncSessionLocal() as winner, AsyncSessionLocal() as loser:
        winner_chain = await winner.get(ModuleFindingChain, chain.id)
        loser_chain = await loser.get(ModuleFindingChain, chain.id)
        winner_owner = await winner.get(Task, first_owner.id)
        loser_owner = await loser.get(Task, losing_owner.id)
        assert winner_chain is not None and loser_chain is not None
        assert winner_owner is not None and loser_owner is not None
        assert winner_chain.escalation_version == loser_chain.escalation_version == 0

        assert await _post_guard_suppression_escalation(
            winner, winner_chain, winner_owner
        )
        await winner.commit()
        assert not await _post_guard_suppression_escalation(
            loser, loser_chain, loser_owner
        )
        await loser.commit()

    await db_session.refresh(chain)
    alerts = await _alerts(db_session, project.id)
    assert chain.escalation_version == 1
    assert chain.remediation_task_id == first_owner.id
    assert len(alerts) == 1
    assert alerts[0].status == "open"
    assert alerts[0].context["remediation_task_id"] == str(first_owner.id)


@pytest.mark.asyncio
async def test_stale_replay_cannot_resurrect_superseded_owner(db_session):
    project = Project(name="guard stale replay", user_id="test-user-123")
    chain = _mature_chain(project.id)
    first_owner = Task(project_id=project.id, title="first owner", payloads=[])
    current_owner = Task(project_id=project.id, title="current owner", payloads=[])
    db_session.add_all([project, chain, first_owner, current_owner])
    await db_session.commit()

    assert await _post_guard_suppression_escalation(
        db_session, chain, first_owner
    )
    await db_session.commit()
    stale_replay = _authority_snapshot(chain)

    assert await _post_guard_suppression_escalation(
        db_session,
        chain,
        None,
        unowned_reason="the first owner failed; awaiting replacement",
    )
    assert await _post_guard_suppression_escalation(
        db_session, chain, current_owner
    )
    await db_session.commit()

    assert not await _post_guard_suppression_escalation(
        db_session, stale_replay, first_owner
    )
    await db_session.commit()

    await db_session.refresh(chain)
    alerts = await _alerts(db_session, project.id)
    open_alerts = [alert for alert in alerts if alert.status == "open"]
    assert chain.escalation_version == 3
    assert chain.remediation_task_id == current_owner.id
    assert len(alerts) == 3
    assert len(open_alerts) == 1
    assert open_alerts[0].context["remediation_task_id"] == str(current_owner.id)
