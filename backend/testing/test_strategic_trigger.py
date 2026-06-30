"""Tests for the coalescing strategic-refresh trigger (app.strategic_trigger).

Covers the anti-stacking guarantees: single-slot coalescing, the min-interval
floor, and consume/clear — plus the durable set via with_vision_locked.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import strategic_trigger as st
from app.models import Project
from app.time_utils import _utcnow

# ===========================================================================
# Pure helpers — no DB
# ===========================================================================


def test_pending_false_when_absent():
    assert st.strategic_refresh_pending(None) is False
    assert st.strategic_refresh_pending({}) is False
    # A non-dict value in the slot is not a valid marker.
    assert st.strategic_refresh_pending({"_strategic_refresh": "oops"}) is False


def test_pending_true_when_marker_present():
    vision = {"_strategic_refresh": {"requested_at": "x", "reason": "r", "count": 1}}
    assert st.strategic_refresh_pending(vision) is True


def test_due_false_when_not_pending():
    assert st.strategic_refresh_due({}, _utcnow()) is False


def test_due_true_when_pending_and_never_fired():
    vision = {"_strategic_refresh": {"reason": "r"}}
    assert st.strategic_refresh_due(vision, _utcnow(), min_interval_seconds=120) is True


def test_due_false_within_min_interval():
    """A refresh that fired 30s ago must not re-fire under a 120s floor."""
    now = _utcnow()
    vision = {
        "_strategic_refresh": {"reason": "r"},
        "_strategic_refresh_last_fired_at": (now - timedelta(seconds=30)).isoformat(),
    }
    assert st.strategic_refresh_due(vision, now, min_interval_seconds=120) is False


def test_due_true_after_min_interval_elapsed():
    now = _utcnow()
    vision = {
        "_strategic_refresh": {"reason": "r"},
        "_strategic_refresh_last_fired_at": (now - timedelta(seconds=200)).isoformat(),
    }
    assert st.strategic_refresh_due(vision, now, min_interval_seconds=120) is True


def test_due_fails_open_on_malformed_last_fired():
    """A garbage timestamp must not wedge the trigger — fail open to firing."""
    vision = {
        "_strategic_refresh": {"reason": "r"},
        "_strategic_refresh_last_fired_at": "not-a-timestamp",
    }
    assert st.strategic_refresh_due(vision, _utcnow(), min_interval_seconds=120) is True


def test_clear_pops_marker_and_stamps_fire_time():
    now = _utcnow()
    vision = {"_strategic_refresh": {"reason": "r"}, "other": 1}
    st.clear_strategic_refresh(vision, now)
    assert "_strategic_refresh" not in vision
    assert vision["_strategic_refresh_last_fired_at"] == now.isoformat()
    assert vision["other"] == 1  # untouched


def test_clear_persists_operator_origin_for_receipt():
    """Consuming an operator-origin marker must preserve its origin in
    _strategic_refresh_last_fired_origin so the UI can attribute receipts
    (only operator-origin consumes show 'Steer picked up')."""
    now = _utcnow()
    vision = {
        "_strategic_refresh": {
            "reason": "r",
            "origin": st.ORIGIN_OPERATOR,
        }
    }
    st.clear_strategic_refresh(vision, now)
    assert "_strategic_refresh" not in vision
    assert vision["_strategic_refresh_last_fired_origin"] == st.ORIGIN_OPERATOR
    assert st.strategic_refresh_last_fired_origin(vision) == st.ORIGIN_OPERATOR


def test_clear_persists_qualification_origin_for_attribution():
    """Consuming a qualification-origin marker must preserve its origin so the UI
    can withhold the 'Steer picked up' receipt (automated refreshes don't show)."""
    now = _utcnow()
    vision = {
        "_strategic_refresh": {
            "reason": "r",
            "origin": st.ORIGIN_QUALIFICATION,
        }
    }
    st.clear_strategic_refresh(vision, now)
    assert vision["_strategic_refresh_last_fired_origin"] == st.ORIGIN_QUALIFICATION
    assert st.strategic_refresh_last_fired_origin(vision) == st.ORIGIN_QUALIFICATION


def test_clear_defaults_to_qualification_for_legacy_marker():
    """A marker written before origin existed defaults to qualification on clear."""
    now = _utcnow()
    vision = {"_strategic_refresh": {"reason": "r"}}
    st.clear_strategic_refresh(vision, now)
    assert vision["_strategic_refresh_last_fired_origin"] == st.ORIGIN_QUALIFICATION


def test_clear_noop_when_no_marker_does_not_stamp():
    """Clearing with nothing pending must not stamp a fire time (so the floor
    measures refresh-driven fires, not ordinary cadence spawns)."""
    vision = {"other": 1}
    st.clear_strategic_refresh(vision, _utcnow())
    assert "_strategic_refresh_last_fired_at" not in vision


# ===========================================================================
# Origin discriminator + breaker applicability + notified flag — pure helpers
# ===========================================================================


def test_origin_none_when_no_marker():
    assert st.strategic_refresh_origin(None) is None
    assert st.strategic_refresh_origin({}) is None


def test_origin_defaults_to_qualification_for_legacy_marker():
    """A marker written before ``origin`` existed must read as qualification, so
    the legacy park-on-broken-loop behavior is preserved."""
    vision = {"_strategic_refresh": {"reason": "r", "count": 1}}
    assert st.strategic_refresh_origin(vision) == st.ORIGIN_QUALIFICATION


def test_origin_reflects_operator_marker():
    vision = {"_strategic_refresh": {"reason": "r", "origin": st.ORIGIN_OPERATOR}}
    assert st.strategic_refresh_origin(vision) == st.ORIGIN_OPERATOR


def test_breaker_applies_to_qualification_not_operator():
    qual = {"_strategic_refresh": {"reason": "r", "origin": st.ORIGIN_QUALIFICATION}}
    oper = {"_strategic_refresh": {"reason": "r", "origin": st.ORIGIN_OPERATOR}}
    legacy = {"_strategic_refresh": {"reason": "r"}}
    assert st.refresh_breaker_applies(qual) is True
    assert st.refresh_breaker_applies(legacy) is True  # legacy → qualification
    assert st.refresh_breaker_applies(oper) is False  # operator bypasses breaker
    assert st.refresh_breaker_applies({}) is False  # nothing pending → moot


def test_notified_flag_set_and_read():
    vision = {"_strategic_refresh": {"reason": "r", "origin": st.ORIGIN_OPERATOR}}
    assert st.strategic_refresh_notified(vision) is False
    st.mark_strategic_refresh_notified(vision)
    assert st.strategic_refresh_notified(vision) is True
    assert vision["_strategic_refresh"]["notified"] is True


def test_mark_notified_noop_without_marker():
    vision = {"other": 1}
    st.mark_strategic_refresh_notified(vision)  # must not raise / create a slot
    assert "_strategic_refresh" not in vision


def test_last_fired_origin_none_when_never_consumed():
    """No last_fired_origin when the marker has never been consumed."""
    assert st.strategic_refresh_last_fired_origin(None) is None
    assert st.strategic_refresh_last_fired_origin({}) is None
    # Pending marker with no fire history.
    vision = {"_strategic_refresh": {"reason": "r"}}
    assert st.strategic_refresh_last_fired_origin(vision) is None


def test_last_fired_origin_reflects_consumed_marker():
    """The last_fired_origin field shows the origin of the most recently
    consumed refresh (used by UI to attribute receipts)."""
    vision = {"_strategic_refresh_last_fired_origin": st.ORIGIN_OPERATOR}
    assert st.strategic_refresh_last_fired_origin(vision) == st.ORIGIN_OPERATOR
    vision = {"_strategic_refresh_last_fired_origin": st.ORIGIN_QUALIFICATION}
    assert st.strategic_refresh_last_fired_origin(vision) == st.ORIGIN_QUALIFICATION


def test_clear_drops_origin_and_notified_on_consume():
    """Consuming clears the whole marker (origin + notified), so a fresh request
    starts clean."""
    now = _utcnow()
    vision = {
        "_strategic_refresh": {
            "reason": "r",
            "origin": st.ORIGIN_OPERATOR,
            "notified": True,
        }
    }
    st.clear_strategic_refresh(vision, now)
    assert "_strategic_refresh" not in vision
    assert st.strategic_refresh_origin(vision) is None


# ===========================================================================
# Operator arming classifier — pure, no DB
# ===========================================================================


def test_classify_passive_note():
    """A normal NOTE is passive, including routine digest event types."""
    from app.models import MessageType

    assert (
        st.classify_operator_arming(MessageType.NOTE) == st.ARMING_PASSIVE_NOTE
    )
    assert (
        st.classify_operator_arming(MessageType.NOTE, event_type="pr_merged")
        == st.ARMING_PASSIVE_NOTE
    )


def test_classify_question_answer_note_as_steering():
    """An operator answer to a Strategic question is active steering."""
    from app.models import MessageType

    assert (
        st.classify_operator_arming(MessageType.NOTE, event_type="question_answer")
        == st.ARMING_STEERING
    )


def test_classify_steering_for_nonpassive_without_event():
    """A non-passive operator type with no event_type is genuine steering."""
    from app.models import MessageType

    assert (
        st.classify_operator_arming(MessageType.FEEDBACK) == st.ARMING_STEERING
    )


def test_classify_routine_event_for_nonpassive_with_routine_event():
    """A non-passive type carrying a routine/system digest event never wakes."""
    from app.models import MessageType

    assert (
        st.classify_operator_arming(
            MessageType.FEEDBACK, event_type="review_feedback"
        )
        == st.ARMING_ROUTINE_EVENT
    )


def test_classify_steering_for_nonroutine_event():
    """A non-passive type with a non-routine event_type still steers."""
    from app.models import MessageType

    assert (
        st.classify_operator_arming(
            MessageType.FEEDBACK, event_type="not_a_routine_event"
        )
        == st.ARMING_STEERING
    )


# ===========================================================================
# Durable set + coalescing — with DB
# ===========================================================================


async def _make_project(db_session) -> Project:
    project = Project(
        name="trigger-test",
        user_id="trigger-user",
        github_repo="o/r",
        vision={"purpose": "test"},
        autonomous_enabled=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_request_sets_durable_marker(db_session):
    project = await _make_project(db_session)
    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await db_session.refresh(project)
    marker = (project.vision or {}).get("_strategic_refresh")
    assert marker is not None
    assert marker["reason"] == "post_qualification:promoted"
    assert marker["count"] == 1
    assert marker["requested_at"]  # stamped


@pytest.mark.asyncio
async def test_request_is_single_slot_coalesced(db_session):
    """Many requests collapse into ONE marker (cannot stack); count is the only
    thing that grows, and the original requested_at is preserved."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await db_session.refresh(project)
    first_at = project.vision["_strategic_refresh"]["requested_at"]

    for _ in range(5):
        await st.request_strategic_refresh(db_session, project.id, "post_qualification:not_promoted")
        await db_session.commit()
    await db_session.refresh(project)

    marker = project.vision["_strategic_refresh"]
    assert marker["count"] == 6  # coalesced, not 6 separate markers
    assert marker["requested_at"] == first_at  # original request time preserved
    assert marker["reason"] == "post_qualification:not_promoted"  # latest reason


@pytest.mark.asyncio
async def test_request_defaults_origin_to_qualification(db_session):
    """A positional 3-arg call (the release_qualifier path) must default origin to
    qualification, preserving the existing breaker-park behavior."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await db_session.refresh(project)
    marker = project.vision["_strategic_refresh"]
    assert marker["origin"] == st.ORIGIN_QUALIFICATION
    assert st.refresh_breaker_applies(project.vision) is True


@pytest.mark.asyncio
async def test_request_qualification_uses_skip_locked_policy(db_session):
    """The qualification path must keep the best-effort skip-locked lock policy."""
    project = await _make_project(db_session)

    with patch.object(st, "with_vision_locked", AsyncMock(return_value=True)) as spy:
        await st.request_strategic_refresh(
            db_session, project.id, "post_qualification:promoted"
        )

    assert spy.await_count == 1
    assert spy.await_args.kwargs["skip_locked"] is True


@pytest.mark.asyncio
async def test_request_persists_operator_origin(db_session):
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert st.strategic_refresh_origin(project.vision) == st.ORIGIN_OPERATOR
    assert st.refresh_breaker_applies(project.vision) is False


@pytest.mark.asyncio
async def test_operator_origin_is_sticky_when_coalesced(db_session):
    """An operator request must not be downgraded by a later automated refresh,
    and re-arming must clear a prior one-time deferral notice."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    await db_session.refresh(project)
    # Simulate that the deferral notice was already emitted.
    project.vision["_strategic_refresh"]["notified"] = True
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "vision")
    await db_session.commit()

    # A later automated (qualification) refresh coalesces in.
    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await db_session.refresh(project)

    marker = project.vision["_strategic_refresh"]
    assert marker["origin"] == st.ORIGIN_OPERATOR  # sticky — not downgraded
    assert "notified" not in marker  # re-armed so a fresh deferral can surface
    assert marker["count"] == 2


@pytest.mark.asyncio
async def test_operator_origin_upgrades_existing_qualification(db_session):
    """A qualification marker upgraded by a later operator request becomes
    operator-origin (breaker-bypassing)."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await st.request_strategic_refresh(
        db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert st.strategic_refresh_origin(project.vision) == st.ORIGIN_OPERATOR
    assert st.refresh_breaker_applies(project.vision) is False


# ===========================================================================
# Steering excerpt persistence (operator message text on the marker)
# ===========================================================================


@pytest.mark.asyncio
async def test_request_persists_operator_steering_excerpt(db_session):
    """An operator request stores the message text on the marker so the steering
    directive survives the volatile recent-messages snapshot aging out."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "operator_message",
        origin=st.ORIGIN_OPERATOR,
        steering_excerpt="Pivot toward billing reliability.",
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert (
        st.strategic_refresh_excerpt(project.vision)
        == "Pivot toward billing reliability."
    )


@pytest.mark.asyncio
async def test_excerpt_truncated_to_max_chars(db_session):
    """A long excerpt is truncated to the per-message budget on the marker."""
    project = await _make_project(db_session)
    long_text = "z" * 2000
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "operator_message",
        origin=st.ORIGIN_OPERATOR,
        steering_excerpt=long_text,
    )
    await db_session.commit()
    await db_session.refresh(project)
    stored = st.strategic_refresh_excerpt(project.vision)
    assert stored is not None
    assert len(stored) == st._STEERING_EXCERPT_MAX_CHARS


@pytest.mark.asyncio
async def test_coalesce_replaces_excerpt_with_newer(db_session):
    """The most recent operator message wins: a newer excerpt replaces the older
    one when two operator messages arm in quick succession."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "operator_message",
        origin=st.ORIGIN_OPERATOR,
        steering_excerpt="First steering message.",
    )
    await db_session.commit()
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "operator_message",
        origin=st.ORIGIN_OPERATOR,
        steering_excerpt="Second, newer steering message.",
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert (
        st.strategic_refresh_excerpt(project.vision)
        == "Second, newer steering message."
    )


@pytest.mark.asyncio
async def test_qualification_origin_stores_no_excerpt(db_session):
    """A non-operator (qualification) refresh never stores a steering excerpt,
    even if one is somehow supplied."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "post_qualification:promoted",
        steering_excerpt="should be ignored",
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert st.strategic_refresh_excerpt(project.vision) is None
    assert "excerpt" not in project.vision["_strategic_refresh"]


@pytest.mark.asyncio
async def test_automated_coalesce_preserves_operator_excerpt(db_session):
    """A coalescing automated refresh (no excerpt) leaves an operator excerpt
    already present on the sticky-operator marker untouched."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session,
        project.id,
        "operator_message",
        origin=st.ORIGIN_OPERATOR,
        steering_excerpt="Operator steering intent.",
    )
    await db_session.commit()
    # A later automated refresh coalesces in with no excerpt.
    await st.request_strategic_refresh(
        db_session, project.id, "post_qualification:promoted"
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert (
        st.strategic_refresh_excerpt(project.vision) == "Operator steering intent."
    )


@pytest.mark.asyncio
async def test_maybe_arm_operator_refresh_passes_message_content(db_session):
    """maybe_arm_operator_refresh forwards the message text as the steering
    excerpt for genuine steering messages."""
    from app.models import MessageType

    project = await _make_project(db_session)
    armed = await st.maybe_arm_operator_refresh(
        db_session,
        project.id,
        MessageType.FEEDBACK,
        message_content="Operator: prioritize the billing fix.",
    )
    assert armed == st.ARMING_STEERING
    await db_session.commit()
    await db_session.refresh(project)
    assert (
        st.strategic_refresh_excerpt(project.vision)
        == "Operator: prioritize the billing fix."
    )


@pytest.mark.asyncio
async def test_request_returns_true_on_persist(db_session):
    """request_strategic_refresh reports True when the marker was actually
    written — the signal callers surface as a truthful operator receipt."""
    project = await _make_project(db_session)
    persisted = await st.request_strategic_refresh(
        db_session, project.id, "post_qualification:promoted"
    )
    assert persisted is True
    await db_session.commit()
    await db_session.refresh(project)
    assert st.strategic_refresh_pending(project.vision) is True


@pytest.mark.asyncio
async def test_request_returns_false_when_lock_exhausted(db_session):
    """When the operator-steering (skip_locked=False) acquire never persists the
    marker — every with_vision_locked attempt returns False (row contended) —
    request_strategic_refresh reports False and writes no marker."""
    project = await _make_project(db_session)
    with patch(
        "app.strategic_trigger.with_vision_locked",
        new=AsyncMock(return_value=False),
    ):
        persisted = await st.request_strategic_refresh(
            db_session,
            project.id,
            "operator_message",
            origin=st.ORIGIN_OPERATOR,
            skip_locked=False,
        )
    assert persisted is False
    await db_session.refresh(project)
    assert st.strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_maybe_arm_returns_not_armed_when_request_fails(db_session):
    """A genuine steering message whose marker cannot be persisted yields the
    truthful ARMING_NOT_ARMED outcome — never a false ARMING_STEERING promise."""
    from app.models import MessageType

    project = await _make_project(db_session)
    with patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=False),
    ):
        outcome = await st.maybe_arm_operator_refresh(
            db_session,
            project.id,
            MessageType.FEEDBACK,
            message_content="Operator: wake up.",
        )
    assert outcome == st.ARMING_NOT_ARMED


