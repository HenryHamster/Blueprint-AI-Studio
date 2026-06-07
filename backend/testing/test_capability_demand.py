"""Tests for app.capability_demand — the missing-foundation demand producer."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.capability_demand import (
    DEFAULT_LOOKBACK,
    collect_missing_capability_demand,
    fetch_missing_demand_for_spawn,
    missing_demand_prompt_hint,
    unresolved_capability_needs,
)
from app.models import TaskStatus
from app.reaper_sweeps import (
    GIVE_UP_FOUNDATION_DIED,
    GIVE_UP_NEVER_SATISFIABLE,
    MISSING_FOUNDATION_MARKER_KEY,
    UNRESUMABLE_CANCEL_CONTEXT_KEY,
)
from testing.factories import make_project, make_task, _utcnow


def _blocked(project_id, need, *, direction_id=None, parked=0, blocked_at=None, **kw):
    """A BLOCKED dependent waiting on a single machine-resolvable ``need``."""
    ctx = {
        "blocker": {
            "source": "agent",
            "waiting_on": need.get("ref"),
            "reason": "BLOCKER: waiting on foundation",
            "need": need,
            "needs": [need],
        },
    }
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    if parked:
        ctx["_blocked_parked_reevals"] = parked
    return make_task(
        project_id=project_id,
        status=TaskStatus.BLOCKED,
        blocked_at=blocked_at or (_utcnow() - timedelta(hours=4)),
        context=ctx,
        **kw,
    )


# ── Pure helper: unresolved_capability_needs ─────────────────────────────────


def test_unresolved_reports_capability_with_no_provider():
    """A capability need that no active/completed candidate advertises is missing."""
    pid = None
    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    task = _blocked(pid, need)
    out = unresolved_capability_needs(task, active=[], completed=[])
    assert out == [need]


def test_unresolved_empty_when_active_candidate_provides_capability():
    """A live ACTIVE candidate advertising the capability marker resolves the need."""
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(None, need)
    provider = make_task(
        project_id=task.project_id,
        status=TaskStatus.IN_PROGRESS,
        context={"provides_capability": "auth-subsystem"},
    )
    out = unresolved_capability_needs(task, active=[provider], completed=[])
    assert out == []


def test_unresolved_excluded_by_shipped_completed_foundation():
    """A capability satisfied by a SHIPPED (pr_merged) COMPLETED foundation is not demand."""
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(None, need)
    shipped = make_task(
        project_id=task.project_id,
        status=TaskStatus.COMPLETED,
        context={"provides_capability": "auth-subsystem", "pr_merged": True},
    )
    out = unresolved_capability_needs(task, active=[], completed=[shipped])
    assert out == []


def test_unresolved_included_when_completed_foundation_unshipped():
    """A COMPLETED foundation with NO merge-unblock signal never shipped → still demand."""
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(None, need)
    unmerged = make_task(
        project_id=task.project_id,
        status=TaskStatus.COMPLETED,
        context={"provides_capability": "auth-subsystem"},  # no pr_merged
    )
    out = unresolved_capability_needs(task, active=[], completed=[unmerged])
    assert out == [need]


def test_unresolved_task_need_absent_vs_resolved():
    """A kind=task (finding-hash) need is demand when its foundation is absent and
    resolved once an active task carries that id."""
    foundation = make_task(status=TaskStatus.IN_PROGRESS)
    need = {"kind": "task", "ref": str(foundation.id)}
    task = _blocked(foundation.project_id, need)

    # Absent foundation → reported.
    assert unresolved_capability_needs(task, active=[], completed=[]) == [need]
    # Active task with the matching id → resolved.
    assert (
        unresolved_capability_needs(task, active=[foundation], completed=[]) == []
    )


def test_unresolved_skips_pr_need():
    """A pr need names an external ref, not a foundation — never demand."""
    need = {"kind": "pr", "ref": "1234"}
    task = _blocked(None, need)
    assert unresolved_capability_needs(task, active=[], completed=[]) == []


# ── Collector: collect_missing_capability_demand ─────────────────────────────


async def _make_project(db_session):
    project = make_project(name="cap-demand", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_collect_aggregates_blocked_dependents_of_one_capability(db_session):
    """Multiple BLOCKED dependents of the same capability collapse to ONE demand
    entry with the right dependent_count and union of direction_ids."""
    project = await _make_project(db_session)
    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    now = _utcnow()
    d1 = _blocked(
        project.id, need, direction_id="d-aaa", parked=2,
        blocked_at=now - timedelta(hours=10),
    )
    d2 = _blocked(
        project.id, need, direction_id="d-bbb", parked=1,
        blocked_at=now - timedelta(hours=3),
    )
    # Same capability, different ref casing → same identity.
    d3 = _blocked(
        project.id, {"kind": "capability", "ref": "Auth-Subsystem"},
        direction_id="d-aaa", blocked_at=now - timedelta(hours=1),
    )
    db_session.add_all([d1, d2, d3])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id, now=now)

    assert result["total"] == 1
    entry = result["demands"][0]
    assert entry["kind"] == "capability"
    assert entry["dependent_count"] == 3
    assert entry["still_blocked"] == 3
    assert entry["already_cancelled"] == 0
    assert sorted(entry["direction_ids"]) == ["d-aaa", "d-bbb"]
    assert entry["label"] == "Auth subsystem"
    assert entry["max_parked_reevals"] == 2
    # Oldest blocker's blocked_at wins.
    assert entry["oldest_blocked_at"] == (now - timedelta(hours=10)).isoformat()
    assert len(entry["sample_task_ids"]) == 3


@pytest.mark.asyncio
async def test_collect_not_reported_when_foundation_completed(db_session):
    """A dependent whose capability is satisfied by a shipped completed foundation
    is NOT reported as demand."""
    project = await _make_project(db_session)
    need = {"kind": "capability", "ref": "auth-subsystem"}
    dependent = _blocked(project.id, need, direction_id="d-aaa")
    shipped = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"provides_capability": "auth-subsystem", "pr_merged": True},
    )
    db_session.add_all([dependent, shipped])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id)
    assert result["total"] == 0
    assert result["demands"] == []


@pytest.mark.asyncio
async def test_collect_is_project_scoped(db_session):
    """Blocked dependents in another project never leak into the demand."""
    project = await _make_project(db_session)
    other = await _make_project(db_session)
    need = {"kind": "capability", "ref": "auth-subsystem"}
    mine = _blocked(project.id, need, direction_id="d-aaa")
    theirs = _blocked(other.id, need, direction_id="d-zzz")
    db_session.add_all([mine, theirs])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id)
    assert result["total"] == 1
    assert result["demands"][0]["dependent_count"] == 1
    assert result["demands"][0]["direction_ids"] == ["d-aaa"]


@pytest.mark.asyncio
async def test_collect_folds_giveup_cancelled_within_lookback(db_session):
    """A give-up-CANCELLED dependent (missing-foundation retirement) within the
    lookback surfaces as already_cancelled demand and aggregates with a live
    blocked dependent of the same capability; an older marker and a pr-need marker
    are both excluded."""
    project = await _make_project(db_session)
    now = _utcnow()
    cap_need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth"}

    live = _blocked(project.id, cap_need, direction_id="d-live")

    cancelled_recent = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_NEVER_SATISFIABLE,
                "need": cap_need,
                "direction_id": "d-dead",
                "at": (now - timedelta(hours=2)).isoformat(),
                MISSING_FOUNDATION_MARKER_KEY: True,
            }
        },
    )
    # Older than the lookback window → excluded (marker 'at' precedes window).
    old_at = (now - DEFAULT_LOOKBACK - timedelta(days=1)).isoformat()
    cancelled_old = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        updated_at=now - DEFAULT_LOOKBACK - timedelta(days=1),
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_NEVER_SATISFIABLE,
                "need": cap_need,
                "direction_id": "d-old",
                "at": old_at,
                MISSING_FOUNDATION_MARKER_KEY: True,
            }
        },
    )
    # A pr-need give-up is not a foundation → excluded.
    cancelled_pr = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_FOUNDATION_DIED,
                "need": {"kind": "pr", "ref": "99"},
                "direction_id": "d-pr",
                "at": (now - timedelta(hours=1)).isoformat(),
            }
        },
    )
    db_session.add_all([live, cancelled_recent, cancelled_old, cancelled_pr])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id, now=now)

    assert result["total"] == 1
    entry = result["demands"][0]
    assert entry["kind"] == "capability"
    assert entry["dependent_count"] == 2
    assert entry["still_blocked"] == 1
    assert entry["already_cancelled"] == 1
    assert sorted(entry["direction_ids"]) == ["d-dead", "d-live"]


# ── Reaper marker: missing-foundation flag is stamped additively ─────────────


@pytest.mark.asyncio
async def test_reaper_stamps_missing_foundation_flag_for_capability(db_session, mocker):
    """A never_satisfiable give-up of a capability need stamps the additive
    missing_foundation flag onto the marker, without disturbing existing keys."""
    from app.reaper_sweeps import _cancel_blocker_as_dead

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(db_session)
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(project.id, need, direction_id="d-aaa")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _cancel_blocker_as_dead(
        task,
        db_session,
        detail="never satisfiable",
        message="Cancelled — blocked need never became satisfiable",
        pending_broadcasts=[],
        give_up_class=GIVE_UP_NEVER_SATISFIABLE,
        need=need,
    )

    marker = (task.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert marker[MISSING_FOUNDATION_MARKER_KEY] is True
    # Existing keys are intact (purely additive).
    assert marker["give_up_class"] == GIVE_UP_NEVER_SATISFIABLE
    assert marker["need"] == need
    assert marker["direction_id"] == "d-aaa"


@pytest.mark.asyncio
async def test_reaper_no_missing_foundation_flag_for_foundation_died(db_session, mocker):
    """A foundation-DIED give-up is not a missing-foundation retirement — the flag
    is absent so the collector does not double-count a foundation that existed."""
    from app.reaper_sweeps import _cancel_blocker_as_dead

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(db_session)
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(project.id, need, direction_id="d-aaa")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _cancel_blocker_as_dead(
        task,
        db_session,
        detail="foundation died",
        message="Cancelled — needed foundation terminally failed/cancelled",
        pending_broadcasts=[],
        give_up_class=GIVE_UP_FOUNDATION_DIED,
        need=need,
    )

    marker = (task.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert MISSING_FOUNDATION_MARKER_KEY not in marker
    assert marker["give_up_class"] == GIVE_UP_FOUNDATION_DIED


# ── Spawn-facing helpers: prompt hint + fail-open wrapper ────────────────────


def test_prompt_hint_none_when_no_demand():
    """No demand → no hint, so spawners can omit the field/hint entirely."""
    assert missing_demand_prompt_hint(None) is None
    assert missing_demand_prompt_hint({"total": 0, "demands": []}) is None


def test_prompt_hint_names_top_demands_and_frames_decision():
    """The hint names the demand and frames the create-or-retire decision."""
    demand = {
        "total": 1,
        "demands": [
            {
                "kind": "capability",
                "ref": "auth-subsystem",
                "label": "Auth subsystem",
                "dependent_count": 3,
                "still_blocked": 2,
                "already_cancelled": 1,
            }
        ],
    }
    hint = missing_demand_prompt_hint(demand)
    assert hint is not None
    assert "Missing-foundation demand" in hint
    # Names the demand (label preferred) and the dependent split.
    assert "Auth subsystem" in hint
    assert "3 stranded dependent" in hint
    # Frames both options without prescribing one.
    assert "create" in hint.lower()
    assert "retir" in hint.lower()


def test_prompt_hint_truncates_and_notes_overflow():
    """Only the top entries are named inline; the rest are summarized as a count."""
    demands = [
        {
            "kind": "capability",
            "ref": f"cap-{i}",
            "label": f"Cap {i}",
            "dependent_count": 10 - i,
            "still_blocked": 10 - i,
            "already_cancelled": 0,
        }
        for i in range(7)
    ]
    hint = missing_demand_prompt_hint({"total": 7, "demands": demands})
    # 5 named inline + a "+2 more" overflow note.
    assert hint.count("- capability:") == 5
    assert "+2 more" in hint


@pytest.mark.asyncio
async def test_fetch_wrapper_returns_collector_result(db_session):
    """fetch_missing_demand_for_spawn returns the same shape as the collector."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    blocked = _blocked(project.id, need)
    db_session.add(blocked)
    await db_session.commit()

    direct = await collect_missing_capability_demand(db_session, project.id)
    via_wrapper = await fetch_missing_demand_for_spawn(db_session, project.id)
    assert via_wrapper == direct
    assert via_wrapper["total"] == 1


@pytest.mark.asyncio
async def test_fetch_wrapper_fails_open(mocker):
    """Any collector error is swallowed → empty demand shape, never raised."""
    mocker.patch(
        "app.capability_demand.collect_missing_capability_demand",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )
    out = await fetch_missing_demand_for_spawn(object(), "p-1")
    assert out == {"total": 0, "demands": []}
