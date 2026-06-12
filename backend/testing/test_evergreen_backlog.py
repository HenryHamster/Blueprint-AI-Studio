"""Tests for utilization-triggered evergreen backlog auto-fill (d-2877ddc7).

Covers:
* the pure decision matrix (acceptance evidence) — CREATE vs the closed-vocab
  no-op reasons, mode gating, triggers, caps, headroom, conflict/lineage;
* enrollment + dedup, candidate selection;
* the single Task-creating path (exactly one priority-10 evergreen-marked probe);
* the research_completion outcome adapter transitions;
* no speculative fix creation when a probe's finding is reproduce-on-HEAD absent.
"""

from sqlmodel import select

from app.evergreen_backlog import (
    DECISION_CREATE,
    DECISION_NOOP,
    EVERGREEN_FILL_FLAG,
    EVERGREEN_ITEM_CONTEXT_KEY,
    EVERGREEN_PROBE_PRIORITY,
    NOOP_REASONS,
    TRIGGER_REASONS,
    FleetSignal,
    apply_research_outcome,
    backlog_summary,
    count_active_probes,
    decide_evergreen_fill,
    enroll_backlog_item,
    select_candidate,
    spawn_evergreen_probe,
)
from app.models import EvergreenBacklogItem, Project, Task


# --------------------------------------------------------------------------- #
# Pure decision matrix (no DB) — the acceptance evidence.
# --------------------------------------------------------------------------- #


def _would_create_signal(**overrides) -> FleetSignal:
    """A signal that, in enforce mode, yields a CREATE — mutate per case."""
    base = {
        "mode": "enforce",
        "util_now": 0.1,
        "util_threshold": 0.5,
        "dispatchable_queue_depth": 0,
        "vm_slots_available": 8,
        "min_free_slots": 2,
        "active_evergreen_probes": 0,
        "max_active": 1,
        "daily_spawn_count": 0,
        "daily_cap": 3,
        "has_candidate": True,
        "parked_proof_waiting": False,
        "project_eligible": True,
        "spend_ceiling_reached": False,
        "circuit_open": False,
        "global_rate_limited": False,
        "candidate_conflict": False,
        "duplicate_lineage": False,
    }
    base.update(overrides)
    return FleetSignal(**base)


def test_enforce_low_util_empty_queue_one_candidate_creates():
    decision = decide_evergreen_fill(_would_create_signal())
    assert decision.action == DECISION_CREATE
    assert decision.reason == "low_utilization"
    assert decision.trigger == "low_utilization"
    assert decision.should_create is True


def test_parked_proof_waiting_triggers_create_even_when_util_not_low():
    decision = decide_evergreen_fill(
        _would_create_signal(util_now=0.9, parked_proof_waiting=True)
    )
    assert decision.action == DECISION_CREATE
    assert decision.trigger == "parked_proof_waiting"


def test_observe_mode_creates_nothing_but_records_trigger():
    decision = decide_evergreen_fill(_would_create_signal(mode="observe"))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "observe_mode"
    # Records what WOULD have triggered, for observability.
    assert decision.trigger == "low_utilization"


def test_mode_off_is_inert():
    decision = decide_evergreen_fill(_would_create_signal(mode="off"))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "mode_off"


def test_busy_queue_no_op():
    decision = decide_evergreen_fill(_would_create_signal(dispatchable_queue_depth=3))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "busy_queue"


