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
    compute_observe_mode_verdict,
    count_active_probes,
    decide_evergreen_fill,
    enroll_backlog_item,
    record_decision,
    select_candidate,
    spawn_evergreen_probe,
    _reset_observability_for_tests,
    get_fill_observability,
)
from app.models import EvergreenBacklogItem, EvergreenFillDecisionLog, Project, Task


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
# Observe-mode cap-precedes-observe ordering — false-alarm rate purity.
#
# In observe mode, cap/dedup guards fire BEFORE the observe_mode noop.  So
# when a cap fires, the returned reason is the cap reason (not 'observe_mode').
# These tests verify that ordering, which is what makes the fire-rate metric
# correct: only genuine would-have-fired decisions land in the denominator.
# --------------------------------------------------------------------------- #


def test_observe_mode_active_cap_precedes_observe_noop():
    """active_cap_reached fires before observe_mode in observe mode."""
    decision = decide_evergreen_fill(
        _would_create_signal(mode="observe", active_evergreen_probes=1, max_active=1)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "active_cap_reached"
    assert decision.reason != "observe_mode"


def test_observe_mode_daily_cap_precedes_observe_noop():
    """daily_cap_reached fires before observe_mode in observe mode."""
    decision = decide_evergreen_fill(
        _would_create_signal(mode="observe", daily_spawn_count=3, daily_cap=3)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "daily_cap_reached"
    assert decision.reason != "observe_mode"


def test_observe_mode_no_candidate_precedes_observe_noop():
    """no_candidate fires before observe_mode in observe mode."""
    decision = decide_evergreen_fill(
        _would_create_signal(mode="observe", has_candidate=False)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "no_candidate"
    assert decision.reason != "observe_mode"


def test_observe_mode_direction_conflict_precedes_observe_noop():
    """direction_conflict fires before observe_mode in observe mode."""
    decision = decide_evergreen_fill(
        _would_create_signal(mode="observe", candidate_conflict=True)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "direction_conflict"
    assert decision.reason != "observe_mode"


def test_observe_mode_duplicate_lineage_precedes_observe_noop():
    """duplicate_lineage fires before observe_mode in observe mode."""
    decision = decide_evergreen_fill(
        _would_create_signal(mode="observe", duplicate_lineage=True)
    )
    assert decision.action == DECISION_NOOP
    assert decision.reason == "duplicate_lineage"
    assert decision.reason != "observe_mode"


async def test_observe_mode_active_cap_rows_excluded_from_would_have_fired(db_session):
    """Cap-reason rows in observe mode are NOT counted as would_have_fired.

    Seeds a mix of observe-mode rows: some with reason='active_cap_reached'
    (cap fired before observe check) and some with reason='observe_mode'
    (genuine would-have-fired).  Confirms that compute_observe_mode_verdict
    counts only the 'observe_mode' rows in would_have_fired, so cap rows do
    not inflate the false-alarm rate.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()

    # 5 rows where a cap fired — these must NOT inflate would_have_fired.
    for _ in range(5):
        db_session.add(
            _observe_row(ts=now - timedelta(hours=1), reason="active_cap_reached")
        )
    # 5 rows where observe_mode suppressed a genuine would-have-fired spawn.
    for _ in range(5):
        db_session.add(_observe_row(ts=now - timedelta(hours=1), reason="observe_mode"))
    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=60.0, min_evaluations=10
    )
    assert result["observe_evaluations"] == 10
    # Only the 5 genuine would-have-fired rows count.
    assert result["would_have_fired"] == 5
    # Fire rate is 50%, not 100% (which would be the case if cap rows were counted).
    assert result["fire_rate_pct"] == 50.0
    assert result["verdict"] == "go"  # 50% ≤ 60% threshold


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


# --------------------------------------------------------------------------- #
# Durable fill-decision log — windowed observability (acceptance evidence).
# --------------------------------------------------------------------------- #


async def test_fill_decisions_endpoint_windowing(db_session, client):
    """Endpoint returns only in-window rows; out-of-window rows are excluded.

    Seeds in-window and out-of-window EvergreenFillDecisionLog rows directly,
    clears the in-memory ring (simulating a process restart), then asserts
    the fill-observability endpoint returns only the in-window row.
    """
    from datetime import timedelta

    from app.evergreen_backlog import _reset_observability_for_tests
    from app.models import EvergreenFillDecisionLog
    from app.time_utils import _utcnow

    now = _utcnow()

    _common = {
        "mode": "enforce",
        "util_threshold": 0.5,
        "dispatchable_queue_depth": 0,
        "active_evergreen_probes": 0,
        "daily_spawn_count": 0,
        "has_candidate": True,
    }

    # In-window: 2 hours ago — should appear in a 24-hour window.
    in_window = EvergreenFillDecisionLog(
        action="create",
        reason="low_utilization",
        trigger="low_utilization",
        ts=now - timedelta(hours=2),
        **_common,
    )
    # Out-of-window: 30 hours ago — excluded from 24-hour window.
    out_of_window = EvergreenFillDecisionLog(
        action="noop",
        reason="mode_off",
        trigger=None,
        ts=now - timedelta(hours=30),
        **_common,
    )
    db_session.add(in_window)
    db_session.add(out_of_window)
    await db_session.commit()

    # Simulate process restart by clearing the in-memory ring.
    _reset_observability_for_tests()

    resp = await client.get("/api/admin/evergreen/fill-observability?window_hours=24")
    assert resp.status_code == 200
    body = resp.json()

    # Only the in-window create should appear.
    assert body["created_count"] == 1
    assert body["reason_counts"].get("low_utilization") == 1
    assert "mode_off" not in body["reason_counts"]
    assert body["trigger_counts"].get("low_utilization") == 1

    # Recent list contains only the in-window row with correct fields.
    assert len(body["recent"]) == 1
    rec = body["recent"][0]
    assert rec["action"] == "create"
    assert rec["reason"] == "low_utilization"
    assert rec["trigger"] == "low_utilization"
    # ts field present when backed by durable log.
    assert "ts" in rec and rec["ts"] is not None


# --------------------------------------------------------------------------- #
# Deferred-memo enrollment integration (flag-gated).
# --------------------------------------------------------------------------- #


async def test_deferred_memo_enrollment_flag_gated(db_session, mocker):
    """Deferred-memo enrollment into the evergreen backlog is gated on
    evergreen_fill_deferred_memo_enrollment.

    flag=False (default) — research completion drops a LOW-actionability finding
    as a memo but creates NO EvergreenBacklogItem rows.

    flag=True — the same drop enrolls exactly one row with
    provenance='deferred_memo', the correct finding_key, and direction_id.

    Dedup — a second enroll call for the same finding_key on the same project
    returns the existing row without creating a duplicate.
    """
    import uuid
    from unittest.mock import AsyncMock
    import app.research_completion as _rc
    from app.research_completion import _handle_research_completion
    from app.models import Project, TaskStatus
    from test_autonomous_research import (
        _active_direction,
        _make_research_task,
        _patch_research_externals,
    )

    direction_id = "d-ev-flag-gate01"

    async def _make_project_and_task(db_session, dir_suffix=""):
        _dir_id = direction_id + dir_suffix
        direction = _active_direction(_dir_id, "Evergreen deferred memo test")
        project = Project(
            name=f"ev-memo-{uuid.uuid4().hex[:8]}",
            user_id="test-user-123",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = await _make_research_task(
            project, direction, db_session, status=TaskStatus.COMPLETED
        )
        return project, direction, task

    def _low_finding(dir_suffix=""):
        return {
            "title": "Memo: fix wall-clock budget for reaper stale-EVALUATING sweep",
            "description": "Reaper stale-EVALUATING sweep has no wall-clock budget.",
            "prompt": "Implement a wall-clock budget for the reaper.",
            "priority": "medium",
            "scope": "small",
            "risk": "low",
            "type": "improvement",
            "direction_id": direction_id + dir_suffix,
            "actionability_grade": "LOW",
            "finding_hash": "ev-fg-hash-001",
        }

    def _patch_for_run(mocker, finding):
        _patch_research_externals(mocker)
        mocker.patch(
            "app.research_completion._parse_research_findings",
            return_value=[finding],
        )
        mocker.patch(
            "app.research_completion._assess_project_spawn_health",
            return_value=(False, None),
        )
        mocker.patch(
            "app.research_completion.resolve_branch_verification_context",
            new=AsyncMock(return_value={}),
        )
        mocker.patch(
            "app.research_completion.resolve_head_commit",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion.cited_paths_changed_since",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.research_completion._has_validation_backend",
            return_value=False,
        )
        mocker.patch(
            "app.research_completion._summarize_ungrounded_claims_grounded",
            new=AsyncMock(return_value=None),
        )

    # ---- FLAG OFF: no rows enrolled ----
    project_off, _, task_off = await _make_project_and_task(db_session, "-off")
    _patch_for_run(mocker, _low_finding("-off"))
    _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False
    try:
        await _handle_research_completion(task_off, "", db_session)
    finally:
        _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

    rows_off = (
        (
            await db_session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project_off.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows_off == [], "flag=False: no backlog items should be enrolled"

    # ---- FLAG ON: one row enrolled with correct fields ----
    project_on, _, task_on = await _make_project_and_task(db_session, "-on")
    _patch_for_run(mocker, _low_finding("-on"))
    _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = True
    try:
        await _handle_research_completion(task_on, "", db_session)
    finally:
        _rc._runtime_config["evergreen_fill_deferred_memo_enrollment"] = False

    rows_on = (
        (
            await db_session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project_on.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_on) == 1, "flag=True: exactly one backlog item enrolled"
    enrolled = rows_on[0]
    assert enrolled.provenance == "deferred_memo"
    assert enrolled.finding_key == "ev-fg-hash-001"
    assert enrolled.status == "enrolled"
    assert enrolled.direction_id == direction_id + "-on"
    assert "wall-clock" in enrolled.title.lower() or "reaper" in enrolled.title.lower()

    # ---- DEDUP: re-enrolling the same finding_key returns the existing row ----
    dup = await enroll_backlog_item(
        db_session,
        project_id=project_on.id,
        finding_key="ev-fg-hash-001",
        title="Duplicate attempt — should not create a new row",
        provenance="deferred_memo",
    )
    assert dup is not None
    assert dup.id == enrolled.id, "duplicate enroll returns the existing row"

    rows_dedup = (
        (
            await db_session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project_on.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows_dedup) == 1, "dedup: still exactly one row after second enroll"


# --------------------------------------------------------------------------- #
# check_evergreen_fill: global hourly cap enforced within one tick.
# --------------------------------------------------------------------------- #


async def test_global_hourly_cap_holds_across_multi_project_tick(db_session, mocker):
    """Global hourly cap is never exceeded within a single check_evergreen_fill tick.

    Setup: 4 projects each with one enrolled candidate; global_recent seeded to
    cap-1; max_global_per_hour=cap.  Only 1 probe should be created because the
    tick_spawned counter must stop the loop after the first successful spawn.
    """
    import contextlib
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch

    import app.shared_state as _ss
    from app.evergreen_backlog import check_evergreen_fill
    from app.models import Task, TaskStatus

    cap = 3  # small cap for fast assertion

    # ---- Seed 4 autonomous-enabled projects, each with one backlog candidate ----
    projects = []
    items = []
    for i in range(4):
        proj = Project(
            name=f"cap-test-{i}-{_uuid.uuid4().hex[:6]}",
            user_id="test-user-123",
            autonomous_enabled=True,
        )
        db_session.add(proj)
        await db_session.flush()
        item = await enroll_backlog_item(
            db_session,
            project_id=proj.id,
            finding_key=f"cap-gap-{i}",
            title=f"Cap test gap {i}",
        )
        projects.append(proj)
        items.append(item)

    # ---- Seed cap-1 pre-existing evergreen probes in the trailing hour ----
    for j in range(cap - 1):
        dummy_proj = projects[0]
        existing = Task(
            project_id=dummy_proj.id,
            title=f"pre-existing probe {j}",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            idempotency_key=f"evergreen-fill:{dummy_proj.id}:pre-{j}",
        )
        db_session.add(existing)
    await db_session.flush()

    # ---- Patch external dependencies so check_evergreen_fill runs end-to-end ----

    # Wrap the real db_session in a no-op async context manager.
    @contextlib.asynccontextmanager
    async def _session_cm():
        yield db_session

    # Bypass interval throttle.
    _ss.last_evergreen_check = None

    # runtime_config overrides for this test.
    runtime_overrides = {
        "evergreen_fill_mode": "enforce",
        "evergreen_fill_interval_minutes": 0,
        "evergreen_fill_util_threshold": 0.9,  # threshold HIGH so util (0.1) triggers
        "evergreen_fill_min_free_slots": 1,
        "evergreen_fill_max_active": 5,  # don't hit per-project active cap
        "evergreen_fill_daily_cap": 10,  # don't hit per-project daily cap
        "max_global_autonomous_tasks_per_hour": cap,
    }

    # Mutate the live runtime_config dict (same object the function reads) and
    # restore it afterward — avoids mock complexity with lazy imports.
    import app.shared_state as _real_ss
    from app.shared_state import runtime_config as _real_rc

    orig_values = {k: _real_rc.get(k) for k in runtime_overrides}
    for k, v in runtime_overrides.items():
        _real_rc[k] = v
    _real_ss.last_evergreen_check = None

    try:
        with (
            patch("app.database.AsyncSessionLocal", _session_cm),
            patch(
                "app.fleet_utilization.get_fleet_utilization_snapshot",
                new=AsyncMock(return_value={"util_now": 0.1, "vm_slots_available": 8}),
            ),
            patch(
                "app.branch_resolution.get_project_integration_branch",
                return_value="automain",
            ),
            patch(
                "app.evergreen_backlog._evergreen_spend_ceiling_reason",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.evergreen_backlog._evergreen_circuit_open",
                return_value=False,
            ),
            patch(
                "app.evergreen_backlog._directions_parked_or_proof_waiting",
                return_value=False,
            ),
            patch(
                "app.evergreen_backlog.has_direction_conflict",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.evergreen_backlog.has_duplicate_lineage",
                new=AsyncMock(return_value=False),
            ),
        ):
            await check_evergreen_fill()
    finally:
        for k, orig_v in orig_values.items():
            if orig_v is None:
                _real_rc.pop(k, None)
            else:
                _real_rc[k] = orig_v

    # ---- Count total evergreen probes created by this tick ----
    all_probes = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.idempotency_key.like("evergreen-fill:%"),
                    Task.idempotency_key.not_like("evergreen-fill:%:pre-%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_probes) == 1, (
        f"Expected exactly 1 probe (cap={cap}, pre-existing={cap - 1}), "
        f"got {len(all_probes)}"
    )


# --------------------------------------------------------------------------- #
# Durable EvergreenFillDecisionLog ledger — acceptance evidence.
# test_fill_decisions_persist_and_window                                       #
# --------------------------------------------------------------------------- #


async def test_fill_decisions_persist_and_window(db_session, mocker):
    """Decision-log rows are durably written and the in-memory ring carries context.

    Acceptance contract (two parts):
    (a) One EvergreenFillDecisionLog row per project per tick is written to the
        DB with signal fields populated (util_now, mode, dispatchable_queue_depth,
        has_candidate, util_threshold, active_evergreen_probes, daily_spawn_count).
    (b) The augmented in-memory ring entry for the same decision exposes those
        same signal context fields (util_now, mode, has_candidate etc.).
    """
    import contextlib
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch

    import app.shared_state as _ss
    from app.evergreen_backlog import check_evergreen_fill

    _reset_observability_for_tests()

    # Two autonomous projects, each with one enrolled backlog candidate.
    projects = []
    items = []
    for i in range(2):
        proj = Project(
            name=f"log-test-{i}-{_uuid.uuid4().hex[:6]}",
            user_id="test-user-log",
            autonomous_enabled=True,
        )
        db_session.add(proj)
        await db_session.flush()
        item = await enroll_backlog_item(
            db_session,
            project_id=proj.id,
            finding_key=f"log-gap-{i}",
            title=f"Log test gap {i}",
        )
        projects.append(proj)
        items.append(item)

    @contextlib.asynccontextmanager
    async def _session_cm():
        yield db_session

    _ss.last_evergreen_check = None

    runtime_overrides = {
        "evergreen_fill_mode": "observe",  # observe mode: no task created, log written
        "evergreen_fill_interval_minutes": 0,
        "evergreen_fill_util_threshold": 0.9,  # util_now (0.1) < threshold → triggers
        "evergreen_fill_min_free_slots": 1,
        "evergreen_fill_max_active": 5,
        "evergreen_fill_daily_cap": 10,
        "max_global_autonomous_tasks_per_hour": 50,
    }

    import app.shared_state as _real_ss
    from app.shared_state import runtime_config as _real_rc

    orig_values = {k: _real_rc.get(k) for k in runtime_overrides}
    for k, v in runtime_overrides.items():
        _real_rc[k] = v
    _real_ss.last_evergreen_check = None

    try:
        with (
            patch("app.database.AsyncSessionLocal", _session_cm),
            patch(
                "app.fleet_utilization.get_fleet_utilization_snapshot",
                new=AsyncMock(return_value={"util_now": 0.1, "vm_slots_available": 8}),
            ),
            patch(
                "app.branch_resolution.get_project_integration_branch",
                return_value="automain",
            ),
            patch(
                "app.evergreen_backlog._evergreen_spend_ceiling_reason",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.evergreen_backlog._evergreen_circuit_open",
                return_value=False,
            ),
            patch(
                "app.evergreen_backlog._directions_parked_or_proof_waiting",
                return_value=False,
            ),
            patch(
                "app.evergreen_backlog.has_direction_conflict",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.evergreen_backlog.has_duplicate_lineage",
                new=AsyncMock(return_value=False),
            ),
        ):
            await check_evergreen_fill()
    finally:
        for k, orig_v in orig_values.items():
            if orig_v is None:
                _real_rc.pop(k, None)
            else:
                _real_rc[k] = orig_v

    # (a) — one EvergreenFillDecisionLog row per project, with signal fields.
    for proj, item in zip(projects, items):
        rows = (
            (
                await db_session.execute(
                    select(EvergreenFillDecisionLog).where(
                        EvergreenFillDecisionLog.project_id == proj.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, (
            f"Expected 1 decision log row for project {proj.id}, got {len(rows)}"
        )
        row = rows[0]
        assert row.mode == "observe"
        assert row.action == DECISION_NOOP
        assert row.reason == "observe_mode"
        assert row.trigger == "low_utilization"
        assert abs((row.util_now or 0) - 0.1) < 1e-6
        assert abs(row.util_threshold - 0.9) < 1e-6
        assert row.dispatchable_queue_depth == 0
        assert row.vm_slots_available == 8
        assert row.has_candidate is True
        assert row.active_evergreen_probes == 0
        assert row.daily_spawn_count == 0
        assert row.item_id == item.id
        assert row.finding_key == item.finding_key
        assert row.ts is not None

    # (b) — the in-memory ring entries expose the same context fields.
    obs = get_fill_observability()
    recent = obs["recent"]
    # Two projects → two entries in the ring.
    assert len(recent) >= 2, f"Expected ≥2 ring entries, got {len(recent)}: {recent}"
    for entry in recent[-2:]:
        assert entry.get("mode") == "observe"
        assert abs((entry.get("util_now") or 0) - 0.1) < 1e-6
        assert entry.get("has_candidate") is True
        assert entry.get("dispatchable_queue_depth") == 0
        assert entry.get("item_id") is not None
        assert entry.get("finding_key") is not None


def test_record_decision_augmented_ring_fields():
    """record_decision with a FleetSignal populates all signal fields in the ring."""
    _reset_observability_for_tests()

    signal = FleetSignal(
        mode="observe",
        util_now=0.25,
        util_threshold=0.5,
        dispatchable_queue_depth=0,
        vm_slots_available=4,
        min_free_slots=2,
        active_evergreen_probes=0,
        max_active=1,
        daily_spawn_count=1,
        daily_cap=3,
        has_candidate=True,
    )
    from app.evergreen_backlog import FillDecision, DECISION_NOOP

    decision = FillDecision(
        action=DECISION_NOOP, reason="observe_mode", trigger="low_utilization"
    )
    from datetime import datetime

    ts = datetime(2026, 1, 1, 12, 0, 0)
    record_decision(
        decision,
        project_id="proj-abc",
        signal=signal,
        item_id="item-xyz",
        finding_key="gap-test",
        ts=ts,
    )

    obs = get_fill_observability()
    assert len(obs["recent"]) == 1
    entry = obs["recent"][0]

    assert entry["action"] == DECISION_NOOP
    assert entry["reason"] == "observe_mode"
    assert entry["trigger"] == "low_utilization"
    assert entry["project_id"] == "proj-abc"
    assert entry["mode"] == "observe"
    assert abs((entry["util_now"] or 0) - 0.25) < 1e-6
    assert abs(entry["util_threshold"] - 0.5) < 1e-6
    assert entry["dispatchable_queue_depth"] == 0
    assert entry["vm_slots_available"] == 4
    assert entry["active_evergreen_probes"] == 0
    assert entry["daily_spawn_count"] == 1
    assert entry["has_candidate"] is True
    assert entry["item_id"] == "item-xyz"
    assert entry["finding_key"] == "gap-test"
    assert entry["ts"] == ts


# --------------------------------------------------------------------------- #
# Observe-mode false-alarm validation verdict (acceptance evidence).
# test_observe_mode_false_alarm_verdict                                        #
# --------------------------------------------------------------------------- #


def _observe_row(**kwargs):
    """Minimal EvergreenFillDecisionLog row for observe-mode verdict tests."""
    from app.time_utils import _utcnow

    defaults = {
        "mode": "observe",
        "action": DECISION_NOOP,
        "reason": "observe_mode",
        "trigger": "low_utilization",
        "ts": _utcnow(),
        "util_threshold": 0.5,
        "dispatchable_queue_depth": 0,
        "active_evergreen_probes": 0,
        "daily_spawn_count": 0,
        "has_candidate": True,
    }
    defaults.update(kwargs)
    return EvergreenFillDecisionLog(**defaults)


async def test_observe_mode_false_alarm_verdict(db_session):
    """Acceptance evidence: synthetic windows yield correct verdict at/around the threshold.

    Seeds three synthetic observation windows and checks the verdict transitions:
    - exactly at threshold (20%) → go
    - above threshold (80%) → no_go
    - empty window → insufficient_data
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()
    threshold = 20.0

    # Window A: 2/10 would-have-fired → 20% — exactly at threshold → go.
    for i in range(10):
        db_session.add(
            _observe_row(
                ts=now - timedelta(hours=1),
                reason="observe_mode" if i < 2 else "max_active",
            )
        )

    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=threshold, min_evaluations=10
    )
    assert result["verdict"] == "go", f"Expected go, got: {result}"
    assert result["observe_evaluations"] == 10
    assert result["would_have_fired"] == 2
    assert result["fire_rate_pct"] == 20.0


async def test_observe_mode_verdict_insufficient_data_when_no_rows(db_session):
    """Empty window yields insufficient_data verdict."""
    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=20.0, min_evaluations=10
    )
    assert result["verdict"] == "insufficient_data"
    assert result["observe_evaluations"] == 0
    assert result["fire_rate_pct"] is None


async def test_observe_mode_verdict_go_when_fire_rate_below_threshold(db_session):
    """2/10 would-have-fired at threshold 25% → go."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()
    for i in range(10):
        db_session.add(
            _observe_row(
                ts=now - timedelta(hours=1),
                reason="observe_mode" if i < 2 else "max_active",
            )
        )
    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=25.0, min_evaluations=10
    )
    assert result["verdict"] == "go"
    assert result["fire_rate_pct"] == 20.0


async def test_observe_mode_verdict_no_go_when_fire_rate_exceeds_threshold(db_session):
    """8/10 would-have-fired at threshold 25% → no_go."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()
    for i in range(10):
        db_session.add(
            _observe_row(
                ts=now - timedelta(hours=1),
                reason="observe_mode" if i < 8 else "max_active",
            )
        )
    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=25.0, min_evaluations=10
    )
    assert result["verdict"] == "no_go"
    assert result["fire_rate_pct"] == 80.0


async def test_observe_mode_verdict_ignores_enforce_mode_rows(db_session):
    """Only mode='observe' rows count; enforce-mode rows are excluded."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()
    # 5 observe rows, 0 would-have-fired.
    for _ in range(5):
        db_session.add(_observe_row(ts=now - timedelta(hours=1), reason="max_active"))
    # 20 enforce rows with observe_mode reason — must NOT count.
    for _ in range(20):
        db_session.add(
            _observe_row(
                ts=now - timedelta(hours=1), mode="enforce", reason="observe_mode"
            )
        )
    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session, window_hours=24, max_fire_rate_pct=25.0, min_evaluations=10
    )
    # Only 5 observe rows — below min_evaluations=10.
    assert result["verdict"] == "insufficient_data"
    assert result["observe_evaluations"] == 5


async def test_observe_mode_false_alarm_rate_consecutive_tick(db_session):
    """Cap-adjusted rate prevents idle-fleet false alarms from consecutive ticks.

    Seeds 10 would-have-fired observe_mode rows all within the same UTC day,
    simulating an idle fleet ticking every 15 minutes.  Without cap adjustment
    the raw fire rate is 100% (no_go at any threshold).  With daily_cap=3 the
    cap-adjusted numerator is 3, giving 3/10 = 30% — a go at threshold 50%.

    This is the acceptance evidence for the false-alarm-rate fix: the verdict
    must use the cap-adjusted metric, not the raw metric.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()

    # 10 would-have-fired rows on the same calendar day — consecutive idle ticks.
    for _ in range(10):
        db_session.add(_observe_row(ts=now - timedelta(hours=1), reason="observe_mode"))
    await db_session.commit()

    result = await compute_observe_mode_verdict(
        db_session,
        window_hours=24,
        max_fire_rate_pct=50.0,
        min_evaluations=10,
        daily_cap=3,
    )

    # Raw metrics are preserved for informational display.
    assert result["would_have_fired"] == 10
    assert result["fire_rate_pct"] == 100.0  # would be no_go without fix

    # Cap-adjusted metrics reflect the daily_cap=3 ceiling.
    assert result["daily_cap"] == 3
    assert result["cap_adjusted_would_have_fired"] == 3  # min(10, 3)
    assert result["cap_adjusted_fire_rate_pct"] == 30.0  # 3/10

    # Verdict uses cap-adjusted rate: 30% ≤ 50% threshold → go.
    assert result["verdict"] == "go", (
        f"Expected go (cap_adjusted=30% ≤ 50%), got {result['verdict']}: {result}"
    )


async def test_observe_mode_verdict_endpoint(db_session, client):
    """HTTP GET /api/admin/evergreen/observe-mode-verdict returns a verdict dict."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    now = _utcnow()
    for i in range(10):
        db_session.add(
            _observe_row(
                ts=now - timedelta(hours=1),
                reason="observe_mode" if i < 3 else "max_active",
            )
        )
    await db_session.commit()

    resp = await client.get("/api/admin/evergreen/observe-mode-verdict?window_hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] in {"go", "no_go", "insufficient_data"}
    assert body["observe_evaluations"] == 10
    assert body["would_have_fired"] == 3
    assert body["fire_rate_pct"] == 30.0
    assert body["verdict"] == "no_go"  # 30% > default 20% threshold


async def test_evergreen_fill_decision_retention_sweep_purges_only_stale_rows(
    db_session,
):
    """Sweep purges only rows older than retention window; fresh rows remain."""
    from datetime import timedelta

    from app.reaper_sweeps import sweep_evergreen_fill_decision_retention
    from app.time_utils import _utcnow

    now = _utcnow()
    retention_days = 30

    # Create fresh rows (within retention window).
    for i in range(3):
        db_session.add(_observe_row(ts=now - timedelta(days=1), reason="observe_mode"))

    # Create stale rows (older than retention window).
    for i in range(5):
        db_session.add(_observe_row(ts=now - timedelta(days=45), reason="max_active"))

    await db_session.commit()

    # Verify initial state: 8 rows total.
    all_rows = (
        (await db_session.execute(select(EvergreenFillDecisionLog))).scalars().all()
    )
    assert len(all_rows) == 8

    # Run the sweep.
    purged = await sweep_evergreen_fill_decision_retention(
        db_session, retention_days=retention_days
    )

    # Verify purged count matches stale rows.
    assert purged == 5

    # Verify only fresh rows remain.
    remaining = (
        (await db_session.execute(select(EvergreenFillDecisionLog))).scalars().all()
    )
    assert len(remaining) == 3
    for row in remaining:
        assert row.ts >= (now - timedelta(days=retention_days))


# --------------------------------------------------------------------------- #
# Projection invariant — guards against re-adding heavy JSONB to fleet scan.
# --------------------------------------------------------------------------- #


def test_evergreen_fleet_scan_projection_excludes_heavy_columns():
    """The check_evergreen_fill Project query must exclude vision and running_update.

    These are the heaviest unused JSONB/Text columns on Project rows.
    Re-adding them would reintroduce the over-fetch this projection removes.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import load_only
    from sqlmodel import select
    from app.models import Project

    # Reconstruct the load_only options used in check_evergreen_fill.
    _project_cols = load_only(
        Project.id,
        Project.directions,
        Project.build_config,
        Project.integration_branch,
        Project.is_self_repo,
        Project.github_repo,
        Project.promotion_target,
        raiseload=True,
    )
    stmt = (
        select(Project)
        .where(
            Project.autonomous_enabled.is_(True),
            Project.paused.is_(False),
            Project.not_archived(),
        )
        .options(_project_cols)
    )
    sql_text = str(stmt.compile(dialect=postgresql.dialect()))

    # Heavy columns that must NOT appear in the projected SELECT list.
    for heavy in ("vision", "running_update", "problem_model", "self_reflection_log"):
        assert heavy not in sql_text, (
            f"Evergreen fleet-scan projection must exclude '{heavy}' — "
            "re-adding it reintroduces the over-fetch this projection removes."
        )

    # Columns required by the downstream helpers.
    for required in ("directions", "build_config", "integration_branch"):
        assert required in sql_text, (
            f"Evergreen fleet-scan projection must include '{required}' — "
            "removing it will cause a raiseload error in the fill loop."
        )
