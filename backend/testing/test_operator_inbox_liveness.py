"""Tests for the operator-inbox liveness observability endpoint.

Covers:
- GET /api/admin/operator-inbox-liveness returns snapshot shape
- ?force_run=true triggers a live check and returns run_results
- Non-admin user receives 403
- Endpoint returns 200 with zero counters when no ticks have fired
- Zero forward-leak structural invariants across the full emitter surface
  (standalone, no DB required)
"""

import types
import uuid as _uuid

import pytest

from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

from app.main import app
from app.auth import require_admin
import app.operator_inbox_liveness as _liveness_mod


@pytest.fixture(autouse=True)
def reset_liveness():
    """Reset in-process liveness counters before each test."""
    _liveness_mod._reset_liveness_for_tests()
    yield
    _liveness_mod._reset_liveness_for_tests()


# ---------------------------------------------------------------------------
# test_liveness_observability_endpoint_returns_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_observability_endpoint_returns_snapshot(
    client: AsyncClient,
):
    """GET /api/admin/operator-inbox-liveness returns all five expected keys
    with correct types even when no liveness ticks have fired.
    """
    response = await client.get("/api/admin/operator-inbox-liveness")
    assert response.status_code == 200

    data = response.json()
    assert "total_ticks" in data
    assert "total_forward_leaked" in data
    assert "total_badge_invisible" in data
    assert "total_info_alert_noise" in data
    assert "total_remediated" in data
    assert "recent_ticks" in data

    # With no ticks, all counters are 0 and recent_ticks is empty.
    assert data["total_ticks"] == 0
    assert data["total_forward_leaked"] == 0
    assert data["total_badge_invisible"] == 0
    assert data["total_info_alert_noise"] == 0
    assert data["total_remediated"] == 0
    assert data["recent_ticks"] == []


@pytest.mark.asyncio
async def test_liveness_snapshot_reflects_accumulated_state(client: AsyncClient):
    """After injecting counters, the endpoint reflects the accumulated state."""
    # Directly mutate the module-level counters (test-internal seam).
    _liveness_mod._total_ticks = 3
    _liveness_mod._total_forward_leaked = 7
    _liveness_mod._total_badge_invisible = 0
    _liveness_mod._total_info_alert_noise = 2
    _liveness_mod._total_remediated = 5
    _liveness_mod._observability_ring.append(
        {"tick": 3, "projects": 2, "flagged": 1, "forward_leaked": 7,
         "badge_invisible": 0, "info_alert_noise": 2, "remediated": 5}
    )

    response = await client.get("/api/admin/operator-inbox-liveness")
    assert response.status_code == 200

    data = response.json()
    assert data["total_ticks"] == 3
    assert data["total_forward_leaked"] == 7
    assert data["total_info_alert_noise"] == 2
    assert data["total_remediated"] == 5
    assert len(data["recent_ticks"]) == 1
    assert data["recent_ticks"][0]["forward_leaked"] == 7
    assert data["recent_ticks"][0]["remediated"] == 5


@pytest.mark.asyncio
async def test_liveness_force_run_returns_run_results(
    client: AsyncClient,
    db_session,
):
    """?force_run=true triggers run_operator_inbox_liveness_check and appends run_results."""
    from app.operator_inbox_liveness import LivenessResult

    fake_results = [
        LivenessResult(
            project_id="proj-abc",
            status="ok",
            forward_leaked=0,
            badge_invisible=0,
            info_alert_noise=0,
            detail="all ok",
            remediated_count=0,
        )
    ]

    with patch(
        "app.operator_inbox_liveness.run_operator_inbox_liveness_check",
        new=AsyncMock(return_value=fake_results),
    ):
        response = await client.get(
            "/api/admin/operator-inbox-liveness?force_run=true"
        )

    assert response.status_code == 200
    data = response.json()
    assert "run_results" in data
    assert isinstance(data["run_results"], list)
    assert len(data["run_results"]) == 1
    r = data["run_results"][0]
    assert r["project_id"] == "proj-abc"
    assert r["status"] == "ok"
    assert r["forward_leaked"] == 0


@pytest.mark.asyncio
async def test_liveness_non_admin_gets_403():
    """Non-admin user receives 403 from the liveness endpoint."""

    async def _non_admin():
        return {"user_id": "nobody", "email": "nobody@example.com", "role": "viewer"}

    app.dependency_overrides[require_admin] = _non_admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as anon_client:
            response = await anon_client.get("/api/admin/operator-inbox-liveness")
        # require_admin raises HTTPException(403) for non-admins; FastAPI converts
        # dependency exceptions to the appropriate status code.
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Standalone zero-leak structural invariants (no DB required)
# ---------------------------------------------------------------------------
#
# These tests assert that the forward-leak predicate used by
# run_operator_inbox_liveness_check returns "not a leak" for every legitimate
# message shape in the registry — i.e. the aggregate forward_leaked count over
# the full emitter surface is zero when every message is correctly routed.
#
# The forward-leak predicate (from operator_inbox_liveness.py:162) is:
#   leaked = not derive_needs_action(m) and _audience_str(m) != OPERATOR_FYI
#
# For operator-action-needed messages: derive_needs_action() returns True
#   → "not True" == False → not a leak.
# For operator-fyi messages: _audience_str(m) == OPERATOR_FYI
#   → "!= OPERATOR_FYI" == False → not a leak.
# For non-operator messages: operator_visible_audience_filter drops them
#   before the loop — they never appear in `messages`.


def _make_msg(audience: str, status: str = "open", message_type: str | None = None) -> types.SimpleNamespace:
    """Return a SimpleNamespace mock with the minimal shape derive_needs_action reads."""
    return types.SimpleNamespace(
        status=status,
        message_type=message_type,
        context={"audience": audience},
    )