def test_high_utilization_no_op():
    decision = decide_evergreen_fill(
        _would_create_signal(util_now=0.8, parked_proof_waiting=False)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "high_utilization"


def test_no_headroom_no_op():
    decision = decide_evergreen_fill(
        _would_create_signal(vm_slots_available=1, min_free_slots=2)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "no_headroom"


def test_saturated_active_cap_no_op():
    decision = decide_evergreen_fill(
        _would_create_signal(active_evergreen_probes=1, max_active=1)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "active_cap_reached"


def test_daily_cap_no_op():
    decision = decide_evergreen_fill(
        _would_create_signal(daily_spawn_count=3, daily_cap=3)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "daily_cap_reached"


def test_no_candidate_no_op():
    decision = decide_evergreen_fill(_would_create_signal(has_candidate=False))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "no_candidate"


def test_direction_conflict_no_op():
    decision = decide_evergreen_fill(_would_create_signal(candidate_conflict=True))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "direction_conflict"


def test_duplicate_lineage_no_op():
    decision = decide_evergreen_fill(_would_create_signal(duplicate_lineage=True))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "duplicate_lineage"


def test_project_ineligible_no_op():
    decision = decide_evergreen_fill(_would_create_signal(project_eligible=False))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "project_ineligible"


def test_spend_ceiling_no_op():
    decision = decide_evergreen_fill(_would_create_signal(spend_ceiling_reached=True))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "spend_ceiling"


def test_circuit_open_no_op():
    decision = decide_evergreen_fill(_would_create_signal(circuit_open=True))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "circuit_open"


def test_global_rate_limited_no_op():
    decision = decide_evergreen_fill(_would_create_signal(global_rate_limited=True))
    assert decision.action == DECISION_NOOP
    assert decision.reason == "global_rate_limited"


def test_every_decision_reason_is_closed_vocabulary():
    # Exhaustively walk a few signals and assert reasons stay in the vocab.
    for sig in (
        _would_create_signal(),
        _would_create_signal(mode="off"),
        _would_create_signal(dispatchable_queue_depth=1),
        _would_create_signal(util_now=0.99),
        _would_create_signal(has_candidate=False),
    ):
        d = decide_evergreen_fill(sig)
        if d.action == DECISION_CREATE:
            assert d.reason in TRIGGER_REASONS
        else:
            assert d.reason in NOOP_REASONS


# --------------------------------------------------------------------------- #
# DB-backed: enrollment / dedup / selection.
# --------------------------------------------------------------------------- #


async def _make_project(db_session, **kw) -> Project:
    project = Project(
        name=kw.pop("name", "evergreen-proj"), user_id="test-user-123", **kw
    )
    db_session.add(project)
    await db_session.flush()
    return project


async def test_enroll_is_idempotent_by_finding_key(db_session):
    project = await _make_project(db_session)
    a = await enroll_backlog_item(
        db_session,
        project_id=project.id,
        finding_key="gap-xyz",
        title="Some latent gap",
    )
    b = await enroll_backlog_item(
        db_session,
        project_id=project.id,
        finding_key="gap-xyz",
        title="Some latent gap (again)",
    )
    assert a is not None and b is not None
    assert a.id == b.id  # deduped — one ledger row
    rows = (
        (
            await db_session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_enroll_reopens_retired_item(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-r", title="t"
    )
    item.status = "retired"
    item.outcome = "retired_attempts_exhausted"
    await db_session.flush()
    again = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-r", title="t"
    )
    assert again.id == item.id
    assert again.status == "enrolled"
    assert again.outcome is None


async def test_select_candidate_skips_attempt_exhausted(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-a", title="t"
    )
    assert (await select_candidate(db_session, project_id=project.id)).id == item.id
    item.attempts = 3
    await db_session.flush()
    assert await select_candidate(db_session, project_id=project.id) is None


# --------------------------------------------------------------------------- #
# DB-backed: the single Task-creating path.
# --------------------------------------------------------------------------- #


async def test_spawn_creates_exactly_one_priority_10_evergreen_probe(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-spawn", title="Probe me"
    )
    task = await spawn_evergreen_probe(
        db_session, project_id=project.id, item=item, base_branch="automain"
    )
    assert task is not None
    # Exactly one probe.
    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    # Priority 10 — the acceptance contract.
    assert task.priority == EVERGREEN_PROBE_PRIORITY == 10
    # Marked evergreen in context + payload.
    assert task.context.get(EVERGREEN_FILL_FLAG) is True
    assert task.context.get(EVERGREEN_ITEM_CONTEXT_KEY) == str(item.id)
    assert task.first_payload.get("type") == "research"
    assert task.first_payload.get(EVERGREEN_FILL_FLAG) is True
    # Ledger row advanced to spawned + linked + attempt bumped.
    assert item.status == "spawned"
    assert item.spawned_task_id == task.id
    assert item.attempts == 1
    assert await count_active_probes(db_session, project_id=project.id) == 1


async def test_spawn_idempotency_blocks_second_probe(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-once", title="once"
    )
    t1 = await spawn_evergreen_probe(
        db_session, project_id=project.id, item=item, base_branch="automain"
    )
    assert t1 is not None
    # Re-spawn with the same item id reuses the idempotency key → no second probe.
    item.status = "enrolled"  # pretend we tried again
    t2 = await spawn_evergreen_probe(
        db_session, project_id=project.id, item=item, base_branch="automain"
    )
    assert t2 is None


# --------------------------------------------------------------------------- #
# DB-backed: research_completion outcome adapter.
# --------------------------------------------------------------------------- #


async def test_outcome_advanced_when_children_spawned(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-adv", title="t"
    )
    item.status = "spawned"
    await db_session.flush()
    new_status = await apply_research_outcome(
        db_session,
        item_id=str(item.id),
        impl_spawned=2,
        reproduce_rejected=0,
        surviving=2,
    )
    assert new_status == "advanced"
    assert item.outcome == "children_spawned"


async def test_outcome_verified_stale_when_reproduce_absent_no_fix_created(db_session):
    """No speculative fix creation when the finding does not reproduce on HEAD."""
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-stale", title="t"
    )
    item.status = "spawned"
    await db_session.flush()
    new_status = await apply_research_outcome(
        db_session,
        item_id=str(item.id),
        impl_spawned=0,
        reproduce_rejected=1,
        surviving=0,
    )
    assert new_status == "verified_stale"
    assert item.outcome == "reproduce_absent"
    # The adapter created NO fix/create task — only the (already-counted) probe.
    non_probe = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.priority != EVERGREEN_PROBE_PRIORITY,
                )
            )
        )
        .scalars()
        .all()
    )
    assert non_probe == []


