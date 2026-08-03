"""Persistence contracts for bounded discovery-review cycles.

These tests intentionally exercise both ORM defaults and database constraints.
The latter keep discovery provenance and review state authoritative across
backend workers.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    DiscoveryCycle,
    DiscoveryEvidenceItem,
    Project,
    StrategicSpawnRequest,
    Task,
)


def _project() -> Project:
    return Project(
        name=f"discovery-{uuid.uuid4().hex[:8]}",
        user_id="discovery-model-test-user",
    )


def _cycle(
    project_id: uuid.UUID,
    *,
    direction_key: str = "d-discovery",
    generation: int = 1,
    **overrides,
) -> DiscoveryCycle:
    values = {
        "project_id": project_id,
        "direction_key": direction_key,
        "generation": generation,
        "outcome_kind": "mixed",
    }
    values.update(overrides)
    return DiscoveryCycle(**values)


def _require_foreign_key_actions(db_session) -> None:
    """Deletion semantics require the Postgres-backed integration fixture.

    The shared SQLite fixture intentionally leaves foreign-key enforcement off.
    Enabling it inside an already-open fixture transaction is unreliable and
    also makes SQLAlchemy unable to tear down the deliberate Task/Cycle FK
    cycle. The normal targeted backend harness runs these assertions against
    Postgres.
    """

    if db_session.bind.dialect.name == "sqlite":
        pytest.skip("foreign-key action coverage requires Postgres")


@pytest.mark.asyncio
async def test_discovery_cycle_defaults_and_snapshot_round_trip(db_session):
    project = _project()
    db_session.add(project)
    await db_session.flush()

    intent = {
        "primary_outcome": "Agents discover a useful empirical result.",
        "fallback_outcome": "A candid, inspectable demo.",
        "source_messages": [{"id": "m-1", "text": "Make the result real."}],
    }
    bet = {
        "claim": "The candidate beats the incumbent.",
        "decision_question": "Does the effect survive a matched baseline?",
        "alternatives": ["null effect", "measurement artifact"],
    }
    cycle = _cycle(
        project.id,
        intent_snapshot=intent,
        bet_snapshot=bet,
    )
    db_session.add(cycle)
    await db_session.commit()

    loaded = (
        await db_session.execute(
            select(DiscoveryCycle).where(DiscoveryCycle.id == cycle.id)
        )
    ).scalar_one()

    assert loaded.generation == 1
    assert loaded.phase == "probe"
    assert loaded.state == "open"
    assert loaded.intent_snapshot == intent
    assert loaded.bet_snapshot == bet
    assert loaded.mechanism_fingerprint is None
    assert loaded.candidate_sha is None
    assert loaded.source_research_task_id is None
    assert loaded.review_task_id is None
    assert loaded.deadline_at is None
    assert loaded.verdict is None
    assert loaded.review_result is None
    assert loaded.reviewed_at is None
    assert loaded.revision == 0
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


@pytest.mark.asyncio
async def test_only_one_live_cycle_exists_per_project_direction(db_session):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    db_session.add(_cycle(project.id, generation=1))
    await db_session.commit()

    db_session.add(_cycle(project.id, generation=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_next_generation_is_allowed_after_prior_cycle_is_reviewed(db_session):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    first = _cycle(
        project.id,
        generation=1,
        state="reviewed",
        verdict="advance",
        review_result={"observed": "A real but still narrow effect."},
    )
    db_session.add(first)
    await db_session.commit()

    second = _cycle(project.id, generation=2)
    db_session.add(second)
    await db_session.commit()

    generations = (
        (
            await db_session.execute(
                select(DiscoveryCycle.generation)
                .where(
                    DiscoveryCycle.project_id == project.id,
                    DiscoveryCycle.direction_key == "d-discovery",
                )
                .order_by(DiscoveryCycle.generation)
            )
        )
        .scalars()
        .all()
    )
    assert generations == [1, 2]


@pytest.mark.asyncio
async def test_review_blocked_cycle_still_owns_live_direction_slot(db_session):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    db_session.add(_cycle(project.id, generation=1, state="review_blocked"))
    await db_session.commit()

    db_session.add(_cycle(project.id, generation=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("verdict", ["advance", "park", "stop", "ask_operator"])
@pytest.mark.asyncio
async def test_discovery_cycle_accepts_controller_action_verdicts(
    db_session,
    verdict,
):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        _cycle(
            project.id,
            state="reviewed",
            verdict=verdict,
        )
    )
    await db_session.commit()


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    [
        ({"outcome_kind": "deterministic"}, "chk_discoverycycle_outcome_kind"),
        ({"phase": "planning"}, "chk_discoverycycle_phase"),
        ({"state": "paused"}, "chk_discoverycycle_state"),
        ({"verdict": "renew"}, "chk_discoverycycle_verdict"),
        ({"generation": 0}, "chk_discoverycycle_generation"),
        ({"revision": -1}, "chk_discoverycycle_revision"),
        (
            {"mechanism_fingerprint": "not-a-sha256"},
            "chk_discoverycycle_mechanism_fingerprint",
        ),
    ],
)
@pytest.mark.asyncio
async def test_discovery_cycle_rejects_invalid_vocabulary(
    db_session,
    overrides,
    constraint_name,
):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    db_session.add(_cycle(project.id, **overrides))

    with pytest.raises(IntegrityError) as exc_info:
        await db_session.commit()
    # PostgreSQL names the violated constraint; SQLite generally reports only
    # the failed expression. Keep the name assertion where the dialect exposes it.
    if db_session.bind.dialect.name == "postgresql":
        assert constraint_name in str(exc_info.value)


@pytest.mark.asyncio
async def test_evidence_dedupe_is_scoped_to_one_cycle(db_session):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    first_cycle = _cycle(
        project.id,
        direction_key="d-first",
        generation=1,
        state="reviewed",
    )
    second_cycle = _cycle(project.id, direction_key="d-second", generation=1)
    db_session.add_all([first_cycle, second_cycle])
    await db_session.flush()

    common = {
        "semantic_role": "representative",
        "source_kind": "artifact",
        "source_id": "artifact-1",
        "checksum": "a" * 64,
        "dedupe_key": "artifact:artifact-1",
    }
    db_session.add(DiscoveryEvidenceItem(cycle_id=first_cycle.id, **common))
    db_session.add(DiscoveryEvidenceItem(cycle_id=second_cycle.id, **common))
    await db_session.commit()

    db_session.add(DiscoveryEvidenceItem(cycle_id=first_cycle.id, **common))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checksum", "too-short"),
        ("mechanism_fingerprint", "too-short"),
    ],
)
@pytest.mark.asyncio
async def test_evidence_rejects_invalid_hashes(db_session, field, value):
    project = _project()
    db_session.add(project)
    await db_session.flush()
    cycle = _cycle(project.id)
    db_session.add(cycle)
    await db_session.flush()
    values = {
        "cycle_id": cycle.id,
        "semantic_role": "failure",
        "source_kind": "artifact",
        "checksum": "b" * 64,
        "dedupe_key": f"invalid:{field}",
        field: value,
    }
    db_session.add(DiscoveryEvidenceItem(**values))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_deleting_cycle_cascades_evidence_and_nulls_task_lineage(db_session):
    _require_foreign_key_actions(db_session)
    project = _project()
    db_session.add(project)
    await db_session.flush()
    source_task = Task(project_id=project.id, title="Source research")
    review_task = Task(project_id=project.id, title="Discovery review")
    db_session.add_all([source_task, review_task])
    await db_session.flush()
    cycle = _cycle(
        project.id,
        source_research_task_id=source_task.id,
        review_task_id=review_task.id,
    )
    db_session.add(cycle)
    await db_session.flush()
    source_task.discovery_cycle_id = cycle.id
    review_task.discovery_cycle_id = cycle.id
    request = StrategicSpawnRequest(
        project_id=project.id,
        parent_task_id=source_task.id,
        idempotency_key=f"discovery-lineage-{uuid.uuid4()}",
        task_type="research",
        specification={"prompt": "Run the next discriminating probe."},
        specification_hash="c" * 64,
        direction_id="d-discovery",
        discovery_cycle_id=cycle.id,
    )
    evidence = DiscoveryEvidenceItem(
        cycle_id=cycle.id,
        semantic_role="representative",
        source_kind="task_artifact",
        source_id=str(source_task.id),
        checksum="d" * 64,
        evidence_metadata={"sample": 3},
        dedupe_key="task-artifact:representative:3",
    )
    db_session.add_all([source_task, review_task, request, evidence])
    cycle_id = cycle.id
    source_task_id = source_task.id
    review_task_id = review_task.id
    request_id = request.id
    evidence_id = evidence.id
    await db_session.commit()

    loaded_cycle = await db_session.get(DiscoveryCycle, cycle_id)
    loaded_request = await db_session.get(StrategicSpawnRequest, request_id)
    loaded_source = await db_session.get(Task, source_task_id)
    loaded_review = await db_session.get(Task, review_task_id)
    assert loaded_request.discovery_cycle_id == cycle_id
    assert loaded_source.discovery_cycle_id == cycle_id
    assert loaded_review.discovery_cycle_id == cycle_id
    assert loaded_cycle.source_research_task_id == source_task_id
    assert loaded_cycle.review_task_id == review_task_id

    await db_session.delete(loaded_cycle)
    await db_session.commit()
    db_session.expire_all()

    assert await db_session.get(DiscoveryEvidenceItem, evidence_id) is None
    assert (await db_session.get(Task, source_task_id)).discovery_cycle_id is None
    assert (await db_session.get(Task, review_task_id)).discovery_cycle_id is None
    assert (
        await db_session.get(StrategicSpawnRequest, request_id)
    ).discovery_cycle_id is None


@pytest.mark.asyncio
async def test_deleting_project_cascades_cycle_and_evidence(db_session):
    _require_foreign_key_actions(db_session)
    project = _project()
    db_session.add(project)
    await db_session.flush()
    cycle = _cycle(project.id)
    db_session.add(cycle)
    await db_session.flush()
    evidence = DiscoveryEvidenceItem(
        cycle_id=cycle.id,
        semantic_role="worst_case",
        source_kind="artifact",
        checksum="e" * 64,
        dedupe_key="artifact:worst-case",
    )
    db_session.add(evidence)
    project_id = project.id
    cycle_id = cycle.id
    evidence_id = evidence.id
    await db_session.commit()

    loaded_project = await db_session.get(Project, project_id)
    await db_session.delete(loaded_project)
    await db_session.commit()
    db_session.expire_all()

    assert await db_session.get(DiscoveryCycle, cycle_id) is None
    assert await db_session.get(DiscoveryEvidenceItem, evidence_id) is None
