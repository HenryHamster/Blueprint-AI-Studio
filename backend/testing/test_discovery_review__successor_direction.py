"""Container-vs-assignment contracts for the discovery successor payload."""

from __future__ import annotations

import uuid

import pytest

from app.discovery_review import (
    complete_discovery_review,
    maybe_close_and_spawn_discovery_review,
)
from app.models import StrategicSpawnRequest, Task
from testing.test_discovery_review import _cycle_with_source, _result


async def _advance_to_successor_payload(
    db_session,
    *,
    claim: str,
    direction_overrides: dict | None = None,
):
    """Advance one cycle and hand back the successor's spawn specification."""

    project, cycle, _ = await _cycle_with_source(
        db_session, direction_overrides=direction_overrides
    )
    spawned = await maybe_close_and_spawn_discovery_review(db_session, cycle.id)
    review = await db_session.get(Task, spawned.task_id)
    await complete_discovery_review(
        db_session,
        task=review,
        raw_result=_result(cycle, review, claim=claim),
    )
    await db_session.flush()
    request = await db_session.get(
        StrategicSpawnRequest,
        uuid.UUID(cycle.review_result["successor_spawn_request_id"]),
    )
    assert request is not None
    task_spec = request.specification["task"]
    return project, task_spec, task_spec["payloads"][0]


# A claim shaped exactly like the production defect: a settled statement of
# fact, not a proposition that could turn out false. Re-verifying it is free
# and always succeeds, which is why it could recur forever once it occupied
# the direction's identity slot (d-1210b978; rollups #17884/#17908).
_SETTLED_FACT_CLAIM = (
    "Both named liveness tests exist at backend/testing/test_orchestrator_liveness.py "
    "(test_second_halt_after_recovery_raises_a_fresh_signal:318)"
)


@pytest.mark.asyncio
async def test_successor_direction_keeps_open_ended_text_not_the_bet_claim(
    db_session,
):
    """The container stays the direction; the bet is only the assignment.

    Writing the bet ``claim`` into the successor's direction ``text`` collapsed
    the two levels, so a settled-fact claim became a direction that could only
    be re-confirmed. The direction's own durable text is carried forward for
    exactly this purpose and must win the identity slot.
    """

    _, task_spec, payload = await _advance_to_successor_payload(
        db_session, claim=_SETTLED_FACT_CLAIM
    )

    successor_direction = payload["directions"][0]
    assert successor_direction["text"] == "Prove the bounded mechanism"
    assert _SETTLED_FACT_CLAIM not in successor_direction["text"]
    # The packet builder documents that ``source_parent.title`` is literally
    # "Research: {direction_text}" and bounds the direction mapping out of the
    # decision packet on that basis, so the title is authority, not cosmetics.
    assert task_spec["title"] == "Research: Prove the bounded mechanism"


@pytest.mark.asyncio
async def test_successor_still_carries_the_whole_bet_as_its_assignment(db_session):
    """Moving the claim out of the direction slot must not lose the work.

    Trading premise churn for a dropped assignment would be strictly worse, so
    every ``next_bet`` field has to reach the research child intact. The agent
    resolves its research focus as payload.prompt first, and only that slot is
    uncapped -- the task description is truncated to 500 chars downstream.
    """

    _, task_spec, payload = await _advance_to_successor_payload(
        db_session, claim=_SETTLED_FACT_CLAIM
    )

    assignment = payload["prompt"]
    assert _SETTLED_FACT_CLAIM in assignment
    assert "Does the mechanism improve the target outcome?" in assignment
    assert "A measured probe improves the target outcome." in assignment
    assert "Retire after two probes without improvement." in assignment
    # The child must be able to tell the bounded assignment apart from the
    # standing direction it sits inside; that distinction is the whole fix.
    assert "single-issue assignment" in assignment
    # payload.prompt is load-bearing, not a duplicate of a slot that already
    # worked: the description never carried the bet, and strategic_spawn_requests
    # truncates that column to 500 chars, which this assignment already exceeds.
    assert _SETTLED_FACT_CLAIM not in task_spec["description"]
    assert len(assignment) > 500


@pytest.mark.asyncio
async def test_successor_completion_criteria_is_the_direction_bar_not_the_bet_bar(
    db_session,
):
    """``completion_criteria`` is a direction field and must hold its own bar."""

    _, _, payload = await _advance_to_successor_payload(
        db_session,
        claim=_SETTLED_FACT_CLAIM,
        direction_overrides={
            "completion_criteria": "The direction is done when the mechanism ships."
        },
    )

    successor_direction = payload["directions"][0]
    assert successor_direction["completion_criteria"] == (
        "The direction is done when the mechanism ships."
    )
    # The bet's own bar belongs to the bet, and still reaches the child there.
    assert "A measured probe improves the target outcome." in payload["prompt"]


@pytest.mark.asyncio
async def test_successor_omits_completion_criteria_the_direction_never_had(
    db_session,
):
    """An absent direction bar is omitted, never backfilled from the bet."""

    _, _, payload = await _advance_to_successor_payload(
        db_session, claim=_SETTLED_FACT_CLAIM
    )

    successor_direction = payload["directions"][0]
    assert "completion_criteria" not in successor_direction


@pytest.mark.asyncio
async def test_successor_falls_back_to_the_claim_when_authority_has_no_text(
    db_session,
):
    """Never hand the child an anonymous direction.

    The fallback is the safe direction of the trade: a bet-shaped direction
    text is churn, but a blank one leaves the research child with no subject
    at all. Only reachable when neither live nor frozen authority carries text.
    """

    _, _, payload = await _advance_to_successor_payload(
        db_session,
        claim=_SETTLED_FACT_CLAIM,
        direction_overrides={"text": None},
    )

    assert payload["directions"][0]["text"] == _SETTLED_FACT_CLAIM