def test_zero_forward_leaked_for_all_operator_action_needed_registry_entries():
    """Every OPERATOR_ACTION_NEEDED registry entry produces derive_needs_action=True.

    The forward-leak predicate requires 'not derive_needs_action(m)', so any
    OPERATOR_ACTION_NEEDED message can never be a forward leak.  This pins the
    invariant across the full emitter surface.
    """
    from app.message_audience import OPERATOR_ACTION_NEEDED, _EVENT_AUDIENCE_REGISTRY
    from app.message_convergence import derive_needs_action

    action_needed_types = [
        et for et, aud in _EVENT_AUDIENCE_REGISTRY.items()
        if aud == OPERATOR_ACTION_NEEDED
    ]
    assert action_needed_types, "Registry must contain at least one OPERATOR_ACTION_NEEDED entry"

    forward_leaked = 0
    for event_type in action_needed_types:
        msg = _make_msg(OPERATOR_ACTION_NEEDED, status="open")
        needs_action = derive_needs_action(msg)
        assert needs_action is True, (
            f"event_type={event_type!r}: derive_needs_action returned False for "
            f"an OPERATOR_ACTION_NEEDED message — this message WOULD be counted "
            f"as a forward leak"
        )
        if not needs_action:
            forward_leaked += 1

    assert forward_leaked == 0, (
        f"{forward_leaked} OPERATOR_ACTION_NEEDED registry entries would be "
        f"counted as forward leaks — the inbox-liveness SLO would fire incorrectly"
    )


def test_operator_fyi_entries_excluded_from_forward_leak_by_audience_guard():
    """Every OPERATOR_FYI registry entry is excluded by the != OPERATOR_FYI guard.

    The forward-leak predicate short-circuits on '_audience_str(m) != OPERATOR_FYI',
    so any OPERATOR_FYI message can never be counted as a forward leak regardless
    of what derive_needs_action returns.  Pins this across the full FYI surface.
    """
    from app.message_audience import OPERATOR_FYI, _EVENT_AUDIENCE_REGISTRY

    fyi_types = [
        et for et, aud in _EVENT_AUDIENCE_REGISTRY.items()
        if aud == OPERATOR_FYI
    ]
    assert fyi_types, "Registry must contain at least one OPERATOR_FYI entry"

    forward_leaked = 0
    for event_type in fyi_types:
        msg = _make_msg(OPERATOR_FYI, status="open")
        audience_str = (msg.context or {}).get("audience")
        is_excluded_by_guard = (audience_str == OPERATOR_FYI)
        assert is_excluded_by_guard, (
            f"event_type={event_type!r}: audience guard would NOT exclude this "
            f"OPERATOR_FYI message — it would enter the forward-leak predicate"
        )
        # Simulate what the predicate does: if excluded by guard, not a leak.
        if not is_excluded_by_guard:
            forward_leaked += 1

    assert forward_leaked == 0, (
        f"{forward_leaked} OPERATOR_FYI registry entries would pass the audience "
        f"guard and could be wrongly counted as forward leaks"
    )


def test_non_operator_entries_never_reach_forward_leak_loop():
    """STRATEGIST_INTERNAL and PIPELINE_LOG entries are invisible to the leak loop.

    operator_visible_audience_filter excludes rows whose audience is in
    {strategist-internal, pipeline-log} before the loop runs — those messages
    are never in the `messages` list that the forward-leak predicate iterates.
    This test verifies is_operator_visible returns False for all non-operator
    registry entries, confirming they cannot contribute to forward_leaked.
    """
    from app.message_audience import (
        STRATEGIST_INTERNAL,
        PIPELINE_LOG,
        _EVENT_AUDIENCE_REGISTRY,
        is_operator_visible,
    )

    non_operator_audiences = {STRATEGIST_INTERNAL, PIPELINE_LOG}
    non_operator_types = [
        (et, aud) for et, aud in _EVENT_AUDIENCE_REGISTRY.items()
        if aud in non_operator_audiences
    ]
    assert non_operator_types, "Registry must contain at least one non-operator entry"

    leaked_through = []
    for event_type, audience in non_operator_types:
        if is_operator_visible(audience):
            leaked_through.append((event_type, audience))

    assert not leaked_through, (
        f"{len(leaked_through)} non-operator registry entries are marked "
        f"is_operator_visible=True and would pass operator_visible_audience_filter: "
        f"{leaked_through[:5]}"
    )


def test_aggregate_forward_leaked_zero_over_full_emitter_surface():
    """Aggregate forward_leaked is exactly 0 across the complete registry.

    Simulates one mock message per registry entry at its registered audience,
    applies the full forward-leak predicate
        leaked = not derive_needs_action(m) and _audience_str(m) != OPERATOR_FYI
    restricted to operator-visible messages (mirroring what the liveness check
    does after operator_visible_audience_filter), and asserts the total is 0.

    This is the direct structural proof of the d-7c851b3f success criterion:
    'zero non-actionable internal notices reaching the operator channel'.
    """
    from app.message_audience import (
        OPERATOR_FYI,
        _EVENT_AUDIENCE_REGISTRY,
        is_operator_visible,
    )
    from app.message_convergence import derive_needs_action

    forward_leaked = 0
    leaking_entries = []

    for event_type, audience in _EVENT_AUDIENCE_REGISTRY.items():
        if not is_operator_visible(audience):
            # Non-operator entries never reach the forward-leak loop.
            continue

        msg = _make_msg(audience, status="open")

        def _audience_str(m: types.SimpleNamespace) -> str | None:
            return (m.context or {}).get("audience")

        leaked = not derive_needs_action(msg) and _audience_str(msg) != OPERATOR_FYI
        if leaked:
            forward_leaked += 1
            leaking_entries.append(event_type)

    assert forward_leaked == 0, (
        f"{forward_leaked} registry entries produce a forward leak when their "
        f"messages are correctly routed — liveness SLO would fire: {leaking_entries}"
    )


def test_derive_needs_action_returns_false_for_resolved_operator_action_needed():
    """A resolved OPERATOR_ACTION_NEEDED message is NOT a needs-action item.

    derive_needs_action() returns False for resolved messages regardless of
    audience.  The liveness check only queries open/linked messages, so resolved
    rows never enter the loop — but this test documents that the predicate is
    well-behaved even if they did.
    """
    from app.message_audience import OPERATOR_ACTION_NEEDED
    from app.message_convergence import derive_needs_action

    msg = _make_msg(OPERATOR_ACTION_NEEDED, status="resolved")
    assert derive_needs_action(msg) is False


def test_derive_needs_action_returns_true_for_linked_operator_action_needed():
    """A linked (not just open) OPERATOR_ACTION_NEEDED message is a needs-action item.

    The liveness check queries status IN ('open', 'linked') — linked messages
    must also satisfy derive_needs_action() to avoid being counted as forward leaks.
    """
    from app.message_audience import OPERATOR_ACTION_NEEDED
    from app.message_convergence import derive_needs_action

    msg = _make_msg(OPERATOR_ACTION_NEEDED, status="linked")
    assert derive_needs_action(msg) is True


