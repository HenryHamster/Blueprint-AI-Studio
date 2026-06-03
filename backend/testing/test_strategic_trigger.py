"""Tests for the coalescing strategic-refresh trigger (app.strategic_trigger).

Covers the anti-stacking guarantees: single-slot coalescing, the min-interval
floor, and consume/clear — plus the durable set via with_vision_locked.
"""

from __future__ import annotations

from datetime import timedelta

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


def test_clear_noop_when_no_marker_does_not_stamp():
    """Clearing with nothing pending must not stamp a fire time (so the floor
    measures refresh-driven fires, not ordinary cadence spawns)."""
    vision = {"other": 1}
    st.clear_strategic_refresh(vision, _utcnow())
    assert "_strategic_refresh_last_fired_at" not in vision


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
