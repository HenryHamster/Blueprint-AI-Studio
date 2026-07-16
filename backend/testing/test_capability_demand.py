"""Tests for app.capability_demand — the missing-foundation demand producer."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.capability_demand import (
    DEFAULT_LOOKBACK,
    collect_missing_capability_demand,
    compute_unproven_demanded_capabilities,
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


def test_unresolved_floor_marker_does_not_mask_missing_human_capability():
    """The namespaced floor marker must not satisfy a real capability ref."""
    need = {"kind": "capability", "ref": "auth-subsystem"}
    task = _blocked(None, need)
    provider = make_task(
        project_id=task.project_id,
        status=TaskStatus.IN_PROGRESS,
        title="Build auth subsystem foundation",
        context={},
    )

    out = unresolved_capability_needs(task, active=[provider], completed=[])
    assert out == [need]


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
async def test_collect_hollow_compute_doc_stays_visible_but_grounded_doc_suppresses(
    db_session,
):
    from app.models import KnowledgeDocument

    # Task ref must be a 16-hex finding hash for normalize_prerequisite to accept it.
    task_ref = "cafe0123deadbeef"
    need = {"kind": "task", "ref": task_ref}

    hollow_project = await _make_project(db_session)
    hollow_producer = make_task(
        project_id=hollow_project.id,
        status=TaskStatus.COMPLETED,
        context={"finding_hash": task_ref},
    )
    hollow_dependent = _blocked(hollow_project.id, need)
    hollow_doc = KnowledgeDocument(
        project_id=hollow_project.id,
        source_task_id=hollow_producer.id,
        source_kind="compute_experiment",
        title="hollow compute",
        summary="hollow compute",
        body="hollow compute",
        topic=task_ref,
        source_citations=[],
        forward_quality_verdict={
            "analysis_mode": "fallback",
            "citation_grounding_miss": True,
            "probes": {
                "grounding": {
                    "checked": True,
                    "file_citations": 1,
                    "grounded": 0,
                    "unresolved": ["https://signed.example.com/jobs/result.json"],
                }
            },
        },
    )

    grounded_project = await _make_project(db_session)
    grounded_producer = make_task(
        project_id=grounded_project.id,
        status=TaskStatus.COMPLETED,
        context={"finding_hash": task_ref},
    )
    grounded_dependent = _blocked(grounded_project.id, need)
    grounded_doc = KnowledgeDocument(
        project_id=grounded_project.id,
        source_task_id=grounded_producer.id,
        source_kind="compute_experiment",
        title="grounded compute",
        summary="grounded compute",
        body="grounded compute",
        topic=task_ref,
        source_citations=["gs://bucket/jobs/result.json"],
        forward_quality_verdict={
            "analysis_mode": "fallback",
            "outcome": "SPEND_CAP_EXCEEDED",
            "probes": {
                "grounding": {
                    "checked": True,
                    "file_citations": 1,
                    "grounded": 1,
                    "unresolved": [],
                }
            },
        },
    )

    db_session.add_all(
        [hollow_producer, hollow_dependent, grounded_producer, grounded_dependent]
    )
    await db_session.commit()
    db_session.add_all([hollow_doc, grounded_doc])
    await db_session.commit()

    hollow_result = await collect_missing_capability_demand(
        db_session, hollow_project.id
    )
    assert hollow_result["total"] == 1
    assert hollow_result["demands"][0]["ref"] == task_ref

    grounded_result = await collect_missing_capability_demand(
        db_session, grounded_project.id
    )
    assert grounded_result["total"] == 0
    assert grounded_result["demands"] == []


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
async def test_collect_excludes_title_like_task_ref_from_missing_foundation_demand(
    db_session,
):
    """A BLOCKED dependent whose kind=task need carries a TITLE-LIKE ref (free
    prose, not a task UUID / finding hash) must NOT surface as missing-foundation
    demand: a title-like task ref names no machine-resolvable foundation, so it
    can never be a creatable "missing task". An EXACT task ref (UUID) for an
    absent foundation, and a capability need, DO surface — proving the collector
    excludes only the malformed title-like task refs, not exact task demand."""
    import uuid as _uuid

    project = await _make_project(db_session)

    # Title-like kind=task refs — the malformed case this policy rejects. These
    # mimic legacy/hand-written rows that bypassed build_need's validation.
    title_like_a = _blocked(
        project.id,
        {"kind": "task", "ref": "Build the seeded evaluation harness"},
        direction_id="d-title-a",
    )
    title_like_b = _blocked(
        project.id,
        {"kind": "task", "ref": "auth-subsystem", "label": "Auth subsystem"},
        direction_id="d-title-b",
    )
    # Control 1: an EXACT (UUID) task ref whose foundation is absent → real demand.
    exact_task_ref = str(_uuid.uuid4())
    exact_dep = _blocked(
        project.id,
        {"kind": "task", "ref": exact_task_ref, "label": "the foundation"},
        direction_id="d-exact",
    )
    # Control 2: a capability need with no provider → real demand.
    cap_dep = _blocked(
        project.id,
        {"kind": "capability", "ref": "payment-core"},
        direction_id="d-cap",
    )
    db_session.add_all([title_like_a, title_like_b, exact_dep, cap_dep])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id)

    # Only the exact task ref and the capability need are demand; neither
    # title-like task ref appears under ANY identity.
    assert result["total"] == 2
    refs = {(e["kind"], e["ref"]) for e in result["demands"]}
    assert ("task", exact_task_ref) in refs
    assert ("capability", "payment-core") in refs
    assert all(
        e["ref"] not in ("Build the seeded evaluation harness", "auth-subsystem")
        for e in result["demands"]
    )
    assert all(e["kind"] != "task" or e["ref"] == exact_task_ref for e in result["demands"])


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


@pytest.mark.asyncio
async def test_collect_bounds_completed_scan_to_recency_window(db_session):
    """The COMPLETED/terminal candidate scan is recency-bounded so a long-lived
    project's full history is never loaded in one fat-row select (the digest-path
    wedge of 2026-06-15). A shipped foundation OLDER than the candidate window is
    not scanned, so it no longer suppresses a live blocker's demand — while an
    equivalent foundation INSIDE the window still suppresses it. Without the bound
    the ancient foundation would be fetched and the first assertion would read 0.
    """
    from app.capability_demand import _CANDIDATE_FETCH_LOOKBACK

    project = await _make_project(db_session)
    now = _utcnow()
    need = {"kind": "capability", "ref": "auth-subsystem"}
    dependent = _blocked(project.id, need, direction_id="d-aaa")

    # Shipped foundation, but last touched well before the candidate window opens.
    old_shipped = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        updated_at=now - _CANDIDATE_FETCH_LOOKBACK - timedelta(days=2),
        context={"provides_capability": "auth-subsystem", "pr_merged": True},
    )
    db_session.add_all([dependent, old_shipped])
    await db_session.commit()

    # Beyond-window foundation is not scanned → demand is (advisedly) reported.
    out_of_window = await collect_missing_capability_demand(
        db_session, project.id, now=now
    )
    assert out_of_window["total"] == 1
    assert out_of_window["demands"][0]["ref"] == "auth-subsystem"

    # An equivalent foundation INSIDE the window suppresses the demand again,
    # proving the bound excludes only the ancient rows, not recent foundations.
    recent_shipped = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        updated_at=now - timedelta(hours=1),
        context={"provides_capability": "auth-subsystem", "pr_merged": True},
    )
    db_session.add(recent_shipped)
    await db_session.commit()

    in_window = await collect_missing_capability_demand(
        db_session, project.id, now=now
    )
    assert in_window["total"] == 0


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


def test_prompt_hint_prefers_canonical_registry_name():
    """When a capability demand corresponds to a registry capability, the hint
    names it by its CANONICAL registry name so producers/consumers converge."""
    demand = {
        "total": 1,
        "demands": [
            {
                "kind": "capability",
                # Free-prose consumer ref that normalizes onto the registry name.
                "ref": "Seeded Eval Harness",
                "label": "Seeded evaluation harness (win rate + 95% CI)",
                "dependent_count": 2,
                "still_blocked": 2,
                "already_cancelled": 0,
            }
        ],
    }
    hint = missing_demand_prompt_hint(
        demand, registry_names=["seeded-eval-harness", "payment-core"]
    )
    assert hint is not None
    # The canonical registry name is shown ...
    assert "seeded-eval-harness" in hint
    # ... NOT the consumer's free-prose label.
    assert "Seeded evaluation harness (win rate + 95% CI)" not in hint


def test_prompt_hint_keeps_label_when_ref_is_off_registry():
    """A demand whose ref is NOT a registry capability keeps label-preferred text."""
    demand = {
        "total": 1,
        "demands": [
            {
                "kind": "capability",
                "ref": "mystery-cap",
                "label": "Mystery subsystem",
                "dependent_count": 1,
                "still_blocked": 1,
                "already_cancelled": 0,
            }
        ],
    }
    hint = missing_demand_prompt_hint(demand, registry_names=["seeded-eval-harness"])
    assert hint is not None
    assert "Mystery subsystem" in hint


def test_prompt_hint_without_registry_names_is_unchanged():
    """Omitting registry_names preserves today's label-preferred behavior exactly."""
    demand = {
        "total": 1,
        "demands": [
            {
                "kind": "capability",
                "ref": "seeded-eval-harness",
                "label": "Seeded eval harness",
                "dependent_count": 1,
                "still_blocked": 1,
                "already_cancelled": 0,
            }
        ],
    }
    assert "Seeded eval harness" in missing_demand_prompt_hint(demand)


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