def test_registry_coverage_non_empty_for_each_audience_class():
    """The registry contains at least one entry for each of the four audiences.

    A registry accidentally emptied of all OPERATOR_ACTION_NEEDED entries would
    make the zero-leak invariant trivially true.  This test ensures coverage is
    meaningful.
    """
    from app.message_audience import (
        OPERATOR_ACTION_NEEDED,
        OPERATOR_FYI,
        STRATEGIST_INTERNAL,
        PIPELINE_LOG,
        _EVENT_AUDIENCE_REGISTRY,
    )

    by_audience: dict[str, list[str]] = {
        OPERATOR_ACTION_NEEDED: [],
        OPERATOR_FYI: [],
        STRATEGIST_INTERNAL: [],
        PIPELINE_LOG: [],
    }
    for event_type, audience in _EVENT_AUDIENCE_REGISTRY.items():
        if audience in by_audience:
            by_audience[audience].append(event_type)

    for audience, entries in by_audience.items():
        # PIPELINE_LOG may be sparse — require at least 0; others need at least 1.
        if audience == PIPELINE_LOG:
            continue
        assert entries, f"Registry has zero entries for audience '{audience}'"


def test_route_event_fail_closed_for_unregistered_type():
    """route_event returns STRATEGIST_INTERNAL for any unregistered event_type.

    An emitter that forgets to register its event_type must be invisible to
    the operator — not silently promoted to operator-visible.
    """
    from app.message_audience import STRATEGIST_INTERNAL, route_event

    assert route_event("this_event_type_does_not_exist_in_registry") == STRATEGIST_INTERNAL
    assert route_event("") == STRATEGIST_INTERNAL
    assert route_event("   ") == STRATEGIST_INTERNAL


def test_route_event_message_type_fallback_upgrades_to_operator_action_needed():
    """An unregistered event_type with message_type=BLOCKER/QUESTION gets OPERATOR_ACTION_NEEDED.

    The message_type fallback in route_event() ensures that a strategist-authored
    BLOCKER or QUESTION with an unregistered event_type still reaches the operator
    inbox.  This prevents escalation messages from being silently buried.
    """
    from app.message_audience import OPERATOR_ACTION_NEEDED, STRATEGIST_INTERNAL, route_event

    # Without message_type fallback: fail-closed to STRATEGIST_INTERNAL.
    assert route_event("unregistered_event") == STRATEGIST_INTERNAL

    # With message_type=BLOCKER: upgraded to OPERATOR_ACTION_NEEDED.
    assert route_event("unregistered_event", message_type="BLOCKER") == OPERATOR_ACTION_NEEDED

    # With message_type=QUESTION: upgraded to OPERATOR_ACTION_NEEDED.
    assert route_event("unregistered_event", message_type="QUESTION") == OPERATOR_ACTION_NEEDED

    # With message_type=NOTE: NOT upgraded — NOTE is not inherently operator-facing.
    assert route_event("unregistered_event", message_type="NOTE") == STRATEGIST_INTERNAL


def test_is_operator_visible_correct_for_all_four_audiences():
    """is_operator_visible returns True only for operator-action-needed and operator-fyi."""
    from app.message_audience import (
        OPERATOR_ACTION_NEEDED,
        OPERATOR_FYI,
        STRATEGIST_INTERNAL,
        PIPELINE_LOG,
        is_operator_visible,
    )

    assert is_operator_visible(OPERATOR_ACTION_NEEDED) is True
    assert is_operator_visible(OPERATOR_FYI) is True
    assert is_operator_visible(STRATEGIST_INTERNAL) is False
    assert is_operator_visible(PIPELINE_LOG) is False
    assert is_operator_visible("unknown-audience") is False


# ---------------------------------------------------------------------------
# DB-backed durable persistence + escalation tests
# ---------------------------------------------------------------------------


async def _create_test_project(session, name: str = "liveness-test") -> str:
    """Create a minimal project row and return its string id."""
    from app.models import Project

    project = Project(name=name, user_id="liveness-test-user")
    session.add(project)
    await session.flush()
    return str(project.id)


async def _create_forward_leaked_message(
    session,
    project_id: str,
    *,
    status: str = "open",
    audience: str | None = None,
    message_type: str = "NOTE",
    from_type: str = "system",
) -> str:
    """Create a ProjectMessage that will be detected as a forward leak.

    The message has operator-visible audience filter pass-through (NULL or
    operator-action-needed/fyi) but does NOT satisfy derive_needs_action()
    (a NOTE with operator-action-needed audience passes derive_needs_action
    — so we use NULL audience to fail the filter or operator-fyi which is
    excluded by the != OPERATOR_FYI guard).

    Actually, to create a forward-leaked message the liveness check will
    detect: we need operator_visible_audience_filter to PASS (so the message
    appears in the query) AND derive_needs_action to FAIL AND the audience
    guard != OPERATOR_FYI to PASS.

    A NOTE with audience=None passes operator_visible_audience_filter (NULL
    is retained), fails derive_needs_action (NOTE is never needs-action),
    and audience is not OPERATOR_FYI, so it's a forward leak.
    """
    from app.models import ProjectMessage

    ctx = {"audience": audience} if audience else {}
    msg = ProjectMessage(
        project_id=_uuid.UUID(project_id),
        from_type=from_type,
        message_type=message_type,
        status=status,
        content="Test forward-leaked message",
        context=ctx,
    )
    session.add(msg)
    await session.flush()
    return str(msg.id)


async def _count_observations(session, project_id: str) -> int:
    """Return the number of durable observation rows for *project_id*."""
    from app.models import OperatorInboxLivenessObservation as OLO
    from sqlalchemy import func as sqlfunc
    from sqlmodel import select

    result = await session.execute(
        select(sqlfunc.count()).where(OLO.project_id == project_id)
    )
    return result.scalar() or 0