async def test_outcome_requeue_then_retire_on_environmental_failure(db_session):
    project = await _make_project(db_session)
    item = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="gap-env", title="t"
    )
    item.status = "spawned"
    item.attempts = 1
    await db_session.flush()
    # attempts < max → requeued to enrolled.
    s1 = await apply_research_outcome(
        db_session,
        item_id=str(item.id),
        impl_spawned=0,
        reproduce_rejected=0,
        surviving=0,
        environmental_failure=True,
        max_attempts=3,
    )
    assert s1 == "enrolled"
    assert item.outcome == "requeued_environmental"
    # attempts exhausted → retired.
    item.attempts = 3
    item.status = "spawned"
    await db_session.flush()
    s2 = await apply_research_outcome(
        db_session,
        item_id=str(item.id),
        impl_spawned=0,
        reproduce_rejected=0,
        surviving=0,
        environmental_failure=True,
        max_attempts=3,
    )
    assert s2 == "retired"
    assert item.outcome == "retired_attempts_exhausted"


async def test_backlog_summary_counts_by_status(db_session):
    project = await _make_project(db_session)
    await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="k1", title="t"
    )
    i2 = await enroll_backlog_item(
        db_session, project_id=project.id, finding_key="k2", title="t"
    )
    i2.status = "spawned"
    await db_session.flush()
    summary = await backlog_summary(db_session, project_id=project.id)
    assert summary["counts"]["enrolled"] == 1
    assert summary["counts"]["spawned"] == 1
    assert summary["counts"]["advanced"] == 0


# --------------------------------------------------------------------------- #
# Admin settings exposure / validation + admin observability endpoints.
# --------------------------------------------------------------------------- #


async def test_settings_get_exposes_evergreen_knobs_with_defaults(client):
    resp = await client.get("/api/admin/settings")
    assert resp.status_code == 200
    body = resp.json()
    # Backward compatibility: defaults present and OFF by default.
    assert body["evergreen_fill_mode"] == "off"
    assert body["evergreen_fill_interval_minutes"] == 15
    assert body["evergreen_fill_util_threshold"] == 0.5
    assert body["evergreen_fill_min_free_slots"] == 2
    assert body["evergreen_fill_max_active"] == 1
    assert body["evergreen_fill_daily_cap"] == 3
    assert body["evergreen_fill_deferred_memo_enrollment"] is False


async def test_settings_patch_mode_roundtrip_and_validation(client):
    ok = await client.patch(
        "/api/admin/settings", json={"evergreen_fill_mode": "enforce"}
    )
    assert ok.status_code == 200
    got = await client.get("/api/admin/settings")
    assert got.json()["evergreen_fill_mode"] == "enforce"

    bad = await client.patch(
        "/api/admin/settings", json={"evergreen_fill_mode": "sometimes"}
    )
    assert bad.status_code == 400


async def test_settings_patch_numeric_bounds_rejected(client):
    bad_thresh = await client.patch(
        "/api/admin/settings", json={"evergreen_fill_util_threshold": 2.0}
    )
    assert bad_thresh.status_code == 400
    bad_cap = await client.patch(
        "/api/admin/settings", json={"evergreen_fill_daily_cap": -1}
    )
    assert bad_cap.status_code == 400
    bad_bool = await client.patch(
        "/api/admin/settings",
        json={"evergreen_fill_deferred_memo_enrollment": "true"},
    )
    assert bad_bool.status_code == 400


async def test_admin_enroll_list_and_observability_endpoints(client, db_session):
    project = await _make_project(db_session, name="endpoint-proj")
    await db_session.commit()

    # Enroll via admin endpoint (idempotent).
    enroll = await client.post(
        "/api/admin/evergreen/backlog",
        json={
            "project_id": str(project.id),
            "finding_key": "ep-gap",
            "title": "Endpoint gap",
        },
    )
    assert enroll.status_code == 200, enroll.text
    item_id = enroll.json()["item"]["id"]

    dup = await client.post(
        "/api/admin/evergreen/backlog",
        json={
            "project_id": str(project.id),
            "finding_key": "ep-gap",
            "title": "Endpoint gap again",
        },
    )
    assert dup.status_code == 200
    assert dup.json()["item"]["id"] == item_id  # deduped

    # Empty finding_key rejected.
    bad = await client.post(
        "/api/admin/evergreen/backlog",
        json={"project_id": str(project.id), "finding_key": "  ", "title": "x"},
    )
    assert bad.status_code == 400

    listing = await client.get(f"/api/admin/evergreen/backlog?project_id={project.id}")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    bad_status = await client.get(
        f"/api/admin/evergreen/backlog?project_id={project.id}&status=bogus"
    )
    assert bad_status.status_code == 400

    summary = await client.get(
        f"/api/admin/evergreen/backlog-summary?project_id={project.id}"
    )
    assert summary.status_code == 200
    assert summary.json()["counts"]["enrolled"] == 1

    obs = await client.get("/api/admin/evergreen/fill-observability")
    assert obs.status_code == 200
    obs_body = obs.json()
    assert "reason_counts" in obs_body
    assert "recent" in obs_body