@pytest.mark.asyncio
async def test_free_prose_demand_retires_when_kebab_foundation_lands(db_session):
    """A BLOCKED dependent needing a free-prose capability ref ('Seeded Eval Harness')
    yields ZERO demand when a shipped COMPLETED task advertises its kebab equivalent
    ('seeded-eval-harness'). The demand identity and matcher must use the same
    normalize_capability_name normalization so free-prose demand retires correctly."""
    project = await _make_project(db_session)

    # Dependent declares its need in free prose (spaces, mixed case).
    free_prose_need = {
        "kind": "capability",
        "ref": "Seeded Eval Harness",
        "label": "Seeded evaluation harness",
    }
    dependent = _blocked(project.id, free_prose_need, direction_id="d-eval")

    # Foundation advertises the canonical kebab name the hint/strategist uses.
    foundation = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"provides_capability": "seeded-eval-harness", "pr_merged": True},
    )
    db_session.add_all([dependent, foundation])
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id)

    # The kebab foundation satisfies the free-prose demand → zero remaining demand.
    assert result["total"] == 0
    assert result["demands"] == []


# ── compute_unproven_demanded_capabilities ───────────────────────────────────


def _problem_model(*, caps: list[dict]) -> dict:
    """Build a problem_model dict with capability entries.

    Each cap dict supports: name, status, backing_paths, evidence_refs.
    """
    return {"capabilities": caps}