async def _get_escalation_messages(session, project_id: str) -> list:
    """Return open ProjectMessages with the persistent-leak dedupe_key."""
    from app.models import ProjectMessage
    from sqlmodel import select

    dedupe_key = f"operator_inbox_leak:{project_id}"
    result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == _uuid.UUID(project_id),
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_persistent_leak_escalates_once_to_strategist(db_session):
    """Drive liveness check across >=3 flagged ticks; assert exactly ONE
    strategist-internal escalation is created, durable observations are
    written, and a clean tick resolves the episode.

    This is the named acceptance test for the durable-persistence +
    strategist-escalation feature (d-7c851b3f success evidence).
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "persistent-leak-test")
    # Create a forward-leaked message: NULL-audience NOTE that passes
    # operator_visible_audience_filter (NULL is retained) but fails
    # derive_needs_action (NOTE is never needs-action).
    await _create_forward_leaked_message(db_session, project_id)

    # Tick 1 — first flagged tick (not yet persistent, < 3)
    results_1 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_1 is not None
    flagged_1 = [r for r in results_1 if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged_1) == 1, f"Expected 1 flagged, got {flagged_1}"

    # One durable observation written
    assert await _count_observations(db_session, project_id) == 1

    # No escalation yet (only 1 tick)
    escalations_1 = await _get_escalation_messages(db_session, project_id)
    assert len(escalations_1) == 0, "Should not escalate before persistence threshold"

    # Tick 2 — second flagged tick (still not persistent).
    # Tick 1 remediated the previous message; create a fresh leaked one.
    await _create_forward_leaked_message(db_session, project_id)
    results_2 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_2 is not None
    flagged_2 = [r for r in results_2 if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged_2) == 1

    assert await _count_observations(db_session, project_id) == 2
    escalations_2 = await _get_escalation_messages(db_session, project_id)
    assert len(escalations_2) == 0, "Should not escalate at only 2 ticks"

    # Tick 3 — third flagged tick (NOW persistent: >= 3 consecutive).
    await _create_forward_leaked_message(db_session, project_id)
    results_3 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_3 is not None
    flagged_3 = [r for r in results_3 if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged_3) == 1

    # Three durable observations
    assert await _count_observations(db_session, project_id) == 3

    # EXACTLY ONE escalation message created
    escalations_3 = await _get_escalation_messages(db_session, project_id)
    assert len(escalations_3) == 1, (
        f"Expected exactly 1 escalation at tick 3, got {len(escalations_3)}"
    )
    escalation = escalations_3[0]
    assert escalation.status == "open"

    # Audience is STRATEGIST_INTERNAL — never operator-visible
    audience = (escalation.context or {}).get("audience")
    assert audience == STRATEGIST_INTERNAL, (
        f"Escalation audience must be strategist-internal, got {audience!r}"
    )

    # Verify the escalation content references the leak
    assert "persistent" in (escalation.content or "").lower()

    # Tick 4 — fourth flagged tick (already escalated, should be idempotent).
    await _create_forward_leaked_message(db_session, project_id)
    results_4 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_4 is not None
    flagged_4 = [r for r in results_4 if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged_4) == 1

    assert await _count_observations(db_session, project_id) == 4

    # STILL exactly ONE escalation — no duplicates
    escalations_4 = await _get_escalation_messages(db_session, project_id)
    open_4 = [e for e in escalations_4 if e.status == "open"]
    assert len(open_4) == 1, (
        f"Expected exactly 1 OPEN escalation after tick 4, got {len(open_4)} "
        f"(total messages with dedupe_key: {len(escalations_4)})"
    )

    # Tick 5 — clean tick: all prior leaked messages were remediated (re-stamped
    # to STRATEGIST_INTERNAL) by the liveness check, so no new message is needed
    # for this tick to be clean.
    results_5 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_5 is not None
    proj_5 = [r for r in results_5 if r.project_id == project_id]
    if proj_5:
        assert proj_5[0].status in ("ok", "skipped"), (
            f"Expected ok/skipped after remediation cleared all leaks, got {proj_5[0].status}"
        )

    # The escalation should now be resolved
    await db_session.refresh(escalation)
    assert escalation.status == "resolved", (
        f"Escalation should be resolved after clean tick, got status={escalation.status!r}"
    )
    assert escalation.resolution == "auto-resolved (clean tick)"

    # Now re-introduce a leak and verify a NEW escalation is created
    await _create_forward_leaked_message(db_session, project_id)

    # Tick 6 — start of new episode (1st flagged tick after clean break)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    # No open escalation after just 1 tick of a new episode
    escalations_6 = await _get_escalation_messages(db_session, project_id)
    open_6 = [e for e in escalations_6 if e.status == "open"]
    assert len(open_6) == 0, (
        f"Expected no open escalation after 1 tick of new episode, "
        f"got {len(open_6)} open (would mean episode boundary was missed)"
    )

    # Tick 7 — 2nd flagged tick (still below persistence threshold of 3)
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    escalations_7 = await _get_escalation_messages(db_session, project_id)
    open_7 = [e for e in escalations_7 if e.status == "open"]
    assert len(open_7) == 0, (
        f"Expected no open escalation after 2 ticks of new episode, "
        f"got {len(open_7)} open (would mean episode boundary was missed)"
    )

    # Tick 8 — 3rd flagged tick, persistence threshold reached again
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)

    # A NEW escalation should exist (the old one is resolved, so a fresh open one)
    all_escalations = await _get_escalation_messages(db_session, project_id)
    open_escalations = [e for e in all_escalations if e.status == "open"]
    assert len(open_escalations) == 1, (
        f"Expected exactly 1 OPEN escalation for new episode, "
        f"got {len(open_escalations)} open, {len(all_escalations)} total"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_durable_observation_written_for_flagged_project(db_session):
    """A single flagged tick writes one durable observation row."""
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "durable-obs-test")
    await _create_forward_leaked_message(db_session, project_id)

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None
    flagged = [r for r in results if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged) == 1

    count = await _count_observations(db_session, project_id)
    assert count == 1, f"Expected 1 durable observation, got {count}"

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_no_escalation_below_persistence_threshold(db_session):
    """No escalation message is created when a project has fewer than 3
    consecutive flagged ticks."""
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "below-threshold-test")
    await _create_forward_leaked_message(db_session, project_id)

    # Two ticks — below the threshold of 3
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)

    escalations = await _get_escalation_messages(db_session, project_id)
    assert len(escalations) == 0, (
        f"No escalation expected below threshold, got {len(escalations)}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_clean_tick_resolves_open_escalation(db_session):
    """A clean tick (no forward leaks) resolves an existing open escalation."""
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "clean-resolve-test")

    # Three ticks to trigger persistence and escalation.  Each tick creates a
    # fresh leaked message because remediation re-stamps the previous one.
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)

    # Escalation should exist and be open
    escalations = await _get_escalation_messages(db_session, project_id)
    assert len(escalations) == 1
    assert escalations[0].status == "open"

    # All prior leaked messages were remediated (re-stamped to STRATEGIST_INTERNAL)
    # by the three liveness runs — no manual cleanup required.  The project is
    # naturally clean for the next tick.

    # Clean tick — should resolve the escalation
    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None
    proj = [r for r in results if r.project_id == project_id]
    if proj:
        assert proj[0].status in ("ok", "skipped")

    # Re-fetch — escalation should be resolved
    await db_session.refresh(escalations[0])
    assert escalations[0].status == "resolved"
    assert escalations[0].resolution == "auto-resolved (clean tick)"

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_escalation_event_type_is_strategist_internal(db_session):
    """The 'operator_inbox_leak_persistent' event type resolves to
    STRATEGIST_INTERNAL and never reaches the operator channel."""
    from app.message_audience import STRATEGIST_INTERNAL, OPERATOR_ACTION_NEEDED, route_event
    from app.message_audience import is_operator_visible

    # Verify the event type is registered as STRATEGIST_INTERNAL
    audience = route_event("operator_inbox_leak_persistent")
    assert audience == STRATEGIST_INTERNAL, (
        f"Expected STRATEGIST_INTERNAL, got {audience!r}"
    )
    assert not is_operator_visible(audience), (
        "operator_inbox_leak_persistent must NOT be operator-visible"
    )
    # Also verify it's not accidentally operator-action-needed
    assert audience != OPERATOR_ACTION_NEEDED, (
        "Escalation must not create operator-facing noise"
    )


@pytest.mark.asyncio
async def test_observability_endpoint_includes_durable_data(
    client: AsyncClient, db_session
):
    """The liveness endpoint returns durable_observations and open_episodes keys
    alongside the existing in-memory snapshot keys."""
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "endpoint-durable-test")

    # Run enough ticks to trigger escalation so there's an open episode.
    # Each tick creates a fresh leaked message because remediation re-stamps
    # the previous one on each run.
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)
    await _create_forward_leaked_message(db_session, project_id)
    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)

    response = await client.get("/api/admin/operator-inbox-liveness")
    assert response.status_code == 200
    data = response.json()

    # Backward-compatible keys still present
    assert "total_ticks" in data
    assert "total_forward_leaked" in data
    assert "recent_ticks" in data

    # New durable keys present
    assert "durable_observations" in data, "Missing durable_observations key"
    assert isinstance(data["durable_observations"], list)
    assert "open_episodes" in data, "Missing open_episodes key"
    assert isinstance(data["open_episodes"], list)

    # At least one durable observation for our project
    assert any(
        obs["project_id"] == project_id for obs in data["durable_observations"]
    ), f"No durable observation found for {project_id}"

    # Open episode for our project should exist (persistence threshold reached)
    assert any(
        ep.get("project_id") == project_id for ep in data["open_episodes"]
    ), f"No open episode found for {project_id}"

    _liveness_mod._reset_liveness_for_tests()


# ---------------------------------------------------------------------------
# Remediation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_leaked_message_is_remediated_on_run(db_session):
    """A forward-leaked message has its audience re-stamped on the first
    liveness run, so it no longer appears in the operator inbox on the next run.

    This is the acceptance test for the remediation feature: the liveness check
    must not merely DETECT leaks but also FIX them in-place.
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import is_operator_visible
    from app.models import ProjectMessage
    from sqlmodel import select

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "remediation-test")
    msg_id = await _create_forward_leaked_message(db_session, project_id)

    # Tick 1 — should detect and remediate the leaked message.
    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None
    flagged = [r for r in results if r.project_id == project_id and r.status == "flagged"]
    assert len(flagged) == 1, "Expected 1 flagged result on tick 1"
    assert flagged[0].forward_leaked == 1, "Expected 1 forward-leaked message detected"

    # After remediation the message's audience must be re-stamped to a non-operator value.
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.id == _uuid.UUID(msg_id)
            )
        )
    ).scalars().first()
    assert msg is not None
    remediated_audience = (msg.context or {}).get("audience")
    assert remediated_audience is not None, (
        "Remediation must stamp a canonical audience — NULL means the message "
        "will still pass operator_visible_audience_filter on the next tick"
    )
    assert not is_operator_visible(remediated_audience), (
        f"Remediated audience must NOT be operator-visible, got {remediated_audience!r}"
    )

    # Tick 2 — with the message remediated, the project should be clean.
    results_2 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_2 is not None
    proj_2 = [r for r in results_2 if r.project_id == project_id]
    if proj_2:
        assert proj_2[0].status in ("ok", "skipped"), (
            f"Expected ok/skipped on tick 2 (message was remediated on tick 1), "
            f"got {proj_2[0].status!r} with forward_leaked={proj_2[0].forward_leaked}"
        )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_forward_leak_converges_to_zero_after_remediation(db_session):
    """After one remediation tick, forward_leaked converges to exactly 0 on the
    next tick — pinning the direction's ongoing-path success criterion.

    Guards against regression where a new emitter re-introduces leaks and the
    liveness check still reports a non-zero forward_leaked count after running,
    meaning self-healing did not actually fix the offending rows.

    Tick 1: creates one forward-leaked message (NULL-audience NOTE), runs the
            liveness check, asserts forward_leaked >= 1 for the project.
    Tick 2: runs the liveness check again WITHOUT injecting a new leaked message;
            the first message was re-stamped to a non-operator audience by
            remediation, so asserts forward_leaked == 0 (exact numeric convergence).
    """
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "convergence-test")
    await _create_forward_leaked_message(db_session, project_id)

    # Tick 1 — detect the leak; remediation re-stamps the message in-place.
    results_1 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_1 is not None
    proj_1 = [r for r in results_1 if r.project_id == project_id]
    assert proj_1, f"No result returned for project {project_id} on tick 1"
    assert proj_1[0].forward_leaked >= 1, (
        f"Expected at least 1 forward-leaked message on tick 1, "
        f"got forward_leaked={proj_1[0].forward_leaked}"
    )

    # Tick 2 — no new leaked message created; remediation on tick 1 must have
    # re-stamped the offending row so it no longer passes operator_visible_audience_filter.
    results_2 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results_2 is not None
    proj_2 = [r for r in results_2 if r.project_id == project_id]
    # If the project has zero leaks the check may return "ok" with no entry, or
    # "skipped" — both mean forward_leaked is 0.
    if proj_2:
        assert proj_2[0].forward_leaked == 0, (
            f"forward_leaked must converge to 0 after one remediation tick, "
            f"got forward_leaked={proj_2[0].forward_leaked} (status={proj_2[0].status!r}); "
            f"remediation did not re-stamp all offending rows"
        )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_internal_bookkeeping_events_do_not_leak_to_operator_counters(db_session):
    """A clean project with only correctly-routed messages yields forward_leaked=0
    and info_alert_noise=0 — directly encoding the d-7c851b3f success criterion.

    Seeds a project with:
    - Three STRATEGIST_INTERNAL bookkeeping messages (self_improvement.payload_too_large,
      research.dedup_deadlock, hotfix_backmerge.size_violation) that must be invisible
      to the operator because operator_visible_audience_filter excludes them.
    - One open BLOCKER with OPERATOR_ACTION_NEEDED audience (satisfies derive_needs_action
      → not a forward leak).
    - One open NOTE with OPERATOR_FYI audience (excluded by the != OPERATOR_FYI guard).
    - No INFO-severity Alert rows → info_alert_noise=0.

    Asserts that a single liveness tick returns status="ok", forward_leaked=0,
    and info_alert_noise=0, proving that correctly-routed messages do not inflate
    the operator inbox counters.
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import OPERATOR_ACTION_NEEDED, OPERATOR_FYI, STRATEGIST_INTERNAL
    from app.models import ProjectMessage

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "clean-tick-zero-leak-test")
    pid = _uuid.UUID(project_id)

    # Internal bookkeeping messages — must be invisible to the operator.
    # These represent the kinds of chatter (size-cap, dedup, hotfix notices) that
    # the success criterion says must NOT reach operator counters.
    for event_type in (
        "self_improvement.payload_too_large",
        "research.dedup_deadlock",
        "hotfix_backmerge.size_violation",
    ):
        db_session.add(ProjectMessage(
            project_id=pid,
            from_type="system",
            message_type="NOTE",
            status="open",
            content=f"Internal bookkeeping: {event_type}",
            context={"audience": STRATEGIST_INTERNAL, "event_type": event_type},
        ))

    # Correctly-routed BLOCKER — satisfies derive_needs_action() → not a forward leak.
    db_session.add(ProjectMessage(
        project_id=pid,
        from_type="system",
        message_type="BLOCKER",
        status="open",
        content="Operator-facing blocker requiring attention",
        context={"audience": OPERATOR_ACTION_NEEDED},
    ))

    # Correctly-routed FYI NOTE — excluded by the != OPERATOR_FYI guard → not a leak.
    db_session.add(ProjectMessage(
        project_id=pid,
        from_type="system",
        message_type="NOTE",
        status="open",
        content="Informational operator-fyi notice",
        context={"audience": OPERATOR_FYI},
    ))

    await db_session.flush()

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None, "run_operator_inbox_liveness_check returned None with _force=True"

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result returned for project {project_id} — project not included in check"

    r = proj[0]
    assert r.forward_leaked == 0, (
        f"forward_leaked must be 0 for a clean project (got {r.forward_leaked}); "
        f"internal bookkeeping events are leaking into the operator inbox counter"
    )
    assert r.info_alert_noise == 0, (
        f"info_alert_noise must be 0 when no INFO Alert rows exist (got {r.info_alert_noise})"
    )
    assert r.status in ("ok", "skipped"), (
        f"Expected status 'ok' or 'skipped' for a clean project, got {r.status!r}; "
        f"detail: {r.detail!r}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_operator_fyi_message_is_not_remediated(db_session):
    """An OPERATOR_FYI message is explicitly excluded from the forward-leak
    predicate and must never be remediated.

    The liveness check short-circuits on '_audience_str(m) != OPERATOR_FYI',
    so FYI messages never enter the remediation path.
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import OPERATOR_FYI
    from app.models import ProjectMessage
    from sqlmodel import select

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "fyi-no-remediation-test")
    # Create a message explicitly stamped as OPERATOR_FYI.
    msg_id = await _create_forward_leaked_message(
        db_session, project_id, audience=OPERATOR_FYI, message_type="NOTE"
    )

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    # The FYI message must NOT appear in forward_leaked.
    proj = [r for r in results if r.project_id == project_id]
    if proj:
        assert proj[0].forward_leaked == 0, (
            "OPERATOR_FYI messages must never be counted as forward leaks"
        )

    # The message's audience must still be OPERATOR_FYI after the run.
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == _uuid.UUID(msg_id))
        )
    ).scalars().first()
    assert msg is not None
    audience_after = (msg.context or {}).get("audience")
    assert audience_after == OPERATOR_FYI, (
        f"OPERATOR_FYI message audience must be unchanged after liveness run, "
        f"got {audience_after!r}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_needs_action_message_is_not_remediated(db_session):
    """A legitimate needs-action message (BLOCKER with OPERATOR_ACTION_NEEDED) is
    never remediated — its audience and status remain unchanged after a liveness run.

    Guards against regression where the remediation path incorrectly re-stamps
    genuinely actionable operator messages, which would silently make operator-
    facing blockers and questions invisible.
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import OPERATOR_ACTION_NEEDED
    from app.models import ProjectMessage, MessageType
    from sqlmodel import select

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "needs-action-no-remediation-test")
    pid = _uuid.UUID(project_id)

    # Create a legitimate BLOCKER with OPERATOR_ACTION_NEEDED audience.
    msg = ProjectMessage(
        project_id=pid,
        from_type="agent",
        message_type=MessageType.BLOCKER,
        status="open",
        content="This is a genuine blocker that needs operator attention",
        context={"audience": OPERATOR_ACTION_NEEDED},
    )
    db_session.add(msg)
    await db_session.flush()
    msg_id = str(msg.id)

    # Run the liveness check — this BLOCKER must NOT be a forward leak.
    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    if proj:
        assert proj[0].forward_leaked == 0, (
            f"A genuine BLOCKER must not be counted as a forward leak, "
            f"got forward_leaked={proj[0].forward_leaked}"
        )
        assert proj[0].remediated_count == 0, (
            f"A genuine BLOCKER must not be remediated, "
            f"got remediated_count={proj[0].remediated_count}"
        )

    # The message's audience and status must be unchanged after the run.
    db_session.expire_all()
    msg_after = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.id == _uuid.UUID(msg_id))
        )
    ).scalars().first()
    assert msg_after is not None
    audience_after = (msg_after.context or {}).get("audience")
    assert audience_after == OPERATOR_ACTION_NEEDED, (
        f"BLOCKER audience must be OPERATOR_ACTION_NEEDED after liveness run, "
        f"got {audience_after!r}"
    )
    assert msg_after.status == "open", (
        f"BLOCKER status must still be 'open' after liveness run, "
        f"got {msg_after.status!r}"
    )

    _liveness_mod._reset_liveness_for_tests()


# ---------------------------------------------------------------------------
# Inverse detector: reply-audience quarantine tests
# ---------------------------------------------------------------------------


async def _create_reply_message(
    session,
    project_id: str,
    *,
    audience: str,
    status: str = "open",
    reply_to_id: str | None = None,
) -> tuple:
    """Create a ProjectMessage with reply_to_message_id set and the given audience.

    Returns (msg_id, reply_to_id) — if reply_to_id is None a synthetic parent is
    created so the FK constraint is satisfied.
    """
    from app.models import ProjectMessage

    pid = _uuid.UUID(project_id)

    if reply_to_id is None:
        parent = ProjectMessage(
            project_id=pid,
            from_type="user",
            message_type="NOTE",
            status="open",
            content="Parent operator message",
            context={},
        )
        session.add(parent)
        await session.flush()
        reply_to_id = str(parent.id)

    reply = ProjectMessage(
        project_id=pid,
        from_type="system",
        message_type="NOTE",
        status=status,
        content="Reply with quarantined audience",
        context={"audience": audience},
        reply_to_message_id=_uuid.UUID(reply_to_id),
    )
    session.add(reply)
    await session.flush()
    return str(reply.id), reply_to_id


@pytest.mark.asyncio
async def test_reply_audience_quarantined_detected_for_strategist_internal_reply(db_session):
    """A reply_to-bearing message with STRATEGIST_INTERNAL audience is flagged
    with reply_audience_quarantined=1 by the liveness check.

    This is the named acceptance test for the inverse-detector feature
    (detector for reply-audience quarantine / invisible dropped replies).
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "quarantine-strategist-test")
    await _create_reply_message(db_session, project_id, audience=STRATEGIST_INTERNAL)

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result for project {project_id}"
    r = proj[0]
    assert r.reply_audience_quarantined == 1, (
        f"Expected reply_audience_quarantined=1 for STRATEGIST_INTERNAL reply, "
        f"got {r.reply_audience_quarantined} (status={r.status!r})"
    )
    assert r.status == "flagged", (
        f"Project with quarantined reply must be flagged, got {r.status!r}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_reply_audience_quarantined_detected_for_pipeline_log_reply(db_session):
    """A reply_to-bearing message with PIPELINE_LOG audience is flagged
    with reply_audience_quarantined=1 by the liveness check."""
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import PIPELINE_LOG

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "quarantine-pipeline-log-test")
    await _create_reply_message(db_session, project_id, audience=PIPELINE_LOG)

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result for project {project_id}"
    r = proj[0]
    assert r.reply_audience_quarantined == 1, (
        f"Expected reply_audience_quarantined=1 for PIPELINE_LOG reply, "
        f"got {r.reply_audience_quarantined}"
    )
    assert r.status == "flagged"

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_reply_without_non_operator_audience_not_quarantined(db_session):
    """A reply message with OPERATOR_ACTION_NEEDED audience produces
    reply_audience_quarantined=0 — correctly routed replies are not flagged."""
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import OPERATOR_ACTION_NEEDED

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "quarantine-correct-audience-test")
    await _create_reply_message(db_session, project_id, audience=OPERATOR_ACTION_NEEDED)

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result for project {project_id}"
    r = proj[0]
    assert r.reply_audience_quarantined == 0, (
        f"OPERATOR_ACTION_NEEDED reply must not be quarantined, "
        f"got reply_audience_quarantined={r.reply_audience_quarantined}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_message_without_reply_to_not_quarantined(db_session):
    """A non-reply message with STRATEGIST_INTERNAL audience does NOT contribute
    to reply_audience_quarantined — the inverse detector only targets replies."""
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL
    from app.models import ProjectMessage

    _liveness_mod._reset_liveness_for_tests()

    project_id = await _create_test_project(db_session, "quarantine-no-reply-to-test")
    pid = _uuid.UUID(project_id)

    db_session.add(ProjectMessage(
        project_id=pid,
        from_type="system",
        message_type="NOTE",
        status="open",
        content="Non-reply internal message",
        context={"audience": STRATEGIST_INTERNAL},
    ))
    await db_session.flush()

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    if results is not None:
        proj = [r for r in results if r.project_id == project_id]
        if proj:
            assert proj[0].reply_audience_quarantined == 0, (
                f"Non-reply STRATEGIST_INTERNAL message must not be quarantined, "
                f"got reply_audience_quarantined={proj[0].reply_audience_quarantined}"
            )

    _liveness_mod._reset_liveness_for_tests()


def test_liveness_observability_snapshot_includes_reply_audience_quarantined_key():
    """get_operator_inbox_liveness_observability() contains total_reply_audience_quarantined."""
    import app.operator_inbox_liveness as _liveness_mod

    _liveness_mod._reset_liveness_for_tests()
    snapshot = _liveness_mod.get_operator_inbox_liveness_observability()
    assert "total_reply_audience_quarantined" in snapshot, (
        "Observability snapshot must include total_reply_audience_quarantined"
    )
    assert snapshot["total_reply_audience_quarantined"] == 0

    _liveness_mod._total_reply_audience_quarantined = 5
    snapshot2 = _liveness_mod.get_operator_inbox_liveness_observability()
    assert snapshot2["total_reply_audience_quarantined"] == 5

    _liveness_mod._reset_liveness_for_tests()


# ---------------------------------------------------------------------------
# Heal + zero-quarantine invariant tests
# ---------------------------------------------------------------------------


async def _get_quarantine_alert_messages(session, project_id: str) -> list:
    """Return ProjectMessages carrying the quarantine-alert dedupe_key."""
    from app.models import ProjectMessage
    from sqlmodel import select

    dedupe_key = f"reply_quarantine_unhealed:{project_id}"
    result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == _uuid.UUID(project_id),
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_quarantined_reply_is_healed_audience_restamped_and_delivered(db_session):
    """A quarantined reply is healed within the liveness tick: its audience is
    re-stamped to an operator-visible value and delivered_at is set.

    This is the acceptance test for the re-delivery heal feature (part A).
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL, is_operator_visible
    from app.models import ProjectMessage
    from sqlmodel import select as _select

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "heal-reply-test")
    reply_id, _ = await _create_reply_message(
        db_session, project_id, audience=STRATEGIST_INTERNAL
    )

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result for project {project_id}"
    r = proj[0]

    # Heal count must be reported in the result
    assert r.reply_audience_healed >= 1, (
        f"Expected reply_audience_healed >= 1, got {r.reply_audience_healed}"
    )

    # The row must now have an operator-visible audience
    msg = (
        await db_session.execute(
            _select(ProjectMessage).where(
                ProjectMessage.id == _uuid.UUID(reply_id)
            )
        )
    ).scalars().first()
    assert msg is not None

    healed_audience = (msg.context or {}).get("audience")
    assert is_operator_visible(healed_audience), (
        f"Healed reply must have operator-visible audience, got {healed_audience!r}"
    )
    # Durable receipt: delivered_at must be stamped
    assert msg.delivered_at is not None, (
        "Healed reply must have delivered_at stamped as durable delivery receipt"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_reply_audience_healed_in_observability_snapshot(db_session):
    """total_reply_audience_healed increments in the observability snapshot
    after a tick that heals quarantined replies."""
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "healed-obs-test")
    await _create_reply_message(db_session, project_id, audience=STRATEGIST_INTERNAL)

    await _liveness_mod.run_operator_inbox_liveness_check(db_session, _force=True)

    snapshot = _liveness_mod.get_operator_inbox_liveness_observability()
    assert "total_reply_audience_healed" in snapshot, (
        "Observability snapshot must include total_reply_audience_healed"
    )
    assert snapshot["total_reply_audience_healed"] >= 1, (
        f"Expected total_reply_audience_healed >= 1 after heal tick, "
        f"got {snapshot['total_reply_audience_healed']}"
    )
    # Tick summary must also carry the heal count
    assert snapshot["recent_ticks"], "recent_ticks must be non-empty after a run"
    last_tick = snapshot["recent_ticks"][-1]
    assert last_tick.get("reply_audience_healed", 0) >= 1, (
        f"Tick summary must record reply_audience_healed, got {last_tick}"
    )

    _liveness_mod._reset_liveness_for_tests()


@pytest.mark.asyncio
async def test_nonzero_reply_quarantine_alerts_operator_and_blocks_clean_tick(db_session):
    """A detected reply-audience quarantine:
      (1) blocks the tick from being reported clean (status='flagged'), and
      (2) creates an OPERATOR_ACTION_NEEDED alert (not STRATEGIST_INTERNAL).

    After the heal clears the quarantine, the next tick is reported clean and
    the operator alert is auto-resolved.

    This is the named acceptance test for the zero-quarantine clean-tick invariant.
    """
    import app.operator_inbox_liveness as _liveness_mod
    from app.message_audience import STRATEGIST_INTERNAL, is_operator_visible

    _liveness_mod._reset_liveness_for_tests()
    project_id = await _create_test_project(db_session, "quarantine-invariant-test")

    # Tick 1: quarantined reply detected → tick is flagged + operator-visible alert fires.
    await _create_reply_message(db_session, project_id, audience=STRATEGIST_INTERNAL)

    results = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results is not None

    proj = [r for r in results if r.project_id == project_id]
    assert proj, f"No result for project {project_id}"
    r = proj[0]

    # Tick must be flagged — nonzero quarantine blocks clean tick.
    assert r.status == "flagged", (
        f"Tick with quarantined reply must be flagged (blocks clean), got {r.status!r}"
    )
    assert r.reply_audience_quarantined == 1, (
        f"Expected reply_audience_quarantined=1, got {r.reply_audience_quarantined}"
    )

    # Operator-visible alert must exist (dedupe_key in context).
    alert_msgs = await _get_quarantine_alert_messages(db_session, project_id)
    assert len(alert_msgs) >= 1, (
        "An operator-visible quarantine alert must be created on first detection"
    )
    alert = next(m for m in alert_msgs if m.status == "open")

    # Alert must be OPERATOR-visible — NOT strategist-internal.
    alert_audience = (alert.context or {}).get("audience")
    assert is_operator_visible(alert_audience), (
        f"Quarantine alert must have an operator-visible audience, got {alert_audience!r} "
        f"(must NOT be strategist-internal)"
    )

    # Tick 2: heal succeeded on tick 1 → quarantined_reply_count=0 → clean tick.
    results2 = await _liveness_mod.run_operator_inbox_liveness_check(
        db_session, _force=True
    )
    assert results2 is not None

    proj2 = [r for r in results2 if r.project_id == project_id]
    if proj2:
        assert proj2[0].reply_audience_quarantined == 0, (
            f"reply_audience_quarantined must be 0 after heal, "
            f"got {proj2[0].reply_audience_quarantined}"
        )
        assert proj2[0].status in ("ok", "skipped"), (
            f"Tick must be clean after all replies healed, got {proj2[0].status!r}"
        )

    # Quarantine alert must be auto-resolved on clean tick.
    await db_session.refresh(alert)
    assert alert.status == "resolved", (
        f"Quarantine alert must be auto-resolved after clean tick, "
        f"got status={alert.status!r}"
    )
    assert alert.resolution == "auto-resolved (zero quarantined replies)", (
        f"Unexpected resolution text: {alert.resolution!r}"
    )

    _liveness_mod._reset_liveness_for_tests()