@pytest.mark.asyncio
async def test_maybe_arm_returns_steering_when_request_succeeds(db_session):
    """A genuine steering message whose marker IS persisted yields
    ARMING_STEERING (the request_strategic_refresh return is threaded through)."""
    from app.models import MessageType

    project = await _make_project(db_session)
    with patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    ):
        outcome = await st.maybe_arm_operator_refresh(
            db_session,
            project.id,
            MessageType.FEEDBACK,
            message_content="Operator: wake up.",
        )
    assert outcome == st.ARMING_STEERING


@pytest.mark.asyncio
async def test_maybe_arm_returns_passive_note_without_requesting(db_session):
    """A passive NOTE returns its classification verbatim and never calls
    request_strategic_refresh (no wake attempt for a non-steering message)."""
    from app.models import MessageType

    project = await _make_project(db_session)
    with patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    ) as req:
        outcome = await st.maybe_arm_operator_refresh(
            db_session, project.id, MessageType.NOTE, message_content="fyi"
        )
    assert outcome == st.ARMING_PASSIVE_NOTE
    req.assert_not_awaited()


# ===========================================================================
# Operator cancel — retract an armed steer without leaving a receipt
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_clears_pending_marker(db_session):
    """Cancelling an armed steer removes the pending marker so it will not fire."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert st.strategic_refresh_pending(project.vision) is True

    cleared = await st.cancel_strategic_refresh(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    assert cleared is True
    assert st.strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_cancel_does_not_leave_receipt(db_session):
    """A cancelled steer never ran: no last_fired_at/origin stamp (no 'Steer
    picked up' receipt) and the min-interval floor is not advanced."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_steering", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()

    await st.cancel_strategic_refresh(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    assert "_strategic_refresh_last_fired_at" not in (project.vision or {})
    assert st.strategic_refresh_last_fired_origin(project.vision) is None


@pytest.mark.asyncio
async def test_operator_count_initial_value_for_fresh_markers(db_session):
    """Fresh operator markers start with operator_count=1; fresh qualification
    markers start with operator_count=0 so the UI coalesce badge is truthful."""
    project = await _make_project(db_session)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_message", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert project.vision["_strategic_refresh"]["operator_count"] == 1

    # Clear and re-arm with qualification origin
    project.vision.pop("_strategic_refresh", None)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(project, "vision")
    await db_session.commit()

    await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
    await db_session.commit()
    await db_session.refresh(project)
    assert project.vision["_strategic_refresh"]["operator_count"] == 0


@pytest.mark.asyncio
async def test_operator_count_tracks_only_operator_coalesces(db_session):
    """Automated (qualification) coalesces must NOT increment operator_count;
    only genuine operator requests count toward the operator-steer tally."""
    project = await _make_project(db_session)
    # First request: operator (operator_count = 1)
    await st.request_strategic_refresh(
        db_session, project.id, "operator_message", origin=st.ORIGIN_OPERATOR
    )
    await db_session.commit()
    # Three automated coalesces — operator_count must stay at 1
    for _ in range(3):
        await st.request_strategic_refresh(db_session, project.id, "post_qualification:promoted")
        await db_session.commit()
    await db_session.refresh(project)

    marker = project.vision["_strategic_refresh"]
    assert marker["count"] == 4         # total coalesces tracked as before
    assert marker["operator_count"] == 1  # only the original operator request


@pytest.mark.asyncio
async def test_operator_count_increments_on_each_operator_coalesce(db_session):
    """Each additional operator request increments operator_count so the UI can
    show '+N steers coalesced' accurately for back-to-back human steers."""
    project = await _make_project(db_session)
    for _ in range(4):
        await st.request_strategic_refresh(
            db_session, project.id, "operator_message", origin=st.ORIGIN_OPERATOR
        )
        await db_session.commit()
    await db_session.refresh(project)

    marker = project.vision["_strategic_refresh"]
    assert marker["count"] == 4
    assert marker["operator_count"] == 4


@pytest.mark.asyncio
async def test_cancel_is_idempotent_noop_when_nothing_armed(db_session):
    """Cancelling with no marker pending is a harmless no-op returning False,
    so a duplicate/late client retry cannot error."""
    project = await _make_project(db_session)

    cleared = await st.cancel_strategic_refresh(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    assert cleared is False
    assert st.strategic_refresh_pending(project.vision) is False