def _demand_result(*, demands: list[dict]) -> dict:
    return {"total": len(demands), "demands": demands}


class TestComputeUnprovenDemandedCapabilities:
    """Pure-function tests for compute_unproven_demanded_capabilities."""

    def test_empty_demand_returns_empty(self):
        """No demand → empty result regardless of problem_model."""
        pm = _problem_model(caps=[
            {"name": "my-capability", "status": "working"},
        ])
        result = compute_unproven_demanded_capabilities(
            _demand_result(demands=[]), pm
        )
        assert result == []

    def test_empty_problem_model_returns_empty(self):
        """No problem_model → empty result."""
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "my-capability"},
        ])
        result = compute_unproven_demanded_capabilities(demand, {})
        assert result == []

    def test_proven_capability_excluded(self):
        """A demanded capability that IS proven is not in the intersection."""
        pm = _problem_model(caps=[
            {"name": "my-capability", "status": "working",
             "evidence_refs": [{"proven": True, "ref": "test", "source_id": "s1"}]},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "my-capability", "dependent_count": 2},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert result == []

    def test_unproven_demanded_included(self):
        """An unproven working capability that IS demanded → in intersection."""
        pm = _problem_model(caps=[
            {"name": "my-capability", "status": "working"},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "my-capability", "dependent_count": 3},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert len(result) == 1
        assert result[0]["ref"] == "my-capability"
        assert result[0]["proof_status"] == "unproven"
        assert result[0]["dependent_count"] == 3

    def test_multiple_demands_only_unproven_returned(self):
        """Among multiple demanded caps, only unproven ones are returned."""
        pm = _problem_model(caps=[
            {"name": "proven-cap", "status": "working",
             "evidence_refs": [{"proven": True, "ref": "t1", "source_id": "s1"}]},
            {"name": "unproven-cap", "status": "working"},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "proven-cap", "dependent_count": 1},
            {"kind": "capability", "ref": "unproven-cap", "dependent_count": 5},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert len(result) == 1
        assert result[0]["ref"] == "unproven-cap"

    def test_non_capability_kind_excluded(self):
        """Task-kind demands are excluded from the capability intersection."""
        pm = _problem_model(caps=[
            {"name": "my-capability", "status": "working"},
        ])
        demand = _demand_result(demands=[
            {"kind": "task", "ref": "some-task-id", "dependent_count": 1},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert result == []

    def test_no_backing_paths_in_intersection(self):
        """An unproven demanded cap with no backing_paths IS still in the
        intersection — the earning loop caller decides what to do next."""
        pm = _problem_model(caps=[
            {"name": "no-backing-cap", "status": "working"},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "no-backing-cap", "dependent_count": 2},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert len(result) == 1
        assert result[0]["ref"] == "no-backing-cap"

    def test_normalization_matches_flexibly(self):
        """Free-prose ref normalizes to match the canonical capability name."""
        pm = _problem_model(caps=[
            {"name": "my-test-capability", "status": "working"},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "My Test Capability", "dependent_count": 2},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert len(result) == 1
        assert result[0]["proof_status"] == "unproven"

    def test_backing_paths_shows_partial_not_unproven(self):
        """A demanded capability with backing_paths but no evidence_refs
        classifies as 'partial', not 'unproven', in the intersection."""
        pm = _problem_model(caps=[
            {"name": "backed-cap", "status": "working",
             "backing_paths": ["backend/app/some_module.py"]},
        ])
        demand = _demand_result(demands=[
            {"kind": "capability", "ref": "backed-cap", "dependent_count": 2},
        ])
        result = compute_unproven_demanded_capabilities(demand, pm)
        assert len(result) == 1
        assert result[0]["ref"] == "backed-cap"
        assert result[0]["proof_status"] == "partial"


# ── PLATFORM_WORKING_CAPABILITIES filter ─────────────────────────────────────


@pytest.mark.asyncio
async def test_platform_working_capability_filtered_from_demand(db_session):
    """Cancelled dependents waiting on a platform-registered working capability
    do not appear in the missing-foundation demand surface."""
    from app.capability_registry import PLATFORM_WORKING_CAPABILITIES

    project = await _make_project(db_session)
    now = _utcnow()
    platform_cap = next(iter(PLATFORM_WORKING_CAPABILITIES))

    cancelled = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_NEVER_SATISFIABLE,
                "need": {"kind": "capability", "ref": platform_cap},
                "direction_id": "d-test",
                "at": (now - timedelta(hours=1)).isoformat(),
                MISSING_FOUNDATION_MARKER_KEY: True,
            }
        },
    )
    db_session.add(cancelled)
    await db_session.commit()

    result = await collect_missing_capability_demand(db_session, project.id, now=now)
    assert result["total"] == 0, (
        f"Platform capability '{platform_cap}' should be filtered from demand"
    )
    assert result["demands"] == []
